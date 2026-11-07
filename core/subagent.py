"""手机子Agent的请求工具隔离与委托状态。"""

from __future__ import annotations

from typing import Any
from .result_delivery import DELIVERY_GUIDANCE
from .task_records import BackgroundTaskRegistry

DISPATCH_TOOL = 'phone_start_background_task'
SUBAGENT_EXTRA = 'work_as_subagent'


def is_user_subagent(event: Any) -> bool:
    payload = getattr(event, 'get_extra', lambda _: None)('cron_payload') or {}
    return isinstance(payload, dict) and payload.get('origin') == 'phone_background_task' and bool(payload.get(SUBAGENT_EXTRA))


def foreground_delegates(service: Any, event: Any) -> bool:
    """显式开启且属于普通会话时，将手机工作交给独立Agent。"""
    return bool(service.config.model.work_as_subagent) and not service.is_task_session(event)


def route_request(service: Any, event: Any, request: Any) -> bool:
    """只替换当前请求的ToolSet，保持宿主全局工具集合。"""
    from astrbot.core.agent.tool import ToolSet

    child = is_user_subagent(event)
    delegate = foreground_delegates(service, event)
    synthetic = service.is_task_session(event) and event.get_platform_name() == 'cron'
    if not child and not delegate and not synthetic:
        return False
    if delegate:
        event.set_extra('phone_output_candidate', False)
        event.set_extra('phone_quiet_run', False)
        event.set_extra('phone_shared_current_run', False)
    tool_set = getattr(request, 'func_tool', None)
    if tool_set is not None:
        tools = list(tool_set.tools)
        if child:
            tools = [t for t in tools if t.name.startswith('phone_') and t.name not in {DISPATCH_TOOL, 'phone_task_template'}]
        elif synthetic:
            tools = [t for t in tools if t.name != 'send_message_to_user']
        else:
            tools = [t for t in tools if not t.name.startswith('phone_') or t.name in {DISPATCH_TOOL, 'phone_task_template'}]
        request.func_tool = ToolSet(tools=tools)
    parts = getattr(request, 'extra_user_content_parts', None)
    if parts is None:
        parts = []
        request.extra_user_content_parts = parts
    from astrbot.core.agent.message import TextPart

    marker = '【手机后台子Agent】'
    if not any(marker in str(getattr(p, 'text', p)) for p in parts):
        text = (
            '手机工作由当前独立Agent直接完成，按指令调用手机工具；不要再次委托。'
            '结果只用phone_share_to_user，target留空自动使用登记的share_target；切勿填写用户ID或cron会话。'
            if child else
            '当前为手机后台任务。结果通过phone_share_to_user按任务目标汇报；cron虚拟会话不能投递消息。'
            if synthetic else
            '手机操作请调用phone_start_background_task提交完整手机指令。它立即返回任务ID，'
            '独立Agent后台执行；你继续处理主任务及普通对话，无需等待或反复轮询。'
            '查询或取消用同一工具的action=status/list/cancel。'
        )
        if child or synthetic:
            text += DELIVERY_GUIDANCE
        parts.append(TextPart(text=marker + text).mark_as_temp())
    return delegate
