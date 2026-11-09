"""SQLite 任务进度与证据；事务保存状态，终态保护迟到回调。"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .action_outcomes import outcome_of
from .errors import PhoneControlError
from .tool_reports import result_failed, result_text, safe_summary

ACTIVE = frozenset({'queued', 'running'})
TERMINAL = frozenset({'completed', 'cancelled', 'ended', 'interrupted', 'failed'})


def event_job_id(event):
    payload = event.get_extra('cron_payload') or {}
    if not isinstance(payload, dict) or payload.get('origin') != 'phone_background_task':
        return ''
    job = event.get_extra('cron_job') or {}
    return str(job.get('id') or '') if isinstance(job, dict) else ''


class BackgroundTaskRegistry:
    """保留所有在途任务、最近100条终态及每任务8张关键截图。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(':memory:') if path is None else None
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ts REAL NOT NULL, mime TEXT NOT NULL, digest TEXT NOT NULL, data BLOB NOT NULL)')

    @contextmanager
    def _db(self):
        db = self._memory or sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise PhoneControlError('后台任务记录读写失败，请检查任务数据库与存储权限') from exc
        finally:
            if db is not self._memory:
                db.close()

    @property
    def records(self):
        with self._db() as db:
            return {key: json.loads(body) for key, body in db.execute('SELECT id, body FROM tasks ORDER BY rowid')}

    def _save(self, db, record):
        db.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?)', (record['job_id'], json.dumps(record, ensure_ascii=False)))

    def add(self, job, *, sender: str, session: str):
        now = time.time()
        payload = getattr(job, 'payload', None) or {}
        record = {
            'job_id': str(job.job_id), 'sender': sender, 'session': session,
            'summary': str(job.description or '')[:500],
            'instruction': str(payload.get('note') or '')[:4000],
            'status': 'queued', 'result': '', 'progress': '等待宿主启动手机Agent',
            'wait_reason': '等待调度', 'steps': 0, 'last_tool': '',
            'created_at': now, 'updated_at': now, 'started_at': None, 'finished_at': None,
            'events': [], 'screenshots': [], 'deliveries': [],
        }
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO tasks VALUES (?, ?)', (record['job_id'], json.dumps(record, ensure_ascii=False)))
            self._prune(db)

    def _prune(self, db):
        rows = [json.loads(body) for (body,) in db.execute('SELECT body FROM tasks')]
        ended = sorted((r for r in rows if r['status'] in TERMINAL), key=lambda r: r['updated_at'], reverse=True)
        for item in ended[100:]:
            db.execute('DELETE FROM evidence WHERE task_id=?', (item['job_id'],))
            db.execute('DELETE FROM tasks WHERE id=?', (item['job_id'],))

    def _change(self, job_id, change):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (job_id,)).fetchone()
            if row is None:
                return
            record = json.loads(row[0])
            if record['status'] in TERMINAL:
                return
            change(record, db)
            record['updated_at'] = time.time()
            self._save(db, record)
            self._prune(db)

    def update(self, job_id: str, status: str, result: str = ''):
        if status not in ACTIVE | TERMINAL:
            raise PhoneControlError('后台任务状态无效')
        def change(record, db):
            record['status'] = status
            if result:
                record['result'] = str(result)[:4000]
            if status == 'running':
                record['started_at'] = record['started_at'] or time.time()
                record.update(progress='Agent已启动，等待模型响应', wait_reason='等待模型响应')
            elif status in TERMINAL:
                record['finished_at'] = time.time()
                if result:
                    record['progress'] = str(result)[:500]
                record['wait_reason'] = '' if status == 'completed' else (str(result)[:500] or record['wait_reason'] or '执行已结束，未确认结果')
                if status == 'ended' and record.get('action_result_counts', {}).get('result_unconfirmed'):
                    record['wait_reason'] = '动作已执行，仍有目标结果未确认；请核对动作记录与截图证据'
                if status == 'failed' and record['events'] and not record['events'][-1]['ok']:
                    record['wait_reason'] = record['events'][-1]['summary']
        self._change(job_id, change)

    def tool_started(self, event, name):
        def change(record, db):
            record.update(last_tool=name, progress='执行 ' + name, wait_reason='等待工具返回')
        self._change(event_job_id(event), change)

    def tool_finished(self, event, name, result):
        text = result_text(result)
        failed = result_failed(result) or text.startswith(('已中止', '手机 token 额度已用完'))
        def change(record, db):
            summary = safe_summary(result)
            record.update(last_tool=name, steps=record['steps'] + 1,
                          progress=f'{name}：{summary}',
                          wait_reason=summary if failed else '等待模型决定下一步')
            outcome = outcome_of(result)
            if outcome:
                counts = record.setdefault('action_result_counts', {})
                counts[outcome['status']] = counts.get(outcome['status'], 0) + 1
                record['action_outcomes'] = (record.get('action_outcomes', []) + [outcome])[-100:]
            record['events'] = (record['events'] + [{'ts': time.time(), 'tool': name, 'ok': not failed, 'outcome': outcome_of(result), 'summary': summary}])[-50:]
        self._change(event_job_id(event), change)

    def completion_status(self, job_id):
        record = self.records.get(job_id) or {}
        outcomes = record.get('action_outcomes', [])
        counts = record.get('action_result_counts', {})
        if counts.get('failed') or any(o['status'] == 'failed' for o in outcomes) or (record.get('events') and not record['events'][-1]['ok']):
            return 'failed'
        if counts.get('result_unconfirmed') or any(o['status'] == 'result_unconfirmed' for o in outcomes):
            return 'ended'
        return 'completed'

    def waiting(self, event, reason):
        def change(record, db):
            record['wait_reason'] = str(reason)[:500]
        self._change(event_job_id(event), change)

    def screenshot(self, event, data: bytes, mime: str):
        if mime not in {'image/png', 'image/jpeg'} or not data or len(data) > 2 * 1024 * 1024:
            return
        def change(record, db):
            digest = hashlib.sha256(data).hexdigest()
            if any(s['sha256'] == digest for s in record['screenshots']):
                return
            eid, ts = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?)', (eid, record['job_id'], ts, mime, digest, data))
            record['screenshots'].append({'id': eid, 'ts': ts, 'mime': mime, 'sha256': digest, 'bytes': len(data)})
            for old in record['screenshots'][:-8]:
                db.execute('DELETE FROM evidence WHERE id=?', (old['id'],))
            record['screenshots'] = record['screenshots'][-8:]
        self._change(event_job_id(event), change)

    def delivered(self, event, *, text, link, target):
        from .traffic import mask_url
        def change(record, db):
            record['deliveries'] = (record['deliveries'] + [{
                'ts': time.time(), 'text': str(text)[:1000], 'link': mask_url(link) if link else '',
                'target': target, 'status': 'sent',
            }])[-20:]
        self._change(event_job_id(event), change)

    def evidence(self, job_id, evidence_id):
        with self._db() as db:
            return db.execute('SELECT mime, data FROM evidence WHERE task_id=? AND id=?', (job_id, evidence_id)).fetchone()

    def owned(self, sender: str, session: str):
        return sorted((r for r in self.records.values() if r['sender'] == sender and r['session'] == session), key=lambda r: r['created_at'], reverse=True)

    def active(self):
        return [r['job_id'] for r in self.records.values() if r['status'] in ACTIVE]

    def recover(self):
        for job_id, record in self.records.items():
            if record['status'] == 'running':
                self.update(job_id, 'interrupted', '插件或进程重启中断执行；保留已有进度和证据，请核对后重新提交')

    def agent_exited(self, job_id, task):
        if task.cancelled():
            self.update(job_id, 'cancelled', 'Agent执行已取消')
        else:
            error = task.exception()
            if error is not None:
                self.update(job_id, 'failed', type(error).__name__ + '：' + safe_summary(str(error)))
            else:
                self.update(job_id, 'ended', 'Agent已退出，未收到确认完成的回调；请核对结果证据')
