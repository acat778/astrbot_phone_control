"""插件自主任务 mixin：定时自主浏览、随机玩手机、持续刷手机循环与设备/用户任务钩子。"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_data_path,
    get_astrbot_plugin_path,
)

try:  # AstrBot 内置命令的写法：一个 GreedyStr 参数吃下命令后的全部文本
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - AstrBot 版本或单测环境没有该类型
    GreedyStr = str  # type: ignore[assignment,misc]

from ..actions import DeviceActions
from ..adb import AdbClient
from ..app_memory import AppMemoryStore
from ..apps import (
    KNOWN_APP_LABELS,
    UNKNOWN_APP_HINT_TEMPLATE,
    app_label_for_package,
    format_preferred_apps,
    match_preferred_app,
    parse_package_list,
    resolve_app,
    resolve_package,
)
from ..backends import BackendManager
from ..browse import BrowseSession
from ..chat_memory import ChatMemory
from ..config import (
    UNCONFIGURED_MESSAGE,
    PhoneConfig,
)
from ..config_apply import ACTION_START, ACTION_STOP, build_apply_plan
from ..diary import PhoneDiary
from ..interactions import (
    KIND_COMMENT,
    KIND_PASSIVE_REPLY,
    KIND_POST,
    InteractionStore,
)
from ..learning import LearningError, LearningStore
from ..novelty import NoveltyStore
from ..live_screen import LiveScreenWatcher
from ..memory_sync import (
    COMPANION_PLUGIN_NAME,
    COMPANION_SOURCE_PLUGIN,
    LEARNING_MEMORY_TYPE,
    LIVINGMEMORY_PLUGIN_NAME,
    build_bot_action_payload,
    wants_companion,
    wants_livingmemory,
)
from ..errors import (
    ActionFailedError,
    AdbNotConfiguredError,
    AppNotInstalledError,
    PhoneControlError,
)
from ..models import BrowseRequest, BrowseResult, DeviceInfo, ScreenSnapshot, UiNode
from ..continuous import (
    build_continuous_note,
    next_run_delay,
    normalize_continuous_apps,
    normalize_gap_seconds,
)
from ..play_window import activity_allowed, WINDOW_ORIGINS
from ..random_play import (
    build_play_note,
    build_random_play_note,
    next_random_delay,
    should_play,
)
from .. import verification as verification_mod
from ..device_gate import (
    DeviceExecutionGate,
    ExecutionCancelled,
    GateError,
)
from ..screenshot_cleanup import cleanup_directory
from ..page_content import (
    PAGE_ACTION_WINDOW_MAX_SECONDS,
    PAGE_COMPACT_BODY_CHARS,
    PAGE_COMPACT_MAX_TEXT_CHARS,
    PAGE_FLOW_LIMIT,
    PAGE_MAX_TEXT_CHARS,
    PageTrafficSnapshot,
    build_page_traffic,
)
from ..traffic import extract_share_links, infer_flow_hints, mask_url, render_traffic
from ..traffic_capture import TrafficCapture
from ..share_policy import (
    classify_session_umo,
    is_recent_interaction,
    is_share_window,
    share_window_text,
)
from ..scheduler import (
    CONTINUOUS_JOB_ORIGIN,
    JOB_ORIGIN as AUTO_BROWSE_JOB_ORIGIN,
    AutoBrowseError,
    AutoBrowseScheduler,
    BackgroundTaskError,
    create_background_task,
    continuous_job_running,
    create_continuous_task,
    delete_plugin_jobs,
    list_background_tasks,
    list_plugin_jobs,
    validate_background_instruction,
)
from ..token_quota import (
    PlayBudget,
    PlayBudgetRegistry,
    TokenQuota,
    estimate_fallback_usage,
    estimate_messages_tokens,
    estimate_tokens,
    usage_breakdown,
    usage_is_empty,
)
from .. import usage_audit
from ..agent_usage_patch import (
    USAGE_EXTRA as AGENT_USAGE_EXTRA,
    install_agent_usage_capture,
    uninstall_agent_usage_capture,
)
from astrbot.core.platform.message_type import MessageType
from ..task_priority import (
    UserTaskPriority,
    should_resume_continuous,
    should_yield_to_user,
)
from ..tools import build_phone_tools, check_admin_permission
from ..media import (
    MediaAudioCapture,
    build_vision_prompt,
    clamp_seconds as clamp_media_seconds,
    clean_transcript as clean_media_transcript,
    plan_frame_times,
    safe_unlink as safe_unlink_media,
)
from ..telemetry import MetricsStore
from ..ui import dump_ui, render_ui
from .shared import (
    _AutonomousDeviceEvent,
    _HELP_TEXT,
    _PHONE_CONTROL_TOOLS,
    _build_guidance,
    _companion_plugin_available,
    _read_plugin_version,
    PLUGIN_NAME,
    PHONE_KEYWORDS,
    plugin_root_dir,
    _GUIDANCE,
    _format_time,
    _llm_response_text,
    _INBOX_KEYWORDS,
    _INBOX_EXCLUDE_WORDS,
)


# 任务收尾清理的设备作用域 purpose：只有该作用域内收尾才逐步复查门禁。
_TASK_CLEANUP_PURPOSE = "task_cleanup"


class TasksMixin:
    def _prepare_phone_runner(self, runner: Any) -> None:
        """在Agent执行前隔离当前请求工具，覆盖原生cron入口。"""
        from ..subagent import route_request

        event = getattr(getattr(runner.run_context, "context", None), "event", None)
        req = getattr(runner, "req", None)
        if event is not None and req is not None and check_admin_permission(event, self.service.config) is None:
            route_request(self.service, event, req)

    def _background_task_submitted(self, event: Any) -> None:
        """委托成功后释放发起会话的手机占用，主任务继续执行。"""
        event.set_extra("phone_background_delegated", True)
        self.device_gate.release_event(event)
        self._exit_user_priority(event)

    async def _apply_auto_config_when_ready(self) -> None:
        """等待 cron 管理器就绪后按配置创建任务（最多重试约 10 秒）。"""
        for attempt in range(20):
            try:
                await self.scheduler.apply_config()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 启动期竞态，继续重试
                if attempt >= 19:
                    logger.warning("同步自主浏览任务失败：%s", exc)
                    return
                await asyncio.sleep(0.5)

    async def _auto_browse(self, app: str, umo: str) -> str:
        """降级模式下的自主浏览回调：直接浏览并返回摘要文本。"""
        event = _AutonomousDeviceEvent(umo)
        try:
            with self.device_gate.scope(
                event, purpose="auto_browse", write=True
            ):
                result = await self.service.browse(
                    app,
                    items=self.service.config.auto.max_items,
                    collect_screenshots=False,
                    remember=True,
                )
        except GateError as exc:
            return f"error: {exc.message}"
        finally:
            self.device_gate.release_event(event)
        return f"【自主浏览】{result.summary_text()}"

    def _device_authorize(self, event: Any) -> str | None:
        """设备访问鉴权（主人/管理员/白名单；cron 任务放行）。"""
        try:
            return check_admin_permission(event, self.service.config)
        except Exception:  # noqa: BLE001 - 配置异常时拒绝而不是放行
            return "error: 无法校验手机操作权限，请检查插件配置"

    def _device_blocking_reason(self, event: Any, purpose: str, write: bool) -> str:
        """持久化故障、验证暂停/过期等阻断条件统一入口（异常时 fail-closed）。"""
        try:
            if getattr(self, "_terminated", False) or getattr(self, "_stopping_phone", False):
                return "手机控制正在停止，请稍后重新发起操作"
            health = self._verification.health_reason()
            if health:
                return health
            # 用户任务优先：自主任务让路后不得重新取得设备租约（否则会反向挡住用户任务）。
            if (
                self._device_is_autonomous(event)
                and self.service.user_priority.active
            ):
                return (
                    "用户任务优先：AI 自刷/随机/后台玩手机已暂停，先让用户任务使用手机；"
                    "请立即收尾停下，用户任务结束后会自动恢复自刷。"
                )
            purpose_text = str(purpose or "")
            if purpose_text.split(":", 1)[0] == "command":
                sub = purpose_text.split(":", 1)[1] if ":" in purpose_text else ""
                return self._verification.guard_command(sub)
            return self._verification.guard_tool(purpose_text, write=bool(write))
        except Exception:  # noqa: BLE001 - 门禁自身异常时拒绝执行
            logger.exception("手机设备门禁判定失败，已阻止本次操作")
            return "error: 手机设备门禁判定失败，已阻止本次操作；请查看 AstrBot 日志"

    def _device_deadline_seconds(self, event: Any, purpose: str) -> float:
        """验证类工具的执行期限（会话剩余与单次尝试剩余的较小值）。"""
        if str(purpose or "") in self._verification._VERIFY_TOOLS:
            return self._verification.execution_deadline_seconds()
        return float("inf")

    def _device_is_autonomous(self, event: Any) -> bool:
        from ..subagent import is_user_subagent

        if is_user_subagent(event):
            return False
        try:
            return bool(self.service.task_origin(event))
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            return False

    async def _guard_device_action(self, op: str) -> None:
        """DeviceActions 动作前的批次内复查。"""
        self.device_gate.checkpoint(str(op))

    async def _cleanup_phone_home(self, *, guarded: bool = False) -> dict[str, Any]:
        """回桌面后按白名单清理第三方与最近任务，保留常驻应用。"""
        def check(op):
            if guarded:
                self.device_gate.checkpoint(op)

        report: dict[str, Any] = {"stopped": 0, "errors": []}
        try:
            check("cleanup_home")
            if guarded:
                await self.actions.press_key("home")
            else:
                await self.adb.shell("input keyevent 3")
            await asyncio.sleep(0.4)
            check("cleanup_apps")
            from ..background import clean_background
            report = await clean_background(
                self.adb, whitelist=self.service.config.security.background_keep_packages,
                checkpoint=check if guarded else None,
            )
            check("cleanup_done")
        except GateError:
            raise
        except Exception as exc:
            logger.warning("清理手机失败：%s", exc)
            report["errors"].append(str(exc))
        return report

    async def _abort_running_cron_event(self, job_id: str) -> int:
        """中止指定 cron job 正在运行的 Agent（返回中止数量）。

        AstrBot 的 cron 主动任务不走 pipeline 的 active_event_registry，无法用
        ``request_agent_stop_all`` 停止；这里取消 on_agent_begin 记录的正在运行
        task（同时保留 registry 兜底，兼容后续版本）。
        """
        if not job_id:
            return 0
        stopped = 0
        cancelled = []
        events = []
        for umo, (task, running_job_id, _origin) in list(
            self._active_agent_tasks.items()
        ):
            if running_job_id and running_job_id != job_id:
                continue
            if not running_job_id:
                continue
            if task.done():
                self._active_agent_tasks.pop(umo, None)
                continue
            self.service.background_tasks.update(running_job_id, "cancelled")
            task.cancel()
            cancelled.append(task)
            event = self._background_agent_events.get(umo)
            if event is not None:
                events.append(event)
            self._active_agent_tasks.pop(umo, None)
            stopped += 1
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
            for event in events:
                await self.on_task_agent_done(event)
        try:
            from astrbot.core.utils.active_event_registry import (
                active_event_registry,
            )
        except Exception:  # noqa: BLE001 - 框架无该模块时忽略
            return stopped
        umo = f"cron:OtherMessage:{job_id}"
        try:
            stopped += int(active_event_registry.request_agent_stop_all(umo) or 0)
        except Exception as exc:  # noqa: BLE001 - 中止失败只记录
            logger.warning("中止 cron 任务 %s 失败：%s", job_id, exc)
        return stopped

    def _looks_like_user_phone_request(self, event: AstrMessageEvent) -> bool:
        """判断用户消息是否在请求手机相关任务（用于抢占自刷）。"""
        try:
            if event.get_extra("phone_background_delegated"):
                return False
            if not self._config().auto.user_priority:
                return False
            if self.service.task_origin(event):
                return False
            if str(event.get_platform_name() or "") == "cron":
                return False
            text = str(event.get_message_str() or "").strip().lower()
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            return False
        if not text:
            return False
        if text.startswith("/phone"):
            return True
        return any(str(keyword).lower() in text for keyword in PHONE_KEYWORDS)

    async def _enter_user_priority(self, event: AstrMessageEvent) -> None:
        """登记用户手机任务；首次登记时中止 AI 自刷/后台任务让路。"""
        if not self._config().auto.user_priority:
            return
        session = str(getattr(event, "unified_msg_origin", "") or "")
        if not self.service.user_priority.begin(session):
            return
        logger.info("用户任务优先：已登记用户手机任务，AI 自刷/后台任务让路")
        try:
            # 先撤销自主任务设备租约并递增取消代次，在途操作下一次检查即中止。
            self.device_gate.preempt("user_priority")
            preempted = await self._preempt_autonomous_tasks()
        except Exception as exc:  # noqa: BLE001 - 让路失败不影响用户任务
            logger.warning("用户任务优先：通知自主任务让路失败：%s", exc)
            return
        if preempted:
            logger.info("用户任务优先：已中止 %s 个自主任务运行", preempted)

    async def _preempt_autonomous_tasks(self) -> int:
        """中止正在运行的自主任务（持续刷轮次会被删除、用户任务结束后自动重排）。"""
        cron_manager = getattr(self.context, "cron_manager", None)
        if cron_manager is None:
            return 0
        continuous_job = self._continuous_active_job_id
        try:
            jobs = await list_plugin_jobs(cron_manager)
        except BackgroundTaskError:
            jobs = []
        aborted = 0
        for job in jobs:
            job_id = str(job.get("job_id") or "")
            if job_id:
                aborted += await self._abort_running_cron_event(job_id)
        if continuous_job:
            with suppress(Exception):
                await delete_plugin_jobs(cron_manager, job_id=continuous_job)
            self._continuous_active_job_id = ""
            self._continuous_active_since = 0.0
            if self._continuous_enabled:
                logger.info(
                    "用户任务优先：已中止在途持续刷轮次，用户任务结束后自动重排下一轮"
                )
        return aborted

    def _exit_user_priority(self, event: AstrMessageEvent) -> None:
        """结束用户手机任务；最后一个用户任务结束时自动恢复持续刷。"""
        if not self.service.user_priority.sessions:
            return
        session = str(getattr(event, "unified_msg_origin", "") or "")
        if not self.service.user_priority.end(session):
            return
        if self._verification.paused:
            logger.info("用户任务结束：验证等待人工协助，不恢复持续刷")
            return
        if not should_resume_continuous(
            continuous_enabled=self._continuous_enabled,
            user_active_after_end=self.service.user_priority.active,
        ):
            return
        self._continuous_last_finished = 0.0
        if self._continuous_enabled:
            try:
                self._start_continuous_loop()
            except Exception as exc:  # noqa: BLE001 - 恢复失败只记录
                logger.warning("用户任务结束：恢复持续刷失败：%s", exc)
        logger.info("用户任务结束：已自动恢复持续刷（下一轮立即排程）")

    def _is_autonomous_task(self, event: AstrMessageEvent) -> bool:
        """是否本插件创建的自主任务（play/随机/持续刷/后台/自主浏览）。"""
        if self.service.is_task_session(event):
            return True
        payload = None
        try:
            payload = event.get_extra("cron_payload")
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            payload = None
        if isinstance(payload, dict):
            return str(payload.get("origin") or "") == AUTO_BROWSE_JOB_ORIGIN
        return False

    def _looks_like_phone_request(self, event: AstrMessageEvent, req: Any) -> bool:
        """判断请求是否像手机相关（用于 scrcpy 预热，不记录额度）。"""
        if self._is_phone_request(event, req):
            return True
        if self.service.is_task_session(event):
            return True
        parts = [
            str(getattr(req, "system_prompt", "") or ""),
            str(getattr(req, "prompt", "") or ""),
        ]
        for part in getattr(req, "extra_user_content_parts", None) or []:
            text = part if isinstance(part, str) else getattr(part, "text", "")
            if text:
                parts.append(str(text))
        text = "\n".join(parts).lower()
        return any(keyword in text for keyword in PHONE_KEYWORDS)

    def _is_phone_request(self, event: AstrMessageEvent, req: Any) -> bool:
        """判断当前 LLM 请求是否与手机能力相关（只给这些请求计费）。"""
        if self.service.play_payload(event):
            return True
        try:
            if event.get_extra("phone_active"):
                return True
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            pass
        marker = "【手机操控能力"
        texts = [
            str(getattr(req, "system_prompt", "") or ""),
            str(getattr(req, "prompt", "") or ""),
        ]
        for part in getattr(req, "extra_user_content_parts", None) or []:
            text = part if isinstance(part, str) else getattr(part, "text", "")
            if text:
                texts.append(str(text))
        return any(marker in text for text in texts)

    async def _auto_cleanup_after_task(self, event: AstrMessageEvent) -> None:
        """玩完手机后回桌面并清理后台（确定性收尾，不依赖模型主动调用工具）。

        HOME、等待与逐包清理全部在本次收尾自己的设备作用域内执行，并持有
        可让路租约：其他任务持租约时直接跳过；新任务需要设备时接管该租约，旧收尾
        在下一次门禁检查处停止，不会在新任务已拿到设备后继续清后台。
        """
        try:
            config = self.service.config
        except Exception:  # noqa: BLE001 - 配置异常时跳过收尾
            return
        if not bool(config.security.auto_clean_after_task):
            return
        if not self.service.configured:
            return
        if self._verification.paused:
            logger.info("验证等待人工协助：跳过 HOME/清后台，保留验证页面")
            return
        if self.device_gate.busy:
            # 其他任务仍持有设备租约：HOME/清后台会打断它，交给最后结束的任务收尾。
            logger.info("任务结束：手机仍被其他任务占用，跳过 HOME/清后台")
            return
        acquired = False
        try:
            with self.device_gate.scope(
                event,
                purpose=_TASK_CLEANUP_PURPOSE,
                write=True,
                preemptible=True,
            ):
                acquired = True
                await self._cleanup_phone_home(guarded=True)
        except ExecutionCancelled as exc:
            logger.info(
                "任务结束：手机已被新任务接管，停止 HOME/清后台收尾（%s）",
                exc.message,
            )
        except GateError as exc:
            logger.info("任务结束：当前不能收尾清理：%s", exc.message)
        finally:
            if acquired:
                self.device_gate.release_event(event)

    async def _sync_activity_jobs(self) -> None:
        """删除已关闭模式或已超出时段的待执行轮次与定时排程。"""
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            return
        auto = self.service.config.auto
        for job in await list_plugin_jobs(manager):
            origin = job.get("origin", "")
            if origin not in {*WINDOW_ORIGINS, AUTO_BROWSE_JOB_ORIGIN}:
                continue
            if activity_allowed(auto, origin):
                continue
            job_id = job["job_id"]
            await self._abort_running_cron_event(job_id)
            await manager.delete_job(job_id)
            if job_id == self._continuous_active_job_id:
                self._continuous_active_job_id = ""
                self._continuous_active_since = 0.0

    async def _watch_activity_window(self, event, task) -> None:
        """活动结束或配置变更后取消对应 Agent，等待原有收尾释放资源。"""
        try:
            while not task.done():
                if not activity_allowed(self.service.config.auto, self.service.task_origin(event)):
                    logger.info("自主活动时段结束或模式已切换，中止任务：%s", self.service.task_origin(event))
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return
                await asyncio.sleep(1)
        finally:
            # 框架取消 run 时未必发送结束钩子，由事件标记保证收尾只执行一次。
            if task.done():
                await self.on_task_agent_done(event)

    def _track_activity_window(self, event, task) -> None:
        if self.service.task_origin(event) not in {*WINDOW_ORIGINS, AUTO_BROWSE_JOB_ORIGIN, "phone_background_task"}:
            return
        monitors = self._activity_window_tasks
        monitor = asyncio.create_task(self._watch_activity_window(event, task))
        monitors.add(monitor)
        monitor.add_done_callback(monitors.discard)

    def _start_random_play_loop(self) -> None:
        """启动单实例随机玩手机循环（配置 random_enabled 关闭时循环空转等待）。"""
        if self._random_task is not None and not self._random_task.done():
            return
        self._random_stop = asyncio.Event()
        self._random_task = asyncio.create_task(
            self._random_play_loop(),
            name=f"{PLUGIN_NAME}_random_play",
        )
        logger.info("随机玩手机：后台循环已启动")

    async def _sleep_or_stop(self, seconds: float) -> bool:
        """等待指定秒数；插件停用时返回 True。"""
        try:
            await asyncio.wait_for(
                self._random_stop.wait(), timeout=max(0.1, float(seconds))
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def _random_play_loop(self) -> None:
        """随机间隔 + 概率掷骰；命中后创建静默的主动任务。"""
        while not self._random_stop.is_set():
            if not self.service.configured:
                # 假开启：不排任何自主任务，等配置地址并重载插件后恢复。
                if await self._sleep_or_stop(60):
                    return
                continue
            if self.service.user_priority.active:
                # 用户任务优先：用户任务期间不排随机玩手机，结束后自动恢复。
                if await self._sleep_or_stop(30):
                    return
                continue
            if self._verification.paused:
                # 验证等待人工协助：不创建新的手机任务。
                if await self._sleep_or_stop(30):
                    return
                continue
            if self._verification.health_reason():
                # 验证状态无法持久化：自动循环停排，等写入恢复后自动继续。
                logger.warning("随机玩手机：验证状态持久化失败，暂停排程")
                if await self._sleep_or_stop(30):
                    return
                continue
            if self.device_gate.busy:
                if await self._sleep_or_stop(10):
                    return
                continue
            if self._continuous_enabled:
                # 持续刷手机运行时不叠加随机玩手机，避免同时抢设备。
                if await self._sleep_or_stop(60):
                    return
                continue
            auto = self.service.config.auto
            if not auto.random_enabled:
                if await self._sleep_or_stop(60):
                    return
                continue
            if not activity_allowed(auto, "phone_random_play"):
                if await self._sleep_or_stop(1):
                    return
                continue
            delay = next_random_delay(
                auto.random_interval_min, auto.random_interval_max
            )
            logger.info(
                "随机玩手机：下次计划在 %.1f 分钟后掷骰（概率 %.0f%%）",
                delay / 60.0,
                auto.random_probability * 100,
            )
            if await self._sleep_or_stop(delay):
                return
            auto = self.service.config.auto
            if not auto.random_enabled:
                continue
            if not activity_allowed(auto, "phone_random_play"):
                continue
            if not should_play(auto.random_probability):
                logger.info("随机玩手机：本次未命中概率，跳过")
                continue
            if self._verification.paused:
                logger.info("随机玩手机：验证等待人工协助，本次跳过")
                continue
            if self._user_active_recently(minutes=10):
                logger.info("随机玩手机：用户最近 10 分钟内有交互，本次让路跳过")
                continue
            quota_error = self.service.quota_error_text()
            if quota_error:
                logger.info("随机玩手机：token 额度已用完，本次跳过：%s", quota_error)
                continue
            await self._schedule_random_play(auto)

    def _user_active_recently(self, minutes: float = 10.0) -> bool:
        """最近 minutes 分钟内是否有用户交互（有则随机玩手机让路）。"""
        try:
            path = self.data_dir / "last_session.json"
            if not path.exists():
                return False
            return (time.time() - path.stat().st_mtime) < minutes * 60
        except OSError:
            return False

    async def _schedule_random_play(self, auto: Any) -> None:
        """创建 run_once 的 active_agent 任务（payload.session 为空 = 静默）。"""
        if not activity_allowed(self.service.config.auto, "phone_random_play"):
            return
        cron_manager = getattr(self.context, "cron_manager", None)
        if cron_manager is None:
            logger.warning("随机玩手机：cron 管理器不可用，本次跳过")
            return
        target_tokens = max(0, int(self.service.config.quota.play_task_tokens))
        security = self.service.config.security
        note = build_random_play_note(
            share_only_interesting=bool(auto.share_only_interesting),
            target_tokens=target_tokens,
            mood_available=_companion_plugin_available(),
            interactions_enabled=bool(
                security.allow_interactions or security.allow_comments or security.allow_passive_replies or security.allow_posts
            ),
            share_hint=self.service.share_policy_hint(),
            novelty_hint=self.service.novelty_prompt(),
            break_rule=self.service.config.flexibility.free_browse_active,
            flexibility=self.service.config.flexibility,
        )
        payload = {
            "session": "",
            "note": note,
            "origin": "phone_random_play",
            "play_task": True,
            "target_tokens": target_tokens,
        }
        from ..interactions import interaction_plan
        payload["note"] += "\n" + interaction_plan(security, self.service.config.flexibility)
        try:
            job = await cron_manager.add_active_job(
                name="phone-random-play",
                cron_expression=None,
                payload=payload,
                run_once=True,
                run_at=datetime.now(timezone.utc) + timedelta(seconds=2),
            )
        except Exception as exc:  # noqa: BLE001 - 调度失败只记录，下一轮继续
            logger.warning("随机玩手机：创建主动任务失败：%s", exc)
            return
        await self._apply_task_provider_preference(job)
        logger.info(
            "随机玩手机：已创建静默主动任务（job=%s，session 为空不投递到聊天）",
            getattr(job, "job_id", "?"),
        )

    def _start_continuous_loop(self) -> None:
        """启动单实例持续刷手机循环（幂等）。"""
        if self._verification.paused:
            logger.info("持续刷手机：验证等待人工协助，暂不启动循环")
            return
        if self._verification.health_reason():
            logger.warning("持续刷手机：验证状态持久化失败，暂不启动循环")
            return
        if self._continuous_task is not None and not self._continuous_task.done():
            return
        self._continuous_stop = asyncio.Event()
        self._continuous_task = asyncio.create_task(
            self._continuous_loop(),
            name=f"{PLUGIN_NAME}_continuous",
        )
        logger.info(
            "持续刷手机：后台循环已启动（间隔 %s 秒，每轮预算 %s）",
            normalize_gap_seconds(self.service.config.auto.continuous_gap_seconds),
            int(self.service.config.auto.continuous_target_tokens or 0)
            or int(self.service.config.quota.play_task_tokens),
        )

    def _continuous_status_text(self) -> str:
        """持续刷手机状态文本（/phone continuous status）。"""
        auto = self.service.config.auto
        gap = normalize_gap_seconds(auto.continuous_gap_seconds)
        target = int(auto.continuous_target_tokens or 0) or int(
            self.service.config.quota.play_task_tokens
        )
        raw_apps = normalize_continuous_apps(auto.continuous_apps)
        apps_text = "AI 自选"
        if raw_apps:
            try:
                installed = self.service.cached_installed_packages()
                matches = [match_preferred_app(name, installed) for name in raw_apps]
                apps_text = format_preferred_apps(matches) or "、".join(raw_apps)
            except Exception:  # noqa: BLE001 - 解析失败回退原文
                apps_text = "、".join(raw_apps)
        lines = [
            "持续刷手机：",
            f"- 状态：{'已开启' if self._continuous_enabled else '未开启'}"
            "（自主活动选择自动刷并启用时自动启动）",
            f"- 轮次间隔：{gap} 秒；每轮 token 预算：{target}",
            f"- 偏好 App：{apps_text}",
            f"- 本轮任务：{self._continuous_active_job_id[:8] if self._continuous_active_job_id else '空闲'}",
        ]
        if self._continuous_last_finished:
            ago = max(0, int(time.time() - self._continuous_last_finished))
            lines.append(f"- 上一轮结束：{ago} 秒前；已完成 {self._continuous_round_index} 轮")
        try:
            stats = self.service.learning.stats()
            lines.append(f"- 学习档案：{stats.get('entries', 0)} 条（/phone learn 查看）")
        except Exception:  # noqa: BLE001 - 学习统计失败不影响状态
            pass
        return "\n".join(lines)

    async def _recover_stale_continuous_job(self, cron_manager: Any) -> str:
        """清理重启后遗留的持续刷轮次任务，返回被清理的 job_id。

        持续刷每轮是 ``run_once`` 主动任务：进程重启时若上一轮被中断，
        AstrBot 会把该任务状态留在 ``running`` 且不会被自动清理。若新实例直接
        沿用这个任务 id，循环会一直等待一个实际不存在的轮次（表现为「启动后
        不自己玩手机」）。因此当本实例没有在途轮次、但 DB 里仍有
        ``phone_continuous`` 任务时，删掉遗留任务并立即重新排一轮。
        """
        if cron_manager is None:
            return ""
        try:
            existing = await continuous_job_running(cron_manager)
        except Exception:  # noqa: BLE001 - 查询失败按无遗留处理
            return ""
        if not existing:
            return ""
        logger.info(
            "持续刷手机：清理重启前遗留的轮次任务（%s），重新排下一轮",
            existing[:8],
        )
        with suppress(Exception):
            await delete_plugin_jobs(cron_manager, job_id=existing)
        self._continuous_active_job_id = ""
        self._continuous_active_since = 0.0
        return existing

    async def _continuous_loop(self) -> None:
        """持续刷循环：一轮 run_once 任务结束后按间隔创建下一轮。"""
        while not self._continuous_stop.is_set():
            try:
                if not self._continuous_enabled:
                    await asyncio.sleep(30)
                    continue
                if not self.service.configured:
                    await asyncio.sleep(60)
                    continue
                if self.service.user_priority.active:
                    # 用户任务优先：用户手机任务结束后由 on_agent_done 恢复排程。
                    await asyncio.sleep(10)
                    continue
                if self._verification.paused:
                    # 验证等待人工协助：不创建新的持续刷轮次。
                    await asyncio.sleep(10)
                    continue
                if self._verification.health_reason():
                    # 验证状态无法持久化：自动循环停排，等写入恢复后自动继续。
                    logger.warning("持续刷手机：验证状态持久化失败，暂停排程")
                    await asyncio.sleep(30)
                    continue
                if self.device_gate.busy:
                    # 其他任务仍在使用手机：等它结束再排下一轮。
                    await asyncio.sleep(10)
                    continue
                quota_error = self.service.quota_error_text()
                if quota_error:
                    logger.info("持续刷手机：token 额度不可用，暂停 5 分钟")
                    await asyncio.sleep(300)
                    continue
                cron_manager = getattr(self.context, "cron_manager", None)
                if cron_manager is None:
                    await asyncio.sleep(30)
                    continue
                if not self._continuous_active_job_id:
                    recovered = await self._recover_stale_continuous_job(cron_manager)
                    if recovered:
                        logger.info(
                            "持续刷手机：遗留轮次已清理，立即重新排下一轮"
                        )
                if self._continuous_active_job_id:
                    still_there = False
                    with suppress(Exception):
                        current_jobs = await list_plugin_jobs(cron_manager)
                        still_there = any(
                            str(item.get("job_id") or "")
                            == self._continuous_active_job_id
                            for item in current_jobs
                        )
                    if not still_there:
                        self._continuous_active_job_id = ""
                        if not self._continuous_last_finished:
                            self._continuous_last_finished = time.time()
                        await asyncio.sleep(1)
                        continue
                    stale = (
                        time.monotonic() - self._continuous_active_since > 1800
                    )
                    if stale:
                        logger.warning(
                            "持续刷手机：上一轮超过 30 分钟未结束，清理后重试"
                        )
                        with suppress(Exception):
                            await delete_plugin_jobs(
                                cron_manager,
                                job_id=self._continuous_active_job_id,
                            )
                        self._continuous_active_job_id = ""
                    else:
                        await asyncio.sleep(10)
                        continue
                auto = self.service.config.auto
                if not activity_allowed(auto, CONTINUOUS_JOB_ORIGIN):
                    await asyncio.sleep(1)
                    continue
                gap = normalize_gap_seconds(auto.continuous_gap_seconds)
                delay = next_run_delay(
                    gap,
                    self._continuous_last_finished or None,
                    time.time() if self._continuous_last_finished else None,
                )
                if delay > 0:
                    await asyncio.sleep(min(delay, 30))
                    continue
                await self._start_continuous_round(auto)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 循环失败只记录并退避
                logger.warning("持续刷手机循环异常：%s", exc)
                await asyncio.sleep(30)

    async def _start_continuous_round(self, auto: Any) -> None:
        """创建一轮持续刷手机静默任务。"""
        if not activity_allowed(self.service.config.auto, CONTINUOUS_JOB_ORIGIN):
            return
        cron_manager = getattr(self.context, "cron_manager", None)
        target = int(getattr(auto, "continuous_target_tokens", 0) or 0)
        if target <= 0:
            target = int(self.service.config.quota.play_task_tokens)
        raw_apps = normalize_continuous_apps(getattr(auto, "continuous_apps", ""))
        apps: list[str] = []
        if raw_apps:
            installed: list[str] | None = None
            try:
                installed = await self.service.installed_packages()
            except Exception as exc:  # noqa: BLE001 - 解析失败按原文写入提示
                logger.warning("持续刷手机：读取已安装 App 失败，偏好按原文处理：%s", exc)
            matches = [match_preferred_app(name, installed) for name in raw_apps]
            apps = [match.display for match in matches]
            missing = [match.label or match.raw for match in matches if match.installed is False]
            logger.info(
                "持续刷手机：偏好 App 解析：%s%s",
                format_preferred_apps(matches),
                f"（未安装跳过：{'、'.join(missing)}）" if missing else "",
            )
        recent_learning = ""
        try:
            recent_learning = self.service.learning.render_for_prompt(3)
        except Exception:  # noqa: BLE001 - 学习档案读取失败不影响任务
            recent_learning = ""
        try:
            note = build_continuous_note(
                target_tokens=target,
                apps=apps,
                learn_tool_available=True,
                recent_learning=recent_learning,
                share_hint=self.service.share_policy_hint(),
                novelty_hint=self.service.novelty_prompt(),
                break_rule=self.service.config.flexibility.free_browse_active,
                flexibility=self.service.config.flexibility,
            )
        except TypeError:
            note = build_continuous_note(
                target_tokens=target,
                apps=apps,
                learn_tool_available=True,
            )
        if self._continuous_missing_learn and not self.service.config.flexibility.free_browse_active:
            note += (
                "\n注意：上一轮你没有用 phone_learn 记录学到的东西。本轮请认真理解内容，"
                "结束前至少调用一次 phone_learn 记录 1 条真正理解到的内容，别刷完就忘。"
            )
        from ..interactions import interaction_plan
        note += "\n" + interaction_plan(self.service.config.security, self.service.config.flexibility)
        try:
            job = await create_continuous_task(
                cron_manager,
                note=note,
                target_tokens=target,
                apps=apps,
            )
        except BackgroundTaskError as exc:
            logger.warning("持续刷手机：%s", exc)
            await asyncio.sleep(30)
            return
        await self._apply_task_provider_preference(job)
        self._continuous_round_index += 1
        self._continuous_active_job_id = str(getattr(job, "job_id", "") or "")
        self._continuous_active_since = time.monotonic()
        logger.info(
            "持续刷手机：已创建第 %s 轮静默任务（job=%s，预算 %s tokens）",
            self._continuous_round_index,
            self._continuous_active_job_id[:8] or "?",
            target,
        )

    def _mark_continuous_round_finished(self, event: AstrMessageEvent) -> None:
        """持续刷的一轮结束：记录时间、清空活动 job，便于循环排下一轮。"""
        try:
            payload = event.get_extra("cron_payload")
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            payload = None
        if not isinstance(payload, dict):
            return
        if str(payload.get("origin") or "") != CONTINUOUS_JOB_ORIGIN:
            return
        self._continuous_active_job_id = ""
        self._continuous_last_finished = time.time()
        learned = bool(event.get_extra("phone_learned"))
        self._continuous_missing_learn = not learned
        self._device_log(
            "持续刷手机：本轮结束"
            + ("（已记录学习内容）" if learned else "（本轮未记录 phone_learn，下一轮会提醒补记）")
        )

    async def _restart_random_play_loop(self) -> None:
        """按新配置重启随机玩手机后台循环（重新计算下一次计划）。"""
        task = getattr(self, "_random_task", None)
        self._random_task = None
        try:
            self._random_stop.set()
        except Exception:  # noqa: BLE001 - 事件异常不影响重启
            pass
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        try:
            self._start_random_play_loop()
            logger.info("配置热应用：随机玩手机已按新配置重新排程")
        except Exception as exc:  # noqa: BLE001 - 启动失败只记录
            logger.warning("配置热应用：重启随机玩手机失败：%s", exc)
