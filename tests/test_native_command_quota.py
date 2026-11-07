"""原生命令在额度耗尽时仍可完成确定性操作，模型任务保持门禁。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_main_0130_features import FakeEvent, FakeToolContext, _cleanup, _make_plugin
from test_subagent import configure


@pytest.mark.parametrize('sub', ['screenshot', 'connect', 'open 测试', 'status', 'browse 测试'])
def test_deterministic_commands_ignore_model_quota(tmp_path, monkeypatch, sub):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        event = FakeEvent()
        event.stop_event = lambda: None
        event.send = AsyncMock()
        say = AsyncMock()
        monkeypatch.setattr(plugin, '_say', say)
        monkeypatch.setattr(plugin, '_say_command', say)
        monkeypatch.setattr(plugin.service, 'quota_error_text', lambda: '额度已用完')
        screenshot = AsyncMock(return_value=(b'image', tmp_path / 'screen.png'))
        connect = AsyncMock(return_value=SimpleNamespace(serial='test', manufacturer='', model='', width=1, height=1, current_package=''))
        open_app = AsyncMock(return_value='opened')
        browse = AsyncMock()
        monkeypatch.setattr(plugin.service, 'screenshot', screenshot)
        monkeypatch.setattr(plugin.service, 'connect', connect)
        monkeypatch.setattr(plugin.service, 'open_app', open_app)
        monkeypatch.setattr(plugin, '_cmd_browse', browse)
        monkeypatch.setattr(plugin.service, 'status_text', AsyncMock(return_value='status-ready'))
        try:
            await plugin.phone_command(event, sub)
            assert all('额度已用完' not in str(c) for c in say.call_args_list)
            assert screenshot.await_count == int(sub == 'screenshot')
            assert connect.await_count == int(sub == 'connect')
            assert open_app.await_count == int(sub.startswith('open'))
            assert browse.await_count == int(sub.startswith('browse'))
            assert event.send.await_count == int(sub == 'screenshot')
            assert not plugin.device_gate.busy
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


@pytest.mark.parametrize('sub', ['task 测试', 'play', 'watch', 'media'])
def test_model_commands_stop_before_execution_when_quota_exhausted(tmp_path, monkeypatch, sub):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        say = AsyncMock()
        monkeypatch.setattr(plugin, '_say', say)
        monkeypatch.setattr(plugin.service, 'quota_error_text', lambda: '额度已用完')
        try:
            await plugin._dispatch_phone(FakeEvent(), sub)
            assert say.call_args.args[1] == '额度已用完'
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_background_start_checks_quota_but_management_and_permission_remain(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        event = FakeEvent()
        event.stop_event = lambda: None
        say = AsyncMock()
        monkeypatch.setattr(plugin, '_say', say)
        screenshot = AsyncMock()
        monkeypatch.setattr(plugin.service, 'screenshot', screenshot)
        monkeypatch.setattr(plugin.service, 'quota_error_text', lambda: '额度已用完')
        try:
            assert await tool.call(FakeToolContext(event), instruction='test') == '额度已用完'
            assert not plugin.context.cron_manager.jobs
            assert 'tasks' in await tool.call(FakeToolContext(event), action='list')
            denied = FakeEvent(role='member', sender_id='other')
            denied.stop_event = lambda: None
            await plugin.phone_command(denied, 'screenshot')
            assert '权限不足' in say.call_args.args[1]
            screenshot.assert_not_awaited()
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())
