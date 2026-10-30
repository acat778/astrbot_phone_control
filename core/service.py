"""手机能力门面模块：工具层与命令层共用的稳定接口。

``PhoneService`` 只依赖 ``core/`` 下的设备、配置、记忆与调度组件，不反向依赖
``main.py``。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .actions import DeviceActions, validate_touch_point
from .adb import AdbClient
from .apps import (
    KNOWN_APP_LABELS,
    UNKNOWN_APP_HINT_TEMPLATE,
    app_label_for_package,
    parse_package_list,
    pick_random_app,
    resolve_app,
    resolve_package,
)
from .browse import BrowseSession
from .background import clean_background
from .chat_memory import ChatMemory
from .config import UNCONFIGURED_MESSAGE, PhoneConfig
from .diary import PhoneDiary
from .task_round import current_round
from .attribution import KIND_UNKNOWN, origin_kind
from .errors import (
    ActionFailedError,
    AdbNotConfiguredError,
    AppNotInstalledError,
    PhoneControlError,
)
from .interactions import InteractionStore
from .learning import LearningError, LearningStore
from .models import BrowseRequest, BrowseResult, DeviceInfo, ScreenSnapshot, UiNode
from .novelty import NoveltyStore
from .page_content import (
    PAGE_ACTION_WINDOW_MAX_SECONDS,
    PAGE_COMPACT_BODY_CHARS,
    PAGE_COMPACT_MAX_TEXT_CHARS,
    PAGE_FLOW_LIMIT,
    PAGE_MAX_TEXT_CHARS,
    PageTrafficSnapshot,
    build_page_traffic,
)
from .plugin_shared import (
    _INBOX_EXCLUDE_WORDS,
    _INBOX_KEYWORDS,
    _format_time,
    _llm_response_text,
)
from .scheduler import CONTINUOUS_JOB_ORIGIN
from .scheduler import JOB_ORIGIN as AUTO_BROWSE_JOB_ORIGIN
from .screenshot_cleanup import cleanup_directory
from .play_flow import share_rule_text
from .share_policy import (
    SHARE_STATE_FILENAME,
    classify_session_umo,
    is_recent_interaction,
    is_share_window,
    load_share_state,
    record_share,
    save_share_state,
    share_cooldown_remaining_seconds,
    share_window_text,
)
from .task_priority import UserTaskPriority, should_yield_to_user
from .telemetry import MetricsStore
from .token_quota import (
    PlayBudget,
    PlayBudgetRegistry,
    TokenQuota,
    estimate_fallback_usage,
    usage_breakdown,
    usage_is_empty,
)
from .ui import dump_ui, render_ui


# 验证码/做题操作批次：整批校验并生成计划后才开始设备动作。
_VERIFY_OPS_LIMIT = 20
_VERIFY_TAP_ACTIONS = frozenset({"tap", "click", "point"})
_VERIFY_DRAG_ACTIONS = frozenset({"drag", "swipe"})


def _verify_coordinate(item: Any, axis: str, limit: int) -> int:
    """校验单个坐标分量并返回整数；任何非法坐标抛 :class:`ActionFailedError`。"""
    if not isinstance(item, dict):
        raise ActionFailedError("操作坐标必须是 {x,y} 对象")
    raw = item.get(axis)
    if isinstance(raw, bool) or raw is None:
        raise ActionFailedError(f"{axis} 必须是整数")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActionFailedError(f"{axis} 必须是整数") from exc
    if isinstance(raw, float) and not raw.is_integer():
        raise ActionFailedError(f"{axis} 必须是整数")
    if value < 0 or value >= limit:
        raise ActionFailedError(
            f"{axis} 坐标越界：{value}，有效范围 0..{limit - 1}"
        )
    return value


def _validate_verify_batch(ops: Any, confirm: Any) -> tuple[list[Any], Any]:
    """设备动作前的批次结构校验：数量、类型与 confirm 形式。"""
    if ops is None:
        raw_ops: list[Any] = []
    elif isinstance(ops, list):
        raw_ops = list(ops)
    else:
        raise ActionFailedError("验证操作批次必须是列表")
    if len(raw_ops) > _VERIFY_OPS_LIMIT:
        raise ActionFailedError(
            f"验证操作数量超出上限 {_VERIFY_OPS_LIMIT}（实际 {len(raw_ops)}）"
        )
    if confirm is not None and not isinstance(confirm, dict):
        raise ActionFailedError("确认坐标必须是 {x,y} 对象")
    return raw_ops, confirm


def _plan_verify_steps(
    ops: list[Any], confirm: Any, width: int, height: int
) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    """把整批 ops/confirm 解析成已验证计划；任何非法项都在设备动作前抛出。"""
    plan: list[dict[str, Any]] = []
    for item in ops:
        if not isinstance(item, dict):
            raise ActionFailedError("每个操作项必须是 {action,x,y} 对象")
        action = str(item.get("action") or "tap").strip().lower()
        if action in _VERIFY_DRAG_ACTIONS:
            x1 = _verify_coordinate(item, "x1", width)
            y1 = _verify_coordinate(item, "y1", height)
            x2 = _verify_coordinate(item, "x2", width)
            y2 = _verify_coordinate(item, "y2", height)
            duration = item.get("duration_ms")
            if duration is None or duration == "":
                duration = 500
            else:
                try:
                    duration = int(duration)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ActionFailedError("duration_ms 必须是整数") from exc
            plan.append(
                {
                    "action": "drag",
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "duration_ms": max(100, min(5000, duration)),
                }
            )
        elif action in _VERIFY_TAP_ACTIONS:
            x = _verify_coordinate(item, "x", width)
            y = _verify_coordinate(item, "y", height)
            plan.append({"action": "tap", "x": x, "y": y})
        else:
            raise ActionFailedError(
                f"不支持的操作类型：{action}（仅支持 tap/drag）"
            )
    confirm_point: dict[str, int] | None = None
    if confirm is not None:
        confirm_point = {
            "x": _verify_coordinate(confirm, "x", width),
            "y": _verify_coordinate(confirm, "y", height),
        }
    return plan, confirm_point


class PhoneService:
    """手机能力门面：工具层与命令层共用的稳定接口。"""

    def __init__(
        self,
        *,
        raw_config: Any,
        adb: AdbClient | None,
        actions: DeviceActions | None,
        diary: PhoneDiary,
        data_dir: str | Path,
        device_log: Any = None,
        quota: TokenQuota | None = None,
        play_budgets: PlayBudgetRegistry | None = None,
        chat_memory: ChatMemory | None = None,
        interactions: InteractionStore | None = None,
        telemetry: Any = None,
        novelty: Any = None,
    ) -> None:
        self._raw_config = raw_config
        self.adb = adb
        self.actions = actions
        self.diary = diary
        self.data_dir = Path(data_dir)
        self.screenshots_dir = self.data_dir / "screenshots"
        self._device_log = device_log
        self._browse_session: BrowseSession | None = None
        self.quota = quota if quota is not None else TokenQuota(self.data_dir)
        self.play_budgets = (
            play_budgets if play_budgets is not None else PlayBudgetRegistry()
        )
        self.chat_memory = (
            chat_memory if chat_memory is not None else ChatMemory(self.data_dir / "chat")
        )
        self.interactions = (
            interactions
            if interactions is not None
            else InteractionStore(self.data_dir)
        )
        self.telemetry = (
            telemetry if telemetry is not None else MetricsStore(self.data_dir)
        )
        # 内容新鲜感/厌倦感：记录同类内容疲劳度并按时间衰减，供提示词注入。
        self.novelty = (
            novelty if novelty is not None else NoveltyStore(self.data_dir)
        )
        self.learning = LearningStore(self.data_dir)
        # 由插件装配：AstrBot Star Context（OCR/后台任务）、实时屏幕 watcher 与后端管理器。
        self.app_context: Any = None
        from .app_policy import AppPolicy

        self.app_policy = AppPolicy(self)
        if self.actions is not None:
            self.actions.app_open_guard = self.app_policy.require_allowed
        self.live_screen: Any = None
        self.backends: Any = None
        # UI 快照缓存：text/desc 定位优先复用最近一次快照（模型刚看过屏幕时
        # 不必再 uiautomator dump 一次）；任何控制动作后失效，且复用前校验前台 App。
        self._ui_cache: tuple[float, ScreenSnapshot] | None = None
        self._ui_cache_ttl = 15.0
        # 当前页面抓包快照：抓包与屏幕识别共用；设备写动作会更新动作时间并失效缓存。
        self._page_changed_at = 0.0
        self._page_traffic_cache: tuple[float, PageTrafficSnapshot] | None = None
        self._page_traffic_cache_ttl = 1.5
        # App 使用经验与用户任务优先状态（由插件装配；工具层也直接读取）。
        self.app_memory: Any = None
        self.user_priority = UserTaskPriority()
        self._installed_cache: tuple[float, list[str]] | None = None
        self.memory_sync_hook: Any = None
        # 语义索引刷新钩子（由插件注入：只置位后台维护事件，不做任何 IO）。
        self.semantic_refresh_hook: Any = None
        # 自主任务模型供应商应用钩子（由插件注入，避免 core 模块依赖 AstrBot）
        self.task_provider_hook: Any = None
        # 最近一次 token 实际 usage 对账结果（/phone quota 展示）
        self.last_usage_reconcile: dict[str, Any] | None = None
        self.traffic: Any = None
        self.verification: Any = None
        # 流媒体内容识别：设备播放音频采集客户端（由插件装配，缺省时降级为仅画面）。
        self.media: Any = None
        from .transfers import TransferFiles
        self.files = TransferFiles(adb, self.data_dir / "transfers")
        self.recordings: Any = None
        # 统一设备执行入口（插件装配；None 表示未启用门禁，供离线单测使用）。
        self.gate: Any = None
        self._last_screenshot_cleanup = 0.0

    # ---- 配置 ---------------------------------------------------------

    @property
    def configured(self) -> bool:
        """是否已配置可用的 ADB 主机与端口。"""
        return self.config.adb.configured

    def ensure_configured(self) -> None:
        """未配置 ADB 时抛出带操作指引的异常，禁止尝试连接。"""
        if not self.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        if self.adb is None or self.actions is None:
            raise AdbNotConfiguredError(
                UNCONFIGURED_MESSAGE + "；配置已更新，请重载插件后生效"
            )

    def guard_device(self, op: str = "") -> None:
        """设备读写前的统一门禁检查（无执行作用域时视为后台基础设施操作）。"""
        gate = getattr(self, "gate", None)
        if gate is not None:
            gate.checkpoint(str(op))

    def guard_device_write(self, op: str = "") -> None:
        """按写操作语义复查验证门禁（用于底层会切 App/点击的动作）。

        工具包装器按工具名分类设置作用域写标记；可能导航的底层动作在这里再按
        写操作复查一次，避免工具分类漏标时验证会话进行中仍切换 App 或点击。
        无验证控制器（离线单测）时只做普通门禁检查。
        """
        controller = getattr(self, "verification", None)
        if controller is not None:
            try:
                denied = str(
                    controller.guard_tool(
                        str(op or "device_write"), write=True
                    )
                    or ""
                ).strip()
            except Exception:  # noqa: BLE001 - 验证状态读取失败时保持普通门禁
                denied = ""
            if denied:
                if denied.startswith("error:"):
                    denied = denied[len("error:") :].strip()
                raise PhoneControlError(denied)
        self.guard_device(op)

    @property
    def config(self) -> PhoneConfig:
        """按当前原始配置实时解析出的快照（WebUI 改动即时生效）。"""
        return PhoneConfig.from_mapping(
            self._raw_config,
            warn=lambda message: logger.warning(message),
        )

    # ---- 内容新鲜感/厌倦感 ---------------------------------------------

    @property
    def novelty_settings(self):
        """当前新鲜感配置快照（每次实时解析，面板保存即时生效）。"""
        return self.config.novelty

    def record_novelty(self, app: str, text: str, *, weight: float = 1.0) -> Any:
        """记录一次内容曝光并返回新鲜感匹配结果；未启用或失败时返回 None。"""
        settings = self.novelty_settings
        if not settings.enabled:
            return None
        try:
            return self.novelty.record(app, text, settings, weight=weight)
        except Exception as exc:  # noqa: BLE001 - 状态记录失败不影响主流程
            logger.warning("新鲜感记录失败：%s", exc)
            return None

    def novelty_prompt(self) -> str:
        """渲染新鲜感状态注入文本（未启用/无数据时返回空串）。"""
        settings = self.novelty_settings
        if not settings.enabled:
            return ""
        try:
            return self.novelty.render_for_prompt(settings, break_rule=self.config.flexibility.interest_active)
        except Exception as exc:  # noqa: BLE001 - 读取失败不影响提示词
            logger.warning("新鲜感状态读取失败：%s", exc)
            return ""

    def novelty_status_text(self) -> str:
        """``/phone novelty status`` 文本；读取失败返回可操作提示。"""
        try:
            return self.novelty.status_text(self.novelty_settings)
        except Exception as exc:  # noqa: BLE001
            return f"读取新鲜感状态失败：{exc}"

    # ---- App 记忆 / 用户任务优先 / 已安装 App --------------------------

    def should_yield_to_user(self, event: Any) -> bool:
        """自主任务的这次工具调用是否应让路给用户手机任务。"""
        from .subagent import is_user_subagent

        if is_user_subagent(event):
            return False
        try:
            origin = self.task_origin(event)
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            origin = ""
        return should_yield_to_user(origin=origin, user_active=self.user_priority.active)

    @property
    def contents(self):
        from .content_task import ContentTasks
        if not hasattr(self, "_contents"):
            self._contents = ContentTasks(self)
        return self._contents

    @property
    def experiences(self):
        from .app_experience import AppExperience
        if not hasattr(self, "_experiences"):
            self._experiences = AppExperience(self)
        return self._experiences

    def memory_user_id(self, event: Any) -> str:
        """同步到 MemoryCompanion 时绑定的用户 ID。

        规则：用户会话优先用发送者 ID；自刷/后台 cron 任务用
        ``memory.companion_user_id``，留空则回退 ``security.owner_ids[0]``。
        """
        try:
            if not self.is_task_session(event):
                sender = str(event.get_sender_id() or "").strip()
                if sender:
                    return sender
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            pass
        configured = str(self.config.memory.companion_user_id or "").strip()
        if configured:
            return configured
        owners = self.config.security.owner_ids
        return str(owners[0]) if owners else ""

    async def sync_memory(
        self, event: Any, payload: dict[str, Any]
    ) -> tuple[bool, str]:
        """把一条记忆同步到 MemoryCompanion（由插件注入实现）；失败返回原因。"""
        hook = getattr(self, "memory_sync_hook", None)
        if not callable(hook):
            return False, "记忆同步未启用"
        if not payload:
            return False, "记忆内容为空"
        try:
            return await hook(event, payload)
        except Exception as exc:  # noqa: BLE001 - 同步失败不能影响记忆写入
            logger.warning("同步 MemoryCompanion 失败：%s", exc)
            return False, str(exc)

    def cached_installed_packages(self) -> list[str] | None:
        """返回已安装 App 缓存（过期返回 None，供同步状态文本使用）。"""
        cached = self._installed_cache
        if cached is None:
            return None
        cached_at, packages = cached
        if time.monotonic() - cached_at > 300:
            return None
        return list(packages)

    async def installed_packages(self, *, ttl: float = 300.0) -> list[str]:
        """读取已安装第三方 App 包名（带 TTL 缓存；失败返回空列表）。"""
        cached = self._installed_cache
        if cached is not None and time.monotonic() - cached[0] <= ttl:
            return list(cached[1])
        if self.adb is None:
            return []
        try:
            output = await self.adb.shell("pm list packages -3", timeout=20)
        except Exception as exc:  # noqa: BLE001 - 解析失败降级为不校验安装状态
            logger.warning("读取已安装 App 列表失败（偏好 App 解析降级）：%s", exc)
            return []
        packages = parse_package_list(output)
        self._installed_cache = (time.monotonic(), packages)
        return list(packages)

    async def random_browse_app(self) -> tuple[str, str]:
        """随机选择一个已安装第三方 App（用于默认浏览 App 留空）。"""
        packages = await self.installed_packages()
        package = pick_random_app(packages)
        if not package:
            raise AppNotInstalledError(
                "没有读到已安装的第三方 App，无法随机选择；可先 phone_list_apps "
                "查看或直接指定 App 名称/包名"
            )
        return package, app_label_for_package(package) or package

    def _log(self, message: str) -> None:
        if callable(self._device_log):
            try:
                self._device_log(message)
            except Exception:  # noqa: BLE001 - 日志回调不允许影响主流程
                pass

    # ---- Token 额度 / 玩手机预算 --------------------------------------

    def quota_error_text(self) -> str | None:
        """全局额度用完时的 error 文本；未启用/未超限返回 None。"""
        settings = self.config.quota
        if not settings.enabled or settings.max_tokens <= 0:
            return None
        used = self.quota.used(settings.window_hours)
        if used >= settings.max_tokens:
            return (
                f"error: 手机 token 额度已用完（已用 {used:.0f} / 上限 "
                f"{settings.max_tokens}，窗口 {settings.window_hours} 小时），暂停刷手机；"
                "额度按滚动窗口统计，稍后会自动滑出，也可让主人在配置里调整 quota"
            )
        return None

    def quota_warning_text(self) -> str | None:
        """额度使用达到 90% 时的软告警文本。"""
        settings = self.config.quota
        if not settings.enabled or settings.max_tokens <= 0:
            return None
        used = self.quota.used(settings.window_hours)
        if used >= settings.max_tokens * 0.9:
            percent = used / settings.max_tokens * 100
            return (
                f"提示：手机 token 额度已用 {percent:.0f}%（{used:.0f} / "
                f"{settings.max_tokens}，窗口 {settings.window_hours} 小时），接近上限。"
            )
        return None

    def quota_status_text(self) -> str:
        """生成 /phone quota 的状态文本（额度 + 实际 usage 对账明细）。"""
        settings = self.config.quota
        window = float(settings.window_hours)
        lines: list[str] = []
        if not settings.enabled or settings.max_tokens <= 0:
            lines.append(
                "手机 token 额度：当前不限制（quota.enabled=false 或 max_tokens=0）；"
                "用量仍会记录供面板查看，需要限流时在配置里启用并填正数"
            )
        else:
            lines.append(
                self.quota.status_text(
                    window_hours=window,
                    max_tokens=settings.max_tokens,
                )
            )
        try:
            actual = self.quota.actual_usage(window)
        except Exception:  # noqa: BLE001 - 统计失败不影响额度展示
            actual = {}
        try:
            estimated = self.quota.estimated_usage(window)
        except Exception:  # noqa: BLE001
            estimated = 0.0
        if actual:
            lines.append(
                "实际 usage 对账（窗口内）："
                f"{float(actual.get('total', 0)):.0f} tokens"
                f"（输入未缓存 {int(actual.get('input_other', 0))} / "
                f"输入缓存 {int(actual.get('input_cached', 0))} / "
                f"输出 {int(actual.get('output', 0))}，"
                f"{int(actual.get('calls', 0))} 次对账）"
            )
        if estimated > 0:
            lines.append(
                f"其中运行中估算记录（未经对账）约 {estimated:.0f} tokens；"
                "差异在 Agent run 结束后按 provider_stats 实际 usage 补记"
            )
        last = self.last_usage_reconcile
        if last:
            lines.append(
                "最近一次对账：实际 "
                f"{float(last.get('actual', 0)):.0f} vs 估算 "
                f"{float(last.get('estimated', 0)):.0f}，补记 "
                f"{float(last.get('delta', 0)):.0f}"
                f"（模型 {', '.join(last.get('models') or []) or '未知'}）"
            )
        budget = settings.play_task_tokens
        lines.append(
            "本轮玩手机默认预算：不限制"
            if budget <= 0
            else f"本轮玩手机默认预算：{budget} tokens"
        )
        lines.append("明细：/phone quota audit [小时] 可对照 AstrBot provider_stats 实际用量")
        return "\n".join(lines)

    def play_payload(self, event: Any) -> dict[str, Any]:
        """取出当前事件关联的玩手机任务载荷（非 play 任务返回空 dict）。"""
        payload = None
        try:
            payload = event.get_extra("cron_payload")
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            payload = None
        if not isinstance(payload, dict) or not payload.get("play_task"):
            return {}
        return payload

    def task_origin(self, event: Any) -> str:
        """当前事件所属的任务类型（play/random/background），非任务返回空串。"""
        payload = self.play_payload(event)
        if payload:
            return str(payload.get("origin") or "play")
        raw = None
        try:
            raw = event.get_extra("cron_payload")
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            raw = None
        if isinstance(raw, dict):
            origin = str(raw.get("origin") or "")
            if origin in {
                "phone_play",
                "phone_random_play",
                "phone_background_task",
                CONTINUOUS_JOB_ORIGIN,
                AUTO_BROWSE_JOB_ORIGIN,
            }:
                return origin
        return ""

    def activity_allowed(self, event: Any) -> bool:
        from .play_window import activity_allowed
        return activity_allowed(self.config.auto, self.task_origin(event))

    def is_task_session(self, event: Any) -> bool:
        """当前会话是否为玩手机/后台任务（决定实时屏幕是否按需启动）。"""
        return bool(self.task_origin(event))

    def ensure_play_budget(self, event: Any) -> PlayBudget | None:
        """确保当前 play 任务的预算已登记，返回预算状态。"""
        payload = self.play_payload(event)
        if not payload:
            return None
        session = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            target = int(payload.get("target_tokens") or 0)
        except (TypeError, ValueError):
            target = 0
        if target <= 0:
            target = self.config.quota.play_task_tokens
        if target <= 0:
            # 0 = 不限制：不登记本轮预算，避免「预算已完成」提前收尾。
            return None
        budget = self.play_budgets.get(session)
        if budget is None or budget.target_tokens != target:
            budget = self.play_budgets.begin(session, target)
        budget.break_rule = self.config.flexibility.budget_active
        return budget

    def record_tokens(
        self,
        tokens: float,
        *,
        session: str = "",
        source: str = "",
        play: bool = False,
        input_other: int = 0,
        input_cached: int = 0,
        output: int = 0,
        actual: bool = False,
    ) -> None:
        """记录一次 token 用量（估算或实际 usage 对账）；play 任务同时累加到本轮预算。"""
        try:
            self.quota.record(
                tokens,
                session=session,
                source=source,
                play=bool(play),
                input_other=input_other,
                input_cached=input_cached,
                output=output,
                actual=actual,
            )
        except Exception as exc:  # noqa: BLE001 - 计费失败不影响主流程
            logger.warning("记录 token 用量失败：%s", exc)
            return
        # 玩手机预算只统计「开局之后」的消耗：初始请求的提示词成本仍计入全局额度，
        # 但不计入本轮 play 预算，否则低预算会把任务刚开局就判定为完成。
        if play and not str(source or "").startswith("llm_request"):
            self.play_budgets.add(session, float(tokens or 0))

    def record_internal_usage(
        self,
        usage: Any,
        *,
        source: str,
        event: Any = None,
        session: str = "",
        play: bool = False,
        prompt_text: str = "",
        completion_text: str = "",
        image_count: int = 0,
    ) -> float:
        """记录插件内部 ``llm_generate`` 调用的真实 usage（媒体识图/OCR/solver）。

        Agent 工具循环之外的模型调用不会进入 AstrBot 的 usage 统计钩子，必须按
        返回的 ``LLMResponse.usage`` 立即记账；usage 缺失时按提示词/图片数保守估算。
        """
        breakdown = usage_breakdown(usage)
        actual = not usage_is_empty(breakdown)
        if not actual:
            breakdown = estimate_fallback_usage(
                prompt_text=prompt_text,
                completion_text=completion_text,
                image_count=image_count,
            )
        tokens = float(breakdown.total)
        if tokens <= 0:
            return 0.0
        if event is not None:
            session = session or str(
                getattr(event, "unified_msg_origin", "") or ""
            )
            play = play or bool(self.play_payload(event))
            if play:
                self.ensure_play_budget(event)
        self.record_tokens(
            tokens,
            session=session,
            source=source,
            play=play,
            input_other=breakdown.input_other,
            input_cached=breakdown.input_cached,
            output=breakdown.output,
            actual=actual,
        )
        return tokens

    async def apply_task_provider(self, job: Any) -> bool:
        """为自主任务应用配置的模型供应商（由插件注入实现）；失败静默回退默认。"""
        hook = getattr(self, "task_provider_hook", None)
        if not callable(hook):
            return False
        try:
            return bool(await hook(job))
        except Exception as exc:  # noqa: BLE001 - 供应商应用失败不阻塞任务
            logger.warning("自主任务模型供应商应用失败：%s", exc)
            return False

    # ---- 设备 ---------------------------------------------------------

    async def connect(self) -> DeviceInfo:
        """强制重连设备并返回设备信息。"""
        self.ensure_configured()
        info = await self.adb.connect(force=True)
        self._log(f"已连接 {info.serial}")
        return info

    async def device_info(self) -> DeviceInfo:
        self.ensure_configured()
        return await self.adb.device_info()

    def _backend_status_lines(self) -> list[str]:
        """当前控制/感知后端状态行（失败时返回空）。"""
        manager = getattr(self, "backends", None)
        if manager is None:
            return []
        try:
            return manager.status_lines()
        except Exception:  # noqa: BLE001 - 后端状态失败不影响设备状态
            return []

    async def status_text(self) -> str:
        """生成设备状态的中文摘要；未配置 ADB 时返回可操作错误。"""
        self.guard_device("status")
        config = self.config
        if not config.adb.configured or self.adb is None:
            suffix = "；配置已更新，请重载插件后生效" if config.adb.configured else ""
            return (
                "手机能力未启用：未配置 ADB 地址/端口（假开启）。\n"
                "插件面板与配置可正常使用；"
                f"请在插件配置『ADB 连接』填写 adb.host 与 adb.port 后重新加载插件。{suffix}"
            )
        try:
            await self.adb.ensure_ready()
            info = await self.adb.device_info()
        except PhoneControlError as exc:
            lines = [
                f"ADB 目标：{config.adb.target}",
                f"连接状态：异常（{exc}）",
                "可执行 /phone connect 尝试重新连接；仍失败请检查模拟器与端口。",
            ]
            lines.extend(self._backend_status_lines())
            return "\n".join(lines)
        battery = (
            f"{info.battery_level}%（{info.battery_status or '未知状态'}）"
            if info.battery_level is not None
            else "未知"
        )
        screen_on = "亮屏" if info.screen_on else "灭屏"
        if info.screen_on is None:
            screen_on = "未知"
        lines = [
            "手机设备状态",
            f"ADB 目标：{info.serial or config.adb.target}",
                f"连接：{'已连接' if info.adb_connected else '未连接'}",
                f"型号：{info.manufacturer} {info.model}".strip()
                + f"（Android {info.android_version or '?'} / SDK {info.sdk or '?'}）",
                f"分辨率：{info.width}x{info.height} @ {info.density}dpi",
                f"设备类型：{dict(emulator='模拟器', physical='物理真机').get(info.device_kind, '未知')}；软键盘：{dict({True: '可见', False: '未显示'}).get(info.keyboard_visible, '未知')}；输入框聚焦后可直接注入文字",
            f"电量：{battery}",
                f"屏幕：{screen_on}",
            f"前台：{info.current_package or '未知'}"
            + (f"/{info.current_activity}" if info.current_activity else ""),
        ]
        lines.extend(self._backend_status_lines())
        lines.append(self.experiences.status_text())
        images = config.image
        destination = "两地" if images.save_images_both else {"server": "AstrBot 服务端", "phone": "手机"}[images.image_storage_location]
        lines.append(f"图片保存：{'开启' if images.auto_save_images else '关闭'}；存储位置：{destination}；phone_saved_images 可查询与传手机")
        if hasattr(self, "_contents"):
            lines.append("内容任务：" + json.dumps(self.contents.status(), ensure_ascii=False))
        return "\n".join(lines)

    async def screen(self, max_nodes: int = 0) -> tuple[str, ScreenSnapshot]:
        """读取 UI 树并渲染成紧凑文本，同时返回快照。"""
        self.guard_device("screen")
        self.ensure_configured()
        config = self.config
        await self.adb.ensure_ready()
        snapshot = await dump_ui(self.adb, timeout=config.perception.dump_timeout)
        limit = max_nodes if max_nodes and max_nodes > 0 else config.perception.max_ui_nodes
        text = render_ui(
            snapshot,
            max_nodes=limit,
            max_text_len=config.perception.max_text_len,
        )
        self._cache_ui_snapshot(snapshot)
        return text, snapshot

    async def find_node(self, *, text: str = "", desc: str = "") -> UiNode | None:
        """在当前 UI 树里按文本/描述定位控件（优先复用新鲜快照）。"""
        cached = self._cached_ui_snapshot()
        if cached is not None:
            matches = cached.find(
                text=text or None,
                desc=desc or None,
            )
            if matches and await self._ui_cache_foreground_matches(cached):
                return await self._visible_node(matches, text=text, desc=desc)
        _, snapshot = await self.screen()
        matches = snapshot.find(
            text=text or None,
            desc=desc or None,
        )
        return await self._visible_node(matches, text=text, desc=desc)

    async def _visible_node(self, matches: list[UiNode], *, text="", desc="") -> UiNode | None:
        if not matches:
            return None
        width, height = await self.actions.screen_size()
        visible = [node for node in matches if node.enabled and node.bounds.visible_center(width, height) is not None]
        def rank(node):
            exact = (not text or node.text.casefold() == text.casefold()) and (not desc or node.desc.casefold() == desc.casefold())
            return not exact, not node.clickable, node.bounds.width * node.bounds.height
        return min(visible, key=rank) if visible else matches[0]

    async def node_click_point(self, node: UiNode) -> tuple[int, int]:
        """按当前屏幕与控件的交集定位触摸点。"""
        width, height = await self.actions.screen_size()
        point = node.bounds.visible_center(width, height)
        if not node.enabled or point is None:
            raise PhoneControlError("目标控件当前不可点击或完全在屏幕外，请重新查看屏幕并滚动到可见位置")
        return point

    # ---- UI 快照缓存 ---------------------------------------------------

    def _cache_ui_snapshot(self, snapshot: ScreenSnapshot) -> None:
        """记录最近一次成功读取的 UI 快照。"""
        self._ui_cache = (time.monotonic(), snapshot)

    def _cached_ui_snapshot(self) -> ScreenSnapshot | None:
        """返回 TTL 内的 UI 快照；过期则丢弃。"""
        cached = self._ui_cache
        if cached is None:
            return None
        captured_at, snapshot = cached
        if time.monotonic() - captured_at > self._ui_cache_ttl:
            self._ui_cache = None
            return None
        return snapshot

    async def _ui_cache_foreground_matches(self, snapshot: ScreenSnapshot) -> bool:
        """校验缓存快照的前台 App 与当前一致，避免复用过时页面。"""
        expected = str(getattr(snapshot, "package", "") or "")
        if not expected:
            return True
        if self.actions is None:
            return False
        try:
            current, _activity = await self.actions.foreground()
        except PhoneControlError:
            return False
        return not current or current == expected

    def _invalidate_ui_cache(self) -> None:
        """任何会改变页面内容的动作后失效缓存，并记录页面变化时间。

        页面变化时间是抓包侧判断「哪些流量属于当前页面」的下界：动作前的流量
        可能来自上一页面或后台 App，不能当作当前页面内容。
        """
        self._ui_cache = None
        self._page_changed_at = time.time()
        self._page_traffic_cache = None

    @staticmethod
    def _normalize_capture_result(
        result: Any, fallback_path: Path
    ) -> tuple[Path, dict[str, Any]]:
        """归一化截图返回：Path/str/tuple/dict/带属性的对象。"""
        meta: dict[str, Any] = {}
        path: Any = None
        if result is None:
            path = fallback_path
        elif isinstance(result, Path):
            path = result
        elif isinstance(result, str):
            path = result
        elif isinstance(result, dict):
            path = result.get("path") or result.get("file") or fallback_path
            method = result.get("method") or result.get("format")
            if method:
                meta["method"] = str(method).lower()
            elapsed = (
                result.get("elapsed_ms")
                or result.get("duration_ms")
                or result.get("cost_ms")
            )
            if elapsed is not None:
                meta["elapsed_ms"] = float(elapsed)
        elif isinstance(result, tuple):
            for item in result:
                if path is None and isinstance(item, (Path, str)):
                    path = item
                elif isinstance(item, dict):
                    meta.update(
                        {
                            key: value
                            for key, value in item.items()
                            if key in {"method", "format", "elapsed_ms", "duration_ms", "cost_ms"}
                        }
                    )
            path = path or fallback_path
        else:
            for attr in ("path", "file"):
                value = getattr(result, attr, None)
                if isinstance(value, (Path, str)):
                    path = value
                    break
            method = getattr(result, "method", None) or getattr(
                result, "format", None
            )
            if method:
                meta["method"] = str(method).lower()
            elapsed = getattr(result, "elapsed_ms", None) or getattr(
                result, "duration_ms", None
            )
            if elapsed is not None:
                meta["elapsed_ms"] = float(elapsed)
            path = path or fallback_path
        if "method" not in meta:
            suffix = str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else "png"
            meta["method"] = "jpeg" if suffix in {"jpg", "jpeg"} else "png"
        return Path(path), meta

    def _record_screenshot_metric(
        self, meta: dict[str, Any] | None, *, fallback_ms: float = 0.0
    ) -> None:
        """记录一次截图耗时指标（异常忽略，不影响截图流程）。"""
        try:
            info = meta or {}
            elapsed = info.get("elapsed_ms")
            try:
                ms = float(elapsed) if elapsed is not None else float(fallback_ms)
            except (TypeError, ValueError):
                ms = float(fallback_ms or 0.0)
            self.telemetry.record_screenshot(
                ms=ms,
                source=str(info.get("source") or "adb"),
                method=str(info.get("method") or info.get("format") or "png"),
                **({"frame_age_ms":info["frame_age_ms"]} if "frame_age_ms" in info else {}),
            )
        except Exception:  # noqa: BLE001 - 指标失败不影响截图
            pass

    async def capture(
        self,
        *,
        fast: bool = False,
        expected_package: str = "",
        quiet: bool = False,
        record_metric: bool = True,
        persist: bool = True,
    ) -> tuple[bytes, Path, dict[str, Any]]:
        """截图并返回 ``(字节, 路径, 元信息)``。

        优先用动作层 ``screenshot(path, expected_package=..., fast=...)``；
        不支持该签名时退化为 ``screenshot(path)``。fast 由 ``perception.screenshot_mode`` 决定：
        auto 交给动作层自适应（静态 PNG、PNG 慢时切 raw），raw 强制快速截图。

        ``quiet`` / ``record_metric`` / ``persist`` 供实时屏幕抓帧使用：不写 info 日志、
        不记截图指标、固定覆盖同一张临时文件，避免每帧刷日志/堆积截图。
        """
        self.guard_device("capture")
        self.ensure_configured()
        # 每次截图顺带按节流清理过期截图（后台另有 60 秒定时清理）。
        self.cleanup_screenshots()
        config = self.config
        mode = config.perception.screenshot_mode
        use_fast = bool(fast) or mode == "raw"
        manager = getattr(self, "backends", None)
        if manager is not None:
            expected = str(expected_package or "").strip()
            try:
                perception = await manager.perception()
                data, fmt, meta = await perception.screenshot(
                    fast=use_fast, expected_package=expected
                )
            except PhoneControlError:
                raise
            except Exception as exc:  # noqa: BLE001 - 感知后端异常统一转错误
                raise ActionFailedError(f"感知后端截图失败：{exc}") from exc
            suffix = ".jpg" if str(fmt).lower() in {"jpeg", "jpg"} else ".png"
            if persist:
                self.screenshots_dir.mkdir(parents=True, exist_ok=True)
                path = self.screenshots_dir / f"shot-{int(time.time() * 1000)}{suffix}"
            else:
                path = self.data_dir / f"live_frame_last{suffix}"
            try:
                path.write_bytes(data)
            except OSError as exc:
                raise ActionFailedError(f"保存截图失败：{path}（{exc}）") from exc
            meta = dict(meta or {})
            meta.setdefault("method", fmt)
            meta.setdefault("format", fmt)
            meta["path"] = str(path)
            if not quiet:
                self._log(
                    f"截图完成：source={meta.get('source', 'backend')}，"
                    f"method={meta.get('method')}，耗时={meta.get('elapsed_ms')}ms"
                )
            manager.record_capture(str(meta.get("source") or "scrcpy"))
            if record_metric:
                self._record_screenshot_metric(meta)
            return data, path, meta
        foreground_package = str(expected_package or "").strip()
        if not foreground_package:
            try:
                foreground_package, _activity = await self.actions.foreground()
            except PhoneControlError:
                foreground_package = ""
        if persist:
            self.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshots_dir / f"shot-{int(time.time() * 1000)}.png"
        else:
            path = self.data_dir / "live_frame_last.png"
        started = time.monotonic()
        kwargs = {"expected_package": foreground_package or None, "fast": use_fast}
        fallback_used = False
        try:
            result = await self.actions.screenshot(path, **kwargs)
        except TypeError:
            # 动作层不支持带 fast/expected_package 的签名：降级为基础调用
            result = await self.actions.screenshot(path)
        except PhoneControlError:
            if not use_fast:
                raise
            # 快速截图失败时回退普通截图，并在返回元信息里标注
            if not quiet:
                self._log("快速截图失败，回退普通截图")
            fallback_used = True
            result = await self.actions.screenshot(path)
        elapsed_fallback = round((time.monotonic() - started) * 1000, 1)
        resolved, meta = self._normalize_capture_result(result, path)
        # 动作层会把 method/duration_ms/retries/mismatch 等写入 last_screenshot_meta
        action_meta = getattr(self.actions, "last_screenshot_meta", None)
        if isinstance(action_meta, dict):
            for key in (
                "method",
                "source",
                "retries",
                "mismatch",
                "actual",
                "before",
                "after",
            ):
                value = action_meta.get(key)
                if value is not None and value != "":
                    meta[key] = value
            duration = action_meta.get("duration_ms")
            if isinstance(duration, (int, float)):
                meta["elapsed_ms"] = float(duration)
            if action_meta.get("path"):
                resolved = Path(str(action_meta["path"]))
        manager = getattr(self, "backends", None)
        if getattr(self.adb, "app_mode", False):
            meta["source"] = self.adb.screen_source
        if manager is not None:
            manager.record_capture(str(meta.get("source") or "adb"))
        meta.setdefault("elapsed_ms", elapsed_fallback)
        meta.setdefault("fast", use_fast)
        if fallback_used:
            meta["fast"] = False
            meta["fallback"] = True
        try:
            data = Path(resolved).read_bytes()
        except OSError as exc:
            raise ActionFailedError(f"读取截图文件失败：{resolved}（{exc}）") from exc
        if not quiet:
            self._log(
                f"截图完成：method={meta.get('method')}，"
                f"耗时={meta.get('elapsed_ms')}ms，fast={use_fast}"
            )
        if record_metric:
            self._record_screenshot_metric(meta, fallback_ms=elapsed_fallback)
        return data, Path(resolved), meta

    async def screenshot(self) -> tuple[bytes, Path]:
        """截图并落盘，返回图片字节与路径。"""
        data, path, _meta = await self.capture()
        return data, path

    # ---- 抓包页面内容（抓包与屏幕识别共用） ----------------------------

    async def page_traffic(
        self,
        *,
        foreground_package: str = "",
        activity: str = "",
        compact: bool = False,
        force_refresh: bool = False,
        limit: int = PAGE_FLOW_LIMIT,
    ) -> PageTrafficSnapshot:
        """构造当前页面的抓包内容快照。

        选择口径：前台 App 匹配 + 最近一次设备写动作之后 + 未过期（默认 90 秒），
        同一页面只保留最新一条。抓包未开启时快速返回不可用快照、不访问设备；
        查询前台失败时退化为最近流量，并在快照 notes 里说明。

        Args:
            foreground_package: 已知前台包名（屏幕工具已查过时传入，避免重复查询）。
            activity: 已知前台 Activity。
            compact: 是否用更短的正文/总量上限（``phone_screen`` 附带展示用）。
            force_refresh: 忽略 1.5 秒短缓存（抓包工具显式取最新时使用）。
            limit: 最多保留多少个页面分组（同页面只取最新一条），默认 6。
        """
        traffic = self.traffic
        if traffic is None:
            return build_page_traffic(running=False, error="抓包未初始化")
        try:
            status = traffic.status()
        except Exception as exc:  # noqa: BLE001 - 状态异常按不可用降级
            return build_page_traffic(running=False, error=f"抓包状态不可用：{exc}")
        if not status.get("running"):
            return build_page_traffic(
                running=False,
                error=str(status.get("error") or "抓包未开启"),
                foreground_package=foreground_package,
                activity=activity,
            )

        now_mono = time.monotonic()
        cached = self._page_traffic_cache
        if (
            not force_refresh
            and not foreground_package
            and cached is not None
            and now_mono - cached[0] <= self._page_traffic_cache_ttl
        ):
            return cached[1]

        package = str(foreground_package or "")
        act = str(activity or "")
        if not package and self.configured and self.actions is not None:
            try:
                package, act = await self.actions.foreground()
            except Exception as exc:  # noqa: BLE001 - 前台查询失败退化为最近流量
                self._log(f"页面识别：查询前台 App 失败，按最近流量展示（{exc}）")
                package, act = "", ""
        config = self.config
        keep = int(getattr(config.traffic, "keep_seconds", 900) or 900)
        body_chars = int(getattr(config.traffic, "body_chars", 1200) or 1200)
        if compact:
            body_chars = min(body_chars, PAGE_COMPACT_BODY_CHARS)
        try:
            flows = traffic.recent(limit=PAGE_FLOW_LIMIT * 10, since_seconds=keep)
        except Exception as exc:  # noqa: BLE001 - 记录读取失败按不可用降级
            return build_page_traffic(
                running=False,
                error=f"读取抓包记录失败：{exc}",
                foreground_package=package,
                activity=act,
            )
        recent_action = 0.0
        if self._page_changed_at and (
            time.time() - self._page_changed_at <= PAGE_ACTION_WINDOW_MAX_SECONDS
        ):
            recent_action = self._page_changed_at
        snapshot = build_page_traffic(
            flows=flows,
            running=True,
            foreground_package=package,
            activity=act,
            since_ts=recent_action,
            limit=max(1, min(int(limit or PAGE_FLOW_LIMIT), 12)),
            body_chars=body_chars,
            max_total_chars=(
                PAGE_COMPACT_MAX_TEXT_CHARS if compact else PAGE_MAX_TEXT_CHARS
            ),
            now=time.time(),
        )
        if not foreground_package:
            self._page_traffic_cache = (now_mono, snapshot)
        return snapshot

    # ---- 流媒体内容识别 ------------------------------------------------

    def _provider_settings(self, event: Any) -> dict[str, Any]:
        """读取 AstrBot provider_settings（图片描述 Provider 等）。"""
        if self.app_context is None:
            return {}
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            try:
                cfg = self.app_context.get_config(umo=umo)
            except TypeError:
                cfg = self.app_context.get_config()
        except Exception:  # noqa: BLE001 - 配置读取失败按无配置处理
            return {}
        try:
            settings = cfg.get("provider_settings", {}) or {}
        except Exception:  # noqa: BLE001
            settings = getattr(cfg, "provider_settings", {}) or {}
        return settings if isinstance(settings, dict) else {}

    async def _resolve_media_stt_provider(self, event: Any, config: PhoneConfig) -> tuple[Any, str]:
        """选语音识别 Provider：插件指定 → AstrBot 当前 STT；返回 ``(实例, 说明)``。"""
        configured = str(config.model.asr_provider_id or config.media.asr_provider_id or "").strip()
        app_context = self.app_context
        if app_context is None:
            return None, "AstrBot 上下文不可用"
        if configured:
            provider = app_context.get_provider_by_id(configured)
            if provider is None:
                return None, f"未找到语音识别 Provider「{configured}」"
            if not callable(getattr(provider, "get_text", None)):
                return None, f"语音识别 Provider「{configured}」需要使用音频对话模型转写"
            return provider, f"插件配置 {configured}"
        getter = getattr(app_context, "get_using_stt_provider_async", None)
        if not callable(getter):
            return None, "AstrBot 不支持语音识别 Provider 查询"
        try:
            provider = await getter(str(getattr(event, "unified_msg_origin", "") or ""))
        except Exception as exc:  # noqa: BLE001 - Provider 获取失败按不可用处理
            return None, f"获取语音识别 Provider 失败：{exc}"
        if provider is None:
            return None, "AstrBot 未配置语音识别（STT）Provider"
        provider_id = ""
        try:
            provider_id = str(provider.meta().id or "")
        except Exception:  # noqa: BLE001 - 鸭子类型 Provider
            provider_id = str(getattr(provider, "provider_config", {}).get("id") or "")
        return provider, f"AstrBot 当前语音识别 Provider {provider_id or ''}".strip()

    async def _resolve_media_vision_provider_id(self, event: Any, config: PhoneConfig) -> tuple[str, str]:
        """选画面描述 Provider：插件指定 → OCR Provider → 图片描述 → 当前会话 Provider。"""
        direct = str(config.media.vision_provider_id or "").strip()
        if direct:
            return direct, "插件配置 media.vision_provider_id"
        ocr = str(config.perception.ocr_provider_id or "").strip()
        if ocr:
            return ocr, "插件配置 perception.ocr_provider_id"
        caption = str(self._provider_settings(event).get("default_image_caption_provider_id") or "").strip()
        if caption:
            return caption, "AstrBot 图片描述 Provider"
        app_context = self.app_context
        if app_context is None:
            return "", "AstrBot 上下文不可用"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        getter = getattr(app_context, "get_using_provider_async", None)
        try:
            provider = await getter(umo) if callable(getter) else None
        except Exception as exc:  # noqa: BLE001 - Provider 获取失败按不可用处理
            return "", f"获取当前会话 Provider 失败：{exc}"
        if provider is None:
            return "", "AstrBot 未配置可用的画面理解 Provider"
        provider_id = ""
        try:
            provider_id = str(provider.meta().id or "")
        except Exception:  # noqa: BLE001 - 鸭子类型 Provider
            provider_config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(provider_config.get("id") or "")
        return provider_id, "当前会话 Provider"

    async def watch_media(self, event: Any, *, seconds: float = 0, question: str = "", mode: str = "auto", stream_kind: str = "auto", video_url: str = "", duration_seconds: float = 0, play_x=None, play_y=None) -> str:
        getter = getattr(event, "get_extra", None)
        run = lambda: self._watch_media_direct(event, seconds=seconds, question=question, mode=mode, stream_kind=stream_kind, video_url=video_url, duration_seconds=duration_seconds, play_x=play_x, play_y=play_y)
        if callable(getter) and getter("phone_agent_run_active"):
            return await self.contents.poll_watch(event, run)
        return await run()

    async def _watch_media_direct(self, event: Any, *, seconds=0, question="", mode="auto", stream_kind="auto", video_url="", duration_seconds=0, play_x=None, play_y=None) -> str:
        """按配置识别完整点播或连续片段，直播始终分段。"""
        self.guard_device("watch_media")
        self.ensure_configured()
        if self.config.media.full_video_enabled and mode != "audio":
            try:
                result = await self.contents.full.watch(event, seconds=seconds, question=question, mode=mode, stream_kind=stream_kind, video_url=video_url, duration_seconds=duration_seconds, play_x=play_x, play_y=play_y)
                return json.dumps(result, ensure_ascii=False)
            finally:
                getter = getattr(event, "get_extra", None)
                if not callable(getter) or not getter("phone_agent_run_active"):
                    await self.contents.finish_owner(self.contents.owner(event), abnormal=False)
        return json.dumps(await self._watch_segment(event, seconds=seconds, question=question, mode=mode), ensure_ascii=False)

    async def _watch_segment(self, event: Any, *, seconds=0, question="", mode="auto"):
        manager=self.contents
        owner=manager.owner(event)
        existing = manager.active.get(owner)
        if existing and existing.get("_full_request") and not existing.get("_segmented_reason"):
            await manager.finish_owner(owner, abnormal=False)
        if self.config.flexibility.free_browse_active and owner in manager.active:
            package, _ = await self.actions.foreground()
            current = manager.current(event)
            if current["kind"] != "video" or current["package"] != package:
                await manager.finish_owner(owner, abnormal=False)
        if owner not in manager.active:
            package,activity=await self.actions.foreground()
            await manager.begin(event,kind="video",content_id=f"{package}/{activity}",question=question,mode=mode)
        if manager.current(event)["kind"]!="video":
            raise PhoneControlError("当前是图文任务，请结束后再看视频")
        try:
            result=await manager.read(event,seconds=seconds)
            return result
        finally:
            getter=getattr(event,"get_extra",None)
            if not callable(getter) or not getter("phone_agent_run_active"):
                await manager.finish_owner(owner,abnormal=False)

    # ---- 截图自动清理 --------------------------------------------------

    def cleanup_screenshots(self, *, force: bool = False) -> dict[str, Any]:
        """按配置保留时长清理过期截图（非 force 时最多每 30 秒执行一次）。"""
        result: dict[str, Any] = {
            "enabled": False,
            "deleted": 0,
            "bytes_freed": 0,
            "scanned": 0,
            "errors": 0,
            "skipped": True,
        }
        try:
            perception = self.config.perception
        except Exception:  # noqa: BLE001 - 配置异常时不动磁盘
            return result
        if hasattr(self, "_contents"):
            self._contents.cleanup()
        if not perception.screenshot_auto_clean:
            # 关闭自动清理时不删任何文件（force 仅跳过节流，不绕过开关）。
            return result
        now = time.monotonic()
        if not force and (now - self._last_screenshot_cleanup) < 30.0:
            return result
        self._last_screenshot_cleanup = now
        try:
            cleaned = cleanup_directory(
                self.screenshots_dir,
                retention_minutes=perception.screenshot_retention_minutes,
            )
        except Exception as exc:  # noqa: BLE001 - 清理失败不能影响截图主流程
            logger.warning("截图自动清理失败：%s", exc)
            result["errors"] = 1
            return result
        result.update(cleaned)
        result["enabled"] = True
        result["skipped"] = False
        if cleaned.get("deleted"):
            logger.debug(
                "截图自动清理：保留 %s 分钟，删除 %s 个旧截图，释放 %.1f MB",
                cleaned.get("retention_minutes"),
                cleaned.get("deleted"),
                int(cleaned.get("bytes_freed") or 0) / (1024 * 1024),
            )
        return result

    # ---- 快速截图 / 人机验证辅助 --------------------------------------

    async def fast_image(self) -> tuple[bytes, str]:
        """快速截一张图（不跑 uiautomator dump、不记指标），返回 ``(bytes, mime)``。"""
        data, _path, meta = await self.capture(
            fast=True,
            quiet=True,
            record_metric=False,
            persist=False,
        )
        method = str(meta.get("method") or meta.get("format") or "png").lower()
        mime = "image/jpeg" if method in {"jpeg", "jpg"} else "image/png"
        return data, mime

    def _ensure_verify_deadline(self) -> None:
        """验证批次内复查单次尝试期限；到期抛出可中止批次的执行错误。"""
        controller = getattr(self, "verification", None)
        if controller is None:
            return
        try:
            remaining = controller.attempt_remaining_seconds()
        except Exception:  # noqa: BLE001 - 期限读取失败不阻塞手动操作
            return
        if remaining <= 0:
            raise PhoneControlError("本次验证尝试已到期，停止继续操作并转人工协助")

    async def exec_verify_ops(
        self,
        ops: Any,
        *,
        confirm: Any = None,
        wait_seconds: float = 1.0,
    ) -> str:
        """执行验证码/做题求解器给出的操作序列（tap/drag + 最终确认点）。

        设备动作前先整体解析并校验完整 ops 与 confirm（动作类型、数量、坐标范围
        等），任何非法项都不会产生设备动作；执行阶段逐步复查设备门禁与单次尝试
        期限，暂停/取消/期限变化立即中止后续步骤。
        """
        self.ensure_configured()
        raw_ops, raw_confirm = _validate_verify_batch(ops, confirm)
        width, height = await self.actions.screen_size()
        plan, confirm_point = _plan_verify_steps(
            raw_ops, raw_confirm, width, height
        )
        steps: list[str] = []
        for step in plan:
            # 批次内每步都复查执行门禁：暂停/取消代次/期限变化后立即停止。
            self.guard_device("verify_op")
            self._ensure_verify_deadline()
            if step["action"] == "drag":
                await self.actions.swipe(
                    step["x1"],
                    step["y1"],
                    step["x2"],
                    step["y2"],
                    duration_ms=step["duration_ms"],
                )
                steps.append(
                    f"drag({step['x1']},{step['y1']})"
                    f"→({step['x2']},{step['y2']})"
                )
            else:
                await self.tap(step["x"], step["y"])
                steps.append(f"tap({step['x']},{step['y']})")
            await asyncio.sleep(0.15)
        if confirm_point is not None:
            self.guard_device("verify_confirm")
            # 作用域期限可能在预占尝试前按「无限」记录；确认前按当前尝试剩余
            # 期限再复查一次，避免到期后的最终确认点击。
            self._ensure_verify_deadline()
            await self.tap(confirm_point["x"], confirm_point["y"])
            steps.append(
                f"确认 tap({confirm_point['x']},{confirm_point['y']})"
            )
        self._invalidate_ui_cache()
        try:
            pause = max(0.2, min(3.0, float(wait_seconds or 0.2)))
        except (TypeError, ValueError):
            pause = 1.0
        await asyncio.sleep(pause)
        if not steps:
            return "没有可执行的操作（ops 为空）"
        return "已执行验证操作：" + "，".join(steps)

    def _find_inbox_node(self, snapshot: ScreenSnapshot) -> UiNode | None:
        """在当前 UI 树里找最像「消息/回复我的/互动消息」入口的节点。"""
        best: UiNode | None = None
        best_score = 0
        for node in snapshot.nodes:
            label = " ".join(
                part for part in (node.text, node.desc) if part
            ).strip()
            if not label or len(label) > 24:
                continue
            if any(word in label for word in _INBOX_EXCLUDE_WORDS):
                continue
            for keyword, weight in _INBOX_KEYWORDS:
                if keyword not in label:
                    continue
                score = weight + (10 if node.clickable else 0)
                if score > best_score:
                    best_score = score
                    best = node
                break
        return best

    @staticmethod
    def _find_slider_node(snapshot: ScreenSnapshot) -> UiNode | None:
        """在 UI 树里找滑块/进度条类节点（用于自动滑动验证）。"""
        candidates: list[UiNode] = []
        for node in snapshot.nodes:
            class_name = str(node.class_name or "")
            label = " ".join(part for part in (node.text, node.desc) if part).strip()
            looks_slider = (
                "SeekBar" in class_name
                or "ProgressBar" in class_name
                or any(
                    word in label
                    for word in ("滑块", "拖动", "滑动验证", "按住滑块", "向右滑动")
                )
            )
            if looks_slider and node.bounds.width >= 60:
                candidates.append(node)
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.bounds.width)

    async def check_inbox(
        self,
        *,
        app: str = "",
        include_image: bool = True,
        max_nodes: int = 0,
    ) -> tuple[str, bytes | None, str]:
        """进入 App（可选）并尝试打开「消息/回复我的/互动消息」页，返回文本+截图。"""
        self.guard_device("check_inbox")
        self.ensure_configured()
        lines: list[str] = ["【收件箱检查】"]
        if app:
            # 切 App 属于设备写副作用：即使工具分类漏标为只读，也要按写语义复查。
            self.guard_device_write("phone_check_inbox:open_app")
            lines.append(await self.open_app(app))
            await asyncio.sleep(1.5)
        _, snapshot = await self.screen(max_nodes=max_nodes)
        node = self._find_inbox_node(snapshot)
        page_text = ""
        if node is not None:
            label = node.label or node.text or node.desc or "消息入口"
            center_x, center_y = node.bounds.center
            # 点击消息入口同样属于设备写副作用。
            self.guard_device_write("phone_check_inbox:tap")
            await self.tap(center_x, center_y)
            await asyncio.sleep(1.2)
            page_text, _snapshot = await self.screen(max_nodes=max_nodes)
            lines.append(f"已尝试打开消息入口：{label}")
        else:
            lines.append(
                "当前页面没有找到明显的「回复我的/互动消息/消息/通知」入口；"
                "请用 phone_screen 自己找消息 tab 或顶部入口。"
            )
        lines.append(
            "看到别人回复你的内容后：先按心情与内容判断值不值得回；"
            "要回才 phone_interaction_gate(kind=\"passive_reply\") 申请名额，"
            "回复后用 phone_interaction_record 记录；不想回就跳过，不要硬回。"
        )
        if page_text:
            verify_words = ("验证", "滑块", "识图", "转正答题", "人机")
            if any(word in page_text for word in verify_words):
                lines.append(
                    "当前页面可能有人机验证/答题：优先 phone_solve_verification"
                    "（两阶段识图+自动执行，含最终确认点）；需要手动精细控制再用 "
                    "phone_bypass_verify；不要反复 phone_wait/phone_screen 干等。"
                )
            lines.append("")
            lines.append(page_text)
        image: bytes | None = None
        mime = ""
        if include_image:
            image, mime = await self.fast_image()
        return "\n".join(lines), image, mime

    async def _slide_verification(self) -> str:
        """尝试从 UI 树定位滑块并自动滑到最右（失败时让模型用截图+坐标）。"""
        _, snapshot = await self.screen()
        node = self._find_slider_node(snapshot)
        if node is None:
            return (
                "UI 树里没有找到滑块/进度条节点；请用 capture 快速截图后自己判断坐标，"
                "再用 action=drag 一次完成。"
            )
        width, height = await self.actions.screen_size()
        bounds = node.bounds
        center_y = max(1, min(height - 2, bounds.center_y))
        if bounds.width >= 500:
            start_x = max(1, bounds.left + 30)
            end_x = min(width - 2, bounds.right - 15)
        else:
            start_x = max(1, min(width - 2, bounds.center_x))
            end_x = min(width - 2, start_x + max(120, width // 3))
        await self.actions.swipe(
            start_x, center_y, end_x, center_y, duration_ms=800
        )
        self._invalidate_ui_cache()
        await asyncio.sleep(0.8)
        return (
            f"已尝试自动滑动滑块：({start_x},{center_y})→({end_x},{center_y})"
            "（以 UI 树节点 bounds 为参考）；如未通过请 capture 截图后手动 drag。"
        )

    async def bypass_verify(
        self,
        *,
        action: str = "capture",
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        duration_ms: int = 400,
        points: Any = None,
        interval_ms: int = 250,
        include_image: bool = True,
    ) -> tuple[str, bytes | None, str]:
        """人机验证辅助：快速截图 / 拖拽 / 多点顺序点击 / 自动滑滑块。"""
        self.guard_device("bypass_verify")
        self.ensure_configured()
        name = str(action or "capture").strip().lower()
        width, height = await self.actions.screen_size()

        def _point(value: Any, axis: str, limit: int) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ActionFailedError(f"{axis} 必须是整数") from exc
            if number < 0 or number >= limit:
                raise ActionFailedError(f"{axis} 坐标越界：{number}，有效范围 0..{limit - 1}")
            return number

        if name in {"capture", "screenshot"}:
            text = (
                "已快速截图（不跑 uiautomator dump，约 0.6s 级）。看图后："
                "滑块用 action=drag（或先试 action=slide 自动拖）；"
                "顺序点击/识图点击用 action=tap_points 一次传入全部坐标，不要反复 dump。"
            )
        elif name == "drag":
            start_x = _point(x1, "x1", width)
            start_y = _point(y1, "y1", height)
            end_x = _point(x2, "x2", width)
            end_y = _point(y2, "y2", height)
            duration = max(100, min(5000, int(duration_ms or 400)))
            await self.actions.swipe(
                start_x, start_y, end_x, end_y, duration_ms=duration
            )
            self._invalidate_ui_cache()
            await asyncio.sleep(0.3)
            text = f"已拖拽 ({start_x},{start_y})→({end_x},{end_y})，{duration}ms"
        elif name == "tap_points":
            raw_points = points if isinstance(points, list) else []
            if not 1 <= len(raw_points) <= 12:
                raise ActionFailedError("tap_points 需要 1-12 个点（[{x,y}, ...]）")
            cleaned: list[tuple[int, int]] = []
            for item in raw_points:
                if not isinstance(item, dict):
                    raise ActionFailedError("points 的每一项必须是 {x,y} 对象")
                cleaned.append(
                    (
                        _point(item.get("x"), "x", width),
                        _point(item.get("y"), "y", height),
                    )
                )
            wait = max(0, min(3000, int(interval_ms or 250))) / 1000.0
            for index, (point_x, point_y) in enumerate(cleaned):
                await self.tap(point_x, point_y)
                if wait and index < len(cleaned) - 1:
                    await asyncio.sleep(wait)
            text = f"已按顺序点击 {len(cleaned)} 个点：{cleaned}"
        elif name == "slide":
            text = await self._slide_verification()
        else:
            raise ActionFailedError(
                "action 必须是 capture/drag/tap_points/slide 之一"
            )
        image: bytes | None = None
        mime = ""
        if include_image:
            image, mime = await self.fast_image()
        return text, image, mime

    async def learn(
        self,
        content: Any,
        *,
        source: str = "",
        tags: Any = None,
        attribution: str = "",
        origin: str = "",
        origin_detail: str = "",
        initiator_user_id: str = "",
        requested_by: str = "",
    ) -> str:
        """记录一条学习内容（供持续刷/自主学习使用）。

        本地存储关闭时不再写入插件本地学习档案，只返回结果文案；调用方负责
        判断是否还有 MemoryCompanion / LivingMemory 同步等远端去向。
        """
        local_enabled = bool(self.config.memory.local_enabled)
        record: dict[str, Any] = {}
        total = 0
        if local_enabled:
            try:
                record = self.learning.record(
                    content=content,
                    source=source,
                    tags=tags if isinstance(tags, list) else [],
                    attribution=attribution,
                    origin=origin,
                    origin_detail=origin_detail,
                    initiator_user_id=initiator_user_id,
                    requested_by=requested_by,
                )
            except LearningError as exc:
                raise ActionFailedError(str(exc)) from exc
            total = len(self.learning.entries())
            self.request_semantic_refresh()
        snippet = str(record.get("content") or content or "")[:80]
        if not local_enabled:
            return f"本地存储已关闭，学习内容未写入插件本地：{snippet}"
        return f"已记录学习内容（累计 {total} 条）：{snippet}"

    def learning_attribution(self, event: Any) -> dict[str, str]:
        """判定当前学习记录的来源归属与发起用户。

        Returns:
            ``{"origin","origin_detail","kind","initiator_user_id","requested_by"}``；
            自行唤醒（随机/持续/定时自主浏览）的 ``kind=autonomous``，用户指定使用
            手机（会话工具、命令、用户发起的后台任务/玩手机）为 ``kind=user``。
        """
        origin = ""
        try:
            origin = self.task_origin(event)
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            origin = ""
        payload: dict[str, Any] = {}
        try:
            raw = event.get_extra("cron_payload")
            if isinstance(raw, dict):
                payload = raw
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            payload = {}
        requested_by = str(
            payload.get("requested_by") or payload.get("sender_id") or ""
        ).strip()
        initiator = ""
        try:
            initiator = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            initiator = ""
        kind = origin_kind(origin)
        if kind == KIND_UNKNOWN and payload.get("play_task"):
            kind = origin_kind(str(payload.get("origin") or ""))
        if not initiator and kind == "user":
            initiator = requested_by
        detail = str(payload.get("job_id") or payload.get("session") or "").strip()
        return {
            "origin": origin,
            "origin_detail": detail,
            "kind": kind,
            "initiator_user_id": initiator,
            "requested_by": requested_by,
        }

    def request_semantic_refresh(self) -> None:
        """请求后台增量刷新语义索引（只置位事件，不做 IO、不创建任务）。"""
        hook = getattr(self, "semantic_refresh_hook", None)
        if not callable(hook):
            return
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - 索引刷新失败不影响主流程
            logger.debug("请求语义索引刷新失败（忽略）：%s", exc)

    # ---- 会话记录 / 主动分享 -----------------------------------------

    def remember_session(self, umo: str) -> None:
        """记录最近一次与用户交互的会话 UMO（供 phone_share_to_user 兜底）。"""
        target = str(umo or "").strip()
        if not target:
            return
        try:
            path = self.data_dir / "last_session.json"
            payload = {"umo": target, "updated_at": time.time()}
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning("记录最近会话失败：%s", exc)

    def last_session(self) -> str:
        """读取最近一次与用户交互的会话 UMO（没有则返回空串）。"""
        path = self.data_dir / "last_session.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("umo") or "").strip()

    # ---- 分享策略（AI 自己判断目标 + 白天免打扰） ----------------------

    def is_autonomous_play(self, event: Any) -> bool:
        """事件是否为 AI 自刷/随机/持续刷/后台玩手机（非用户直接任务）。"""
        try:
            return bool(self.play_payload(event))
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            return False

    def share_allowed_now(self) -> tuple[bool, str]:
        """当前是否允许主动分享；返回 ``(允许, 时段说明)``。"""
        auto = self.config.auto
        window = share_window_text(
            start_hour=auto.share_start_hour,
            end_hour=auto.share_end_hour,
            tz_name=auto.share_timezone,
        )
        if not auto.share_quiet_hours_enabled:
            return True, f"全天可分享（已关闭免打扰，分享时段配置 {window}）"
        allowed = is_share_window(
            start_hour=auto.share_start_hour,
            end_hour=auto.share_end_hour,
            tz_name=auto.share_timezone,
        )
        return allowed, window

    # ---- 分享冷却（按目标会话） --------------------------------------

    def _share_state_path(self) -> Path:
        return self.data_dir / SHARE_STATE_FILENAME

    def share_cooldown_remaining(self, target: str) -> int:
        """距离上次向该目标分享还需等待的秒数（0=可分享）。"""
        try:
            minutes = self.config.auto.share_cooldown_minutes
        except Exception:  # noqa: BLE001 - 配置读取失败按不限制处理
            minutes = 0
        state = load_share_state(self._share_state_path())
        return share_cooldown_remaining_seconds(
            state.get(str(target or "").strip()), minutes=minutes
        )

    def record_share(self, target: str) -> None:
        """记录一次成功分享的目标时间（供按目标会话冷却）。"""
        key = str(target or "").strip()
        if not key:
            return
        state = load_share_state(self._share_state_path())
        save_share_state(self._share_state_path(), record_share(state, key))

    def shareable_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        """可主动分享的会话：全部群聊 + 最近有交流的私聊用户。"""
        auto = self.config.auto
        result: list[dict[str, Any]] = []
        for item in self.chat_memory.session_summaries(limit=limit):
            umo = str(item.get("umo") or "").strip()
            kind = classify_session_umo(umo)
            if kind == "other":
                kind = "group" if item.get("is_group") else "user"
            if kind == "user":
                if not is_recent_interaction(
                    item.get("last_ts"), hours=auto.share_user_recent_hours
                ):
                    continue
            elif kind != "group":
                continue
            entry = dict(item)
            entry["kind"] = kind
            result.append(entry)
        return result

    def validate_share_target(self, target: str) -> str:
        """校验分享目标：必须是聊天记忆里的群/人；私聊需最近有交流。"""
        text = str(target or "").strip()
        if not text:
            return "未指定分享目标"
        kind = classify_session_umo(text)
        if kind == "other":
            return f"分享目标不是可识别的群聊/私聊会话：{text}"
        if not self.chat_memory.is_known_session(text):
            return (
                f"分享目标 {text} 没有最近的聊天记录；请先用 phone_recent_chat "
                "查看可分享的群/人再选"
            )
        if kind == "user":
            auto = self.config.auto
            last = self.chat_memory.last_ts(text)
            if not is_recent_interaction(
                last, hours=auto.share_user_recent_hours
            ):
                return (
                    f"私聊目标 {text} 最近 {auto.share_user_recent_hours} 小时内"
                    "没有交流，只分享给最近有交流的用户"
                )
        return ""

    def share_policy_hint(self) -> str:
        """给自刷/随机/持续刷提示词用的分享策略说明（目标与冷却由配置约束）。"""
        auto = self.config.auto
        allowed, window = self.share_allowed_now()
        if auto.share_quiet_hours_enabled and not allowed:
            return (
                f"\n分享：现在是免打扰时段（分享时段 {window}），本轮不要主动分享，"
                "保持静默收尾即可。"
            )
        text = share_rule_text(auto.share_cooldown_minutes)
        return (
            "\n分享：" + text
            + "分享目标由你根据最近聊天内容判断：先 phone_recent_chat 看最近群聊/私聊在聊什么、"
            "喜欢什么，再决定分享到哪个群或哪个人，不要使用固定目标；私聊只分享给最近有交流"
            f"的用户（{auto.share_user_recent_hours} 小时内聊过），群聊按最近聊天内容选；"
            f"当前处于分享时段 {window}。"
        )

    def resolve_app_target(self, app: str) -> tuple[str, str]:
        """把 App 名称解析成 ``(包名, 展示名)``。

        解析顺序：内置画像别名/中文标签 → 常见 App 中文名/包名映射 → 直接包名；
        无法识别时抛带 ``phone_list_apps`` 引导的异常。
        """
        text = str(app or "").strip()
        if not text:
            raise AppNotInstalledError(
                "App 名称不能为空；可先调用 phone_list_apps 查看已安装 App"
            )
        profile = resolve_app(text)
        if profile is not None and profile.package:
            return profile.package, profile.label or profile.package
        package = resolve_package(text)
        if package:
            label = KNOWN_APP_LABELS.get(package) or app_label_for_package(package)
            return package, label or package
        raise AppNotInstalledError(UNKNOWN_APP_HINT_TEMPLATE.format(name=text))

    async def open_app(self, app: str) -> str:
        """按中文名/别名/包名打开任意已安装 App。"""
        self.guard_device("open_app")
        self.ensure_configured()
        package, label = self.resolve_app_target(app)
        if not await self.actions.is_installed(package):
            raise AppNotInstalledError(
                f"{label}（{package}）未安装；可先调用 phone_list_apps 查看已安装 App"
            )
        await self.actions.open_app(package)
        self._invalidate_ui_cache()
        return f"已打开 {label}（{package}）"

    # ---- 浏览与记忆 ---------------------------------------------------

    def _browse_session_for_config(self, config: PhoneConfig) -> BrowseSession:
        settings = config.browse
        if self._browse_session is None or self._browse_session.settings != settings:
            self._browse_session = BrowseSession(
                self.actions,
                settings=settings,
                log_fn=self._log,
                screenshot_dir=self.screenshots_dir,
            )
        return self._browse_session

    async def browse(
        self,
        app: str,
        *,
        items: int = 0,
        collect_screenshots: bool = False,
        remember: bool = True,
    ) -> BrowseResult:
        """浏览任意 App 的信息流；``remember`` 时写入浏览日志。"""
        self.guard_device("browse")
        self.ensure_configured()
        package, label = self.resolve_app_target(app)
        config = self.config
        request = BrowseRequest(
            app=package,
            items=items or config.browse.default_items,
            collect_screenshots=collect_screenshots,
            remember=remember,
        )
        result = await self._browse_session_for_config(config).run(request)
        self._invalidate_ui_cache()
        if label and result.app == package:
            result.app = label
        if remember and config.browse.auto_remember and config.memory.local_enabled:
            try:
                round_info = current_round()
                await self.diary.add_browse_async(
                    result,
                    round_id=round_info[0] if round_info else "",
                    round_kind=round_info[1] if round_info else "",
                    max_entries=config.browse.diary_max_entries,
                    keep_rounds=config.browse.diary_protect_rounds,
                )
                result.diary_recorded = True
                self.request_semantic_refresh()
            except Exception as exc:  # noqa: BLE001 - 记忆写入失败不影响浏览结果
                logger.warning("写入浏览日志失败：%s", exc)
        # 新鲜感/厌倦感：浏览采样到的内容按主题累计疲劳度；占用不阻塞浏览结果。
        try:
            labels = self.record_novelty_items(label or result.app, result.items)
        except Exception as exc:  # noqa: BLE001 - 状态记录失败不影响浏览
            logger.warning("记录浏览内容新鲜感失败：%s", exc)
            labels = []
        if labels:
            result.novelty_labels = labels
        await self._auto_clean_after_browse(config.security.auto_clean_after_browse)
        return result

    async def cleanup_diary(self) -> dict[str, Any]:
        """按配置清理浏览日志（启动/配置变更时调用，不访问设备）。"""
        settings = self.config.browse
        return await self.diary.apply_limits_async(
            max_entries=settings.diary_max_entries,
            keep_rounds=settings.diary_protect_rounds,
        )

    def record_novelty_items(self, app: str, items: Any) -> list[str]:
        """记录一组内容的曝光，返回本次达到「已看腻」阈值的内容主题标签。"""
        labels: list[str] = []
        for item in items or ():
            title = str(
                getattr(item, "title", "")
                or (getattr(item, "texts", None) or [""])[0]
            ).strip()
            if not title:
                continue
            match = self.record_novelty(app, title, weight=1.0)
            if match is None or not getattr(match, "bored", False):
                continue
            entry = getattr(match, "entry", None)
            label_text = str(getattr(entry, "label", "") or title).strip()
            if label_text and label_text not in labels:
                labels.append(label_text)
        return labels

    async def _auto_clean_after_browse(self, enabled: bool) -> None:
        """浏览结束后静默清理一次后台（失败只记日志，不影响返回文案）。"""
        if not enabled:
            return
        try:
            report = await self.clean_background()
            if report["errors"]:
                logger.warning("浏览结束后清理异常：%s", "；".join(report["errors"]))
            self._log("浏览结束后已自动清理后台进程")
        except PhoneControlError as exc:
            logger.warning("浏览结束后自动清理后台失败：%s", exc)

    async def clean_background(self, *, package: str = "", preserve_foreground: bool = True, checkpoint=None) -> dict[str, Any]:
        """所有清理入口共享当前白名单；浏览过程中保留当前前台。"""
        self.guard_device("clean_background")
        foreground = ""
        if preserve_foreground:
            foreground, _activity = await self.actions.foreground()
            if not foreground:
                raise ActionFailedError("无法确认当前前台应用，未执行后台清理")
        return await clean_background(
            self.adb, whitelist=self.config.security.background_keep_packages,
            foreground=foreground, package=package, checkpoint=checkpoint or self.guard_device,
        )

    async def read_clipboard(self) -> str:
        """通过 scrcpy 当前回应读取设备剪贴板，不使用缓存。"""
        self.guard_device("clipboard")
        self.ensure_configured()
        manager = getattr(self, "backends", None)
        if manager is None:
            raise ActionFailedError("剪贴板控制通道尚未配置")
        try:
            text = await manager.read_clipboard()
        except Exception as exc:
            raise ActionFailedError(f"无法读取设备剪贴板：{exc}") from exc
        self.guard_device("clipboard_done")
        return text

    async def capture_post_top(self):
        from .share_screenshot import capture_post_top
        return await capture_post_top(self)

    async def recall(self, query: str = "", *, limit: int = 5) -> str:
        """检索浏览日志并格式化成中文文本。"""
        entries = await self.diary.search_async(query, limit=limit)
        if not entries:
            if query:
                return f"浏览日志里没有找到与 {query!r} 相关的记录"
            return "浏览日志还是空的，先让我浏览一些内容吧"
        title = f"浏览日志（{'关键词 ' + query if query else '最近'}，{len(entries)} 条）"
        lines = [title]
        for entry in entries:
            lines.append(
                f"[{_format_time(float(entry.get('ts') or 0))}] {entry.get('app') or '未知'}"
            )
            for item in (entry.get("items") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                head = str(item.get("title") or item.get("text") or "").strip()
                author = str(item.get("author") or "").strip()
                lines.append("  - " + (head or "（无文本）") + (f" —— {author}" if author else ""))
            note = str(entry.get("note") or "").strip()
            if note:
                lines.append(f"  备注：{note}")
        return "\n".join(lines)

    def _wait_limits(self) -> tuple[float, float]:
        """读取等待配置：返回 ``(默认秒数, 单次上限秒数)``。"""
        perception = getattr(self.config, "perception", None)
        try:
            default = float(getattr(perception, "wait_default_seconds", 2.0))
        except (TypeError, ValueError):
            default = 2.0
        try:
            maximum = float(getattr(perception, "wait_max_seconds", 60))
        except (TypeError, ValueError):
            maximum = 60.0
        default = min(60.0, max(0.2, default))
        maximum = min(600.0, max(1.0, maximum))
        return default, max(default, maximum)

    async def wait(self, *, seconds: float = 0, for_text: str = "") -> str:
        """等待若干秒（默认/上限取配置），或等待指定文本出现。"""
        self.guard_device("wait")
        self.ensure_configured()
        default, maximum = self._wait_limits()
        try:
            requested = float(seconds or 0)
        except (TypeError, ValueError):
            requested = 0.0
        total = requested if requested > 0 else default
        total = min(maximum, max(0.2, total))
        if for_text:
            node = await self.actions.wait_for_text(for_text, timeout=total)
            if node is None:
                return f"等待 {total:.1f}s 内没有出现文本 {for_text!r}"
            return f"文本 {for_text!r} 已出现：{node.label}"
        await asyncio.sleep(total)
        return f"已等待 {total:.1f}s"


    async def _control_backend_or_none(self) -> Any:
        """取控制后端；没有管理器/启动失败时返回 None 由调用方走 ADB。"""
        manager = getattr(self, "backends", None)
        if manager is None:
            return None
        try:
            return await manager.control()
        except Exception as exc:  # noqa: BLE001 - 控制后端失败回退 ADB
            self._log(f"控制后端不可用，回退 ADB：{exc}")
            return None

    async def tap(self, x: int, y: int) -> None:
        self.guard_device("tap")
        x, y = validate_touch_point(x, y, *(await self.actions.screen_size()))
        backend = await self._control_backend_or_none()
        try:
            if backend is None:
                await self.actions.tap(x, y)
            else:
                await backend.tap(x, y)
        finally:
            self._invalidate_ui_cache()

    async def long_press(
        self, x: int, y: int, *, duration_ms: int = 800
    ) -> None:
        self.guard_device("long_press")
        x, y = validate_touch_point(x, y, *(await self.actions.screen_size()))
        backend = await self._control_backend_or_none()
        try:
            if backend is None:
                await self.actions.long_press(x, y, duration_ms=duration_ms)
            else:
                await backend.long_press(x, y, duration_ms=duration_ms)
        finally:
            self._invalidate_ui_cache()

    async def swipe_direction(
        self, direction: str, *, duration_ms: int = 800
    ) -> None:
        self.guard_device("swipe")
        backend = await self._control_backend_or_none()
        try:
            if backend is None:
                await self.actions.swipe_direction(direction, duration_ms=duration_ms)
            else:
                await backend.swipe(direction, duration_ms=duration_ms)
        finally:
            self._invalidate_ui_cache()

    async def input_text(self, text: str, *, press_enter: bool = False) -> None:
        self.guard_device("input_text")
        backend = await self._control_backend_or_none()
        try:
            if backend is None:
                await self.actions.input_text(text, press_enter=press_enter)
            else:
                await backend.input_text(text, press_enter=press_enter)
        finally:
            self._invalidate_ui_cache()

    async def press_key(self, key: str) -> None:
        self.guard_device("press_key")
        backend = await self._control_backend_or_none()
        try:
            if backend is None:
                await self.actions.press_key(key)
            else:
                await backend.press_key(key)
        finally:
            self._invalidate_ui_cache()
