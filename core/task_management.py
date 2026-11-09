"""后台任务删除：排程和Agent清理成功后事务删除记录与截图。"""

import asyncio

from .errors import PhoneControlError


async def delete_background_task(service, job_id):
    lock = getattr(service, '_task_delete_lock', None)
    if lock is None:
        lock = service._task_delete_lock = asyncio.Lock()
    async with lock:
        registry = service.background_tasks
        record = registry.records.get(job_id)
        if record is None:
            raise PhoneControlError('未找到后台任务，请核对完整任务ID')
        manager = getattr(getattr(service, 'app_context', None), 'cron_manager', None)
        cancel = getattr(service, 'background_cancel_hook', None)
        active = record['status'] in {'queued', 'running'}
        if active and (manager is None or not callable(cancel)):
            raise PhoneControlError('后台任务清理接口未就绪，任务记录已保留')
        # 先阻止已派发但尚未开始的Agent；终态保护所有迟到回调。
        registry.update(job_id, 'cancelled', '用户删除任务，执行已取消；等待排程与Agent清理')
        schedule_error = None
        try:
            if manager is not None:
                jobs = await manager.list_jobs('active_agent') or []
                if any(str(j.job_id) == job_id for j in jobs):
                    await manager.delete_job(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            schedule_error = exc
        finally:
            if callable(cancel):
                try:
                    await cancel(job_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise PhoneControlError('Agent清理失败，任务记录与证据已保留，请重试删除') from exc
        if schedule_error is not None:
            raise PhoneControlError('排程清理失败，已请求中止Agent；任务记录与证据已保留，请重试删除') from schedule_error
        registry.delete(job_id)
        return {'job_id': job_id, 'status': 'deleted'}
