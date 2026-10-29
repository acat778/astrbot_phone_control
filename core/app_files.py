"""手机ADB sync双向流式传输，校验大小和SHA256后原子提交。"""

from __future__ import annotations

import asyncio
import hashlib
from astrbot.api import logger
from pathlib import Path, PurePosixPath
import secrets
import shlex
import stat
import struct
import time

from .errors import ActionFailedError
from .app_streams import ProgressWriter
from .device_gate import current_scope
from .process_lease import DeviceLease

MAX_FILE_BYTES = 128 * 1024 * 1024
CHUNK = 65536
IO_IDLE_SECONDS = 15


def phone_path(path):
    value = str(path)
    normal = str(PurePosixPath(value))
    if (len(value.encode()) > 1024 or any(ord(c) < 32 for c in value)
            or ".." in PurePosixPath(value).parts
            or not normal.startswith(("/sdcard/", "/storage/emulated/0/", "/data/local/tmp/"))):
        raise ActionFailedError("文件路径需位于手机共享存储或数据临时目录")
    return normal



class AppFiles:
    def __init__(self, adb):
        self.adb = adb

    async def _read(self, reader, length):
        scope = current_scope()
        if scope:
            scope.checkpoint("file_transfer")
        result=bytearray()
        while len(result)<length:
            if scope:scope.checkpoint("file_transfer")
            chunk=await asyncio.wait_for(reader.read(length-len(result)),IO_IDLE_SECONDS)
            if not chunk:raise asyncio.IncompleteReadError(bytes(result),length)
            result.extend(chunk)
        return bytes(result)

    async def _packet(self, writer, tag, data=b"", number=None):
        scope = current_scope()
        if scope:
            scope.checkpoint("file_transfer")
        writer.write(tag.encode() + struct.pack("<I", len(data) if number is None else number) + data)
        if isinstance(writer, ProgressWriter):
            await writer.drain(idle_timeout=IO_IDLE_SECONDS)
        else:
            await asyncio.wait_for(writer.drain(), IO_IDLE_SECONDS)

    async def _header(self, reader):
        raw = await self._read(reader, 8)
        tag, size = raw[:4], struct.unpack("<I", raw[4:])[0]
        if tag == b"FAIL":
            if size > CHUNK:
                raise ActionFailedError("手机文件错误消息过长")
            detail = (await self._read(reader, size)).decode("utf-8", "replace")
            raise ActionFailedError("手机文件传输失败", detail=detail[:500])
        return tag, size

    async def _remote_hash(self, path):
        value = (await self.adb.shell("sha256sum " + shlex.quote(path), timeout=30)).split()
        if not value or len(value[0]) != 64:
            raise ActionFailedError("手机文件校验值无效")
        return value[0]

    async def push(self, source, destination):
        source = Path(source)
        if not source.is_file() or not 0 <= source.stat().st_size <= MAX_FILE_BYTES:
            raise ActionFailedError("上传文件不存在或超过128 MiB")
        destination = phone_path(destination)
        nonce = secrets.token_hex(16)
        temporary = destination + ".relay-" + nonce + ".part"
        lease = DeviceLease(self.adb, "transfer-" + nonce)
        await lease.arm(None, cleanup="rm -f " + shlex.quote(temporary))
        try:
            digest = hashlib.sha256(); size = 0
            async with asyncio.timeout(180):
                async with self.adb.bridge.streams.connect("sync:") as (reader, writer):
                    await self._packet(writer, "SEND", (temporary + ",33188").encode())
                    with source.open("rb") as handle:
                        while chunk := handle.read(CHUNK):
                            size += len(chunk)
                            if size > MAX_FILE_BYTES:
                                raise ActionFailedError("上传文件超过128 MiB")
                            digest.update(chunk)
                            await self._packet(writer, "DATA", chunk)
                    await self._packet(writer, "DONE", number=int(time.time()))
                    if await self._header(reader) != (b"OKAY", 0):
                        raise ActionFailedError("手机未确认文件写入")
                if await self._remote_hash(temporary) != digest.hexdigest():
                    raise ActionFailedError("手机文件SHA256校验失败")
                await self.adb.shell("mv -f " + shlex.quote(temporary) + " " + shlex.quote(destination), timeout=10)
            return size
        finally:
            try:
                await lease.close()
            except Exception as exc:
                logger.warning("文件租约收尾未完成，设备看门狗将清理临时文件：%s", exc)

    async def read(self, source, maximum):
        source = phone_path(source)
        if type(maximum) is not int or not 0 <= maximum <= 20 * 1024 * 1024 + 1:
            raise ActionFailedError("手机文件读取上限无效")
        result = bytearray()
        async with asyncio.timeout(60):
            async with self.adb.bridge.streams.connect("sync:") as (reader, writer):
                await self._packet(writer, "STAT", source.encode())
                tag, mode = await self._header(reader)
                if tag != b"STAT" or not stat.S_ISREG(mode):
                    raise ActionFailedError("只能读取手机普通文件")
                size, _mtime = struct.unpack("<II", await self._read(reader, 8))
                if size > MAX_FILE_BYTES:
                    raise ActionFailedError("手机文件超过128 MiB")
                await self._packet(writer, "RECV", source.encode())
                while len(result) < min(size, maximum):
                    tag, length = await self._header(reader)
                    if tag != b"DATA" or not 0 < length <= CHUNK or len(result) + length > size:
                        raise ActionFailedError("手机文件数据长度无效")
                    chunk = await self._read(reader, min(length, maximum - len(result)))
                    result.extend(chunk)
        return bytes(result)

    async def pull(self, source, destination):
        source = phone_path(source)
        target = Path(destination)
        if target.is_dir():
            target = target / PurePosixPath(source).name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + secrets.token_hex(8) + ".part")
        size = 0; digest = hashlib.sha256()
        try:
            async with asyncio.timeout(180):
                async with self.adb.bridge.streams.connect("sync:") as (reader, writer):
                    await self._packet(writer, "STAT", source.encode())
                    tag, mode = await self._header(reader)
                    if tag != b"STAT" or not stat.S_ISREG(mode):
                        raise ActionFailedError("只能下载手机普通文件")
                    expected, _mtime = struct.unpack("<II", await self._read(reader, 8))
                    if expected > MAX_FILE_BYTES:
                        raise ActionFailedError("手机文件超过128 MiB")
                    await self._packet(writer, "RECV", source.encode())
                    with temporary.open("xb") as handle:
                        temporary.chmod(0o600)
                        while True:
                            tag, length = await self._header(reader)
                            if tag == b"DONE" and length == 0:
                                break
                            if tag != b"DATA" or not 0 < length <= CHUNK or size + length > expected:
                                raise ActionFailedError("手机文件数据长度无效")
                            chunk = await self._read(reader, length)
                            handle.write(chunk); digest.update(chunk); size += length
                if size != expected or await self._remote_hash(source) != digest.hexdigest():
                    raise ActionFailedError("下载文件大小或SHA256校验失败")
                temporary.replace(target)
            return size
        finally:
            temporary.unlink(missing_ok=True)
