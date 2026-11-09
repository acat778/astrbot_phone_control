"""真实工具包装与任务回调共用结果条件。"""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_main_0130_features import FakeEvent, FakeToolContext, _tools_ns, _make_plugin, _cleanup
from test_subagent import configure

Tap = _tools_ns['PhoneTapTool']
mod = importlib.import_module(Tap.__module__.rsplit('.', 2)[0] + '.action_outcomes')
models = importlib.import_module(Tap.__module__.rsplit('.', 2)[0] + '.models')


def test_tap_changes_page_once_and_rejects_invalid_condition_before_write(monkeypatch):
    async def run():
        monkeypatch.setattr(Tap, '_guard', AsyncMock())
        before = models.ScreenSnapshot(width=100, height=100, nodes=[models.UiNode(ref=1, text='提交')])
        after = models.ScreenSnapshot(width=100, height=100, nodes=[models.UiNode(ref=1, text='提交成功')])
        service = SimpleNamespace(config=SimpleNamespace(model=SimpleNamespace(work_as_subagent=False)), tap=AsyncMock(), screen=AsyncMock(side_effect=[('', before), ('', after)]))
        tool = Tap(service=service)
        result = await tool.call(FakeToolContext(FakeEvent()), x=20, y=20, expect={'text': '提交成功'})
        assert mod.outcome_of(result)['status'] == 'execution_succeeded'
        service.tap.assert_awaited_once_with(20, 20)
        invalid = await tool.call(FakeToolContext(FakeEvent()), x=20, y=20, expect={'text': ''})
        assert invalid.startswith('error:')
        assert mod.outcome_of(invalid)['status'] == 'failed'
        assert service.tap.await_count == 1
    asyncio.run(run())


def test_agent_finish_preserves_unconfirmed_click_after_successful_share(tmp_path, monkeypatch):
    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)
        tool = configure(plugin)
        try:
            key = json.loads(await tool.call(FakeToolContext(FakeEvent()), instruction='提交表单'))['job_id']
            job = plugin.context.cron_manager.jobs[key]
            child = FakeEvent(umo='cron:OtherMessage:' + key, platform='cron', payload={'cron_payload': job.payload})
            child.set_extra('cron_job', {'id': key})
            plugin.service.background_tasks.update(key, 'running')
            await plugin.track_phone_tool_result(child, SimpleNamespace(name='phone_tap'), {}, mod.action_result('已点击'))
            await plugin.track_phone_tool_result(child, SimpleNamespace(name='phone_share_to_user'), {}, '已送达')
            monkeypatch.setattr(plugin, '_auto_cleanup_after_task', AsyncMock())
            await plugin.on_task_agent_done(child, response=SimpleNamespace(completion_text='操作完成'))
            record = plugin.service.background_tasks.records[key]
            assert record['status'] == 'ended'
            assert '未确认' in record['wait_reason']
            assert record['result'] == '操作完成'
        finally:
            await _cleanup(plugin)
    asyncio.run(run())
