"""手机工具失败的主人通知：异步发送、按目标合并、限频和有界重试。"""

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass

from .identity import PLUGIN_NAME


@dataclass(frozen=True)
class ReportSettings:
    enabled: bool = True
    target: str = ""
    cooldown_seconds: int = 300


def result_text(result):
    if isinstance(result, str):
        return result
    content = (
        result.get("content", [])
        if isinstance(result, dict)
        else getattr(result, "content", [])
    )
    parts = []
    for item in content or []:
        text = (
            item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        )
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def result_failed(result):
    from .action_outcomes import outcome_of
    detail = outcome_of(result)
    if detail is not None and detail["status"] == "failed":
        return True
    if result is None:
        return True
    if isinstance(result, dict):
        if result.get("isError") or result.get("is_error") or result.get("ok") is False:
            return True
    elif getattr(result, "isError", False) or getattr(result, "is_error", False):
        return True
    return result_text(result).lstrip().lower().startswith("error:")


def experience_mismatch(result):
    """仅按工具产生的结构化分类识别经验失配，正文不参与分类。"""
    meta = result.get("_meta") if isinstance(result, dict) else getattr(result, "meta", None)
    detail = meta.get("phone_control") if isinstance(meta, dict) else None
    return isinstance(detail, dict) and detail.get("failure_kind") == "experience_mismatch"


def safe_summary(result):
    text = (result_text(result) or "工具未返回结果或返回错误状态")[:4096]
    text = text.split("【App 经验更新】", 1)[0]
    text = re.sub(r"https?://\S+", "[连接地址]", text)
    text = re.sub(
        r"(?i)\b(?:bearer\s+\S+|(?:sk|tskey)[-_][A-Za-z0-9_-]+)", "[凭据]", text
    )
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|password|token|secret)([\s\"':=]+)[^\s,}]+",
        r"\1\2[已隐藏]",
        text,
    )
    return " ".join(text.split())[:350]


def owner_targets(*, target, owners, origin, last_session):
    if target:
        return [target.strip()]
    seed = next(
        (
            umo
            for umo in (origin, last_session)
            if isinstance(umo, str)
            and len(umo.split(":", 2)) == 3
            and umo.split(":", 1)[0] != "cron"
        ),
        "",
    )
    if not seed:
        return []
    platform = seed.split(":", 1)[0]
    return list(
        dict.fromkeys(
            f"{platform}:FriendMessage:{owner}"
            for owner in owners
            if str(owner).strip()
        )
    )[:32]


class ToolErrorReporter:
    def __init__(
        self, settings, targets, send, log, *, clock=time.monotonic, batch_delay=0.4
    ):
        self.settings = settings
        self.targets = targets
        self.send = send
        self.log = log
        self.clock = clock
        self.batch_delay = batch_delay
        self.pending = {}
        self.last_sent = {}
        self.retry_at = {}
        self.attempts = {}
        self.wake = asyncio.Event()
        self.task = None
        self.closed = False
        self.sent = 0
        self.failed = 0
        self._missing_target_logged = False

    def status(self):
        return {
            "enabled": self.settings().enabled,
            "pending": sum(sum(v.values()) for v in self.pending.values()),
            "sent": self.sent,
            "failed": self.failed,
        }

    def submit(self, event, tool_name, result):
        if (
            self.closed
            or not self.settings().enabled
            or not str(tool_name).startswith("phone_")
            or not result_failed(result)
            or experience_mismatch(result)
            or (
                tool_name == "phone_share_to_user"
                and "会话分享不足冷却时长" in result_text(result)
            )
        ):
            return
        targets = self.targets(event)
        if not targets:
            if not self._missing_target_logged:
                self.log(
                    "工具错误汇报未发送：请配置 report.target，或配置 security.owner_ids 并保留实际平台会话"
                )
                self._missing_target_logged = True
            return
        self._missing_target_logged = False
        detail = (str(tool_name)[:80], safe_summary(result))
        for target in targets[:32]:
            bucket = self.pending.setdefault(target, {})
            key = (
                detail
                if detail in bucket or len(bucket) < 32
                else ("其他手机工具", "错误种类较多，详情请查看 AstrBot 日志")
            )
            bucket[key] = bucket.get(key, 0) + 1
        self.wake.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(
                self._run(), name=f"{PLUGIN_NAME}_tool_error_report"
            )

    async def _run(self):
        try:
            await asyncio.sleep(self.batch_delay)
            while self.pending and not self.closed:
                if not self.settings().enabled:
                    self.pending.clear()
                    return
                self.wake.clear()
                now = self.clock()
                due = {
                    target: max(
                        self.last_sent.get(target, float("-inf"))
                        + self.settings().cooldown_seconds,
                        self.retry_at.get(target, float("-inf")),
                    )
                    for target in self.pending
                }
                ready = [target for target in self.pending if due[target] <= now]
                if not ready:
                    try:
                        await asyncio.wait_for(
                            self.wake.wait(), max(0.01, min(due.values()) - now)
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                for target in ready:
                    if self.closed or not self.settings().enabled:
                        return
                    bucket = self.pending.pop(target)
                    total = sum(bucket.values())
                    lines = [f"手机工具异常汇报（{total} 次）"]
                    lines.extend(
                        f"• {name} ×{count}：{text}"
                        for (name, text), count in list(bucket.items())[:8]
                    )
                    if len(bucket) > 8:
                        lines.append(
                            f"另有 {len(bucket) - 8} 类错误，详情请查看 AstrBot 日志。"
                        )
                    lines.append(
                        "请检查相关模型、设备连接或插件配置；此次通知不代表工具已恢复。"
                    )
                    try:
                        sent = await asyncio.wait_for(
                            self.send(target, "\n".join(lines)), 10
                        )
                        if not sent:
                            raise RuntimeError("消息接口未确认送达")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.attempts[target] = self.attempts.get(target, 0) + 1
                        self.failed += 1
                        self.log(
                            f"工具错误汇报发送失败（第 {self.attempts[target]} 次，{type(exc).__name__}）"
                        )
                        if self.attempts[target] < 3:
                            active = self.pending.setdefault(target, {})
                            for key, count in bucket.items():
                                active[key] = active.get(key, 0) + count
                            self.retry_at[target] = self.clock() + (
                                5 if self.attempts[target] == 1 else 30
                            )
                        else:
                            self.last_sent[target] = self.clock()
                            self.attempts.pop(target, None)
                            self.retry_at.pop(target, None)
                            self.log("工具错误汇报本次重试已结束，请检查通知平台连接")
                        continue
                    self.sent += 1
                    self.last_sent[target] = self.clock()
                    self.attempts.pop(target, None)
                    self.retry_at.pop(target, None)
                    # 只保存有限数量目标的限频状态。
                    if len(self.last_sent) > 128:
                        self.last_sent.pop(next(iter(self.last_sent)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(
                f"工具错误汇报任务失败（{type(exc).__name__}），后续工具错误将重新安排汇报"
            )

    async def close(self):
        self.closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.pending.clear()
