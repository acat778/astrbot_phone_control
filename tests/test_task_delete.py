"""删除事务、执行收尾失败重试、会话隔离和迟到回调。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.errors import PhoneControlError
from core.task_management import delete_background_task
from core.task_records import BackgroundTaskRegistry


def fixture(tmp_path):
    registry = BackgroundTaskRegistry(tmp_path / 'tasks.sqlite3')
    job = SimpleNamespace(job_id='job-id', description='任务', payload={})
    registry.add(job, sender='u', session='s')
    data = {'cron_payload': {'origin': 'phone_background_task'}, 'cron_job': {'id': job.job_id}}
    event = SimpleNamespace(get_extra=lambda k: data.get(k))
    registry.screenshot(event, b'proof', 'image/png')
    manager = SimpleNamespace(list_jobs=AsyncMock(return_value=[job]), delete_job=AsyncMock())
    cancel = AsyncMock()
    service = SimpleNamespace(background_tasks=registry, app_context=SimpleNamespace(cron_manager=manager), background_cancel_hook=cancel)
    return service, event, manager, cancel


def test_active_task_stops_before_records_and_evidence_disappear(tmp_path):
    async def run():
        service, event, manager, cancel = fixture(tmp_path)
        registry = service.background_tasks
        proof_id = registry.records['job-id']['screenshots'][0]['id']
        async def stop(job_id):
            assert registry.records[job_id]['status'] == 'cancelled'
            assert registry.evidence(job_id, proof_id)
            registry.tool_finished(event, 'phone_tap', 'late callback')
        cancel.side_effect = stop
        assert await delete_background_task(service, 'job-id') == {'job_id': 'job-id', 'status': 'deleted'}
        manager.delete_job.assert_awaited_once_with('job-id')
        cancel.assert_awaited_once_with('job-id')
        registry.tool_finished(event, 'phone_screen', 'late callback')
        registry.update('job-id', 'completed', 'late completion')
        fresh = BackgroundTaskRegistry(tmp_path / 'tasks.sqlite3')
        assert fresh.records == {} and fresh.evidence('job-id', proof_id) is None
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['schedule', 'cancel'])
def test_cleanup_failure_preserves_evidence_and_retry_completes(tmp_path, boundary):
    async def run():
        service, event, manager, cancel = fixture(tmp_path)
        proof_id = service.background_tasks.records['job-id']['screenshots'][0]['id']
        target = manager.delete_job if boundary == 'schedule' else cancel
        target.side_effect = RuntimeError('broken')
        with pytest.raises(PhoneControlError, match='保留'):
            await delete_background_task(service, 'job-id')
        assert service.background_tasks.records['job-id']['status'] == 'cancelled'
        assert service.background_tasks.evidence('job-id', proof_id)
        cancel.assert_awaited_once_with('job-id')
        target.side_effect = None
        await delete_background_task(service, 'job-id')
        assert not service.background_tasks.records
    asyncio.run(run())


def test_unavailable_cleanup_keeps_active_task_and_evidence(tmp_path):
    async def run():
        service, event, manager, cancel = fixture(tmp_path)
        service.app_context.cron_manager = None
        with pytest.raises(PhoneControlError, match='未就绪'):
            await delete_background_task(service, 'job-id')
        assert service.background_tasks.records['job-id']['status'] == 'queued'
        cancel.assert_not_awaited()
        with pytest.raises(PhoneControlError):
            service.background_tasks.delete('job-id')
    asyncio.run(run())


def test_completed_history_can_be_deleted_without_device_or_scheduler(tmp_path):
    async def run():
        service, event, manager, cancel = fixture(tmp_path)
        service.background_tasks.update('job-id', 'completed')
        service.app_context = None
        service.background_cancel_hook = None
        await delete_background_task(service, 'job-id')
        assert not service.background_tasks.records
    asyncio.run(run())
