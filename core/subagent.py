"""手机子Agent的请求工具隔离与委托状态。"""

from __future__ import annotations

from typing import Any

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
    tool_set = getattr(request, 'func_tool', None)
    if tool_set is not None:
        tools = list(tool_set.tools)
        if child:
            tools = [t for t in tools if t.name.startswith('phone_') and t.name != DISPATCH_TOOL]
        elif synthetic:
            tools = [t for t in tools if t.name != 'send_message_to_user']
        else:
            tools = [t for t in tools if not t.name.startswith('phone_') or t.name == DISPATCH_TOOL]
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
            '结果只用phone_share_to_user发送到任务载荷share_target，切勿向cron会话发消息。'
            if child else
            '当前为手机后台任务。结果通过phone_share_to_user按任务目标汇报；cron虚拟会话不能投递消息。'
            if synthetic else
            '手机操作请调用phone_start_background_task提交完整手机指令。它立即返回任务ID，'
            '独立Agent后台执行；你继续处理主任务及普通对话，无需等待或反复轮询。'
            '查询或取消用同一工具的action=status/list/cancel。'
        )
        parts.append(TextPart(text=marker + text).mark_as_temp())
    return delegate


class BackgroundTaskRegistry:
    """当前插件实例的任务状态；保留最近100条已结束记录。"""

    def __init__(self):
        self.records: dict[str, dict[str, Any]] = {}

    def add(self, job, *, sender: str, session: str):
        self.records[str(job.job_id)] = {
            'job_id': str(job.job_id), 'sender': sender, 'session': session,
            'summary': str(job.description or '')[:100], 'status': 'queued', 'result': '',
        }
        finished = [key for key, item in self.records.items() if item['status'] not in {'queued', 'running'}]
        for key in finished[:-100]:
            self.records.pop(key, None)

    def update(self, job_id: str, status: str, result: str = ''):
        record = self.records.get(job_id)
        if record is not None and record['status'] != 'cancelled':
            record.update(status=status, result=result[:1000])

    def owned(self, sender: str, session: str):
        return [dict(r) for r in self.records.values() if r['sender'] == sender and r['session'] == session]

    def active(self):
        return [r['job_id'] for r in self.records.values() if r['status'] in {'queued', 'running'}]
