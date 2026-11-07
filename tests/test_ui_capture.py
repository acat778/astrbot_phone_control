"""当前节点采集的组件校验、失败传播和取消回收。"""
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from core.errors import AdbCommandError, UiDumpError
from core.ui_capture import capture_output


def test_component_verified_before_execution_and_shared_by_relay():
    async def run():
        from core.app_adb import AppAdbClient
        from core.adb import AdbClient
        assert AppAdbClient.dump_ui_output is AdbClient.dump_ui_output
        adb = SimpleNamespace(_command_timeout=10, run=AsyncMock())
        async def shell(command, **kwargs):
            if command.startswith('sha256sum'):
                if adb.run.await_count == 0:
                    return ''
                import hashlib
                from pathlib import Path
                component = Path(adb.run.await_args.args[0][1])
                return hashlib.sha256(component.read_bytes()).hexdigest()+'  component'
            assert 'fun.acat.uicapture.UiCapture' in command
            assert 'trap ' in command and '&& cat ' in command
            assert kwargs['retry'] is False
            return '<hierarchy><node text="fresh"/></hierarchy>'
        adb.shell = AsyncMock(side_effect=shell)
        assert b'fresh' in await capture_output(adb, '/dev/tty')
        assert adb.run.await_count == 1
        assert b'fresh' in await capture_output(adb, '/dev/tty')
        assert adb.run.await_count == 1
    asyncio.run(run())


def test_bad_component_never_executes():
    async def run():
        adb = SimpleNamespace(_command_timeout=10, run=AsyncMock(), shell=AsyncMock(return_value='wrong'))
        with pytest.raises(UiDumpError, match='摘要'):
            await capture_output(adb, '/dev/tty')
        assert adb.shell.await_count == 2
        assert all('UiCapture' not in c.args[0] for c in adb.shell.await_args_list)
    asyncio.run(run())


def test_runtime_failure_propagates_and_cancel_releases_lock():
    async def run():
        adb = SimpleNamespace(_command_timeout=10, run=AsyncMock(), shell=AsyncMock(side_effect=AdbCommandError('runtime failed')))
        with pytest.raises(AdbCommandError, match='runtime failed'):
            await capture_output(adb, '/dev/tty')
        assert not adb._ui_capture_lock.locked()
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        adb.shell.side_effect = blocked
        task = asyncio.create_task(capture_output(adb, '/dev/tty'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not adb._ui_capture_lock.locked()
    asyncio.run(run())
