"""App 使用经验召回的独立向量索引与后台维护（纯逻辑 + AstrBot 适配）。

App 操控经验与聊天记忆分开存储、分开配置向量模型：

* 经验档案：``<plugin_data>/app_memory.json``（:mod:`core.app_memory`）；
* 索引目录：``<plugin_data>/semantic_app/``（本模块，独立于聊天找回的 ``semantic/``）；
* 向量模型：``prompt.app_memory_embedding_provider_id``，留空自动使用 AstrBot
  已配置的默认 Embedding Provider。

读路径 :class:`AppRecallReader` 在操作前按「当前 App + 页面/屏幕线索」检索相关经验，
语义索引不可用（未配置向量模型、索引缺失/损坏、查询超时）时回退到本地档案按页面
关键词与更新时间排序，保证基础召回始终可用。写路径 :class:`AppRecallMaintainer`
在后台增量构建索引，不阻塞工具调用与聊天。

本模块不访问 ADB/设备，也不写入经验档案。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

from .app_memory import (
    AppMemoryEntry,
    _clip,
    _format_time,
)
from .failover_adapter import EmbeddingModelChanged
from .identity import PLUGIN_NAME
from .semantic import (
    DEFAULT_DOC_CHARS,
    DEFAULT_INDEX_MAX_DOCS,
    SemanticIndex,
    SemanticSnapshot,
    build_app_memory_documents,
    build_index,
    normalize_vector,
    read_app_memory_entries,
)
from .semantic_recall import (
    ResolvedEmbedding,
    SemanticEmbeddingResolver,
    query_embedding,
)

APP_INDEX_DIRNAME = "semantic_app"
DEFAULT_RECALL_LIMIT = 3
DEFAULT_RECALL_MAX_CHARS = 600
DEFAULT_RECALL_MIN_SCORE = 0.2
DEFAULT_QUERY_CHARS = 400
DEFAULT_QUERY_TIMEOUT_SECONDS = 3.0
APP_RECALL_HEADER = "【App 使用经验（按当前 App/页面召回）】"
APP_RECALL_FOOTER = (
    "（以当前屏幕为准；界面与经验不符说明 App 已改版，先重新探索，"
    "成功后再用 phone_app_remember 覆盖更新该页面经验。）"
)
RENDER_NOTE_CHARS = 140
RENDER_STEP_CHARS = 60
RENDER_STEPS_SHOWN = 4
# 后台维护参数
STARTUP_DELAY_SECONDS = 5.0
DEBOUNCE_SECONDS = 3.0
REFRESH_MINUTES = 10
FAILURE_BASE_BACKOFF_SECONDS = 60.0
FAILURE_MAX_BACKOFF_SECONDS = 1800.0

ConfigGetter = Callable[[], Any]
EntryProvider = Callable[[], list[AppMemoryEntry]]


def app_index_root(data_dir: str | Path) -> Path:
    """App 经验索引目录（与聊天找回索引分开）。"""
    return Path(data_dir) / APP_INDEX_DIRNAME


def _entry_key(entry: AppMemoryEntry) -> tuple[str, str]:
    return entry.app, entry.page


def _rank_entries(
    entries: list[AppMemoryEntry],
    *,
    page_hint: str,
    context: str,
) -> list[AppMemoryEntry]:
    """档案兜底排序：页面/屏幕线索命中优先，其余按更新时间倒序。"""
    hint = f"{page_hint}\n{context}".lower()
    page = " ".join(str(page_hint or "").split()).lower()

    def score(entry: AppMemoryEntry) -> tuple[float, float, float]:
        entry_page = entry.page.lower()
        page_score = 0.0
        if page and entry_page and (entry_page in page or page in entry_page):
            page_score = 2.0
        elif entry_page and entry_page in hint:
            page_score = 1.0
        elif not entry_page:
            page_score = 0.5
        return (page_score, entry.updated_at, entry.version)

    return sorted(entries, key=score, reverse=True)


def render_entries(
    entries: list[AppMemoryEntry],
    *,
    limit: int = DEFAULT_RECALL_LIMIT,
    max_chars: int = DEFAULT_RECALL_MAX_CHARS,
    header: str = APP_RECALL_HEADER,
) -> str:
    """把召回的经验条目渲染成模型可读文本；无条目返回空串。"""
    if not entries:
        return ""
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = DEFAULT_RECALL_LIMIT
    if count <= 0:
        return ""
    lines: list[str] = [header]
    for entry in entries[:count]:
        scene = entry.page or "通用"
        when = _format_time(entry.updated_at)
        stale = "（可能已失效，请以当前屏幕重新确认）" if entry.stale else ""
        line = (
            f"- [{scene}]（{when}）{stale}"
            f"{_clip(entry.note, RENDER_NOTE_CHARS)}"
        )
        if entry.steps:
            shown = " → ".join(
                _clip(step, RENDER_STEP_CHARS)
                for step in entry.steps[:RENDER_STEPS_SHOWN]
            )
            line += f"；步骤：{shown}"
            if len(entry.steps) > RENDER_STEPS_SHOWN:
                line += " …"
        lines.append(line)
    text = "\n".join(lines)
    footer = APP_RECALL_FOOTER
    try:
        budget = int(max_chars)
    except (TypeError, ValueError):
        budget = DEFAULT_RECALL_MAX_CHARS
    if budget > 0 and len(text) + len(footer) + 1 > budget:
        keep = max(0, budget - len(footer) - 1)
        text = _clip(text, keep) if keep else ""
    if not text:
        return ""
    return f"{text}\n{footer}"


def build_query(*, label: str = "", app_key: str = "", page_hint: str = "", context: str = "") -> str:
    """构造经验检索查询文本（App 标识 + 页面线索 + 当前屏幕内容摘要）。"""
    parts = [str(label or "").strip(), str(app_key or "").strip()]
    page = " ".join(str(page_hint or "").split())
    if page:
        parts.append(page)
    body = " ".join(str(context or "").split())
    if body:
        parts.append(body)
    query = "\n".join(part for part in parts if part)
    if len(query) > DEFAULT_QUERY_CHARS:
        query = query[:DEFAULT_QUERY_CHARS]
    return query


class AppRecallReader:
    """操作前按当前 App/页面召回经验的只读路径。

    语义索引可用时按余弦相似度粗排，再映射回档案条目；不可用时回退到本地档案
    按页面关键词 + 更新时间排序。任何失败都返回空串，不影响手机操作。
    """

    def __init__(
        self,
        *,
        index: SemanticIndex,
        resolver: SemanticEmbeddingResolver,
        config_getter: ConfigGetter,
        entry_provider: EntryProvider,
    ) -> None:
        self._index = index
        self._resolver = resolver
        self._config_getter = config_getter
        self._entry_provider = entry_provider
        self._cache: tuple[tuple[int, int, int], SemanticSnapshot] | None = None

    async def select(
        self,
        *,
        app_key: str,
        label: str = "",
        page_hint: str = "",
        context: str = "",
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> list[AppMemoryEntry]:
        """按当前 App/页面返回相关经验条目（语义优先、档案兜底）。"""
        key = str(app_key or "").strip().lower()
        if not key:
            return []
        try:
            config = self._config_getter()
        except Exception:  # noqa: BLE001 - 配置读取失败不影响操作
            return []
        if not bool(getattr(config, "app_memory_enabled", False)):
            return []
        entries = self._app_entries(key)
        if not entries:
            return []
        ordered: list[AppMemoryEntry] = []
        try:
            ordered = await asyncio.wait_for(
                self._semantic_order(
                    entries,
                    key=key,
                    label=label,
                    page_hint=page_hint,
                    context=context,
                    limit=limit,
                    provider_id=str(
                        getattr(config, "app_memory_embedding_provider_id", "") or ""
                    ),
                ),
                timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 语义检索失败回退档案排序
            logger.debug("App 经验语义召回失败，回退档案查找：%s", exc)
            ordered = []
        if not ordered:
            ordered = _rank_entries(entries, page_hint=page_hint, context=context)
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = DEFAULT_RECALL_LIMIT
        return ordered[:count] if count else []

    async def render(
        self,
        *,
        app_key: str,
        label: str = "",
        page_hint: str = "",
        context: str = "",
        limit: int = DEFAULT_RECALL_LIMIT,
        max_chars: int = DEFAULT_RECALL_MAX_CHARS,
    ) -> str:
        """返回当前 App/页面相关经验文本；未启用或无经验时返回空串。"""
        entries = await self.select(
            app_key=app_key,
            label=label,
            page_hint=page_hint,
            context=context,
            limit=limit,
        )
        if not entries:
            return ""
        return render_entries(entries, limit=limit, max_chars=max_chars)

    def _app_entries(self, app_key: str) -> list[AppMemoryEntry]:
        try:
            entries = list(self._entry_provider())
        except Exception:  # noqa: BLE001 - 档案读取失败按无经验处理
            return []
        return [entry for entry in entries if entry.app == app_key]

    async def _semantic_order(
        self,
        entries: list[AppMemoryEntry],
        *,
        key: str,
        label: str,
        page_hint: str,
        context: str,
        limit: int,
        provider_id: str,
    ) -> list[AppMemoryEntry]:
        provider = self._resolver.resolve(provider_id)
        if provider is None:
            return []
        snapshot = await asyncio.to_thread(self._snapshot, provider)
        if snapshot is None or snapshot.count == 0:
            return []
        query = build_query(
            label=label, app_key=key, page_hint=page_hint, context=context
        )
        if not query.strip():
            return []
        raw_vector = await query_embedding(provider, query)
        query_vector = normalize_vector(raw_vector)
        if not query_vector or len(query_vector) != snapshot.dim:
            return []
        try:
            top_k = max(1, int(limit) * 3)
        except (TypeError, ValueError):
            top_k = DEFAULT_RECALL_LIMIT * 3
        hits = await asyncio.to_thread(
            snapshot.search,
            query_vector,
            top_k=top_k,
            min_score=DEFAULT_RECALL_MIN_SCORE,
        )
        by_key = {_entry_key(entry): entry for entry in entries}
        ordered: list[AppMemoryEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            meta = hit.doc.meta or {}
            if str(meta.get("source") or "").strip().lower() != key:
                continue
            page = str(meta.get("page") or "")
            entry = by_key.get((key, page))
            if entry is None or _entry_key(entry) in seen:
                continue
            seen.add(_entry_key(entry))
            ordered.append(entry)
            if len(ordered) >= max(1, int(limit)):
                break
        return ordered

    def _snapshot(self, provider: ResolvedEmbedding) -> SemanticSnapshot | None:
        """读取索引快照（按 manifest 文件标识缓存；损坏时返回 None）。"""
        try:
            stat = self._index.manifest_path.stat()
        except OSError:
            return None
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        cached = self._cache
        if (
            cached is not None
            and cached[0] == signature
            and cached[1].provider_key == provider.key
        ):
            return cached[1]
        try:
            snapshot = self._index.load_snapshot(provider_key=provider.key)
        except Exception:  # noqa: BLE001 - 索引损坏按无索引处理
            snapshot = None
        if snapshot is None:
            return None
        self._cache = (signature, snapshot)
        return snapshot


class AppRecallMaintainer:
    """App 经验语义索引的后台维护：档案 → 增量 embedding → 原子切换 generation。"""

    def __init__(
        self,
        *,
        index: SemanticIndex,
        resolver: SemanticEmbeddingResolver,
        config_getter: ConfigGetter,
        data_dir: str | Path,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
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
        self._last_reason = ""

    def request_refresh(self) -> None:
        """请求一次增量刷新（只置位事件，不做 IO）。"""
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task | None:
        """启动后台维护任务（需要运行中的事件循环）。"""
        if self.running:
            return self._task
        try:
            self._task = asyncio.create_task(
                self._run(), name=f"{PLUGIN_NAME}_app_recall"
            )
        except RuntimeError:  # pragma: no cover - 离线单测无事件循环
            self._task = None
        return self._task

    async def stop(self) -> None:
        """取消后台维护任务。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        try:
            if self._startup_delay > 0:
                await asyncio.sleep(self._startup_delay)
            while True:
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 循环不因单次异常退出
                    logger.debug("App 经验索引维护循环异常：%s", exc)
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
        except asyncio.CancelledError:
            raise

    def _next_wait_seconds(self) -> float | None:
        interval = float(REFRESH_MINUTES * 60)
        now = time.monotonic()
        if self._failures and self._retry_at > now:
            return max(1.0, self._retry_at - now)
        return interval

    def _backoff_seconds(self) -> float:
        delay = FAILURE_BASE_BACKOFF_SECONDS * (2 ** max(0, self._failures - 1))
        return min(FAILURE_MAX_BACKOFF_SECONDS, delay)

    async def refresh(self, *, force: bool = False) -> bool:
        """执行一次索引刷新；失败不抛出，旧索引继续可用。"""
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
        prompt = getattr(config, "prompt", None)
        if prompt is None or not bool(getattr(prompt, "app_memory_enabled", False)):
            return False
        provider = self._resolver.resolve(
            str(getattr(prompt, "app_memory_embedding_provider_id", "") or "")
        )
        if provider is None:
            return False
        now = time.monotonic()
        if not force and self._failures and now < self._retry_at:
            return False
        try:
            docs = await asyncio.to_thread(self._collect)
            result = await build_index(
                self._index,
                docs,
                embed=lambda texts: self._embed(provider, texts),
                provider_id=provider.provider_id,
                model=provider.model,
                dim=provider.dim,
                doc_chars=DEFAULT_DOC_CHARS,
                index_max_docs=DEFAULT_INDEX_MAX_DOCS,
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

    def _collect(self):
        entries = read_app_memory_entries(self._data_dir / "app_memory.json")
        return build_app_memory_documents(entries, doc_chars=DEFAULT_DOC_CHARS)

    async def _embed(
        self, provider: ResolvedEmbedding, texts: list[str]
    ) -> list[list[float]]:
        raw_provider = provider.provider
        get_many = getattr(raw_provider, "get_embeddings", None)
        if callable(get_many):
            result = await asyncio.wait_for(
                get_many(list(texts)),
                timeout=getattr(raw_provider, "batch_timeout", 120.0),
            )
            return list(result or [])
        get_one = getattr(raw_provider, "get_embedding", None)
        if not callable(get_one):
            raise RuntimeError("Embedding Provider 缺少 get_embedding/get_embeddings 接口")
        vectors: list[list[float]] = []
        for text in texts:
            vectors.append(await asyncio.wait_for(get_one(text), timeout=120.0))
        return vectors

    def _record_failure(self, exc: Exception) -> None:
        self._failures += 1
        delay = self._backoff_seconds()
        self._retry_at = time.monotonic() + delay
        self._last_reason = f"{type(exc).__name__}: {exc}"
        if self._failures == 1:
            logger.warning(
                "App 经验索引构建失败（%s），%.0f 秒后重试；旧索引继续可用",
                self._last_reason,
                delay,
            )
        else:
            logger.debug("App 经验索引构建再次失败：%s", self._last_reason)
        self._index.write_status(
            {
                "updated_at": time.time(),
                "ok": False,
                "error": self._last_reason,
                "failures": self._failures,
                "retry_in_seconds": delay,
                "path": str(self._index.root),
            }
        )

    def _record_success(self, provider: ResolvedEmbedding, result: Any) -> None:
        was_failing = self._failures > 0
        self._failures = 0
        self._retry_at = 0.0
        self._last_reason = ""
        if result.changed:
            logger.info(
                "App 经验索引已更新：%d 条（重新 embedding %d 条，复用 %d 条）",
                result.count,
                result.embedded,
                result.reused,
            )
        elif was_failing:
            logger.info("App 经验索引构建已恢复正常（%d 条）", result.count)
        else:
            logger.debug("App 经验索引无变化（%d 条），跳过写盘", result.count)
        if result.changed or was_failing:
            self._index.write_status(
                {
                    "updated_at": time.time(),
                    "ok": True,
                    "generation": result.generation,
                    "count": result.count,
                    "embedded": result.embedded,
                    "reused": result.reused,
                    "provider_id": provider.provider_id,
                    "model": provider.model,
                    "dim": provider.dim,
                    "path": str(self._index.root),
                }
            )
