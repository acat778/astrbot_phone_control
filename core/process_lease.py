"""插件心跳租约：本机监护进程与 Android 独立看门狗。"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
import hashlib
import shlex
import re
import os
import signal
import sys
import uuid
import time
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger
from .errors import AdbCommandError

TTL = 15
INTERVAL = 3
ROOT = Path(__file__).resolve().parents[1]


def revision() -> str:
    """代码内容指纹用于区分同版本号下的代码更新。"""
    digest = hashlib.sha256()
    paths = [
        ROOT / "metadata.yaml",
        ROOT / "main.py",
        *sorted((ROOT / "core").rglob("*.py")),
        *sorted((ROOT / "bin").glob("*.py")),
    ]
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def guard_supported() -> bool:
    """本机进程监护器依赖 Linux 的 /proc、fcntl 与 prctl；其他平台直接启动。"""
    return sys.platform.startswith("linux")


def guarded_command(command, resource: Path) -> list[str]:
    """构造带心跳监护的命令；非 Linux 平台退化为直接命令。"""
    if not guard_supported():
        return list(map(str, command))
    return [
        sys.executable,
        str(ROOT / "bin/phone_process_guard.py"),
        "--resource",
        str(resource),
        "--revision",
        revision(),
        "--ttl",
        str(TTL),
        "--",
        *map(str, command),
    ]


def start_heartbeat(process) -> asyncio.Task:
    """任务取消/插件退出时关闭心跳管道，监护器自行结束子进程组。"""

    async def beat():
        if not guard_supported():
            return
        try:
            while process.returncode is None:
                process.stdin.write(b".\n")
                await process.stdin.drain()
                await asyncio.sleep(INTERVAL)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            process.stdin.close()

    task = asyncio.create_task(beat(), name=f"{PLUGIN_NAME}_process_heartbeat")
    task.add_done_callback(lambda _: process.stdin.close())
    return task


async def stop_heartbeat(task) -> None:
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def reclaim_legacy_processes(markers) -> int:
    """回收属于当前数据目录且未受监护器管理的常驻进程（仅 Linux）。"""
    if not guard_supported():
        logger.info("当前平台不支持进程监护与遗留进程回收：跳过")
        return 0
    processes = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        logger.info("读取 /proc 失败，跳过遗留进程回收：%s", exc)
        return 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = entry.joinpath("cmdline").read_bytes().decode().split("\0")
            stat = entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            processes[int(entry.name)] = (argv, int(stat[1]), stat[19])
        except (OSError, UnicodeError, IndexError, ValueError):
            continue

    def guarded(pid):
        seen = set()
        while pid in processes and pid not in seen:
            seen.add(pid)
            argv, parent, _ = processes[pid]
            if any(Path(arg).name == "phone_process_guard.py" for arg in argv):
                return True
            pid = parent
        return False

    owned = set()
    for pid, (argv, _parent, _start) in processes.items():
        text = " ".join(argv)
        executable = any(Path(arg).name in {"mitmdump", "phone-wg"} for arg in argv[:2])
        decoder = (
            len(argv) > 2
            and argv[:2] in (["sh", "-c"], ["/bin/sh", "-c"])
            and "scrcpy_feed.py" in text
        )
        tokens = argv
        if decoder:
            try:
                tokens = shlex.split(argv[2])
            except ValueError:
                continue
        matches = any(
            str(marker) in tokens or f"confdir={marker}" in tokens for marker in markers
        )
        if (
            pid != os.getpid()
            and (executable or decoder)
            and matches
            and not guarded(pid)
        ):
            owned.add(pid)
    for _ in range(len(processes)):
        children = {pid for pid, (_, parent, _) in processes.items() if parent in owned}
        if children <= owned:
            break
        owned.update(children)

    def still_same(pid):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return stat[19] == processes[pid][2] and stat[0] != "Z"
        except (OSError, IndexError):
            return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in owned:
            if still_same(pid):
                with suppress(ProcessLookupError):
                    os.kill(pid, sig)
        if sig == signal.SIGTERM and owned:
            await asyncio.sleep(1)
    if owned:
        logger.info("已回收当前插件数据目录的 %d 个未托管子进程", len(owned))
    return len(owned)


class DeviceLease:
    """Android 端看门狗仅持有本次子进程 PID，插件定时刷新到期时间。"""

    def __init__(self, adb, name: str):
        self.adb = adb
        self.name = name
        self.path = (
            f"/data/local/tmp/phone-lease-{name}-{revision()[:12]}-{uuid.uuid4().hex}"
        )
        self.task = None
        self.watchdog_pid = None

    async def prepare(self) -> None:
        prefix = self.path.rsplit("-", 2)[0]
        previous = await self.adb.shell(f"ls {prefix}-* 2>/dev/null || true", timeout=5)
        paths = [
            line.strip()
            for line in str(previous).splitlines()
            if line.strip().startswith(prefix + "-") and " " not in line.strip()
        ]
        if paths:
            await self.adb.shell(
                "rm -f " + " ".join(map(shlex.quote, paths)), timeout=5
            )
            # 看门狗最多 2 秒一次检查，再给强制退出和清理留 2 秒。
            await asyncio.sleep(4)

    def watchdog_body(self, pid: str, cleanup: str = "") -> str:
        if pid:
            identity = f"$(awk '{{print $22}}' /proc/{pid}/stat 2>/dev/null)"
            alive = (
                f"kill -0 {pid} 2>/dev/null && "
                f'[ "$(awk \'{{print $22}}\' /proc/{pid}/stat 2>/dev/null)" = "$IDENTITY" ]'
            )
            terminate = (
                f"if {alive}; then kill {pid} 2>/dev/null; sleep 1; "
                f"if {alive}; then kill -9 {pid} 2>/dev/null; fi; fi; "
            )
        else:
            identity, alive, terminate = "", "true", ""
        return (
            f'IDENTITY="{identity}"; while {alive}; do '
            f"E=$(cat {shlex.quote(self.path)} 2>/dev/null); "
            'case "$E" in ""|*[!0-9]*) break;; esac; '
            '[ "$(date +%s)" -lt "$E" ] || break; sleep 2; done; '
            + terminate
            + (cleanup or "true")
            + f"; rm -f {shlex.quote(self.path)}"
        )

    def wrap_launch(self, command: str) -> str:
        """服务与看门狗在同一条设备命令内创建，宿主在启动中退出也有到期约束。"""
        marker = "& echo PID=$!"
        if marker not in command:
            raise ValueError("设备启动命令缺少 PID 标记")
        # 子 shell 继承本次服务 PID；看门狗不依赖宿主的 adb 连接。
        body = self.watchdog_body("$1")
        replacement = (
            f"& P=$!; echo PID=$P; echo $(( $(date +%s) + {TTL} )) > {self.path}; "
            f'sh -c {shlex.quote(body)} phone-watch "$P" < /dev/null > /dev/null 2>&1 & echo GUARD_PID=$!'
        )
        return command.replace(marker, replacement)

    async def arm(self, pid: int | None, *, cleanup: str = "") -> None:
        body = self.watchdog_body(str(int(pid)) if pid else "", cleanup)
        output = await self.adb.shell(
            f"echo $(( $(date +%s) + {TTL} )) > {self.path}; sh -c {shlex.quote(body)} "
            "< /dev/null > /dev/null 2>&1 & echo GUARD_PID=$!",
            timeout=10,
        )
        self.observe(output)

    def observe(self, output) -> None:
        match = re.search(r"GUARD_PID=(\d+)", str(output))
        if not match:
            raise RuntimeError("设备子进程看门狗未返回 PID")
        self.watchdog_pid = int(match.group(1))

        async def beat():
            last_success = time.monotonic()
            failures = 0
            try:
                while True:
                    await asyncio.sleep(1 if failures else INTERVAL)
                    if time.monotonic() - last_success >= TTL - 2:
                        logger.warning("设备心跳未在租约期限内恢复（%s），等待子进程退出", self.name)
                        break
                    command = (
                        f"E=$(cat {shlex.quote(self.path)} 2>/dev/null); N=$(date +%s); "
                        'case "$E" in ""|*[!0-9]*) exit 75;; esac; '
                        '[ "$E" -gt "$N" ] || exit 75; '
                        # 同目录原子替换使看门狗始终读取完整到期时间。
                        f"T={shlex.quote(self.path + '.renew')}; "
                        "trap 'rm -f \"$T\"' EXIT; "
                        f"printf '%s\\n' $((N + {TTL})) > \"$T\" || exit 74; "
                        f"C=$(cat {shlex.quote(self.path)} 2>/dev/null); "
                        '[ "$C" = "$E" ] && [ "$C" -gt "$(date +%s)" ] || exit 75; '
                        f'mv -f "$T" {shlex.quote(self.path)}'
                    )
                    try:
                        await self.adb.shell(command, timeout=3)
                    except Exception as exc:
                        if isinstance(exc, AdbCommandError) and exc.returncode == 75:
                            logger.warning("设备租约已失效（%s），停止续期：%s", self.name, exc)
                            break
                        failures += 1
                        logger.warning(
                            "设备心跳暂时失败（%s，%s），租约期内重试：%s", self.name, failures, exc
                        )
                        continue
                    if failures:
                        logger.info("设备心跳已恢复，连续失败次数=%s", failures)
                    failures = 0
                    last_success = time.monotonic()
            finally:
                with suppress(Exception):
                    await self.adb.shell(f"rm -f {self.path}", timeout=5)

        self.task = asyncio.create_task(
            beat(), name=f"{PLUGIN_NAME}_device_heartbeat"
        )

    async def close(self):
        await stop_heartbeat(self.task)
        self.task = None
        await self.adb.shell(f"rm -f {self.path}", timeout=5)
        if self.watchdog_pid:
            await self.adb.shell(
                f"N=0; while kill -0 {self.watchdog_pid} 2>/dev/null && [ $N -lt 40 ]; "
                "do sleep 0.1; N=$((N+1)); done",
                timeout=6,
            )
            self.watchdog_pid = None
