"""聊天语义找回的 AstrBot 适配层：Embedding Provider 解析、只读检索与后台维护。

本模块把纯标准库索引（:mod:`core.semantic`）接到 AstrBot 与插件本地档案上，
并严格区分两条路径：

* **读路径** :class:`SemanticReader`：普通聊天只读本地索引并计算一次查询向量；
  只持有索引、Provider 解析器与配置读取函数，**不持有 PhoneService**，因此结构
  上无法访问 ADB/设备/后台任务；
* **写路径** :class:`SemanticMaintainer`：后台任务里读取本地学习档案与浏览日志，分批调用 embedding 并原子写入新一代索引；只在插件启动空闲、写入学习/
  日记后（防抖）与周期检查时运行，永不从聊天 hook 触发。

失败全部降级：缺少 Embedding Provider、索引缺失/损坏、查询超时或异常都只跳过
本轮注入（warning 只记录一次），不影响对话主流程。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

from .failover_adapter import EmbeddingModelChanged
from .identity import PLUGIN_NAME
from .semantic import (
    DEFAULT_DOC_CHARS,
    DEFAULT_INDEX_MAX_DOCS,
    DEFAULT_MAX_CHARS,
    DEFAULT_MIN_SCORE,
    DEFAULT_QUERY_CHARS,
    DEFAULT_TOP_K,
    SemanticBuildError,
    SemanticIndex,
    SemanticSnapshot,
    build_index,
    collect_documents,
    format_hits,
    normalize_vector,
    provider_key,
)

# ---- 后台维护参数（测试可注入覆盖） --------------------------------------

# 单批 embedding 的等待上限（构建在后台执行，长文本/慢供应商也不阻塞聊天）。
BUILD_EMBED_TIMEOUT_SECONDS = 120.0
# 插件启动后先空闲一段时间再建索引，避免与初始化/设备连接争用。
STARTUP_DELAY_SECONDS = 5.0
# 写入学习/日记后的防抖窗口：短时间多次写入合并为一次增量构建。
DEBOUNCE_SECONDS = 3.0
# 构建失败后的指数退避（60s → 30min），收敛重试且不刷告警。
FAILURE_BASE_BACKOFF_SECONDS = 60.0
FAILURE_MAX_BACKOFF_SECONDS = 1800.0
# 读路径遇到损坏索引后的失败缓存，避免每轮聊天都重读坏文件。
READER_FAILURE_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class ResolvedEmbedding:
    """已解析的 Embedding Provider（含索引身份键）。"""

    provider: Any
    provider_id: str
    model: str
    dim: int

    @property
    def key(self) -> str:
        """provider/模型/维度身份键；索引与之一致才可比较相似度。"""
        return provider_key(self.provider_id, self.model, self.dim)


async def query_embedding(provider: ResolvedEmbedding, text: str) -> Any:
    """查询一条文本的向量（兼容只有批量接口的 Provider 实现）。"""
    getter = getattr(provider.provider, "get_embedding", None)
    if callable(getter):
        return await getter(text)
    get_many = getattr(provider.provider, "get_embeddings", None)
    if not callable(get_many):
        raise SemanticBuildError("Embedding Provider 缺少查询 embedding 的接口")
    result = await get_many([text])
    if not isinstance(result, (list, tuple)) or not result:
        raise SemanticBuildError("Embedding Provider 返回空向量结果")
    return result[0]


def _provider_id_of(provider: Any) -> str:
    """从 Provider 实例提取稳定 ID（优先 provider_config.id）。"""
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict):
        for name in ("id", "provider_id"):
            value = config.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for name in ("provider_id", "id"):
        try:
            value = getattr(provider, name, None)
        except Exception:  # noqa: BLE001 - 属性访问异常按无 ID 处理
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return type(provider).__name__


def _provider_model_of(provider: Any) -> str:
    """从 Provider 实例提取模型名（get_model 失败时回退 provider_config）。"""
    get_model = getattr(provider, "get_model", None)
    if callable(get_model):
        try:
            value = get_model()
            if inspect.isawaitable(value):
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:  # noqa: BLE001 - 模型名只用于身份键与日志
            value = None
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict):
        for name in ("embedding_model", "model", "model_name"):
            value = config.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _is_embedding_provider(provider: Any) -> bool:
    """鸭子类型判断是否具备 embedding 能力（不依赖具体 AstrBot 类）。"""
    return callable(getattr(provider, "get_embedding", None)) or callable(
        getattr(provider, "get_embeddings", None)
    )


class SemanticEmbeddingResolver:
    """解析配置指定的 Embedding Provider；留空时自动选择已配置的 Provider。

    * 显式 ID 不存在/不是 Embedding 类型：warning 一次并停用（不静默换模型，
      避免索引维度/语义错配）；
    * 留空：优先使用 AstrBot 暴露的默认 embedding 标记（若版本提供），否则按
      Provider ID 排序取第一个，保证选择稳定可日志；
    * 解析结果按「请求的 ID」缓存；插件重载后随之重新解析。

    ``label``/``config_key`` 用于区分不同索引（聊天找回 / App 经验召回）的告警文案。
    """

    def __init__(
        self,
        context: Any,
        *,
        label: str = "语义找回",
        config_key: str = "recall.embedding_provider_id",
        failover=None,
        slot="embedding",
    ) -> None:
        self._context = context
        self._failover, self._slot = failover, slot
        self._label = str(label or "语义找回")
        self._config_key = str(config_key or "recall.embedding_provider_id")
        self._cached_request: str | None = None
        self._cached: ResolvedEmbedding | None = None
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message)

    def resolve(self, provider_id: str = "") -> ResolvedEmbedding | None:
        """解析 Provider；不可用时返回 None（并记录一次可诊断的 warning）。"""
        requested = str(provider_id or "").strip()
        if (
            self._failover is not None
            and self._failover.policy(self._slot).mode != "disabled"
        ):
            from .failover_adapter import PinnedEmbedding

            original = self._resolve_uncached(requested)
            primary = requested or (original.provider_id if original else "")
            chosen = self._failover.current(self._slot, primary)
            resolved = self._resolve_uncached(chosen) if chosen else original
            if resolved is None:
                # 缺失主模型也允许尝试已选备用；身份先固定为第一个有效候选。
                for candidate in self._failover.candidates(self._slot, primary)[1:]:
                    resolved = self._resolve_uncached(candidate)
                    if resolved is not None:
                        break
            if resolved is None:
                return None
            return ResolvedEmbedding(
                PinnedEmbedding(self._failover, self._slot, primary, resolved.provider),
                resolved.provider_id,
                resolved.model,
                resolved.dim,
            )
        if self._cached_request == requested:
            return self._cached
        resolved = self._resolve_uncached(requested)
        self._cached_request = requested
        self._cached = resolved
        return resolved

    def _resolve_uncached(self, requested: str) -> ResolvedEmbedding | None:
        if requested:
            provider = self._get_provider_by_id(requested)
            if provider is None:
                self._warn_once(
                    f"missing:{requested}",
                    f"{self._label}：找不到 Embedding Provider「{requested}」，"
                    f"已停用（请检查 {self._config_key}）",
                )
                return None
            if not _is_embedding_provider(provider):
                self._warn_once(
                    f"type:{requested}",
                    f"{self._label}：Provider「{requested}」不是 Embedding 类型，"
                    "已停用（请填写 Embedding Provider ID）",
                )
                return None
            return self._build(provider, requested)

        providers = [
            item
            for item in self._all_embedding_providers()
            if _is_embedding_provider(item)
        ]
        if not providers:
            self._warn_once(
                "no-provider",
                f"{self._label}：AstrBot 未配置可用的 Embedding Provider，已停用"
                f"（请在 AstrBot 配置 Embedding Provider，或填写 {self._config_key}）",
            )
            return None
        chosen = self._pick_provider(providers)
        chosen_id = _provider_id_of(chosen)
        logger.info("%s：自动选择 Embedding Provider「%s」", self._label, chosen_id)
        return self._build(chosen, chosen_id)

    def _get_provider_by_id(self, provider_id: str) -> Any:
        getter = getattr(self._context, "get_provider_by_id", None)
        if not callable(getter):
            return None
        try:
            return getter(provider_id)
        except Exception:  # noqa: BLE001 - Provider 版本差异按不可用处理
            return None

    def _all_embedding_providers(self) -> list[Any]:
        getter = getattr(self._context, "get_all_embedding_providers", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:  # noqa: BLE001 - Provider 版本差异按不可用处理
            return []

    def _pick_provider(self, providers: list[Any]) -> Any:
        """选择默认 Provider：默认标记优先，否则按 ID 排序取第一个。"""
        default_id = self._default_embedding_id()
        if default_id:
            for provider in providers:
                if _provider_id_of(provider) == default_id:
                    return provider
        flagged = [
            provider
            for provider in providers
            if self._has_default_flag(provider)
        ]
        if len(flagged) == 1:
            return flagged[0]
        return sorted(
            providers, key=lambda item: (_provider_id_of(item), _provider_model_of(item))
        )[0]

    @staticmethod
    def _has_default_flag(provider: Any) -> bool:
        """读取 Provider 上可能存在的默认标记（不同 AstrBot 版本兼容）。"""
        try:
            if getattr(provider, "is_default", False):
                return True
        except Exception:  # noqa: BLE001
            pass
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            return bool(config.get("default") is True or config.get("is_default") is True)
        return False

    def _default_embedding_id(self) -> str:
        """读取 AstrBot 可能暴露的默认 Embedding Provider ID（没有则返回空串）。"""
        manager = getattr(self._context, "provider_manager", None)
        for holder in (manager, self._context):
            if holder is None:
                continue
            for name in (
                "default_embedding_provider_id",
                "embedding_provider_id",
            ):
                try:
                    value = getattr(holder, name, "")
                except Exception:  # noqa: BLE001
                    value = ""
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _build(self, provider: Any, provider_id: str) -> ResolvedEmbedding | None:
        try:
            dim = int(provider.get_dim())
        except Exception as exc:  # noqa: BLE001 - 维度不可用视为 Provider 不可用
            self._warn_once(
                f"dim:{provider_id}",
                f"{self._label}：无法读取 Embedding Provider「{provider_id}」的维度"
                f"（{exc}），已停用",
            )
            return None
        if dim <= 0:
            self._warn_once(
                f"dim-zero:{provider_id}",
                f"{self._label}：Embedding Provider「{provider_id}」维度非法（{dim}），"
                "已停用",
            )
            return None
        return ResolvedEmbedding(
            provider=provider,
            provider_id=provider_id,
            model=_provider_model_of(provider),
            dim=dim,
        )


class SemanticReader:
    """普通聊天的只读检索路径。

    只持有索引、Provider 解析器与 RecallSettings 读取函数；不持有 PhoneService，
    不创建后台任务、不写任何文件，也不读取学习/日记/App 经验原始档案。
    """

    def __init__(
        self,
        index: SemanticIndex,
        resolver: SemanticEmbeddingResolver,
        config_getter: Callable[[], Any],
    ) -> None:
        self._index = index
        self._resolver = resolver
        self._config_getter = config_getter
        self._cache: tuple[tuple[int, int, int], SemanticSnapshot] | None = None
        self._failed_key = ""
        self._failed_signature: tuple[int, int, int] | None = None
        self._failed_until = 0.0
        self._debug_keys: set[str] = set()

    def _debug_once(self, key: str, message: str, *args: Any) -> None:
        if key in self._debug_keys:
            return
        self._debug_keys.add(key)
        logger.debug(message, *args)

    async def render(self, query: str) -> str:
        """按查询文本检索并渲染中文注入文本；任何失败都返回空串。"""
        try:
            config = self._config_getter()
        except Exception:  # noqa: BLE001 - 配置读取失败不影响聊天
            return ""
        if not bool(getattr(config, "enabled", False)):
            return ""
        text = " ".join(str(query or "").split())
        if not text:
            return ""
        text = text[:DEFAULT_QUERY_CHARS]
        provider = self._resolver.resolve(
            str(getattr(config, "embedding_provider_id", "") or "")
        )
        if provider is None:
            return ""
        try:
            timeout = float(getattr(config, "timeout_seconds", 3) or 3)
        except (TypeError, ValueError):
            timeout = 3.0
        try:
            return await asyncio.wait_for(
                self._render_locked(text, config, provider),
                timeout=max(0.2, timeout),
            )
        except asyncio.TimeoutError:
            self._debug_once(
                "timeout",
                "语义找回：本轮读取索引/查询向量超时（%.1fs），已跳过",
                timeout,
            )
            return ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 找回失败绝不影响对话
            self._debug_once(
                f"error:{type(exc).__name__}",
                "语义找回：本轮检索失败，已跳过：%s",
                exc,
            )
            return ""

    async def _render_locked(
        self, text: str, config: Any, provider: ResolvedEmbedding
    ) -> str:
        snapshot = await self._snapshot(provider)
        if snapshot is None or snapshot.count == 0:
            return ""
        try:
            raw_vector = await self._query_embedding(provider, text)
        except Exception as exc:  # noqa: BLE001 - 查询失败本轮跳过
            self._debug_once(
                f"embed:{type(exc).__name__}",
                "语义找回：查询向量计算失败，已跳过：%s",
                exc,
            )
            return ""
        query_vector = normalize_vector(raw_vector)
        if not query_vector or len(query_vector) != snapshot.dim:
            self._debug_once(
                "dim-mismatch",
                "语义找回：查询向量维度与索引不一致（索引可能正在重建），本轮跳过",
            )
            return ""
        try:
            top_k = int(getattr(config, "top_k", DEFAULT_TOP_K))
            min_score = float(getattr(config, "min_score", DEFAULT_MIN_SCORE))
            max_chars = int(getattr(config, "max_chars", DEFAULT_MAX_CHARS))
        except (TypeError, ValueError):
            top_k, min_score, max_chars = (
                DEFAULT_TOP_K,
                DEFAULT_MIN_SCORE,
                DEFAULT_MAX_CHARS,
            )
        hits = await asyncio.to_thread(
            snapshot.search, query_vector, top_k=top_k, min_score=min_score
        )
        return format_hits(hits, max_chars=max_chars)

    @staticmethod
    async def _query_embedding(provider: ResolvedEmbedding, text: str) -> Any:
        """查询一条文本的向量（兼容只有批量接口的 Provider 实现）。"""
        return await query_embedding(provider, text)

    async def _snapshot(
        self, provider: ResolvedEmbedding
    ) -> SemanticSnapshot | None:
        """读当前索引快照（按 manifest 文件标识缓存；损坏时短暂失败缓存）。"""
        try:
            stat = self._index.manifest_path.stat()
        except OSError:
            return None
        # 索引切换走 os.replace（inode 变化）；同时带上 size 以兼容粗粒度 mtime
        # 的文件系统与手工破坏 manifest 的场景。
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        cached = self._cache
        if (
            cached is not None
            and cached[0] == signature
            and cached[1].provider_key == provider.key
        ):
            return cached[1]
        now = time.monotonic()
        if (
            self._failed_key == provider.key
            and self._failed_signature == signature
            and self._failed_until > now
        ):
            return None
        try:
            snapshot = await asyncio.to_thread(
                self._index.load_snapshot, provider_key=provider.key
            )
        except Exception:  # noqa: BLE001 - 索引损坏按无索引处理
            snapshot = None
        if snapshot is None:
            self._failed_key = provider.key
            self._failed_signature = signature
            self._failed_until = now + READER_FAILURE_CACHE_SECONDS
            self._debug_once(
                "snapshot",
                "语义找回：本地索引缺失/损坏/与当前 Embedding Provider 不匹配，"
                "本轮跳过（等待后台重建）",
            )
            return None
        self._cache = (signature, snapshot)
        self._failed_key = ""
        self._failed_signature = None
        self._failed_until = 0.0
        return snapshot


class SemanticMaintainer:
    """后台索引维护：收集本地档案 → 增量 embedding → 原子切换 generation。

    触发方式：

    * 插件启动后空闲（``start()`` 后先等待 ``startup_delay`` 再构建）；
    * 写入学习/日记后调用 :meth:`request_refresh`（只置位事件，循环内防抖合并）；
    * ``recall.refresh_minutes`` 周期检查（配置为 0 时只在事件触发时检查）。

    构建失败按指数退避收敛重试，只记录一次 warning；旧索引始终保留可用。
    """

    def __init__(
        self,
        *,
        index: SemanticIndex,
        resolver: SemanticEmbeddingResolver,
        config_getter: Callable[[], Any],
        data_dir: str | Path,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        """初始化维护器（不启动任务，不读文件）。"""
        self._index = index
        self._resolver = resolver
        self._config_getter = config_getter
        self._data_dir = Path(data_dir)
        self._startup_delay = max(0.0, float(startup_delay_seconds))
        self._debounce = max(0.0, float(debounce_seconds))
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._failures = 0
        self._retry_at = 0.0

    def request_refresh(self) -> None:
        """请求一次增量刷新（只置位事件，不做 IO、不创建任务/线程）。"""
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task | None:
        """启动后台维护任务（需要有运行中的事件循环）。"""
        if self.running:
            return self._task
        try:
            self._task = asyncio.create_task(
                self._run(), name=f"{PLUGIN_NAME}_semantic"
            )
        except RuntimeError:  # pragma: no cover - 离线单测无事件循环时跳过
            self._task = None
        return self._task

    async def stop(self) -> None:
        """取消后台维护任务（插件停用/重载时调用）。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        """维护循环：启动空闲构建 → 等待事件/周期 → 防抖 → 增量构建。"""
        try:
            if self._startup_delay > 0:
                await asyncio.sleep(self._startup_delay)
            while True:
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 循环不因单次异常退出
                    logger.debug("语义索引维护循环异常：%s", exc)
                # 清掉本轮已消费的触发；等待期间的新写入会再次置位。
                self._wake.clear()
                waited = self._next_wait_seconds()
                try:
                    if waited is None:
                        await self._wake.wait()
                    else:
                        await asyncio.wait_for(self._wake.wait(), timeout=waited)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                if self._debounce > 0:
                    await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:  # pragma: no cover - 正常停用路径
            raise

    def _next_wait_seconds(self) -> float | None:
        """下一次周期检查的等待时间；失败退避时取较小值，0 表示只等事件。"""
        try:
            config = self._config_getter()
            interval = int(getattr(config.recall, "refresh_minutes", 0) or 0) * 60
        except Exception:  # noqa: BLE001 - 配置异常按默认间隔
            interval = 600
        now = time.monotonic()
        if self._failures and self._retry_at > now:
            backoff = max(1.0, self._retry_at - now)
            return backoff if interval <= 0 else float(min(interval, backoff))
        if interval <= 0:
            return None
        return float(interval)

    def _backoff_seconds(self) -> float:
        delay = FAILURE_BASE_BACKOFF_SECONDS * (2 ** max(0, self._failures - 1))
        return min(FAILURE_MAX_BACKOFF_SECONDS, delay)

    async def refresh(self, *, force: bool = False) -> bool:
        """执行一次索引刷新；返回是否成功（失败不抛出，旧索引保留可用）。"""
        async with self._lock:
            manager = getattr(self._resolver, "_failover", None)
            slot = getattr(self._resolver, "_slot", "embedding")
            policy = manager.policy(slot) if manager else None
            attempts = 1 + len(manager.candidates(slot, "")) if policy else 1
            for _ in range(attempts):
                try:
                    return await self._refresh_locked(force=force)
                except EmbeddingModelChanged:
                    force = True
            return False

    async def _refresh_locked(self, *, force: bool) -> bool:
        try:
            config = self._config_getter()
        except Exception:  # noqa: BLE001 - 配置读取失败本轮跳过
            return False
        recall = getattr(config, "recall", None)
        if recall is None or not bool(getattr(recall, "enabled", False)):
            return False
        # 本地存储关闭时不建索引：聊天找回只读插件本地档案。
        memory = getattr(config, "memory", None)
        if memory is not None and not bool(getattr(memory, "local_enabled", True)):
            return False
        provider = self._resolver.resolve(recall.embedding_provider_id)
        if provider is None:
            return False
        now = time.monotonic()
        if not force and self._failures and now < self._retry_at:
            return False
        try:
            docs = await asyncio.to_thread(
                collect_documents,
                self._data_dir,
                include_learning=bool(getattr(recall, "include_learning", True)),
                include_diary=bool(getattr(recall, "include_diary", True)),
                doc_chars=int(getattr(recall, "doc_chars", DEFAULT_DOC_CHARS)),
                index_max_docs=int(
                    getattr(recall, "index_max_docs", DEFAULT_INDEX_MAX_DOCS)
                ),
            )
            result = await build_index(
                self._index,
                docs,
                embed=lambda texts: self._embed(provider, texts),
                provider_id=provider.provider_id,
                model=provider.model,
                dim=provider.dim,
                doc_chars=int(getattr(recall, "doc_chars", DEFAULT_DOC_CHARS)),
                index_max_docs=int(
                    getattr(recall, "index_max_docs", DEFAULT_INDEX_MAX_DOCS)
                ),
                force=force,
            )
        except asyncio.CancelledError:
            raise
        except EmbeddingModelChanged:
            raise
        except Exception as exc:  # noqa: BLE001 - 构建失败保留旧索引并退避
            self._record_failure(exc)
            return False
        self._record_success(provider, result)
        return True

    async def _embed(
        self, provider: ResolvedEmbedding, texts: list[str]
    ) -> list[list[float]]:
        """调用 Provider 批量 embedding（无批量接口时逐条兜底）。"""
        raw_provider = provider.provider
        get_many = getattr(raw_provider, "get_embeddings", None)
        if callable(get_many):
            result = await asyncio.wait_for(
                get_many(list(texts)),
                timeout=getattr(
                    raw_provider, "batch_timeout", BUILD_EMBED_TIMEOUT_SECONDS
                ),
            )
            return list(result or [])
        get_one = getattr(raw_provider, "get_embedding", None)
        if not callable(get_one):
            raise SemanticBuildError(
                "Embedding Provider 缺少 get_embedding/get_embeddings 接口"
            )
        vectors: list[list[float]] = []
        for text in texts:
            vectors.append(
                await asyncio.wait_for(
                    get_one(text), timeout=BUILD_EMBED_TIMEOUT_SECONDS
                )
            )
        return vectors

    def _record_failure(self, exc: Exception) -> None:
        """记录构建失败并设置指数退避；告警只记录一次，避免刷屏。"""
        self._failures += 1
        delay = self._backoff_seconds()
        self._retry_at = time.monotonic() + delay
        message = f"{type(exc).__name__}: {exc}"
        if self._failures == 1:
            logger.warning(
                "语义索引构建失败（%s），%.0f 秒后重试；旧索引继续可用",
                message,
                delay,
            )
        else:
            logger.debug("语义索引构建再次失败：%s", message)
        self._index.write_status(
            {
                "updated_at": time.time(),
                "ok": False,
                "error": message,
                "failures": self._failures,
                "retry_in_seconds": delay,
                "path": str(self._index.root),
            }
        )

    def _record_success(self, provider: ResolvedEmbedding, result: Any) -> None:
        """记录构建成功；恢复时记录一次 info，未变化时只写 debug。"""
        was_failing = self._failures > 0
        self._failures = 0
        self._retry_at = 0.0
        if result.changed:
            logger.info(
                "语义索引已更新：%d 条（重新 embedding %d 条，复用 %d 条；来源 %s）",
                result.count,
                result.embedded,
                result.reused,
                "、".join(
                    f"{key}:{value}" for key, value in sorted(result.sources.items())
                )
                or "无",
            )
        elif was_failing:
            logger.info("语义索引构建已恢复正常（%d 条）", result.count)
        else:
            logger.debug("语义索引无变化（%d 条），跳过写盘", result.count)
        if result.changed or was_failing:
            self._index.write_status(
                {
                    "updated_at": time.time(),
                    "ok": True,
                    "generation": result.generation,
                    "count": result.count,
                    "embedded": result.embedded,
                    "reused": result.reused,
                    "sources": result.sources,
                    "provider_id": provider.provider_id,
                    "model": provider.model,
                    "dim": provider.dim,
                    "path": str(self._index.root),
                }
            )
