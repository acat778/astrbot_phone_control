"""人机验证辅助与手动控制工具：滑块/顺序点击处理，以及两阶段识图所需的提示词、JSON 解析与结果概括辅助。"""

from __future__ import annotations

from ..failover_adapter import generate

import asyncio
import json
import math
import re
from typing import Any

import mcp
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .. import verification as verification_mod

from .base import (
    _PhoneToolBase,
    _as_int,
    _event_of,
    _image_result,
    _normalize_points,
    _result_text,
    _wrap_call,
)
from .perception import _record_internal_usage


_VERIFY_ACTIONS = ("capture", "drag", "tap_points", "slide")


@dataclass
class PhoneBypassVerifyTool(_PhoneToolBase):
    """一次手势完成滑块/顺序点击等人机验证。"""

    name: str = "phone_bypass_verify"
    description: str = (
        "处理人机验证/滑块/顺序点击验证，尽量一次调用完成：capture=快速截图（不跑 "
        "uiautomator dump，约 0.6s，用来看验证码/拼图形式）；drag=按坐标拖拽一次完成"
        "（滑块、拼图、拖拽类验证）；tap_points=按顺序一次点击多个坐标（顺序点击/点选"
        "文字类验证）；slide=自动在当前界面找滑块并拖到最右（简单滑块）。遇到滑块/顺序"
        "点击/识图验证优先用本工具：先 capture 看清验证形式，再选 drag/tap_points/slide "
        "一次解决，不要反复 phone_screen(mode=ui) 空等。复杂验证优先 "
        "phone_solve_verification，本工具做手动精细控制。首次写操作需界面存在验证提示；"
        "纯图片验证先用 phone_solve_verification 识别。普通按钮使用 phone_tap/phone_swipe。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_VERIFY_ACTIONS),
                    "description": (
                        "capture=快速截图 / drag=坐标拖拽 / "
                        "tap_points=顺序点击 / slide=自动找滑块拖到最右。"
                    ),
                },
                "x1": {
                    "type": "integer",
                    "description": "起点/点击横坐标（drag/tap_points 用）。",
                    "default": 0,
                },
                "y1": {
                    "type": "integer",
                    "description": "起点/点击纵坐标（drag/tap_points 用）。",
                    "default": 0,
                },
                "x2": {
                    "type": "integer",
                    "description": "终点横坐标（drag 用）。",
                    "default": 0,
                },
                "y2": {
                    "type": "integer",
                    "description": "终点纵坐标（drag 用）。",
                    "default": 0,
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "拖拽时长（毫秒），默认 400。",
                    "default": 400,
                },
                "points": {
                    "type": "array",
                    "description": "tap_points 的顺序点击坐标列表。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "横坐标。"},
                            "y": {"type": "integer", "description": "纵坐标。"},
                        },
                        "required": ["x", "y"],
                    },
                    "default": [],
                },
                "interval_ms": {
                    "type": "integer",
                    "description": "tap_points 两次点击的间隔（毫秒），默认 250。",
                    "default": 250,
                },
                "include_image": {
                    "type": "boolean",
                    "description": "是否把操作后（或 capture）的截图回传给模型，默认 true。",
                    "default": True,
                },
            },
            "required": ["action"],
        }
    )

    @_wrap_call
    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        event = await self._guard(context)
        action = str(kwargs.get("action") or "").strip().lower()
        controller = getattr(self.service, "verification", None)
        if controller is None or action == "capture" or action not in _VERIFY_ACTIONS:
            return await self._bypass_impl(context, **kwargs)
        if action == "tap_points" and not _normalize_points(kwargs.get("points")):
            return "error: tap_points 需要有效的顺序点击坐标列表；未开始验证尝试"
        if not getattr(controller, "active", False):
            # 验证会话的首次写入以当前页面证据为依据，避免普通点击占用验证控制器。
            try:
                text, _ = await asyncio.wait_for(self.service.screen(max_nodes=80), 12)
            except Exception as exc:
                return f"error: 验证页面观察失败（{type(exc).__name__}），未开始验证尝试；请先截图确认"
            if not verification_mod.challenge_present(text):
                return (
                    "error: 当前页面未检测到验证提示，未执行验证动作或建立验证会话；"
                    "普通按钮请使用 phone_tap/phone_swipe，纯图片验证请先用 "
                    "phone_solve_verification(mode=auto) 识别"
                )
        allowed, reason = await controller.reserve_attempt(event, kind="bypass")
        if not allowed:
            return reason
        result = await self._bypass_impl(context, **kwargs)
        await controller.finish_attempt(event, passed=False, reason="bypass")
        text = _result_text(result)
        if controller.paused:
            return f"{text}\n\n{controller.status_text()}"
        state = controller.state or {}
        limit = controller._limits()
        return (
            f"{text}\n\n（验证自动尝试 {int(state.get('attempts') or 0)}/"
            f"{int(limit.get('max_attempts') or 0)}；会话 {state.get('id')}；"
            "未确认通过前不要重复点击）"
        )

    async def _bypass_impl(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        event = _event_of(context)
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _VERIFY_ACTIONS:
            return (
                f"error: 不支持的 action {action!r}，只能是 "
                "capture / drag / tap_points / slide"
            )
        points = _normalize_points(kwargs.get("points"))
        if action == "tap_points" and not points:
            return (
                "error: tap_points 需要 points（顺序点击的坐标列表，"
                '如 [{"x": 100, "y": 200}, {"x": 300, "y": 400}]）'
            )
        include_image = bool(kwargs.get("include_image", True))
        text, image, mime = await self.service.bypass_verify(
            action=action,
            x1=_as_int(kwargs.get("x1"), 0),
            y1=_as_int(kwargs.get("y1"), 0),
            x2=_as_int(kwargs.get("x2"), 0),
            y2=_as_int(kwargs.get("y2"), 0),
            duration_ms=_as_int(kwargs.get("duration_ms"), 400),
            points=points,
            interval_ms=_as_int(kwargs.get("interval_ms"), 250),
            include_image=include_image,
        )
        return _image_result(text, image if include_image else None, mime)


_CAPTCHA_TYPES = ("slider", "sequence", "image_click", "puzzle", "none")


_CAPTCHA_TYPE_ALIASES = {
    "滑块": "slider",
    "滑动": "slider",
    "slide": "slider",
    "顺序": "sequence",
    "顺序点击": "sequence",
    "点击": "sequence",
    "识图": "image_click",
    "点选": "image_click",
    "图片": "image_click",
    "拼图": "puzzle",
    "拖拽": "puzzle",
    "无": "none",
    "没有": "none",
}


_VERIFY_MODES = ("auto", "captcha", "quiz")


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


_SOLVE_FIRST_PROMPT = (
    "你是手机屏幕视觉分析助手。仔细观察这张手机截图，只返回一个 JSON 对象，不要输出"
    "任何其他内容，格式：\n"
    '{"content":"图片内容摘要（不超过80字）","is_captcha":true/false,'
    '"captcha_type":"slider|sequence|image_click|puzzle|none","is_quiz":true/false}\n'
    "判断规则：普通文章、评论编辑框、发送/确认/安全按钮、视频播放器都属于普通页面。必须看到明确挑战题面或专用验证组件，才能判为验证；不能仅凭按钮文字或颜色猜测。需要滑动滑块、拖动拼图、按顺序点击文字/图标、按提示点选图片的，"
    "is_captcha=true 且 captcha_type 取对应类型（slider=滑块，sequence=按顺序点击，"
    "image_click=按提示点选图片，puzzle=拼图，判断不了写 none）；出现题目/选择题/考试/"
    "测试/问卷需要作答的，is_quiz=true；普通页面两者都为 false。"
)


def _solve_second_prompt(kind: str, captcha_type: str = "") -> str:
    """第二段识图提示词：要求视觉模型给出可执行操作序列与最终确认点。"""
    review = (
        "先独立复核这张新截图是否确实有验证码或待作答题目，不预设之前的分类正确。"
        "普通文章、评论输入框、发送/确认按钮、视频播放器均不是验证或做题。"
        "challenge_confirmed 必须为布尔值；有清楚挑战才填 true；普通页填 false、ops=[]、confirm=null。"
        "无法确认时省略 challenge_confirmed，ops=[]，禁止凭空编造挑战或点击。\n"
    )
    if kind == "quiz":
        return review + (
            "你是手机屏幕解题助手。这是刚重新截取的手机屏幕，请解答屏幕上的题目/测试，"
            "只返回一个 JSON 对象，不要输出其他内容，格式：\n"
            '{"challenge_confirmed":true或false,"ops":[{"action":"tap","x":整数,"y":整数}],"confirm":{"x":整数,"y":整数}或null,'
            '"answer":"选择的选项文本","reason":"不超过60字的理由"}\n'
            "要求：answer 写你选择的选项文本；ops 是按顺序点击选项的坐标（手机屏幕像素"
            "坐标，左上角为原点）；confirm 是作答后点击「下一题/确认/提交」按钮的坐标，"
            "没有就填 null；只给出真正需要点击的坐标，不确定就不要乱点。"
        )
    type_hint = {
        "slider": (
            "若确认是滑块验证：ops 用 {\"action\":\"drag\",\"x1\":整数,\"y1\":整数,"
            "\"x2\":整数,\"y2\":整数,\"duration_ms\":整数} 把滑块拖到目标位置。"
        ),
        "sequence": (
            "若确认是顺序点击验证：按提示顺序逐次点击，每次一个 {\"action\":\"tap\",...}。"
        ),
        "image_click": (
            "若确认是按提示点选图片/文字验证：按提示顺序逐次点击目标，每次一个 "
            "{\"action\":\"tap\",...}。"
        ),
        "puzzle": "若确认是拼图验证：用 drag 把拼图块拖到缺口位置。",
    }.get(captcha_type, "若确认是人机验证：按验证要求给出 tap 或 drag 操作。")
    return review + (
        "你是手机屏幕验证码求解助手。这是刚重新截取的手机屏幕，请先确认是否存在挑战，再给出完成它的"
        "操作。只返回一个 JSON 对象，不要输出其他内容，格式：\n"
         '{"challenge_confirmed":true或false,"ops":[{"action":"tap","x":整数,"y":整数}或{"action":"drag","x1":整数,'
        '"y1":整数,"x2":整数,"y2":整数,"duration_ms":整数}],'
        '"confirm":{"x":整数,"y":整数}或null,"note":"不超过60字的说明"}\n'
        f"{type_hint}\n"
        "要求：坐标基于手机屏幕像素，左上角为原点；ops 按执行顺序排列；confirm 是操作"
        "完成后需要点击的「确认/提交/完成」按钮坐标，没有就填 null；只返回 JSON 对象本身。"
    )


def _json_object_end(text: str, start: int) -> int:
    """从 ``text[start] == '{'`` 起找配对的右花括号（跳过字符串内的括号），失败返回 -1。"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth <= 0:
                return index
    return -1


def _parse_json_object(candidate: str) -> dict[str, Any] | None:
    """尝试把字符串解析成 JSON 对象；不是对象或解析失败时返回 None。"""
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里提取 JSON 对象，容忍 ```json 代码块与前后说明文字。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    candidates.extend(
        match.group(1).strip()
        for match in _JSON_BLOCK_RE.finditer(raw)
        if match.group(1).strip()
    )
    for candidate in candidates:
        parsed = _parse_json_object(candidate)
        if parsed is not None:
            return parsed
    # 兜底：从每个左花括号起点按括号配平截取，取第一段可解析的对象
    for start, char in enumerate(raw):
        if char != "{":
            continue
        end = _json_object_end(raw, start)
        if end < 0:
            continue
        parsed = _parse_json_object(raw[start : end + 1])
        if parsed is not None:
            return parsed
    return None


def _verify_timeout_seconds(service: Any, requested: Any = 0) -> float:
    """两阶段识图的单次超时。

    参数 ``requested`` 大于 0 时优先；否则取 ``perception.verify_timeout_seconds``，
    再回退 ``perception.ocr_timeout_seconds``，最后 30 秒。
    """
    try:
        value = float(requested or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return max(1.0, min(300.0, value))
    perception = getattr(getattr(service, "config", None), "perception", None)
    for attr in ("verify_timeout_seconds", "ocr_timeout_seconds"):
        try:
            seconds = float(getattr(perception, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return max(5.0, min(120.0, seconds))
    return 30.0


def _capture_mime(meta: Any) -> str:
    """按截图元信息里的 method/format 推断图片 MIME。"""
    info = meta if isinstance(meta, dict) else {}
    method = str(info.get("method") or info.get("format") or "png").strip().lower()
    return "image/jpeg" if method in {"jpeg", "jpg"} else "image/png"


def _vision_response_text(response: Any) -> str:
    """取 llm_generate 返回对象的文本（completion_text 优先，其次 result_chain）。"""
    text = str(getattr(response, "completion_text", "") or "").strip()
    if text:
        return text
    result_chain = getattr(response, "result_chain", None)
    if result_chain is None:
        return ""
    try:
        return str(result_chain.get_plain_text() or "").strip()
    except Exception:  # noqa: BLE001 - 结果链文本读取失败按空处理
        return ""


async def _vision_json(
    app_context: Any,
    provider_id: str,
    prompt: str,
    image_path: Any,
    timeout: float,
    *,
    service: Any = None,
    event: Any = None,
    source: str = "internal:vision",
) -> tuple[dict[str, Any] | None, str]:
    """调用视觉 Provider 并把返回文本解析成 JSON 对象，返回 ``(对象, 原始文本)``。

    超时抛 :class:`asyncio.TimeoutError`，其他调用异常原样上抛，由工具按阶段给出建议。
    传入 ``service`` 时按返回 usage 立即记账（内部识图调用不在 Agent 钩子内）。
    """
    response = await generate(
        service,
        "vision",
        app_context=app_context,
        timeout=max(1.0, float(timeout)),
        chat_provider_id=provider_id,
        prompt=prompt,
        image_urls=[str(image_path)],
    )
    text = _vision_response_text(response)
    _record_internal_usage(service, response, event, source, prompt, text, 1)
    return _extract_json(text), text


def _clean_solve_text(value: Any, limit: int = 160) -> str:
    """压缩模型返回的文本字段（合并空白）并按长度截断。"""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _as_bool(value: Any, default: bool = False) -> bool:
    """宽松解析布尔值（模型有时把 true/false 写成字符串或 0/1）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "是", "对"}:
        return True
    if text in {"false", "0", "no", "n", "否", "不", ""}:
        return False
    return default


def _normalize_captcha_type(value: Any) -> str:
    """把模型返回的验证码类型归一化为枚举值（支持中文/别名/带后缀描述）。"""
    text = str(value or "").strip().lower()
    if text in _CAPTCHA_TYPES:
        return text
    alias = _CAPTCHA_TYPE_ALIASES.get(text)
    if alias:
        return alias
    for keyword, mapped in _CAPTCHA_TYPE_ALIASES.items():
        if keyword in text:
            return mapped
    return "none"


def _normalize_verify_ops(value: Any) -> list[dict[str, Any]]:
    """把模型返回的 ops 归一化成 tap/drag 操作列表，丢弃坐标缺失/非法的项。"""
    if not isinstance(value, list):
        return []
    ops: list[dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "tap").strip().lower()
        if action in {"drag", "swipe"} and all(
            item.get(axis) is not None for axis in ("x1", "y1", "x2", "y2")
        ):
            x1 = _as_int(item.get("x1"), -1)
            y1 = _as_int(item.get("y1"), -1)
            x2 = _as_int(item.get("x2"), -1)
            y2 = _as_int(item.get("y2"), -1)
            if min(x1, y1, x2, y2) < 0:
                continue
            ops.append(
                {
                    "action": "drag",
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "duration_ms": max(
                        100, min(5000, _as_int(item.get("duration_ms"), 500))
                    ),
                }
            )
        elif action in {"tap", "click", "point"}:
            x = _as_int(item.get("x"), -1)
            y = _as_int(item.get("y"), -1)
            if min(x, y) < 0:
                continue
            ops.append({"action": "tap", "x": x, "y": y})
    return ops


def _normalize_confirm(value: Any) -> dict[str, int] | None:
    """把模型返回的 confirm 归一化为 ``{"x": int, "y": int}``；无效返回 None。"""
    if not isinstance(value, dict):
        return None
    x = _as_int(value.get("x"), -1)
    y = _as_int(value.get("y"), -1)
    if min(x, y) < 0:
        return None
    return {"x": x, "y": y}


@dataclass
class _SolveRun:
    """一次验证求解的模型可见结果与结构化判定（无控制器时判定为 None）。"""

    result: ToolExecResult
    outcome: Any = None
    observation: Any = None


def _append_text(result: ToolExecResult, text: str) -> ToolExecResult:
    """在工具结果尾部追加一段文本（保留截图等非文本内容）。"""
    value = str(text or "").strip()
    if not value:
        return result
    if isinstance(result, str):
        return f"{result}\n\n{value}"
    if isinstance(result, mcp.types.CallToolResult):
        result.content.append(mcp.types.TextContent(type="text", text=value))
        return result
    return f"{_result_text(result)}\n\n{value}"


def _attempt_remaining(controller: Any) -> float:
    """当前尝试剩余秒数；控制器未提供或读取失败时返回 inf（不限制）。"""
    getter = getattr(controller, "attempt_remaining_seconds", None)
    if not callable(getter):
        return math.inf
    try:
        remaining = float(getter())
    except Exception:  # noqa: BLE001 - 状态读取失败时按不限处理
        return math.inf
    return remaining if math.isfinite(remaining) else math.inf


def _attempt_summary(controller: Any) -> str:
    """渲染验证尝试与失败计数（读不到会话状态时返回空串）。"""
    state = getattr(controller, "state", None)
    if not isinstance(state, dict) or not state:
        return ""
    max_attempts = _as_int(state.get("max_attempts"), 0)
    limits_fn = getattr(controller, "_limits", None)
    if callable(limits_fn):
        try:
            max_attempts = _as_int((limits_fn() or {}).get("max_attempts"), max_attempts)
        except Exception:  # noqa: BLE001 - 限额读取失败回退会话内记录
            pass
    attempts = _as_int(state.get("attempts"), 0)
    failures = _as_int(state.get("failure_count"), 0)
    quiz_answered = _as_int(state.get("quiz_answered"), 0)
    parts = [
        f"尝试 {attempts}/{max_attempts}" if max_attempts else f"尝试 {attempts} 次"
    ]
    parts.append(f"失败 {failures} 次")
    if quiz_answered:
        parts.append(f"答题已作 {quiz_answered} 题")
    return "，".join(parts)


def _outcome_label(outcome: Any) -> str:
    """把结构化判定转成给模型看的中文结论。"""
    value = str(getattr(outcome, "outcome", outcome) or "")
    labels = {
        verification_mod.OUTCOME_CLEARED: "验证挑战已解除",
        verification_mod.OUTCOME_PROGRESS: "答题进度已前进",
        verification_mod.OUTCOME_FAILED: "本次未通过",
        verification_mod.OUTCOME_UNKNOWN: "无法确认结果",
        verification_mod.OUTCOME_NOT_CHALLENGE: "未检测到验证/答题",
    }
    return labels.get(value, "无法确认结果")


def _outcome_summary(controller: Any, outcome: Any) -> str:
    """生成「判定结论 + 尝试/失败计数」说明，供模型决定是否继续。"""
    value = str(getattr(outcome, "outcome", outcome) or "")
    parts = [f"判定：{_outcome_label(outcome)}"]
    question = _as_int(getattr(outcome, "question", 0), 0)
    if value == verification_mod.OUTCOME_PROGRESS and question > 0:
        parts.append(f"当前第 {question} 题")
    counts = _attempt_summary(controller)
    if counts:
        parts.append(f"验证{counts}")
    text = "；".join(parts) + "。"
    if value in {
        verification_mod.OUTCOME_CLEARED,
        verification_mod.OUTCOME_NOT_CHALLENGE,
    }:
        return text + "本次验证会话已正常关闭。"
    return text + "未确认通过前不要重复点击验证按钮。"
