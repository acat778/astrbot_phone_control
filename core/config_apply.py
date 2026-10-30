"""配置热应用：把新旧配置快照的差异翻译成运行态需要执行的动作。

插件有两种配置修改入口：

* AstrBot 原生插件配置页：框架保存后重载插件，所有组件按新配置重新构造；
* 运行中的 ``/phone`` 命令补丁（如 ``/phone continuous``、``/phone traffic``）：
  插件不能重启自己，必须把变化同步应用到已经启动的组件上。

各配置项被读取的时机不同，分两类：

* ``LIVE_KEYS``：每次使用时都从 :class:`PhoneConfig` 重新解析，保存后立即生效，
  无需额外动作；
* ``RECONCILE_KEYS``：被组件缓存、构造参数或运行标志固化，保存后必须显式重建/
  重启（``AdbClient`` 连接参数、控制/感知后端、持续刷手机循环、实时屏幕 watcher、
  自主浏览 cron、随机玩手机排程）。

本模块只做纯比较（不依赖 AstrBot、不执行 IO），生成动作清单与提示文案，由
``main.PhoneControlPlugin._apply_runtime_config`` 执行，便于单测覆盖与防止新增
配置项漏接。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import get_type_hints

from .config import PhoneConfig

# 每次使用都实时读取的配置字段（保存后立即生效，无需重建组件）。
LIVE_KEYS: frozenset[str] = frozenset(
    {
        "report.enabled",
        "report.target",
        "report.cooldown_seconds",
        "command.status_as_image",
        "command.help_as_image",
        "security.require_admin",
        "security.allowed_user_ids",
        "security.allow_shell_tool",
        "security.allow_interactions",
        "security.max_likes_per_session",
        "security.auto_clean_after_browse",
        "security.auto_clean_after_task",
        "security.background_keep_packages",
        "security.owner_ids",
        "security.allow_comments",
        "security.allow_posts",
        "security.max_comments_per_day",
        "security.max_posts_per_day",
        "security.interaction_cooldown_seconds",
        "security.allow_passive_replies",
        "security.max_passive_replies_per_day",
        "security.passive_reply_cooldown_seconds",
        "perception.max_ui_nodes",
        "perception.dump_timeout",
        "perception.wait_default_seconds",
        "perception.wait_max_seconds",
        "perception.include_screenshot_by_default",
        "perception.max_text_len",
        "perception.screenshot_mode",
        "perception.screenshot_slow_ms",
        "perception.screenshot_auto_clean",
        "perception.screenshot_retention_minutes",
        "perception.live_screen_interval_ms",
        "perception.live_screen_frames",
        "perception.live_screen_idle_stop_seconds",
        "perception.ocr_provider_id",
        "perception.ocr_timeout_seconds",
        "perception.verify_timeout_seconds",
        "control.scrcpy.keep_alive",
        "media.preview_seconds",
        "media.enabled",
        "media.full_video_enabled",
        "media.full_video_max_mb",
        "media.full_video_max_seconds",
        "media.audio_enabled",
        "media.audio_source",
        "media.seconds",
        "media.max_seconds",
        "media.frame_interval_seconds",
        "media.max_frames",
        "media.asr_provider_id",
        "media.vision_provider_id",
        "media.asr_timeout_seconds",
        "media.vision_timeout_seconds",
        "media.keep_audio",
        "browse.default_app",
        "browse.default_items",
        "browse.max_items",
        "browse.swipe_duration_ms",
        "browse.settle_seconds",
        "browse.collect_screenshots",
        "browse.send_screenshots_to_user",
        "browse.auto_remember",
        "image.auto_save_images",
        "image.image_storage_location",
        "image.save_images_both",
        "browse.diary_max_entries",
        "browse.diary_protect_rounds",
        "auto.share_quiet_hours_enabled",
        "auto.share_start_hour",
        "auto.share_end_hour",
        "auto.share_timezone",
        "auto.share_user_recent_hours",
        "auto.share_cooldown_minutes",
        "auto.share_only_interesting",
        "flexibility.enabled",
        "flexibility.max_comments_per_day",
        "flexibility.max_passive_replies_per_day",
        "flexibility.max_posts_per_day",
        "flexibility.ignore_interaction_cooldown",
        "flexibility.free_browse",
        "flexibility.relax_interest_novelty",
        "flexibility.extend_task_budget",
        "auto.continuous_gap_seconds",
        "auto.continuous_target_tokens",
        "auto.continuous_apps",
        "auto.play_timezone",
        "auto.random_windows",
        "auto.continuous_windows",
        "auto.user_priority",
        "model.work_as_subagent",
        "model.provider_id",
        "model.asr_provider_id",
        "model.video_provider_id",
        "model.video_audio_separate",
        "quota.enabled",
        "quota.window_hours",
        "quota.max_tokens",
        "quota.play_task_tokens",
        "quota.per_step_tokens",
        "prompt.inject_guidance",
        "prompt.diary_limit",
        "prompt.app_memory_enabled",
        "prompt.app_memory_limit",
        "prompt.app_memory_embedding_provider_id",
        "control.scrcpy.idle_stop_seconds",
        "debug.log_actions",
        "memory.local_enabled",
        "memory.companion_enabled",
        "memory.livingmemory_enabled",
        "memory.companion_user_id",
        "memory.bot_action_enabled",
        "traffic.enabled",
        "traffic.provider",
        "traffic.transport",
        "traffic.ca_mode",
        "traffic.port",
        "traffic.body_chars",
        "traffic.keep_seconds",
        "traffic.mitmdump_path",
        "verification.max_attempts",
        "verification.total_timeout_seconds",
        "verification.attempt_timeout_seconds",
        "verification.manual_wait_timeout_seconds",
        "verification.notify_target",
        "novelty.threshold",
        "novelty.half_life_hours",
        "novelty.gain",
        "novelty.similarity",
        "novelty.max_entries",
        "novelty.inject_limit",
        # 聊天语义找回全部字段都在使用时实时读取：查询参数每轮从配置快照读取，
        # 索引维护任务下一轮检查时按新参数重建（provider/来源开关/上限变化都会
        # 改变文档 hash 或签名），无需重建插件组件。
        "recall.enabled",
        "recall.embedding_provider_id",
        "recall.top_k",
        "recall.min_score",
        "recall.max_chars",
        "recall.index_max_docs",
        "recall.doc_chars",
        "recall.timeout_seconds",
        "recall.refresh_minutes",
        "recall.include_diary",
        "recall.include_learning",
    }
)

# 被组件缓存/构造参数固化的配置字段（保存后必须重建或重启对应组件）。
RECONCILE_KEYS: frozenset[str] = frozenset(
    {
        "adb.mode",
        "adb.app_port",
        "adb.app_screen_source",
        "adb.app_video_standby",
        "adb.app_frame_max_size",
        "adb.app_frame_fps",
        "adb.app_frame_quality",
        "adb.app_frame_max_age_ms",
        "adb.host",
        "adb.port",
        "adb.path",
        "adb.serial",
        "adb.connect_timeout",
        "adb.command_timeout",
        "control.backend",
        "control.scrcpy.max_size",
        "control.scrcpy.server_jar",
        "control.minitouch.binary_path",
        "perception.screen_source",
        "perception.live_screen_enabled",
        "auto.enabled",
        "auto.cron",
        "auto.apps",
        "auto.session",
        "auto.max_items",
        "auto.prompt",
        "auto.random_enabled",
        "auto.random_interval_min",
        "auto.random_interval_max",
        "auto.random_probability",
        "auto.continuous_enabled",
        "mesh.enabled",
        "mesh.provider",
        "mesh.zerotier_network_id",
        "mesh.zerotier_library",
        "mesh.zerotier_force_tcp_relay",
        "mesh.hostname",
        "mesh.tailscale_auth_key",
        "mesh.tailscale_path",
        "mesh.tailscaled_path",
        "mesh.phone_ip",
        "mesh.auto_adb",
        "mesh.adb_port",
        "mesh.local_adb_port",
        "mesh.proxy_port",
        "wireguard.enabled",
        "wireguard.mode",
        "wireguard.listen_port",
        "wireguard.interface_address",
        "wireguard.server_endpoint",
        "wireguard.public_endpoint",
        "wireguard.server_public_key",
        "wireguard.preshared_key",
        "wireguard.phone_wg_ip",
        "wireguard.adb_local_port",
        "wireguard.adb_target_port",
        "wireguard.socks_port",
        "wireguard.auto_adb",
    }
)

# 控制/感知后端需要重建的字段（``scrcpy.idle_stop_seconds`` 由空闲检查实时读取）。
_BACKEND_RECONCILE_FIELDS = (
    "backend",
    "scrcpy.max_size",
    "scrcpy.server_jar",
    "minitouch.binary_path",
)
# 自主浏览 cron 任务的配置字段（变化后需要按新配置同步任务）。
_SCHEDULER_FIELDS = ("enabled", "cron", "apps", "session", "max_items", "prompt")
# 随机玩手机排程字段（变化后需要重算下一次触发时间）。
_RANDOM_FIELDS = (
    "random_enabled",
    "random_interval_min",
    "random_interval_max",
    "random_probability",
)

# 动作取值
ACTION_NONE = ""
ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_RESTART = "restart"


def config_field_paths() -> frozenset[str]:
    """展开 :class:`PhoneConfig` 的全部叶子字段为点号路径（用于分类完整性测试）。"""
    paths: set[str] = set()

    def walk(config_type: type, prefix: str) -> None:
        hints = get_type_hints(config_type)
        for item in dataclasses.fields(config_type):
            field_type = hints.get(item.name, item.type)
            path = f"{prefix}{item.name}"
            if dataclasses.is_dataclass(field_type):
                walk(field_type, path + ".")
            else:
                paths.add(path)

    walk(PhoneConfig, "")
    return frozenset(paths)


@dataclass(frozen=True)
class ConfigApplyPlan:
    """一次配置保存需要执行的运行态动作。"""

    adb_changed: bool = False
    backends_changed: bool = False
    scheduler_changed: bool = False
    random_changed: bool = False
    continuous_action: str = ACTION_NONE
    live_screen_action: str = ACTION_NONE
    mesh_changed: bool = False
    wireguard_changed: bool = False
    wireguard_action: str = ACTION_NONE
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """是否包含任何需要重建/重启的动作。"""
        return bool(
            self.mesh_changed
            or self.adb_changed
            or self.backends_changed
            or self.scheduler_changed
            or self.random_changed
            or self.continuous_action
            or self.live_screen_action
            or self.wireguard_changed
            or self.wireguard_action
        )

    @property
    def needs_backends_reset(self) -> bool:
        """是否需要重置控制/感知后端（ADB 目标变化也算）。"""
        return bool(self.adb_changed or self.backends_changed)


def _tuple_of(settings: object, names: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(getattr(settings, name) for name in names)


def _nested_value(settings: object, path: str) -> object:
    """按点号路径读取嵌套配置值（如 ``scrcpy.max_size``）。"""
    value: object = settings
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _backend_reconcile_values(config: PhoneConfig) -> tuple[object, ...]:
    """控制/感知后端重建所需比较的字段值。"""
    return tuple(
        _nested_value(config.control, path) for path in _BACKEND_RECONCILE_FIELDS
    )


def build_apply_plan(previous: PhoneConfig, current: PhoneConfig) -> ConfigApplyPlan:
    """比较新旧配置，生成需要执行的运行态动作与提示文案。

    Args:
        previous: 保存前的配置快照。
        current: 保存后的配置快照。

    Returns:
        动作清单；配置未变化时各动作均为空/False，``notes`` 为空元组。
    """
    adb_changed = previous.adb != current.adb
    backends_changed = _backend_reconcile_values(previous) != _backend_reconcile_values(
        current
    ) or (previous.perception.screen_source != current.perception.screen_source)
    scheduler_changed = _tuple_of(previous.auto, _SCHEDULER_FIELDS) != _tuple_of(
        current.auto, _SCHEDULER_FIELDS
    )
    random_changed = _tuple_of(previous.auto, _RANDOM_FIELDS) != _tuple_of(
        current.auto, _RANDOM_FIELDS
    )
    continuous_action = ACTION_NONE
    if previous.auto.continuous_enabled != current.auto.continuous_enabled:
        continuous_action = (
            ACTION_START if current.auto.continuous_enabled else ACTION_STOP
        )
    live_screen_action = ACTION_NONE
    if (
        previous.perception.live_screen_enabled
        != current.perception.live_screen_enabled
    ):
        live_screen_action = (
            ACTION_START if current.perception.live_screen_enabled else ACTION_STOP
        )
    # WireGuard helper 的配置在子进程启动时固化：任何 wireguard 字段变化都需要
    # 停止/启动/按新配置重启，不能只靠「每次使用时读取」。
    wireguard_changed = previous.wireguard != current.wireguard
    wireguard_action = ACTION_NONE
    if wireguard_changed and current.wireguard.enabled:
        wireguard_action = (
            ACTION_RESTART if previous.wireguard.enabled else ACTION_START
        )
    elif wireguard_changed and previous.wireguard.enabled:
        wireguard_action = ACTION_STOP

    notes: list[str] = []
    if adb_changed:
        if current.adb.configured:
            notes.append(f"ADB 连接参数已更新（{current.adb.target}）")
        else:
            notes.append("ADB 地址/端口未配置：手机能力未启用，补全并重新加载插件后启用")
    if backends_changed:
        notes.append("控制/感知后端已按新配置重置，下次手机操作重新连接")
    if continuous_action == ACTION_START:
        notes.append("持续刷手机：已开启，后台循环立即开始排程")
    elif continuous_action == ACTION_STOP:
        notes.append("持续刷手机：已关闭，相关任务已停止")
    if live_screen_action == ACTION_START:
        notes.append("任务期间持续截图：已开启")
    elif live_screen_action == ACTION_STOP:
        notes.append("任务期间持续截图：已关闭并停止 watcher")
    if scheduler_changed:
        if current.auto.enabled:
            notes.append("自主浏览：已按新配置同步 cron 任务")
        else:
            notes.append(
                "自主浏览：已关闭并同步删除定时排程"
            )
    if random_changed:
        notes.append("随机玩手机：已按新配置重新排程")
    if wireguard_action == ACTION_START:
        notes.append("WireGuard：已开启，helper 将按新配置启动")
    elif wireguard_action == ACTION_RESTART:
        notes.append("WireGuard：配置已更新，helper 按新配置重启")
    elif wireguard_action == ACTION_STOP:
        notes.append("WireGuard：已关闭，helper 停止并恢复 ADB 目标")
    elif wireguard_changed:
        notes.append("WireGuard：配置已更新（当前未启用）")

    return ConfigApplyPlan(
        adb_changed=adb_changed,
        backends_changed=backends_changed,
        scheduler_changed=scheduler_changed,
        random_changed=random_changed,
        continuous_action=continuous_action,
        live_screen_action=live_screen_action,
        mesh_changed=previous.mesh != current.mesh,
        wireguard_changed=wireguard_changed,
        wireguard_action=wireguard_action,
        notes=tuple(notes),
    )
