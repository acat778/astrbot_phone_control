"""设备无 root 时抓包跳过重试的回归测试。"""

from __future__ import annotations

import asyncio

from core.traffic_capture import TrafficCapture


class FakeAdb:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, args, **_kwargs):
        self.calls.append(list(args))
        return ""

    async def shell(self, _command, **_kwargs):
        return "1000"


def _capture(tmp_path) -> tuple[TrafficCapture, FakeAdb]:
    adb = FakeAdb()
    return (
        TrafficCapture(adb=adb, data_dir=tmp_path, logger=lambda _m: None),
        adb,
    )


def test_setup_device_marks_root_unavailable(tmp_path, monkeypatch) -> None:
    capture, _adb = _capture(tmp_path)
    monkeypatch.setattr("core.traffic_capture.ROOT_SETTLE_SECONDS", 0)

    def _hash(_path):
        """同步替身：_subject_hash_old 本身是同步方法，不能替换成 async。"""
        return "deadbeef"

    async def _cleanup():
        return None

    async def _wait_root():
        return False

    monkeypatch.setattr(capture, "_subject_hash_old", _hash)
    monkeypatch.setattr(capture, "_cleanup_device_state_locked", _cleanup)
    monkeypatch.setattr(capture, "_wait_root", _wait_root)

    try:
        asyncio.run(capture._setup_device())
    except RuntimeError as exc:
        assert "root" in str(exc)
    else:  # pragma: no cover - 必须有失败
        raise AssertionError("无 root 时应抛出 RuntimeError")
    assert capture.root_unavailable is True
    assert capture.unavailable is True
    assert "root" in capture.unavailable_reason


def test_start_skips_when_root_unavailable(tmp_path) -> None:
    capture, adb = _capture(tmp_path)
    capture._note_failure(
        "RuntimeError: 设备无 root，已跳过抓包（adb root 失败）",
        unavailable=True,
    )

    result = asyncio.run(capture.start())

    assert result["ok"] is False
    assert result["unavailable"] is True
    assert result["root_unavailable"] is True
    # 失败 reason 必须是明确字符串，不能是 None。
    assert isinstance(result["reason"], str) and "root" in result["reason"]
    assert adb.calls == []
    status = capture.status()
    assert status["unavailable"] is True
    assert status["unavailable_reason"]
    assert "root" in status["unavailable_reason"]
    assert "不可用" in capture.status_text()


def test_certificate_process_keeps_event_loop_responsive(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    capture, _adb = _capture(tmp_path)
    in_process = threading.Event()
    release = threading.Event()
    responsiveness = []

    def openssl(*_args, **_kwargs):
        in_process.set()
        responsiveness.append(release.wait(1))
        return SimpleNamespace(stdout="deadbeef\n")

    async def no_root():
        return False

    monkeypatch.setattr("core.traffic_capture.subprocess.run", openssl)
    monkeypatch.setattr("core.traffic_capture.ROOT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(capture, "_wait_root", no_root)

    async def run():
        async def heartbeat():
            while not in_process.is_set():
                await asyncio.sleep(0)
            release.set()

        beat = asyncio.create_task(heartbeat())
        try:
            try:
                await capture._setup_device()
            except RuntimeError as exc:
                assert "root" in str(exc)
            else:
                raise AssertionError("无 root 设备应拒绝系统 CA 配置")
            await beat
        finally:
            release.set()
            beat.cancel()

    asyncio.run(run())
    assert responsiveness == [True], "证书处理期间事件循环应继续运行心跳"
