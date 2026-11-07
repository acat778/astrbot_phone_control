"""部署并调用当前节点采集组件，直连和 Relay 共用同一条路径。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shlex
import time
import uuid

from .errors import UiDumpError


async def capture_output(adb, path: str, *, timeout: float | None = None) -> bytes:
    component = Path(__file__).resolve().parent.parent / "bin/android/phone-ui-capture.jar"
    digest = hashlib.sha256(component.read_bytes()).hexdigest()
    remote = f"/data/local/tmp/acat-ui-{digest}.jar"
    deadline = time.monotonic() + (timeout if timeout is not None else adb._command_timeout)

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise UiDumpError("当前节点采集超时")
        return value

    lock = getattr(adb, "_ui_capture_lock", None)
    if lock is None:
        lock = adb._ui_capture_lock = asyncio.Lock()
    async def capture():
        async with lock:
            probe = await adb.shell(f"sha256sum {remote} 2>/dev/null || true", timeout=remaining())
            if not probe.startswith(digest + " "):
                await adb.run(["push", str(component), remote], timeout=remaining())
                probe = await adb.shell(f"sha256sum {remote}", timeout=remaining())
                if not probe.startswith(digest + " "):
                    raise UiDumpError("手机节点采集组件摘要不匹配")
            from .ui_commands import ui_dump_command

            output_path = f"/data/local/tmp/acat-ui-{uuid.uuid4().hex}.xml" if path == "/dev/tty" else path
            command = ui_dump_command(output_path, component=remote)
            if path == "/dev/tty":
                command = (
                    f"trap {shlex.quote('rm -f ' + shlex.quote(output_path))} EXIT; "
                    + command + f" >/dev/null && cat {shlex.quote(output_path)}"
                )
            return (await adb.shell(command, timeout=remaining(), retry=False)).encode("utf-8")

    try:
        return await asyncio.wait_for(capture(), timeout=remaining())
    except asyncio.TimeoutError as exc:
        raise UiDumpError("当前节点采集超时") from exc
