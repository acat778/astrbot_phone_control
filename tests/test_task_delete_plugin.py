"""命令、工具、面板共用删除生命周期，保留用户会话权限。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_main_0130_features import FakeEvent, FakeToolContext, _make_plugin, _cleanup, plugin_main
from test_main_fake_enabled import _json
from test_subagent import configure


@pytest.mark.parametrize('entry', ['command', 'tool', 'dashboard'])
def test_delete_entries_clear_job_record_and_proof(tmp_path, monkeypatch, entry):
    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        parent = FakeEvent(); parent.stop_event = lambda: None
        try:
            key = json.loads(await tool.call(FakeToolContext(parent), instruction='观察页面'))['job_id']
            registry = plugin.service.background_tasks
            data = {'cron_payload': {'origin': 'phone_background_task'}, 'cron_job': {'id': key}}
            child = SimpleNamespace(get_extra=lambda k: data.get(k))
            registry.screenshot(child, b'evidence', 'image/png')
            proof = registry.records[key]['screenshots'][0]['id']
            # 其他用户或同用户不同会话均不能删除。
            for foreign in [FakeEvent(sender_id='foreign'), FakeEvent(umo='webchat:FriendMessage:foreign')]:
                denied = await tool.call(FakeToolContext(foreign), action='delete', job_id=key)
                assert denied.startswith('error:')
                assert key in registry.records
            if entry == 'tool':
                response = json.loads(await tool.call(FakeToolContext(parent), action='delete', job_id=key))
            elif entry == 'command':
                say = AsyncMock(); monkeypatch.setattr(plugin, '_say', say)
                priority = AsyncMock(); monkeypatch.setattr(plugin, '_enter_user_priority', priority)
                await plugin.phone_command(parent, 'tasks delete ' + key)
                assert '已删除后台任务 ' + key in say.call_args.args[1]
                assert '截图证据已清理' in say.call_args.args[1]
                response = {'status': 'deleted'}
                priority.assert_not_awaited()
            else:
                monkeypatch.setattr(plugin_main, 'request', SimpleNamespace(json=AsyncMock(return_value={'id': key})))
                response = _json(await plugin.dashboard_task_delete())
            assert response['status'] == 'deleted'
            assert key not in registry.records and key not in plugin.context.cron_manager.jobs
            assert registry.evidence(key, proof) is None
            assert not plugin.device_gate.busy
            # 已派发的迟到Agent不得恢复任务或占用设备。
            late = FakeEvent(umo='cron:OtherMessage:' + key, platform='cron', payload={'cron_payload': {'origin': 'phone_background_task'}})
            late.set_extra('cron_job', {'id': key})
            with pytest.raises(asyncio.CancelledError):
                await plugin.track_task_agent_begin(late)
            assert not registry.records
        finally:
            await _cleanup(plugin)
    asyncio.run(run())


def test_running_agent_is_awaited_before_delete_returns(tmp_path, monkeypatch):
    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        task = None
        try:
            key = json.loads(await tool.call(FakeToolContext(FakeEvent()), instruction='观察页面'))['job_id']
            job = plugin.context.cron_manager.jobs[key]
            child = FakeEvent(umo='cron:OtherMessage:' + key, platform='cron', payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': key})
            started, stopped = asyncio.Event(), asyncio.Event()
            async def agent():
                try:
                    await plugin.track_task_agent_begin(child)
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            task = asyncio.create_task(agent())
            await asyncio.wait_for(started.wait(), 2)
            result = json.loads(await tool.call(FakeToolContext(FakeEvent()), action='delete', job_id=key))
            assert result['status'] == 'deleted' and stopped.is_set() and task.done()
            assert key not in plugin.service.background_tasks.records
        finally:
            if task and not task.done():task.cancel()
            await _cleanup(plugin)
    asyncio.run(run())
