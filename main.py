"""astrbot_phone_control 插件入口。

通过远程 ADB 控制安卓模拟器，让主 Agent 以自身人格浏览抖音 / B 站 / 小黑盒，
采集信息流、截图取证、写入浏览日志，并支持聊天指令与定时自主浏览。

模块分工：

* 本模块只保留 AstrBot 入口类 ``PhoneControlPlugin`` 与 **命令/钩子处理器**；
  AstrBot 按函数所在模块绑定插件处理器，装饰器方法必须留在入口模块；
* ``core/plugin/*`` 是插件运行逻辑 mixin（装配、生命周期、命令实现、任务循环、
  设备钩子、实时屏幕/后端、抓包、监控面板、记忆同步、token 记账）；
* ``core/service.py`` 是对工具/命令暴露的能力门面（状态、UI 树、截图、浏览、日记等）；
* ``core/verification_controller.py`` 是验证会话/人工接管控制器；
* ``core/tools/`` 是 32 个 LLM 工具的实现包；
* ``core/adb.py``、``core/ui.py``、``core/actions.py`` 是设备层（ADB/UI/动作）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart
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

from .core.autonomy_config import migrate_autonomy_file

migrate_autonomy_file(Path(get_astrbot_config_path()) / (Path(__file__).parent.name + "_config.json"))

from .core.network_config import migrate_network_file
from .core.model_config import migrate_model_file
from .core.interaction_config import migrate_interaction_file
from .core.flexibility import migrate_flexibility_file

migrate_flexibility_file(Path(get_astrbot_config_path()) / (Path(__file__).parent.name + "_config.json"))

migrate_interaction_file(Path(get_astrbot_config_path()) / (Path(__file__).parent.name + "_config.json"))

if migrate_model_file(Path(get_astrbot_config_path()) / (Path(__file__).parent.name + "_config.json")):
    logger.info("模型选择配置已归入任务与媒体模型")

# 框架导入入口后按 schema 整理配置，先保存统一组网字段与备份。
if migrate_network_file(Path(get_astrbot_config_path()) / (Path(__file__).parent.name + "_config.json")):
    logger.info("组网配置已迁移到统一配置卡片")

from .core.actions import DeviceActions
from .core.adb import AdbClient
from .core.app_memory import AppMemoryStore
from .core.apps import (
    KNOWN_APP_LABELS,
    UNKNOWN_APP_HINT_TEMPLATE,
    app_label_for_package,
    format_preferred_apps,
    match_preferred_app,
    parse_package_list,
    resolve_app,
    resolve_package,
)
from .core.backends import BackendManager
from .core.browse import BrowseSession
from .core.chat_memory import ChatMemory
from .core.config import (
    UNCONFIGURED_MESSAGE,
    PhoneConfig,
)
from .core.config_apply import ACTION_START, ACTION_STOP, build_apply_plan
from .core.diary import PhoneDiary
from .core.interactions import (
    KIND_COMMENT,
    KIND_PASSIVE_REPLY,
    KIND_POST,
    InteractionStore,
)
from .core.learning import LearningError, LearningStore
from .core.novelty import NoveltyStore
from .core.live_screen import LiveScreenWatcher
from .core.memory_sync import (
    COMPANION_PLUGIN_NAME,
    COMPANION_SOURCE_PLUGIN,
    LEARNING_MEMORY_TYPE,
    LIVINGMEMORY_PLUGIN_NAME,
    build_bot_action_payload,
    wants_companion,
    wants_livingmemory,
)
from .core.errors import (
    ActionFailedError,
    AdbNotConfiguredError,
    AppNotInstalledError,
    PhoneControlError,
)
from .core.models import BrowseRequest, BrowseResult, DeviceInfo, ScreenSnapshot, UiNode
from .core.continuous import (
    build_continuous_note,
    next_run_delay,
    normalize_continuous_apps,
    normalize_gap_seconds,
)
from .core.random_play import (
    build_play_note,
    build_random_play_note,
    next_random_delay,
    should_play,
)
from .core import verification as verification_mod
from .core.device_gate import (
    DeviceExecutionGate,
    ExecutionCancelled,
    GateError,
)
from .core.screenshot_cleanup import cleanup_directory
from .core.page_content import (
    PAGE_ACTION_WINDOW_MAX_SECONDS,
    PAGE_COMPACT_BODY_CHARS,
    PAGE_COMPACT_MAX_TEXT_CHARS,
    PAGE_FLOW_LIMIT,
    PAGE_MAX_TEXT_CHARS,
    PageTrafficSnapshot,
    build_page_traffic,
)
from .core.traffic import extract_share_links, infer_flow_hints, mask_url, render_traffic
from .core.traffic_capture import TrafficCapture
from .core.share_policy import (
    classify_session_umo,
    is_recent_interaction,
    is_share_window,
    share_window_text,
)
from .core.scheduler import (
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
from .core.token_quota import (
    PlayBudget,
    PlayBudgetRegistry,
    TokenQuota,
    estimate_fallback_usage,
    estimate_messages_tokens,
    estimate_tokens,
    usage_breakdown,
    usage_is_empty,
)
from .core import usage_audit
from .core.agent_usage_patch import (
    USAGE_EXTRA as AGENT_USAGE_EXTRA,
    install_agent_usage_capture,
    uninstall_agent_usage_capture,
)
from astrbot.core.platform.message_type import MessageType
from .core.task_round import (
    ORIGIN_AUTONOMOUS,
    ORIGIN_COMMAND,
    ORIGIN_USER,
    new_round_id,
    reset_round,
    set_round,
)
from .core.task_priority import (
    UserTaskPriority,
    should_resume_continuous,
    should_yield_to_user,
)
from .core.tools import build_phone_tools, check_admin_permission
from .core.media import (
    MediaAudioCapture,
    build_vision_prompt,
    clamp_seconds as clamp_media_seconds,
    clean_transcript as clean_media_transcript,
    plan_frame_times,
    safe_unlink as safe_unlink_media,
)
from .core.telemetry import MetricsStore
from .core.ui import dump_ui, render_ui

from .core.plugin.bootstrap import BootstrapMixin, LIVE_PLUGIN_INSTANCES
from .core.plugin.commands import CommandsMixin
from .core.plugin.tasks import TasksMixin
from .core.plugin.runtime import RuntimeMixin
from .core.plugin.observability import ObservabilityMixin
from .core.plugin.shared import (
    PLUGIN_NAME,
    _HELP_TEXT,
    _PHONE_CONTROL_TOOLS,
    _build_guidance,
)
from .core.service import PhoneService
from .core.semantic import RECALL_MARKER
from .core.verification_controller import PhoneVerification


def _resolve_data_dir() -> Path:
    """解析插件数据目录（数据卷持久化）。"""
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001 - 装载早期 star_map 可能尚未就绪
        return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


class PhoneControlPlugin(
    BootstrapMixin,
    CommandsMixin,
    TasksMixin,
    RuntimeMixin,
    ObservabilityMixin,
    Star,
):
    """AstrBot 手机操控插件。"""

    _CONTROL_SUBS = frozenset({"stop", "verify", "wg"})
    _HELP_SUBS = frozenset({"", "help", "-h", "--help", "?", "？"})

    async def terminate(self) -> None:
        """插件停用/重载时终止后台循环并恢复设备状态。

        AstrBot 的 ``PluginManager._terminate_plugin`` 只探测插件类**自身**
        ``__dict__`` 中是否有 ``terminate``；停止逻辑在 ``BootstrapMixin`` 里，
        若不在子类类体内重新声明，框架 reload/禁用插件时不会调用，旧实例的
        keepalive/抓包循环会在后台继续运行并操作同一台设备。
        """
        try:
            await super().terminate()
        finally:
            LIVE_PLUGIN_INSTANCES.discard(self)

    @filter.command("phone", desc="/phone help 查看手机操控指令")
    async def phone_command(self, event: AstrMessageEvent, args: GreedyStr) -> None:
        """``/phone`` 命令入口：先鉴权，再做抢占登记等调度副作用。"""
        event.stop_event()
        raw = str(args or "").strip()
        tokens = raw.split()
        sub = tokens[0].lower() if tokens else ""
        if sub in self._HELP_SUBS:
            await self._say_command(event, _HELP_TEXT, "help")
            return
        try:
            denied = check_admin_permission(event, self._config())
        except Exception:  # noqa: BLE001 - 配置异常时拒绝而不是放行
            denied = "error: 无法校验手机操作权限，请检查插件配置"
        if denied:
            await self._say(event, denied)
            return
        self._remember_event_session(event)
        # 鉴权通过后才允许登记用户任务并中止自主任务（任何调度副作用都在鉴权之后）。
        await self._enter_user_priority(event)
        # 命令内产生的浏览日志标记为本轮命令，供清理保护按轮次保留。
        round_token = set_round(new_round_id(), ORIGIN_COMMAND)
        entered = False
        try:
            if sub in self._CONTROL_SUBS:
                await self._dispatch_phone(event, raw)
            else:
                with self.device_gate.scope(
                    event, purpose=f"command:{sub}", write=True
                ):
                    entered = True
                    await self._dispatch_phone(event, raw)
        except GateError as exc:
            await self._say(event, exc.message)
        except PhoneControlError as exc:
            await self._say(event, f"error: {exc}")
        except AutoBrowseError as exc:
            await self._say(event, f"error: {exc}")
        except Exception:  # noqa: BLE001 - 命令层兜底，避免异常打断消息管道
            logger.exception("处理 /phone 指令失败：%r", raw)
            await self._say(event, "error: 指令执行失败，请查看 AstrBot 日志")
        finally:
            reset_round(round_token)
            if entered:
                self.device_gate.release_event(event)
            self._exit_user_priority(event)

    @filter.on_using_llm_tool()
    async def track_phone_tool_use(
        self, event: AstrMessageEvent, tool: Any, tool_args: Any
    ) -> None:
        """每次手机工具调用后按 per_step_tokens 估算下一轮 LLM 开销。"""
        try:
            name = str(getattr(tool, "name", "") or "")
            if not name.startswith("phone_"):
                return
            if name == "phone_start_background_task":
                return
            if not self.service.is_task_session(event):
                # 用户会话里调用手机工具（即使消息没命中关键词）也视为用户手机任务。
                await self._enter_user_priority(event)
            if self._ensure_live_screen_started(event):
                self._live_last_activity = time.monotonic()
            self._note_backend_activity()
            event.set_extra("phone_active", True)
            event.set_extra("phone_quiet_run", True)
            if name in _PHONE_CONTROL_TOOLS:
                pending = list(event.get_extra("phone_control_pending") or [])
                pending.append(name)
                event.set_extra("phone_control_pending", pending)
            self._record_llm_round_metric(event, name, tool_args)
            settings = self._config().quota
            tokens = float(settings.per_step_tokens)
            if tokens <= 0:
                return
            play = bool(self.service.play_payload(event))
            if play:
                self.service.ensure_play_budget(event)
            session = str(getattr(event, "unified_msg_origin", "") or "")
            self.service.record_tokens(
                tokens, session=session, source=f"tool:{name}", play=play
            )
            try:
                recorded = float(event.get_extra("phone_usage_recorded") or 0.0)
                event.set_extra("phone_usage_recorded", recorded + tokens)
            except Exception:  # noqa: BLE001 - 鸭子类型事件
                pass
        except Exception as exc:  # noqa: BLE001 - 计费失败不影响工具调用
            logger.warning("统计手机工具 token 失败：%s", exc)

    @filter.on_llm_tool_respond()
    async def track_phone_tool_result(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: Any,
        tool_result: Any = None,
    ) -> None:
        """手机工具执行结束：记录控制动作是否成功，并从此刻重启下一轮模型计时。"""
        try:
            name = str(getattr(tool, "name", "") or "")
            if not name.startswith("phone_"):
                return
            if not check_admin_permission(event, self.service.config):
                self._tool_reports.submit(event, name, tool_result)
            if name in _PHONE_CONTROL_TOOLS:
                pending = list(event.get_extra("phone_control_pending") or [])
                if pending:
                    pending.pop(0)
                    event.set_extra("phone_control_pending", pending)
                if not self._phone_tool_result_failed(tool_result):
                    # 只有真正执行成功的控制动作才触发「玩完回桌面清后台」；
                    # 权限/参数错误等失败调用不应改变手机状态。
                    event.set_extra("phone_control_used", True)
            event.set_extra("phone_llm_started", time.monotonic())
        except Exception as exc:  # noqa: BLE001 - 汇报/指标失败不影响工具调用
            logger.warning("手机工具结果跟踪失败：%s", type(exc).__name__)

    @filter.on_llm_response()
    async def track_llm_response_usage(
        self, event: AstrMessageEvent, response: Any
    ) -> None:
        """真实 usage 修正估算，并记录 LLM tokens/耗时/tps 指标。"""
        try:
            usage = getattr(response, "usage", None)
            estimated = float(event.get_extra("phone_est_tokens", 0) or 0)
            est_tokens = float(
                event.get_extra("phone_llm_est_tokens", estimated) or estimated
            )
            play = bool(self.service.play_payload(event))
            phone_active = bool(event.get_extra("phone_active"))
            metric_active = bool(event.get_extra("phone_metric_active"))
            started = event.get_extra("phone_llm_started")
            if not (play or phone_active or metric_active):
                return
            real = self._usage_total(usage) if usage is not None else 0.0
            now = time.monotonic()
            try:
                duration_ms = (
                    max(0.0, (now - float(started)) * 1000.0)
                    if started is not None
                    else 0.0
                )
            except (TypeError, ValueError):
                duration_ms = 0.0
            # LLM 指标：tps 用模型**输出** tokens / 本轮耗时（输入上下文不计入
            # token/s）；优先真实 usage.output，其次按最终回复文本估算。
            output_tokens = self._usage_output(usage)
            if output_tokens <= 0:
                output_tokens = float(
                    estimate_tokens(
                        str(getattr(response, "completion_text", "") or "")
                    )
                )
            tokens = output_tokens
            if (phone_active or metric_active) and tokens > 0 and duration_ms > 0:
                try:
                    self.telemetry.record_llm(
                        tokens=tokens,
                        ms=duration_ms,
                        session=str(
                            getattr(event, "unified_msg_origin", "") or ""
                        ),
                    )
                except Exception:  # noqa: BLE001 - 指标失败不影响对话
                    pass
            event.set_extra("phone_est_tokens", 0.0)
            event.set_extra("phone_llm_started", 0.0)
            event.set_extra("phone_llm_est_tokens", 0.0)
            event.set_extra("phone_metric_active", False)
        except Exception as exc:  # noqa: BLE001 - usage 解析失败只记录
            logger.warning("统计 LLM 响应 usage 失败：%s", exc)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_chat_message(self, event: AstrMessageEvent) -> None:
        """记录最近聊天消息（跳过命令、空文本、机器人自己与 cron 事件）。"""
        try:
            platform = str(event.get_platform_name() or "")
            if platform.lower() == "cron":
                return
            # 本地存储关闭后不再把聊天内容写入插件本地。
            if not self._config().memory.local_enabled:
                return
            message_type = event.get_message_type()
            is_group = getattr(message_type, "name", "") == "GROUP_MESSAGE"
            self.chat_memory.record(
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                text=event.get_message_str() or "",
                session_id=event.get_session_id() or "",
                platform=platform,
                sender_id=event.get_sender_id() or "",
                sender_name=event.get_sender_name() or "",
                is_group=is_group,
                self_id=event.get_self_id() or "",
            )
        except Exception as exc:  # noqa: BLE001 - 聊天记录失败不影响对话
            logger.warning("记录聊天消息失败：%s", exc)

    @filter.on_agent_begin()
    async def track_task_agent_begin(
        self,
        event: AstrMessageEvent,
        run_context: Any = None,
    ) -> None:
        """记录插件任务 Agent 的运行 task，供 /phone stop 取消。"""
        payload = event.get_extra("cron_payload") or {}
        if isinstance(payload, dict) and payload.get("work_as_subagent"):
            job_id = str((event.get_extra("cron_job") or {}).get("id") or "")
            record = self.service.background_tasks.records.get(job_id)
            if self._terminated or record is None or record["status"] == "cancelled":
                raise asyncio.CancelledError("后台手机任务已取消或所属插件实例已关闭")
            await self._enter_user_priority(event)
            self.service.background_tasks.update(job_id, "running")
        if not self.service.activity_allowed(event):
            raise asyncio.CancelledError("自主模式已关闭或当前不在允许时间段内")
        try:
            from .core.interactions import interaction_run

            event.set_extra("phone_activity_cleanup_done", False)
            interaction_run(event, restart=True)
            # 所有 Agent run 都标记：工具作用域据此把租约保持到 run 结束。
            with suppress(Exception):
                event.set_extra("phone_agent_run_active", True)
            origin = self.service.task_origin(event)
            # 以一次 Agent run 作为一轮任务，浏览日志清理保护按该编号分组。
            set_round(
                new_round_id(),
                ORIGIN_AUTONOMOUS if origin else ORIGIN_USER,
            )
            if not origin:
                return
            task = asyncio.current_task()
            if task is None:
                return
            cron_job = event.get_extra("cron_job") or {}
            job_id = (
                str(cron_job.get("id") or "")
                if isinstance(cron_job, dict)
                else ""
            )
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo:
                self._active_agent_tasks[umo] = (task, job_id, origin)
                if origin == "phone_background_task":
                    self._background_agent_events[umo] = event
                self._track_activity_window(event, task)
        except Exception:  # noqa: BLE001 - 记录失败不影响任务
            pass

    @filter.on_agent_done()
    async def on_task_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any = None,
        response: Any = None,
    ) -> None:
        """Agent 结束：任务会话停止实时屏幕；玩过手机则确定性收尾。"""
        managed_activity = self.service.task_origin(event) in {
            "phone_random_play", "phone_background_task", CONTINUOUS_JOB_ORIGIN, AUTO_BROWSE_JOB_ORIGIN,
        }
        if managed_activity:
            if event.get_extra("phone_activity_cleanup_done"):
                return
            event.set_extra("phone_activity_cleanup_done", True)
        if self.service.task_origin(event) == "phone_background_task":
            job_id = str((event.get_extra("cron_job") or {}).get("id") or "")
            running = self._active_agent_tasks.get(str(event.unified_msg_origin))
            cancelled = bool(running and running[0].cancelled())
            status = "cancelled" if cancelled else ("completed" if response is not None else "ended")
            self.service.background_tasks.update(job_id, status, str(getattr(response, "completion_text", "") or ""))
        try:
            from .core.interactions import RUN_EXTRA

            # 当前 Agent run 结束：清空轮次标记，避免后续无关调用误归入本轮。
            set_round("", "")
            try:
                self.interactions.finish_run(str(event.get_extra(RUN_EXTRA) or ""))
            except Exception as exc:
                logger.error("释放本轮互动预约失败：%s", exc)
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            self._active_agent_tasks.pop(umo, None)
            self._background_agent_events.pop(umo, None)
            if hasattr(self.service, "_contents"):
                await self.service.contents.finish_owner(self.service.contents.owner(event), abnormal=False)
            # 先释放本 run 的设备租约，后续收尾/恢复调度才能看到真实占用。
            self.device_gate.release_event(event)
            await self._restore_task_provider_preference(umo)
            self._exit_user_priority(event)
            self._mark_continuous_round_finished(event)
            used_phone_control = bool(event.get_extra("phone_control_used"))
            is_task = self.service.is_task_session(event)
            if is_task:
                umo = str(getattr(event, "unified_msg_origin", "") or "")
                self._live_task_sessions.discard(umo)
                if not self._live_task_sessions:
                    await self._stop_live_screen("任务结束")
            if used_phone_control or is_task:
                await self._auto_cleanup_after_task(event)
            await self.service.experiences.finish(event)
            self._schedule_usage_reconcile(event)
        except Exception as exc:  # noqa: BLE001 - 收尾失败不影响 Agent 结束
            logger.warning("任务结束收尾失败：%s", exc)

    @filter.on_waiting_llm_request()
    async def apply_task_provider(self, event: AstrMessageEvent) -> None:
        """自主任务（刷手机/学习）按配置切换模型供应商。

        AstrBot 在构建 Agent 前会优先使用 ``event.selected_provider``；这里只对
        本插件创建的 cron 主动任务注入，用户对话/手动命令会话保持默认模型。
        """
        try:
            config = self._config()
            provider_id = str(
                getattr(config.model, "provider_id", "") or ""
            ).strip()
            if not provider_id:
                return
            if not self._is_autonomous_task(event):
                return
            if event.get_extra("selected_provider"):
                return
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                if provider_id not in self._task_provider_warned_ids:
                    self._task_provider_warned_ids.add(provider_id)
                    logger.warning(
                        "自主任务模型供应商不存在：%s（本次回退默认模型）",
                        provider_id,
                    )
                return
            event.set_extra("selected_provider", provider_id)
        except Exception as exc:  # noqa: BLE001 - 供应商覆盖失败回退默认模型
            logger.debug("自主任务模型供应商应用失败：%s", exc)

    @filter.on_llm_request()
    async def inject_phone_guidance(self, event: AstrMessageEvent, req: Any) -> None:
        """注入手机能力指南；浏览记忆只对授权用户的手机任务注入。"""
        try:
            self._remember_event_session(event)
            with suppress(Exception):
                event.set_extra("phone_agent_run_active", True)
            config = self._config()
            # 鉴权先于任何调度副作用：未授权请求不登记用户任务、不预热后端。
            try:
                authorized = check_admin_permission(event, config) is None
            except Exception:  # noqa: BLE001 - 权限判定异常按未授权处理
                authorized = False
            event.set_extra("phone_output_candidate", bool(authorized and config.adb.configured))
            from .core.subagent import route_request

            if authorized and route_request(self.service, event, req):
                return
            is_task = self.service.is_task_session(event)
            looks_like_phone = self._looks_like_phone_request(event, req)
            event.set_extra("phone_quiet_run", bool(authorized and (is_task or looks_like_phone)))
            event.set_extra("phone_shared_current_run", False)
            if authorized and self._looks_like_user_phone_request(event):
                await self._enter_user_priority(event)
            self._begin_llm_metric(event, req)
            # 检测必须在注入 guidance 之前：任务会话看 cron payload，用户手机请求看
            # 关键词/已有 marker（本方法稍后把【手机操控能力】追加到本轮临时内容块）。
            if authorized and looks_like_phone:
                self._record_request_tokens(event, req)
                self._maybe_warm_scrcpy(event, req)
            if not config.adb.configured:
                return
            if not config.prompt.inject_guidance:
                return
            if not authorized:
                return
            guidance = _build_guidance(config)
            if is_task:
                guidance = guidance.replace(
                    "多步任务先 phone_start_background_task；", "当前手机任务直接完成；"
                )
            sections = [guidance]
            # 浏览日志/学习档案/App 经验/新鲜感/验证状态属于手机活动记忆，
            # 只在手机任务或明确的手机请求里注入，普通对话不扩大上下文。
            if is_task or looks_like_phone:
                local_memory = bool(config.memory.local_enabled)
                if (
                    local_memory
                    and config.browse.auto_remember
                    and config.prompt.diary_limit > 0
                ):
                    diary_text = self.diary.render_for_prompt(config.prompt.diary_limit)
                    if diary_text:
                        sections.append(diary_text)
                if local_memory:
                    try:
                        learning_text = self.service.learning.render_for_prompt(3)
                    except Exception:  # noqa: BLE001 - 学习档案读取失败不影响提示词
                        learning_text = ""
                    if learning_text:
                        sections.append(learning_text)
                if config.prompt.app_memory_enabled and config.prompt.app_memory_limit > 0:
                    try:
                        app_memory_text = await self.service.experiences.render_for_prompt(
                            limit=config.prompt.app_memory_limit,
                            max_chars=900,
                        )
                    except Exception:  # noqa: BLE001 - App 经验读取失败不影响提示词
                        app_memory_text = ""
                    if app_memory_text:
                        sections.append(app_memory_text)
                novelty_text = self.service.novelty_prompt()
                if novelty_text:
                    sections.append(novelty_text)
                try:
                    if self._verification.paused or self._verification.active:
                        sections.append(
                            "【验证状态】\n" + self._verification.status_text()
                        )
                except Exception:  # noqa: BLE001 - 验证状态读取失败不影响提示词
                    pass
            section = "\n\n".join(sections)
            # 动态内容不写 system_prompt（每轮变化会破坏模型前缀缓存），改为追加到
            # 本轮请求的临时用户内容块，且不持久化到会话历史。
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                parts = []
                try:
                    req.extra_user_content_parts = parts
                except Exception:  # noqa: BLE001 - 鸭子类型请求不可写时跳过注入
                    return
            for part in parts:
                text = part if isinstance(part, str) else getattr(part, "text", "")
                if text and "【手机操控能力" in str(text):
                    return
            parts.append(TextPart(text=section).mark_as_temp())
        except Exception as exc:  # noqa: BLE001 - 提示词注入失败不应影响对话
            logger.warning("注入手机能力提示词失败：%s", exc)

    @filter.on_llm_request()
    async def inject_semantic_recall(self, event: AstrMessageEvent, req: Any) -> None:
        """按聊天语义找回历史记忆并注入本轮临时内容块。

        与 ``inject_phone_guidance`` 完全独立：只读本地向量索引并计算一次查询
        embedding，不登记用户任务、不计费、不预热 scrcpy/ADB、不创建后台任务；
        未配置 ADB 的假开启状态同样可用。任何超时/异常都静默降级，不影响对话。
        """
        try:
            config = self._config()
            if not config.recall.enabled:
                return
            # 聊天语义找回只读插件本地记忆；本地存储关闭时不再注入。
            if not config.memory.local_enabled:
                return
            # 只处理普通聊天：cron/后台/自主任务不注入。
            if str(event.get_platform_name() or "").lower() == "cron":
                return
            try:
                if self.service.is_task_session(event):
                    return
            except Exception:  # noqa: BLE001 - 无法判定的会话按任务处理，宁可不注入
                return
            if event.get_extra("cron_payload"):
                return
            try:
                denied = check_admin_permission(event, config)
            except Exception:  # noqa: BLE001 - 权限判定异常按未授权处理
                denied = "error: 无法校验手机操作权限"
            if denied:
                return
            message_type = None
            try:
                message_type = event.get_message_type()
            except Exception:  # noqa: BLE001 - 鸭子类型事件没有消息类型
                message_type = None
            if getattr(message_type, "name", "") == "GROUP_MESSAGE":
                # 手机记忆属于个人数据：默认只在私聊注入，避免泄露到群聊。
                return
            query = str(event.get_message_str() or "").strip()
            if not query:
                # 语音/图片等消息可能没有文本，回退到本轮请求文本。
                query = str(getattr(req, "prompt", "") or "").strip()
            if not query or query.startswith("/"):
                return
            reader = getattr(self, "semantic_reader", None)
            if reader is None:
                return
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                parts = []
                try:
                    req.extra_user_content_parts = parts
                except Exception:  # noqa: BLE001 - 鸭子类型请求不可写时跳过注入
                    return
            for part in parts:
                text = part if isinstance(part, str) else getattr(part, "text", "")
                if text and RECALL_MARKER in str(text):
                    return
            section = await reader.render(query)
            if not section:
                return
            # 动态内容不写 system_prompt（会破坏前缀缓存），且不持久化到会话历史。
            parts.append(TextPart(text=section).mark_as_temp())
        except Exception as exc:  # noqa: BLE001 - 找回失败绝不影响对话
            logger.debug("聊天语义找回注入失败（本轮跳过）：%s", exc)
