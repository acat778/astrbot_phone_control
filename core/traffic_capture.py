"""流量抓包：mitmproxy 回环服务、ADB 或组网传输与 CA 信任管理。

手机代理通过 ADB reverse 或 Tailscale/ZeroTier 组网地址连接服务端。
系统 CA 模式使用 adb root 注入证书；手动模式由使用者或 App 信任公开 CA，
可在无 root 设备使用 HTTP，HTTPS 解密取决于 App 的证书信任策略。

启动、停止与设备保活由 asyncio 锁串行化；子进程和设备代理持有心跳租约。
停止或失败回滚撤回本实例代理与转发，保留其他反向映射和后续代理设置。
确定性不可用记录原因，临时失败按指数退避重试。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .process_lease import DeviceLease, guarded_command, start_heartbeat, stop_heartbeat

from . import traffic as traffic_mod

REMOTE_TMP = "/data/local/tmp/astrbot_phone_traffic"
SYSTEM_CACERTS = "/system/etc/security/cacerts"
CA_WAIT_SECONDS = 20.0
ROOT_SETTLE_SECONDS = 2.0
PROXY_VALUE = "127.0.0.1:{port}"

# 临时失败退避：首次 30 秒，之后翻倍，最多 10 分钟；连续失败达到上限后按
# 确定性不可用处理并停止自动重试。
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 600.0
RETRY_FAILURE_LIMIT = 4

# 设备环境配置失败中属于「本次插件运行内重试不会恢复」的错误特征：设备无 root、
# 系统证书绑定挂载被拒绝/无权限、容器缺少 openssl 等。adb transport 抖动、
# mount 源目录临时缺失（并发清理竞态）等不在此列，按临时失败退避重试。
_DETERMINISTIC_FAILURE_MARKERS = (
    "设备无 root",
    "系统 ca 绑定挂载失败",
    "nsenter/mount 被拒绝",
    "operation not permitted",
    "permission denied",
    "无法计算 ca 证书哈希",
)


class TrafficCapture:
    """管理容器内 mitmdump 与手机侧 CA/代理/reverse 的抓包生命周期。"""

    def __init__(
        self,
        *,
        adb: Any,
        data_dir: str | Path,
        logger: Any = None,
        mitmdump: str = "",
        keep_packages_provider=None,
        settings_provider=None,
        mesh=None,
        manage_device=True,
    ) -> None:
        self.adb = adb
        self._manage_device = manage_device
        self.root = Path(data_dir) / "traffic"
        self.confdir = self.root / "mitm"
        self.log_path = self.root / traffic_mod.FLOW_LOG_NAME
        self.addon_path = self.root / traffic_mod.ADDON_NAME
        self.mitm_log_path = self.root / "mitmdump.log"
        self._logger = logger
        self._mitmdump = mitmdump
        self._keep_packages_provider = keep_packages_provider
        self._settings_provider = settings_provider
        self.mesh = mesh
        self._transport = "adb"
        self._ca_mode = "system"
        self._proxy_value = ""
        self._mesh_proxy = False
        self._device_lease = None
        self._process_heartbeat = None
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_log = None
        self._port = 0
        self._body_chars = traffic_mod.DEFAULT_BODY_CHARS
        self._prev_proxy = ""
        self._last_error = ""
        self._started_at = 0.0
        # 设备无 root 时抓包不可用：本次插件运行内跳过重试，避免反复 adb root/告警。
        self._root_unavailable = False
        # 确定性不可用原因：非空时 start 直接返回该原因，保活循环不再反复重试。
        self._unavailable_reason = ""
        # 临时失败退避与连续失败计数。
        self._failures = 0
        self._next_retry_at = 0.0
        # 本实例是否改动过设备代理/CA/reverse（stop/回滚时据此恢复设备状态）。
        self._device_configured = False
        # 串行化 start/stop/ensure_device/设备环境配置，避免并发清理互相打断。
        self._lock = asyncio.Lock()

    # ---- 基础工具 -----------------------------------------------------
    def _log(self, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger(message)
            except Exception:  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def root_unavailable(self) -> bool:
        """设备无 root、本次运行内已跳过抓包。"""
        return self._root_unavailable

    @property
    def unavailable(self) -> bool:
        """确定性不可用（本次插件运行内重试不会恢复）。"""
        return bool(self._unavailable_reason)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    @property
    def retry_ready(self) -> bool:
        """临时失败退避是否到期（不可用状态永远为 False）。"""
        return not self._unavailable_reason and time.monotonic() >= self._next_retry_at

    @property
    def next_retry_in(self) -> float:
        return max(0.0, self._next_retry_at - time.monotonic())

    @property
    def port(self) -> int:
        return self._port

    def _find_mitmdump(self) -> str:
        if self._mitmdump:
            found = shutil.which(self._mitmdump)
            if found:
                return found
            candidate = Path(self._mitmdump).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            return ""
        found = shutil.which("mitmdump")
        if found:
            return found
        candidate = Path(sys.executable).with_name("mitmdump")
        if candidate.is_file():
            return str(candidate)
        return ""

    def _subject_hash_old(self, ca_path: Path) -> str:
        """用 openssl 计算 Android CA 文件名（subject_hash_old）。"""
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "PEM",
                    "-subject_hash_old",
                    "-in",
                    str(ca_path),
                    "-noout",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        line = (result.stdout or "").strip().splitlines()
        return line[0].strip() if line else ""

    # ---- 失败状态机 ---------------------------------------------------
    def _failure_result(
        self, reason: str, *, unavailable: bool = False
    ) -> dict[str, Any]:
        """构造失败结果；``reason`` 保证为非空字符串。"""
        text = str(reason or "").strip() or "抓包启动失败（未提供具体原因）"
        return {
            "ok": False,
            "running": self.running,
            "port": self._port,
            "reason": text,
            "unavailable": bool(unavailable or self._unavailable_reason),
            "root_unavailable": self._root_unavailable,
            "retry_in": round(self.next_retry_in, 1),
        }

    def _fail_locked(self, reason: str, *, unavailable: bool = False) -> dict[str, Any]:
        """记录一次失败并按确定性/临时性更新状态。"""
        self._note_failure(reason, unavailable=unavailable)
        return self._failure_result(reason, unavailable=self.unavailable)

    def _note_failure(self, reason: str, *, unavailable: bool = False) -> None:
        self._last_error = reason
        if unavailable:
            self._unavailable_reason = reason
            if "设备无 root" in reason or "adb root 失败" in reason:
                self._root_unavailable = True
            return
        self._failures += 1
        if self._failures >= RETRY_FAILURE_LIMIT:
            self._unavailable_reason = (
                f"连续 {self._failures} 次失败，本次运行不再自动重试：{reason}"
            )
            return
        delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (self._failures - 1)))
        self._next_retry_at = time.monotonic() + delay

    def _note_success(self) -> None:
        self._last_error = ""
        self._failures = 0
        self._next_retry_at = 0.0

    def reset_unavailable(self) -> None:
        """清除不可用标记/退避，允许再次尝试（配置变化或用户重新开启抓包后调用）。"""
        if (
            not self._unavailable_reason
            and not self._failures
            and not self._next_retry_at
        ):
            return
        previous = self._unavailable_reason
        self._unavailable_reason = ""
        self._root_unavailable = False
        self._failures = 0
        self._next_retry_at = 0.0
        self._last_error = ""
        if previous:
            self._log(f"抓包：已清除不可用标记，允许重新尝试（原原因：{previous}）")

    @staticmethod
    def _is_deterministic_setup_failure(exc: BaseException) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in _DETERMINISTIC_FAILURE_MARKERS)

    # ---- 设备状态 -----------------------------------------------------
    async def _clear_device_proxy(self) -> None:
        with suppress(Exception):
            await self.adb.shell("settings put global http_proxy :0", timeout=10)

    async def _cleanup_device_state_locked(self, *, port: int | None = None) -> None:
        """清理上一次运行可能残留的代理/reverse/CA 挂载（失败静默）。"""
        target_port = int(port or self._port or traffic_mod.DEFAULT_TRAFFIC_PORT)
        if self._transport == "adb":
            with suppress(Exception):
                await self.adb.run(
                    ["reverse", "--remove", f"tcp:{target_port}"], timeout=10
                )
        current = (
            await self.adb.shell("settings get global http_proxy", timeout=10)
        ).strip()
        if current == PROXY_VALUE.format(port=target_port) or (
            self._proxy_value and current == self._proxy_value
        ):
            await self._clear_device_proxy()
        if self._ca_mode == "system":
            with suppress(Exception):
                await self.adb.shell(
                    "nsenter -t 1 -m -- umount -l "
                    + SYSTEM_CACERTS
                    + " 2>/dev/null; true",
                    timeout=15,
                )

    async def cleanup_device_state(self, *, port: int | None = None) -> None:
        """清理设备侧遗留抓包状态（串行对外入口）。"""
        async with self._lock:
            await self._cleanup_device_state_locked(port=port)

    async def _wait_root(self, attempts: int = 30) -> bool:
        for _ in range(attempts):
            try:
                uid = (await self.adb.shell("id -u", timeout=5)).strip()
            except Exception:  # noqa: BLE001 - adb root 重启 adbd 期间会短暂失败
                await asyncio.sleep(0.5)
                continue
            if uid == "0":
                return True
            await asyncio.sleep(0.5)
        return False

    async def _ensure_reverse(self, attempts: int = 4) -> bool:
        """确保 adb reverse 存在（adb root/adbd 重启可能把 reverse 清掉）。"""
        args = ["reverse", f"tcp:{self._port}", f"tcp:{self._port}"]
        for _ in range(max(1, attempts)):
            try:
                await self.adb.run(args, timeout=15)
            except Exception:  # noqa: BLE001 - 重试后仍失败由调用方处理
                await asyncio.sleep(0.8)
                continue
            try:
                listing = await self.adb.run(["reverse", "--list"], timeout=10)
            except Exception:  # noqa: BLE001
                listing = ""
            if f"tcp:{self._port}" in listing:
                return True
            await asyncio.sleep(0.8)
        return False

    async def _force_stop_apps(self) -> int:
        """按统一保留规则重启应用，使其读取注入的系统 CA。"""
        from .background import DEFAULT_KEEP_PACKAGES, clean_background

        whitelist = (
            self._keep_packages_provider()
            if self._keep_packages_provider
            else DEFAULT_KEEP_PACKAGES
        )
        report = await clean_background(self.adb, whitelist=whitelist)
        if report["errors"]:
            self._log("抓包初始化清理异常：" + "；".join(report["errors"]))
        return report["stopped"]

    async def _setup_device_locked(self) -> None:
        """按所选 CA 信任方式与传输通道配置手机代理（调用方持锁）。"""
        # 设备重配置先撤回前一看门狗，防止旧心跳在代理重建后继续存活。
        if self._device_lease is not None and self._device_lease.task is not None:
            await self._device_lease.close()
            self._device_lease = DeviceLease(self.adb, "traffic")
            await self._device_lease.prepare()
        ca_path = self.confdir / traffic_mod.CA_FILE_NAME
        cert_hash = (
            await asyncio.to_thread(self._subject_hash_old, ca_path)
            if self._ca_mode == "system" else ""
        )
        if self._ca_mode == "system" and not cert_hash:
            raise RuntimeError("无法计算 CA 证书哈希（容器缺少 openssl）")
        # 首次配置时记录设备原有代理，stop 时恢复；保活重配置保留首次记录，
        # 否则会把「指向本实例 mitmdump 的代理」当成原始值。
        if not self._device_configured:
            previous = ""
            with suppress(Exception):
                previous = (
                    await self.adb.shell("settings get global http_proxy", timeout=10)
                ).strip()
            if previous.lower() in {"", "null", ":0"}:
                self._prev_proxy = ""
            else:
                self._prev_proxy = previous
        # 从这里开始本实例会改动设备状态，失败回滚/停止时统一恢复。
        self._device_configured = True
        # 先清掉可能残留的旧挂载/代理，避免重复绑定失败
        await self._cleanup_device_state_locked()
        if self._ca_mode == "system":
            with suppress(Exception):
                await self.adb.run(["root"], timeout=20)
            # adb root 会让 adbd 重启：等它完成后再建 reverse，否则 reverse 会被清掉。
            await asyncio.sleep(ROOT_SETTLE_SECONDS)
            if not await self._wait_root():
                # 设备无 root 属确定性不可用：直接记录原因，避免保活循环反复重试。
                reason = "设备无 root，已跳过抓包（adb root 失败）"
                self._root_unavailable = True
                self._unavailable_reason = reason
                self._last_error = reason
                raise RuntimeError(reason)
            remote_ca = f"{REMOTE_TMP}/cacerts/{cert_hash}.0"
            await self.adb.shell(
                f"rm -rf {REMOTE_TMP}; mkdir -p {REMOTE_TMP}/cacerts; "
                f"cp -a {SYSTEM_CACERTS}/. {REMOTE_TMP}/cacerts/",
                timeout=40,
            )
            await self.adb.run(["push", str(ca_path), remote_ca], timeout=30)
            await self.adb.shell(f"chmod 644 {remote_ca}", timeout=10)
            await self.adb.shell(
                f"nsenter -t 1 -m -- mount --bind {REMOTE_TMP}/cacerts {SYSTEM_CACERTS}",
                timeout=30,
            )
            visible = await self.adb.shell(
                f"ls {SYSTEM_CACERTS}/{cert_hash}.0", timeout=10
            )
            if cert_hash not in visible:
                raise RuntimeError("系统 CA 绑定挂载失败（nsenter/mount 被拒绝）")
            # 已运行的 App 进程不会重新加载系统 CA：先结束它们，保证新进程能解密。
            stopped = await self._force_stop_apps()
            if stopped:
                self._log(
                    f"抓包：已结束 {stopped} 个第三方 App 进程，确保新进程信任 CA"
                )
        if self._transport == "mesh":
            if self.mesh is None:
                raise RuntimeError("未配置组网抓包管理器")
            self._proxy_value = await self.mesh.expose_proxy(self._port)
            self._mesh_proxy = True
        else:
            if not await self._ensure_reverse():
                raise RuntimeError("adb reverse 建立失败（端口映射不可用）")
            self._proxy_value = PROXY_VALUE.format(port=self._port)
        await self._arm_device_lease_locked()
        await self.adb.shell(
            f"settings put global http_proxy {self._proxy_value}", timeout=10
        )
        if self._transport == "adb" and not await self._ensure_reverse(attempts=2):
            raise RuntimeError("adb reverse 在设置代理后丢失，端口映射不可用")

    async def _setup_device(self) -> None:
        """配置设备抓包环境（串行；start/保活内部使用持锁版本）。"""
        async with self._lock:
            await self._setup_device_locked()

    async def _arm_device_lease_locked(self) -> None:
        if (
            self._device_lease is not None
            and self._device_lease.task is not None
            and not self._device_lease.task.done()
        ):
            return
        if self._device_lease is None:
            self._device_lease = DeviceLease(self.adb, "traffic")
        import shlex

        proxy = shlex.quote(self._prev_proxy or ":0")
        expected = shlex.quote(self._proxy_value or PROXY_VALUE.format(port=self._port))
        cleanup = (
            f'if [ "$(settings get global http_proxy)" = {expected} ]; then '
            f"settings put global http_proxy {proxy}; "
            f"true; fi; nsenter -t 1 -m -- umount -l {SYSTEM_CACERTS} 2>/dev/null; true"
        )
        if self._ca_mode == "manual":
            cleanup = cleanup.split("; nsenter", 1)[0] + "; true"
        await self._device_lease.arm(None, cleanup=cleanup)

    async def _restore_device_locked(self) -> None:
        """撤回本次代理与转发，保留调用者后续改动。"""
        if self._transport == "adb" and self._port:
            with suppress(Exception):
                await self.adb.run(
                    ["reverse", "--remove", f"tcp:{self._port}"], timeout=10
                )
        with suppress(Exception):
            current = (
                await self.adb.shell("settings get global http_proxy", timeout=10)
            ).strip()
            if current in {
                self._proxy_value,
                PROXY_VALUE.format(port=self._port),
                "null",
                "",
                ":0",
            }:
                import shlex

                await self.adb.shell(
                    "settings put global http_proxy "
                    + shlex.quote(self._prev_proxy or ":0"),
                    timeout=10,
                )
        if self._ca_mode == "system":
            with suppress(Exception):
                await self.adb.shell(
                    "nsenter -t 1 -m -- umount -l "
                    + SYSTEM_CACERTS
                    + " 2>/dev/null; true",
                    timeout=15,
                )
        if self._mesh_proxy and self.mesh is not None:
            await self.mesh.hide_proxy()
            self._mesh_proxy = False

    async def _teardown_process_locked(self) -> None:
        """停止并清理本实例的 mitmdump 进程/日志句柄（调用方持锁）。"""
        await stop_heartbeat(self._process_heartbeat)
        self._process_heartbeat = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=3)
        if self._proc_log is not None:
            with suppress(Exception):
                self._proc_log.close()
            self._proc_log = None

    async def _cleanup_owned_locked(self):
        failure = None
        if self._device_lease is not None:
            try:
                await self._device_lease.close()
            except Exception as exc:
                failure = exc
            finally:
                self._device_lease = None
        if self._device_configured:
            try:
                await self._restore_device_locked()
            except Exception as exc:
                failure = failure or exc
            finally:
                self._device_configured = False
        try:
            await self._teardown_process_locked()
        finally:
            self._prev_proxy = ""
        if failure is not None:
            raise RuntimeError("抓包清理未全部完成，请检查代理与组网端口状态") from failure

    async def _rollback_start_locked(self) -> None:
        """启动失败/取消时完整回收本次进程、代理与转发。"""
        await self._cleanup_owned_locked()

    # ---- 生命周期 -----------------------------------------------------
    async def start(
        self,
        *,
        port: int | None = None,
        body_chars: int | None = None,
    ) -> dict[str, Any]:
        """启动抓包（幂等、串行）：返回 ``{"ok","reason",...}``。

        运行中再次调用直接返回当前状态，不重复 spawn/清理；失败返回的
        ``reason`` 一定非空，并带 ``unavailable``/``retry_in`` 供上层决定是否重试。
        """
        async with self._lock:
            try:
                return await self._start_locked(port=port, body_chars=body_chars)
            except asyncio.CancelledError:
                await self._rollback_start_locked()
                raise
            except Exception as exc:  # noqa: BLE001 - 未预期异常也要给出明确原因
                await self._rollback_start_locked()
                return self._fail_locked(f"{type(exc).__name__}: 抓包启动异常：{exc}")

    async def _start_locked(
        self,
        *,
        port: int | None = None,
        body_chars: int | None = None,
    ) -> dict[str, Any]:
        if self.running:
            return self.status()
        if self._unavailable_reason:
            return self._failure_result(self._unavailable_reason, unavailable=True)
        settings = self._settings_provider() if self._settings_provider else None
        self._transport = getattr(settings, "transport", "adb")
        self._ca_mode = getattr(settings, "ca_mode", "system")
        target_port = int(port or traffic_mod.DEFAULT_TRAFFIC_PORT)
        # 启动前先清理设备侧可能残留的代理/reverse/CA：插件进程异常退出后兜底，
        # 避免设备代理指向并不存在的 mitmdump 而断网。清理失败静默，不影响启动判定。
        if self._manage_device:
            await self._cleanup_device_state_locked(port=target_port)
        if not 1024 <= target_port <= 65535:
            return self._fail_locked(
                f"ValueError: 端口配置非法：{target_port}（应在 1024–65535）",
                unavailable=True,
            )
        mitmdump = self._find_mitmdump()
        if not mitmdump:
            return self._fail_locked(
                "RuntimeError: 未找到 mitmdump：请用独立 venv 安装 mitmproxy 后，"
                "把 traffic.mitmdump_path 指向该 venv/bin/mitmdump",
                unavailable=True,
            )
        # 每次重启先结束前任设备租约，再关闭本机进程句柄与日志。
        if self._device_lease is not None:
            await self._device_lease.close()
            self._device_lease = None
        await self._teardown_process_locked()
        self._port = target_port
        self._body_chars = traffic_mod.normalize_body_chars(
            body_chars if body_chars is not None else self._body_chars
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.confdir.mkdir(parents=True, exist_ok=True)
            self.addon_path.write_text(
                traffic_mod.build_addon_script(self.log_path, self._body_chars),
                encoding="utf-8",
            )
            traffic_mod.prune_log(self.log_path)
        except OSError as exc:
            return self._fail_locked(f"{type(exc).__name__}: 抓包目录准备失败：{exc}")
        try:
            self._proc_log = self.mitm_log_path.open("ab")
            command = [
                mitmdump,
                "-q",
                "--listen-host",
                "127.0.0.1",
                "-p",
                str(target_port),
                "--set",
                f"confdir={self.confdir}",
                "-s",
                str(self.addon_path),
            ]
            self._proc = await asyncio.create_subprocess_exec(
                *guarded_command(command, self.root / "process.lock"),
                stdin=asyncio.subprocess.PIPE,
                stdout=self._proc_log,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "HOME": str(self.root)},
            )
            self._process_heartbeat = start_heartbeat(self._proc)
        except (OSError, asyncio.SubprocessError) as exc:
            await self._teardown_process_locked()
            return self._fail_locked(
                f"{type(exc).__name__}: 启动 mitmdump 失败：{exc}",
                unavailable=isinstance(
                    exc, (FileNotFoundError, PermissionError, NotADirectoryError)
                ),
            )
        # 插件失联时，设备端看门狗恢复代理，避免手机网络指向已退出的抓包服务。
        # 等 CA 生成
        ca_path = self.confdir / traffic_mod.CA_FILE_NAME
        for _ in range(int(CA_WAIT_SECONDS * 2)):
            if ca_path.is_file() or self._proc.returncode is not None:
                break
            await asyncio.sleep(0.5)
        if not ca_path.is_file():
            tail = self._tail_mitm_log()
            await self._teardown_process_locked()
            return self._fail_locked(
                f"RuntimeError: mitmdump 未生成 CA 证书（端口 {target_port}）：{tail}"
            )
        if self._proc.returncode is not None:
            # 进程已退出（典型：端口被其它进程占用）时不必再配置设备。
            tail = self._tail_mitm_log()
            await self._teardown_process_locked()
            return self._fail_locked(
                f"RuntimeError: mitmdump 启动后立即退出（端口 {target_port}）：{tail}"
            )
        self._device_lease = DeviceLease(self.adb, "traffic") if self._manage_device else None
        if self._device_lease is not None:
            await self._device_lease.prepare()
        try:
            if self._manage_device:
                await self._setup_device_locked()
        except Exception as exc:  # noqa: BLE001 - 设备环境配置失败时整体回滚
            reason = f"{type(exc).__name__}: 设备抓包环境配置失败：{exc}"
            deterministic = self._is_deterministic_setup_failure(exc) or (
                self._root_unavailable
            )
            await self._rollback_start_locked()
            return self._fail_locked(reason, unavailable=deterministic)
        if self._device_lease is not None and self._device_lease.task is None:
            await self._arm_device_lease_locked()
        if not self.running:
            # 设备配置期间 mitmdump 已退出（例如端口被其它实例占用）：
            # 不能先报「已启动」，如实返回失败原因并回滚。
            tail = self._tail_mitm_log()
            await self._rollback_start_locked()
            return self._fail_locked(
                f"RuntimeError: mitmdump 在设备配置阶段退出（端口 {target_port}）：{tail}"
            )
        self._started_at = time.time()
        self._note_success()
        # 所有设备配置完成后才输出成功日志，避免「先报成功再报失败」。
        self._log(
            f"抓包已启动：端口 {self._port}，流量日志 {self.log_path}；"
            + (
                "插件回环代理已就绪"
                if not self._manage_device
                else "系统 CA 已注入" if self._ca_mode == "system"
                else "手动 CA 模式：HTTPS 需 App 信任抓包 CA"
            )
        )
        return self.status()

    def _tail_mitm_log(self, limit: int = 400) -> str:
        try:
            data = self.mitm_log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return "（无日志）"
        return data.strip()[-limit:] or "（无日志）"

    async def stop(self) -> None:
        """停止抓包并恢复设备代理/证书挂载（幂等、串行）。"""
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self, *, log: bool = True) -> None:
        configured = self._device_configured
        had_capture = self.running
        await self._cleanup_owned_locked()
        if log and (configured or had_capture):
            self._log("抓包已停止，设备代理/CA 挂载已恢复" if configured else "抓包已停止")

    async def ensure_device(self) -> None:
        """按传输与 CA 模式检查代理健康，连接中断时撤回（串行）。"""
        async with self._lock:
            await self._ensure_device_locked()

    async def _ensure_device_locked(self) -> None:
        if not self.running or self._unavailable_reason:
            return
        traffic_mod.prune_log(self.log_path)
        proxy = ""
        with suppress(Exception):
            proxy = (
                await self.adb.shell("settings get global http_proxy", timeout=8)
            ).strip()
        expected = self._proxy_value or PROXY_VALUE.format(port=self._port)
        reverse_ok = self._transport == "mesh"
        if self._transport == "mesh":
            if self.mesh is None or not (await self.mesh.status())["ready"]:
                self._note_failure("组网连接中断，已撤回手机代理")
                await self._stop_locked(log=False)
                return
        else:
            with suppress(Exception):
                listing = await self.adb.run(["reverse", "--list"], timeout=8)
                reverse_ok = f"tcp:{self._port}" in listing
        root_ok = self._ca_mode == "manual"
        if self._ca_mode == "system":
            with suppress(Exception):
                root_ok = (await self.adb.shell("id -u", timeout=8)).strip() == "0"
        if proxy == expected and root_ok and not reverse_ok:
            # 只有 reverse 丢失（adb 重连/transport 变化）时只重建端口映射，
            # 不重启 App，避免打断正在进行的玩手机任务。
            self._log("抓包保活：reverse 丢失，重新建立端口映射")
            with suppress(Exception):
                await self._ensure_reverse()
            return
        if proxy != expected or not reverse_ok or not root_ok:
            self._log(
                "抓包保活：设备代理/reverse/root 状态异常"
                f"（proxy={proxy or '空'} reverse={reverse_ok} root={root_ok}），重新配置"
            )
            try:
                await self._setup_device_locked()
            except Exception as exc:  # noqa: BLE001 - 保活重配置失败按失败策略处理
                reason = f"{type(exc).__name__}: 设备抓包环境保活重配置失败：{exc}"
                deterministic = self._is_deterministic_setup_failure(exc) or (
                    self._root_unavailable
                )
                self._note_failure(reason, unavailable=deterministic)
                # 恢复设备并停掉进程，交给上层按退避策略重启。
                await self._stop_locked(log=False)
                return
        if self._proc is not None and self._proc.returncode is not None:
            self._note_failure("RuntimeError: mitmdump 意外退出")
            await self._stop_locked(log=False)

    def status(self) -> dict[str, Any]:
        """返回抓包状态（不访问设备，供工具/命令快速展示）。"""
        flows = 0
        try:
            if self.log_path.exists():
                with self.log_path.open("rb") as handle:
                    flows = sum(1 for _ in handle)
        except OSError:
            flows = 0
        return {
            "ok": self.running,
            "running": self.running,
            "port": self._port,
            "provider": "mitmproxy",
            "transport": self._transport,
            "ca_mode": self._ca_mode,
            "proxy_address": self._proxy_value,
            "flows": flows,
            "log_path": str(self.log_path),
            "mitmdump": self._find_mitmdump() or "",
            "error": self._last_error,
            "reason": self._unavailable_reason or self._last_error,
            "unavailable": bool(self._unavailable_reason),
            "unavailable_reason": self._unavailable_reason,
            "root_unavailable": self._root_unavailable,
            "started_at": self._started_at,
            "failures": self._failures,
            "retry_in": round(self.next_retry_in, 1),
        }

    def status_text(self) -> str:
        status = self.status()
        if status["running"]:
            text = f"抓包：已开启（{status['transport']}，代理 {status['proxy_address']}，CA {status['ca_mode']}，已记录 {status['flows']} 条流量）"
        elif status["unavailable"]:
            text = (
                "抓包：不可用（"
                f"{status['unavailable_reason']}）；本次运行不再自动重试，"
                "可在 /phone traffic 开关或修改配置后重新尝试"
            )
        elif status["error"]:
            retry = (
                f"，约 {int(status['retry_in'])} 秒后重试"
                if status.get("retry_in", 0) > 0
                else ""
            )
            text = f"抓包：未开启（{status['error']}{retry}）"
        else:
            text = "抓包：未开启"
        return text

    def recent(
        self, *, limit: int = traffic_mod.DEFAULT_LIMIT, since_seconds: int = 0
    ) -> list[dict[str, Any]]:
        return traffic_mod.load_recent_flows(
            self.log_path, limit=limit, since_seconds=since_seconds
        )
