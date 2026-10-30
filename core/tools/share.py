"""主动分享工具：把内容链接/截图分享给用户或群。"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

from ..apps import app_label_for_package
from ..errors import PhoneControlError
from ..share_content import join_share_text, pick_share_link, plan_share

from .base import _PhoneToolBase, _wrap_call


@dataclass
class PhoneShareToUserTool(_PhoneToolBase):
    """把内容分享给用户/群（看与分享两重逻辑；链接优先、截图兜底、禁止纯文字）。"""

    name: str = "phone_share_to_user"
    description: str = (
        "把内容分享给用户/群。看和分享是两次独立判断：先决定是否点开/继续看"
        "（感兴趣只说明值得看，不触发分享），看完后再单独判断是否值得分享；"
        "不要因为感兴趣就同时看+分享。分享必须带内容链接或截图，禁止纯文字："
        "链接优先（App 分享按钮，或 phone_traffic 的「可分享链接」；工具会自动尝试"
        "从最近抓包提取）；复制链接后用 phone_clipboard 读取。"
        "content_type=post（默认）附图前自动回到帖子顶部并确认，不能直接复用中途截图；"
        "视频传 video、其他画面传 screen，保留当前画面。拿不到链接时自动附图。"
        "同一内容只发一次、短时间不要刷屏（建议每轮不超过 3 条，默认静默）；"
        "自刷/随机/持续刷对同一目标会话还有冷却（auto.share_cooldown_minutes，默认 30 分钟），"
        "冷却内会被拒绝，不要反复重试。"
        "自刷/随机/持续刷任务必须先 phone_recent_chat 看最近群聊/私聊在聊什么，"
        "再按对方喜好选择具体 target（不要用固定目标）；未先查聊天记忆会被拒绝。"
        "私聊只能分享给最近有交流的用户；白天时段外（默认 08:00–22:00 中国时区）"
        "为免打扰。用户直接任务里 target 留空=回到当前会话。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "content_type": {
                    "type": "string", "enum": ["post", "video", "screen"],
                    "default": "post",
                    "description": "post=帖子顶部截图（默认）；video=当前视频；screen=已确认的当前画面。",
                },
                "text": {
                    "type": "string",
                    "description": "发送前总结为 1–2 句、最多 100 字，只写内容和推荐理由；省略搜索/点按过程。链接另传 link。",
                    "default": "",
                },
                "link": {
                    "type": "string",
                    "description": (
                        "要附带的内容链接（优先）：来自分享按钮/phone_traffic 的可分享链接；"
                        "留空时工具会自动尝试从最近抓包提取，仍无则自动截图。"
                    ),
                    "default": "",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "description": (
                        "是否附带截图；即使为 false，拿不到链接时也会自动补当前内容截图。"
                    ),
                    "default": False,
                },
                "screenshot_path": {
                    "type": "string",
                    "description": "video/screen 可复用的截图路径；post 总是在回顶确认后重新截图。",
                    "default": "",
                },
                "target": {
                    "type": "string",
                    "description": (
                        "分享目标会话 UMO（从 phone_recent_chat 返回的可分享会话里选）："
                        "群聊或最近有交流的用户；自刷/随机/持续刷必填，用户直接任务可留空回到当前会话。"
                    ),
                    "default": "",
                },
            },
            "required": [],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = await self._guard_light(context)
        text = str(kwargs.get("text") or "").strip()
        if len(text) > 100:
            return "error: 分享正文请先总结到 100 字以内，只保留内容要点和推荐理由，再发送一次。"
        link = str(kwargs.get("link") or "").strip()
        include_screenshot = bool(kwargs.get("include_screenshot", False))
        screenshot_path = str(kwargs.get("screenshot_path") or "").strip()
        content_type = str(kwargs.get("content_type") or "post").strip().lower()
        if content_type not in {"post", "video", "screen"}:
            return "error: content_type 必须是 post、video 或 screen"
        autonomous = self.service.is_autonomous_play(event)
        allowed, window = self.service.share_allowed_now()
        if autonomous and not allowed:
            return (
                f"error: 当前为分享免打扰时段（分享时段 {window}），自刷任务不要主动分享；"
                "保持静默收尾即可"
            )
        target = str(kwargs.get("target") or "").strip()
        if target:
            target_error = self.service.validate_share_target(target)
            if target_error:
                return f"error: {target_error}"
        elif autonomous:
            return (
                "error: 自刷/随机/持续刷分享必须自己选 target：先 phone_recent_chat 看最近"
                "群聊/私聊的聊天内容，按对方喜好选一个群或最近有交流的用户再分享"
            )
        else:
            payload = event.get_extra("cron_payload") or {}
            if self.service.task_origin(event) == "phone_background_task" and isinstance(payload, dict):
                target = str(payload.get("share_target") or "").strip()
            else:
                target = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not target:
                target = str(self.service.last_session() or "").strip()
            if not target:
                return (
                    "error: 没有可用的分享目标；请先 phone_recent_chat 选一个会话，"
                    "或让用户先在聊天里说句话以记录当前会话"
                )
        # 自刷/随机/持续刷的分享目标必须来自聊天记忆：要求本轮已成功调用
        # phone_recent_chat（该工具会在事件上打标），否则拒绝，避免无依据分享。
        chat_memory_checked = False
        try:
            chat_memory_checked = bool(event.get_extra("phone_chat_memory_checked"))
        except Exception:  # noqa: BLE001 - 鸭子类型事件没有 extra 时按未检查处理
            chat_memory_checked = False
        if autonomous and not chat_memory_checked:
            return (
                "error: 分享前必须先调用 phone_recent_chat 查看最近聊天记忆，再按群/人的"
                "喜好选择具体分享对象；不要用固定或无依据的目标"
            )
        # 同一目标会话的主动分享冷却：避免自刷任务反复给同一个人/群发内容刷屏。
        if autonomous:
            remaining = self.service.share_cooldown_remaining(target)
            if remaining > 0:
                minutes = max(1, (remaining + 59) // 60)
                return (
                    f"error: 距上次向该会话分享不足冷却时长（还需约 {minutes} 分钟，"
                    "间隔由 auto.share_cooldown_minutes 配置）；本轮不要反复尝试，"
                    "保持静默收尾即可。"
                )
        # 抓包优先：模型没给链接时，自动从最近 flow 提取可分享链接（分享按钮触发的
        # 分享接口响应通常带有 share_url/jump_url）。优先匹配当前前台 App 的流量，
        # 避免把其他 App 的链接误当成当前内容的分享链接。
        traffic_link = ""
        traffic_source = ""
        try:
            traffic = getattr(self.service, "traffic", None)
            if traffic is not None:
                app_hint = ""
                try:
                    package, _activity = await self.service.actions.foreground()
                    app_hint = app_label_for_package(str(package or ""))
                except Exception:  # noqa: BLE001 - 前台识别失败时不做 App 过滤
                    app_hint = ""
                flows = traffic.recent(limit=30, since_seconds=300)
                traffic_link, traffic_source = pick_share_link(
                    flows, now=time.time(), app_hint=app_hint
                )
        except Exception as exc:  # noqa: BLE001 - 抓包提取失败不影响截图兜底
            logger.debug("分享：从抓包提取分享链接失败：%s", exc)
        plan = plan_share(
            text=text,
            link=link,
            include_screenshot=include_screenshot,
            screenshot_path=screenshot_path,
            traffic_link=traffic_link,
            traffic_source=traffic_source,
        )
        if plan.error:
            return f"error: {plan.error}"
        chain = MessageChain()
        combined = join_share_text(plan.text, plan.link)
        if combined:
            chain.message(combined)
        shot_path = screenshot_path
        if plan.screenshot_required:
            # 复用已有截图但文件不存在/读不到时，回退为现截一张，不能因路径失效
            # 把分享降级成纯文字。
            use_existing = bool(content_type != "post" and plan.use_existing_screenshot and shot_path)
            if use_existing and not Path(shot_path).is_file():
                use_existing = False
            if use_existing:
                pass
            else:
                try:
                    if content_type == "post":
                        _data, shot_path, _meta = await self.service.capture_post_top()
                    else:
                        _data, shot_path, _meta = await self.service.capture()
                except PhoneControlError as exc:
                    return f"error: 截图失败：{exc}"
            chain.file_image(str(shot_path))
        star_context = getattr(
            getattr(context, "context", None), "context", None
        )
        if star_context is None or not hasattr(star_context, "send_message"):
            return "error: 当前上下文不支持主动发送消息"
        try:
            session = MessageSession.from_str(target)
        except Exception as exc:  # noqa: BLE001 - 非法 UMO 直接给出可操作提示
            return f"error: 分享目标不合法：{target}（{exc}）"
        try:
            sent = await star_context.send_message(session, chain)
        except Exception as exc:  # noqa: BLE001 - 发送失败不向上抛
            return f"error: 分享失败：{exc}"
        if not sent:
            return f"error: 分享失败：没有找到目标会话 {target}"
        if autonomous:
            self.service.record_share(target)
        if target == str(getattr(event, "unified_msg_origin", "") or "").strip():
            event.set_extra("phone_shared_current_run", True)
        forms: list[str] = []
        if plan.link:
            forms.append(
                "链接（手动提供）"
                if plan.link_source == "manual"
                else "链接（来自最近抓包）"
            )
        if plan.screenshot_required:
            forms.append("截图")
        result = f"已分享给用户（{target}）"
        if forms:
            result += "，含" + "、".join(forms)
        notes = [note for note in plan.notes if str(note).strip()]
        if not plan.link:
            notes.append("未获取到分享链接，已用截图代替，禁止纯文字分享")
        if notes:
            result += "\n" + "；".join(dict.fromkeys(notes))
        return result + "\n交付已完成；继续静默收尾，不重复介绍或再发已分享内容。"
