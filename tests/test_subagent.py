"""主任务继续、独立手机会话、取消收尾与权限隔离。"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from astrbot.core.agent.tool import ToolSet

from test_main_0130_features import FakeEvent, FakeToolContext, PhoneConfig, _cleanup, _make_plugin


class Cron:
    def __init__(self):
        self.jobs = {}

    async def add_active_job(self, **kw):
        job = SimpleNamespace(job_id=f'job-{len(self.jobs)+1}', status='pending', **kw)
        self.jobs[job.job_id] = job
        return job

    async def list_jobs(self, *_):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.jobs.pop(job_id, None)


def configure(plugin, enabled=True):
    plugin.raw_config['model']['work_as_subagent'] = enabled
    plugin.context.cron_manager = Cron()
    return next(t for t in plugin.context.tools if t.name == 'phone_start_background_task')


def request(plugin):
    other = copy.copy(plugin.context.tools[0])
    other.name = 'other_task'
    return SimpleNamespace(func_tool=ToolSet(tools=[*plugin.context.tools, other]),
                           prompt='', system_prompt='', extra_user_content_parts=[])


def test_setting_defaults_off_and_parses_true():
    assert not PhoneConfig.from_mapping(None).model.work_as_subagent
    assert PhoneConfig.from_mapping({'model': {'work_as_subagent': 'true'}}).model.work_as_subagent


@pytest.mark.parametrize('enabled', [True, False])
def test_route_only_replaces_current_request_tools(tmp_path, monkeypatch, enabled):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        configure(plugin, enabled)
        event = FakeEvent(text='用手机看网页，然后继续我的计算任务')
        req = request(plugin)
        original = req.func_tool
        try:
            await plugin.inject_phone_guidance(event, req)
            names = [t.name for t in req.func_tool.tools]
            assert ('phone_tap' in names) is (not enabled)
            assert 'phone_start_background_task' in names and 'other_task' in names
            assert any(t.name == 'phone_tap' for t in original.tools)
            if enabled:
                assert not event.get_extra("phone_output_candidate")
                assert not event.get_extra("phone_quiet_run")
                assert not plugin.service.user_priority.active
                assert not plugin.device_gate.busy
                assert req.func_tool is not original
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_submit_returns_before_child_completes_and_parent_end_keeps_child(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        parent = FakeEvent(text='手机任务与主任务同时执行')
        hold = asyncio.Event()
        child_task = None
        try:
            result = json.loads(await asyncio.wait_for(tool.call(FakeToolContext(parent), instruction='看测试页面'), .5))
            assert result['status'] == 'queued'
            job = plugin.context.cron_manager.jobs[result['job_id']]
            assert job.payload['session'] == '' and job.payload['source_session'] == parent.unified_msg_origin
            child = FakeEvent(umo='cron:OtherMessage:' + job.job_id, platform='cron',
                              payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': job.job_id})

            async def run():
                await plugin.track_task_agent_begin(child)
                with plugin.device_gate.scope(child, purpose='phone_tap'):
                    await hold.wait()
                await plugin.on_task_agent_done(child, response=SimpleNamespace(completion_text='done'))

            child_task = asyncio.create_task(run())
            await asyncio.sleep(0)
            await plugin.on_task_agent_done(parent)
            assert not child_task.done() and plugin.device_gate.owner_umo == child.unified_msg_origin
            assert parent.unified_msg_origin not in plugin.service.user_priority.sessions
            assert not plugin.service.should_yield_to_user(child)
            req = request(plugin)
            await plugin.inject_phone_guidance(child, req)
            names = [t.name for t in req.func_tool.tools]
            assert 'phone_tap' in names and 'phone_start_background_task' not in names and 'other_task' not in names
            hold.set()
            await child_task
            status = json.loads(await tool.call(FakeToolContext(parent), action='status', job_id=job.job_id))
            assert status['status'] == 'completed' and status['result'] == 'done'
            assert not plugin.device_gate.busy
        finally:
            hold.set()
            if child_task is not None:
                await asyncio.gather(child_task, return_exceptions=True)
            await _cleanup(plugin)
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['cancel', 'unload', 'delete_error'])
def test_cancel_and_unload_reclaim_running_child(tmp_path, monkeypatch, mode):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        parent = FakeEvent()
        result = json.loads(await tool.call(FakeToolContext(parent), instruction='等待测试任务'))
        job = plugin.context.cron_manager.jobs[result['job_id']]
        child = FakeEvent(umo='cron:OtherMessage:' + job.job_id, platform='cron', payload={'cron_payload': job.payload})
        child.set_extra('cron_job', {'id': job.job_id})

        async def run():
            await plugin.track_task_agent_begin(child)
            with plugin.device_gate.scope(child, purpose='phone_tap'):
                await asyncio.Event().wait()

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        try:
            if mode == 'unload':
                await plugin.terminate()
            elif mode == 'delete_error':
                async def failed_delete(job_id):
                    raise RuntimeError('database unavailable')
                monkeypatch.setattr(plugin.context.cron_manager, 'delete_job', failed_delete)
                result = await tool.call(FakeToolContext(parent), action='cancel', job_id=job.job_id)
                assert 'database unavailable' in result
            else:
                result = json.loads(await tool.call(FakeToolContext(parent), action='cancel', job_id=job.job_id))
                assert result['status'] == 'cancelled'
            assert task.cancelled() and not plugin.device_gate.busy
            if mode != 'delete_error':
                assert not plugin.context.cron_manager.jobs
            assert not plugin._background_agent_events
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_cancel_before_agent_begin_and_management_scope(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        parent = FakeEvent()
        try:
            result = json.loads(await tool.call(FakeToolContext(parent), instruction='测试'))
            job = plugin.context.cron_manager.jobs[result['job_id']]
            foreign = FakeEvent(umo='webchat:FriendMessage:other')
            assert json.loads(await tool.call(FakeToolContext(foreign), action='list'))['tasks'] == []
            assert (await tool.call(FakeToolContext(foreign), action='cancel', job_id=job.job_id)).startswith('error:')
            assert (await tool.call(FakeToolContext(FakeEvent(role='member', sender_id='other')), instruction='test')).startswith('error:')
            await tool.call(FakeToolContext(parent), action='cancel', job_id=job.job_id)
            child = FakeEvent(umo='cron:OtherMessage:' + job.job_id, platform='cron', payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': job.job_id})
            with pytest.raises(asyncio.CancelledError):
                await plugin.track_task_agent_begin(child)
            assert not plugin.device_gate.busy
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_direct_tools_cannot_bypass_delegation_and_child_cannot_spawn(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        try:
            tap = next(t for t in plugin.context.tools if t.name == 'phone_tap')
            assert 'phone_start_background_task' in await tap.call(FakeToolContext(FakeEvent()), x=1, y=1)
            child = FakeEvent(platform='cron', payload={'cron_payload': {'origin': 'phone_background_task'}})
            assert '直接完成' in await tool.call(FakeToolContext(child), instruction='递归')
            assert not plugin.context.cron_manager.jobs and not plugin.device_gate.busy
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_user_subagent_preempts_autonomous_phone_work(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        auto_event = FakeEvent(umo='cron:OtherMessage:auto', platform='cron',
                               payload={'cron_payload': {'origin': 'phone_random_play'}})
        manager = plugin.context.cron_manager
        manager.jobs['auto'] = SimpleNamespace(job_id='auto', payload={'origin': 'phone_random_play'})

        async def auto_run():
            with plugin.device_gate.scope(auto_event, purpose='phone_tap'):
                await asyncio.Event().wait()

        auto_task = asyncio.create_task(auto_run())
        await asyncio.sleep(0)
        plugin._active_agent_tasks[auto_event.unified_msg_origin] = (auto_task, 'auto', 'phone_random_play')
        parent = FakeEvent()
        child = None
        try:
            result = json.loads(await tool.call(FakeToolContext(parent), instruction='用户手机工作'))
            job = manager.jobs[result['job_id']]
            child = FakeEvent(umo='cron:OtherMessage:' + job.job_id, platform='cron',
                              payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': job.job_id})
            await plugin.track_task_agent_begin(child)
            # 本测试直接调用钩子，撤除当前测试协程登记，保留实际取消的auto任务检查。
            plugin._active_agent_tasks.pop(child.unified_msg_origin, None)
            assert auto_task.cancelled()
            assert not plugin.service.should_yield_to_user(child)
            with plugin.device_gate.scope(child, purpose='phone_tap'):
                assert plugin.device_gate.owner_umo == child.unified_msg_origin
            await plugin.on_task_agent_done(child, response=SimpleNamespace(completion_text='done'))
            assert not plugin.service.user_priority.active
        finally:
            auto_task.cancel()
            await asyncio.gather(auto_task, return_exceptions=True)
            if child is not None:
                plugin._active_agent_tasks.pop(child.unified_msg_origin, None)
            await _cleanup(plugin)
    asyncio.run(scenario())


@pytest.mark.parametrize('platform,origin,filtered', [
    ('cron', 'phone_background_task', True),
    ('webchat', 'phone_background_task', False),
    ('cron', 'external_task', False),
])
def test_synthetic_phone_agent_uses_real_delivery_tool(tmp_path, monkeypatch, platform, origin, filtered):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        configure(plugin, False)
        module = __import__(plugin.__module__.rsplit('.', 1)[0] + '.core.subagent', fromlist=['route_request'])
        event = FakeEvent(platform=platform, payload={'cron_payload': {'origin': origin}})
        req = request(plugin)
        send = copy.copy(plugin.context.tools[0]); send.name = 'send_message_to_user'
        req.func_tool.add_tool(send)
        original = req.func_tool
        try:
            module.route_request(plugin.service, event, req)
            names = {t.name for t in req.func_tool.tools}
            assert ('send_message_to_user' not in names) is filtered
            assert 'phone_share_to_user' in names and 'other_task' in names
            assert original.get_tool('send_message_to_user') is not None
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())
