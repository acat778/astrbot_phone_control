"""插件配置快照。

AstrBot 下发的是可变配置 dict（WebUI 的 object 卡片会嵌套成子 dict），这里在
每次使用时解析成不可变的 dataclass 快照，避免长任务期间配置被改到一半。

兼容两种形态：

* 分组卡片：顶层 object 卡片，如 ``{"adb": {"host": ...}}``；
* 扁平结构：分组字段直接放在顶层，如 ``{"host": ...}``。

所有数值做范围收敛、所有布尔做宽松解析；非法值回退默认值并记录 warning。
ADB 主机/端口默认为空，表示「未配置」：插件仍可正常加载与使用面板/配置，
但不连接设备、不启动手机后台任务（假开启）。日志统一使用
``from astrbot.api import logger``；其余仅依赖标准库，便于本地单测。
"""

from __future__ import annotations

from .flexibility import FlexibilitySettings
from .mesh_config import MeshSettings, mesh_settings
from .tool_reports import ReportSettings
from .network_config import runtime_cards
from .play_window import parse_windows, validate_timezone

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .background import DEFAULT_KEEP_PACKAGES
from .verification import (
    DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
)
from .traffic import (
    DEFAULT_BODY_CHARS as DEFAULT_TRAFFIC_BODY_CHARS,
    DEFAULT_KEEP_SECONDS as DEFAULT_TRAFFIC_KEEP_SECONDS,
    DEFAULT_TRAFFIC_PORT,
)
from .media import (
    AUDIO_SOURCES as MEDIA_AUDIO_SOURCES,
    DEFAULT_AUDIO_SOURCE as DEFAULT_MEDIA_AUDIO_SOURCE,
    DEFAULT_FRAME_INTERVAL_SECONDS as DEFAULT_MEDIA_FRAME_INTERVAL_SECONDS,
    DEFAULT_MAX_FRAMES as DEFAULT_MEDIA_MAX_FRAMES,
    DEFAULT_MAX_SECONDS as DEFAULT_MEDIA_MAX_SECONDS,
    DEFAULT_SECONDS as DEFAULT_MEDIA_SECONDS,
    MIN_SECONDS as MEDIA_MIN_SECONDS,
)
from .share_policy import (
    DEFAULT_SHARE_COOLDOWN_MINUTES,
    DEFAULT_SHARE_END_HOUR,
    DEFAULT_SHARE_START_HOUR,
    DEFAULT_SHARE_TIMEZONE,
    DEFAULT_USER_RECENT_HOURS,
)
from .novelty import (
    DEFAULT_GAIN as DEFAULT_NOVELTY_GAIN,
    DEFAULT_HALF_LIFE_HOURS as DEFAULT_NOVELTY_HALF_LIFE_HOURS,
    DEFAULT_INJECT_LIMIT as DEFAULT_NOVELTY_INJECT_LIMIT,
    DEFAULT_MAX_ENTRIES as DEFAULT_NOVELTY_MAX_ENTRIES,
    DEFAULT_SIMILARITY as DEFAULT_NOVELTY_SIMILARITY,
    DEFAULT_THRESHOLD as DEFAULT_NOVELTY_THRESHOLD,
    NoveltySettings,
)
from .wireguard import (
    DEFAULT_ADB_LOCAL_PORT as DEFAULT_WIREGUARD_ADB_LOCAL_PORT,
    DEFAULT_INTERFACE_ADDRESS as DEFAULT_WIREGUARD_INTERFACE_ADDRESS,
    DEFAULT_LISTEN_PORT as DEFAULT_WIREGUARD_LISTEN_PORT,
    DEFAULT_MODE as DEFAULT_WIREGUARD_MODE,
    DEFAULT_PHONE_WG_IP as DEFAULT_WIREGUARD_PHONE_WG_IP,
    MODES as WIREGUARD_MODES,
    WireGuardSettings,
)

# ADB 主机/端口默认留空：空值表示未配置，插件以“假开启”状态加载。
DEFAULT_ADB_HOST = ""
DEFAULT_ADB_PORT = 0
# 未配置主机/端口时所有设备能力的统一错误文案（命令/工具/提示词共用）。
UNCONFIGURED_MESSAGE = (
    "未配置 ADB 地址/端口，插件当前为未启用状态；"
    "请在插件配置 adb.host 与 adb.port 中填写后重新加载插件"
)
DEFAULT_AUTO_CRON = "0 */2 * * *"
DEFAULT_APP = "douyin"
# 信息流浏览默认 App 留空 = 随机；定时自主浏览的默认 App 仍为 douyin。
DEFAULT_BROWSE_APP = ""
DEFAULT_ITEMS = 5
DEFAULT_MAX_ITEMS = 20
DEFAULT_SETTLE_SECONDS = 2.5
DEFAULT_DIARY_LIMIT = 500
DEFAULT_DIARY_KEEP_ROUNDS = 3
DEFAULT_RANDOM_INTERVAL_MIN = 45
DEFAULT_RANDOM_INTERVAL_MAX = 120
DEFAULT_RANDOM_PROBABILITY = 0.5
DEFAULT_CONTINUOUS_ENABLED = False
DEFAULT_CONTINUOUS_GAP_SECONDS = 60
DEFAULT_CONTINUOUS_TARGET_TOKENS = 0
DEFAULT_CONTINUOUS_APPS = ""
DEFAULT_QUOTA_ENABLED = False
DEFAULT_QUOTA_WINDOW_HOURS = 24
# Token 额度与冷却默认 0 表示不限制；每日次数默认 -1 表示不限制。
DEFAULT_QUOTA_MAX_TOKENS = 0
DEFAULT_QUOTA_PLAY_TASK_TOKENS = 0
DEFAULT_QUOTA_PER_STEP_TOKENS = 0
DEFAULT_ALLOW_COMMENTS = False
DEFAULT_ALLOW_POSTS = False
DEFAULT_MAX_COMMENTS_PER_DAY = -1
DEFAULT_MAX_POSTS_PER_DAY = -1
DEFAULT_INTERACTION_COOLDOWN_SECONDS = 0
DEFAULT_ALLOW_PASSIVE_REPLIES = False
DEFAULT_MAX_PASSIVE_REPLIES_PER_DAY = -1
DEFAULT_PASSIVE_REPLY_COOLDOWN_SECONDS = 0
DEFAULT_LIVE_SCREEN_ENABLED = False
DEFAULT_LIVE_SCREEN_INTERVAL_MS = 1200
DEFAULT_LIVE_SCREEN_FRAMES = 3
DEFAULT_LIVE_SCREEN_IDLE_STOP_SECONDS = 90
MEMORY_BACKENDS = ("local", "companion", "livingmemory", "companion+livingmemory")
DEFAULT_MEMORY_BACKEND = "local"
# 本地存储开关：关闭后学习档案/浏览日志/聊天记忆不再写入插件本地，
# 依赖本地档案的聊天语义找回同时停用（配置卡片随条件隐藏）。
DEFAULT_MEMORY_LOCAL_ENABLED = True
DEFAULT_MEMORY_COMPANION_USER_ID = ""
# 聊天语义找回（recall 卡片）：本地向量索引 + Embedding Provider。
DEFAULT_RECALL_ENABLED = False
DEFAULT_RECALL_EMBEDDING_PROVIDER_ID = ""
DEFAULT_RECALL_TOP_K = 5
DEFAULT_RECALL_MIN_SCORE = 0.25
DEFAULT_RECALL_MAX_CHARS = 1200
DEFAULT_RECALL_INDEX_MAX_DOCS = 2000
DEFAULT_RECALL_DOC_CHARS = 300
DEFAULT_RECALL_TIMEOUT_SECONDS = 3
DEFAULT_RECALL_REFRESH_MINUTES = 10
DEFAULT_OCR_PROVIDER_ID = ""
DEFAULT_OCR_TIMEOUT_SECONDS = 30
DEFAULT_WAIT_DEFAULT_SECONDS = 2.0
DEFAULT_WAIT_MAX_SECONDS = 60
DEFAULT_VERIFY_TIMEOUT_SECONDS = 30
CONTROL_BACKENDS = ("auto", "adb", "scrcpy")
SCREEN_SOURCES = ("auto", "adb", "scrcpy")
DEFAULT_CONTROL_BACKEND = "auto"
DEFAULT_SCRCPY_MAX_SIZE = 720
DEFAULT_SCRCPY_SERVER_JAR = ""
DEFAULT_SCRCPY_IDLE_STOP_SECONDS = 120
DEFAULT_MINITOUCH_BINARY_PATH = ""
DEFAULT_SCREEN_SOURCE = "adb"
DEFAULT_SCREENSHOT_MODE = "auto"
DEFAULT_SCREENSHOT_SLOW_MS = 800
DEFAULT_SCREENSHOT_AUTO_CLEAN = True
DEFAULT_SCREENSHOT_RETENTION_MINUTES = 3
SCREENSHOT_MODES = ("auto", "png", "raw")

_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "enable", "enabled", "开启", "是"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "disable", "disabled", "关闭", "否"})

WarnFn = Callable[[str], None]


def _warn_default(warn: WarnFn | None) -> WarnFn:
    return warn if warn is not None else (lambda message: logger.warning(message))


def _raw(source: Mapping[str, Any] | None, key: str) -> Any:
    if not isinstance(source, Mapping):
        return None
    return source.get(key)


def _pick(
    card: Mapping[str, Any] | None,
    flat: Mapping[str, Any],
    key: str,
) -> Any:
    """优先取分组卡片内的值，没有卡片时回退到顶层扁平键。"""
    if isinstance(card, Mapping) and key in card:
        return card.get(key)
    return flat.get(key)


def _as_bool(raw: Any, default: bool, label: str, warn: WarnFn) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    warn(f"配置项 {label} 的值 {raw!r} 不是合法布尔值，回退默认 {default}")
    return default


def _as_int(
    raw: Any,
    default: int,
    label: str,
    warn: WarnFn,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        warn(f"配置项 {label} 的值 {raw!r} 不是整数，回退默认 {default}")
        return default
    if minimum is not None and value < minimum:
        warn(f"配置项 {label} 的值 {value} 小于下限 {minimum}，已收敛")
        value = minimum
    if maximum is not None and value > maximum:
        warn(f"配置项 {label} 的值 {value} 大于上限 {maximum}，已收敛")
        value = maximum
    return value


def _as_float(
    raw: Any,
    default: float,
    label: str,
    warn: WarnFn,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(str(raw).strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        warn(f"配置项 {label} 的值 {raw!r} 不是数字，回退默认 {default}")
        return default
    if not math.isfinite(value):
        warn(f"配置项 {label} 的值 {raw!r} 不是有限数字，回退默认 {default}")
        return default
    if minimum is not None and value < minimum:
        warn(f"配置项 {label} 的值 {value} 小于下限 {minimum}，已收敛")
        value = minimum
    if maximum is not None and value > maximum:
        warn(f"配置项 {label} 的值 {value} 大于上限 {maximum}，已收敛")
        value = maximum
    return value


def _as_str(raw: Any, default: str, label: str, warn: WarnFn) -> str:
    if raw is None:
        return default
    if isinstance(raw, bool):
        warn(f"配置项 {label} 的值 {raw!r} 不是字符串，回退默认 {default}")
        return default
    text = str(raw).strip()
    return text if text else default


def _as_choice(
    raw: Any,
    default: str,
    label: str,
    warn: WarnFn,
    choices: tuple[str, ...],
) -> str:
    """解析枚举字符串（大小写不敏感）；非法值回退默认并记录 warning。"""
    if raw is None or raw == "":
        return default
    text = str(raw).strip().lower()
    if text in choices:
        return text
    warn(f"配置项 {label} 的值 {raw!r} 不在 {choices} 中，回退默认 {default}")
    return default


def _as_str_list(raw: Any, label: str, warn: WarnFn) -> tuple[str, ...]:
    """归一化字符串列表：支持 list/tuple 与逗号/空白分隔的字符串。"""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, (list, tuple, set)):
        items = [str(item).strip() for item in raw]
    elif isinstance(raw, str):
        normalized = raw.replace("\n", ",").replace("，", ",").replace("\t", ",")
        items = [part.strip() for part in normalized.replace(" ", ",").split(",")]
    else:
        warn(f"配置项 {label} 的值 {raw!r} 不是字符串列表，已忽略")
        return ()
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return tuple(result)


def _as_port(raw: Any, label: str, warn: WarnFn) -> int:
    """解析 ADB 端口；空值与 0 表示未配置（返回 0），显式非法值同样视为未配置。"""
    if raw is None or raw == "" or raw == 0 or raw == "0":
        return 0
    try:
        value = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        warn(f"配置项 {label} 的值 {raw!r} 不是整数，已视为未配置")
        return 0
    if value < 1 or value > 65535:
        warn(f"配置项 {label} 的值 {value} 不在 1-65535 范围内，已视为未配置")
        return 0
    return value


def _as_cron(raw: Any, default: str, label: str, warn: WarnFn) -> str:
    text = _as_str(raw, default, label, warn)
    fields = text.split()
    if len(fields) != 5:
        warn(f"配置项 {label} 的值 {text!r} 不是 5 段 cron 表达式，回退默认 {default}")
        return default
    return " ".join(fields)


@dataclass(frozen=True)
class AdbSettings:
    """ADB 连接参数；主机/端口留空（0）表示未配置，插件处于假开启状态。"""

    mode: str = "direct"
    app_port: int = 6190
    app_screen_source: str = "scrcpy"
    app_video_standby: bool = True
    app_frame_max_size: int = 0
    app_frame_fps: int = 10
    app_frame_quality: int = 85
    app_frame_max_age_ms: int = 250
    host: str = DEFAULT_ADB_HOST
    port: int = DEFAULT_ADB_PORT
    path: str = ""
    serial: str = ""
    connect_timeout: float = 20.0
    command_timeout: float = 30.0

    @property
    def configured(self) -> bool:
        """是否已填写可用的 ADB 目标（显式 serial，或主机 + 端口）。"""
        if self.mode == "app":
            return 1 <= self.app_port <= 65535
        if str(self.serial or "").strip():
            return True
        return bool(str(self.host or "").strip()) and 1 <= int(self.port) <= 65535

    @property
    def target(self) -> str:
        """adb 连接目标：显式 serial 优先，否则 host:port；未配置时为空串。"""
        if self.mode == "app":
            return "Relay App"
        if self.serial:
            return self.serial
        if not self.configured:
            return ""
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class SecuritySettings:
    """权限与互动边界。"""

    require_admin: bool = True
    allowed_user_ids: tuple[str, ...] = ()
    allow_shell_tool: bool = False
    allow_interactions: bool = False
    max_likes_per_session: int = 5
    auto_clean_after_browse: bool = True
    auto_clean_after_task: bool = True
    background_keep_packages: tuple[str, ...] = DEFAULT_KEEP_PACKAGES
    owner_ids: tuple[str, ...] = ()
    allow_comments: bool = DEFAULT_ALLOW_COMMENTS
    allow_posts: bool = DEFAULT_ALLOW_POSTS
    max_comments_per_day: int = DEFAULT_MAX_COMMENTS_PER_DAY
    max_posts_per_day: int = DEFAULT_MAX_POSTS_PER_DAY
    interaction_cooldown_seconds: int = DEFAULT_INTERACTION_COOLDOWN_SECONDS
    allow_passive_replies: bool = DEFAULT_ALLOW_PASSIVE_REPLIES
    max_passive_replies_per_day: int = DEFAULT_MAX_PASSIVE_REPLIES_PER_DAY
    passive_reply_cooldown_seconds: int = DEFAULT_PASSIVE_REPLY_COOLDOWN_SECONDS

    def __post_init__(self):
        if set(self.background_keep_packages) in (set(DEFAULT_KEEP_PACKAGES[:2]), set(DEFAULT_KEEP_PACKAGES[:-1])):
            object.__setattr__(self, "background_keep_packages", DEFAULT_KEEP_PACKAGES)


@dataclass(frozen=True)
class PerceptionSettings:
    """屏幕感知参数。"""

    max_ui_nodes: int = 80
    dump_timeout: float = 20.0
    wait_default_seconds: float = DEFAULT_WAIT_DEFAULT_SECONDS
    wait_max_seconds: int = DEFAULT_WAIT_MAX_SECONDS
    include_screenshot_by_default: bool = False
    max_text_len: int = 60
    screenshot_mode: str = DEFAULT_SCREENSHOT_MODE
    screenshot_slow_ms: int = DEFAULT_SCREENSHOT_SLOW_MS
    screenshot_auto_clean: bool = DEFAULT_SCREENSHOT_AUTO_CLEAN
    screenshot_retention_minutes: int = DEFAULT_SCREENSHOT_RETENTION_MINUTES
    live_screen_enabled: bool = DEFAULT_LIVE_SCREEN_ENABLED
    live_screen_interval_ms: int = DEFAULT_LIVE_SCREEN_INTERVAL_MS
    live_screen_frames: int = DEFAULT_LIVE_SCREEN_FRAMES
    live_screen_idle_stop_seconds: int = DEFAULT_LIVE_SCREEN_IDLE_STOP_SECONDS
    ocr_provider_id: str = DEFAULT_OCR_PROVIDER_ID
    screen_source: str = DEFAULT_SCREEN_SOURCE
    ocr_timeout_seconds: int = DEFAULT_OCR_TIMEOUT_SECONDS
    verify_timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS


@dataclass(frozen=True)
class MediaSettings:
    """流媒体内容识别（设备播放音频采集 + 关键帧描述）参数。"""

    enabled: bool = True
    full_video_enabled: bool = False
    full_video_max_mb: float = 6.0
    full_video_max_seconds: int = 300
    audio_enabled: bool = True
    audio_source: str = DEFAULT_MEDIA_AUDIO_SOURCE
    seconds: int = int(DEFAULT_MEDIA_SECONDS)
    max_seconds: int = int(DEFAULT_MEDIA_MAX_SECONDS)
    frame_interval_seconds: float = DEFAULT_MEDIA_FRAME_INTERVAL_SECONDS
    max_frames: int = DEFAULT_MEDIA_MAX_FRAMES
    asr_provider_id: str = ""
    vision_provider_id: str = ""
    asr_timeout_seconds: int = 60
    vision_timeout_seconds: int = 60
    keep_audio: bool = False
    preview_seconds: int = 8


@dataclass(frozen=True)
class BrowseSettings:
    """信息流浏览参数。"""

    default_app: str = DEFAULT_BROWSE_APP
    default_items: int = DEFAULT_ITEMS
    max_items: int = DEFAULT_MAX_ITEMS
    swipe_duration_ms: int = 800
    settle_seconds: float = DEFAULT_SETTLE_SECONDS
    collect_screenshots: bool = True
    send_screenshots_to_user: bool = False
    auto_remember: bool = True
    diary_max_entries: int = DEFAULT_DIARY_LIMIT
    """浏览日志最多保留条数；达到上限自动清理旧日志。"""
    diary_protect_rounds: int = DEFAULT_DIARY_KEEP_ROUNDS
    """清理时保护最近 N 轮任务的日志（0=不保护）。"""


@dataclass(frozen=True)
class ImageSettings:
    """图片保存参数（独立配置卡片，不随信息流浏览开关显隐）。"""

    auto_save_images: bool = False
    """保存图片开关；关闭时 phone_save_image 不可用。"""
    image_storage_location: str = "server"
    """主要存储位置：server=AstrBot 服务端，phone=手机相册。"""
    save_images_both: bool = False
    """同时保存服务端与手机；开启时隐藏主要存储位置选择。"""


@dataclass(frozen=True)
class AutoSettings:
    """自主浏览（cron 主动任务）参数。"""

    enabled: bool = False
    cron: str = DEFAULT_AUTO_CRON
    apps: str = DEFAULT_APP
    session: str = ""
    max_items: int = 6
    prompt: str = ""
    """自主浏览任务的显式分享目标；自刷/随机/持续刷的分享目标由 AI 根据最近聊天自行判断。"""
    share_quiet_hours_enabled: bool = True
    """白天时段外不主动分享（免打扰）。"""
    share_start_hour: int = DEFAULT_SHARE_START_HOUR
    share_end_hour: int = DEFAULT_SHARE_END_HOUR
    share_timezone: str = DEFAULT_SHARE_TIMEZONE
    share_user_recent_hours: int = DEFAULT_USER_RECENT_HOURS
    share_cooldown_minutes: int = DEFAULT_SHARE_COOLDOWN_MINUTES
    """同一目标会话两次主动分享的最小间隔（分钟，0=不限制）。"""
    play_timezone: str = "Asia/Shanghai"
    random_windows: tuple[str, ...] = ()
    continuous_windows: tuple[str, ...] = ()
    random_enabled: bool = True
    random_interval_min: int = DEFAULT_RANDOM_INTERVAL_MIN
    random_interval_max: int = DEFAULT_RANDOM_INTERVAL_MAX
    random_probability: float = DEFAULT_RANDOM_PROBABILITY
    share_only_interesting: bool = True
    continuous_enabled: bool = DEFAULT_CONTINUOUS_ENABLED
    continuous_gap_seconds: int = DEFAULT_CONTINUOUS_GAP_SECONDS
    continuous_target_tokens: int = DEFAULT_CONTINUOUS_TARGET_TOKENS
    continuous_apps: str = DEFAULT_CONTINUOUS_APPS
    user_priority: bool = True
    """用户手机任务优先于 AI 自刷/随机/后台任务（抢占并自动恢复）。"""

    def app_list(self) -> tuple[str, ...]:
        """把 ``apps`` 字符串解析成 App 名列表。"""
        parts = self.apps.replace("，", ",").replace(" ", ",").split(",")
        result: list[str] = []
        for part in parts:
            name = part.strip()
            if name and name not in result:
                result.append(name)
        return tuple(result) or (DEFAULT_APP,)


@dataclass(frozen=True)
class ScrcpySettings:
    """scrcpy 控制/镜像参数（v5.0.1）。"""

    max_size: int = DEFAULT_SCRCPY_MAX_SIZE
    server_jar: str = DEFAULT_SCRCPY_SERVER_JAR
    keep_alive: bool = True
    idle_stop_seconds: int = DEFAULT_SCRCPY_IDLE_STOP_SECONDS


@dataclass(frozen=True)
class MinitouchSettings:
    """minitouch 可选 touch 后端参数（用户自备 binary）。"""

    binary_path: str = DEFAULT_MINITOUCH_BINARY_PATH


@dataclass(frozen=True)
class ControlSettings:
    """控制/感知后端选择。"""

    backend: str = DEFAULT_CONTROL_BACKEND
    scrcpy: ScrcpySettings = field(default_factory=ScrcpySettings)
    minitouch: MinitouchSettings = field(default_factory=MinitouchSettings)


@dataclass(frozen=True)
class MemorySettings:
    """记忆/学习内容的存储去向（本地插件 / MemoryCompanion / LivingMemory）。

    ``local_enabled`` 控制是否写入插件本地档案；``companion_enabled`` 与
    ``livingmemory_enabled`` 独立控制是否额外同步到对应记忆插件，便于按条件
    显示相关配置项。关闭本地存储时不再写学习档案/浏览日志/聊天记忆，依赖本地
    档案的聊天语义找回同步停用。
    """

    local_enabled: bool = DEFAULT_MEMORY_LOCAL_ENABLED
    companion_enabled: bool = False
    livingmemory_enabled: bool = False
    companion_user_id: str = DEFAULT_MEMORY_COMPANION_USER_ID
    bot_action_enabled: bool = False

    @property
    def backend(self) -> str:
        """兼容旧存储去向字符串（local / companion / livingmemory / 组合）。"""
        if self.companion_enabled and self.livingmemory_enabled:
            return "companion+livingmemory"
        if self.companion_enabled:
            return "companion"
        if self.livingmemory_enabled:
            return "livingmemory"
        return "local"


@dataclass(frozen=True)
class RecallSettings:
    """聊天语义找回：把本地学习档案/浏览日志建成向量索引，普通聊天按语义注入。

    App 使用经验使用独立索引与独立向量模型配置（见 ``PromptSettings``），
    不再进入聊天找回索引。
    """

    enabled: bool = DEFAULT_RECALL_ENABLED
    embedding_provider_id: str = DEFAULT_RECALL_EMBEDDING_PROVIDER_ID
    """留空时自动选择 AstrBot 已配置的 Embedding Provider。"""
    top_k: int = DEFAULT_RECALL_TOP_K
    min_score: float = DEFAULT_RECALL_MIN_SCORE
    max_chars: int = DEFAULT_RECALL_MAX_CHARS
    index_max_docs: int = DEFAULT_RECALL_INDEX_MAX_DOCS
    doc_chars: int = DEFAULT_RECALL_DOC_CHARS
    timeout_seconds: int = DEFAULT_RECALL_TIMEOUT_SECONDS
    refresh_minutes: int = DEFAULT_RECALL_REFRESH_MINUTES
    include_diary: bool = True
    include_learning: bool = True


@dataclass(frozen=True)
class VerificationSettings:
    """验证/做题页面的自动尝试上限与人工协助策略。"""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    total_timeout_seconds: int = DEFAULT_TOTAL_TIMEOUT_SECONDS
    attempt_timeout_seconds: int = DEFAULT_ATTEMPT_TIMEOUT_SECONDS
    manual_wait_timeout_seconds: int = DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS
    notify_target: str = ""


@dataclass(frozen=True)
class TrafficSettings:
    """手机流量抓包（ADB + mitmproxy 解密）参数。"""

    enabled: bool = True
    provider: str = "mitmproxy"
    port: int = DEFAULT_TRAFFIC_PORT
    body_chars: int = DEFAULT_TRAFFIC_BODY_CHARS
    keep_seconds: int = DEFAULT_TRAFFIC_KEEP_SECONDS
    mitmdump_path: str = ""
    transport: str = "adb"
    ca_mode: str = "system"


@dataclass(frozen=True)
class ModelSettings:
    """自主任务与媒体识别使用的模型供应商。"""

    work_as_subagent: bool = False
    provider_id: str = ""
    asr_provider_id: str = ""
    video_provider_id: str = ""
    video_audio_separate: bool = False


@dataclass(frozen=True)
class QuotaSettings:
    """手机 token 额度（滚动窗口）。"""

    enabled: bool = DEFAULT_QUOTA_ENABLED
    window_hours: int = DEFAULT_QUOTA_WINDOW_HOURS
    max_tokens: int = DEFAULT_QUOTA_MAX_TOKENS
    play_task_tokens: int = DEFAULT_QUOTA_PLAY_TASK_TOKENS
    per_step_tokens: int = DEFAULT_QUOTA_PER_STEP_TOKENS


@dataclass(frozen=True)
class PromptSettings:
    """提示词注入参数。"""

    inject_guidance: bool = True
    diary_limit: int = 5
    app_memory_enabled: bool = True
    app_memory_limit: int = 5
    app_memory_embedding_provider_id: str = ""
    """App 经验召回使用的向量模型 Provider ID；留空用默认 Embedding Provider。"""


@dataclass(frozen=True)
class CommandSettings:
    """命令消息输出格式。"""

    status_as_image: bool = True
    help_as_image: bool = True


@dataclass(frozen=True)
class DebugSettings:
    """调试参数。"""

    log_actions: bool = True


# 内容新鲜感/厌倦感配置（novelty 卡片）：threshold=0 表示不启用该机制。
def novelty_settings_from_raw(
    novelty_card: Mapping[str, Any] | None,
    source: Mapping[str, Any],
    warn: WarnFn,
) -> NoveltySettings:
    """把 novelty 卡片解析成设置快照（数值收敛，非法值回退默认）。"""
    return NoveltySettings(
        threshold=_as_float(
            _pick(novelty_card, source, "threshold"),
            DEFAULT_NOVELTY_THRESHOLD,
            "novelty.threshold",
            warn,
            minimum=0.0,
            maximum=100.0,
        ),
        half_life_hours=_as_float(
            _pick(novelty_card, source, "half_life_hours"),
            DEFAULT_NOVELTY_HALF_LIFE_HOURS,
            "novelty.half_life_hours",
            warn,
            minimum=1.0,
            maximum=720.0,
        ),
        gain=_as_float(
            _pick(novelty_card, source, "gain"),
            DEFAULT_NOVELTY_GAIN,
            "novelty.gain",
            warn,
            minimum=1.0,
            maximum=100.0,
        ),
        similarity=_as_float(
            _pick(novelty_card, source, "similarity"),
            DEFAULT_NOVELTY_SIMILARITY,
            "novelty.similarity",
            warn,
            minimum=0.10,
            maximum=0.90,
        ),
        max_entries=_as_int(
            _pick(novelty_card, source, "max_entries"),
            DEFAULT_NOVELTY_MAX_ENTRIES,
            "novelty.max_entries",
            warn,
            minimum=20,
            maximum=2000,
        ),
        inject_limit=_as_int(
            _pick(novelty_card, source, "inject_limit"),
            DEFAULT_NOVELTY_INJECT_LIMIT,
            "novelty.inject_limit",
            warn,
            minimum=1,
            maximum=10,
        ),
    )


def _memory_backend_legacy(
    memory_card: Mapping[str, Any] | None,
    source: Mapping[str, Any],
    warn: WarnFn,
) -> str:
    """读取旧版 memory.backend（兼容 both/all），非法值回退 local。"""
    raw = _pick(memory_card, source, "backend")
    text = str(raw or "").strip().lower()
    if text in {"both", "all"}:
        return "companion+livingmemory"
    if text in MEMORY_BACKENDS:
        return text
    if text and text not in {"local", ""}:
        warn(f"配置项 memory.backend 的值 {raw!r} 不在 {MEMORY_BACKENDS} 中，已按 local 处理")
    return "local"


def migrate_memory_config(raw: Any) -> bool:
    """把旧版 ``memory.backend`` 迁移为 ``companion_enabled``/``livingmemory_enabled``。

    仅在旧配置存在非 local 的 backend、且没有新布尔字段时迁移；迁移后把
    backend 置为 local 作为已迁移标记。返回是否修改了配置（供插件保存）。
    """
    if not isinstance(raw, dict):
        return False
    memory = raw.get("memory")
    if not isinstance(memory, dict):
        return False
    backend = str(memory.get("backend") or "").strip().lower()
    if backend in {"both", "all"}:
        backend = "companion+livingmemory"
    if backend not in {"companion", "livingmemory", "companion+livingmemory"}:
        return False
    # 新开关已有任一为 true：用户已在使用新配置，只需把 backend 归零。
    if bool(memory.get("companion_enabled")) or bool(
        memory.get("livingmemory_enabled")
    ):
        if str(memory.get("backend") or "").strip().lower() == "local":
            return False
        memory["backend"] = "local"
        return True
    memory["companion_enabled"] = backend in {"companion", "companion+livingmemory"}
    memory["livingmemory_enabled"] = backend in {
        "livingmemory",
        "companion+livingmemory",
    }
    memory["backend"] = "local"
    return True


def migrate_image_config(raw: Any) -> bool:
    """把旧版 ``browse.auto_save_images`` 等图片保存配置迁移到独立 ``image`` 卡片。

    仅在新字段仍为默认值、旧字段存在非默认值时迁移；迁移后删除旧字段，避免配置页
    残留未声明键。返回是否修改了配置（供插件保存）。
    """
    if not isinstance(raw, dict):
        return False
    browse = raw.get("browse")
    if not isinstance(browse, dict):
        return False
    legacy_keys = {
        "auto_save_images": False,
        "image_storage_location": "server",
        "save_images_both": False,
    }
    present = [key for key in legacy_keys if key in browse]
    if not present:
        return False
    image = raw.get("image")
    if not isinstance(image, dict):
        image = {}
    changed = False
    for key in present:
        legacy_value = browse.get(key)
        if legacy_value in (None, legacy_keys[key]):
            continue
        if image.get(key) not in (None, legacy_keys[key]):
            continue
        image[key] = legacy_value
        changed = True
    if changed:
        raw["image"] = image
    # 无论旧值是否为默认，都清理已下线的 browse.* 旧键，避免配置页残留未声明字段。
    for key in present:
        browse.pop(key, None)
    return True


@dataclass(frozen=True)
class PhoneConfig:
    """插件完整配置快照。"""

    adb: AdbSettings = field(default_factory=AdbSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    perception: PerceptionSettings = field(default_factory=PerceptionSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    browse: BrowseSettings = field(default_factory=BrowseSettings)
    image: ImageSettings = field(default_factory=ImageSettings)
    auto: AutoSettings = field(default_factory=AutoSettings)
    flexibility: FlexibilitySettings = field(default_factory=FlexibilitySettings)
    model: ModelSettings = field(default_factory=ModelSettings)
    quota: QuotaSettings = field(default_factory=QuotaSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    traffic: TrafficSettings = field(default_factory=TrafficSettings)
    verification: VerificationSettings = field(
        default_factory=VerificationSettings
    )
    control: ControlSettings = field(default_factory=ControlSettings)
    wireguard: WireGuardSettings = field(default_factory=WireGuardSettings)
    mesh: MeshSettings = field(default_factory=MeshSettings)
    prompt: PromptSettings = field(default_factory=PromptSettings)
    novelty: NoveltySettings = field(default_factory=NoveltySettings)
    recall: RecallSettings = field(default_factory=RecallSettings)
    command: CommandSettings = field(default_factory=CommandSettings)
    debug: DebugSettings = field(default_factory=DebugSettings)
    report: ReportSettings = field(default_factory=ReportSettings)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        warn: WarnFn | None = None,
    ) -> "PhoneConfig":
        """把 AstrBot 下发的配置 dict 解析成不可变快照。

        Args:
            raw: AstrBot 配置（dict 或 AstrBotConfig），可为 None。
            warn: warning 输出回调，默认写标准库 logger。

        Returns:
            解析后的 :class:`PhoneConfig`；非法值回退默认。
        """
        source: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        warn_fn = _warn_default(warn)

        def card(name: str) -> Mapping[str, Any] | None:
            node = source.get(name)
            return node if isinstance(node, Mapping) else None

        def model_value(key: str, fallback: Any) -> Any:
            models = card("model") or {}
            return models[key] if key in models else fallback

        adb_card = card("adb")
        adb = AdbSettings(
            mode="app" if (adb_card or {}).get("mode") in ("app", "使用 App 控制") else "direct",
            app_port=_as_int((adb_card or {}).get("app_port"), 6190, "adb.app_port", warn_fn, minimum=1, maximum=65535),
            app_screen_source="screencap" if (adb_card or {}).get("app_screen_source") in ("screencap", "按需截图") else "scrcpy",
            app_video_standby=_as_bool((adb_card or {}).get("app_video_standby"), True, "adb.app_video_standby", warn_fn),
            app_frame_max_size=_as_int((adb_card or {}).get("app_frame_max_size"), 0, "adb.app_frame_max_size", warn_fn, minimum=0, maximum=4096),
            app_frame_fps=_as_int((adb_card or {}).get("app_frame_fps"), 10, "adb.app_frame_fps", warn_fn, minimum=1, maximum=30),
            app_frame_quality=_as_int((adb_card or {}).get("app_frame_quality"), 85, "adb.app_frame_quality", warn_fn, minimum=40, maximum=95),
            app_frame_max_age_ms=_as_int((adb_card or {}).get("app_frame_max_age_ms"), 250, "adb.app_frame_max_age_ms", warn_fn, minimum=100, maximum=10000),
            host=_as_str(
                _pick(adb_card, source, "host"),
                DEFAULT_ADB_HOST,
                "adb.host",
                warn_fn,
            ),
            port=_as_port(_pick(adb_card, source, "port"), "adb.port", warn_fn),
            path=_as_str(_pick(adb_card, source, "path"), "", "adb.path", warn_fn),
            serial=_as_str(
                _pick(adb_card, source, "serial"), "", "adb.serial", warn_fn
            ),
            connect_timeout=_as_float(
                _pick(adb_card, source, "connect_timeout"),
                20.0,
                "adb.connect_timeout",
                warn_fn,
                minimum=1.0,
                maximum=120.0,
            ),
            command_timeout=_as_float(
                _pick(adb_card, source, "command_timeout"),
                30.0,
                "adb.command_timeout",
                warn_fn,
                minimum=1.0,
                maximum=300.0,
            ),
        )

        flex_card = card("flexibility")
        flex_values = {}
        for key, default in FlexibilitySettings().__dict__.items():
            value = _pick(flex_card, source, key)
            if key == "enabled" and (not flex_card or "enabled" not in flex_card):
                value = (card("auto") or {}).get("break_rule", value)
            if key.startswith("max_"):
                flex_values[key] = _as_int(value, default, "flexibility." + key, warn_fn, minimum=-1)
            else:
                flex_values[key] = _as_bool(value, default, "flexibility." + key, warn_fn)
        flexibility = FlexibilitySettings(**flex_values)

        security_card = card("security")
        security = SecuritySettings(
            require_admin=_as_bool(
                _pick(security_card, source, "require_admin"),
                True,
                "security.require_admin",
                warn_fn,
            ),
            allowed_user_ids=_as_str_list(
                _pick(security_card, source, "allowed_user_ids"),
                "security.allowed_user_ids",
                warn_fn,
            ),
            allow_shell_tool=_as_bool(
                _pick(security_card, source, "allow_shell_tool"),
                False,
                "security.allow_shell_tool",
                warn_fn,
            ),
            allow_interactions=_as_bool(
                _pick(security_card, source, "allow_interactions"),
                False,
                "security.allow_interactions",
                warn_fn,
            ),
            max_likes_per_session=_as_int(
                _pick(security_card, source, "max_likes_per_session"),
                5,
                "security.max_likes_per_session",
                warn_fn,
                minimum=0,
                maximum=50,
            ),
            auto_clean_after_browse=_as_bool(
                _pick(security_card, source, "auto_clean_after_browse"),
                True,
                "security.auto_clean_after_browse",
                warn_fn,
            ),
            auto_clean_after_task=_as_bool(
                _pick(security_card, source, "auto_clean_after_task"),
                True,
                "security.auto_clean_after_task",
                warn_fn,
            ),
            background_keep_packages=(
                DEFAULT_KEEP_PACKAGES
                if _pick(security_card, source, "background_keep_packages") is None
                else _as_str_list(
                    _pick(security_card, source, "background_keep_packages"),
                    "security.background_keep_packages", warn_fn,
                )
            ),
            owner_ids=_as_str_list(
                _pick(security_card, source, "owner_ids"),
                "security.owner_ids",
                warn_fn,
            ),
            allow_comments=_as_bool(
                _pick(security_card, source, "allow_comments"),
                DEFAULT_ALLOW_COMMENTS,
                "security.allow_comments",
                warn_fn,
            ),
            allow_posts=_as_bool(
                _pick(security_card, source, "allow_posts"),
                DEFAULT_ALLOW_POSTS,
                "security.allow_posts",
                warn_fn,
            ),
            max_comments_per_day=_as_int(
                _pick(security_card, source, "max_comments_per_day"),
                DEFAULT_MAX_COMMENTS_PER_DAY,
                "security.max_comments_per_day",
                warn_fn,
                minimum=-1,
            ),
            max_posts_per_day=_as_int(
                _pick(security_card, source, "max_posts_per_day"),
                DEFAULT_MAX_POSTS_PER_DAY,
                "security.max_posts_per_day",
                warn_fn,
                minimum=-1,
            ),
            interaction_cooldown_seconds=_as_int(
                _pick(security_card, source, "interaction_cooldown_seconds"),
                DEFAULT_INTERACTION_COOLDOWN_SECONDS,
                "security.interaction_cooldown_seconds",
                warn_fn,
                minimum=0,
            ),
            allow_passive_replies=_as_bool(
                _pick(security_card, source, "allow_passive_replies"),
                DEFAULT_ALLOW_PASSIVE_REPLIES,
                "security.allow_passive_replies",
                warn_fn,
            ),
            max_passive_replies_per_day=_as_int(
                _pick(security_card, source, "max_passive_replies_per_day"),
                DEFAULT_MAX_PASSIVE_REPLIES_PER_DAY,
                "security.max_passive_replies_per_day",
                warn_fn,
                minimum=-1,
            ),
            passive_reply_cooldown_seconds=_as_int(
                _pick(security_card, source, "passive_reply_cooldown_seconds"),
                DEFAULT_PASSIVE_REPLY_COOLDOWN_SECONDS,
                "security.passive_reply_cooldown_seconds",
                warn_fn,
                minimum=0,
            ),
        )

        perception_card = card("perception")
        perception = PerceptionSettings(
            max_ui_nodes=_as_int(
                _pick(perception_card, source, "max_ui_nodes"),
                PerceptionSettings.max_ui_nodes,
                "perception.max_ui_nodes",
                warn_fn,
                minimum=0,
                maximum=2000,
            ),
            dump_timeout=_as_float(
                _pick(perception_card, source, "dump_timeout"),
                20.0,
                "perception.dump_timeout",
                warn_fn,
                minimum=1.0,
                maximum=120.0,
            ),
            wait_default_seconds=_as_float(
                _pick(perception_card, source, "wait_default_seconds"),
                DEFAULT_WAIT_DEFAULT_SECONDS,
                "perception.wait_default_seconds",
                warn_fn,
                minimum=0.2,
                maximum=60.0,
            ),
            wait_max_seconds=_as_int(
                _pick(perception_card, source, "wait_max_seconds"),
                DEFAULT_WAIT_MAX_SECONDS,
                "perception.wait_max_seconds",
                warn_fn,
                minimum=1,
                maximum=600,
            ),
            include_screenshot_by_default=_as_bool(
                _pick(perception_card, source, "include_screenshot_by_default"),
                False,
                "perception.include_screenshot_by_default",
                warn_fn,
            ),
            max_text_len=_as_int(
                _pick(perception_card, source, "max_text_len"),
                PerceptionSettings.max_text_len,
                "perception.max_text_len",
                warn_fn,
                minimum=10,
                maximum=1000,
            ),
            screenshot_mode=_as_choice(
                _pick(perception_card, source, "screenshot_mode"),
                DEFAULT_SCREENSHOT_MODE,
                "perception.screenshot_mode",
                warn_fn,
                SCREENSHOT_MODES,
            ),
            screenshot_slow_ms=_as_int(
                _pick(perception_card, source, "screenshot_slow_ms"),
                DEFAULT_SCREENSHOT_SLOW_MS,
                "perception.screenshot_slow_ms",
                warn_fn,
                minimum=100,
                maximum=10000,
            ),
            screenshot_auto_clean=_as_bool(
                _pick(perception_card, source, "screenshot_auto_clean"),
                DEFAULT_SCREENSHOT_AUTO_CLEAN,
                "perception.screenshot_auto_clean",
                warn_fn,
            ),
            screenshot_retention_minutes=_as_int(
                _pick(perception_card, source, "screenshot_retention_minutes"),
                DEFAULT_SCREENSHOT_RETENTION_MINUTES,
                "perception.screenshot_retention_minutes",
                warn_fn,
                minimum=1,
            ),
            live_screen_enabled=_as_bool(
                _pick(perception_card, source, "live_screen_enabled"),
                DEFAULT_LIVE_SCREEN_ENABLED,
                "perception.live_screen_enabled",
                warn_fn,
            ),
            live_screen_interval_ms=_as_int(
                _pick(perception_card, source, "live_screen_interval_ms"),
                DEFAULT_LIVE_SCREEN_INTERVAL_MS,
                "perception.live_screen_interval_ms",
                warn_fn,
                minimum=300,
                maximum=5000,
            ),
            live_screen_frames=_as_int(
                _pick(perception_card, source, "live_screen_frames"),
                DEFAULT_LIVE_SCREEN_FRAMES,
                "perception.live_screen_frames",
                warn_fn,
                minimum=1,
                maximum=10,
            ),
            live_screen_idle_stop_seconds=_as_int(
                _pick(perception_card, source, "live_screen_idle_stop_seconds"),
                DEFAULT_LIVE_SCREEN_IDLE_STOP_SECONDS,
                "perception.live_screen_idle_stop_seconds",
                warn_fn,
                minimum=30,
                maximum=600,
            ),
            ocr_provider_id=_as_str(
                model_value("vision_provider_id", _pick(perception_card, source, "ocr_provider_id")),
                DEFAULT_OCR_PROVIDER_ID,
                "perception.ocr_provider_id",
                warn_fn,
            ),
            ocr_timeout_seconds=_as_int(
                _pick(perception_card, source, "ocr_timeout_seconds"),
                DEFAULT_OCR_TIMEOUT_SECONDS,
                "perception.ocr_timeout_seconds",
                warn_fn,
                minimum=5,
                maximum=120,
            ),
            verify_timeout_seconds=_as_int(
                _pick(perception_card, source, "verify_timeout_seconds"),
                DEFAULT_VERIFY_TIMEOUT_SECONDS,
                "perception.verify_timeout_seconds",
                warn_fn,
                minimum=5,
                maximum=120,
            ),
            screen_source=_as_choice(
                _pick(perception_card, source, "screen_source"),
                DEFAULT_SCREEN_SOURCE,
                "perception.screen_source",
                warn_fn,
                SCREEN_SOURCES,
            ),
        )

        media_card = card("media")
        media_max_seconds = _as_int(
            _pick(media_card, source, "max_seconds"),
            int(DEFAULT_MEDIA_MAX_SECONDS),
            "media.max_seconds",
            warn_fn,
            minimum=5,
            maximum=int(MEDIA_MIN_SECONDS * 100),
        )
        media_seconds = _as_int(
            _pick(media_card, source, "seconds"),
            int(DEFAULT_MEDIA_SECONDS),
            "media.seconds",
            warn_fn,
            minimum=int(MEDIA_MIN_SECONDS),
            maximum=media_max_seconds,
        )
        media = MediaSettings(
            full_video_enabled=_as_bool(_pick(media_card, source, "full_video_enabled"), False, "media.full_video_enabled", warn_fn),
            full_video_max_mb=_as_float(_pick(media_card, source, "full_video_max_mb"), 6.0, "media.full_video_max_mb", warn_fn, minimum=0.25, maximum=6.0),
            full_video_max_seconds=_as_int(_pick(media_card, source, "full_video_max_seconds"), 300, "media.full_video_max_seconds", warn_fn, minimum=3, maximum=600),
            enabled=_as_bool(
                _pick(media_card, source, "enabled"),
                True,
                "media.enabled",
                warn_fn,
            ),
            audio_enabled=_as_bool(
                _pick(media_card, source, "audio_enabled"),
                True,
                "media.audio_enabled",
                warn_fn,
            ),
            audio_source=_as_choice(
                _pick(media_card, source, "audio_source"),
                DEFAULT_MEDIA_AUDIO_SOURCE,
                "media.audio_source",
                warn_fn,
                MEDIA_AUDIO_SOURCES,
            ),
            preview_seconds=_as_int(_pick(media_card, source, "preview_seconds"), 8, "media.preview_seconds", warn_fn, minimum=3, maximum=media_max_seconds),
            seconds=media_seconds,
            max_seconds=media_max_seconds,
            frame_interval_seconds=_as_float(
                _pick(media_card, source, "frame_interval_seconds"),
                DEFAULT_MEDIA_FRAME_INTERVAL_SECONDS,
                "media.frame_interval_seconds",
                warn_fn,
                minimum=1.0,
                maximum=60.0,
            ),
            max_frames=_as_int(
                _pick(media_card, source, "max_frames"),
                DEFAULT_MEDIA_MAX_FRAMES,
                "media.max_frames",
                warn_fn,
                minimum=1,
                maximum=8,
            ),
            asr_provider_id=_as_str(
                model_value("asr_provider_id", _pick(media_card, source, "asr_provider_id")),
                "",
                "media.asr_provider_id",
                warn_fn,
            ),
            vision_provider_id=_as_str(
                model_value("vision_provider_id", _pick(media_card, source, "vision_provider_id")),
                "",
                "media.vision_provider_id",
                warn_fn,
            ),
            asr_timeout_seconds=_as_int(
                _pick(media_card, source, "asr_timeout_seconds"),
                60,
                "media.asr_timeout_seconds",
                warn_fn,
                minimum=5,
                maximum=300,
            ),
            vision_timeout_seconds=_as_int(
                _pick(media_card, source, "vision_timeout_seconds"),
                60,
                "media.vision_timeout_seconds",
                warn_fn,
                minimum=5,
                maximum=300,
            ),
            keep_audio=_as_bool(
                _pick(media_card, source, "keep_audio"),
                False,
                "media.keep_audio",
                warn_fn,
            ),
        )

        browse_card = card("browse")
        max_items = _as_int(
            _pick(browse_card, source, "max_items"),
            DEFAULT_MAX_ITEMS,
            "browse.max_items",
            warn_fn,
            minimum=1,
            maximum=50,
        )
        default_items = _as_int(
            _pick(browse_card, source, "default_items"),
            DEFAULT_ITEMS,
            "browse.default_items",
            warn_fn,
            minimum=1,
            maximum=max_items,
        )
        browse = BrowseSettings(
            default_app=_as_str(
                _pick(browse_card, source, "default_app"),
                DEFAULT_BROWSE_APP,
                "browse.default_app",
                warn_fn,
            ),
            default_items=min(default_items, max_items),
            max_items=max_items,
            swipe_duration_ms=_as_int(
                _pick(browse_card, source, "swipe_duration_ms"),
                800,
                "browse.swipe_duration_ms",
                warn_fn,
                minimum=100,
                maximum=5000,
            ),
            settle_seconds=_as_float(
                _pick(browse_card, source, "settle_seconds"),
                DEFAULT_SETTLE_SECONDS,
                "browse.settle_seconds",
                warn_fn,
                minimum=0.2,
                maximum=15.0,
            ),
            collect_screenshots=_as_bool(
                _pick(browse_card, source, "collect_screenshots"),
                True,
                "browse.collect_screenshots",
                warn_fn,
            ),
            send_screenshots_to_user=_as_bool(
                _pick(browse_card, source, "send_screenshots_to_user"),
                False,
                "browse.send_screenshots_to_user",
                warn_fn,
            ),
            auto_remember=_as_bool(
                _pick(browse_card, source, "auto_remember"),
                True,
                "browse.auto_remember",
                warn_fn,
            ),
            diary_max_entries=_as_int(
                _pick(browse_card, source, "diary_max_entries"),
                DEFAULT_DIARY_LIMIT,
                "browse.diary_max_entries",
                warn_fn,
                minimum=10,
                maximum=100000,
            ),
            diary_protect_rounds=_as_int(
                _pick(browse_card, source, "diary_protect_rounds"),
                DEFAULT_DIARY_KEEP_ROUNDS,
                "browse.diary_protect_rounds",
                warn_fn,
                minimum=0,
                maximum=100,
            ),
        )

        # 图片保存使用独立卡片；旧版 browse.* 值在未迁移时作为回退读取。
        image_card = card("image")
        legacy_image_card = browse_card if isinstance(browse_card, Mapping) else None
        image = ImageSettings(
            auto_save_images=_as_bool(
                _pick(image_card, legacy_image_card or {}, "auto_save_images"),
                False,
                "image.auto_save_images",
                warn_fn,
            ),
            image_storage_location=_as_choice(
                _pick(image_card, legacy_image_card or {}, "image_storage_location"),
                "server",
                "image.image_storage_location",
                warn_fn,
                ("server", "phone"),
            ),
            save_images_both=_as_bool(
                _pick(image_card, legacy_image_card or {}, "save_images_both"),
                False,
                "image.save_images_both",
                warn_fn,
            ),
        )

        auto_card = card("auto")
        random_min = _as_int(
            _pick(auto_card, source, "random_interval_min"),
            DEFAULT_RANDOM_INTERVAL_MIN,
            "auto.random_interval_min",
            warn_fn,
            minimum=1,
        )
        random_max = _as_int(
            _pick(auto_card, source, "random_interval_max"),
            DEFAULT_RANDOM_INTERVAL_MAX,
            "auto.random_interval_max",
            warn_fn,
            minimum=1,
        )
        if random_max < random_min:
            warn_fn(
                f"配置项 auto.random_interval_max（{random_max}）小于 "
                f"auto.random_interval_min（{random_min}），已按最小值收敛"
            )
            random_max = random_min
        auto_apps = _as_str(
            _pick(auto_card, source, "apps"),
            DEFAULT_APP,
            "auto.apps",
            warn_fn,
        ).replace("，", ",")
        continuous_apps = _as_str(
            _pick(auto_card, source, "continuous_apps"),
            DEFAULT_CONTINUOUS_APPS,
            "auto.continuous_apps",
            warn_fn,
        ).replace("，", ",")
        effective_auto = dict(auto_card or {})
        if "mode" in effective_auto:
            from .autonomy_config import selected_mode, FLAGS
            mode, active = selected_mode(effective_auto)
            effective_auto.update({key: active and mode == label for key, label in FLAGS.items()})
        auto_card = effective_auto
        auto = AutoSettings(
            play_timezone=validate_timezone((auto_card or {}).get("play_timezone", "Asia/Shanghai")),
            random_windows=parse_windows((auto_card or {}).get("random_windows"), "auto.random_windows"),
            continuous_windows=parse_windows((auto_card or {}).get("continuous_windows"), "auto.continuous_windows"),
            enabled=_as_bool(
                _pick(auto_card, source, "enabled"),
                False,
                "auto.enabled",
                warn_fn,
            ),
            cron=_as_cron(
                _pick(auto_card, source, "cron"),
                DEFAULT_AUTO_CRON,
                "auto.cron",
                warn_fn,
            ),
            apps=auto_apps,
            session=_as_str(
                _pick(auto_card, source, "session"), "", "auto.session", warn_fn
            ),
            max_items=_as_int(
                _pick(auto_card, source, "max_items"),
                6,
                "auto.max_items",
                warn_fn,
                minimum=1,
                maximum=50,
            ),
            prompt=_as_str(
                _pick(auto_card, source, "prompt"), "", "auto.prompt", warn_fn
            ),
            share_quiet_hours_enabled=_as_bool(
                _pick(auto_card, source, "share_quiet_hours_enabled"),
                True,
                "auto.share_quiet_hours_enabled",
                warn_fn,
            ),
            share_start_hour=_as_int(
                _pick(auto_card, source, "share_start_hour"),
                DEFAULT_SHARE_START_HOUR,
                "auto.share_start_hour",
                warn_fn,
                minimum=0,
                maximum=23,
            ),
            share_end_hour=_as_int(
                _pick(auto_card, source, "share_end_hour"),
                DEFAULT_SHARE_END_HOUR,
                "auto.share_end_hour",
                warn_fn,
                minimum=0,
                maximum=23,
            ),
            share_timezone=_as_str(
                _pick(auto_card, source, "share_timezone"),
                DEFAULT_SHARE_TIMEZONE,
                "auto.share_timezone",
                warn_fn,
            ),
            share_user_recent_hours=_as_int(
                _pick(auto_card, source, "share_user_recent_hours"),
                DEFAULT_USER_RECENT_HOURS,
                "auto.share_user_recent_hours",
                warn_fn,
                minimum=1,
            ),
            share_cooldown_minutes=_as_int(
                _pick(auto_card, source, "share_cooldown_minutes"),
                DEFAULT_SHARE_COOLDOWN_MINUTES,
                "auto.share_cooldown_minutes",
                warn_fn,
                minimum=0,
                maximum=1440,
            ),
            random_enabled=_as_bool(
                _pick(auto_card, source, "random_enabled"),
                True,
                "auto.random_enabled",
                warn_fn,
            ),
            random_interval_min=random_min,
            random_interval_max=random_max,
            random_probability=_as_float(
                _pick(auto_card, source, "random_probability"),
                DEFAULT_RANDOM_PROBABILITY,
                "auto.random_probability",
                warn_fn,
                minimum=0.0,
                maximum=1.0,
            ),
            share_only_interesting=_as_bool(
                _pick(auto_card, source, "share_only_interesting"),
                True,
                "auto.share_only_interesting",
                warn_fn,
            ),
            continuous_enabled=_as_bool(
                _pick(auto_card, source, "continuous_enabled"),
                DEFAULT_CONTINUOUS_ENABLED,
                "auto.continuous_enabled",
                warn_fn,
            ),
            continuous_gap_seconds=_as_int(
                _pick(auto_card, source, "continuous_gap_seconds"),
                DEFAULT_CONTINUOUS_GAP_SECONDS,
                "auto.continuous_gap_seconds",
                warn_fn,
                minimum=1,
            ),
            continuous_target_tokens=_as_int(
                _pick(auto_card, source, "continuous_target_tokens"),
                DEFAULT_CONTINUOUS_TARGET_TOKENS,
                "auto.continuous_target_tokens",
                warn_fn,
                minimum=0,
            ),
            continuous_apps=continuous_apps,
            user_priority=_as_bool(
                _pick(auto_card, source, "user_priority"),
                True,
                "auto.user_priority",
                warn_fn,
            ),
        )

        verification_card = card("verification")
        verification = VerificationSettings(
            max_attempts=_as_int(
                _pick(verification_card, source, "max_attempts"),
                DEFAULT_MAX_ATTEMPTS,
                "verification.max_attempts",
                warn_fn,
                minimum=0,
            ),
            total_timeout_seconds=_as_int(
                _pick(verification_card, source, "total_timeout_seconds"),
                DEFAULT_TOTAL_TIMEOUT_SECONDS,
                "verification.total_timeout_seconds",
                warn_fn,
                minimum=10,
            ),
            attempt_timeout_seconds=_as_int(
                _pick(verification_card, source, "attempt_timeout_seconds"),
                DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
                "verification.attempt_timeout_seconds",
                warn_fn,
                minimum=5,
            ),
            manual_wait_timeout_seconds=_as_int(
                _pick(verification_card, source, "manual_wait_timeout_seconds"),
                DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS,
                "verification.manual_wait_timeout_seconds",
                warn_fn,
                minimum=30,
            ),
            notify_target=_as_str(
                _pick(verification_card, source, "notify_target"),
                "",
                "verification.notify_target",
                warn_fn,
            ),
        )

        traffic_card = card("traffic")
        traffic = TrafficSettings(
            provider="app" if adb.mode == "app" and (traffic_card or {}).get("provider") in ("app", "App 抓包") else "mitmproxy",
            transport=_as_choice(_pick(traffic_card, source, "transport"), "adb", "traffic.transport", warn_fn, ("adb", "mesh")),
            ca_mode=_as_choice(_pick(traffic_card, source, "ca_mode"), "system", "traffic.ca_mode", warn_fn, ("system", "manual")),
            enabled=_as_bool(
                _pick(traffic_card, source, "enabled"),
                True,
                "traffic.enabled",
                warn_fn,
            ),
            port=_as_int(
                _pick(traffic_card, source, "port"),
                DEFAULT_TRAFFIC_PORT,
                "traffic.port",
                warn_fn,
                minimum=1024,
                maximum=65535,
            ),
            body_chars=_as_int(
                _pick(traffic_card, source, "body_chars"),
                DEFAULT_TRAFFIC_BODY_CHARS,
                "traffic.body_chars",
                warn_fn,
                minimum=200,
                maximum=8000,
            ),
            keep_seconds=_as_int(
                _pick(traffic_card, source, "keep_seconds"),
                DEFAULT_TRAFFIC_KEEP_SECONDS,
                "traffic.keep_seconds",
                warn_fn,
                minimum=60,
                maximum=86400,
            ),
            mitmdump_path=str(
                _pick(traffic_card, source, "mitmdump_path") or ""
            ).strip(),
        )

        wireguard_card, mesh_card = runtime_cards(source, warn_fn)
        wireguard_mode = _as_choice(
            _pick(wireguard_card, source, "mode"),
            DEFAULT_WIREGUARD_MODE,
            "wireguard.mode",
            warn_fn,
            WIREGUARD_MODES,
        )
        wireguard = WireGuardSettings(
            enabled=_as_bool(
                _pick(wireguard_card, source, "enabled"),
                False,
                "wireguard.enabled",
                warn_fn,
            ),
            mode=wireguard_mode,
            listen_port=_as_int(
                _pick(wireguard_card, source, "listen_port"),
                DEFAULT_WIREGUARD_LISTEN_PORT,
                "wireguard.listen_port",
                warn_fn,
                minimum=0,
                maximum=65535,
            ),
            interface_address=_as_str(
                _pick(wireguard_card, source, "interface_address"),
                DEFAULT_WIREGUARD_INTERFACE_ADDRESS[wireguard_mode],
                "wireguard.interface_address",
                warn_fn,
            ),
            server_endpoint=_as_str(
                _pick(wireguard_card, source, "server_endpoint"),
                "",
                "wireguard.server_endpoint",
                warn_fn,
            ),
            public_endpoint=_as_str(
                _pick(wireguard_card, source, "public_endpoint"),
                "",
                "wireguard.public_endpoint",
                warn_fn,
            ),
            server_public_key=_as_str(
                _pick(wireguard_card, source, "server_public_key"),
                "",
                "wireguard.server_public_key",
                warn_fn,
            ),
            preshared_key=_as_str(
                _pick(wireguard_card, source, "preshared_key"),
                "",
                "wireguard.preshared_key",
                warn_fn,
            ),
            phone_wg_ip=_as_str(
                _pick(wireguard_card, source, "phone_wg_ip"),
                DEFAULT_WIREGUARD_PHONE_WG_IP[wireguard_mode],
                "wireguard.phone_wg_ip",
                warn_fn,
            ),
            adb_local_port=_as_int(
                _pick(wireguard_card, source, "adb_local_port"),
                DEFAULT_WIREGUARD_ADB_LOCAL_PORT,
                "wireguard.adb_local_port",
                warn_fn,
                minimum=0,
                maximum=65535,
            ),
            auto_adb=_as_bool(
                _pick(wireguard_card, source, "auto_adb"),
                False,
                "wireguard.auto_adb",
                warn_fn,
            ),
            adb_target_port=_as_int(_pick(wireguard_card, source, "adb_target_port"), 5555, "wireguard.adb_target_port", warn_fn, minimum=1, maximum=65535),
            socks_port=_as_int(_pick(wireguard_card, source, "socks_port"), 0, "wireguard.socks_port", warn_fn, minimum=0, maximum=65535),
        )

        memory_card = card("memory")
        memory_backend_text = _memory_backend_legacy(memory_card, source, warn_fn)
        companion_raw = _pick(memory_card, source, "companion_enabled")
        livingmemory_raw = _pick(memory_card, source, "livingmemory_enabled")
        legacy_companion = memory_backend_text in {
            "companion",
            "companion+livingmemory",
        }
        legacy_livingmemory = memory_backend_text in {
            "livingmemory",
            "companion+livingmemory",
        }
        companion_enabled = _as_bool(
            companion_raw, False, "memory.companion_enabled", warn_fn
        )
        livingmemory_enabled = _as_bool(
            livingmemory_raw, False, "memory.livingmemory_enabled", warn_fn
        )
        # 旧版 backend 非 local 且两个新开关都未选中：按旧选择派生（AstrBot 会为
        # 已安装配置注入新字段默认值 false，不能只看字段是否存在）。迁移时插件会
        # 把 backend 置为 local，之后用户手动关闭开关可正常保存。
        if (
            not companion_enabled
            and not livingmemory_enabled
            and memory_backend_text != "local"
        ):
            companion_enabled = legacy_companion
            livingmemory_enabled = legacy_livingmemory
        memory = MemorySettings(
            local_enabled=_as_bool(
                _pick(memory_card, source, "local_enabled"),
                DEFAULT_MEMORY_LOCAL_ENABLED,
                "memory.local_enabled",
                warn_fn,
            ),
            companion_enabled=companion_enabled,
            livingmemory_enabled=livingmemory_enabled,
            companion_user_id=_as_str(
                _pick(memory_card, source, "companion_user_id"),
                DEFAULT_MEMORY_COMPANION_USER_ID,
                "memory.companion_user_id",
                warn_fn,
            ),
            bot_action_enabled=_as_bool(
                _pick(memory_card, source, "bot_action_enabled"),
                False,
                "memory.bot_action_enabled",
                warn_fn,
            ),
        )

        model_card = card("model")
        model = ModelSettings(
            work_as_subagent=_as_bool(
                _pick(model_card, source, "work_as_subagent"), False,
                "model.work_as_subagent", warn_fn,
            ),
            provider_id=_as_str(
                _pick(model_card, source, "provider_id"),
                "",
                "model.provider_id",
                warn_fn,
            ),
            asr_provider_id=_as_str(
                _pick(model_card, source, "asr_provider_id") or _pick(media_card, source, "asr_provider_id"),
                "",
                "model.asr_provider_id",
                warn_fn,
            ),
            video_provider_id=_as_str(
                _pick(model_card, source, "video_provider_id"), "", "model.video_provider_id", warn_fn,
            ),
            video_audio_separate=_as_bool(
                _pick(model_card, source, "video_audio_separate"), False, "model.video_audio_separate", warn_fn,
            ),
        )

        quota_card = card("quota")
        quota = QuotaSettings(
            enabled=_as_bool(
                _pick(quota_card, source, "enabled"),
                DEFAULT_QUOTA_ENABLED,
                "quota.enabled",
                warn_fn,
            ),
            window_hours=_as_int(
                _pick(quota_card, source, "window_hours"),
                DEFAULT_QUOTA_WINDOW_HOURS,
                "quota.window_hours",
                warn_fn,
                minimum=1,
            ),
            max_tokens=_as_int(
                _pick(quota_card, source, "max_tokens"),
                DEFAULT_QUOTA_MAX_TOKENS,
                "quota.max_tokens",
                warn_fn,
                minimum=0,
            ),
            play_task_tokens=_as_int(
                _pick(quota_card, source, "play_task_tokens"),
                DEFAULT_QUOTA_PLAY_TASK_TOKENS,
                "quota.play_task_tokens",
                warn_fn,
                minimum=0,
            ),
            per_step_tokens=_as_int(
                _pick(quota_card, source, "per_step_tokens"),
                DEFAULT_QUOTA_PER_STEP_TOKENS,
                "quota.per_step_tokens",
                warn_fn,
                minimum=0,
            ),
        )

        control_card = card("control")
        scrcpy_card: Mapping[str, Any] = {}
        minitouch_card: Mapping[str, Any] = {}
        if isinstance(control_card, Mapping):
            raw_scrcpy = control_card.get("scrcpy")
            if isinstance(raw_scrcpy, Mapping):
                scrcpy_card = raw_scrcpy
            raw_minitouch = control_card.get("minitouch")
            if isinstance(raw_minitouch, Mapping):
                minitouch_card = raw_minitouch
        control = ControlSettings(
            backend=_as_choice(
                _pick(control_card, source, "backend"),
                DEFAULT_CONTROL_BACKEND,
                "control.backend",
                warn_fn,
                CONTROL_BACKENDS,
            ),
            scrcpy=ScrcpySettings(
                max_size=_as_int(
                    _pick(scrcpy_card, source, "max_size"),
                    DEFAULT_SCRCPY_MAX_SIZE,
                    "control.scrcpy.max_size",
                    warn_fn,
                    minimum=0,
                    maximum=8192,
                ),
                server_jar=_as_str(
                    _pick(scrcpy_card, source, "server_jar"),
                    DEFAULT_SCRCPY_SERVER_JAR,
                    "control.scrcpy.server_jar",
                    warn_fn,
                ),
                keep_alive=_as_bool(_pick(scrcpy_card, source, "keep_alive"), True, "control.scrcpy.keep_alive", warn_fn),
                idle_stop_seconds=_as_int(
                    _pick(scrcpy_card, source, "idle_stop_seconds"),
                    DEFAULT_SCRCPY_IDLE_STOP_SECONDS,
                    "control.scrcpy.idle_stop_seconds",
                    warn_fn,
                    minimum=10,
                    maximum=3600,
                ),
            ),
            minitouch=MinitouchSettings(
                binary_path=_as_str(
                    _pick(minitouch_card, source, "binary_path") if _as_bool(
                        _pick(control_card, source, "minitouch_enabled"),
                        bool(minitouch_card.get("binary_path")),
                        "control.minitouch_enabled", warn_fn,
                    ) else "",
                    DEFAULT_MINITOUCH_BINARY_PATH,
                    "control.minitouch.binary_path",
                    warn_fn,
                ),
            ),
        )

        prompt_card = card("prompt")
        prompt = PromptSettings(
            inject_guidance=_as_bool(
                _pick(prompt_card, source, "inject_guidance"),
                True,
                "prompt.inject_guidance",
                warn_fn,
            ),
            diary_limit=_as_int(
                _pick(prompt_card, source, "diary_limit"),
                5,
                "prompt.diary_limit",
                warn_fn,
                minimum=0,
                maximum=50,
            ),
            app_memory_enabled=_as_bool(
                _pick(prompt_card, source, "app_memory_enabled"),
                True,
                "prompt.app_memory_enabled",
                warn_fn,
            ),
            app_memory_embedding_provider_id=_as_str(
                model_value("app_memory_embedding_provider_id", _pick(prompt_card, source, "app_memory_embedding_provider_id")) or _pick(card("memory"), source, "embedding_provider_id") or model_value("embedding_provider_id", ""),
                "",
                "prompt.app_memory_embedding_provider_id",
                warn_fn,
            ),
            app_memory_limit=_as_int(
                _pick(prompt_card, source, "app_memory_limit"),
                5,
                "prompt.app_memory_limit",
                warn_fn,
                minimum=0,
            ),
        )

        command_card = card("command")
        command = CommandSettings(
            status_as_image=_as_bool(
                _pick(command_card, source, "status_as_image"), True,
                "command.status_as_image", warn_fn,
            ),
            help_as_image=_as_bool(
                _pick(command_card, source, "help_as_image"), True,
                "command.help_as_image", warn_fn,
            ),
        )

        debug_card = card("debug")
        debug = DebugSettings(
            log_actions=_as_bool(
                _pick(debug_card, source, "log_actions"),
                True,
                "debug.log_actions",
                warn_fn,
            ),
        )

        novelty = novelty_settings_from_raw(card("novelty"), source, warn_fn)

        recall_card = card("recall")
        recall = RecallSettings(
            enabled=_as_bool(
                _pick(recall_card, source, "enabled"),
                DEFAULT_RECALL_ENABLED,
                "recall.enabled",
                warn_fn,
            ),
            embedding_provider_id=_as_str(
                _pick(card("memory"), source, "embedding_provider_id") or model_value("embedding_provider_id", _pick(recall_card, source, "embedding_provider_id")),
                DEFAULT_RECALL_EMBEDDING_PROVIDER_ID,
                "recall.embedding_provider_id",
                warn_fn,
            ),
            top_k=_as_int(
                _pick(recall_card, source, "top_k"),
                DEFAULT_RECALL_TOP_K,
                "recall.top_k",
                warn_fn,
                minimum=1,
                maximum=20,
            ),
            min_score=_as_float(
                _pick(recall_card, source, "min_score"),
                DEFAULT_RECALL_MIN_SCORE,
                "recall.min_score",
                warn_fn,
                minimum=0.0,
                maximum=1.0,
            ),
            max_chars=_as_int(
                _pick(recall_card, source, "max_chars"),
                DEFAULT_RECALL_MAX_CHARS,
                "recall.max_chars",
                warn_fn,
                minimum=100,
                maximum=8000,
            ),
            index_max_docs=_as_int(
                _pick(recall_card, source, "index_max_docs"),
                DEFAULT_RECALL_INDEX_MAX_DOCS,
                "recall.index_max_docs",
                warn_fn,
                minimum=10,
                maximum=50000,
            ),
            doc_chars=_as_int(
                _pick(recall_card, source, "doc_chars"),
                DEFAULT_RECALL_DOC_CHARS,
                "recall.doc_chars",
                warn_fn,
                minimum=50,
                maximum=2000,
            ),
            timeout_seconds=_as_int(
                _pick(recall_card, source, "timeout_seconds"),
                DEFAULT_RECALL_TIMEOUT_SECONDS,
                "recall.timeout_seconds",
                warn_fn,
                minimum=1,
                maximum=30,
            ),
            refresh_minutes=_as_int(
                _pick(recall_card, source, "refresh_minutes"),
                DEFAULT_RECALL_REFRESH_MINUTES,
                "recall.refresh_minutes",
                warn_fn,
                minimum=0,
                maximum=1440,
            ),
            include_diary=_as_bool(
                _pick(recall_card, source, "include_diary"),
                True,
                "recall.include_diary",
                warn_fn,
            ),
            include_learning=_as_bool(
                _pick(recall_card, source, "include_learning"),
                True,
                "recall.include_learning",
                warn_fn,
            ),
        )

        return cls(
            adb=adb,
            security=security,
            perception=perception,
            media=media,
            browse=browse,
            image=image,
            auto=auto,
            flexibility=flexibility,
            quota=quota,
            memory=memory,
            traffic=traffic,
            verification=verification,
            model=model,
            control=control,
            wireguard=wireguard,
            mesh=mesh_settings(mesh_card, warn_fn),
            prompt=prompt,
            novelty=novelty,
            recall=recall,
            command=command,
            debug=debug,
            report=ReportSettings(
                enabled=_as_bool(_pick(card("report"), source, "enabled"), True, "report.enabled", warn_fn),
                target=_as_str(_pick(card("report"), source, "target"), "", "report.target", warn_fn),
                cooldown_seconds=_as_int(_pick(card("report"), source, "cooldown_seconds"), 300, "report.cooldown_seconds", warn_fn, minimum=1, maximum=86400),
            ),
        )
