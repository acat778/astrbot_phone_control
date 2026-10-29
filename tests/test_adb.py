"""core/adb.py 单元测试。

真实设备用例标记 ``integration``，仅在设置 ``PHONE_ADB_HOST`` 环境变量时执行。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import struct
import sys
import time
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.adb as adb_module  # noqa: E402

from core.adb import (  # noqa: E402
    PNG_MIN_BYTES,
    AdbClient,
    _convert_raw_with_pillow,
    _parse_battery,
    _parse_screen_on,
    parse_foreground_dump,
    parse_screencap_raw,
    repair_png_bytes,
    validate_png_bytes,
)
from core.errors import (  # noqa: E402
    AdbCommandError,
    AdbConnectError,
    AdbNotConfiguredError,
    DeviceOfflineError,
)

SERIAL = "192.168.250.10:16448"


def run(coro):
    """在独立事件循环中执行协程（测试环境未安装 pytest-asyncio）。"""
    return asyncio.run(coro)


def make_client(tmp_path: Path, **overrides) -> AdbClient:
    """构造使用当前 Python 解释器充当 adb 的测试客户端。"""
    kwargs = {
        "adb_path": sys.executable,
        "host": "192.168.250.10",
        "port": 16448,
        "home_dir": tmp_path / "adbhome",
        "connect_timeout": 1.0,
        "command_timeout": 2.0,
    }
    kwargs.update(overrides)
    return AdbClient(**kwargs)


class FakeRunner:
    """替换 ``AdbClient._run_adb``：记录调用并按 handler 返回预设结果。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[list[str], float | None]] = []

    async def __call__(self, argv, *, timeout=None):
        self.calls.append((list(argv), timeout))
        result = self.handler(list(argv))
        if inspect.isawaitable(result):
            result = await result
        return result

    def flat_calls(self) -> list[str]:
        return [" ".join(argv) for argv, _timeout in self.calls]


class FakeClock:
    """按脚本推进的 ``perf_counter``，用于模拟慢链路下各阶段耗时。"""

    def __init__(self, *stamps: float) -> None:
        self._stamps = list(stamps) or [0.0]
        self._index = 0

    def perf_counter(self) -> float:
        value = self._stamps[min(self._index, len(self._stamps) - 1)]
        self._index += 1
        return value


def fake_time(*stamps: float):
    """构造只提供 ``perf_counter`` 的 time 替身（仅替换 adb 模块内的引用）。"""
    from types import SimpleNamespace

    return SimpleNamespace(perf_counter=FakeClock(*stamps).perf_counter)


def standard_handler(argv):
    """模拟一台在线设备的标准输出。"""
    flat = " ".join(argv)
    if "start-server" in flat:
        return 0, b"", b""
    if " connect " in flat:
        return 0, b"already connected to 192.168.250.10:16448", b""
    if flat.endswith(" devices") or " devices" in flat:
        return 0, f"List of devices attached\n{SERIAL}\tdevice\n".encode(), b""
    if "shell getprop" in flat:
        return 0, b"23116PN5BC\nXiaomi\n12\n32\n", b""
    if "shell wm size; wm density" in flat:
        return 0, b"Physical size: 900x1600\nPhysical density: 320\n", b""
    if "shell dumpsys battery" in flat:
        return 0, b"Current Battery Service state:\n  status: 2\n  level: 94\n", b""
    if "shell dumpsys power" in flat:
        return 0, b"  mWakefulness=Awake\n", b""
    if "shell dumpsys window" in flat:
        return (
            0,
            b"  mCurrentFocus=Window{94cd8c9 u0 "
            b"com.android.settings/com.android.settings.Settings}\n",
            b"",
        )
    return 0, b"", b""


def make_test_png(width: int = 2, height: int = 2, *, pad: int = 600) -> bytes:
    """用标准库构造一个长度 > PNG_MIN_BYTES 的合法 PNG（供截图校验测试）。"""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    idat = zlib.compress(raw)
    text_chunk = chunk(b"tEXt", b"pad\x00" + b"x" * pad)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + text_chunk
        + chunk(b"IEND", b"")
    )


def make_raw_pixels(width: int = 2, height: int = 2) -> bytes:
    """构造确定性的 RGBA 像素数据。"""
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 40) % 256, (y * 30) % 256, 128, 255))
    return bytes(pixels)


def make_raw_screencap(
    width: int = 2,
    height: int = 2,
    *,
    header_size: int = 16,
    warning: bytes = b"",
    image_format: int = 1,
) -> bytes:
    """构造 ``screencap`` raw 输出（含可选前导 warning 文本）。"""
    pixels = make_raw_pixels(width, height)
    if header_size == 16:
        header = struct.pack("<4I", width, height, image_format, 1)
    else:
        header = struct.pack("<3I", width, height, image_format)
    return warning + header + pixels


def _fake_converter(output: bytes = b"\xff\xd8fake-jpeg", image_format: str = "jpeg"):
    """构造替换 ``AdbClient._raw_to_image`` 的假转换器。"""
    calls: list[tuple[int, int, int]] = []

    async def convert(pixels, width, height, raw_format, *, timeout=None):
        calls.append((width, height, raw_format))
        return output, image_format

    convert.calls = calls  # type: ignore[attr-defined]
    return convert


class FakeProc:
    """模拟 asyncio 子进程对象。"""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


class HangingProc(FakeProc):
    """communicate 会挂起的子进程，用于测试超时杀进程。"""

    def __init__(self) -> None:
        super().__init__()
        self.returncode = None
        self._calls = 0

    async def communicate(self):
        self._calls += 1
        if self.killed:
            return b"", b""
        await asyncio.sleep(30)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


# ----------------------------------------------------------------------
# 构造与命令执行
# ----------------------------------------------------------------------
def test_default_serial_is_host_port(tmp_path):
    client = make_client(tmp_path)
    assert client.serial == SERIAL


def test_explicit_serial_overrides_host_port(tmp_path):
    client = make_client(tmp_path, serial="emulator-5554")
    assert client.serial == "emulator-5554"


def test_missing_adb_path_raises_not_configured(tmp_path):
    with pytest.raises(AdbNotConfiguredError):
        AdbClient(tmp_path / "no-such-adb", "127.0.0.1", 5555, tmp_path / "home")


def test_empty_adb_path_raises_not_configured(tmp_path):
    with pytest.raises(AdbNotConfiguredError):
        AdbClient("", "127.0.0.1", 5555, tmp_path / "home")


def test_env_home_injected_into_subprocess(tmp_path, monkeypatch):
    recorded = {}

    async def fake_create(*argv, **kwargs):
        recorded["argv"] = list(argv)
        recorded["kwargs"] = kwargs
        return FakeProc(stdout=b"hello world\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    client = make_client(tmp_path)
    output = run(client.shell("echo hello"))
    assert output == "hello world"
    assert recorded["argv"][0] == sys.executable
    assert recorded["argv"][1] == "-s"
    assert recorded["argv"][2] == SERIAL
    assert recorded["argv"][3:] == ["shell", "echo hello"]
    env = recorded["kwargs"]["env"]
    assert env["HOME"] == str(tmp_path / "adbhome")
    assert env["ANDROID_USER_HOME"] == str(tmp_path / "adbhome" / ".android")
    assert (tmp_path / "adbhome" / ".android").is_dir()


def test_command_timeout_kills_process(tmp_path, monkeypatch):
    proc = HangingProc()

    async def fake_create(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    client = make_client(tmp_path, command_timeout=0.2)
    with pytest.raises(AdbCommandError) as excinfo:
        run(client.shell("sleep 100"))
    assert "超时" in str(excinfo.value)
    assert proc.killed is True


def test_shell_command_and_output(tmp_path):
    runner = FakeRunner(lambda argv: (0, b"Physical size: 900x1600\n", b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    assert run(client.shell("wm size")) == "Physical size: 900x1600"
    assert runner.calls[0][0][-2:] == ["shell", "wm size"]


def test_shell_nonzero_returncode_raises(tmp_path):
    runner = FakeRunner(lambda argv: (1, b"", b"error: unknown command"))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    with pytest.raises(AdbCommandError) as excinfo:
        run(client.shell("bad command"))
    assert excinfo.value.returncode == 1
    assert "unknown command" in excinfo.value.stderr


def test_offline_error_reconnects_once_then_succeeds(tmp_path):
    state = {"calls": 0, "reconnects": 0}

    def handler(argv):
        state["calls"] += 1
        if state["calls"] == 1:
            return 1, b"", b"error: device offline"
        return 0, b"ok", b""

    async def fake_reconnect():
        state["reconnects"] += 1

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    client._reconnect = fake_reconnect  # type: ignore[method-assign]
    assert run(client.shell("echo ok")) == "ok"
    assert state["reconnects"] == 1
    assert state["calls"] == 2


def test_offline_error_after_retry_raises_device_offline(tmp_path):
    state = {"reconnects": 0}

    async def fake_reconnect():
        state["reconnects"] += 1

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(lambda argv: (1, b"", b"error: device offline"))  # type: ignore[method-assign]
    client._reconnect = fake_reconnect  # type: ignore[method-assign]
    with pytest.raises(DeviceOfflineError):
        run(client.shell("echo ok"))
    assert state["reconnects"] == 1


def test_reconnect_failure_surfaces_device_offline(tmp_path):
    async def fake_reconnect():
        raise AdbConnectError("连不上")

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(lambda argv: (1, b"", b"device offline"))  # type: ignore[method-assign]
    client._reconnect = fake_reconnect  # type: ignore[method-assign]
    with pytest.raises(DeviceOfflineError) as excinfo:
        run(client.shell("echo ok"))
    assert "重连失败" in str(excinfo.value)


def test_exec_out_returns_bytes(tmp_path):
    runner = FakeRunner(lambda argv: (0, b"\x89PNG\r\n\x1a\nrest", b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    data = run(client.exec_out("screencap", "-p"))
    assert data.startswith(b"\x89PNG")
    assert runner.calls[0][0][-3:] == ["exec-out", "screencap", "-p"]


def test_exec_out_requires_arguments(tmp_path):
    client = make_client(tmp_path)
    with pytest.raises(AdbCommandError):
        run(client.exec_out())


def test_screenshot_png_validates_png_header(tmp_path):
    client = make_client(tmp_path)
    valid_png = make_test_png()
    client._run_adb = FakeRunner(lambda argv: (0, valid_png, b""))  # type: ignore[method-assign]
    assert run(client.screenshot_png()) == valid_png
    # 魔数正确但长度过短：视为数据损坏
    client._run_adb = FakeRunner(lambda argv: (0, b"\x89PNG\r\n\x1a\nshort", b""))  # type: ignore[method-assign]
    with pytest.raises(AdbCommandError) as excinfo:
        run(client.screenshot_png())
    assert "过短" in str(excinfo.value)
    client._run_adb = FakeRunner(lambda argv: (0, b"<html>not png</html>", b""))  # type: ignore[method-assign]
    with pytest.raises(AdbCommandError):
        run(client.screenshot_png())
    client._run_adb = FakeRunner(lambda argv: (0, b"", b""))  # type: ignore[method-assign]
    with pytest.raises(AdbCommandError):
        run(client.screenshot_png())


# ----------------------------------------------------------------------
# raw screencap 解析 / PNG 校验 / 自适应截图
# ----------------------------------------------------------------------
def test_parse_screencap_raw_16_byte_header():
    width, height, image_format, pixels = parse_screencap_raw(make_raw_screencap(2, 2))
    assert (width, height, image_format) == (2, 2, 1)
    assert pixels == make_raw_pixels(2, 2)


def test_parse_screencap_raw_12_byte_header():
    data = make_raw_screencap(2, 2, header_size=12)
    width, height, image_format, pixels = parse_screencap_raw(data)
    assert (width, height, image_format) == (2, 2, 1)
    assert pixels == make_raw_pixels(2, 2)


def test_parse_screencap_raw_tolerates_leading_warning():
    warning = b"WARNING: linker: libc.so has text relocations\n"
    data = make_raw_screencap(2, 2, warning=warning)
    width, height, image_format, pixels = parse_screencap_raw(data)
    assert (width, height, image_format) == (2, 2, 1)
    assert pixels == make_raw_pixels(2, 2)


@pytest.mark.parametrize(
    "bad_data",
    [
        b"",
        b"\x00" * 8,
        make_raw_screencap(2, 2)[:-4],  # 像素数据被截断
        b"garbage" * 4,
    ],
)
def test_parse_screencap_raw_invalid_data(bad_data):
    with pytest.raises(AdbCommandError):
        parse_screencap_raw(bad_data)


def test_validate_png_bytes():
    valid_png = make_test_png()
    assert validate_png_bytes(valid_png) == valid_png
    with pytest.raises(AdbCommandError):
        validate_png_bytes(valid_png[: PNG_MIN_BYTES - 1])
    with pytest.raises(AdbCommandError):
        validate_png_bytes(b"not a png" + b"x" * 800)


def test_repair_png_bytes_handles_crlf_corruption():
    pytest.importorskip("PIL")
    valid_png = make_test_png()
    assert repair_png_bytes(valid_png) == valid_png
    # 模拟 SRC 记录的数据损坏形态：\n 前被插入 \r / \r\r
    assert b"\r\n" not in valid_png[8:]  # 除 PNG 签名外不应有 CRLF 组合
    corrupted_crlf = valid_png.replace(b"\n", b"\r\n")
    assert repair_png_bytes(corrupted_crlf) == valid_png
    corrupted_double = valid_png.replace(b"\n", b"\r\r\n")
    assert repair_png_bytes(corrupted_double) == valid_png
    assert repair_png_bytes(b"\x89PNG\r\n\x1a\n" + b"broken-data") is None


def test_convert_raw_with_pillow_outputs_jpeg():
    pytest.importorskip("PIL")
    data = _convert_raw_with_pillow(make_raw_pixels(2, 2), 2, 2, "jpeg")
    assert data.startswith(b"\xff\xd8")


def test_capture_screen_default_uses_png(tmp_path):
    valid_png = make_test_png()
    runner = FakeRunner(lambda argv: (0, valid_png, b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    data, image_format, elapsed_ms = run(client.capture_screen())
    assert data == valid_png
    assert image_format == "png"
    assert elapsed_ms >= 0
    assert len(runner.calls) == 1  # 未触发 raw 路径


def test_capture_screen_fast_uses_raw(tmp_path, monkeypatch):
    raw = make_raw_screencap(2, 2)
    runner = FakeRunner(lambda argv: (0, raw, b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    converter = _fake_converter()
    monkeypatch.setattr(client, "_raw_to_image", converter)
    data, image_format, elapsed_ms = run(client.capture_screen(fast=True))
    assert image_format == "jpeg"
    assert data.startswith(b"\xff\xd8")
    assert converter.calls == [(2, 2, 1)]  # type: ignore[attr-defined]
    flat_calls = runner.flat_calls()
    assert len(flat_calls) == 1
    assert flat_calls[0].endswith("exec-out screencap")
    assert " -p" not in flat_calls[0]


def test_capture_screen_slow_png_returns_png_without_retransfer(tmp_path, monkeypatch):
    """慢链路：PNG 已完整取得就直接返回，不再请求 raw。

    复现 review 中的 UAT 限速场景：PNG 在 24.4s 时已完整取回，旧逻辑因超过
    15s 阈值继续请求 raw，raw 再等 30s 超时后才返回同一张 PNG，总耗时 54.4s。
    """
    valid_png = make_test_png()
    raw = make_raw_screencap(2, 2)

    def handler(argv):
        flat = " ".join(argv)
        return 0, (valid_png if " -p" in flat else raw), b""

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_raw_to_image", _fake_converter())
    monkeypatch.setattr(adb_module, "time", fake_time(0.0, 24.4))
    data, image_format, elapsed_ms = run(client.capture_screen())
    assert data == valid_png
    assert image_format == "png"
    assert elapsed_ms == 24400  # 只花了 PNG 那一次
    flat_calls = client._run_adb.flat_calls()  # type: ignore[attr-defined]
    assert len(flat_calls) == 1  # 没有追加 raw 请求
    assert " -p" in flat_calls[0]
    assert client.last_capture_route == "png"
    assert client.capture_stats()["raw_avg_ms"] is None


def test_capture_screen_slow_link_never_exceeds_png_cost(tmp_path, monkeypatch):
    """限速链路：raw 会超时也无关紧要——根本不该发起 raw。"""
    valid_png = make_test_png()

    async def handler(argv):
        flat = " ".join(argv)
        if " -p" not in flat:
            raise AdbCommandError("execution timeout", detail="raw 30s 未返回")
        return 0, valid_png, b""

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_png_slow_threshold_ms", 15000.0)
    monkeypatch.setattr(adb_module, "time", fake_time(0.0, 24.4))
    data, image_format, _elapsed = run(client.capture_screen())
    assert data == valid_png
    assert image_format == "png"
    assert len(client._run_adb.calls) == 1  # type: ignore[attr-defined]


def test_capture_screen_auto_prefers_raw_when_measured_faster(tmp_path, monkeypatch):
    """自动模式优先 raw 需要实测依据：raw 平均耗时明显低于 PNG 才成立。"""
    raw = make_raw_screencap(2, 2)
    runner = FakeRunner(lambda argv: (0, raw, b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_raw_to_image", _fake_converter())
    client._png_avg_ms = 900.0
    client._raw_avg_ms = 300.0
    assert client.prefers_raw_capture() is True
    data, image_format, _elapsed = run(client.capture_screen())
    assert image_format == "jpeg"
    assert data.startswith(b"\xff\xd8")
    flat_calls = runner.flat_calls()  # type: ignore[attr-defined]
    assert len(flat_calls) == 1
    assert flat_calls[0].endswith("exec-out screencap")
    assert client.last_capture_route == "raw"


def test_capture_screen_auto_keeps_png_when_raw_measured_slower(tmp_path):
    """实测 raw 更慢（如慢链路）时，自动模式必须回到 PNG。"""
    valid_png = make_test_png()
    runner = FakeRunner(lambda argv: (0, valid_png, b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    client._png_avg_ms = 24400.0
    client._raw_avg_ms = 30400.0
    assert client.prefers_raw_capture() is False
    data, image_format, _elapsed = run(client.capture_screen())
    assert data == valid_png
    assert image_format == "png"
    assert len(runner.calls) == 1  # type: ignore[attr-defined]
    assert " -p" in runner.flat_calls()[0]  # type: ignore[attr-defined]


def test_capture_screen_no_samples_defaults_to_png(tmp_path):
    """没有任何实测样本时不猜 raw：PNG 体积只有 raw 的 1/5 左右。"""
    runner = FakeRunner(lambda argv: (0, make_test_png(), b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    assert client.capture_stats() == {"png_avg_ms": None, "raw_avg_ms": None}
    assert client.prefers_raw_capture() is False
    _data, image_format, _elapsed = run(client.capture_screen())
    assert image_format == "png"
    assert len(runner.calls) == 1  # type: ignore[attr-defined]


def test_capture_screen_raw_preference_cleared_after_failure(tmp_path, monkeypatch):
    """优先 raw 的样本一旦失效（raw 失败）就被丢弃，下次回到 PNG。"""
    valid_png = make_test_png()

    async def handler(argv):
        flat = " ".join(argv)
        if " -p" in flat:
            return 0, valid_png, b""
        raise AdbCommandError("raw 失败")

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    client._png_avg_ms = 900.0
    client._raw_avg_ms = 300.0
    data, image_format, _elapsed = run(client.capture_screen())
    assert image_format == "png"
    assert data == valid_png
    assert client.capture_stats()["raw_avg_ms"] is None
    assert client.prefers_raw_capture() is False
    run(client.capture_screen())
    assert " -p" in client._run_adb.flat_calls()[-1]  # type: ignore[attr-defined]


def test_capture_screen_explicit_fast_ignores_png_samples(tmp_path, monkeypatch):
    """显式 fast 不受实测结论影响，仍直接走 raw。"""
    raw = make_raw_screencap(2, 2)
    runner = FakeRunner(lambda argv: (0, raw, b""))
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_raw_to_image", _fake_converter())
    client._png_avg_ms = 100.0  # PNG 明显更快也不影响显式 fast
    client._raw_avg_ms = 5000.0
    data, image_format, _elapsed = run(client.capture_screen(fast=True))
    assert image_format == "jpeg"
    assert data.startswith(b"\xff\xd8")
    flat_calls = runner.flat_calls()  # type: ignore[attr-defined]
    assert flat_calls[0].endswith("exec-out screencap")
    assert " -p" not in flat_calls[0]


def test_capture_screen_png_corrupt_falls_back_to_raw(tmp_path, monkeypatch):
    """PNG 魔数正确但数据无法解码时，才改走 raw。"""
    pytest.importorskip("PIL")
    corrupt_png = b"\x89PNG\r\n\x1a\n" + b"broken-data" * 80  # 长度足够但解码失败
    raw = make_raw_screencap(2, 2)

    def handler(argv):
        flat = " ".join(argv)
        return 0, (corrupt_png if " -p" in flat else raw), b""

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_raw_to_image", _fake_converter())
    data, image_format, _elapsed = run(client.capture_screen())
    assert image_format == "jpeg"
    assert data.startswith(b"\xff\xd8")
    assert client.last_capture_route == "raw"


def test_capture_screen_png_anomaly_falls_back_to_raw(tmp_path, monkeypatch):
    short_png = b"\x89PNG\r\n\x1a\n" + b"x" * 10  # 魔数正确但数据过短
    raw = make_raw_screencap(2, 2)

    def handler(argv):
        flat = " ".join(argv)
        return 0, (short_png if " -p" in flat else raw), b""

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_raw_to_image", _fake_converter())
    data, image_format, _elapsed = run(client.capture_screen())
    assert image_format == "jpeg"
    assert data.startswith(b"\xff\xd8")


def test_capture_screen_raw_fails_falls_back_to_png(tmp_path):
    valid_png = make_test_png()

    def handler(argv):
        flat = " ".join(argv)
        if " -p" in flat:
            return 0, valid_png, b""
        return 0, b"garbage-raw-data", b""

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    data, image_format, _elapsed = run(client.capture_screen(fast=True))
    assert image_format == "png"
    assert data == valid_png


def test_capture_screen_raw_parse_retries_once(tmp_path, monkeypatch):
    state = {"raw_calls": 0}

    def handler(argv):
        flat = " ".join(argv)
        if " -p" in flat:
            return 0, make_test_png(), b""
        state["raw_calls"] += 1
        if state["raw_calls"] == 1:
            return 0, b"broken-raw", b""
        return 0, make_raw_screencap(2, 2), b""

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    monkeypatch.setattr(client, "_raw_to_image", _fake_converter())
    data, image_format, _elapsed = run(client.capture_screen(fast=True))
    assert image_format == "jpeg"
    assert state["raw_calls"] == 2


def test_capture_screen_both_paths_fail_raises(tmp_path):
    async def handler(argv):
        raise AdbCommandError("screencap 失败")

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    with pytest.raises(AdbCommandError) as excinfo:
        run(client.capture_screen())
    assert "截图失败" in str(excinfo.value)


def test_raw_to_image_pil_unavailable_uses_ffmpeg(tmp_path, monkeypatch):
    import core.adb as adb_module

    monkeypatch.setattr(adb_module, "_PILImage", None)

    async def fake_ffmpeg(ffmpeg_path, pixels, width, height, *, timeout):
        assert pixels == make_raw_pixels(2, 2)
        return make_test_png()

    monkeypatch.setattr(adb_module, "_convert_raw_with_ffmpeg", fake_ffmpeg)
    client = make_client(tmp_path)
    client._ffmpeg_checked = True
    client._ffmpeg_path = Path("/fake/ffmpeg")
    data, image_format = run(client._raw_to_image(make_raw_pixels(2, 2), 2, 2, 1))
    assert image_format == "png"
    assert data.startswith(b"\x89PNG")


def test_raw_to_image_both_converters_unavailable(tmp_path, monkeypatch):
    import core.adb as adb_module

    monkeypatch.setattr(adb_module, "_PILImage", None)
    client = make_client(tmp_path)
    client._ffmpeg_checked = True
    client._ffmpeg_path = None
    with pytest.raises(AdbCommandError) as excinfo:
        run(client._raw_to_image(make_raw_pixels(2, 2), 2, 2, 1))
    assert "转换失败" in str(excinfo.value)


def test_raw_to_image_rejects_unsupported_format(tmp_path):
    client = make_client(tmp_path)
    with pytest.raises(AdbCommandError):
        run(client._raw_to_image(make_raw_pixels(2, 2), 2, 2, 2))


# ----------------------------------------------------------------------
# 连接
# ----------------------------------------------------------------------
def test_connect_returns_device_info(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    info = run(client.connect())
    assert info.serial == SERIAL
    assert info.model == "23116PN5BC"
    assert info.manufacturer == "Xiaomi"
    assert info.android_version == "12"
    assert info.sdk == 32
    assert (info.width, info.height) == (900, 1600)
    assert info.density == 320
    assert info.battery_level == 94
    assert info.battery_status == "充电中"
    assert info.current_package == "com.android.settings"
    assert info.current_activity == "com.android.settings.Settings"
    assert info.screen_on is True
    assert info.adb_connected is True
    assert run(client.is_online()) is True


def test_ensure_ready_connects_only_once(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.ensure_ready())
    calls_after_connect = len(runner.calls)
    run(client.ensure_ready())
    assert len(runner.calls) == calls_after_connect


def test_connect_failure_raises_connect_error(tmp_path):
    def handler(argv):
        flat = " ".join(argv)
        if "start-server" in flat:
            return 0, b"", b""
        if " connect " in flat:
            return 1, b"failed to connect to '192.168.250.10:16448': Connection refused", b""
        if " devices" in flat:
            return 0, b"List of devices attached\n", b""
        return 0, b"", b""

    client = make_client(tmp_path, connect_timeout=0.2)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    with pytest.raises(AdbConnectError) as excinfo:
        run(client.connect())
    assert "192.168.250.10:16448" in str(excinfo.value)


def test_connect_wraps_device_info_failure(tmp_path):
    async def failing_device_info(*, retry=True):
        raise AdbCommandError("getprop 失败")

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(standard_handler)  # type: ignore[method-assign]
    client._device_info = failing_device_info  # type: ignore[method-assign]
    with pytest.raises(AdbConnectError) as excinfo:
        run(client.connect())
    assert "读取设备信息失败" in str(excinfo.value)


def test_connect_with_existing_connection_succeeds(tmp_path):
    state = {"connect_calls": 0}

    def handler(argv):
        flat = " ".join(argv)
        if "start-server" in flat:
            return 0, b"", b""
        if " connect " in flat:
            state["connect_calls"] += 1
            return 0, b"already connected to 192.168.250.10:16448", b""
        return standard_handler(argv)

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    info = run(client.connect(force=True))
    assert info.adb_connected is True
    assert state["connect_calls"] >= 1


def test_connect_lock_serializes_concurrent_calls(tmp_path):
    state = {"active": 0, "max_active": 0}

    async def handler(argv):
        flat = " ".join(argv)
        if "start-server" in flat:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            return 0, b"", b""
        return standard_handler(argv)

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]

    async def concurrent() -> None:
        await asyncio.gather(client.connect(), client.connect())

    run(concurrent())
    assert state["max_active"] == 1


def test_is_online_false_when_devices_offline(tmp_path):
    client = make_client(tmp_path)
    client._run_adb = FakeRunner(  # type: ignore[method-assign]
        lambda argv: (0, f"List of devices attached\n{SERIAL}\toffline\n".encode(), b"")
    )
    assert run(client.is_online()) is False


def test_is_online_false_on_command_error(tmp_path):
    async def handler(argv):
        raise AdbCommandError("adb 挂了")

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    assert run(client.is_online()) is False


def test_disconnect_and_close(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.connect())
    run(client.disconnect())
    assert any("disconnect" in flat for flat in runner.flat_calls())
    run(client.close())
    # 关闭后 ensure_ready 会重新连接
    run(client.ensure_ready())
    assert run(client.is_online()) is True


# ----------------------------------------------------------------------
# 运行时重配置（reconfigure）
# ----------------------------------------------------------------------
NEW_TARGET = "192.168.250.11:15555"


def dual_device_handler(argv):
    """模拟两台设备同时在线的 adb 输出（重配后新目标可用）。"""
    flat = " ".join(argv)
    if " devices" in flat:
        return (
            0,
            f"List of devices attached\n{SERIAL}\tdevice\n{NEW_TARGET}\tdevice\n".encode(),
            b"",
        )
    return standard_handler(argv)


def test_reconfigure_target_change_disconnects_and_resets(tmp_path):
    runner = FakeRunner(dual_device_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.connect())
    assert client._connected is True
    calls_before = len(runner.calls)

    result = run(client.reconfigure(host="192.168.250.11", port=15555))

    assert result["changed"] is True
    assert result["target_changed"] is True
    assert result["path_changed"] is False
    assert client.serial == NEW_TARGET
    assert client._connected is False
    # 目标变化只按旧目标断开：不重启 adb server、不主动连接
    assert runner.flat_calls()[calls_before:] == [f"{sys.executable} disconnect {SERIAL}"]

    # 下次 ensure_ready 自动连接新目标
    run(client.ensure_ready())
    assert client._connected is True
    after = runner.flat_calls()[calls_before + 1:]
    assert any(flat == f"{sys.executable} connect {NEW_TARGET}" for flat in after)
    assert any(flat.startswith(f"{sys.executable} -s {NEW_TARGET} ") for flat in after)


def test_reconfigure_same_target_is_idempotent(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.connect())
    calls_before = list(runner.flat_calls())

    result = run(
        client.reconfigure(
            adb_path=sys.executable,
            host="192.168.250.10",
            port=16448,
            serial="",
            connect_timeout=1.0,
            command_timeout=2.0,
        )
    )

    assert result == {
        "changed": False,
        "target_changed": False,
        "path_changed": False,
        "timeouts_changed": False,
        "serial": SERIAL,
        "adb_path": sys.executable,
        "connect_timeout": 1.0,
        "command_timeout": 2.0,
    }
    assert client.serial == SERIAL
    assert client._connected is True
    # 幂等空操作不发任何 adb 命令（不断连）
    assert runner.flat_calls() == calls_before


def test_reconfigure_serial_overrides_host_port(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.connect())
    calls_before = len(runner.calls)

    result = run(client.reconfigure(host="10.0.0.9", port=5555, serial="emulator-5554"))

    assert result["changed"] is True
    assert result["target_changed"] is True
    assert client.serial == "emulator-5554"
    assert runner.flat_calls()[calls_before:] == [f"{sys.executable} disconnect {SERIAL}"]

    # 后续命令按显式序列号下发
    run(client.shell("echo hi"))
    assert runner.calls[-1][0][:3] == [sys.executable, "-s", "emulator-5554"]


def test_reconfigure_clear_serial_falls_back_to_host_port(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path, serial="emulator-5554")
    client._run_adb = runner  # type: ignore[method-assign]

    result = run(client.reconfigure(host="192.168.250.20", port=15555, serial=""))

    assert result["target_changed"] is True
    assert client.serial == "192.168.250.20:15555"
    assert runner.flat_calls()[-1] == f"{sys.executable} disconnect emulator-5554"


def test_reconfigure_host_only_recomputes_derived_target(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]

    result = run(client.reconfigure(host="192.168.250.30"))

    assert result["target_changed"] is True
    assert client.serial == "192.168.250.30:16448"


def test_reconfigure_keeps_explicit_serial_on_host_change(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path, serial="emulator-5554")
    client._run_adb = runner  # type: ignore[method-assign]

    result = run(client.reconfigure(host="192.168.250.30"))

    assert result["changed"] is True
    assert result["target_changed"] is False
    assert client.serial == "emulator-5554"
    assert runner.flat_calls() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"serial": "", "host": ""},   # 目标为空
        {"port": 0},                  # 端口越界
        {"port": 65536},
        {"port": "not-a-port"},       # 非法端口
    ],
)
def test_reconfigure_invalid_target_raises(tmp_path, kwargs):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]

    with pytest.raises(AdbNotConfiguredError):
        run(client.reconfigure(**kwargs))

    # 校验失败不改状态、不发 adb 命令
    assert client.serial == SERIAL
    assert client._connected is False
    assert runner.flat_calls() == []


def test_reconfigure_missing_adb_path_raises(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]

    with pytest.raises(AdbNotConfiguredError):
        run(client.reconfigure(adb_path=tmp_path / "missing-adb"))

    assert client._adb_path == Path(sys.executable)
    assert runner.flat_calls() == []


def test_reconfigure_adb_path_and_timeouts_take_effect(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.connect())
    calls_before = len(runner.calls)
    alt_adb = tmp_path / "adb-alt"
    alt_adb.write_text("#!/bin/sh\n", encoding="utf-8")

    result = run(client.reconfigure(adb_path=alt_adb, connect_timeout=0.5, command_timeout=0.2))

    assert result["changed"] is True
    assert result["target_changed"] is False
    assert result["path_changed"] is True
    assert result["timeouts_changed"] is True
    assert client._adb_path == alt_adb
    # 与构造逻辑一致：超时下限收敛到 1 秒
    assert client._connect_timeout == 1.0
    assert client._command_timeout == 1.0
    assert client._connected is False
    # adb 路径变化同样按旧路径 / 旧目标断开
    assert runner.flat_calls()[calls_before:] == [f"{sys.executable} disconnect {SERIAL}"]

    # 下次 ensure_ready 使用新 adb 路径重新连接
    run(client.ensure_ready())
    after = runner.flat_calls()[calls_before + 1:]
    assert after
    assert all(flat.startswith(f"{alt_adb} ") for flat in after)


def test_reconfigure_timeout_only_keeps_connection(tmp_path):
    runner = FakeRunner(standard_handler)
    client = make_client(tmp_path)
    client._run_adb = runner  # type: ignore[method-assign]
    run(client.connect())
    calls_before = list(runner.flat_calls())

    result = run(client.reconfigure(connect_timeout=5.0))

    assert result["changed"] is True
    assert result["target_changed"] is False
    assert client._connect_timeout == 5.0
    assert client._connected is True
    # 仅调超时不影响已建立的连接
    assert runner.flat_calls() == calls_before


# ----------------------------------------------------------------------
# 设备信息解析
# ----------------------------------------------------------------------
def test_override_size_and_density_preferred(tmp_path):
    def handler(argv):
        flat = " ".join(argv)
        if "shell wm size; wm density" in flat:
            return (
                0,
                b"Physical size: 900x1600\nOverride size: 720x1280\n"
                b"Physical density: 320\nOverride density: 160\n",
                b"",
            )
        if "shell dumpsys power" in flat:
            return 0, b"  mWakefulness=Asleep\n", b""
        if "shell dumpsys battery" in flat:
            return 0, b"  status: 5\n  level: 100\n", b""
        return standard_handler(argv)

    client = make_client(tmp_path)
    client._run_adb = FakeRunner(handler)  # type: ignore[method-assign]
    info = run(client.connect())
    assert (info.width, info.height) == (720, 1280)
    assert info.density == 160
    assert info.battery_level == 100
    assert info.battery_status == "已充满"
    assert info.screen_on is False


def test_parse_battery_helper():
    text = "  status: 3\n  level: 42\n"
    assert _parse_battery(text) == (42, "放电中")
    assert _parse_battery("") == (None, "")
    assert _parse_battery("  level: abc\n") == (None, "")


def test_parse_screen_on_helper():
    assert _parse_screen_on("  mWakefulness=Awake\n") is True
    assert _parse_screen_on("  mWakefulness=Asleep\n") is False
    assert _parse_screen_on("nothing") is None


def test_parse_foreground_dump_priorities():
    text = "\n".join(
        [
            "  mCurrentFocus=Window{94cd8c9 u0 com.android.settings/com.android.settings.Settings}",
            "  mFocusedApp=ActivityRecord{5d013ca u0 com.android.settings/.Settings t7}",
        ]
    )
    assert parse_foreground_dump(text) == ("com.android.settings", "com.android.settings.Settings")
    assert parse_foreground_dump("  mFocusedApp=ActivityRecord{1 u0 com.example/.Main t1}") == (
        "com.example",
        "com.example.Main",
    )
    assert parse_foreground_dump("  mResumedActivity: ActivityRecord{1 u0 com.a.b/.C t1}") == (
        "com.a.b",
        "com.a.b.C",
    )
    assert parse_foreground_dump("") == ("", "")
    assert parse_foreground_dump("  mCurrentFocus=Window{1 u0 no-package}") == ("", "")


# ----------------------------------------------------------------------
# 真实设备集成测试（默认跳过）
# ----------------------------------------------------------------------
ADB_HOST = os.environ.get("PHONE_ADB_HOST", "")
ADB_PORT = int(os.environ.get("PHONE_ADB_PORT", "16448"))
ADB_PATH = os.environ.get(
    "PHONE_ADB_PATH",
    "/home/dev/acat-workspace/.cache/work/android-platform-tools/platform-tools/adb",
)
ADB_HOME = os.environ.get(
    "PHONE_ADB_HOME",
    "/home/dev/acat-workspace/.cache/work/android-home",
)

integration = pytest.mark.skipif(not ADB_HOST, reason="需要 PHONE_ADB_HOST 环境变量指向真实设备")


@pytest.mark.integration
@integration
def test_real_device_connect_info_screenshot():
    async def scenario():
        client = AdbClient(
            ADB_PATH,
            ADB_HOST,
            ADB_PORT,
            ADB_HOME,
            connect_timeout=20.0,
            command_timeout=30.0,
        )
        try:
            info = await client.connect()
            assert info.adb_connected is True
            assert info.width > 0 and info.height > 0
            assert await client.is_online() is True
            png = await client.screenshot_png()
            assert png.startswith(b"\x89PNG")
            shell_output = await client.shell("wm size")
            assert "size:" in shell_output
            return info, png
        finally:
            await client.close()

    info, png = run(scenario())
    assert info.model
    assert len(png) > 1000


@pytest.mark.integration
@integration
def test_real_device_capture_screen_png_vs_fast():
    """真机对比：纯 PNG 耗时 vs raw+本地编码（fast）耗时。"""

    async def scenario():
        client = AdbClient(
            ADB_PATH,
            ADB_HOST,
            ADB_PORT,
            ADB_HOME,
            connect_timeout=20.0,
            command_timeout=30.0,
        )
        try:
            await client.connect()
            started = time.perf_counter()
            png = await client.screenshot_png()
            png_ms = int(round((time.perf_counter() - started) * 1000))
            fast_data, fast_format, fast_ms = await client.capture_screen(fast=True)
            auto_data, auto_format, auto_ms = await client.capture_screen()
            return png, png_ms, fast_data, fast_format, fast_ms, auto_data, auto_format, auto_ms
        finally:
            await client.close()

    png, png_ms, fast_data, fast_format, fast_ms, auto_data, auto_format, auto_ms = run(scenario())
    assert png.startswith(b"\x89PNG")
    assert len(png) > PNG_MIN_BYTES
    assert png_ms > 0
    if fast_format == "jpeg":
        assert fast_data.startswith(b"\xff\xd8")
    else:
        assert fast_data.startswith(b"\x89PNG")
    assert fast_ms > 0 and len(fast_data) > 500
    assert auto_format in ("png", "jpeg")
    assert auto_ms > 0 and len(auto_data) > 500
    print(
        f"\n[真实设备截图] 纯PNG={png_ms}ms/{len(png)}B  "
        f"fast={fast_ms}ms/{len(fast_data)}B/{fast_format}  "
        f"auto={auto_ms}ms/{len(auto_data)}B/{auto_format}"
    )


def test_shell_output_is_bounded_while_both_streams_are_drained(tmp_path):
    client = make_client(tmp_path)
    rc, stdout, stderr = run(client._run_adb(
        [sys.executable, "-c", "import sys;sys.stdout.write('x'*200000);sys.stderr.write('y'*200000);sys.exit(7)"],
        timeout=3, output_limit=8192,
    ))
    assert rc == 7
    assert stdout == b"x" * 8192 + b"\n[output truncated]"
    assert stderr == b"y" * 8192 + b"\n[output truncated]"


def test_shell_result_executes_once_and_keeps_nonzero_exit_code(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    calls = []
    async def execute(argv, *, timeout, output_limit):
        calls.append((argv, timeout, output_limit))
        return 4, b"partial", b"device offline"
    monkeypatch.setattr(client, "_run_adb", execute)
    result = run(client.shell_result("echo partial; exit 4", timeout=2))
    assert result == {"exit_code": 4, "stdout": "partial", "stderr": "device offline"}
    assert len(calls) == 1 and calls[0][0][-2:] == ["shell", "echo partial; exit 4"]
    assert calls[0][2] == 8192


def test_unconfigured_client_never_starts_adb(tmp_path):
    """空白 ADB 地址可构造未启用客户端；任何设备操作都明确报未配置。"""
    from core.errors import AdbNotConfiguredError

    client = AdbClient(
        None,
        "",
        0,
        tmp_path / "home",
        allow_unconfigured=True,
    )
    assert client.configured is False
    assert client.serial == ""
    with pytest.raises(AdbNotConfiguredError):
        asyncio.run(client.connect())  # type: ignore[arg-type]
    with pytest.raises(AdbNotConfiguredError):
        asyncio.run(client.ensure_ready())
    with pytest.raises(AdbNotConfiguredError):
        asyncio.run(client.shell("echo hi"))


def test_configured_client_reports_configured(tmp_path):
    client = AdbClient(sys.executable, "10.0.0.2", 5555, tmp_path / "home")
    assert client.configured is True
    assert client.serial == "10.0.0.2:5555"


@pytest.mark.parametrize("already_online", [False, True])
def test_connect_cli_timeout_uses_device_state_and_remaining_retry_window(tmp_path, already_online):
    from core.errors import AdbTimeoutError
    client = make_client(tmp_path)
    attempts = 0
    polls = 0

    def handler(argv):
        nonlocal attempts, polls
        if "connect" in argv:
            attempts += 1
            if attempts == 1:
                raise AdbTimeoutError("模拟连接子进程超时")
        if "devices" in argv:
            polls += 1
            if polls == 1 and not already_online:
                return 0, b"List of devices attached\n", b""
        return standard_handler(argv)

    runner = FakeRunner(handler)
    client._run_adb = runner
    info = run(client.connect())
    assert info.adb_connected and info.serial == SERIAL
    assert attempts == (1 if already_online else 2)
    timeouts = [timeout for argv, timeout in runner.calls if "connect" in argv]
    assert all(0 < value <= 1 for value in timeouts)
    if len(timeouts) > 1:
        assert timeouts[1] < timeouts[0]
