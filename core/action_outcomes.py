"""动作执行与可观察结果分开取证；观察只读且有界。"""

import asyncio
import json
import math
import time

from .errors import PhoneControlError

MARKER = "\n【动作结果】"
ACTION_TOOLS = frozenset({"phone_tap", "phone_long_press", "phone_swipe", "phone_input_text", "phone_key", "phone_open_app"})
EXPECT_PARAMETER = {"expect": {
    "type": "object", "description": "动作结果条件，全部满足才确认。text为本次新出现的目标页面/提交成功文本；absent_text须从有到无；changed_resource_id须同一唯一节点text变化。失败提示failure_text须本次新出现。观察失败仅标结果未确认，不重复动作。",
    "properties": {
        **{k: {"type": "string"} for k in ("text", "absent_text", "package", "activity", "changed_resource_id", "failure_text")},
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 10, "default": 3},
    }, "additionalProperties": False,
}}


def action_result(message, *, status="result_unconfirmed", execution="succeeded", reason="", condition=None):
    labels = {"execution_succeeded": "执行成功（结果已确认）", "result_unconfirmed": "结果未确认", "failed": "失败"}
    detail = {"status": status, "execution_status": execution,
              "result_status": "confirmed" if status == "execution_succeeded" else ("failed" if status == "failed" else "unconfirmed"),
              "reason": reason, "condition": condition}
    return message + "\n" + labels[status] + ("：" + reason if reason else "") + MARKER + json.dumps(detail, ensure_ascii=False)


def outcome_of(result):
    from .tool_reports import result_text
    text = result_text(result)
    if MARKER not in text:
        return None
    try:
        value = json.loads(text.rsplit(MARKER, 1)[1].split("\n\n", 1)[0])
    except (ValueError, TypeError):
        return None
    if isinstance(value, dict) and value.get("status") in {"execution_succeeded", "result_unconfirmed", "failed"}:
        return value
    return None


class ResultExpectation:
    def __init__(self, raw):
        self.condition = raw
        self.before = None
        self.error = ""
        if raw is None:
            return
        if not isinstance(raw, dict) or not raw or set(raw) - set(EXPECT_PARAMETER["expect"]["properties"]):
            raise PhoneControlError("expect须提供有效的结果条件对象")
        for key, value in raw.items():
            if key != "timeout_seconds" and (not isinstance(value, str) or not value.strip() or len(value) > 500):
                raise PhoneControlError("expect文本条件须为1-500字非空字符串")
        if not any(raw.get(k) for k in ("text", "absent_text", "package", "activity", "changed_resource_id")):
            raise PhoneControlError("expect须包含目标文本、页面或文本变化条件")
        value = raw.get("timeout_seconds", 3)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10:
            raise PhoneControlError("结果观察期限须为0-10秒")
        self.timeout = value

    async def prepare(self, service):
        if self.condition is None:
            return
        try:
            self.before = await asyncio.wait_for(service.screen(), 10)
            if not self.before[1].nodes:
                self.before = None
                self.error = "动作前节点为空，无法建立结果变化基线"
        except (PhoneControlError, asyncio.TimeoutError) as exc:
            self.error = "动作前观察不可用：" + (str(exc) or type(exc).__name__)

    @staticmethod
    def _contains(snapshot, text):
        return any(text in n.text or text in n.desc for n in snapshot.nodes)

    def _matches(self, before, after):
        c = self.condition
        if not after.nodes:
            return False
        if c.get("text") and (not self._contains(after, c["text"]) or self._contains(before, c["text"])):
            return False
        if c.get("absent_text") and (self._contains(after, c["absent_text"]) or not self._contains(before, c["absent_text"])):
            return False
        if c.get("package") and after.package != c["package"]:
            return False
        if c.get("activity") and after.activity != c["activity"]:
            return False
        if c.get("changed_resource_id"):
            old = [n.text for n in before.nodes if n.resource_id == c["changed_resource_id"]]
            new = [n.text for n in after.nodes if n.resource_id == c["changed_resource_id"]]
            if len(old) != 1 or len(new) != 1 or old == new:
                return False
        if not any(c.get(k) for k in ("text", "absent_text", "changed_resource_id")):
            return (before.package, before.activity) != (after.package, after.activity)
        return True

    async def finish(self, service, message):
        if self.condition is None:
            return action_result(message, reason="动作已执行；未提供结果条件，请先观察页面再决定下一步")
        if self.before is None:
            return action_result(message, reason=self.error, condition=self.condition)
        deadline = time.monotonic() + self.timeout
        before = self.before[1]
        while True:
            try:
                _, after = await asyncio.wait_for(service.screen(), max(.001, deadline - time.monotonic()))
            except (PhoneControlError, asyncio.TimeoutError) as exc:
                return action_result(message, reason="动作后观察未确认：" + (str(exc) or type(exc).__name__), condition=self.condition)
            failure = self.condition.get("failure_text")
            if failure and self._contains(after, failure) and not self._contains(before, failure):
                return action_result(message, status="failed", reason="出现指定失败提示：" + failure, condition=self.condition)
            if self._matches(before, after):
                return action_result(message, status="execution_succeeded", reason="本次动作前后观察满足全部结果条件", condition=self.condition)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return action_result(message, reason="观察期限内未取得满足全部条件的新证据；核对页面，勿盲目重试", condition=self.condition)
            await asyncio.sleep(min(.3, remaining))
