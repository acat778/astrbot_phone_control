"""原生任务管理入口、工具事件与持久化记录共用同一执行链。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_main_0130_features import FakeEvent, FakeToolContext, _cleanup, _make_plugin
from test_subagent import configure
from test_main_0130_features import plugin_main
from test_main_fake_enabled import _json


def test_native_submit_query_and_cancel_share_persistent_registry(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        parent = FakeEvent()
        parent.stop_event = lambda: None
        say = AsyncMock()
        monkeypatch.setattr(plugin, '_say', say)
        priority = AsyncMock()
        monkeypatch.setattr(plugin, '_enter_user_priority', priority)
        try:
            await plugin._cmd_task(parent, 'task 读取测试页面')
            saved = json.loads(say.call_args.args[1])
            key = saved['job_id']
            assert key in plugin.service.background_tasks.records
            await plugin.phone_command(parent, 'tasks status ' + key)
            priority.assert_not_awaited()
            assert '等待调度' in say.call_args.args[1]
            result = await tool.call(FakeToolContext(FakeEvent(umo='webchat:FriendMessage:foreign')), action='status', job_id=key)
            assert result.startswith('error:')
            await plugin.phone_command(parent, 'tasks cancel ' + key)
            assert plugin.service.background_tasks.records[key]['status'] == 'cancelled'
            assert not plugin.context.cron_manager.jobs
            assert not plugin.device_gate.busy
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_tool_progress_and_capture_are_readable_after_reload(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        try:
            submitted = json.loads(await tool.call(FakeToolContext(FakeEvent()), instruction='观察测试页面'))
            key = submitted['job_id']
            job = plugin.context.cron_manager.jobs[key]
            child = FakeEvent(umo='cron:OtherMessage:' + key, platform='cron', payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': key})
            plugin.service.background_tasks.update(key, 'running')
            screen = SimpleNamespace(name='phone_screen')
            await plugin.track_phone_tool_use(child, screen, {})
            assert plugin.service.background_tasks.records[key]['wait_reason'] == '等待工具返回'
            with plugin.device_gate.scope(child, purpose='phone_screen', write=False):
                plugin.service._save_task_screenshot(b'image-proof', tmp_path / 'shot.png')
            await plugin.track_phone_tool_result(child, screen, {}, 'error: 等待人工验证')
            from core.task_records import BackgroundTaskRegistry
            fresh = BackgroundTaskRegistry(plugin.data_dir / 'background_tasks.sqlite3')
            saved = fresh.records[key]
            assert saved['steps'] == 1 and '人工验证' in saved['wait_reason']
            assert fresh.evidence(key, saved['screenshots'][0]['id'])[1] == b'image-proof'
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_task_dashboard_pagination_and_evidence_binding(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        try:
            key = json.loads(await tool.call(FakeToolContext(FakeEvent()), instruction='观察测试页面'))['job_id']
            data = {'cron_payload': {'origin': 'phone_background_task'}, 'cron_job': {'id': key}}
            child = SimpleNamespace(get_extra=lambda k: data.get(k))
            plugin.service.background_tasks.screenshot(child, b'image-proof', 'image/png')
            monkeypatch.setattr(plugin_main, 'request', SimpleNamespace(query={'offset': '0', 'limit': '1'}))
            page = _json(await plugin.dashboard_tasks())
            assert page['total'] == 1 and page['tasks'][0]['job_id'] == key
            shot = page['tasks'][0]['screenshots'][0]
            monkeypatch.setattr(plugin_main, 'request', SimpleNamespace(query={'job_id': key, 'id': shot['id']}))
            assert _json(await plugin.dashboard_task_evidence())['data_url'].startswith('data:image/png;base64,')
            monkeypatch.setattr(plugin_main, 'request', SimpleNamespace(query={'job_id': 'foreign', 'id': shot['id']}))
            assert (await plugin.dashboard_task_evidence()).status_code == 404
            monkeypatch.setattr(plugin_main, 'request', SimpleNamespace(query={'offset': '-1'}))
            assert (await plugin.dashboard_tasks()).status_code == 400
            # 设备未配置时，持久化查询仍可用。
            plugin.raw_config['adb']['host'] = ''
            plugin.raw_config['adb']['port'] = 0
            parent = FakeEvent(); parent.stop_event = lambda: None
            say = AsyncMock(); monkeypatch.setattr(plugin, '_say', say)
            await plugin.phone_command(parent, 'tasks status ' + key)
            assert key in say.call_args.args[1]
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_agent_response_does_not_mark_failed_final_step_as_completed(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        try:
            key = json.loads(await tool.call(FakeToolContext(FakeEvent()), instruction='汇报测试结果'))['job_id']
            job = plugin.context.cron_manager.jobs[key]
            child = FakeEvent(umo='cron:OtherMessage:' + key, platform='cron', payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': key})
            plugin.service.background_tasks.update(key, 'running')
            await plugin.track_phone_tool_result(child, SimpleNamespace(name='phone_share_to_user'), {}, 'error: 截图超过帧龄，未发送')
            monkeypatch.setattr(plugin, '_auto_cleanup_after_task', AsyncMock())
            await plugin.on_task_agent_done(child, response=SimpleNamespace(completion_text='任务完成'))
            saved = plugin.service.background_tasks.records[key]
            assert saved['status'] == 'failed'
            assert saved['deliveries'] == []
            assert '帧龄' in saved['wait_reason']
            assert saved['result'] == '任务完成'
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())
