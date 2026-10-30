"""自主浏览调度：用 AstrBot 原生 cron 主动任务定期唤醒主 Agent 浏览信息流。

优先走 ``context.cron_manager.add_active_job``（job_type=active_agent，payload 至少含
``session`` 与 ``note``）；cron 管理器不可用时降级为 asyncio 后台任务：直接跑一次
浏览并把摘要发回原会话，同时记录 warning。

任务归属用 payload 里的 ``origin`` 标记，避免误删用户自己创建的定时任务。
"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .apps import all_app_keys, app_label, resolve_app
from .config import PhoneConfig

JOB_ORIGIN = PLUGIN_NAME
BACKGROUND_JOB_NAME = "phone-background-task"
BACKGROUND_JOB_ORIGIN = "phone_background_task"
BACKGROUND_INSTRUCTION_MAX = 500
_FALLBACK_TICK_SECONDS = 30.0


class AutoBrowseError(Exception):
    """自主浏览调度失败（cron 非法、App 未知等）。"""


class BackgroundTaskError(Exception):
    """后台任务参数/调度错误。"""


def validate_background_instruction(instruction: Any) -> str:
    """校验并归一化后台任务指令（1-500 字，去首尾空白）。"""
    text = str(instruction or "").strip()
    if not text:
        raise BackgroundTaskError("任务指令不能为空：请用 1-500 字描述要手机做什么")
    if len(text) > BACKGROUND_INSTRUCTION_MAX:
        raise BackgroundTaskError(
            f"任务指令过长（{len(text)} 字），请压缩到 {BACKGROUND_INSTRUCTION_MAX} 字以内"
        )
    return text


def build_background_task_note(instruction: Any, share_target: Any = "") -> str:
    """包装后台任务提示词：静默执行、完成后再汇报。"""
    text = validate_background_instruction(instruction)
    target = str(share_target or "").strip() or "当前会话"
    return (
        f"【后台任务】{text}\n\n"
        "这是后台任务：全程静默操作，不要向用户逐步汇报，也不要在工具调用之间输出中间"
        "解说文字；需要连续调用工具时直接调用。"
        f"完成后用 phone_share_to_user 把结果汇报到 target={target}"
        "（必须带内容链接或截图，禁止纯文字：优先取抓包/App 分享接口里的链接，"
        "拿不到链接就 include_screenshot=true 附截图；只是说明文字不作为最终分享内容）。"
        "过程中有评论/发帖时按规则先 phone_interaction_gate 申请、完成后 "
        "phone_interaction_record 记录；收尾由插件自动回桌面并清理后台。"
        "汇报默认 1–2 句、≤80 字；任务结果只给 1–3 个要点，不复述过程/工具输出"
        "（用户要求详情除外）。"
    )


async def create_background_task(
    cron_manager: Any,
    *,
    instruction: Any,
    share_target: Any = "",
    sender_id: str = "",
    source_session: str = "",
    work_as_subagent: bool = False,
) -> Any:
    """创建静默 run_once 主动任务（session 为空 = 不投递过程消息）。"""
    if cron_manager is None:
        raise BackgroundTaskError("cron 管理器不可用，无法创建后台任务")
    text = validate_background_instruction(instruction)
    target = str(share_target or "").strip()
    note = build_background_task_note(text, target)
    payload = {
        "session": "",
        "note": note,
        "origin": BACKGROUND_JOB_ORIGIN,
        "share_target": target,
        "requested_by": str(sender_id or "").strip(),
        "sender_id": str(sender_id or "").strip(),
        "source_session": str(source_session or target).strip(),
        "work_as_subagent": bool(work_as_subagent),
    }
    try:
        return await cron_manager.add_active_job(
            name=BACKGROUND_JOB_NAME,
            cron_expression=None,
            payload=payload,
            description=f"手机后台任务：{text[:80]}",
            run_once=True,
            run_at=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
    except Exception as exc:  # noqa: BLE001 - cron 异常统一转成可读错误
        raise BackgroundTaskError(f"创建后台任务失败：{exc}") from exc


def _job_time_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        if hasattr(value, "isoformat"):
            return str(value.isoformat(timespec="minutes"))
        return str(value)
    except Exception:  # noqa: BLE001 - 时间格式异常不影响列表
        return ""


async def list_background_tasks(cron_manager: Any) -> list[dict[str, Any]]:
    """列出本插件创建的后台任务（origin=phone_background_task）。"""
    if cron_manager is None:
        raise BackgroundTaskError("cron 管理器不可用，无法查询后台任务")
    try:
        jobs = await cron_manager.list_jobs("active_agent")
    except Exception as exc:  # noqa: BLE001
        raise BackgroundTaskError(f"查询后台任务失败：{exc}") from exc
    tasks: list[dict[str, Any]] = []
    for job in jobs or []:
        payload = getattr(job, "payload", None) or {}
        if str(payload.get("origin") or "") != BACKGROUND_JOB_ORIGIN:
            continue
        status = str(getattr(job, "status", "") or "").strip()
        enabled = bool(getattr(job, "enabled", True))
        if not status:
            status = "等待调度" if enabled else "已停用"
        run_once = bool(getattr(job, "run_once", False))
        tasks.append(
            {
                "job_id": str(getattr(job, "job_id", "") or ""),
                "name": str(getattr(job, "name", "") or ""),
                "status": status,
                "run_once": run_once,
                "created_at": _job_time_text(getattr(job, "created_at", None)),
                "next_run_time": _job_time_text(
                    getattr(job, "next_run_time", None)
                ),
                "summary": str(
                    getattr(job, "description", "")
                    or payload.get("instruction", "")
                    or ""
                )[:80],
                "share_target": str(payload.get("share_target") or ""),
            }
        )
    tasks.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return tasks


# 数字、通配、区间、步长与英文星期名（mon-fri 等由 cron 管理器归一化）
_CRON_TOKEN_RE = re.compile(r"^[0-9A-Za-z*,/\-]+$")


def _cron_fields(cron_expression: str) -> list[str]:
    fields = str(cron_expression or "").split()
    if len(fields) != 5:
        raise AutoBrowseError(
            f"cron 表达式 {cron_expression!r} 不合法，必须是 5 段（分 时 日 月 周）"
        )
    for field in fields:
        if not _CRON_TOKEN_RE.match(field):
            raise AutoBrowseError(
                f"cron 表达式 {cron_expression!r} 含非法字符：{field!r}"
            )
    return fields


def _field_matches(field: str, value: int, low: int, high: int) -> bool:
    """判断 5 段 cron 的单个字段是否命中当前数值（降级模式使用）。"""
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            try:
                step = max(1, int(step_text))
            except ValueError:
                return False
        else:
            base = part
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                return False
        else:
            try:
                start = end = int(base)
            except ValueError:
                return False
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def cron_matches(cron_expression: str, moment: datetime) -> bool:
    """判断时间点是否命中 5 段 cron 表达式（降级模式使用）。"""
    try:
        minute, hour, day, month, weekday = _cron_fields(cron_expression)
    except AutoBrowseError:
        return False
    # cron 的星期：0/7 是周日；Python 的 isoweekday：周一到周日 1..7。
    cron_weekday = moment.isoweekday() % 7
    weekday_match = _field_matches(weekday, cron_weekday, 0, 6) or (
        cron_weekday == 0 and _field_matches(weekday, 7, 0, 7)
    )
    day_match = _field_matches(day, moment.day, 1, 31)
    if day != "*" and weekday != "*":
        day_match = day_match or weekday_match
    else:
        day_match = day_match and weekday_match
    return (
        _field_matches(minute, moment.minute, 0, 59)
        and _field_matches(hour, moment.hour, 0, 23)
        and _field_matches(month, moment.month, 1, 12)
        and day_match
    )


class AutoBrowseScheduler:
    """管理自主浏览的 cron 任务。"""

    def __init__(
        self,
        context: Any,
        *,
        config: PhoneConfig | None = None,
        config_provider: Callable[[], PhoneConfig] | None = None,
        run_browse: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        """初始化调度器。

        Args:
            context: AstrBot ``Star`` 的 Context。
            config: 配置快照（无 ``config_provider`` 时的回退），提供默认 cron/Apps/条数。
            config_provider: 实时配置提供者；传入后默认参数始终取最新配置，
                插件保存配置后无需重载即可按新值创建任务。
            run_browse: 降级模式使用的浏览回调：``(app, umo) -> 摘要文本``。
        """
        self.context = context
        self._config_provider = config_provider
        self._config_snapshot = config
        self.run_browse = run_browse
        self._fallback_task: asyncio.Task | None = None
        self._fallback_runs: set[asyncio.Task] = set()
        self._fallback_stop = asyncio.Event()
        self._fallback_jobs_data: list[dict[str, Any]] = []
        self._last_fallback_minute: dict[str, str] = {}

    @property
    def config(self) -> PhoneConfig:
        """当前配置快照：优先实时读取（配置保存后立即生效）。"""
        provider = self._config_provider
        if provider is not None:
            try:
                return provider()
            except Exception:  # noqa: BLE001 - 读取失败回退初始化快照
                pass
        return self._config_snapshot or PhoneConfig()

    # ---- 基础信息 -----------------------------------------------------

    @property
    def cron_manager(self) -> Any:
        return getattr(self.context, "cron_manager", None)

    @property
    def available(self) -> bool:
        """是否可用原生 cron 管理器。"""
        return self.cron_manager is not None

    @staticmethod
    def _job_name(app: str) -> str:
        return f"phone_auto_browse_{app}"

    def _note(self, app: str, max_items: int, prompt: str) -> str:
        """生成唤醒主 Agent 的自由活动提示词（人格主导，不强制采集固定条数）。"""
        label = app_label(app)
        note = (
            f"现在是你的自由活动时间：用手机自由刷 {label}。看什么、停留多久、"
            "是否点开详情或评论、什么时候换内容，都由你按自己的喜好和人格决定；"
            "可以先用 phone_status / phone_screen 观察设备与屏幕，再用 "
            "phone_swipe / phone_tap / phone_wait 自己操作，想快速采样时也可以调用 "
            "phone_browse。不用刻意凑固定条数。"
            "整个过程默认静默、不用逐条汇报；看和分享分开判断——先决定看什么，"
            "看完后再单独判断是否值得分享，分享必须带链接或截图（禁止纯文字），"
            "目标先 phone_recent_chat 看最近聊天再选；每轮最多分享 1 条。"
            "结束时如果环境里有长期记忆工具，把值得记住的内容写入长期记忆。"
        )
        extra = str(prompt or "").strip()
        if extra:
            note += f" 附加偏好：{extra}"
        from .interactions import interaction_plan
        from .play_flow import LIVE_BROWSE_FLOW, content_rule_text, flow_rule_text, render_flexibility
        free = self.config.flexibility.free_browse_active
        note += "\n" + LIVE_BROWSE_FLOW + content_rule_text(free) + flow_rule_text(free)
        note += "\n" + interaction_plan(self.config.security, self.config.flexibility)
        note = render_flexibility(note, self.config.flexibility)
        return note

    # ---- 原生 cron 模式 -----------------------------------------------

    async def enable(
        self,
        *,
        umo: str,
        apps: str | list[str] | tuple[str, ...] | None = None,
        cron: str | None = None,
        max_items: int = 0,
        prompt: str = "",
        sender_id: str = "",
    ) -> list[dict[str, Any]]:
        """创建（或重建）自主浏览任务。

        Args:
            umo: 目标会话 unified_msg_origin。
            apps: App 名列表或逗号分隔字符串；缺省用配置值。
            cron: 5 段 cron 表达式；缺省用配置值。
            max_items: 每次采集条数；0 表示用配置值。
            prompt: 附加提示词。
            sender_id: 创建者用户 ID（cron 唤醒时用于管理员判定）。

        Returns:
            新建任务信息列表。

        Raises:
            AutoBrowseError: 参数非法或 cron 管理器拒绝调度。
        """
        session = str(umo or "").strip()
        if not session:
            raise AutoBrowseError("缺少目标会话（unified_msg_origin），无法创建自主浏览任务")
        cron_expression = str(cron or self.config.auto.cron).strip()
        _cron_fields(cron_expression)
        app_names = self._normalize_apps(apps)
        items = int(max_items) if max_items and int(max_items) > 0 else self.config.auto.max_items

        if not self.available:
            started = self._start_fallback(
                session=session,
                apps=app_names,
                cron=cron_expression,
                max_items=items,
                prompt=prompt,
            )
            logger.warning("cron 管理器不可用，自主浏览已降级为 asyncio 后台任务")
            return started

        # 重建前先清掉这些 App 的旧任务，保证 /phone auto on 幂等。
        await self.disable(apps=app_names)
        created: list[dict[str, Any]] = []
        for app in app_names:
            profile = resolve_app(app)
            note = self._note(app, items, prompt)
            payload = {
                "session": session,
                "note": note,
                "origin": JOB_ORIGIN,
                "app": app,
                "max_items": items,
                "sender_id": str(sender_id or ""),
            }
            try:
                job = await self.cron_manager.add_active_job(
                    name=self._job_name(app),
                    cron_expression=cron_expression,
                    payload=payload,
                    description=note,
                )
            except Exception as exc:  # noqa: BLE001 - cron 管理器异常统一转成中文错误
                # add_active_job 可能已写入数据库但调度失败，清掉残留再报错。
                with suppress(Exception):
                    await self._delete_own_jobs(app=app)
                raise AutoBrowseError(f"创建自主浏览任务失败：{exc}") from exc
            created.append(
                {
                    "job_id": getattr(job, "job_id", ""),
                    "app": app,
                    "label": profile.label if profile else app,
                    "cron": cron_expression,
                    "name": getattr(job, "name", self._job_name(app)),
                }
            )
        logger.info(
            "自主浏览任务已创建：%s",
            "；".join(f"{item['label']} {item['cron']}" for item in created),
        )
        return created

    async def disable(
        self,
        job_id: str | None = None,
        *,
        apps: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        """删除自主浏览任务。

        Args:
            job_id: 指定任务 id；为空时删除全部（或指定 App 的）插件任务。
            apps: 只删除这些 App 的任务。

        Returns:
            实际删除的任务数。
        """
        if not self.available:
            removed = len(self._fallback_jobs_data)
            self._stop_fallback()
            return removed
        app_filter = {str(app).strip().lower() for app in (apps or ())}
        removed = 0
        for job in await self._list_own_jobs():
            if job_id and getattr(job, "job_id", "") != job_id:
                continue
            payload = getattr(job, "payload", None) or {}
            app = str(payload.get("app") or "").strip().lower()
            if app_filter and app not in app_filter:
                continue
            await self.cron_manager.delete_job(getattr(job, "job_id", ""))
            removed += 1
        return removed

    async def list_jobs(self) -> list[dict[str, Any]]:
        """列出插件创建的自主浏览任务。"""
        if not self.available:
            return [
                {
                    "job_id": "",
                    "name": self._job_name(app),
                    "app": app,
                    "label": app_label(app),
                    "cron": cron,
                    "session": session,
                    "enabled": True,
                    "next_run_time": "",
                    "mode": "fallback",
                }
                for app, cron, session in self._fallback_jobs()
            ]
        jobs: list[dict[str, Any]] = []
        for job in await self._list_own_jobs():
            payload = getattr(job, "payload", None) or {}
            app = str(payload.get("app") or "")
            next_run = getattr(job, "next_run_time", None)
            jobs.append(
                {
                    "job_id": getattr(job, "job_id", ""),
                    "name": getattr(job, "name", ""),
                    "app": app,
                    "label": app_label(app),
                    "cron": getattr(job, "cron_expression", ""),
                    "session": str(payload.get("session") or ""),
                    "enabled": bool(getattr(job, "enabled", True)),
                    "next_run_time": next_run.isoformat() if hasattr(next_run, "isoformat") else str(next_run or ""),
                    "mode": "cron",
                }
            )
        return jobs

    async def apply_config(
        self, *, default_umo: str = "", sender_id: str = ""
    ) -> list[dict[str, Any]] | None:
        """按配置同步自主浏览任务（插件启动时调用）。

        Args:
            default_umo: 配置未指定 session 时使用的会话。
            sender_id: 创建者用户 ID。

        Returns:
            新建的任务列表；配置未启用或缺少会话时返回 None。
        """
        auto = self.config.auto
        if not auto.enabled:
            return None
        session = auto.session or default_umo
        if not session:
            logger.warning("自主浏览已启用，但未配置会话（auto.session），本次跳过")
            return None
        return await self.enable(
            umo=session,
            apps=list(auto.app_list()),
            cron=auto.cron,
            max_items=auto.max_items,
            prompt=auto.prompt,
            sender_id=sender_id,
        )

    async def _delete_own_jobs(self, *, app: str | None = None) -> None:
        """删除插件自己的任务（可按 App 过滤），用于失败回滚。"""
        for job in await self._list_own_jobs():
            payload = getattr(job, "payload", None) or {}
            if app is not None and str(payload.get("app") or "") != app:
                continue
            with suppress(Exception):
                await self.cron_manager.delete_job(getattr(job, "job_id", ""))

    async def _list_own_jobs(self) -> list[Any]:
        if not self.available:
            return []
        try:
            jobs = await self.cron_manager.list_jobs("active_agent")
        except TypeError:
            jobs = await self.cron_manager.list_jobs()
        except Exception as exc:  # noqa: BLE001 - 列出失败不能让插件启动失败
            logger.warning("列出 cron 任务失败：%s", exc)
            return []
        result = []
        for job in jobs or []:
            payload = getattr(job, "payload", None)
            if isinstance(payload, dict) and payload.get("origin") == JOB_ORIGIN:
                result.append(job)
        return result

    def _normalize_apps(
        self, apps: str | list[str] | tuple[str, ...] | None
    ) -> list[str]:
        if apps is None or apps == "":
            raw_apps: list[str] = list(self.config.auto.app_list())
        elif isinstance(apps, str):
            raw_apps = [
                part.strip()
                for part in apps.replace("，", ",").split(",")
                if part.strip()
            ]
        else:
            raw_apps = [str(app).strip() for app in apps if str(app).strip()]
        if not raw_apps:
            raise AutoBrowseError(
                f"未指定 App，可用：{'/'.join(all_app_keys())} 或包名"
            )
        for app in raw_apps:
            if resolve_app(app) is None:
                raise AutoBrowseError(
                    f"未知 App：{app}，可用：{'/'.join(all_app_keys())} 或包名"
                )
        return raw_apps

    # ---- 降级模式：asyncio 后台任务 -----------------------------------

    def _start_fallback(
        self,
        *,
        session: str,
        apps: list[str],
        cron: str,
        max_items: int,
        prompt: str,
    ) -> list[dict[str, Any]]:
        self._stop_fallback()
        self._fallback_stop = asyncio.Event()
        if any(re.search(r"[A-Za-z]", field) for field in cron.split()):
            logger.warning(
                "降级自主浏览仅支持数字 cron 表达式（当前 %r），星期名等写法可能不会触发",
                cron,
            )
        self._fallback_jobs_data = [
            {
                "session": session,
                "app": app,
                "cron": cron,
                "max_items": max_items,
                "prompt": prompt,
            }
            for app in apps
        ]
        self._last_fallback_minute.clear()
        self._fallback_task = asyncio.create_task(
            self._fallback_loop(), name=f"{PLUGIN_NAME}_fallback_scheduler")
        return [
            {
                "job_id": "",
                "app": app,
                "label": app_label(app),
                "cron": cron,
                "name": self._job_name(app),
            }
            for app in apps
        ]

    def _fallback_jobs(self) -> list[tuple[str, str, str]]:
        return [
            (
                str(item["app"]),
                str(item["cron"]),
                str(item["session"]),
            )
            for item in self._fallback_jobs_data
        ]

    async def _fallback_loop(self) -> None:
        while not self._fallback_stop.is_set():
            now = datetime.now()
            minute_key = now.strftime("%Y%m%d%H%M")
            for item in self._fallback_jobs_data:
                app = str(item["app"])
                cron = str(item["cron"])
                if self._last_fallback_minute.get(app) == minute_key:
                    continue
                if not cron_matches(cron, now):
                    continue
                self._last_fallback_minute[app] = minute_key
                task = asyncio.create_task(
                    self._run_fallback_browse(item), name=f"{PLUGIN_NAME}_fallback_browse")
                self._fallback_runs.add(task)
                task.add_done_callback(self._fallback_runs.discard)
            try:
                await asyncio.wait_for(
                    self._fallback_stop.wait(), timeout=_FALLBACK_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                continue

    async def _run_fallback_browse(self, item: dict[str, Any]) -> None:
        if self.run_browse is None:
            logger.warning("降级自主浏览缺少浏览回调，本次跳过")
            return
        app = str(item["app"])
        session = str(item["session"])
        try:
            summary = await self.run_browse(app, session)
        except Exception as exc:  # noqa: BLE001 - 后台任务异常只记录
            logger.warning("降级自主浏览失败（%s）：%s", app, exc)
            return
        if not summary:
            return
        try:
            await self.context.send_message(session, MessageChain().message(summary))
        except Exception as exc:  # noqa: BLE001 - 主动消息失败只记录
            logger.warning("降级自主浏览结果发送失败：%s", exc)

    def _stop_fallback(self) -> None:
        task = self._fallback_task
        self._fallback_task = None
        if task is not None:
            task.cancel()
        current = asyncio.current_task()
        for task in self._fallback_runs:
            if task is not current:
                task.cancel()
        self._fallback_runs.clear()
        self._fallback_jobs_data = []
        self._fallback_stop.set()

    async def close(self) -> None:
        """停止备用调度器并等待其在途浏览任务退出。"""
        tasks = [self._fallback_task, *self._fallback_runs]
        self._stop_fallback()
        pending = [task for task in tasks if task is not None and task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# ----------------------------------------------------------------------
# 持续刷手机 / 插件任务管理（供 /phone continuous 与 /phone stop 使用）
# ----------------------------------------------------------------------

CONTINUOUS_JOB_NAME = "phone-continuous"
CONTINUOUS_JOB_ORIGIN = "phone_continuous"
PLUGIN_JOB_ORIGINS = frozenset(
    {
        JOB_ORIGIN,
        BACKGROUND_JOB_ORIGIN,
        "phone_play",
        "phone_random_play",
        CONTINUOUS_JOB_ORIGIN,
    }
)


async def create_continuous_task(
    cron_manager: Any,
    *,
    note: Any,
    target_tokens: int = 0,
    apps: list[str] | None = None,
) -> Any:
    """创建一轮静默「持续刷手机」run_once 主动任务。"""
    if cron_manager is None:
        raise BackgroundTaskError("cron 管理器不可用，无法创建持续刷任务")
    text = str(note or "").strip()
    if not text:
        raise BackgroundTaskError("持续刷任务提示词为空")
    payload = {
        "session": "",
        "note": text,
        "origin": CONTINUOUS_JOB_ORIGIN,
        "play_task": True,
        "target_tokens": max(0, int(target_tokens or 0)),
        "apps": list(apps or []),
    }
    try:
        return await cron_manager.add_active_job(
            name=CONTINUOUS_JOB_NAME,
            cron_expression=None,
            payload=payload,
            description="持续刷手机（一轮）",
            run_once=True,
            run_at=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
    except Exception as exc:  # noqa: BLE001 - cron 异常统一转成可读错误
        raise BackgroundTaskError(f"创建持续刷手机任务失败：{exc}") from exc


def _job_origin(job: Any) -> str:
    payload = getattr(job, "payload", None) or {}
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("origin") or "")


async def list_plugin_jobs(cron_manager: Any) -> list[dict[str, Any]]:
    """列出本插件创建的 active_agent 任务（含自主浏览/后台/play/随机/持续刷）。"""
    if cron_manager is None:
        raise BackgroundTaskError("cron 管理器不可用，无法查询任务")
    try:
        jobs = await cron_manager.list_jobs("active_agent")
    except Exception as exc:  # noqa: BLE001
        raise BackgroundTaskError(f"查询插件任务失败：{exc}") from exc
    result: list[dict[str, Any]] = []
    for job in jobs or []:
        origin = _job_origin(job)
        if origin not in PLUGIN_JOB_ORIGINS:
            continue
        payload = getattr(job, "payload", None) or {}
        result.append(
            {
                "job_id": str(getattr(job, "job_id", "") or ""),
                "name": str(getattr(job, "name", "") or ""),
                "origin": origin,
                "status": str(getattr(job, "status", "") or ""),
                "created_at": _job_time_text(getattr(job, "created_at", None)),
                "next_run_time": _job_time_text(getattr(job, "next_run_time", None)),
                "summary": str(
                    getattr(job, "description", "")
                    or (payload.get("instruction") if isinstance(payload, dict) else "")
                    or (payload.get("note") if isinstance(payload, dict) else "")
                    or ""
                )[:80],
            }
        )
    return result


async def delete_plugin_jobs(
    cron_manager: Any, *, job_id: str = ""
) -> list[str]:
    """删除插件任务；job_id 为空时删除全部，返回被删除的 job_id 列表。"""
    if cron_manager is None:
        raise BackgroundTaskError("cron 管理器不可用，无法停止任务")
    target = str(job_id or "").strip()
    try:
        jobs = await list_plugin_jobs(cron_manager)
    except BackgroundTaskError:
        raise
    deleted: list[str] = []
    for job in jobs:
        current = str(job.get("job_id") or "")
        if not current:
            continue
        if target and current != target and not current.startswith(target):
            continue
        try:
            await cron_manager.delete_job(current)
        except Exception as exc:  # noqa: BLE001 - 单个失败不阻塞其它
            logger.warning("删除插件任务 %s 失败：%s", current, exc)
            continue
        deleted.append(current)
    return deleted


async def continuous_job_running(cron_manager: Any) -> str:
    """返回当前存在的持续刷任务 job_id（没有则空串）。"""
    if cron_manager is None:
        return ""
    try:
        jobs = await cron_manager.list_jobs("active_agent")
    except Exception:  # noqa: BLE001 - 查询失败按无任务处理
        return ""
    for job in jobs or []:
        if _job_origin(job) == CONTINUOUS_JOB_ORIGIN:
            return str(getattr(job, "job_id", "") or "")
    return ""
