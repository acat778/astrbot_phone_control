"""有归属的手机端录制与成品流式下载。"""
from __future__ import annotations
import asyncio
import contextvars
import hashlib
from astrbot.api import logger
import math
import re
import secrets
from .errors import ActionFailedError
from .app_files import MAX_FILE_BYTES
from .device_gate import current_scope


class AppRecording:
    def __init__(self, bridge, files):
        self.bridge, self.files = bridge, files
        self.current = None
        self.lock = asyncio.Lock()

    def owned(self, owner):
        if self.current is None:raise ActionFailedError('当前没有手机录制任务')
        if self.current['owner'] != owner:raise ActionFailedError('手机录制属于其他会话')
        if self.current['session'] != self.bridge.session:
            self.current = None
            raise ActionFailedError('录制随App控制连接结束，请重新开始')
        return self.current

    async def start(self, owner, *, mode='both', seconds=60, source='output'):
        async with self.lock:
            if self.current is not None and self.current['session'] == self.bridge.session:
                raise ActionFailedError('请先完成或取消当前手机录制')
            if 'media_recording' not in self.bridge.peer.get('features',[]):
                raise ActionFailedError('请更新 Phone Relay App 以使用手机录制')
            if mode not in ('audio','video','both') or not math.isfinite(seconds) or not 1 <= seconds <= 900:
                raise ActionFailedError('录制模式需为audio/video/both，时长为1–900秒')
            recording = secrets.token_hex(16)
            self.current = {'owner':owner,'session':self.bridge.session,'recording_id':recording,'mode':mode}
            try:
                result = await self.bridge.call('media_start',recording_id=recording,mode=mode,seconds=seconds,source=source,timeout=25)
                if result.get('recording_id') != recording or not result.get('running'):
                    raise ActionFailedError('手机未确认录制启动')
                return result
            except BaseException as error:
                try:
                    await self._close_current()
                except Exception as cleanup:
                    error.add_note('手机录制收尾失败：'+str(cleanup))
                raise

    async def status(self, owner):
        if self.current is None:return {'running':False,'status':'idle'}
        current = self.owned(owner)
        return await self.bridge.call('media_status',recording_id=current['recording_id'],timeout=10)

    async def stop(self, owner):
        async with self.lock:
            current = self.owned(owner);recording = current['recording_id']
            result = await self.bridge.call('media_stop',recording_id=recording,timeout=25)
            if not result.get('ready') or result.get('error'):
                raise ActionFailedError('手机录制未完成',detail=str(result.get('error') or result.get('reason')))
            size = result.get('size');digest = result.get('sha256')
            if type(size) is not int or not 0 < size <= MAX_FILE_BYTES or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
                raise ActionFailedError('录制文件大小或校验信息无效')
            target = self.files.allocate('.mp4' if result['mode']!='audio' else '.m4a')
            partial = target.with_suffix(target.suffix+'.part');received = 0;hashed = hashlib.sha256()
            try:
                async with asyncio.timeout(180):
                    async with self.bridge.streams.connect('artifact:'+recording) as (reader,_writer):
                        with partial.open('xb') as output:
                            partial.chmod(0o600)
                            while chunk := await asyncio.wait_for(reader.read(65536),15):
                                scope = current_scope()
                                if scope:scope.checkpoint('recording_download')
                                received += len(chunk)
                                if received > size:raise ActionFailedError('录制文件超过声明大小')
                                hashed.update(chunk);output.write(chunk)
                if received != size or hashed.hexdigest() != digest:
                    raise ActionFailedError('录制文件大小或SHA256校验失败')
                partial.replace(target)
                await self._close_current()
                return {**result,**(await self.files.describe(target)),'running':False,'status':'saved'}
            finally:partial.unlink(missing_ok=True)

    async def cancel(self, owner):
        async with self.lock:
            self.owned(owner)
            await self._close_current()
            return {'running':False,'status':'cancelled'}

    async def _close_current(self):
        current = self.current
        if current is None:return
        if current['session'] == self.bridge.session and self.bridge.writer is not None:
            try:
                cleanup = asyncio.create_task(self.bridge.call('media_delete',recording_id=current['recording_id'],timeout=15), context=contextvars.Context())
                await asyncio.shield(cleanup)
            except Exception as exc:
                logger.warning('手机录制收尾失败，连接结束时将清理：%s',exc)
                raise
        self.current = None

    async def close(self):
        async with self.lock:await self._close_current()
