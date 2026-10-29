"""设备动作封装：点击、滑动、文本输入、按键、应用控制与等待。

所有动作前都会调用 ``AdbClient.ensure_ready()``；动作之间不做额外 sleep，
节奏由调用方（浏览流程 / 工具层）控制。仅依赖标准库；日志通过注入的 logger 输出。

设备门禁通过公开属性 ``guard`` 注入（可构造后再绑定）：每个写动作与截图入口都会
先 ``await _guard_check(op)``，由门禁回调校验占用、暂停、取消代次与执行期限；回调
抛出的异常（如 ``ExecutionCancelled``）原样向上传播，调用方据此中止当前批次。
未注入门禁时动作只做原有的参数与连接校验。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import re
import time
from pathlib import Path
from typing import Any, Callable

from .adb import (
    FOCUS_DUMP_COMMAND,
    AdbClient,
    parse_foreground_dump,
    parse_wm_size,
)
from .errors import (
    ActionFailedError,
    AdbCommandError,
    AppNotInstalledError,
    PhoneControlError,
    UiDumpError,
)
from .models import ScreenSnapshot, UiNode
from .ui import dump_ui, find_nodes, screen_signature

LogFn = Callable[[str], None]
# 设备门禁回调：接收操作名，内部同步校验并可能抛 ExecutionCancelled；也兼容返回 awaitable。
GuardFn = Callable[[str], Any]

# press_key 支持的按键名与 adb keyevent 码
_KEY_CODES: dict[str, int] = {
    "back": 4,
    "home": 3,
    "enter": 66,
    "return": 66,
    "recents": 187,
    "app_switch": 187,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "tab": 61,
    "delete": 67,
    "backspace": 67,
    "escape": 111,
    "menu": 82,
    "search": 84,
    "space": 62,
    "camera": 27,
    "wakeup": 224,
    "sleep": 223,
}

_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# ADBKeyboard 广播协议
_ADBKEYBOARD_PACKAGE = "com.android.adbkeyboard"
_ADB_INPUT_B64 = "ADB_INPUT_B64"
_ADB_CLEAR_TEXT = "ADB_CLEAR_TEXT"

# 无 ADBKeyboard 时 clear_text 的退路：移动到行尾后连续删除固定次数
_CLEAR_DELETE_REPEAT = 120

# 双击/滑动等动作的最小参数
_MIN_SWIPE_DURATION_MS = 100

# 截图前台包校验失败后的重试间隔
_SCREENSHOT_RETRY_INTERVAL_S = 0.25
# 截图允许的落盘后缀
_SCREENSHOT_SUFFIXES = {".png": ".png", ".jpg": ".jpg", ".jpeg": ".jpg"}


def _validate_int(value: object, name: str) -> int:
    """把参数转成整数，非法时抛 ``ActionFailedError``。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ActionFailedError(f"{name} 必须是整数", detail=repr(value)) from exc


def _validate_package(package: str) -> str:
    """校验 Android 包名格式，避免把非法字符带进 shell 命令。"""
    name = (package or "").strip()
    if not _PACKAGE_RE.match(name):
        raise ActionFailedError("包名格式非法", detail=f"{package!r}，示例：com.ss.android.ugc.aweme")
    return name


def validate_touch_point(x: object, y: object, width: int, height: int) -> tuple[int, int]:
    """所有触控后端共用的屏幕坐标校验。"""
    x_int, y_int = _validate_int(x, "x"), _validate_int(y, "y")
    if not 0 <= x_int < width:
        raise ActionFailedError(f"x 坐标越界：{x_int}", detail=f"有效范围 0..{width - 1}")
    if not 0 <= y_int < height:
        raise ActionFailedError(f"y 坐标越界：{y_int}", detail=f"有效范围 0..{height - 1}")
    return x_int, y_int


def _escape_input_text(text: str) -> str:
    """把文本转成 ``input text`` 可用的 shell 参数（空格转 %s、单引号转义）。"""
    converted = text.replace(" ", "%s").replace("'", "'\\''")
    return f"'{converted}'"


class DeviceActions:
    """把常用 UI 动作封装成异步方法。"""

    def __init__(
        self,
        adb: AdbClient,
        *,
        logger: LogFn | None = None,
        guard: GuardFn | None = None,
    ) -> None:
        self._adb = adb
        self._logger = logger
        # 设备门禁回调：公开属性，插件可在构造动作层之后晚绑定；None 表示不校验。
        self.guard: GuardFn | None = guard
        self.app_open_guard = None
        self._screen_size: tuple[int, int] | None = None
        self._adbkeyboard_available: bool | None = None
        # 截图性能/正确性相关状态
        self.last_screenshot_meta: dict[str, object] = {}
        self._screenshot_profile: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        """写入注入的日志回调；回调异常不影响动作执行。"""
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - 日志回调不允许打断设备操作
            pass

    async def _guard_check(self, op: str) -> None:
        """设备操作前的统一门禁检查；未注入门禁时直接放行。

        门禁回调每次从公开属性 ``guard`` 读取（支持晚绑定），内部校验占用、
        暂停、取消代次与执行期限，失败时抛 ``ExecutionCancelled`` 中止当前批次。
        """
        guard = self.guard
        if guard is None:
            return
        result = guard(op)
        if inspect.isawaitable(result):
            await result

    async def _check_point(self, x: object, y: object) -> tuple[int, int]:
        """校验坐标落在屏幕范围内。"""
        width, height = await self.screen_size()
        return validate_touch_point(x, y, width, height)

    async def _has_adbkeyboard(self) -> bool:
        """检测设备上是否已启用 ADBKeyboard 输入法（结果缓存）。"""
        if self._adbkeyboard_available is not None:
            return self._adbkeyboard_available
        try:
            output = await self._adb.shell("ime list -s")
        except PhoneControlError as exc:
            # 连接类失败不缓存结果，等设备恢复后还能重新检测
            self._log(f"检测 ADBKeyboard 失败：{exc}")
            return False
        available = _ADBKEYBOARD_PACKAGE in output.lower()
        self._adbkeyboard_available = available
        if available:
            self._log("检测到 ADBKeyboard，中文/特殊文本走 ADB_INPUT_B64 广播")
        return available

    # ------------------------------------------------------------------
    # 屏幕尺寸
    # ------------------------------------------------------------------
    async def screen_size(self) -> tuple[int, int]:
        """返回屏幕分辨率 ``(width, height)``（首次查询后缓存）。"""
        await self._guard_check("screen_size")
        if self._screen_size is not None:
            return self._screen_size
        await self._adb.ensure_ready()
        output = await self._adb.shell("wm size")
        width, height = parse_wm_size(output)
        if width <= 0 or height <= 0:
            raise ActionFailedError(
                "无法解析屏幕分辨率",
                detail=f"wm size 输出：{output.strip()[:200]}",
            )
        self._screen_size = (width, height)
        self._log(f"屏幕分辨率：{width}x{height}")
        return self._screen_size

    # ------------------------------------------------------------------
    # 触摸动作
    # ------------------------------------------------------------------
    async def tap(self, x: int, y: int) -> None:
        """单击指定坐标。"""
        await self._guard_check("tap")
        await self._adb.ensure_ready()
        x_int, y_int = await self._check_point(x, y)
        await self._adb.shell(f"input tap {x_int} {y_int}")
        self._log(f"tap ({x_int}, {y_int})")

    async def double_tap(self, x: int, y: int, *, interval_ms: int = 120) -> None:
        """双击指定坐标；两次点击在同一条 shell 命令内串行执行以保证间隔。"""
        await self._guard_check("double_tap")
        await self._adb.ensure_ready()
        interval = _validate_int(interval_ms, "interval_ms")
        if interval <= 0:
            raise ActionFailedError("双击间隔必须为正整数", detail=f"interval_ms={interval}")
        x_int, y_int = await self._check_point(x, y)
        seconds = interval / 1000.0
        await self._adb.shell(
            f"input tap {x_int} {y_int}; sleep {seconds:.3f}; input tap {x_int} {y_int}"
        )
        self._log(f"double_tap ({x_int}, {y_int}), 间隔 {interval}ms")

    async def long_press(self, x: int, y: int, *, duration_ms: int = 800) -> None:
        """长按指定坐标（通过原地 swipe 实现）。"""
        await self._guard_check("long_press")
        await self._adb.ensure_ready()
        duration = _validate_int(duration_ms, "duration_ms")
        if duration < _MIN_SWIPE_DURATION_MS:
            raise ActionFailedError(
                f"长按时长不能小于 {_MIN_SWIPE_DURATION_MS}ms",
                detail=f"duration_ms={duration}",
            )
        x_int, y_int = await self._check_point(x, y)
        await self._adb.shell(f"input swipe {x_int} {y_int} {x_int} {y_int} {duration}")
        self._log(f"long_press ({x_int}, {y_int}), {duration}ms")

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 400,
    ) -> None:
        """从 ``(x1, y1)`` 滑动到 ``(x2, y2)``。"""
        await self._guard_check("swipe")
        await self._adb.ensure_ready()
        duration = _validate_int(duration_ms, "duration_ms")
        if duration < _MIN_SWIPE_DURATION_MS:
            raise ActionFailedError(
                f"滑动时长不能小于 {_MIN_SWIPE_DURATION_MS}ms",
                detail=f"duration_ms={duration}",
            )
        start_x, start_y = await self._check_point(x1, y1)
        end_x, end_y = await self._check_point(x2, y2)
        await self._adb.shell(f"input swipe {start_x} {start_y} {end_x} {end_y} {duration}")
        self._log(f"swipe ({start_x}, {start_y}) -> ({end_x}, {end_y}), {duration}ms")

    async def swipe_direction(
        self,
        direction: str,
        *,
        duration_ms: int = 400,
        ratio: float = 0.6,
    ) -> None:
        """按方向滑动：up/down/left/right。

        ``up`` 表示手指向上滑（信息流翻到下一条），其余方向同理。
        ``ratio`` 控制滑动距离占屏幕短边的比例，取值 (0, 1]。
        """
        name = str(direction or "").strip().lower()
        if name not in ("up", "down", "left", "right"):
            raise ActionFailedError(
                "滑动方向非法",
                detail=f"{direction!r}，可选：up/down/left/right",
            )
        try:
            ratio_value = float(ratio)
        except (TypeError, ValueError) as exc:
            raise ActionFailedError("ratio 必须是数字", detail=repr(ratio)) from exc
        if not 0 < ratio_value <= 1:
            raise ActionFailedError("ratio 必须在 (0, 1] 区间内", detail=f"ratio={ratio_value}")
        await self._adb.ensure_ready()
        width, height = await self.screen_size()
        center_x, center_y = width // 2, height // 2
        delta_x = int(width * ratio_value / 2)
        delta_y = int(height * ratio_value / 2)
        if name == "up":
            start_y = min(height - 1, center_y + delta_y)
            end_y = max(0, center_y - delta_y)
            await self.swipe(center_x, start_y, center_x, end_y, duration_ms=duration_ms)
        elif name == "down":
            start_y = max(0, center_y - delta_y)
            end_y = min(height - 1, center_y + delta_y)
            await self.swipe(center_x, start_y, center_x, end_y, duration_ms=duration_ms)
        elif name == "left":
            start_x = min(width - 1, center_x + delta_x)
            end_x = max(0, center_x - delta_x)
            await self.swipe(start_x, center_y, end_x, center_y, duration_ms=duration_ms)
        else:  # right
            start_x = max(0, center_x - delta_x)
            end_x = min(width - 1, center_x + delta_x)
            await self.swipe(start_x, center_y, end_x, center_y, duration_ms=duration_ms)

    # ------------------------------------------------------------------
    # 文本输入
    # ------------------------------------------------------------------
    async def input_text(self, text: str, *, press_enter: bool = False) -> None:
        """输入文本。

        优先使用 ADBKeyboard（支持中文，走 ``ADB_INPUT_B64`` 广播）；未安装
        ADBKeyboard 时仅支持 ASCII，走 ``input text`` 并对空格/引号转义。
        """
        await self._guard_check("input_text")
        if getattr(self._adb, "app_mode", False):
            await self._adb.input_text(text, press_enter=press_enter)
            return
        if not isinstance(text, str):
            raise ActionFailedError("text 必须是字符串", detail=repr(text))
        await self._adb.ensure_ready()
        if not text:
            if press_enter:
                await self.press_key("enter")
            return
        if await self._has_adbkeyboard():
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            await self._adb.shell(f"am broadcast -a {_ADB_INPUT_B64} --es msg {encoded}")
            self._log(f"ADBKeyboard 输入 {len(text)} 个字符")
        elif text.isascii():
            if "\n" in text or "\r" in text:
                raise ActionFailedError(
                    "input text 不支持换行",
                    detail="请安装并启用 ADBKeyboard（com.android.adbkeyboard）后再输入多行文本",
                )
            await self._adb.shell(f"input text {_escape_input_text(text)}")
            self._log(f"input text 输入 {len(text)} 个字符")
        else:
            raise ActionFailedError(
                "检测到非 ASCII 文本，但设备未启用 ADBKeyboard",
                detail=(
                    "请安装 ADBKeyboard 并在系统设置中启用（包名 com.android.adbkeyboard），"
                    "或改用纯英文/数字输入"
                ),
            )
        if press_enter:
            await self.press_key("enter")

    async def clear_text(self) -> None:
        """清空当前输入框内容。"""
        await self._guard_check("clear_text")
        await self._adb.ensure_ready()
        if await self._has_adbkeyboard():
            await self._adb.shell(f"am broadcast -a {_ADB_CLEAR_TEXT}")
            self._log("已通过 ADBKeyboard 清空输入框")
            return
        # 无 ADBKeyboard：先移动到行尾，再连续发送删除键
        delete_codes = " ".join(["67"] * _CLEAR_DELETE_REPEAT)
        await self._adb.shell(f"input keyevent 123 {delete_codes}")
        self._log(f"已发送 {_CLEAR_DELETE_REPEAT} 次删除键清空输入框")

    async def press_key(self, key: str) -> None:
        """按下系统按键（back/home/enter/recents/volume_* 等）。"""
        await self._guard_check("press_key")
        await self._adb.ensure_ready()
        name = str(key or "").strip().lower()
        if name.isdigit():
            code = int(name)
        else:
            code = _KEY_CODES.get(name, -1)
        if code is None or code < 0:
            raise ActionFailedError(
                "不支持的按键",
                detail=f"{key!r}，可选：{'/'.join(sorted(_KEY_CODES))}",
            )
        await self._adb.shell(f"input keyevent {code}")
        self._log(f"press_key {name} -> {code}")

    # ------------------------------------------------------------------
    # 应用控制
    # ------------------------------------------------------------------
    async def open_app(self, package: str) -> None:
        """通过 monkey 启动应用 Launcher 入口。"""
        await self._guard_check("open_app")
        name = _validate_package(package)
        await self._adb.ensure_ready()
        if not await self.is_installed(name):
            raise AppNotInstalledError(f"应用未安装：{name}")
        if self.app_open_guard is not None:
            await self.app_open_guard(name)
            await self._guard_check("open_app")
        try:
            output = await self._adb.shell(
                f"monkey -p {name} -c android.intent.category.LAUNCHER 1"
            )
        except AdbCommandError as exc:
            stderr_text = getattr(exc, "stderr", "") or ""
            if "no activities found" in f"{exc} {stderr_text}".lower():
                raise AppNotInstalledError(f"应用没有可启动的入口：{name}") from exc
            raise ActionFailedError("启动应用失败", detail=str(exc)) from exc
        if "no activities found" in output.lower() or "monkey aborted" in output.lower():
            raise AppNotInstalledError(f"应用没有可启动的入口：{name}", detail=output[-300:])
        self._log(f"已启动应用 {name}")

    async def force_stop(self, package: str) -> None:
        """强制停止应用。"""
        await self._guard_check("force_stop")
        name = _validate_package(package)
        await self._adb.ensure_ready()
        await self._adb.shell(f"am force-stop {name}")
        self._log(f"已停止应用 {name}")

    async def foreground(self) -> tuple[str, str]:
        """返回前台 ``(package, activity)``；解析失败返回两个空串。"""
        await self._adb.ensure_ready()
        try:
            query = getattr(self._adb,"foreground_output",None)
            output = await query() if callable(query) else await self._adb.shell(FOCUS_DUMP_COMMAND)
        except AdbCommandError as exc:
            self._log(f"查询前台应用失败：{exc}")
            return "", ""
        package, activity = parse_foreground_dump(output)
        if not package:
            self._log("未能解析出前台应用")
        return package, activity

    async def is_installed(self, package: str) -> bool:
        """判断应用是否已安装。"""
        name = _validate_package(package)
        await self._adb.ensure_ready()
        output = await self._adb.shell(f"pm list packages {name}")
        expected = f"package:{name}"
        installed = any(line.strip() == expected for line in output.splitlines())
        self._log(f"应用 {name} {'已安装' if installed else '未安装'}")
        return installed

    # ------------------------------------------------------------------
    # 等待与截图
    # ------------------------------------------------------------------
    async def wait_for_text(
        self,
        text: str,
        *,
        timeout: float = 10.0,
        interval: float = 0.6,
    ) -> UiNode | None:
        """轮询等待界面出现指定文本；超时返回 ``None``。"""
        await self._guard_check("wait_for_text")
        if not isinstance(text, str) or not text.strip():
            raise ActionFailedError("待查找文本不能为空")
        await self._adb.ensure_ready()
        loop = asyncio.get_running_loop()
        total = max(0.1, float(timeout))
        deadline = loop.time() + total
        while True:
            remaining = deadline - loop.time()
            try:
                snapshot = await dump_ui(
                    self._adb,
                    timeout=max(1.0, remaining),
                    max_retries=1,
                )
            except UiDumpError as exc:
                self._log(f"等待文本 {text!r} 时 dump 失败：{exc}")
            else:
                matched = find_nodes(snapshot, text=text)
                if matched:
                    return matched[0]
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._log(f"等待文本 {text!r} 超时（{total:.1f}s）")
                return None
            await asyncio.sleep(min(max(0.05, float(interval)), remaining))

    async def wait_until_stable(
        self,
        *,
        timeout: float = 8.0,
        interval: float = 0.5,
        stable_hits: int = 2,
    ) -> ScreenSnapshot:
        """轮询直到连续 ``stable_hits`` 次界面指纹相同，返回最后一张快照。"""
        await self._guard_check("wait_until_stable")
        await self._adb.ensure_ready()
        loop = asyncio.get_running_loop()
        total = max(0.1, float(timeout))
        deadline = loop.time() + total
        need_hits = max(1, _validate_int(stable_hits, "stable_hits"))
        previous_signature: str | None = None
        hits = 0
        last_snapshot: ScreenSnapshot | None = None
        last_error: Exception | None = None
        while True:
            remaining = deadline - loop.time()
            try:
                snapshot = await dump_ui(
                    self._adb,
                    timeout=max(1.0, remaining),
                    max_retries=1,
                )
            except UiDumpError as exc:
                last_error = exc
                self._log(f"等待界面稳定时 dump 失败：{exc}")
            else:
                last_snapshot = snapshot
                signature = screen_signature(snapshot)
                if signature == previous_signature:
                    hits += 1
                else:
                    hits = 1
                previous_signature = signature
                if hits >= need_hits:
                    return snapshot
            remaining = deadline - loop.time()
            if remaining <= 0:
                if last_snapshot is not None:
                    self._log(f"界面在 {total:.1f}s 内未完全稳定，返回最后一次快照")
                    return last_snapshot
                if last_error is not None:
                    raise last_error
                raise UiDumpError("等待界面稳定超时，且没有任何可用快照")
            await asyncio.sleep(min(max(0.05, float(interval)), remaining))

    def reset_screenshot_profile(self) -> None:
        """清空进程内截图策略缓存与最近一次截图元数据。"""
        self._screenshot_profile.clear()
        self.last_screenshot_meta = {}

    def screenshot_profile(self) -> dict[str, bool]:
        """返回各 App 的截图策略缓存副本（包名 -> 是否优先 raw 路径）。"""
        return dict(self._screenshot_profile)

    @staticmethod
    def _screenshot_suffix(image_format: str) -> str:
        """按实际图片格式返回落盘后缀。"""
        return ".jpg" if image_format == "jpeg" else ".png"

    @staticmethod
    def _resolve_screenshot_target(target: Path, image_format: str) -> Path:
        """按实际格式纠正落盘路径后缀（目录或已有后缀都会纠正）。"""
        suffix = DeviceActions._screenshot_suffix(image_format)
        if target.exists() and target.is_dir():
            return target / f"screenshot-{int(time.time() * 1000)}{suffix}"
        if target.suffix:
            return target.with_suffix(suffix)
        return target.with_name(target.name + suffix)

    async def screenshot(
        self,
        path: str | Path,
        *,
        expected_package: str | None = None,
        fast: bool = False,
        retries: int = 2,
    ) -> Path:
        """截图并保存，可选校验截图前后前台包是否为目标 App。

        - ``expected_package`` 非空时，截图前/后各查一次前台包；不一致则
          sleep 0.25s 后重试，最多 ``retries`` 次；仍不一致时返回最后一张
          可用画面（日志中标明 mismatch，不抛致命错误）；
        - ``fast=True`` 强制走 raw 截图路径；否则按进程内缓存的 App 画像
          自动选择，只有实测证明 raw 明显更快时才把该 App 记入画像
          （PNG 慢不会触发切换，慢链路下 raw 反而更慢）；
        - 成功后 ``last_screenshot_meta`` 记录 method/duration_ms/retries/
          expected/actual 等信息；落盘后缀按实际格式（.png / .jpg）。
        """
        await self._guard_check("screenshot")
        await self._adb.ensure_ready()
        expected = str(expected_package or "").strip()
        attempts = max(0, _validate_int(retries, "retries")) + 1
        last_data: bytes | None = None
        last_format = "png"
        last_duration_ms = 0
        last_before = ""
        last_after = ""
        used_fast = False
        mismatch = False
        attempts_used = 0
        capture_meta = {}
        captured_returned = 0.0
        for attempt in range(attempts):
            attempts_used = attempt
            before_package, _ = await self.foreground()
            app_key = expected or before_package
            cached_prefer_fast = bool(app_key and self._screenshot_profile.get(app_key, False))
            used_fast = bool(fast or cached_prefer_fast)
            data, image_format, duration_ms = await self._adb.capture_screen(
                fast=used_fast,
            )
            captured_returned=time.monotonic()
            capture_meta=dict(getattr(self._adb,"last_screen_meta",{})) if getattr(self._adb,"screen_source","")=="app_scrcpy" else {}
            if not data:
                raise ActionFailedError("截图数据为空")
            after_package, _ = await self.foreground()
            mismatch = bool(expected) and (
                before_package != expected or after_package != expected
            )
            last_data = data
            last_format = image_format
            last_duration_ms = duration_ms
            last_before = before_package
            last_after = after_package
            # 仅当调用方没有强制 fast、且实测证明 raw 确实更快时，才把该 App 记入画像。
            # 不能拿「PNG 耗时超过固定阈值」当依据：慢链路上 raw 要传 4~5 倍数据量，
            # 一次慢截图会被误记成「该 App 该一直用 raw」，之后每次都更慢。
            if not fast and app_key and not cached_prefer_fast:
                if self._adb.last_capture_route == "raw" and self._adb.prefers_raw_capture():
                    self._log(
                        f"App {app_key} 实测 raw 截图更快（{duration_ms}ms），"
                        "后续该 App 优先使用 raw 路径"
                    )
                    self._screenshot_profile[app_key] = True
            if not mismatch:
                break
            self._log(
                f"截图前台包校验不一致（期望 {expected}，截图前 "
                f"{last_before or '未知'}，截图后 {last_after or '未知'}），"
                f"重试 {attempt + 1}/{attempts}"
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(_SCREENSHOT_RETRY_INTERVAL_S)
        if last_data is None:
            raise ActionFailedError("截图失败：没有获取到任何画面")
        if "frame_age_ms" in capture_meta:
            capture_meta["frame_age_ms"]=round(capture_meta["frame_age_ms"]+(time.monotonic()-captured_returned)*1000,1)
            if capture_meta["frame_age_ms"]>capture_meta.get("frame_age_limit_ms",250):
                raise ActionFailedError("截图前台校验后画面已超过允许年龄，请降低推帧带宽或调整明确的帧龄上限")
        target = self._resolve_screenshot_target(Path(path).expanduser(), last_format)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(last_data)
        except OSError as exc:
            raise ActionFailedError("截图保存失败", detail=f"{target}: {exc}") from exc
        resolved = target.resolve()
        actual = last_after or last_before
        self.last_screenshot_meta = {
            **capture_meta,
            "method": last_format,
            "duration_ms": last_duration_ms,
            "retries": attempts_used,
            "expected": expected,
            "actual": actual,
            "before": last_before,
            "after": last_after,
            "mismatch": mismatch,
            "fast": used_fast,
            "format": last_format,
            "path": str(resolved),
        }
        if mismatch:
            self._log(
                f"警告：截图前后前台包与期望不一致（期望 {expected}，截图前 "
                f"{last_before or '未知'}，截图后 {last_after or '未知'}），"
                "已返回最后一张可用画面"
            )
        self._log(
            f"截图已保存：{resolved}（{last_format}，{last_duration_ms}ms，"
            f"重试 {attempts_used} 次）"
        )
        return resolved
