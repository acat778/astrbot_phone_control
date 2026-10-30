"""后台任务工具：提交独立手机Agent并查询、取消本人会话任务。"""

from __future__ import annotations

import inspect

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..config import UNCONFIGURED_MESSAGE
from ..share_policy import classify_session_umo
from ..errors import AdbNotConfiguredError
from ..scheduler import BackgroundTaskError, create_background_task, validate_background_instruction
from .base import _PermissionDenied, _PhoneToolBase, _error, _event_of, _json_text, _wrap_call, check_admin_permission


@dataclass
class PhoneStartBackgroundTaskTool(_PhoneToolBase):
    """手机Agent提交立即返回，任务在独立会话中执行。"""

    name: str = 'phone_start_background_task'
    description: str = (
        '提交手机子Agent后台执行，立即返回job_id；主Agent继续主任务，无需等待。'
        'action=start传instruction（1-500字），完成后向share_target（默认原会话）分享。'
        'action=list/status/cancel查询或取消本人的当前会话任务；status/cancel须job_id。'
    )
    parameters: dict = Field(default_factory=lambda: {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['start', 'list', 'status', 'cancel'], 'default': 'start'},
            'instruction': {'type': 'string', 'description': '完整手机指令，1-500字。'},
            'share_target': {'type': 'string', 'description': '结果汇报会话UMO；留空使用原会话。', 'default': ''},
            'job_id': {'type': 'string', 'description': '查询或取消时填写完整任务ID。'},
        },
    })

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        action = str(kwargs.get('action') or 'start').strip().lower()
        if action not in {'start', 'list', 'status', 'cancel'}:
            return 'error: action只能是start/list/status/cancel'
        if self.service.is_task_session(event):
            return 'error: 当前已是独立手机任务，请直接完成指令；任务管理由发起会话执行。'
        session = str(getattr(event, 'unified_msg_origin', '') or '')
        sender = str(event.get_sender_id() or '')
        manager = getattr(getattr(self.service, 'app_context', None), 'cron_manager', None)
        if manager is None:
            return 'error: cron管理器不可用，无法管理手机后台Agent'
        await self._refresh(manager)
        if action != 'start':
            return await self._manage(manager, action, str(kwargs.get('job_id') or ''), session, sender)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        target = str(kwargs.get('share_target') or '').strip() or session
        target_error = (
            self.service.validate_share_target(target) if target != session
            else ('' if classify_session_umo(target) in {'user', 'group'} else '当前会话不是可投递的群聊或私聊')
        )
        if target_error:
            return 'error: ' + target_error
        reason = self.service.background_start_guard(event)
        if reason:
            return reason if reason.startswith('error:') else 'error: ' + reason
        registry = self.service.background_tasks
        if config.model.work_as_subagent and registry.active():
            return 'error: 已有后台手机任务进行中，请先查询或取消；同一手机串行执行。'
        try:
            instruction = validate_background_instruction(kwargs.get('instruction'))
            job = await create_background_task(
                manager, instruction=instruction,
                share_target=target,
                sender_id=sender, source_session=session,
                work_as_subagent=config.model.work_as_subagent,
            )
        except BackgroundTaskError as exc:
            return _error(exc)
        registry.add(job, sender=sender, session=session)
        await self.service.apply_task_provider(job)
        submitted = getattr(self.service, 'background_submitted_hook', None)
        if callable(submitted):
            result = submitted(event)
            if inspect.isawaitable(result):
                await result
        return _json_text({
            'status': 'queued', 'job_id': str(job.job_id),
            'message': '独立手机Agent已接单，主任务可继续；完成后汇报，无需等待或持续轮询。',
        })

    async def _refresh(self, manager):
        ids = {str(j.job_id) for j in await manager.list_jobs('active_agent') or []}
        registry = self.service.background_tasks
        for job_id in registry.active():
            if job_id not in ids:
                registry.update(job_id, 'ended', '宿主任务已结束，未收到完成结果；请核对实际汇报。')

    async def _manage(self, manager, action, job_id, session, sender):
        registry = self.service.background_tasks
        owned = registry.owned(sender, session)
        if action == 'list':
            return _json_text({'tasks': owned})
        record = next((r for r in owned if r['job_id'] == job_id and job_id), None)
        if record is None:
            return 'error: 当前用户会话没有此后台手机任务，请核对完整job_id'
        if action == 'status':
            return _json_text(record)
        if record['status'] not in {'queued', 'running'}:
            return _json_text(record)
        cancel = getattr(self.service, 'background_cancel_hook', None)
        if not callable(cancel):
            return 'error: 后台Agent取消接口未就绪'
        registry.update(job_id, 'cancelled')
        try:
            await manager.delete_job(job_id)
        finally:
            await cancel(job_id)
        return _json_text({'job_id': job_id, 'status': 'cancelled'})
