"""用真实进程验证心跳失联、进程组回收与资源互斥。"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.process_lease import ROOT


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待状态变化超时")


def spawn(tmp_path, name, rev="first"):
    pidfile = tmp_path / name
    script = ("import subprocess,time; from pathlib import Path; "
              "p=subprocess.Popen(['sleep','60']); "
              f"Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bin/phone_process_guard.py"),
         "--resource", str(tmp_path / "owned.lock"), "--revision", rev,
         "--ttl", "0.5", "--", sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc, pidfile


@pytest.mark.parametrize("disconnect", [False, True])
def test_lost_plugin_reaps_process_group(tmp_path, disconnect):
    proc, pidfile = spawn(tmp_path, "child.pid")
    try:
        wait_for(pidfile.exists)
        child = int(pidfile.read_text())
        proc.stdin.write(b"heartbeat\n")
        proc.stdin.flush()
        if disconnect:
            proc.stdin.close()
        proc.wait(timeout=5)
        # 监护器负责回收管道孙进程，退出后连 defunct 记录也应消失。
        state = Path(f"/proc/{child}/stat")
        wait_for(lambda: not state.exists())
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_updated_code_waits_for_predecessor_before_start(tmp_path):
    old, first = spawn(tmp_path, "first.pid", "old-code")
    new = None
    try:
        wait_for(first.exists)
        new, second = spawn(tmp_path, "second.pid", "new-code")
        time.sleep(0.1)
        assert not second.exists()
        old.stdin.close()
        old.wait(timeout=5)
        wait_for(second.exists)
        manifest = json.loads((tmp_path / "owned.lock").read_text())
        assert manifest["revision"] == "new-code"
        new.stdin.close()
        new.wait(timeout=5)
    finally:
        for process in (old, new):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_android_launch_embeds_watchdog_before_return():
    from core.process_lease import DeviceLease
    lease = DeviceLease(None, "scrcpy")
    command = lease.wrap_launch("app_process / Example < /dev/null > /dev/null 2>&1 & echo PID=$!")
    assert "GUARD_PID=$!" in command
    assert "/proc/$1/stat" in command
    assert "date +%s" in command
    assert lease.path in command
    assert command.index("P=$!") < command.index("GUARD_PID=$!")


def test_reclaim_untended_process_uses_exact_data_directory(tmp_path):
    from core.process_lease import reclaim_legacy_processes
    helper = tmp_path / "mitmdump"
    helper.write_text("import time; time.sleep(60)\n")
    marker = tmp_path / "owned"
    owned = subprocess.Popen([sys.executable, str(helper), f"confdir={marker}"])
    other = subprocess.Popen([sys.executable, str(helper), f"confdir={marker}-other"])
    try:
        assert asyncio.run(reclaim_legacy_processes([marker])) == 1
        owned.wait(timeout=3)
        assert other.poll() is None
    finally:
        for proc in (owned, other):
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)


def test_device_heartbeat_retries_transient_failure_without_extending_expiry(monkeypatch):
    import asyncio
    import core.process_lease as leases
    async def run():
        calls=[];recovered=asyncio.Event()
        class Adb:
            async def shell(self,command,**kwargs):
                calls.append(command)
                if 'case "$E"' in command:
                    if len([x for x in calls if 'case "$E"' in x])==1:
                        raise TimeoutError('临时ADB延迟')
                    recovered.set()
                return ''
        sleep=asyncio.sleep
        async def quick_sleep(seconds):await sleep(.001)
        monkeypatch.setattr(leases.asyncio,'sleep',quick_sleep)
        lease=leases.DeviceLease(Adb(),'audio');lease.observe('GUARD_PID=123')
        await asyncio.wait_for(recovered.wait(),1)
        await leases.stop_heartbeat(lease.task)
        beats=[x for x in calls if 'case "$E"' in x]
        assert len(beats)>=2
        assert all('[ "$E" -gt "$N" ] || exit 75' in x for x in beats)
        assert leases.TTL==15
    asyncio.run(run())


def capture_renew_command(monkeypatch, lease_path):
    import core.process_lease as leases
    commands = []
    class Adb:
        async def shell(self, command, **kwargs):
            if 'case "$E"' in command:
                commands.append(command)
                raise asyncio.CancelledError
            return ''
    async def run():
        lease = leases.DeviceLease(Adb(), 'test')
        lease.path = str(lease_path)
        lease.observe('GUARD_PID=123')
        with pytest.raises(asyncio.CancelledError):
            await lease.task
        return lease
    monkeypatch.setattr(leases, 'INTERVAL', .001)
    return asyncio.run(run()), commands[0]


def test_renewal_never_exposes_empty_lease_to_live_watchdog(tmp_path, monkeypatch):
    lease_path = tmp_path / 'lease'
    lease, command = capture_renew_command(monkeypatch, lease_path)
    lease_path.write_text(str(int(time.time()) + 15))
    ready, cleaned = tmp_path / 'ready', tmp_path / 'cleaned'
    body = lease.watchdog_body('', 'touch ' + str(cleaned)).replace('sleep 2', 'sleep .005')
    watch = subprocess.Popen(['sh', '-c',
        'cat() { command cat "$@"; touch ' + str(ready) + '; }; ' + body])
    try:
        wait_for(ready.exists)
        # 在写入函数内暂停，此时 shell 已执行重定向，可稳定观察发布窗口。
        subprocess.run(['sh', '-c',
            'echo() { sleep .1; command echo "$@"; }; '
            'printf() { sleep .1; command printf "$@"; }; ' + command], check=True)
        assert watch.poll() is None, '有效续期期间看门狗不应退出或清理设备'
        assert not cleaned.exists()
        assert int(lease_path.read_text()) > int(time.time())
    finally:
        lease_path.unlink(missing_ok=True)
        watch.wait(timeout=3)


@pytest.mark.parametrize('value', [None, '', 'invalid', '1'])
def test_renewal_preserves_missing_or_expired_lease(tmp_path, monkeypatch, value):
    path = tmp_path / 'lease'
    _, command = capture_renew_command(monkeypatch, path)
    if value is not None:
        path.write_text(value)
    result = subprocess.run(['sh', '-c', command])
    assert result.returncode == 75
    assert path.read_text() == value if value is not None else not path.exists()


def test_confirmed_lost_device_lease_stops_heartbeat_immediately(monkeypatch):
    import core.process_lease as leases
    from core.errors import AdbCommandError
    calls = []
    class Adb:
        async def shell(self, command, **kwargs):
            if 'case "$E"' in command:
                calls.append(command)
                raise AdbCommandError('lease lost', returncode=75)
            return ''
    async def run():
        lease = leases.DeviceLease(Adb(), 'traffic')
        lease.observe('GUARD_PID=123')
        await asyncio.wait_for(lease.task, .2)
        assert len(calls) == 1
    monkeypatch.setattr(leases, 'INTERVAL', .001)
    asyncio.run(run())


@pytest.mark.parametrize('remove', [True, False])
def test_renewal_rechecks_current_lease_after_staging(tmp_path, monkeypatch, remove):
    path = tmp_path / 'lease'
    _, command = capture_renew_command(monkeypatch, path)
    path.write_text(str(int(time.time()) + 15))
    change = 'rm -f ' + str(path) if remove else 'command echo 1 > ' + str(path)
    result = subprocess.run(['sh', '-c',
        'printf() { command printf "$@"; ' + change + '; }; ' + command])
    assert result.returncode == 75
    assert not path.exists() if remove else path.read_text().strip() == '1'
    assert not Path(str(path) + '.renew').exists()
