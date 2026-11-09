"""动作结果依赖本次前后证据，观察异常不重放设备副作用。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.action_outcomes import ResultExpectation, action_result, outcome_of
from core.errors import PhoneControlError, UiDumpError
from core.models import ScreenSnapshot, UiNode
from core.task_records import BackgroundTaskRegistry
from core.tool_reports import result_failed


def shot(text='', package='test.app', activity='Page', resource='value'):
    return ('ui', ScreenSnapshot(width=100, height=100, package=package, activity=activity,
                                nodes=[UiNode(ref=0, text=text, resource_id=resource)]))


@pytest.mark.parametrize('condition,before,after,status', [
    ({'text': '目标页'}, shot('首页'), shot('目标页'), 'execution_succeeded'),
    ({'text': '提交成功'}, shot('待提交'), shot('提交成功'), 'execution_succeeded'),
    ({'text': '提交成功'}, shot('提交成功'), shot('提交成功'), 'result_unconfirmed'),
    ({'absent_text': '待提交'}, shot('待提交'), shot('提交成功'), 'execution_succeeded'),
    ({'absent_text': '待提交'}, shot('别的内容'), shot('提交成功'), 'result_unconfirmed'),
    ({'changed_resource_id': 'value'}, shot('1'), shot('2'), 'execution_succeeded'),
    ({'changed_resource_id': 'missing'}, shot('1'), shot('2'), 'result_unconfirmed'),
    ({'package': 'other.app', 'activity': 'Next'}, shot('a'), shot('b', 'other.app', 'Next'), 'execution_succeeded'),
    ({'package': 'test.app'}, shot('a'), shot('a'), 'result_unconfirmed'),
    ({'text': '成功', 'failure_text': '失败'}, shot('待提交'), shot('提交失败'), 'failed'),
    ({'text': '成功', 'package': 'other.app'}, shot('待提交'), shot('成功'), 'result_unconfirmed'),
])
def test_conditions_require_new_complete_evidence(condition, before, after, status):
    async def run():
        service = SimpleNamespace(screen=AsyncMock(side_effect=[before, after]))
        expected = ResultExpectation(dict(condition, timeout_seconds=0))
        await expected.prepare(service)
        result = await expected.finish(service, '已点击')
        detail = outcome_of(result)
        assert detail['status'] == status
        assert detail['execution_status'] == 'succeeded'
        assert result_failed(result) is (status == 'failed')
    asyncio.run(run())


@pytest.mark.parametrize('error', [UiDumpError('节点超时'), asyncio.TimeoutError()])
def test_post_observation_failure_keeps_execution_fact(error):
    async def run():
        action = AsyncMock()
        service = SimpleNamespace(screen=AsyncMock(side_effect=[shot('前'), error]))
        expected = ResultExpectation({'text': '后', 'timeout_seconds': 0})
        await expected.prepare(service)
        await action()
        result = await expected.finish(service, '已点击')
        action.assert_awaited_once()
        assert outcome_of(result)['status'] == 'result_unconfirmed'
        assert outcome_of(result)['execution_status'] == 'succeeded'
        assert not result_failed(result)
    asyncio.run(run())


def test_observation_cancellation_propagates():
    async def run():
        service = SimpleNamespace(screen=AsyncMock(side_effect=[shot('前'), asyncio.CancelledError()]))
        expected = ResultExpectation({'text': '后', 'timeout_seconds': 0})
        await expected.prepare(service)
        with pytest.raises(asyncio.CancelledError):
            await expected.finish(service, '已点击')
    asyncio.run(run())


@pytest.mark.parametrize('condition', [{}, {'timeout_seconds': 1}, {'text': ''}, {'text': 'ok', 'timeout_seconds': float('nan')}, {'text': 'ok', 'unknown': True}])
def test_bad_conditions_rejected_before_action(condition):
    with pytest.raises(PhoneControlError):
        ResultExpectation(condition)


def test_later_read_or_delivery_does_not_turn_unconfirmed_action_into_completion(tmp_path):
    registry = BackgroundTaskRegistry(tmp_path / 'tasks.sqlite3')
    registry.add(SimpleNamespace(job_id='j', description='提交', payload={}), sender='u', session='s')
    data = {'cron_payload': {'origin': 'phone_background_task'}, 'cron_job': {'id': 'j'}}
    event = SimpleNamespace(get_extra=lambda k: data.get(k))
    registry.tool_finished(event, 'phone_tap', action_result('已点击', reason='未指定条件'))
    registry.tool_finished(event, 'phone_screen', '看到了页面')
    registry.tool_finished(event, 'phone_share_to_user', '已送达')
    reloaded = BackgroundTaskRegistry(tmp_path / 'tasks.sqlite3')
    assert reloaded.completion_status('j') == 'ended'
    assert reloaded.records['j']['events'][0]['outcome']['status'] == 'result_unconfirmed'
