"""ADB 异步客户端。

职责：管理 adb server / 设备连接，执行 shell / exec-out 命令，采集设备信息与截图。
仅依赖 Python 标准库，不导入 astrbot；日志通过构造函数注入的 logger 输出。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import struct
import time
from pathlib import Path
from typing import Callable, Sequence

# Pillow 为可选依赖：存在时用于 raw 截图本地编码，缺失时回退 ffmpeg / PNG
try:  # pragma: no cover - 依赖运行环境
    from PIL import Image as _PILImage
except Exception:  # noqa: BLE001
    _PILImage = None

from .errors import (
    AdbCommandError,
    AdbConnectError,
    AdbNotConfiguredError,
    AdbTimeoutError,
    DeviceOfflineError,
    PhoneControlError,
)
from .models import DeviceInfo

LogFn = Callable[[str], None]

# 设备失联类错误关键字：命中后自动重连一次
_OFFLINE_HINTS = (
    "device offline",
    "error: device",
    "device not found",
    "no devices",
    "emulators found",
    "connection reset",
    "cannot connect",
    "failed to connect",
    "adb server didn't ack",
    "adb: device",
    "error: closed",
)

# Android BatteryManager.BATTERY_STATUS_* 的中文说明
_BATTERY_STATUS_LABELS = {
    1: "未知",
    2: "充电中",
    3: "放电中",
    4: "未充电",
    5: "已充满",
}

# 前台组件形如 `com.example/.MainActivity` 或 `com.example/com.example.MainActivity`
_PKG_ACT_RE = re.compile(r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)")
_WM_SIZE_RE = re.compile(r"(Physical|Override) size:\s*(\d+)x(\d+)")
_WM_DENSITY_RE = re.compile(r"(Physical|Override) density:\s*(\d+)")

# 一条 shell 命令同时查询窗口焦点，窗口信息缺失时回退到 Activity 栈信息
FOCUS_DUMP_COMMAND = (
    'dumpsys window displays | grep -E "mCurrentFocus|mFocusedApp"'
    ' || dumpsys activity activities | grep -E "mResumedActivity|topResumedActivity"'
)

# ---- 截图（PNG / raw）策略 ----
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8"
PNG_MIN_BYTES = 500             # PNG 数据长度下限，低于该值视为损坏
# 慢链路提示阈值：超过该值只记日志/作为诊断信号，**不再丢弃已获取的 PNG**。
# 慢链路下 raw 需传 5.76MB 裸像素（900x1600x4），是 PNG 的 4~5 倍，重传只会更慢。
PNG_SLOW_THRESHOLD_MS = 15000.0
# 自动选择 raw 的实测依据：raw 平均耗时需低于 PNG 的该比例才认为「确实更快」。
_RAW_PREFER_RATIO = 0.8
# 截图耗时记忆的指数滑动平均系数（越小越平滑）。
_CAPTURE_EWMA_ALPHA = 0.4
_JPEG_QUALITY = 82              # raw -> JPEG 的质量（体积与清晰度折中）
_RAW_HEADER_SIZE_NEW = 16       # Android 10+ 头：(w, h, format, colorspace)
_RAW_HEADER_SIZE_SRC = 12       # Android 10 之前的 12 字节头：(w, h, format)
_RAW_SEARCH_LIMIT = 4096        # raw 头部前导 warning 文本的最大搜索范围
_RAW_MAX_DIMENSION = 8192       # 单边最大像素，防异常头导致超大分配
_RAW_MAX_PIXELS = 32_000_000    # 总像素上限，防异常头内存爆炸
_RAW_SUPPORTED_FORMAT = 1       # PixelFormat.RGBA_8888
_FFMPEG_TIMEOUT_S = 15.0


def parse_foreground_dump(text: str) -> tuple[str, str]:
    """从 dumpsys window / activity 文本解析前台 ``(package, activity)``。

    解析失败返回两个空串，不抛异常。mCurrentFocus 优先级最高，其次
    mFocusedApp、topResumedActivity、mResumedActivity。
    """
    if not text:
        return "", ""
    priorities = ("mCurrentFocus=", "mFocusedApp=", "topResumedActivity=", "mResumedActivity")
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for keyword in priorities:
            if keyword not in stripped or keyword in found:
                continue
            match = _PKG_ACT_RE.search(stripped)
            if not match:
                continue
            package, activity = match.group(1), match.group(2)
            if activity.startswith("."):
                activity = package + activity
            found[keyword] = (package, activity)
    for keyword in priorities:
        if keyword in found:
            return found[keyword]
    return "", ""


def _decode(data: bytes | None) -> str:
    """把子进程输出解码为文本并去掉首尾空白。"""
    if not data:
        return ""
    return data.decode("utf-8", "replace").strip()


def _argv_text(argv: Sequence[str]) -> str:
    """把 argv 拼成可读命令文本（用于异常与日志）。"""
    return " ".join(str(item) for item in argv)


def _looks_offline(text: str) -> bool:
    """判断 adb 输出是否属于设备掉线/未连接类错误。"""
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _OFFLINE_HINTS)


def parse_wm_size(text: str) -> tuple[int, int]:
    """解析 ``wm size`` 输出，Override 优先于 Physical，失败返回 (0, 0)。"""
    physical: tuple[int, int] | None = None
    override: tuple[int, int] | None = None
    for match in _WM_SIZE_RE.finditer(text or ""):
        width, height = int(match.group(2)), int(match.group(3))
        if match.group(1) == "Override":
            override = (width, height)
        else:
            physical = (width, height)
    return override or physical or (0, 0)


def _parse_wm_density(text: str) -> int:
    """解析 ``wm density`` 输出，Override 优先于 Physical，失败返回 0。"""
    physical: int | None = None
    override: int | None = None
    for match in _WM_DENSITY_RE.finditer(text or ""):
        value = int(match.group(2))
        if match.group(1) == "Override":
            override = value
        else:
            physical = value
    return override or physical or 0


def _parse_battery(text: str) -> tuple[int | None, str]:
    """解析 ``dumpsys battery`` 的电量与状态。"""
    level: int | None = None
    status: int | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("level:"):
            try:
                level = int(stripped.split(":", 1)[1].strip())
            except (IndexError, ValueError):
                level = None
        elif stripped.startswith("status:"):
            try:
                status = int(stripped.split(":", 1)[1].strip())
            except (IndexError, ValueError):
                status = None
    label = _BATTERY_STATUS_LABELS.get(status, "") if status is not None else ""
    return level, label


def _parse_screen_on(text: str) -> bool | None:
    """解析 ``dumpsys power`` 的 mWakefulness，判断屏幕是否点亮。"""
    for line in (text or "").splitlines():
        if "mWakefulness=" not in line:
            continue
        value = line.split("mWakefulness=", 1)[1].strip().split()[0] if line.split("mWakefulness=", 1)[1].strip() else ""
        if not value:
            continue
        return value.lower() == "awake"
    return None


def _raw_header_values_ok(width: int, height: int, image_format: int) -> bool:
    """校验 raw 截图头中的尺寸与像素格式是否在合理范围。"""
    if not (1 <= width <= _RAW_MAX_DIMENSION and 1 <= height <= _RAW_MAX_DIMENSION):
        return False
    if width * height > _RAW_MAX_PIXELS:
        return False
    if image_format not in (1, 2, 3, 4):
        return False
    return True


def _peek_raw_header_16(data: bytes, offset: int) -> tuple[int, int, int, int] | None:
    """探测 ``offset`` 处是否为 16 字节头，返回 ``(width, height, format, pixel_len)``。"""
    if len(data) - offset < _RAW_HEADER_SIZE_NEW:
        return None
    width, height, image_format, colorspace = struct.unpack_from("<4I", data, offset)
    if not (_raw_header_values_ok(width, height, image_format) and colorspace <= 0xFFFF):
        return None
    return width, height, image_format, width * height * 4


def _peek_raw_header_12(data: bytes, offset: int) -> tuple[int, int, int, int] | None:
    """探测 ``offset`` 处是否为 SRC 兼容的 12 字节头。"""
    if len(data) - offset < _RAW_HEADER_SIZE_SRC:
        return None
    width, height, image_format = struct.unpack_from("<3I", data, offset)
    if not _raw_header_values_ok(width, height, image_format):
        return None
    return width, height, image_format, width * height * 4


def parse_screencap_raw(data: bytes) -> tuple[int, int, int, bytes]:
    """解析 ``adb exec-out screencap`` 的 raw 截图数据。

    - 优先按 16 字节头 ``(width, height, format, colorspace)`` 解析，
      兼容 Android 10 之前的 12 字节头 ``(width, height, format)``；
    - 容忍头部之前的 warning / 日志文本（最多搜索 ``_RAW_SEARCH_LIMIT`` 字节）；
    - 像素区按 SRC 的做法取尾部 ``width * height * 4`` 字节。

    数据过短、头部无法识别或像素长度不足时抛 ``AdbCommandError``。
    """
    if not data or len(data) < _RAW_HEADER_SIZE_SRC + 4:
        raise AdbCommandError(
            "raw 截图数据为空或过短",
            detail=f"长度 {len(data)} 字节",
        )
    search_end = min(len(data) - _RAW_HEADER_SIZE_SRC, _RAW_SEARCH_LIMIT)
    for offset in range(search_end + 1):
        modern = _peek_raw_header_16(data, offset)
        if modern is not None:
            width, height, image_format, pixel_length = modern
            available = len(data) - offset
            if available >= _RAW_HEADER_SIZE_NEW + pixel_length:
                pixels = data[-pixel_length:]  # SRC 兼容：取尾部像素区
                return width, height, image_format, pixels
            if offset == 0:
                # 现代 adb 恒为 16 字节头：头合法但像素不够，说明数据被截断
                raise AdbCommandError(
                    "raw 截图数据不完整",
                    detail=(
                        f"16 字节头声明 {width}x{height}，期望 "
                        f"{_RAW_HEADER_SIZE_NEW + pixel_length} 字节，实际 {len(data)} 字节"
                    ),
                )
            continue
        legacy = _peek_raw_header_12(data, offset)
        if legacy is not None:
            width, height, image_format, pixel_length = legacy
            if len(data) - offset >= _RAW_HEADER_SIZE_SRC + pixel_length:
                pixels = data[-pixel_length:]  # SRC 兼容：取尾部像素区
                return width, height, image_format, pixels
    raise AdbCommandError(
        "raw 截图头部解析失败",
        detail=f"长度 {len(data)} 字节，头部 {data[:32].hex()}",
    )


def validate_png_bytes(data: bytes) -> bytes:
    """校验 PNG 截图数据（魔数 + 最小长度），异常时抛 ``AdbCommandError``。"""
    if not data:
        raise AdbCommandError("截图返回空数据", detail="screencap -p 输出为空")
    if not data.startswith(_PNG_SIGNATURE):
        raise AdbCommandError(
            "截图数据不是有效 PNG",
            detail=f"输出前 16 字节：{data[:16].hex()}",
        )
    if len(data) <= PNG_MIN_BYTES:
        raise AdbCommandError(
            f"PNG 截图数据过短（{len(data)} 字节）",
            detail="疑似 adb 输出损坏，按数据异常处理",
        )
    return data


def repair_png_bytes(data: bytes) -> bytes | None:
    """按三种换行损坏形态尝试恢复 PNG 数据（参考 SRC 的截图解码策略）。

    依次尝试原样、``\\r\\n -> \\n``、``\\r\\r\\n -> \\n`` 三种形态并用 Pillow
    校验可解码性；无 Pillow 时只能做魔数/长度校验，直接返回原数据。
    返回 ``None`` 表示所有形态都无法解码，应视为 PNG 数据异常。
    """
    if _PILImage is None:
        return data
    candidates: list[bytes] = [data]
    normalized_crlf = data.replace(b"\r\n", b"\n")
    if normalized_crlf not in candidates:
        candidates.append(normalized_crlf)
    normalized_cr_crlf = data.replace(b"\r\r\n", b"\n")
    if normalized_cr_crlf not in candidates:
        candidates.append(normalized_cr_crlf)
    for candidate in candidates:
        try:
            with _PILImage.open(io.BytesIO(candidate)) as image:
                image.verify()
            return candidate
        except Exception:  # noqa: BLE001 - 逐个形态尝试，全部失败才算异常
            continue
    return None


def _convert_raw_with_pillow(
    pixels: bytes,
    width: int,
    height: int,
    image_format: str = "jpeg",
) -> bytes:
    """用 Pillow 把 RGBA raw 像素编码成 JPEG/PNG 字节。"""
    if _PILImage is None:
        raise AdbCommandError("Pillow 不可用，无法转换 raw 截图")
    try:
        image = _PILImage.frombytes("RGBA", (width, height), pixels)
        rgb = image.convert("RGB")
        buffer = io.BytesIO()
        if image_format == "jpeg":
            rgb.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
            expected_signature = _JPEG_SIGNATURE
        else:
            rgb.save(buffer, format="PNG")
            expected_signature = _PNG_SIGNATURE
    except Exception as exc:  # noqa: BLE001 - 统一转换为 AdbCommandError
        raise AdbCommandError(
            f"Pillow 转换 {image_format} 失败",
            detail=str(exc),
        ) from exc
    data = buffer.getvalue()
    if not data.startswith(expected_signature):
        raise AdbCommandError(f"Pillow 输出不是有效 {image_format.upper()}")
    return data


async def _convert_raw_with_ffmpeg(
    ffmpeg_path: str | Path,
    pixels: bytes,
    width: int,
    height: int,
    *,
    timeout: float,
) -> bytes:
    """用 ffmpeg 把 RGBA raw 像素编码成 PNG 字节。"""
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgba",
        "-video_size",
        f"{width}x{height}",
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "pipe:1",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AdbCommandError("启动 ffmpeg 失败", detail=str(exc)) from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=pixels),
            timeout=max(1.0, float(timeout)),
        )
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.communicate(), timeout=2.0)
        except (TimeoutError, ProcessLookupError):
            pass
        raise AdbCommandError(
            f"ffmpeg 转换超时（{timeout:.1f}s）",
            detail="已终止 ffmpeg 子进程",
        ) from None
    if process.returncode != 0:
        raise AdbCommandError(
            f"ffmpeg 转换失败（退出码 {process.returncode}）",
            detail=_decode(stderr)[:300],
        )
    if not stdout.startswith(_PNG_SIGNATURE):
        raise AdbCommandError("ffmpeg 输出不是有效 PNG", detail=stdout[:16].hex())
    return stdout


class AdbClient:
    """远程 ADB 设备的异步客户端。"""

    def __init__(
        self,
        adb_path: str | Path | None,
        host: str,
        port: int,
        home_dir: str | Path,
        *,
        serial: str = "",
        connect_timeout: float = 20.0,
        command_timeout: float = 30.0,
        logger: LogFn | None = None,
        allow_unconfigured: bool = False,
        defer_binary: bool = False,
    ) -> None:
        self._host = str(host or "").strip()
        self._port = int(port)
        resolved_serial = (serial or "").strip()
        # 记录序列号是否为显式配置：显式序列号优先于 host:port，重配时按同一规则解析目标
        self._serial_explicit = bool(resolved_serial)
        unconfigured = not resolved_serial and not self._host
        if unconfigured:
            if not allow_unconfigured:
                raise AdbNotConfiguredError(
                    "未配置 ADB 设备地址",
                    detail="请在插件配置中填写 adb.host / adb.port 或 adb.serial",
                )
            # 未配置时仍允许构造客户端（插件假开启）：保留占位路径，任何真实
            # 设备操作都会在 _ensure_configured 中报未配置，不会启动 adb。
            self._adb_path = Path(str(adb_path)) if adb_path else Path()
            self._unconfigured = True
        else:
            self._adb_path = Path(str(adb_path)) if defer_binary else self._resolve_adb_path(adb_path)
            self._unconfigured = False
        if not resolved_serial and self._host:
            resolved_serial = f"{self._host}:{self._port}"
        self._serial = resolved_serial
        self._home_dir = Path(home_dir).expanduser()
        self._connect_timeout = max(1.0, float(connect_timeout))
        self._command_timeout = max(1.0, float(command_timeout))
        self._logger = logger
        self._connected = False
        self._connecting = False
        self._lock_obj: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._png_slow_threshold_ms = PNG_SLOW_THRESHOLD_MS
        # 两条截图路径的实测平均耗时（毫秒，EWMA）；None 表示尚无样本。
        self._png_avg_ms: float | None = None
        self._raw_avg_ms: float | None = None
        self._last_capture_route: str = ""
        self._ffmpeg_path: Path | None = None
        self._ffmpeg_checked = False

    @property
    def configured(self) -> bool:
        """是否已配置可用的设备目标（主机或显式序列号）。"""
        return bool(self._serial) and not self._unconfigured

    def _ensure_configured(self) -> None:
        """执行设备命令前的未配置检查（假开启状态下禁止启动 adb）。"""
        if self.configured:
            return
        raise AdbNotConfiguredError(
            "未配置 ADB 地址/端口，插件当前为未启用状态",
            detail="请在插件配置 adb.host 与 adb.port 中填写后重新加载插件",
        )

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_adb_path(adb_path: str | Path) -> Path:
        """解析 adb 可执行文件路径（支持裸命令名走 PATH 查找）。"""
        raw = str(adb_path or "").strip()
        if not raw:
            raise AdbNotConfiguredError(
                "未配置 adb 可执行文件路径",
                detail="请重新加载插件准备官方 ADB，或在配置中填写 adb.path",
            )
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return candidate
        found = shutil.which(raw)
        if found:
            return Path(found)
        raise AdbNotConfiguredError(
            f"adb 可执行文件不存在：{raw}",
            detail="请重新加载插件准备官方 ADB，或在配置中填写正确的 adb.path",
        )

    @staticmethod
    def _coerce_timeout(name: str, value: object) -> float:
        """把超时收敛到与构造函数一致的下限（最少 1 秒）。"""
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError) as exc:
            raise AdbNotConfiguredError(
                f"{name} 非法：{value!r}",
                detail="超时必须是不少于 1 秒的数值",
            ) from exc

    def _log(self, message: str) -> None:
        """写入注入的日志回调；回调异常不影响主流程。"""
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - 日志回调不允许打断设备操作
            pass

    def _lock(self) -> asyncio.Lock:
        """返回当前事件循环绑定的连接锁（兼容多事件循环的测试场景）。"""
        loop = asyncio.get_running_loop()
        if self._lock_obj is None or self._lock_loop is not loop:
            self._lock_obj = asyncio.Lock()
            self._lock_loop = loop
        return self._lock_obj

    def _base_command(self) -> list[str]:
        """返回带设备序列号的 adb 命令前缀。"""
        self._ensure_configured()
        return [str(self._adb_path), "-s", self._serial]

    def _ensure_home(self) -> None:
        """确保 adb 的 HOME / ANDROID_USER_HOME 目录存在且可写。"""
        try:
            self._home_dir.mkdir(parents=True, exist_ok=True)
            (self._home_dir / ".android").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AdbNotConfiguredError(
                "无法创建 adb 运行目录",
                detail=f"{self._home_dir}: {exc}",
            ) from exc

    def _build_env(self) -> dict[str, str]:
        """构造子进程环境变量，隔离容器/设备授权状态。"""
        env = os.environ.copy()
        env["HOME"] = str(self._home_dir)
        env["ANDROID_USER_HOME"] = str(self._home_dir / ".android")
        return env

    # ------------------------------------------------------------------
    # 子进程执行
    # ------------------------------------------------------------------
    async def _run_adb(self, argv: Sequence[str], *, timeout: float | None = None, output_limit: int | None = None) -> tuple[int, bytes, bytes]:
        """启动一次 adb 子进程并返回 ``(returncode, stdout, stderr)``。

        超时或启动失败时抛 ``AdbCommandError`` / ``AdbNotConfiguredError``；
        不做返回值语义判断（由 ``_execute`` 处理）。
        """
        if not argv:
            raise AdbCommandError("ADB 命令为空")
        self._ensure_configured()
        self._ensure_home()
        effective_timeout = self._command_timeout if timeout is None else max(0.05, float(timeout))
        env = self._build_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AdbNotConfiguredError(
                f"adb 可执行文件无法运行：{self._adb_path}",
                detail=str(exc),
            ) from exc
        except OSError as exc:
            raise AdbCommandError(
                "启动 adb 子进程失败",
                command=_argv_text(argv),
                detail=str(exc),
            ) from exc
        async def read_bounded(stream: asyncio.StreamReader) -> bytes:
            data = bytearray()
            truncated = False
            while chunk := await stream.read(8192):
                remaining = max(0, int(output_limit) - len(data))
                data.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining
            return bytes(data) + (b"\n[output truncated]" if truncated else b"")

        async def collect() -> tuple[bytes, bytes]:
            if output_limit is None:
                return await proc.communicate()
            stdout, stderr = await asyncio.gather(read_bounded(proc.stdout), read_bounded(proc.stderr))
            await proc.wait()
            return stdout, stderr

        try:
            stdout, stderr = await asyncio.wait_for(collect(), timeout=effective_timeout)
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise
        except TimeoutError:
            detail = f"：{_argv_text(argv)}" if output_limit is None else ""
            self._log(f"adb 命令超时（{effective_timeout:.1f}s），终止进程{detail}")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except (TimeoutError, ProcessLookupError):
                pass
            raise AdbTimeoutError(
                f"ADB 命令超时（{effective_timeout:.1f}s）",
                command=_argv_text(argv),
                detail="超时后已终止 adb 子进程",
            ) from None
        returncode = proc.returncode if proc.returncode is not None else -1
        return returncode, stdout or b"", stderr or b""

    async def _execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        retry: bool = True,
    ) -> tuple[int, bytes, bytes]:
        """执行 adb 命令；返回码非 0 时按错误类型归类，掉线时自动重连重试一次。"""
        rc, stdout, stderr = await self._run_adb(argv, timeout=timeout)
        if rc == 0:
            return rc, stdout, stderr
        command = _argv_text(argv)
        stderr_text = _decode(stderr)
        detail = stderr_text or _decode(stdout)
        if retry and _looks_offline(detail):
            if self._connecting:
                raise DeviceOfflineError(
                    "设备离线",
                    command=command,
                    returncode=rc,
                    stderr=stderr_text,
                    detail=detail[:500],
                )
            self._log(f"adb 命令疑似掉线，尝试重连一次：{command}")
            try:
                await self._reconnect()
            except PhoneControlError as exc:
                raise DeviceOfflineError(
                    "设备离线且重连失败",
                    command=command,
                    returncode=rc,
                    stderr=stderr_text,
                    detail=str(exc),
                ) from exc
            rc, stdout, stderr = await self._run_adb(argv, timeout=timeout)
            if rc == 0:
                return rc, stdout, stderr
            command = _argv_text(argv)
            stderr_text = _decode(stderr)
            detail = stderr_text or _decode(stdout)
            if _looks_offline(detail):
                raise DeviceOfflineError(
                    "设备离线（已重连一次仍失败）",
                    command=command,
                    returncode=rc,
                    stderr=stderr_text,
                    detail=detail[:500],
                )
            raise AdbCommandError(
                f"ADB 命令失败（退出码 {rc}）",
                command=command,
                returncode=rc,
                stderr=stderr_text,
                detail=detail[:500],
            )
        raise AdbCommandError(
            f"ADB 命令失败（退出码 {rc}）",
            command=command,
            returncode=rc,
            stderr=stderr_text,
            detail=detail[:500],
        )

    async def _shell_optional(self, command: str, *, timeout: float | None = None, retry: bool = True) -> str:
        """执行可选 shell 命令：失败时记录日志并返回空串，不中断流程。"""
        try:
            return await self.shell(command, timeout=timeout, retry=retry)
        except PhoneControlError as exc:
            self._log(f"可选命令执行失败：{command} -> {exc}")
            return ""

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    @property
    def serial(self) -> str:
        """设备序列号（默认是 ``host:port``，也可由调用方显式指定）。"""
        return self._serial

    async def _is_online_raw(self) -> bool:
        """查询 ``adb devices``，判断当前序列号是否处于 device 状态。"""
        try:
            rc, stdout, _stderr = await self._run_adb(
                [str(self._adb_path), "devices"],
                timeout=min(15.0, max(5.0, self._connect_timeout)),
            )
        except PhoneControlError as exc:
            self._log(f"查询 adb devices 失败：{exc}")
            return False
        if rc != 0:
            return False
        for line in _decode(stdout).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self._serial and parts[1] == "device":
                return True
        return False

    async def is_online(self) -> bool:
        """设备当前是否在线（adb devices 状态为 device）。"""
        return await self._is_online_raw()

    async def _quiet_disconnect(self) -> None:
        """尽力断开设备连接，忽略一切错误。"""
        try:
            await self._run_adb(
                [str(self._adb_path), "disconnect", self._serial],
                timeout=min(10.0, self._connect_timeout),
            )
        except PhoneControlError as exc:
            self._log(f"断开旧连接失败（忽略）：{exc}")

    async def _reconnect(self) -> None:
        """持锁重建连接（供命令失败重试使用）。"""
        async with self._lock():
            self._connected = False
            await self._do_connect(force=True)

    async def _do_connect(self, force: bool) -> DeviceInfo:
        """实际建连流程；调用方必须已持有连接锁。"""
        self._connecting = True
        try:
            if force:
                await self._quiet_disconnect()
            rc, stdout, stderr = await self._run_adb(
                [str(self._adb_path), "start-server"],
                timeout=self._connect_timeout,
            )
            if rc != 0:
                raise AdbConnectError(
                    "adb server 启动失败",
                    detail=(_decode(stderr) or _decode(stdout))[:500],
                )
            deadline = asyncio.get_running_loop().time() + self._connect_timeout
            last_detail = ""
            online = False
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    rc, stdout, stderr = await self._run_adb(
                        [str(self._adb_path), "connect", self._serial],
                        timeout=max(0.05, min(10.0, remaining)),
                    )
                except AdbTimeoutError as exc:
                    # 子进程回收后，ADB server 的握手仍可能在总连接窗口内完成。
                    last_detail = str(exc)
                else:
                    message = (_decode(stdout) + " " + _decode(stderr)).strip()
                    if message:
                        last_detail = message
                # 不用 connect 的输出判定成败（不同 adb 版本返回码不一致），统一以 devices 为准
                online = await self._is_online_raw()
                if online:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.5, remaining))
            if not online:
                raise AdbConnectError(
                    f"连接设备失败：{self._serial}",
                    detail=last_detail[:500] or f"连接超时（{self._connect_timeout:.1f}s）",
                )
            try:
                info = await self._device_info(retry=False)
            except PhoneControlError as exc:
                raise AdbConnectError(
                    "连接后读取设备信息失败",
                    detail=str(exc),
                ) from exc
            self._connected = True
            self._log(
                f"已连接设备 {self._serial}（{info.model or '未知机型'}，"
                f"{info.width}x{info.height}，Android {info.android_version or '?'}）"
            )
            return info
        finally:
            self._connecting = False

    async def connect(self, force: bool = False) -> DeviceInfo:
        """建立连接并返回设备信息；``force=True`` 时先断开旧连接再重连。"""
        self._ensure_configured()
        async with self._lock():
            return await self._do_connect(force=force)

    async def ensure_ready(self) -> None:
        """幂等就绪检查：尚未连接时自动调用 ``connect``。"""
        self._ensure_configured()
        if self._connected:
            return
        await self.connect()

    async def reconfigure(
        self,
        *,
        adb_path: str | Path | None = None,
        host: str | None = None,
        port: int | None = None,
        serial: str | None = None,
        connect_timeout: float | None = None,
        command_timeout: float | None = None,
    ) -> dict[str, object]:
        """按运行中的插件配置热更新 ADB 连接参数。

        参数语义：``None`` 表示保持原值；``serial=""`` 表示清空显式序列号、
        回退到 ``host:port``；``adb_path=None`` 或空串表示沿用当前已解析的
        adb 路径（配置留空时由调用方先解析出插件自带 adb 再传入）。

        目标（serial 优先，否则 ``host:port``）或 adb 路径变化时，先用旧目标
        静默断开连接，再更新字段并把 ``_connected`` 置为 False；本次不主动
        连接、不重启 adb server，下次 ``ensure_ready`` / ``connect`` 会自动
        连接新目标。目标为空或端口非法时抛 ``AdbNotConfiguredError``，校验
        失败不改动任何现有状态。

        Returns:
            ``{"changed": bool, "target_changed": bool, "path_changed": bool,
            "timeouts_changed": bool, "serial": str, "adb_path": str,
            "connect_timeout": float, "command_timeout": float}``。
        """
        # 先在局部变量上完成全部校验与取值计算：任何一项非法都在断开旧连接
        # 之前抛错，避免出现「旧连接已断、新参数未生效」的半更新状态。
        new_host = self._host if host is None else str(host or "").strip()
        if port is None:
            new_port = self._port
        else:
            try:
                new_port = int(port)
            except (TypeError, ValueError) as exc:
                raise AdbNotConfiguredError(
                    f"adb.port 非法：{port!r}",
                    detail="端口必须是 1-65535 的整数",
                ) from exc
            if not 1 <= new_port <= 65535:
                raise AdbNotConfiguredError(
                    f"adb.port 超出范围：{new_port}",
                    detail="端口必须是 1-65535 的整数",
                )
        if serial is None:
            # 未显式传入序列号时沿用原状态：原序列号是派生的就继续派生，
            # 这样只改 host/port 也能正确重算出新目标。
            new_serial_explicit = self._serial_explicit
            new_serial_value = self._serial if self._serial_explicit else ""
        else:
            new_serial_value = str(serial or "").strip()
            new_serial_explicit = bool(new_serial_value)
        if new_serial_explicit:
            new_target = new_serial_value
        elif new_host:
            new_target = f"{new_host}:{new_port}"
        else:
            new_target = ""
        if not new_target:
            raise AdbNotConfiguredError(
                "未配置 ADB 设备地址",
                detail="请填写 adb.host / adb.port 或 adb.serial",
            )
        new_adb_path: Path | None = None
        if adb_path is not None and str(adb_path).strip():
            # 与构造逻辑一致：重新解析、校验可执行文件存在性
            new_adb_path = self._resolve_adb_path(adb_path)
        new_connect_timeout = (
            self._connect_timeout
            if connect_timeout is None
            else self._coerce_timeout("adb.connect_timeout", connect_timeout)
        )
        new_command_timeout = (
            self._command_timeout
            if command_timeout is None
            else self._coerce_timeout("adb.command_timeout", command_timeout)
        )

        target_changed = new_target != self._serial
        path_changed = new_adb_path is not None and new_adb_path != self._adb_path
        host_changed = new_host != self._host
        port_changed = new_port != self._port
        timeouts_changed = (
            new_connect_timeout != self._connect_timeout
            or new_command_timeout != self._command_timeout
        )
        changed = (
            target_changed
            or path_changed
            or host_changed
            or port_changed
            or new_serial_explicit != self._serial_explicit
            or timeouts_changed
        )
        result: dict[str, object] = {
            "changed": changed,
            "target_changed": target_changed,
            "path_changed": path_changed,
            "timeouts_changed": timeouts_changed,
            "serial": new_target,
            "adb_path": str(new_adb_path or self._adb_path),
            "connect_timeout": new_connect_timeout,
            "command_timeout": new_command_timeout,
        }
        if not changed:
            # 同目标同参数的重复应用为幂等空操作：不断连、不发任何 adb 命令
            return result

        # 连接目标或 adb 路径变化会改变命令实际作用的设备/二进制，必须重置连接态
        reconnect_required = target_changed or path_changed
        async with self._lock():
            if reconnect_required:
                # 必须在更新字段前按旧 adb 路径 / 旧序列号断开
                await self._quiet_disconnect()
            self._host = new_host
            self._port = new_port
            self._serial = new_target
            self._serial_explicit = new_serial_explicit
            self._unconfigured = False
            if new_adb_path is not None:
                self._adb_path = new_adb_path
            self._connect_timeout = new_connect_timeout
            self._command_timeout = new_command_timeout
            if reconnect_required:
                self._connected = False
        if reconnect_required:
            self._log(f"ADB 连接参数已更新，旧连接已断开：{self._serial}")
        else:
            self._log("ADB 运行参数已更新（连接目标未变）")
        return result

    async def clear_target(self) -> None:
        """清空连接目标进入未配置状态（ADB 配置被清空时释放旧设备句柄）。"""
        await self._quiet_disconnect()
        async with self._lock():
            self._host = ""
            self._port = 0
            self._serial = ""
            self._serial_explicit = False
            self._unconfigured = True
            self._connected = False
        self._log("ADB 目标已清空，客户端进入未配置状态")

    async def disconnect(self) -> None:
        """断开当前设备连接（忽略错误）。"""
        await self._quiet_disconnect()
        self._connected = False
        self._log(f"已断开设备 {self._serial}")

    async def close(self) -> None:
        """释放客户端状态（不杀共享 adb server）。"""
        self._connected = False
        self._log("AdbClient 已关闭")

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------
    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> str:
        """执行 adb 客户端子命令（root/reverse/push 等），返回 stdout 文本。

        与 :meth:`shell` 不同，这里不追加 ``shell``，而是直接调用 adb 客户端命令；
        失败时抛 :class:`AdbCommandError`。
        """
        argv = [*self._base_command(), *[str(item) for item in args]]
        rc, stdout, stderr = await self._run_adb(argv, timeout=timeout)
        if rc != 0:
            stderr_text = _decode(stderr)
            raise AdbCommandError(
                f"ADB 命令失败（退出码 {rc}）",
                command=_argv_text(argv),
                returncode=rc,
                stderr=stderr_text,
                detail=(stderr_text or _decode(stdout))[:500],
            )
        return _decode(stdout).strip()

    async def shell(self, command: str, *, timeout: float | None = None, retry: bool = True) -> str:
        """执行 ``adb shell <command>``，返回解码后的 stdout。"""
        rc, stdout, stderr = await self._execute(
            self._base_command() + ["shell", command],
            timeout=timeout,
            retry=retry,
        )
        return _decode(stdout)

    async def shell_result(self, command: str, *, timeout: float) -> dict:
        """单次设备 shell 执行，保留退出码与两路有界输出。"""
        rc, stdout, stderr = await self._run_adb(
            self._base_command() + ["shell", command], timeout=timeout, output_limit=8192
        )
        return {"exit_code": rc, "stdout": _decode(stdout), "stderr": _decode(stderr)}

    async def exec_out(self, *args: str, timeout: float | None = None) -> bytes:
        """执行 ``adb exec-out`` 并返回原始字节（适用于截图、cat 二进制）。"""
        if not args:
            raise AdbCommandError("exec_out 缺少命令参数")
        rc, stdout, stderr = await self._execute(
            self._base_command() + ["exec-out"] + [str(arg) for arg in args],
            timeout=timeout,
        )
        return stdout

    async def dump_ui_output(self, path: str, *, timeout: float | None = None) -> bytes:
        """使用设备可用的系统运行时采集节点，保留进程退出校验。"""
        from .ui_commands import ui_dump_command

        return await self.exec_out("sh", "-c", ui_dump_command(path), timeout=timeout)

    async def screenshot_png(self, *, timeout: float | None = None) -> bytes:
        """截取当前屏幕，返回 PNG 字节（校验魔数与最小长度）。"""
        data = await self.exec_out("screencap", "-p", timeout=timeout)
        return validate_png_bytes(data)

    # ------------------------------------------------------------------
    # 截图：PNG / raw 自适应
    # ------------------------------------------------------------------
    def _ffmpeg_executable(self) -> Path | None:
        """查找 ffmpeg 可执行文件（带缓存，可用 PHONE_FFMPEG_PATH 覆盖）。"""
        if self._ffmpeg_checked:
            return self._ffmpeg_path
        self._ffmpeg_checked = True
        override = str(os.environ.get("PHONE_FFMPEG_PATH") or "").strip()
        found = shutil.which(override or "ffmpeg")
        self._ffmpeg_path = Path(found) if found else None
        return self._ffmpeg_path

    async def _raw_to_image(
        self,
        pixels: bytes,
        width: int,
        height: int,
        image_format: int,
        *,
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        """把 raw 像素编码为图片：Pillow JPEG(82) -> Pillow PNG -> ffmpeg PNG。"""
        if image_format != _RAW_SUPPORTED_FORMAT:
            raise AdbCommandError(
                "不支持的 raw 截图像素格式",
                detail=f"format={image_format}（仅支持 1=RGBA_8888）",
            )
        errors: list[str] = []
        if _PILImage is not None:
            for target_format in ("jpeg", "png"):
                try:
                    return _convert_raw_with_pillow(pixels, width, height, target_format), target_format
                except AdbCommandError as exc:
                    errors.append(f"Pillow {target_format}: {exc}")
                    self._log(f"raw 转 {target_format.upper()} 失败：{exc}")
        else:
            errors.append("Pillow 不可用")
        ffmpeg_path = self._ffmpeg_executable()
        if ffmpeg_path is not None:
            convert_timeout = max(5.0, float(timeout)) if timeout is not None else _FFMPEG_TIMEOUT_S
            try:
                data = await _convert_raw_with_ffmpeg(
                    ffmpeg_path,
                    pixels,
                    width,
                    height,
                    timeout=convert_timeout,
                )
                return data, "png"
            except AdbCommandError as exc:
                errors.append(f"ffmpeg: {exc}")
                self._log(f"raw 转 PNG（ffmpeg）失败：{exc}")
        else:
            errors.append("ffmpeg 不可用")
        raise AdbCommandError("raw 截图转换失败", detail="；".join(errors)[:500])

    async def _capture_raw_image(self, *, timeout: float | None = None) -> tuple[bytes, str]:
        """执行 raw ``screencap`` 并转换；解析异常时按契约重试一次。"""
        last_error: AdbCommandError | None = None
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(0.05)
            raw = await self.exec_out("screencap", timeout=timeout)
            try:
                width, height, image_format, pixels = parse_screencap_raw(raw)
            except AdbCommandError as exc:
                last_error = exc
                self._log(f"raw 截图解析失败（第 {attempt + 1}/2 次）：{exc}")
                continue
            return await self._raw_to_image(
                pixels,
                width,
                height,
                image_format,
                timeout=timeout,
            )
        raise last_error if last_error is not None else AdbCommandError("raw 截图失败")

    async def _capture_png_image(self, *, timeout: float | None = None) -> bytes:
        """执行 PNG 截图并做数据校验（含换行损坏修复）。"""
        data = await self.screenshot_png(timeout=timeout)
        repaired = repair_png_bytes(data)
        if repaired is None:
            raise AdbCommandError(
                "PNG 截图数据校验失败",
                detail="三种换行形态均无法解码，按数据异常处理",
            )
        return repaired

    def _record_capture_duration(self, route: str, duration_ms: int) -> None:
        """记录一次成功截图的实测耗时（EWMA），作为「raw 是否更快」的唯一依据。"""
        value = float(max(0, duration_ms))
        attr = "_png_avg_ms" if route == "png" else "_raw_avg_ms"
        previous = getattr(self, attr)
        setattr(
            self,
            attr,
            value if previous is None else previous + _CAPTURE_EWMA_ALPHA * (value - previous),
        )
        self._last_capture_route = route

    def _drop_raw_sample(self) -> None:
        """丢弃 raw 耗时样本：该路径刚失败，不能再按失效的结论优先 raw。"""
        self._raw_avg_ms = None

    @property
    def last_capture_route(self) -> str:
        """最近一次成功截图实际走的路径（``"png"`` / ``"raw"``）。"""
        return self._last_capture_route

    def capture_stats(self) -> dict[str, float | None]:
        """两条截图路径的实测平均耗时（毫秒），供日志与诊断使用。"""
        return {"png_avg_ms": self._png_avg_ms, "raw_avg_ms": self._raw_avg_ms}

    def prefers_raw_capture(self) -> bool:
        """自动模式是否应优先走 raw 路径。

        仅当两条路径都有实测样本、且 raw 平均耗时低于 PNG 的
        ``_RAW_PREFER_RATIO`` 时才成立。没有样本时一律走 PNG：
        PNG 约 1.2MB、raw 约 5.76MB，链路越慢 PNG 越占优。
        """
        png, raw = self._png_avg_ms, self._raw_avg_ms
        if png is None or raw is None or png <= 0:
            return False
        return raw <= png * _RAW_PREFER_RATIO

    async def capture_screen(
        self,
        *,
        fast: bool = False,
        timeout: float | None = None,
    ) -> tuple[bytes, str, int]:
        """截图并返回 ``(图片字节, "png"|"jpeg", 耗时毫秒)``。

        策略（**有效图片优先，不重复传输**）：

        - 默认先走 ``exec-out screencap -p``；只要 PNG 数据校验通过就立即返回，
          不会因为耗时超过 ``_png_slow_threshold_ms`` 而丢弃已到手的 PNG 重传 raw；
        - 仅当 PNG 命令失败或数据损坏时才改走 raw
          ``exec-out screencap`` + 本地编码（Pillow JPEG(82) / ffmpeg PNG）；
        - ``fast=True`` 直接走 raw 路径，失败再回退 PNG；
        - 自动模式只有在实测证明 raw 明显更快（``prefers_raw_capture()``）时
          才先试 raw，此时 PNG 尚未传输，不存在重复传输；
        - 两条路径都失败时抛 ``AdbCommandError``。
        """
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int(round((time.perf_counter() - started) * 1000))

        if fast:
            try:
                image, image_format = await self._capture_raw_image(timeout=timeout)
            except PhoneControlError as exc:
                self._log(f"fast 截图 raw 路径失败，回退 PNG：{exc}")
                self._drop_raw_sample()
            else:
                duration = elapsed_ms()
                self._record_capture_duration("raw", duration)
                return image, image_format, duration
            png = await self._capture_png_image(timeout=timeout)
            duration = elapsed_ms()
            self._record_capture_duration("png", duration)
            return png, "png", duration

        if self.prefers_raw_capture():
            # 已有实测样本表明 raw 更快；此时 PNG 还没开始传，先试 raw 不会造成重复传输
            try:
                image, image_format = await self._capture_raw_image(timeout=timeout)
            except PhoneControlError as exc:
                self._log(f"raw 路径（实测更快）失败，回退 PNG：{exc}")
                self._drop_raw_sample()
            else:
                duration = elapsed_ms()
                self._record_capture_duration("raw", duration)
                return image, image_format, duration

        png_error: PhoneControlError | None = None
        try:
            png = await self._capture_png_image(timeout=timeout)
        except PhoneControlError as exc:
            png_error = exc
            self._log(f"PNG 截图失败，尝试 raw 路径：{exc}")
        else:
            png_ms = elapsed_ms()
            self._record_capture_duration("png", png_ms)
            if png_ms > self._png_slow_threshold_ms:
                # 只记日志不重传：慢链路上 raw 是 PNG 的 4~5 倍数据量，重传只会更慢
                self._log(
                    f"PNG 截图耗时 {png_ms}ms 超过提示阈值 "
                    f"{self._png_slow_threshold_ms:.0f}ms，仍直接返回该 PNG"
                )
            return png, "png", png_ms

        try:
            image, image_format = await self._capture_raw_image(timeout=timeout)
        except PhoneControlError as raw_error:
            raise AdbCommandError(
                "截图失败（PNG 与 raw 路径均不可用）",
                detail=f"PNG：{png_error}；raw：{raw_error}",
            ) from png_error
        duration = elapsed_ms()
        self._record_capture_duration("raw", duration)
        return image, image_format, duration

    # ------------------------------------------------------------------
    # 设备信息
    # ------------------------------------------------------------------
    async def _device_info(self, *, retry: bool = True) -> DeviceInfo:
        """采集设备信息（内部实现，供 connect 复用）。"""
        if not await self._is_online_raw():
            raise DeviceOfflineError("设备不在线", detail=f"serial={self._serial}")
        props_text = await self._shell_optional(
            "getprop ro.product.model; getprop ro.product.manufacturer;"
            " getprop ro.build.version.release; getprop ro.build.version.sdk;"
            " getprop ro.kernel.qemu; getprop ro.hardware; getprop ro.build.hv.platform",
            retry=retry,
        )
        prop_lines = props_text.splitlines()
        model = prop_lines[0].strip() if len(prop_lines) > 0 else ""
        manufacturer = prop_lines[1].strip() if len(prop_lines) > 1 else ""
        android_version = prop_lines[2].strip() if len(prop_lines) > 2 else ""
        try:
            sdk = int(prop_lines[3].strip()) if len(prop_lines) > 3 and prop_lines[3].strip() else 0
        except ValueError:
            sdk = 0
        qemu = prop_lines[4].strip() if len(prop_lines) > 4 else ""
        hardware = prop_lines[5].strip().lower() if len(prop_lines) > 5 else ""
        if len(prop_lines) > 6:
            hardware += " " + prop_lines[6].strip().lower()
        device_kind = "emulator" if qemu == "1" or any(
            marker in (model + " " + manufacturer + " " + hardware).lower()
            for marker in ("mumu", "goldfish", "ranchu", "vbox", "nox")
        ) else ("physical" if qemu == "0" or hardware else "unknown")
        ime_text = await self._shell_optional("dumpsys input_method", retry=retry)
        visible = re.search(r"(?:mInputShown|mIsInputViewShown|isInputViewShown)=(true|false)", ime_text)
        keyboard_visible = visible.group(1) == "true" if visible else None
        size_text = await self._shell_optional("wm size; wm density", retry=retry)
        width, height = parse_wm_size(size_text)
        density = _parse_wm_density(size_text)
        battery_text = await self._shell_optional("dumpsys battery | head -40", retry=retry)
        battery_level, battery_status = _parse_battery(battery_text)
        power_text = await self._shell_optional('dumpsys power | grep -E "mWakefulness="', retry=retry)
        screen_on = _parse_screen_on(power_text)
        focus_text = await self._shell_optional(FOCUS_DUMP_COMMAND, retry=retry)
        current_package, current_activity = parse_foreground_dump(focus_text)
        return DeviceInfo(
            serial=self._serial,
            model=model,
            manufacturer=manufacturer,
            android_version=android_version,
            sdk=sdk,
            width=width,
            height=height,
            density=density,
            battery_level=battery_level,
            battery_status=battery_status,
            current_package=current_package,
            current_activity=current_activity,
            screen_on=screen_on,
            adb_connected=True,
            device_kind=device_kind,
            keyboard_visible=keyboard_visible,
        )

    async def device_info(self) -> DeviceInfo:
        """采集设备型号、分辨率、电量、前台组件、屏幕状态等信息。"""
        return await self._device_info(retry=True)
