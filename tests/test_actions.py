"""core/actions.py 单元测试。

所有设备响应用 FakeAdb 模拟；真实设备用例标记 ``integration``，
仅在设置 ``PHONE_ADB_HOST`` 环境变量时执行。
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.actions as actions_module  # noqa: E402
from core.actions import DeviceActions  # noqa: E402
from core.adb import AdbClient  # noqa: E402
from core.errors import (  # noqa: E402
    ActionFailedError,
    AdbCommandError,
    AppNotInstalledError,
    UiDumpError,
)
from core.models import ScreenSnapshot, UiNode  # noqa: E402
from core.ui import dump_ui, render_ui, screen_signature  # noqa: E402

FOCUS_OUTPUT = (
    "  mCurrentFocus=Window{94cd8c9 u0 com.example.app/com.example.app.MainActivity}\n"
)
ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
SOGOU_IME = "com.sohu.inputmethod.sogou.chuizi/com.sohu.inputmethod.sogou.SogouIME"


def run(coro):
    """在独立事件循环中执行协程（测试环境未安装 pytest-asyncio）。"""
    return asyncio.run(coro)


class FakeAdb:
    """DeviceActions 所需的最小 AdbClient 鸭子类型。"""

    def __init__(
        self,
        *,
        ime: str = ADBKEYBOARD_IME,
        installed: tuple[str, ...] = ("com.example.app",),
        monkey_output: str = "Events injected: 1\n",
        wm_output: str = "Physical size: 900x1600",
        foreground_output: str = FOCUS_OUTPUT,
        foreground_sequence: list[str] | None = None,
        capture_bytes: bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        capture_format: str = "png",
        capture_duration_ms: int = 120,
        capture_route="png",
        prefer_raw=False,
        capture_error: Exception | None = None,
        shell_error: Exception | None = None,
    ) -> None:
        self.ime = ime
        self.installed = installed
        self.monkey_output = monkey_output
        self.wm_output = wm_output
        self.foreground_output = foreground_output
        self.foreground_package = "com.example.app"
        self.foreground_sequence = list(foreground_sequence or [])
        self.capture_bytes = capture_bytes
        self.capture_format = capture_format
        self.capture_duration_ms = capture_duration_ms
        self.capture_route = capture_route
        self.prefer_raw = prefer_raw
        self.capture_error = capture_error
        self.shell_error = shell_error
        self.ensure_ready_calls = 0
        self.commands: list[str] = []
        self.capture_calls: list[dict] = []
        self.foreground_calls = 0

    @property
    def serial(self) -> str:
        return "192.168.250.10:16448"

    async def ensure_ready(self) -> None:
        self.ensure_ready_calls += 1

    async def shell(self, command, *, timeout=None, retry=True) -> str:
        self.commands.append(command)
        if self.shell_error is not None:
            raise self.shell_error
        return self._handle(command)

    def _handle(self, command: str) -> str:
        if command == "wm size":
            return self.wm_output
        if command == "ime list -s":
            return self.ime
        if command.startswith("pm list packages"):
            package = command.rsplit(" ", 1)[-1]
            return f"package:{package}" if package in self.installed else ""
        if "mCurrentFocus" in command:
            return self._next_foreground_output()
        if command.startswith("monkey "):
            return self.monkey_output
        return ""

    def _next_foreground_output(self) -> str:
        """按 foreground_sequence 依次返回前台包（序列用尽后重复最后一个）。"""
        self.foreground_calls += 1
        if not self.foreground_sequence and self.foreground_output:
            return self.foreground_output
        if self.foreground_sequence:
            if len(self.foreground_sequence) > 1:
                package = self.foreground_sequence.pop(0)
            else:
                package = self.foreground_sequence[0]
        else:
            package = self.foreground_package
        if not package:
            return ""
        return (
            f"  mCurrentFocus=Window{{1 u0 {package}/{package}.MainActivity}}\n"
        )

    async def capture_screen(self, *, fast: bool = False, timeout=None) -> tuple[bytes, str, int]:
        self.capture_calls.append({"fast": fast, "timeout": timeout})
        if self.capture_error is not None:
            raise self.capture_error
        call_index = len(self.capture_calls)
        data = self.capture_bytes
        image_format = self.capture_format
        duration = self.capture_duration_ms
        if callable(data):
            data = data(call_index)
        if callable(image_format):
            image_format = image_format(call_index)
        if callable(duration):
            duration = duration(call_index)
        return data, image_format, duration

    @property
    def last_capture_route(self) -> str:
        value = self.capture_route
        if callable(value):
            value = value(len(self.capture_calls))
        return str(value)

    def prefers_raw_capture(self) -> bool:
        value = self.prefer_raw
        if callable(value):
            value = value(len(self.capture_calls))
        return bool(value)

    def action_commands(self) -> list[str]:
        """过滤掉查询类命令，只保留真正改变设备状态的命令。"""
        skip_prefixes = ("wm size", "ime list -s", "pm list packages", "monkey ")
        return [
            command
            for command in self.commands
            if not command.startswith(skip_prefixes) and "mCurrentFocus" not in command
        ]


def make_actions(fake: FakeAdb, logger=None) -> DeviceActions:
    return DeviceActions(fake, logger=logger)


# ----------------------------------------------------------------------
# 屏幕尺寸
# ----------------------------------------------------------------------
def test_screen_size_parses_and_caches():
    fake = FakeAdb()
    actions = make_actions(fake)
    assert run(actions.screen_size()) == (900, 1600)
    assert run(actions.screen_size()) == (900, 1600)
    assert fake.commands.count("wm size") == 1
    assert fake.ensure_ready_calls >= 1


def test_screen_size_prefers_override():
    fake = FakeAdb(wm_output="Physical size: 900x1600\nOverride size: 720x1280")
    actions = make_actions(fake)
    assert run(actions.screen_size()) == (720, 1280)


def test_screen_size_invalid_raises():
    fake = FakeAdb(wm_output="garbage")
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.screen_size())


# ----------------------------------------------------------------------
# 触摸动作
# ----------------------------------------------------------------------
def test_tap_command_and_log():
    logs: list[str] = []
    fake = FakeAdb()
    actions = make_actions(fake, logger=logs.append)
    run(actions.tap(450, 800))
    assert "input tap 450 800" in fake.action_commands()
    assert any("tap (450, 800)" in line for line in logs)


def test_tap_rejects_out_of_bounds_coordinates():
    fake = FakeAdb()
    actions = make_actions(fake)
    for x, y in ((900, 100), (-1, 100), (100, 1600), (100, -5)):
        with pytest.raises(ActionFailedError):
            run(actions.tap(x, y))
    assert fake.action_commands() == []


def test_tap_rejects_non_integer_coordinates():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.tap("abc", 100))  # type: ignore[arg-type]


def test_double_tap_uses_single_serial_command():
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.double_tap(100, 200, interval_ms=150))
    assert fake.action_commands() == ["input tap 100 200; sleep 0.150; input tap 100 200"]


def test_double_tap_rejects_invalid_interval():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.double_tap(100, 200, interval_ms=0))
    assert fake.action_commands() == []


def test_long_press_uses_swipe():
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.long_press(10, 20, duration_ms=700))
    assert fake.action_commands() == ["input swipe 10 20 10 20 700"]


def test_long_press_rejects_short_duration():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.long_press(10, 20, duration_ms=50))


def test_swipe_command():
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.swipe(10, 20, 30, 40, duration_ms=400))
    assert fake.action_commands() == ["input swipe 10 20 30 40 400"]


def test_swipe_rejects_short_duration_and_bad_endpoint():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.swipe(10, 20, 30, 40, duration_ms=99))
    with pytest.raises(ActionFailedError):
        run(actions.swipe(10, 20, 30, 1600, duration_ms=400))
    assert fake.action_commands() == []


def test_swipe_direction_commands():
    cases = {
        "up": "input swipe 450 1280 450 320 400",
        "down": "input swipe 450 320 450 1280 400",
        "left": "input swipe 720 800 180 800 400",
        "right": "input swipe 180 800 720 800 400",
        "UP": "input swipe 450 1280 450 320 400",
    }
    for direction, expected in cases.items():
        fake = FakeAdb()
        actions = make_actions(fake)
        run(actions.swipe_direction(direction))
        assert fake.action_commands() == [expected], direction


def test_swipe_direction_rejects_invalid_args():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.swipe_direction("diagonal"))
    for ratio in (0, -0.5, 1.5):
        with pytest.raises(ActionFailedError):
            run(actions.swipe_direction("up", ratio=ratio))
    assert fake.action_commands() == []


# ----------------------------------------------------------------------
# 文本输入
# ----------------------------------------------------------------------
def test_input_text_chinese_via_adbkeyboard():
    fake = FakeAdb(ime=ADBKEYBOARD_IME)
    actions = make_actions(fake)
    text = "你好，世界"
    run(actions.input_text(text))
    assert "ime list -s" in fake.commands
    broadcasts = [command for command in fake.commands if "ADB_INPUT_B64" in command]
    assert len(broadcasts) == 1
    encoded = broadcasts[0].split("--es msg ", 1)[1].strip()
    assert base64.b64decode(encoded).decode("utf-8") == text
    assert fake.commands.count("ime list -s") == 1  # 检测结果缓存


def test_input_text_ascii_fallback_without_adbkeyboard():
    fake = FakeAdb(ime=SOGOU_IME)
    actions = make_actions(fake)
    run(actions.input_text("hello world"))
    assert fake.action_commands() == ["input text 'hello%sworld'"]


def test_input_text_ascii_special_characters_quoted():
    fake = FakeAdb(ime=SOGOU_IME)
    actions = make_actions(fake)
    run(actions.input_text("a'b;c&d"))
    assert fake.action_commands() == ["input text 'a'\\''b;c&d'"]


def test_input_text_chinese_without_adbkeyboard_raises():
    fake = FakeAdb(ime=SOGOU_IME)
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError) as excinfo:
        run(actions.input_text("你好"))
    assert "ADBKeyboard" in str(excinfo.value)
    assert fake.action_commands() == []


def test_input_text_newline_requires_adbkeyboard():
    fake = FakeAdb(ime=SOGOU_IME)
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.input_text("line1\nline2"))
    assert fake.action_commands() == []


def test_input_text_press_enter():
    fake = FakeAdb(ime=SOGOU_IME)
    actions = make_actions(fake)
    run(actions.input_text("hi", press_enter=True))
    assert fake.action_commands() == ["input text 'hi'", "input keyevent 66"]


def test_input_text_empty_with_press_enter():
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.input_text("", press_enter=True))
    assert fake.action_commands() == ["input keyevent 66"]


def test_clear_text_with_adbkeyboard():
    fake = FakeAdb(ime=ADBKEYBOARD_IME)
    actions = make_actions(fake)
    run(actions.clear_text())
    assert fake.action_commands() == ["am broadcast -a ADB_CLEAR_TEXT"]


def test_clear_text_fallback_deletes_repeatedly():
    fake = FakeAdb(ime=SOGOU_IME)
    actions = make_actions(fake)
    run(actions.clear_text())
    commands = fake.action_commands()
    assert len(commands) == 1
    parts = commands[0].split()
    assert parts[:3] == ["input", "keyevent", "123"]
    assert parts.count("67") == 120


# ----------------------------------------------------------------------
# 按键
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "code"),
    [
        ("back", 4),
        ("home", 3),
        ("enter", 66),
        ("recents", 187),
        ("volume_up", 24),
        ("volume_down", 25),
        ("power", 26),
        ("tab", 61),
        ("delete", 67),
        ("escape", 111),
        ("BACK", 4),
        ("26", 26),
    ],
)
def test_press_key_mapping(key, code):
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.press_key(key))
    assert fake.action_commands() == [f"input keyevent {code}"]


def test_press_key_invalid_raises():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.press_key("teleport"))
    assert fake.action_commands() == []


# ----------------------------------------------------------------------
# 应用控制
# ----------------------------------------------------------------------
def test_open_app_launches_with_monkey():
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.open_app("com.example.app"))
    assert "pm list packages com.example.app" in fake.commands
    assert "monkey -p com.example.app -c android.intent.category.LAUNCHER 1" in fake.commands


def test_open_app_not_installed_raises():
    fake = FakeAdb(installed=())
    actions = make_actions(fake)
    with pytest.raises(AppNotInstalledError):
        run(actions.open_app("com.example.app"))
    assert not any(command.startswith("monkey ") for command in fake.commands)


def test_open_app_no_launcher_entry_raises():
    fake = FakeAdb(monkey_output="** No activities found to run, monkey aborted.\n")
    actions = make_actions(fake)
    with pytest.raises(AppNotInstalledError):
        run(actions.open_app("com.example.app"))


def test_open_app_invalid_package_raises():
    fake = FakeAdb()
    actions = make_actions(fake)
    for package in ("com.example app", "com..bad", "com.example;rm -rf /", ""):
        with pytest.raises(ActionFailedError):
            run(actions.open_app(package))


def test_force_stop_command():
    fake = FakeAdb()
    actions = make_actions(fake)
    run(actions.force_stop("com.example.app"))
    assert fake.action_commands() == ["am force-stop com.example.app"]


def test_is_installed_parses_package_listing():
    fake = FakeAdb(installed=("com.example.app",))
    actions = make_actions(fake)
    assert run(actions.is_installed("com.example.app")) is True
    assert run(actions.is_installed("com.other.app")) is False
    assert "pm list packages com.example.app" in fake.commands


def test_foreground_returns_package_and_activity():
    fake = FakeAdb()
    actions = make_actions(fake)
    assert run(actions.foreground()) == ("com.example.app", "com.example.app.MainActivity")


def test_foreground_returns_empty_on_query_failure():
    fake = FakeAdb(shell_error=AdbCommandError("dumpsys 失败"))
    actions = make_actions(fake)
    assert run(actions.foreground()) == ("", "")


# ----------------------------------------------------------------------
# 截图
# ----------------------------------------------------------------------
def test_screenshot_writes_png_file(tmp_path):
    fake = FakeAdb()
    actions = make_actions(fake)
    target = run(actions.screenshot(tmp_path / "shot"))
    assert isinstance(target, Path)
    assert target.name == "shot.png"
    assert target.read_bytes() == fake.capture_bytes
    assert target.is_absolute()
    assert len(fake.capture_calls) == 1
    assert fake.capture_calls[0]["fast"] is False  # 静态场景默认走 PNG


def test_screenshot_meta_records_details(tmp_path):
    fake = FakeAdb(capture_duration_ms=321)
    actions = make_actions(fake)
    run(actions.screenshot(tmp_path / "shot"))
    meta = actions.last_screenshot_meta
    assert meta["method"] == "png"
    assert meta["duration_ms"] == 321
    assert meta["retries"] == 0
    assert meta["expected"] == ""
    assert meta["actual"] == "com.example.app"
    assert meta["mismatch"] is False
    assert meta["fast"] is False
    assert str(meta["path"]).endswith("shot.png")
    assert fake.foreground_calls == 2  # 截图前后各查一次


def test_screenshot_jpeg_suffix_corrected(tmp_path):
    jpeg = b"\xff\xd8fake-jpeg"
    fake = FakeAdb(capture_format="jpeg", capture_bytes=jpeg)
    actions = make_actions(fake)
    target = run(actions.screenshot(tmp_path / "shot.png"))
    assert target.name == "shot.jpg"
    assert target.read_bytes() == jpeg
    assert actions.last_screenshot_meta["method"] == "jpeg"


def test_screenshot_existing_directory(tmp_path):
    fake = FakeAdb()
    actions = make_actions(fake)
    target = run(actions.screenshot(tmp_path))
    assert target.parent == tmp_path
    assert target.suffix == ".png"
    assert target.read_bytes() == fake.capture_bytes


def test_screenshot_expected_package_match(tmp_path):
    fake = FakeAdb()
    actions = make_actions(fake)
    shot = run(actions.screenshot(tmp_path / "a", expected_package="com.example.app"))
    assert shot.exists()
    meta = actions.last_screenshot_meta
    assert meta["expected"] == "com.example.app"
    assert meta["actual"] == "com.example.app"
    assert meta["mismatch"] is False
    assert meta["retries"] == 0
    assert len(fake.capture_calls) == 1


def test_screenshot_mismatch_retries_then_succeeds(tmp_path, monkeypatch):
    fake = FakeAdb(
        foreground_sequence=[
            "com.other.app",
            "com.other.app",
            "com.example.app",
            "com.example.app",
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(actions_module.asyncio, "sleep", fake_sleep)
    actions = make_actions(fake)
    shot = run(actions.screenshot(tmp_path / "a", expected_package="com.example.app"))
    assert shot.exists()
    meta = actions.last_screenshot_meta
    assert meta["mismatch"] is False
    assert meta["retries"] == 1
    assert len(fake.capture_calls) == 2
    assert sleeps == [0.25]


def test_screenshot_mismatch_exhausted_returns_last_picture(tmp_path, monkeypatch):
    fake = FakeAdb(foreground_sequence=["com.other.app"])
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(actions_module.asyncio, "sleep", fake_sleep)
    logs: list[str] = []
    actions = make_actions(fake, logger=logs.append)
    shot = run(
        actions.screenshot(
            tmp_path / "a",
            expected_package="com.example.app",
            retries=1,
        )
    )
    assert shot.read_bytes() == fake.capture_bytes
    meta = actions.last_screenshot_meta
    assert meta["mismatch"] is True
    assert meta["retries"] == 1
    assert meta["expected"] == "com.example.app"
    assert meta["actual"] == "com.other.app"
    assert len(fake.capture_calls) == 2
    assert sleeps == [0.25]
    assert any("不一致" in line for line in logs)


def test_screenshot_retries_zero_single_attempt(tmp_path):
    fake = FakeAdb(foreground_sequence=["com.other.app"])
    actions = make_actions(fake)
    run(
        actions.screenshot(
            tmp_path / "a",
            expected_package="com.example.app",
            retries=0,
        )
    )
    assert len(fake.capture_calls) == 1
    assert actions.last_screenshot_meta["mismatch"] is True


def test_screenshot_foreground_unknown_counts_as_mismatch(tmp_path):
    fake = FakeAdb(foreground_sequence=[""])  # 查询不到前台包
    actions = make_actions(fake)
    run(actions.screenshot(tmp_path / "a", expected_package="com.example.app", retries=0))
    assert actions.last_screenshot_meta["mismatch"] is True


def test_screenshot_slow_png_does_not_pin_app_to_raw(tmp_path):
    """慢链路上的慢 PNG 不应把 App 永久钉在 raw 路径上。

    复现 review 中的 UAT 限速场景：PNG 单次要 24.4s。旧逻辑只要超过固定阈值
    就把该 App 记入画像，之后每次都走 raw（5.76MB），越走越慢。
    """
    fake = FakeAdb(capture_duration_ms=24400, capture_route="png", prefer_raw=False)
    actions = make_actions(fake)
    run(actions.screenshot(tmp_path / "a"))
    assert actions.screenshot_profile() == {}
    run(actions.screenshot(tmp_path / "b"))
    assert [call["fast"] for call in fake.capture_calls] == [False, False]


def test_screenshot_remembers_raw_per_app_when_measured_faster(tmp_path):
    """只有 adb 层实测出 raw 更快、且本次确实走了 raw 时，才记住该 App。"""
    fake = FakeAdb(
        capture_format="jpeg",
        capture_bytes=b"\xff\xd8fake-jpeg",
        capture_duration_ms=300,
        capture_route="raw",
        prefer_raw=True,
    )
    actions = make_actions(fake)
    run(actions.screenshot(tmp_path / "a"))
    assert actions.screenshot_profile().get("com.example.app") is True
    run(actions.screenshot(tmp_path / "b"))
    assert [call["fast"] for call in fake.capture_calls] == [False, True]
    actions.reset_screenshot_profile()
    assert actions.screenshot_profile() == {}
    assert actions.last_screenshot_meta == {}
    run(actions.screenshot(tmp_path / "c"))
    assert fake.capture_calls[-1]["fast"] is False


def test_screenshot_explicit_fast_does_not_change_profile(tmp_path):
    fake = FakeAdb(capture_format="jpeg", capture_bytes=b"\xff\xd8fake-jpeg")
    actions = make_actions(fake)
    run(actions.screenshot(tmp_path / "a", fast=True))
    assert fake.capture_calls[-1]["fast"] is True
    assert actions.screenshot_profile() == {}
    assert actions.last_screenshot_meta["fast"] is True


def test_screenshot_empty_data_raises(tmp_path):
    fake = FakeAdb(capture_bytes=b"")
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.screenshot(tmp_path / "shot.png"))


def test_screenshot_capture_error_propagates(tmp_path):
    fake = FakeAdb(capture_error=AdbCommandError("截图失败"))
    actions = make_actions(fake)
    with pytest.raises(AdbCommandError):
        run(actions.screenshot(tmp_path / "shot.png"))


# ----------------------------------------------------------------------
# 等待
# ----------------------------------------------------------------------
def make_snapshot(texts: list[str]) -> ScreenSnapshot:
    nodes = [UiNode(ref=index, text=text) for index, text in enumerate(texts)]
    return ScreenSnapshot(
        package="com.example.app",
        activity="com.example.app.MainActivity",
        width=900,
        height=1600,
        nodes=nodes,
        captured_at=1_700_000_000.0,
    )


def test_wait_for_text_returns_node_on_match(monkeypatch):
    snapshots = [make_snapshot(["无"]), make_snapshot(["无", "目标文本"])]
    state = {"calls": 0}

    async def fake_dump(adb, *, timeout=None, max_retries=2):
        index = min(state["calls"], len(snapshots) - 1)
        state["calls"] += 1
        return snapshots[index]

    monkeypatch.setattr(actions_module, "dump_ui", fake_dump)
    fake = FakeAdb()
    actions = make_actions(fake)
    node = run(actions.wait_for_text("目标", timeout=2.0, interval=0.01))
    assert node is not None
    assert node.text == "目标文本"
    assert state["calls"] == 2


def test_wait_for_text_timeout_returns_none(monkeypatch):
    async def fake_dump(adb, *, timeout=None, max_retries=2):
        return make_snapshot(["无"])

    monkeypatch.setattr(actions_module, "dump_ui", fake_dump)
    fake = FakeAdb()
    actions = make_actions(fake)
    assert run(actions.wait_for_text("目标", timeout=0.05, interval=0.01)) is None


def test_wait_for_text_tolerates_dump_errors(monkeypatch):
    state = {"calls": 0}

    async def fake_dump(adb, *, timeout=None, max_retries=2):
        state["calls"] += 1
        raise UiDumpError("dump 失败")

    monkeypatch.setattr(actions_module, "dump_ui", fake_dump)
    fake = FakeAdb()
    actions = make_actions(fake)
    assert run(actions.wait_for_text("目标", timeout=0.05, interval=0.01)) is None
    assert state["calls"] >= 1


def test_wait_until_stable_returns_after_repeated_signature(monkeypatch):
    snapshots = [
        make_snapshot(["A"]),
        make_snapshot(["B"]),
        make_snapshot(["B"]),
    ]
    state = {"calls": 0}

    async def fake_dump(adb, *, timeout=None, max_retries=2):
        index = min(state["calls"], len(snapshots) - 1)
        state["calls"] += 1
        return snapshots[index]

    monkeypatch.setattr(actions_module, "dump_ui", fake_dump)
    fake = FakeAdb()
    actions = make_actions(fake)
    snapshot = run(actions.wait_until_stable(timeout=2.0, interval=0.01, stable_hits=2))
    assert [node.text for node in snapshot.nodes] == ["B"]
    assert state["calls"] == 3


def test_wait_until_stable_timeout_returns_last_snapshot(monkeypatch):
    state = {"calls": 0}

    async def fake_dump(adb, *, timeout=None, max_retries=2):
        index = state["calls"] % 2
        state["calls"] += 1
        return make_snapshot(["A" if index == 0 else "B"])

    monkeypatch.setattr(actions_module, "dump_ui", fake_dump)
    fake = FakeAdb()
    actions = make_actions(fake)
    snapshot = run(actions.wait_until_stable(timeout=0.05, interval=0.01, stable_hits=5))
    assert snapshot.nodes
    assert state["calls"] >= 2


def test_wait_until_stable_all_dumps_fail(monkeypatch):
    async def fake_dump(adb, *, timeout=None, max_retries=2):
        raise UiDumpError("dump 失败")

    monkeypatch.setattr(actions_module, "dump_ui", fake_dump)
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(UiDumpError):
        run(actions.wait_until_stable(timeout=0.05, interval=0.01))


def test_wait_for_text_rejects_empty_text():
    fake = FakeAdb()
    actions = make_actions(fake)
    with pytest.raises(ActionFailedError):
        run(actions.wait_for_text("  "))


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
def test_real_device_tap_swipe_input_screenshot(tmp_path):
    """真实设备：connect -> dump -> render -> tap -> 中文输入 -> swipe -> 截图。"""

    async def scenario():
        client = AdbClient(ADB_PATH, ADB_HOST, ADB_PORT, ADB_HOME, connect_timeout=20.0)
        try:
            info = await client.connect()
            actions = DeviceActions(client)
            size = await actions.screen_size()
            assert size == (info.width, info.height)

            # Chrome 地址栏是稳定的 EditText：用于验证 tap 聚焦 + ADBKeyboard 中文输入
            await actions.open_app("com.android.chromium")
            await asyncio.sleep(3.0)
            snapshot = await actions.wait_until_stable(timeout=20.0)
            assert snapshot.nodes
            assert "屏幕" in render_ui(snapshot)

            def find_url_bar(current_snapshot):
                return next(
                    (
                        node
                        for node in current_snapshot.nodes
                        if node.resource_id.endswith("url_bar")
                    ),
                    None,
                )

            url_bar = find_url_bar(snapshot)
            if url_bar is None:
                # 可能被首启页/弹窗挡住：按返回后重试一次
                await actions.press_key("back")
                await asyncio.sleep(1.0)
                snapshot = await dump_ui(client, timeout=20.0)
                url_bar = find_url_bar(snapshot)
            assert url_bar is not None, "Chrome 地址栏不可见（可能有首启弹窗）"

            await actions.tap(url_bar.bounds.center_x, url_bar.bounds.center_y)
            await asyncio.sleep(0.8)
            await actions.clear_text()
            expected = "你好世界"
            await actions.input_text(expected)
            await asyncio.sleep(1.0)
            typed_snapshot = await dump_ui(client, timeout=20.0)
            assert any(
                expected in (node.text or "") for node in typed_snapshot.nodes
            ), "ADBKeyboard 中文输入未出现在地址栏"

            await actions.clear_text()
            await asyncio.sleep(0.8)
            cleared_snapshot = await dump_ui(client, timeout=20.0)
            assert not any(
                expected in (node.text or "") for node in cleared_snapshot.nodes
            ), "clear_text 未清空地址栏"

            await actions.press_key("back")  # 收起输入法
            await asyncio.sleep(0.5)

            # Settings 列表上滑后界面指纹应发生变化
            await actions.open_app("com.android.settings")
            await asyncio.sleep(3.0)
            before = await actions.wait_until_stable(timeout=20.0)
            before_signature = screen_signature(before)
            # 列表可能已经在顶部或底部：任一方向的滑动使指纹变化即可
            await actions.swipe_direction("up", duration_ms=600)
            await asyncio.sleep(1.8)
            after = await dump_ui(client, timeout=20.0)
            after_signature = screen_signature(after)
            if after_signature == before_signature:
                await actions.swipe_direction("down", duration_ms=600)
                await asyncio.sleep(1.8)
                after = await dump_ui(client, timeout=20.0)
                after_signature = screen_signature(after)
            assert before_signature != after_signature

            foreground_package, _ = await actions.foreground()
            shot = await actions.screenshot(
                tmp_path / "device",
                expected_package=foreground_package,
            )
            meta = actions.last_screenshot_meta
            assert shot.exists()
            assert meta["method"] in ("png", "jpeg")
            assert meta["expected"] == foreground_package
            assert meta["mismatch"] is False
            expected_magic = (
                b"\x89PNG" if meta["method"] == "png" else b"\xff\xd8"
            )
            assert shot.read_bytes().startswith(expected_magic)

            # fast 路径（raw + 本地编码）与元数据
            fast_shot = await actions.screenshot(tmp_path / "device_fast", fast=True)
            fast_meta = actions.last_screenshot_meta
            assert fast_shot.exists()
            assert fast_meta["fast"] is True
            assert fast_meta["method"] in ("png", "jpeg")
            print(
                f"\n[真实设备截图] default={meta['method']}/{meta['duration_ms']}ms  "
                f"fast={fast_meta['method']}/{fast_meta['duration_ms']}ms"
            )
            await actions.press_key("home")
            return (
                size,
                int(meta["duration_ms"]),
                int(fast_meta["duration_ms"]),
                str(meta["method"]),
                str(fast_meta["method"]),
            )
        finally:
            await client.close()

    size, default_ms, fast_ms, default_method, fast_method = run(scenario())
    assert size[0] > 0 and size[1] > 0
    assert default_ms > 0 and fast_ms > 0
    assert default_method in ("png", "jpeg") and fast_method in ("png", "jpeg")


def test_app_frame_age_includes_post_capture_foreground_check(tmp_path):
    class App(FakeAdb):
        screen_source='app_scrcpy'
        last_screen_meta={'frame_age_ms':240,'frame_age_limit_ms':250}
        checks=0
        async def shell(self,*args,**kwargs):
            self.checks+=1
            if self.checks>1:await asyncio.sleep(.03)
            return await super().shell(*args,**kwargs)
    app=App(foreground_sequence=['com.example.app'])
    actions=make_actions(app)
    with pytest.raises(ActionFailedError,match='画面已超过'):
        run(actions.screenshot(tmp_path/'stale',expected_package='com.example.app'))
    assert not list(tmp_path.iterdir())
