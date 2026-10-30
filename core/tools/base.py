"""工具层公共基础设施：结果/异常辅助、权限校验、设备门禁与异常包裹。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any

import mcp
from mcp.types import ContentBlock
from pydantic.dataclasses import dataclass
from astrbot.api import FunctionTool, logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..device_gate import ExecutionCancelled, GateError
from ..config import UNCONFIGURED_MESSAGE, PhoneConfig
from ..errors import AdbNotConfiguredError, PhoneControlError, UiTargetNotFoundError
from ..apps import app_label_for_package

# 操作前自动召回 App 经验的工具：这些工具会读取/切换页面，召回结果随结果返回。
_APP_RECALL_TOOLS = frozenset(
    {
        "phone_open_app",
        "phone_screen",
        "phone_read_screen",
        "phone_live_screen",
        "phone_check_inbox",
        "phone_content",
    }
)
# 失败会触发经验失效标记的工具（当前页面上的具体操作/导航）。
_APP_RECALL_FAILURE_TOOLS = frozenset(
    {
        "phone_tap",
        "phone_long_press",
        "phone_swipe",
        "phone_input_text",
        "phone_key",
        "phone_open_app",
        "phone_check_inbox",
        "phone_content",
    }
)
_APP_RECALL_STATE_KEY = "phone_app_recall_state"
_APP_RECALL_STALE_HINT = (
    "\n\n【App 经验更新】刚才召回的经验可能已失效（已记为可疑）："
    "这是内部经验维护提示，无需向用户汇报；"
    "请先以当前屏幕重新探索正确入口，成功后用 phone_app_remember 覆盖更新该 App/页面经验，"
    "不要继续按旧步骤重试。"
)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _result_text(result: Any) -> str:
    """把 ToolExecResult 转成可读文本（用于结果判定/追加提示）。"""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
    return str(result)


def _error(exc: BaseException) -> str:
    """把异常转成模型可读的错误文本。"""
    message = str(exc).strip() or exc.__class__.__name__
    return f"error: {message}"


def _novelty_bored_note(match: Any, *, break_rule: bool = False) -> str:
    """把「已看腻」的新鲜感匹配结果转成给模型的简短提示；未到阈值返回空串。"""
    if match is None or not getattr(match, "bored", False):
        return ""
    entry = getattr(match, "entry", None)
    label = str(getattr(entry, "label", "") or "").strip()
    fatigue = float(getattr(match, "fatigue", 0.0) or 0.0)
    subject = f"「{label}」" if label else "这类内容"
    return (
        f"新鲜感提示：{subject}你已看腻（疲劳 {fatigue:.0f}/100）；"
        + ("break_rule 已开启，阈值仅作参考，可以按真实意愿继续看。" if break_rule
           else "之后降低同类内容的打开/停留概率，优先换主题或换 App。")
    )


def _event_of(context: ContextWrapper[AstrAgentContext]) -> Any:
    """从工具调用上下文里取出 AstrMessageEvent。"""
    agent_context = getattr(context, "context", None)
    event = getattr(agent_context, "event", None)
    if event is None:
        raise RuntimeError("工具调用缺少消息事件上下文")
    return event


def _is_cron_event(event: Any) -> bool:
    """判断是否为 cron 主动任务合成的消息事件。"""
    if type(event).__name__ == "CronMessageEvent":
        return True
    try:
        return str(event.get_platform_name() or "") == "cron"
    except Exception:  # noqa: BLE001 - 事件类型可能是鸭子类型
        return False


def _agent_run_active(event: Any) -> bool:
    """事件是否处于 AstrBot Agent run 中（由插件 on_agent_begin/on_llm_request 标记）。

    直接调用工具（测试/外部 API）没有该标记：工具作用域退出时立即释放设备租约，
    避免后续不同事件被上一次调用的租约挡住；Agent run 内则保持租约到 run 结束。
    """
    try:
        return bool(event.get_extra("phone_agent_run_active"))
    except Exception:  # noqa: BLE001 - 鸭子类型事件
        return False


def _sender_id_of(event: Any) -> str:
    try:
        return str(event.get_sender_id() or "").strip()
    except Exception:  # noqa: BLE001 - 某些平台事件不实现 get_sender_id
        return ""


def check_admin_permission(event: Any, config: PhoneConfig) -> str | None:
    """校验调用者是否有权使用手机能力。

    优先级：cron 主动任务放行 → ``security.owner_ids``（非空时只认主人，AstrBot
    管理员也不放行）→ 未启用 require_admin 放行 → admin/白名单放行。

    Args:
        event: 触发本次工具调用的消息事件。
        config: 当前配置快照。

    Returns:
        ``None`` 表示放行；否则返回以 ``error:`` 开头的拒绝文本。
    """
    security = config.security
    if _is_cron_event(event):
        return None
    sender_id = _sender_id_of(event)
    if security.owner_ids:
        if sender_id and sender_id in security.owner_ids:
            return None
        return (
            "error: 手机操控仅插件主人可用，请在插件配置 security.owner_ids 添加你的"
            f"用户 ID（你的 ID 是 {sender_id or '未知'}）"
        )
    if not security.require_admin:
        return None
    if str(getattr(event, "role", "member")) == "admin":
        return None
    if sender_id and sender_id in security.allowed_user_ids:
        return None
    return (
        "error: 权限不足，手机操控仅允许管理员或白名单用户使用。"
        f"你的用户 ID 是 {sender_id or '未知'}；"
        "可把该 ID 加入 AstrBot 管理员列表，或写入插件配置 security.allowed_user_ids"
        "（也可配置 security.owner_ids 指定插件主人）。"
    )


def _ok(payload: str) -> str:
    return payload if payload else "ok"


def _append_text(result: Any, text: str) -> Any:
    """在工具结果末尾追加文本（兼容字符串与 MCP CallToolResult）。"""
    if not text:
        return result
    if isinstance(result, str):
        return result + text
    if isinstance(result, mcp.types.CallToolResult):
        result.content.append(mcp.types.TextContent(type="text", text=text))
        return result
    return result


def _image_result(text: str, data: bytes | None, mime: str) -> ToolExecResult:
    """组装「文本 + 可选图片」工具结果；图片 MIME 以服务层返回为准。"""
    text = _ok(text)
    if not data:
        return text
    content: list[ContentBlock] = [
        mcp.types.TextContent(type="text", text=text),
        mcp.types.ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mimeType=str(mime or "").strip() or "image/png",
        ),
    ]
    return mcp.types.CallToolResult(content=content)


def _normalize_points(value: Any) -> list[dict[str, int]] | None:
    """把 points 归一化成 ``[{"x": int, "y": int}]``；空值返回 None。"""
    if not isinstance(value, (list, tuple)):
        return None
    points: list[dict[str, int]] = []
    for item in value:
        if isinstance(item, dict):
            points.append(
                {"x": _as_int(item.get("x"), 0), "y": _as_int(item.get("y"), 0)}
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append({"x": _as_int(item[0], 0), "y": _as_int(item[1], 0)})
    return points or None


@dataclass
class _PhoneToolBase(FunctionTool[AstrAgentContext]):
    """所有手机工具的公共基类。"""

    service: Any = None

    async def _guard(self, context: ContextWrapper[AstrAgentContext]) -> Any:
        """返回事件；依次校验权限、ADB 配置、token 额度与玩手机预算。"""
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        allowed = getattr(self.service, "activity_allowed", None)
        if callable(allowed) and not allowed(event):
            raise _ToolNotice("自主活动当前不在允许时间段内，请结束本轮任务。")
        quota_error = self.service.quota_error_text()
        if quota_error:
            raise _ToolNotice(quota_error)
        if self.service.should_yield_to_user(event):
            raise _ToolNotice(
                "用户任务优先：AI 自刷/随机/后台玩手机已暂停，先让用户任务使用手机；"
                "请立即收尾停下，用户任务结束后会自动恢复自刷。"
            )
        budget = self.service.ensure_play_budget(event)
        if budget is not None and budget.target_tokens > 0 and budget.completed:
            raise _ToolNotice(
                "本轮玩手机预算已完成，请立即收尾汇报（可 phone_share_to_user 一次）"
            )
        return event

    async def _guard_light(self, context: ContextWrapper[AstrAgentContext]) -> Any:
        """只校验权限与 ADB 配置（不拦额度/预算）。

        用于 phone_learn 这类「收尾时才调用」的元工具：预算完成时模型仍必须能
        记录本轮学到的内容，不能被额度/预算提示挡回去。
        """
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        return event

    @contextlib.contextmanager
    def _gate_scope_after_reservation(self, event: Any):
        """预占尝试后重新进入设备作用域，使执行期限按当前状态重新计算。

        工具包装器在进入工具实现前创建 ``gate.scope``；验证工具在实现内部才
        预占尝试，此时作用域期限可能按「没有进行中的尝试」记为无限。预占后
        重新进入同一事件作用域（同一租约可重入），让后续设备动作按当前单次
        尝试/会话剩余期限复查。
        """
        gate = getattr(self.service, "gate", None)
        if gate is None or event is None:
            yield
            return
        with gate.scope(event, purpose=self.name, write=self._is_write_tool()):
            yield

    def _is_write_tool(self) -> bool:
        """该工具是否属于会改变设备状态的写操作（未知工具按写操作）。

        依据验证控制器登记的工具集合判断：验证工具与纯只读观察工具按读处理，
        其余按写处理，供统一设备门禁区分暂停/占用时的放行范围。可能切换 App、
        点击或输入的工具（如 phone_check_inbox）必须按写操作，不得登记为只读。
        """
        controller = getattr(self.service, "verification", None)
        if controller is None:
            return True
        verify_tools = getattr(controller, "_VERIFY_TOOLS", frozenset())
        read_only = getattr(controller, "_READ_ONLY_TOOLS", frozenset())
        return self.name not in verify_tools and self.name not in read_only

    # ---- 内容新鲜感/厌倦感 -------------------------------------------

    def _record_novelty(self, app: str, text: str, *, weight: float = 1.0) -> Any:
        """记录一次内容曝光；未启用/失败返回 None（不影响工具主流程）。"""
        record = getattr(self.service, "record_novelty", None)
        if not callable(record):
            return None
        try:
            return record(app or "", text or "", weight=weight)
        except Exception:  # noqa: BLE001 - 状态记录失败不影响工具结果
            return None

    def _novelty_bored_note(self, match: Any) -> str:
        """新鲜的「已看腻」提示文本。"""
        return _novelty_bored_note(match, break_rule=self.service.config.flexibility.interest_active)

    # ---- 操作前 App 经验召回 / 失败更新 ------------------------------

    def _app_recall_state(self, event: Any) -> dict[str, Any]:
        """当前 Agent run 内的经验召回状态（页面去重与失效标记用）。"""
        try:
            state = event.get_extra(_APP_RECALL_STATE_KEY)
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            state = None
        if not isinstance(state, dict):
            state = {"pages": [], "entries": []}
        return state

    def _save_app_recall_state(self, event: Any, state: dict[str, Any]) -> None:
        try:
            event.set_extra(_APP_RECALL_STATE_KEY, state)
        except Exception:  # noqa: BLE001 - 事件不支持 extra 时忽略
            pass

    async def _current_app_page(self, event: Any) -> tuple[str, str, str]:
        """返回当前前台 ``(package, activity, 屏幕文本摘要)``；取不到时为空串。"""
        service = self.service
        try:
            snapshot = service._cached_ui_snapshot()
        except Exception:  # noqa: BLE001 - 缓存读取失败按无快照处理
            snapshot = None
        if snapshot is not None and snapshot.package:
            texts: list[str] = []
            for node in snapshot.nodes[:40]:
                text = node.text or node.desc
                if text:
                    texts.append(str(text))
            return snapshot.package, snapshot.activity, " ".join(texts)[:400]
        # 截屏/OCR 类工具不会更新 UI 快照：直接查一次前台包名，保证可召回。
        try:
            package, activity = await service.actions.foreground()
        except Exception:  # noqa: BLE001 - 前台查询失败时跳过召回
            return "", "", ""
        return str(package or ""), str(activity or ""), ""

    def _target_app_package(self, kwargs: Any) -> str:
        """从工具参数里解析目标 App 包名（打开/进入 App 时优先于当前前台）。"""
        if not isinstance(kwargs, dict):
            return ""
        app = str(kwargs.get("app") or "").strip()
        if not app:
            return ""
        try:
            package, _label = self.service.resolve_app_target(app)
            return str(package or "")
        except Exception:  # noqa: BLE001 - 解析失败回退前台包名
            return ""

    def _local_recall_entries(self, package: str, limit: int) -> list[Any]:
        """语义召回不可用时的本地档案兜底（按更新时间倒序）。"""
        store = getattr(self.service, "app_memory", None)
        if store is None:
            return []
        try:
            entries = [
                entry for entry in store.entries() if entry.app == package
            ]
        except Exception:  # noqa: BLE001 - 档案读取失败按无经验处理
            return []
        return entries[: max(1, int(limit))]

    async def _pre_operation_recall(self, event: Any, kwargs: Any = None) -> str:
        """操作前按当前 App/页面召回经验；同一页面的重复工具调用只召回一次。"""
        if self.name not in _APP_RECALL_TOOLS or event is None:
            return ""
        try:
            prompt = self.service.config.prompt
        except Exception:  # noqa: BLE001 - 配置读取失败时跳过召回
            return ""
        if not prompt.app_memory_enabled or prompt.app_memory_limit <= 0:
            return ""
        reader = getattr(self.service, "app_recall_reader", None)
        package, activity, context = await self._current_app_page(event)
        # 打开/进入指定 App：目标包名比「刚启动时的当前前台」更可靠。
        if self.name in {"phone_open_app", "phone_check_inbox"}:
            target = self._target_app_package(kwargs)
            if target:
                if package != target:
                    activity, context = "", ""
                package = target
        if not package:
            return ""
        state = self._app_recall_state(event)
        page_key = f"{package}|{activity}" if activity else package
        if page_key in state["pages"]:
            return ""
        label = app_label_for_package(package) or package
        limit = min(3, max(1, int(prompt.app_memory_limit)))
        entries: list[Any] = []
        if reader is not None:
            try:
                entries = await asyncio.wait_for(
                    reader.select(
                        app_key=package,
                        label=label,
                        page_hint=activity,
                        context=context,
                        limit=limit,
                    ),
                    timeout=8.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 召回失败回退本地档案
                logger.debug("操作前 App 经验语义召回失败，回退档案：%s", exc)
                entries = []
        if not entries:
            entries = self._local_recall_entries(package, limit)
        state["pages"] = (list(state["pages"]) + [page_key])[-20:]
        if not entries:
            self._save_app_recall_state(event, state)
            return ""
        from ..app_recall import render_entries

        known = {(item["app"], item["page"]) for item in state["entries"]}
        state["entries"] = (
            list(state["entries"])
            + [
                {"app": entry.app, "page": entry.page}
                for entry in entries
                if (entry.app, entry.page) not in known
            ]
        )[-20:]
        self._save_app_recall_state(event, state)
        return render_entries(entries, limit=3, max_chars=600)

    def _mark_recall_failed(self, event: Any) -> str:
        """操作失败后把本轮召回过的经验累计失效；返回给模型的更新提示。"""
        if event is None:
            return ""
        state = self._app_recall_state(event)
        entries = state.get("entries") or []
        if not entries:
            return ""
        by_app: dict[str, list[str]] = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            app = str(item.get("app") or "").strip()
            if not app:
                continue
            by_app.setdefault(app, []).append(str(item.get("page") or ""))
        marked = False
        for app, pages in by_app.items():
            try:
                updated = self.service.experiences.mark_failed(app, pages)
            except Exception:  # noqa: BLE001 - 标记失败不影响工具结果
                updated = []
            if updated:
                marked = True
        # 本轮已命中的经验不再重复标记；后续失败重新召回后再累计。
        state["entries"] = []
        self._save_app_recall_state(event, state)
        return _APP_RECALL_STALE_HINT if marked else ""


class _PermissionDenied(Exception):
    """内部信号：权限门禁拒绝（由 :func:`_wrap_call` 转成文本）。"""


class _ToolNotice(Exception):
    """内部信号：额度/预算等提示（原样返回文本，不加 error: 前缀）。"""


def _gate_error_text(message: str) -> str:
    """门禁拒绝文案统一成 ``error:`` 开头（门禁自身可能已带该前缀）。"""
    text = str(message or "").strip() or "当前不能操作手机"
    return text if text.startswith("error:") else f"error: {text}"


def _wrap_call(handler):
    """给工具实现统一套上设备执行作用域、权限与异常处理。

    设备作用域在进入时校验鉴权/占用/暂停/期限，退出只清理上下文；门禁不存在
    （离线单测未装配门禁）时跳过作用域，工具按原有逻辑执行。
    """

    async def wrapper(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        target_missing = False
        try:
            event = _event_of(context)
        except Exception:  # noqa: BLE001 - 缺少事件时交给 handler 统一报错
            event = None
        gate = getattr(self.service, "gate", None)
        if self.name == "phone_start_background_task":
            gate = None
        elif event is not None:
            from ..subagent import foreground_delegates

            if foreground_delegates(self.service, event):
                denied = check_admin_permission(event, self.service.config)
                return denied or "error: 手机工作已设为后台子Agent，请用 phone_start_background_task 提交任务。"
        try:
            if gate is not None and event is not None:
                with gate.scope(
                    event, purpose=self.name, write=self._is_write_tool()
                ):
                    result = await handler(self, context, **kwargs)
            else:
                result = await handler(self, context, **kwargs)
        except ExecutionCancelled as exc:
            # 暂停/取消/代次变化不是工具错误：要求模型立即收尾，不加 error: 前缀
            detail = str(getattr(exc, "message", "") or exc).strip()
            return f"已中止：{detail}" if detail else "已中止：手机执行已被取消"
        except GateError as exc:
            return _gate_error_text(getattr(exc, "message", "") or str(exc))
        except _ToolNotice as exc:
            return str(exc)
        except _PermissionDenied as exc:
            return str(exc)
        except UiTargetNotFoundError as exc:
            target_missing = True
            result = _error(exc)
        except PhoneControlError as exc:
            return _error(exc)
        except asyncio.TimeoutError:
            return "error: 操作超时，设备可能无响应"
        except Exception as exc:  # noqa: BLE001 - 工具不允许把异常抛给 Agent 循环
            logger.warning("手机工具 %s 执行失败：%s", self.name, exc)
            return _error(exc)
        finally:
            # Agent run 外的直接工具调用：作用域结束即释放租约，避免阻塞后续事件。
            if gate is not None and event is not None and not _agent_run_active(event):
                try:
                    gate.release_event(event)
                except Exception:  # noqa: BLE001 - 释放失败由 stale 回收兜底
                    pass
        result_text = _result_text(result)
        if event is not None and hasattr(self.service, "experiences"):
            try:
                self.service.experiences.observe(event, self.name, kwargs, result_text)
                if self.name in {"phone_screen", "phone_read_screen"} and hasattr(self.service, "_contents"):
                    self.service.contents.observe(event, result_text)
            except Exception as exc:
                logger.warning("收集操作经验证据失败：%s", exc)
            # 操作失败：累计本轮召回经验的失败次数，连续失败标记可能失效。
            if (
                self.name in _APP_RECALL_FAILURE_TOOLS
                and result_text.startswith("error:")
            ):
                hint = ""
                try:
                    hint = self._mark_recall_failed(event)
                except Exception as exc:  # noqa: BLE001 - 失效标记失败不影响结果
                    logger.debug("App 经验失效标记失败：%s", exc)
                if hint:
                    result = _append_text(result, hint)
                    if target_missing:
                        result = mcp.types.CallToolResult(
                            content=[mcp.types.TextContent(type="text", text=result)],
                            isError=True,
                            _meta={"phone_control": {"failure_kind": "experience_mismatch"}},
                        )
            # 操作前召回：屏幕/切换 App 类工具返回当前 App/页面相关经验。
            elif self.name in _APP_RECALL_TOOLS and not result_text.startswith(
                ("error:", "已中止")
            ):
                try:
                    recall = await self._pre_operation_recall(event, kwargs)
                except Exception as exc:  # noqa: BLE001 - 召回失败不影响工具结果
                    logger.debug("操作前 App 经验召回失败：%s", exc)
                    recall = ""
                if recall:
                    result = _append_text(result, "\n\n" + recall)
        # 额度接近上限时在结果尾部追加软告警（不改变工具结果本身）
        try:
            warning = self.service.quota_warning_text()
        except Exception:  # noqa: BLE001 - 告警失败不影响结果
            warning = None
        if warning:
            if isinstance(result, str):
                result = f"{result}\n\n{warning}"
            elif isinstance(result, mcp.types.CallToolResult):
                result.content.append(
                    mcp.types.TextContent(type="text", text=warning)
                )
        return result

    return wrapper
