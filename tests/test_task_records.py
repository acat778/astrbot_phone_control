"""任务持久化、终态并发与证据留存的确定性验证。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.errors import PhoneControlError
from core.task_records import BackgroundTaskRegistry


def job(key='job-1'):
    return SimpleNamespace(job_id=key, description='测试任务', payload={'note': '查看指定页面并汇报'})


def event(key='job-1'):
    data = {'cron_payload': {'origin': 'phone_background_task'}, 'cron_job': {'id': key}}
    return SimpleNamespace(get_extra=lambda k: data.get(k))


def test_reopen_progress_wait_result_and_independent_screenshot(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    store = BackgroundTaskRegistry(path)
    store.add(job(), sender='owner', session='session')
    store.update('job-1', 'running')
    store.tool_started(event(), 'phone_screen')
    assert BackgroundTaskRegistry(path).records['job-1']['wait_reason'] == '等待工具返回'
    source = tmp_path / 'source.png'
    source.write_bytes(b'png-content')
    store.screenshot(event(), source.read_bytes(), 'image/png')
    source.unlink()
    store.tool_finished(event(), 'phone_screen', 'error: 设备离线，等待重新连接')
    saved = BackgroundTaskRegistry(path).owned('owner', 'session')[0]
    assert saved['steps'] == 1 and not saved['events'][0]['ok']
    assert '设备离线' in saved['wait_reason']
    proof = saved['screenshots'][0]
    assert store.evidence('job-1', proof['id']) == ('image/png', b'png-content')
    assert store.evidence('other-job', proof['id']) is None
    assert not store.owned('other-owner', 'session')
    assert not store.owned('owner', 'other-session')
    store.delivered(event(), text='读取成功', link='https://example.com/result?token=secret-value', target='session')
    store.update('job-1', 'completed', '任务完成')
    saved = BackgroundTaskRegistry(path).owned('owner', 'session')[0]
    assert saved['status'] == 'completed' and saved['result'] == '任务完成'
    assert saved['deliveries'][0]['status'] == 'sent'
    assert 'secret-value' not in saved['deliveries'][0]['link']
    assert saved['finished_at'] >= saved['started_at'] >= saved['created_at']


def test_late_instance_cannot_overwrite_cancelled_or_completed(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    old, new = BackgroundTaskRegistry(path), BackgroundTaskRegistry(path)
    old.add(job(), sender='owner', session='session')
    new.update('job-1', 'cancelled', '用户取消')
    old.update('job-1', 'completed', '迟到结果')
    old.tool_finished(event(), 'phone_tap', 'ok')
    old.screenshot(event(), b'image', 'image/png')
    saved = new.records['job-1']
    assert saved['status'] == 'cancelled' and saved['result'] == '用户取消'
    assert saved['steps'] == 0 and saved['screenshots'] == []
    detached = new.records
    detached['job-1']['status'] = 'running'
    assert new.records['job-1']['status'] == 'cancelled'


def test_recovery_marks_running_interrupted_and_keeps_queued(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    store = BackgroundTaskRegistry(path)
    for key in ('queued', 'running'):
        store.add(job(key), sender='owner', session='session')
    store.update('running', 'running')
    store.tool_finished(event('running'), 'phone_input_text', '已输入')
    fresh = BackgroundTaskRegistry(path)
    fresh.recover()
    assert fresh.records['running']['status'] == 'interrupted'
    assert fresh.records['running']['steps'] == 1
    assert '重启中断' in fresh.records['running']['wait_reason']
    assert fresh.active() == ['queued']


def test_screenshot_and_terminal_history_bounds(tmp_path):
    store = BackgroundTaskRegistry(tmp_path / 'tasks.sqlite3')
    store.add(job(), sender='owner', session='session')
    for i in range(12):
        store.screenshot(event(), bytes([i]), 'image/png')
    shots = store.records['job-1']['screenshots']
    assert len(shots) == 8
    store.screenshot(event(), b'\x0b', 'image/png')
    assert len(store.records['job-1']['screenshots']) == 8
    with store._db() as db:
        assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 8
    store.update('job-1', 'completed')
    for i in range(101):
        store.add(job(str(i)), sender='owner', session='session')
        store.update(str(i), 'ended')
    assert len(store.records) == 100
    assert store.evidence('job-1', shots[0]['id']) is None


def test_corrupted_database_is_not_overwritten(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    path.write_bytes(b'broken-database')
    with pytest.raises(PhoneControlError, match='数据库'):
        BackgroundTaskRegistry(path)
    assert path.read_bytes() == b'broken-database'


def test_abnormal_agent_exit_retains_error_and_does_not_claim_completion(tmp_path):
    async def scenario():
        store = BackgroundTaskRegistry(tmp_path / 'tasks.sqlite3')
        store.add(job(), sender='owner', session='session')
        store.update('job-1', 'running')
        async def failed():
            raise RuntimeError('模型服务不可用')
        task = asyncio.create_task(failed())
        task.add_done_callback(lambda done: store.agent_exited('job-1', done))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        saved = store.records['job-1']
        assert saved['status'] == 'failed'
        assert '模型服务不可用' in saved['wait_reason']
    asyncio.run(scenario())
