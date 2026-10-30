"""插件 /phone 命令实现 mixin：子命令分发与各命令处理。"""

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

from ..command_card import COMMAND_CARD_TEMPLATE, download_command_image
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


class CommandsMixin:
    async def _dispatch_phone(self, event: AstrMessageEvent, raw: str) -> None:
        """解析并执行 ``/phone`` 子指令。"""
        tokens = raw.split()
        if not tokens or tokens[0].lower() in {"help", "-h", "--help", "?", "？"}:
            await self._say_command(event, _HELP_TEXT, "help")
            return

        config = self._config()
        denied = check_admin_permission(event, config)
        if denied:
            await self._say(event, denied)
            return

        sub = tokens[0].lower()
        rest = tokens[1:]
        if sub in {"mesh", "network"}:
            action = rest[0].lower() if rest else "status"
            if action not in {"start", "login", "stop", "status"}:
                await self._say(event, "用法：/phone network status|start|login|stop")
                return
            result = await self._network_control(action)
            await self._say(event, json.dumps(result, ensure_ascii=False, indent=2))
            return

        if self._verification.paused and sub not in {
            "verify",
            "status",
            "stop",
            "help",
            "traffic",
            "quota",
            "learn",
            "novelty",
            "wg",
        }:
            await self._say(
                event,
                "手机自动操作已暂停：当前在等待人工完成验证。\n"
                + self._verification.status_text(),
            )
            return
        quota_error = self.service.quota_error_text()
        if quota_error and sub not in {"help", "quota", "stop", "learn", "continuous", "traffic", "novelty", "wg"}:
            await self._say(event, quota_error)
            return
        if not config.adb.configured and sub not in {"help", "diary", "quota", "stop", "learn", "traffic", "novelty"}:
            await self._say_command(event, f"error: {UNCONFIGURED_MESSAGE}", sub)
            return
        if sub == "status":
            status_text = await self.service.status_text()
            if self.service.user_priority.active:
                status_text += (
                    "\n- 用户任务优先：AI 自刷/后台任务已暂停（用户任务结束后自动恢复）"
                )
            status_text += "\n- " + self.device_gate.status_text()
            wireguard = getattr(self, "_wireguard", None)
            if wireguard is not None:
                status_text += "\n- " + wireguard.brief_text()
            await self._say_command(event, status_text, "status")
        elif sub == "connect":
            info = await self.service.connect()
            await self._say(
                event,
                f"已连接 {info.serial}：{info.manufacturer} {info.model}，"
                f"{info.width}x{info.height}，前台 {info.current_package or '未知'}",
            )
        elif sub == "screenshot":
            _data, path = await self.service.screenshot()
            await event.send(MessageChain().file_image(str(path)))
            await self._say(event, f"截图已保存：{path}")
        elif sub == "open":
            if not rest:
                await self._say(
                    event,
                    "用法：/phone open <app>（中文名/别名/包名）；"
                    "不知道装了什么可让我调用 phone_list_apps 查看",
                )
                return
            await self._say(event, await self.service.open_app(rest[0]))
        elif sub == "browse":
            await self._cmd_browse(event, config, rest)
        elif sub == "diary":
            await self._cmd_diary(event, rest)
        elif sub == "auto":
            await self._cmd_auto(event, config, rest)
        elif sub == "quota":
            await self._cmd_quota(event, rest)
        elif sub == "interactions":
            await self._cmd_interactions(event, config, rest)
        elif sub == "task":
            await self._cmd_task(event, raw)
        elif sub == "tasks":
            await self._cmd_tasks(event)
        elif sub == "play":
            await self._cmd_play(event, config, rest)
        elif sub == "stop":
            await self._cmd_stop(event, rest)
        elif sub == "continuous":
            await self._cmd_continuous(event, config, rest)
        elif sub == "traffic":
            await self._cmd_traffic(event, config, rest)
        elif sub == "wg":
            await self._cmd_wireguard(event, rest)
        elif sub == "verify":
            await self._cmd_verify(event, rest)
        elif sub == "learn":
            await self._cmd_learn(event, rest)
        elif sub == "novelty":
            await self._cmd_novelty(event, rest)
        elif sub in {"watch", "media"}:
            await self._cmd_watch(event, rest)
        else:
            await self._say(event, f"未知子指令：{sub}\n" + _HELP_TEXT)

    async def _say_command(self, event: AstrMessageEvent, text: str, sub: str) -> None:
        """按 status/help 各自开关输出，渲染故障以文字说明。"""
        settings = self._config().command
        as_image = (sub == "status" and settings.status_as_image) or (
            sub == "help" and settings.help_as_image
        )
        if not as_image:
            await self._say(event, text)
            return
        try:
            image_url = await self.html_render(
                COMMAND_CARD_TEMPLATE,
                {"title": "手机操控 · " + ("帮助" if sub == "help" else "状态"), "text": text},
                return_url=True,
                options={"type": "png", "full_page": True},
            )
            if not image_url:
                raise ValueError("文字转图服务未返回图片资源地址")
            image_path = await download_command_image(str(image_url))
        except Exception as exc:  # noqa: BLE001 - 图片服务错误向用户明确说明
            logger.warning("/phone %s 图片渲染失败：%s", sub, exc)
            await self._say(event, "图片渲染失败，请检查 AstrBot 文字转图服务。\n" + text)
            return
        await event.send(MessageChain().file_image(str(image_path)))

    async def _say(self, event: AstrMessageEvent, text: str) -> None:
        """发送一条纯文本消息。"""
        if text:
            await event.send(MessageChain().message(str(text)))

    async def _cmd_browse(
        self,
        event: AstrMessageEvent,
        config: PhoneConfig,
        rest: list[str],
    ) -> None:
        app = config.browse.default_app.strip()
        items = config.browse.default_items
        args = list(rest)
        if args and not args[0].isdigit():
            app = args.pop(0)
        if args and args[0].isdigit():
            items = int(args.pop(0))
        if not app:
            try:
                app, picked_label = await self.service.random_browse_app()
            except PhoneControlError as exc:
                await self._say(event, f"随机选择 App 失败：{exc}")
                return
            await self._say(event, f"未指定 App，随机选择 {picked_label}")
        try:
            _package, app_label_text = self.service.resolve_app_target(app)
        except PhoneControlError:
            app_label_text = app
        await self._say(event, f"正在浏览 {app_label_text}，目标 {items} 条…")
        result = await self.service.browse(
            app,
            items=items,
            collect_screenshots=config.browse.collect_screenshots,
            remember=config.browse.auto_remember,
        )
        lines = [result.summary_text()]
        if result.diary_recorded:
            lines.append("已写入浏览记忆")
        await self._say(event, "\n".join(lines))
        if config.browse.send_screenshots_to_user:
            for item in result.items[:3]:
                if item.screenshot_path and Path(item.screenshot_path).is_file():
                    await event.send(MessageChain().file_image(item.screenshot_path))

    async def _cmd_diary(self, event: AstrMessageEvent, rest: list[str]) -> None:
        limit = 5
        query = ""
        if rest:
            if rest[0].isdigit():
                limit = int(rest[0])
            else:
                query = " ".join(rest)
        await self._say(event, await self.service.recall(query, limit=limit))

    async def _cmd_auto(
        self,
        event: AstrMessageEvent,
        config: PhoneConfig,
        rest: list[str],
    ) -> None:
        action = rest[0].lower() if rest else "list"
        if action == "on":
            if len(rest) < 2:
                await self._say(
                    event,
                    "用法：/phone auto on <app[,app]> [cron]\n"
                    "例如：/phone auto on douyin 0 */2 * * *",
                )
                return
            apps = rest[1]
            cron = " ".join(rest[2:]) if len(rest) > 2 else config.auto.cron
            notes = await self._apply_config_patch({"auto": {"mode": "定时玩", "activity_enabled": True, "apps": apps, "cron": cron, "session": event.unified_msg_origin}})
            if any("失败" in note for note in notes):
                await self._say(event, "\n".join(notes))
                return
            jobs = await self.scheduler.enable(
                umo=event.unified_msg_origin,
                apps=apps,
                cron=cron,
                max_items=config.auto.max_items,
                prompt=config.auto.prompt,
                sender_id=str(event.get_sender_id() or ""),
            )
            lines = ["已创建自主浏览任务："]
            for job in jobs:
                suffix = f"（job {job['job_id'][:8]}）" if job.get("job_id") else ""
                lines.append(f"- {job['label']}：{job['cron']}{suffix}")
            await self._say(event, "\n".join(lines))
        elif action == "off":
            job_id = rest[1] if len(rest) > 1 else None
            removed = await self.scheduler.disable(job_id)
            if not await self.scheduler.list_jobs():
                await self._apply_config_patch({"auto": {"enabled": False}})
            await self._say(event, f"已取消 {removed} 个自主浏览任务")
        elif action == "list":
            jobs = await self.scheduler.list_jobs()
            if not jobs:
                await self._say(event, "当前没有自主浏览任务")
                return
            lines = ["自主浏览任务："]
            for job in jobs:
                job_id = str(job.get("job_id") or "")
                suffix = f"（job {job_id[:8]}）" if job_id else "（降级模式）"
                next_run = str(job.get("next_run_time") or "")
                lines.append(
                    f"- {job.get('label') or job.get('app')}：{job.get('cron')}"
                    f"{suffix}" + (f" 下次 {next_run}" if next_run else "")
                )
            await self._say(event, "\n".join(lines))
        else:
            await self._say(event, "用法：/phone auto on|off|list")

    async def _cmd_interactions(
        self,
        event: AstrMessageEvent,
        config: PhoneConfig,
        rest: list[str],
    ) -> None:
        """显示今日评论/发帖次数、上限与最近记录。"""
        limit = 5
        if rest:
            try:
                limit = int(rest[0])
            except ValueError:
                await self._say(event, "用法：/phone interactions [n]，n 为 1-30 的整数")
                return
            limit = max(1, min(limit, 30))
        security = config.security
        from ..interactions import interaction_limits
        comments, active_cooldown = interaction_limits(security, config.flexibility, KIND_COMMENT)
        posts, _ = interaction_limits(security, config.flexibility, KIND_POST)
        passive, passive_cooldown = interaction_limits(security, config.flexibility, KIND_PASSIVE_REPLY)
        await self._say(
            event,
            self.service.interactions.status_text(
                allow_comments=security.allow_comments,
                allow_posts=security.allow_posts,
                max_comments=comments,
                max_posts=posts,
                cooldown_seconds=active_cooldown,
                allow_passive_replies=security.allow_passive_replies,
                max_passive_replies=passive,
                passive_cooldown_seconds=passive_cooldown,
                allow_interactions=security.allow_interactions,
                max_likes_per_session=security.max_likes_per_session,
                limit=limit,
            ),
        )

    async def _cmd_task(self, event: AstrMessageEvent, raw: str) -> None:
        """``/phone task <instruction>``：把多步任务转入后台静默执行。"""
        if self._verification.paused:
            await self._say(
                event,
                "手机自动操作已暂停（等待人工完成验证），暂不能创建后台手机任务；\n"
                + self._verification.status_text(),
            )
            return
        parts = str(raw or "").strip().split(maxsplit=1)
        instruction = parts[1].strip() if len(parts) > 1 else ""
        try:
            instruction = validate_background_instruction(instruction)
        except BackgroundTaskError as exc:
            await self._say(
                event,
                f"{exc}\n用法：/phone task <instruction>（例如 /phone task 去B站看三个科技视频并总结）",
            )
            return
        share_target = str(getattr(event, "unified_msg_origin", "") or "")
        cron_manager = getattr(self.context, "cron_manager", None)
        try:
            job = await create_background_task(
                cron_manager,
                instruction=instruction,
                share_target=share_target,
                sender_id=str(event.get_sender_id() or ""),
            )
        except BackgroundTaskError as exc:
            await self._say(event, f"error: {exc}")
            return
        await self._apply_task_provider_preference(job)
        await self._say(
            event,
            f"已接单，转入后台静默执行（job {getattr(job, 'job_id', '?')}），"
            "完成后会主动向你汇报；期间可继续聊天，用 /phone tasks 查看进度。",
        )

    async def _cmd_tasks(self, event: AstrMessageEvent) -> None:
        """``/phone tasks``：列出本插件创建的后台任务。"""
        cron_manager = getattr(self.context, "cron_manager", None)
        try:
            tasks = await list_background_tasks(cron_manager)
        except BackgroundTaskError as exc:
            await self._say(event, f"error: {exc}")
            return
        if not tasks:
            extra = (
                "；持续刷手机运行中（/phone continuous status 查看）"
                if self._continuous_enabled
                else ""
            )
            await self._say(event, f"当前没有本插件创建的后台任务{extra}")
            return
        lines = ["本插件后台任务（静默运行，完成后主动汇报）："]
        if self._continuous_enabled:
            lines.append(
                "- 持续刷手机：运行中（/phone continuous status 查看，/phone stop 停止）"
            )
        for task in tasks:
            job_id = str(task.get("job_id") or "")
            short_id = job_id[:8] if job_id else "降级"
            created = str(task.get("created_at") or "").replace("T", " ")[:16]
            next_run = str(task.get("next_run_time") or "").replace("T", " ")[:16]
            extras = []
            if created:
                extras.append(f"创建 {created}")
            if next_run:
                extras.append(f"计划 {next_run}")
            suffix = f"（{'，'.join(extras)}）" if extras else ""
            lines.append(
                f"- {short_id}｜{task.get('status') or '未知'}{suffix}："
                f"{task.get('summary') or '（无摘要）'}"
            )
        await self._say(event, "\n".join(lines))

    async def _cmd_play(
        self,
        event: AstrMessageEvent,
        config: PhoneConfig,
        rest: list[str],
    ) -> None:
        """立即以静默 run_once 主动任务启动一轮玩手机。"""
        if self._verification.paused:
            await self._say(
                event,
                "手机自动操作已暂停（等待人工完成验证），暂不能开新一轮玩手机；\n"
                + self._verification.status_text(),
            )
            return
        tokens = config.quota.play_task_tokens
        if rest:
            try:
                tokens = int(rest[0])
            except ValueError:
                await self._say(event, "用法：/phone play [tokens]，tokens 为正整数")
                return
            tokens = max(0, tokens)
        quota_error = self.service.quota_error_text()
        if quota_error:
            await self._say(event, quota_error)
            return
        cron_manager = getattr(self.context, "cron_manager", None)
        if cron_manager is None:
            await self._say(event, "error: cron 管理器不可用，无法安排玩手机任务")
            return
        security = config.security
        note = build_play_note(
            tokens,
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
            "origin": "phone_play",
            "play_task": True,
            "target_tokens": tokens,
        }
        try:
            job = await cron_manager.add_active_job(
                name="phone-play",
                cron_expression=None,
                payload=payload,
                run_once=True,
                run_at=datetime.now(timezone.utc) + timedelta(seconds=2),
            )
        except Exception as exc:  # noqa: BLE001 - 调度失败给出可操作提示
            await self._say(event, f"error: 安排玩手机任务失败：{exc}")
            return
        await self._apply_task_provider_preference(job)
        await self._say(
            event,
            f"已安排本轮玩手机：预算 {tokens} tokens，静默运行（不逐条汇报）；"
            f"完成后按心情与额度决定是否分享（job {getattr(job, 'job_id', '?')}）。",
        )

    async def _cmd_stop(self, event: AstrMessageEvent, rest: list[str]) -> None:
        if not hasattr(self, "_phone_stop_lock"):
            self._phone_stop_lock = asyncio.Lock()
        async with self._phone_stop_lock:
            self._stopping_phone = True
            try:
                await self._stop_phone_impl(event, rest)
            finally:
                self._stopping_phone = False

    async def _stop_phone_impl(
        self, event: AstrMessageEvent, rest: list[str]
    ) -> None:
        """``/phone stop [job_id|all]``：停止本插件任务并收尾手机。"""
        resource_errors = []
        recordings = getattr(self.service, "recordings", None)
        if recordings is not None:
            try:
                await recordings.close()
            except Exception as exc:
                resource_errors.append(f"手机录制停止失败：{exc}")
        target = rest[0] if rest else "all"
        job_id = "" if target.lower() in {"all", "-a", "全部"} else target
        if hasattr(self.service, "_contents"):
            await self.service.contents.close(abnormal=True)
        self._phone_stopped = True
        self.device_gate.force_release("phone_stop")
        self._continuous_enabled = False
        self._continuous_active_job_id = ""
        await self._apply_config_patch({
            "auto": {"activity_enabled": False},
            "traffic": {"enabled": False},
        })
        auto_task = getattr(self, "_auto_config_task", None)
        if auto_task is not None and not auto_task.done():
            auto_task.cancel()
            with suppress(asyncio.CancelledError):
                await auto_task
        await self.scheduler.close()
        # 预热属于同一资源生命周期，先取消再关闭后端，避免关闭后又启动。
        warmup = getattr(self, "_scrcpy_warmup_task", None)
        if warmup is not None and not warmup.done():
            warmup.cancel()
            with suppress(asyncio.CancelledError):
                await warmup
        try:
            await self._traffic.stop()
        except Exception as exc:
            resource_errors.append(f"抓包停止失败：{exc}")
        cron_manager = getattr(self.context, "cron_manager", None)
        deleted: list[str] = []
        stopped_runs = 0
        try:
            jobs = await list_plugin_jobs(cron_manager)
        except BackgroundTaskError:
            jobs = []
        if job_id:
            jobs = [
                job
                for job in jobs
                if str(job.get("job_id") or "").startswith(job_id)
            ]
            if not jobs:
                logger.info("未找到指定任务 %s，继续收尾设备", job_id)
        for job in jobs:
            self.service.background_tasks.update(str(job.get("job_id") or ""), "cancelled")
            stopped_runs += await self._abort_running_cron_event(
                str(job.get("job_id") or "")
            )
        if stopped_runs:
            await asyncio.sleep(0.3)
        try:
            deleted = await delete_plugin_jobs(cron_manager, job_id=job_id)
        except BackgroundTaskError as exc:
            logger.warning("停止插件任务失败：%s", exc)
        await self._stop_live_screen("用户停止任务")
        manager = getattr(self, "backends", None)
        if manager is not None:
            try:
                await manager.stop()
            except Exception as exc:
                resource_errors.append(str(exc))
        if self.service.configured:
            report = await self._cleanup_phone_home()
        else:
            report = {"stopped": 0, "errors": []}
        verification_note = ""
        try:
            verification_note = await self._verification.cancel_for_stop()
        except Exception as exc:
            logger.warning("停止后的验证状态处理失败：%s", exc)
        report = report or {}
        report.setdefault("errors", []).extend(resource_errors)
        lines = [
            f"已停止 {len(deleted)} 个插件任务（中止 Agent {stopped_runs} 个）。",
        ]
        if self.service.configured:
            lines.append(
                f"手机已回桌面，强停应用 {report.get('stopped', 0)} 个；"
                "抓包、持续刷与随机任务已关闭。"
            )
        else:
            lines.append("ADB 未配置（插件未启用）：已停止插件任务与后台循环，未操作设备。")
        if report.get("errors"):
            lines.append("后台清理未完成：" + "；".join(report["errors"]))
        if verification_note:
            lines.append(verification_note)
        await self._say(event, "\n".join(lines))

    async def _cmd_continuous(
        self,
        event: AstrMessageEvent,
        config: PhoneConfig,
        rest: list[str],
    ) -> None:
        """``/phone continuous on|off|status``：持续刷手机开关与状态。"""
        action = rest[0].lower() if rest else "status"
        if action in {"on", "start", "开启"} and self._verification.paused:
            await self._say(
                event,
                "手机自动操作已暂停（等待人工完成验证），暂不能开启持续刷；\n"
                + self._verification.status_text(),
            )
            return
        if action in {"on", "start", "开启"}:
            patch: dict[str, Any] = {"auto": {"mode": "自动刷", "activity_enabled": True}}
            auto_patch: dict[str, Any] = {}
            if len(rest) > 1:
                try:
                    gap = normalize_gap_seconds(int(rest[1]))
                except ValueError:
                    await self._say(
                        event,
                        "用法：/phone continuous on [间隔秒] [每轮token预算]",
                    )
                    return
                auto_patch["continuous_gap_seconds"] = gap
            if len(rest) > 2:
                try:
                    tokens = max(0, int(rest[2]))
                except ValueError:
                    await self._say(
                        event,
                        "用法：/phone continuous on [间隔秒] [每轮token预算]",
                    )
                    return
                auto_patch["continuous_target_tokens"] = tokens
            if auto_patch:
                patch["auto"].update(auto_patch)
            await self._apply_config_patch(patch)
            if not self._continuous_enabled:
                self._continuous_enabled = True
                self._continuous_last_finished = 0.0
                self._start_continuous_loop()
            await self._say(
                event,
                "已开启持续刷手机：一轮接一轮地浏览并学习内容，"
                "用 /phone stop 或 /phone continuous off 停止。\n"
                + self._continuous_status_text(),
            )
        elif action in {"off", "stop", "关闭"}:
            self._continuous_enabled = False
            await self._apply_config_patch(
                {"auto": {"continuous_enabled": False}}
            )
            await self._say(
                event, "已关闭持续刷手机，相关自主任务已停止。"
            )
        elif action in {"status", "状态"}:
            await self._say(event, self._continuous_status_text())
        else:
            await self._say(event, "用法：/phone continuous on|off|status [间隔秒] [token预算]")

    async def _cmd_verify(
        self, event: AstrMessageEvent, rest: list[str]
    ) -> None:
        """``/phone verify [status|resume <id>|cancel <id>]``：人工协助处置。"""
        action = rest[0].lower() if rest else "status"
        session_id = rest[1] if len(rest) > 1 else ""
        if action in {"status", "状态"}:
            await self._say(event, self._verification.status_text())
        elif action in {"resume", "恢复", "完成"}:
            if not session_id:
                await self._say(
                    event,
                    "用法：/phone verify resume <编号>（编号见 /phone verify status）",
                )
                return
            await self._say(event, await self._verification.resume(session_id, event))
        elif action in {"cancel", "取消"}:
            if not session_id:
                await self._say(
                    event,
                    "用法：/phone verify cancel <编号>（编号见 /phone verify status）",
                )
                return
            await self._say(event, await self._verification.cancel(session_id, event))
        else:
            await self._say(
                event,
                "用法：/phone verify [status|resume <id>|cancel <id>]\n"
                "status=查看验证等待状态；resume=用户完成验证后确认恢复；"
                "cancel=取消等待并解除设备暂停",
            )

    async def _cmd_learn(self, event: AstrMessageEvent, rest: list[str]) -> None:
        """``/phone learn [关键词|条数]``：查看 AI 在手机上学到的内容。"""
        limit = 8
        query = ""
        if rest:
            if rest[0].isdigit():
                limit = max(1, min(int(rest[0]), 30))
            else:
                query = " ".join(rest)
        try:
            if query:
                entries = self.service.learning.search(query, limit=limit)
            else:
                entries = self.service.learning.recent(limit=limit)
        except Exception as exc:  # noqa: BLE001 - 学习档案读取失败给可读错误
            await self._say(event, f"error: 读取学习档案失败：{exc}")
            return
        if not entries:
            await self._say(
                event,
                "学习档案还是空的：持续刷手机/自主浏览时 AI 会用 phone_learn 记录理解到的内容。",
            )
            return
        lines = [f"AI 在手机上学到的内容（{len(entries)} 条）："]
        for entry in entries:
            when = time.strftime(
                "%m-%d %H:%M", time.localtime(float(entry.get("ts") or 0))
            )
            source = str(entry.get("source") or "").strip()
            source_text = f"[{source}] " if source else ""
            tags = entry.get("tags") or []
            tag_text = (
                "（" + "/".join("#" + str(tag) for tag in tags[:3]) + "）"
                if tags
                else ""
            )
            lines.append(
                f"- {when} {source_text}{entry.get('content') or ''}{tag_text}"
            )
        await self._say(event, "\n".join(lines))

    async def _cmd_novelty(self, event: AstrMessageEvent, rest: list[str]) -> None:
        """``/phone novelty [status|reset]``：查看/清空内容新鲜感状态。"""
        action = (rest[0].lower() if rest else "status")
        if action in {"status", "状态"}:
            await self._say(event, self.service.novelty_status_text())
        elif action in {"reset", "clear", "清空"}:
            try:
                removed = self.service.novelty.reset()
            except Exception as exc:  # noqa: BLE001 - 清空失败给可读错误
                await self._say(event, f"error: 清空新鲜感状态失败：{exc}")
                return
            await self._say(
                event,
                f"已清空内容新鲜感状态（{removed} 个主题），下次会重新累计疲劳度。",
            )
        else:
            await self._say(event, "用法：/phone novelty [status|reset]")

    async def _cmd_watch(self, event: AstrMessageEvent, rest: list[str]) -> None:
        """``/phone watch [秒数] [问题]``：识别当前正在播放的内容。"""
        self._phone_stopped = False
        seconds = 0.0
        if rest:
            text = str(rest[0]).strip().lower().replace("秒", "").replace("s", "")
            try:
                seconds = float(text)
            except (TypeError, ValueError):
                seconds = 0.0
        question = " ".join(rest[1:]).strip() if len(rest) > 1 else ""
        await self._say(
            event,
            await self.service.watch_media(
                event,
                seconds=seconds,
                question=question,
                mode="auto",
            ),
        )

    async def _cmd_quota(self, event: AstrMessageEvent, rest: list[str]) -> None:
        """``/phone quota [audit [小时]]``：额度状态或对照实际 usage 对账。"""
        action = rest[0].lower() if rest else "status"
        if action not in {"audit", "对账"}:
            await self._say(event, self.service.quota_status_text())
            return
        config = self._config()
        hours = float(config.quota.window_hours or 24)
        if len(rest) > 1:
            try:
                hours = float(max(1, min(168, int(rest[1]))))
            except (TypeError, ValueError):
                await self._say(event, "用法：/phone quota audit [小时]，小时为 1-168 的整数")
                return
        try:
            entries = self.service.quota.entries(hours)
        except Exception as exc:  # noqa: BLE001 - 明细读取失败只提示
            await self._say(event, f"读取 token 明细失败：{exc}")
            return
        used = sum(float(entry.get("tokens") or 0) for entry in entries)
        umos = sorted(
            {
                str(entry.get("session") or "")
                for entry in entries
                if str(entry.get("session") or "")
            }
        )
        lines = [
            f"手机 token 对账（窗口 {hours:g} 小时）",
            f"- 插件记账：{used:.0f} tokens（{len(entries)} 条明细，{len(umos)} 个会话）",
        ]
        if umos:
            actual = await usage_audit.fetch_window_usage(umos, hours)
            if actual is not None and actual.calls > 0:
                diff = float(actual.total) - used
                lines.append(
                    "- AstrBot provider_stats（同会话窗口内全部模型运行）："
                    f"{actual.total} tokens（输入未缓存 {actual.input_other} / "
                    f"输入缓存 {actual.input_cached} / 输出 {actual.output}，"
                    f"{actual.calls} 次 run）"
                )
                lines.append(
                    f"- 差值：{diff:+.0f}（同会话可能含普通聊天；插件内部识图调用"
                    "不在 provider_stats 内，已单独记账）"
                )
            else:
                lines.append("- AstrBot provider_stats：查询失败或窗口内暂无运行记录")
        else:
            lines.append("- 窗口内没有手机相关记账，暂无可对账会话")
        lines.append(
            "说明：手机额度只统计手机相关 run；AstrBot 面板总用量包含普通聊天，口径不同。"
        )
        await self._say(event, "\n".join(lines))

    async def _cmd_traffic(
        self,
        event: AstrMessageEvent,
        config: PhoneConfig,
        rest: list[str],
    ) -> None:
        """``/phone traffic [status|tail [n]|on|off]``：抓包状态/最近流量/开关。"""
        action = (rest[0].lower() if rest else "status")
        if action in {"status", "状态"}:
            await self._say(event, self._traffic_text())
        elif action in {"tail", "log", "查看"}:
            try:
                limit = int(rest[1]) if len(rest) > 1 else 10
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            await self._say(event, self._traffic_text(limit=limit))
        elif action in {"on", "开启"}:
            self._phone_stopped = False
            if config.traffic.enabled:
                await self._say(event, "抓包已经是开启状态。\n" + self._traffic_text())
                return
            await self._apply_config_patch({"traffic": {"enabled": True}})
            await self._say(event, "已开启抓包。\n" + self._traffic_text())
        elif action in {"off", "关闭"}:
            if not config.traffic.enabled:
                await self._say(event, "抓包已经是关闭状态。")
                return
            await self._apply_config_patch({"traffic": {"enabled": False}})
            await self._say(event, "已关闭抓包，设备代理与 CA 挂载已恢复。")
        else:
            await self._say(
                event,
                "用法：/phone traffic [status|tail [n]|on|off]\n"
                "status=查看抓包状态与最近流量；tail [n]=看最近 n 条；on/off=开关抓包",
            )

    async def _cmd_wireguard(
        self,
        event: AstrMessageEvent,
        rest: list[str],
    ) -> None:
        """``/phone wg [status|start|stop|restart|config]``：WireGuard 组网管理。"""
        action = rest[0].lower() if rest else "status"
        manager = getattr(self, "_wireguard", None)
        if manager is None:
            await self._say(event, "error: WireGuard 未初始化（插件装配异常）")
            return
        aliases = {
            "status": "status",
            "状态": "status",
            "start": "start",
            "开启": "start",
            "stop": "stop",
            "关闭": "stop",
            "restart": "restart",
            "重启": "restart",
            "config": "config",
            "配置": "config",
        }
        name = aliases.get(action)
        if name is None:
            await self._say(
                event,
                "用法：/phone wg [status|start|stop|restart|config]\n"
                "status=查看 WireGuard 运行状态；start/stop/restart=开关/重启 helper；"
                "config=查看导入用配置（client=服务器需添加的 Peer 片段，"
                "server=手机端完整 WireGuard 配置）",
            )
            return
        if name in {"start", "stop", "restart"}:
            await self._say(event, f"正在执行 WireGuard {name}…")
        try:
            result = await manager.control(name)
        except Exception as exc:  # noqa: BLE001 - 动作失败给可读错误
            logger.warning("WireGuard 动作失败（%s）：%s", name, exc)
            await self._say(event, f"error: WireGuard {name} 失败：{exc}")
            return
        text = str(result.get("text") or "")
        if not result.get("ok") and not text.startswith("error:"):
            text = f"error: {text}" if text else "error: WireGuard 动作失败"
        await self._say(event, text)
