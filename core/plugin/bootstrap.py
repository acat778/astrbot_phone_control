"""插件装配与生命周期 mixin：组件装配、初始化/终止、配置持久化与热应用。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import weakref
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_data_path,
    get_astrbot_plugin_path,
)

try:  # AstrBot 内置命令的写法：一个 GreedyStr 参数吃下命令后的全部文本
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - AstrBot 版本或单测环境没有该类型
    GreedyStr = str  # type: ignore[assignment,misc]

from ..actions import DeviceActions
from ..adb import AdbClient
from ..app_memory import AppMemoryStore
from ..apps import (
    KNOWN_APP_LABELS,
    UNKNOWN_APP_HINT_TEMPLATE,
    app_label_for_package,
    format_preferred_apps,
    match_preferred_app,
    parse_package_list,
    resolve_app,
    resolve_package,
)
from ..backends import BackendManager
from ..browse import BrowseSession
from ..chat_memory import ChatMemory
from ..config import (
    UNCONFIGURED_MESSAGE,
    PhoneConfig,
    migrate_image_config,
    migrate_memory_config,
)
from ..config_apply import ACTION_START, ACTION_STOP, build_apply_plan
from ..diary import PhoneDiary
from ..interactions import (
    KIND_COMMENT,
    KIND_PASSIVE_REPLY,
    KIND_POST,
    InteractionStore,
)
from ..learning import LearningError, LearningStore
from ..novelty import NoveltyStore
from ..live_screen import LiveScreenWatcher
from ..memory_sync import (
    COMPANION_PLUGIN_NAME,
    COMPANION_SOURCE_PLUGIN,
    LEARNING_MEMORY_TYPE,
    LIVINGMEMORY_PLUGIN_NAME,
    build_bot_action_payload,
    wants_companion,
    wants_livingmemory,
)
from ..errors import (
    ActionFailedError,
    AdbNotConfiguredError,
    AppNotInstalledError,
    PhoneControlError,
)
from ..models import BrowseRequest, BrowseResult, DeviceInfo, ScreenSnapshot, UiNode
from ..continuous import (
    build_continuous_note,
    next_run_delay,
    normalize_continuous_apps,
    normalize_gap_seconds,
)
from ..random_play import (
    build_play_note,
    build_random_play_note,
    next_random_delay,
    should_play,
)
from .. import verification as verification_mod
from ..device_gate import (
    DeviceExecutionGate,
    ExecutionCancelled,
    GateError,
)
from ..screenshot_cleanup import cleanup_directory
from ..page_content import (
    PAGE_ACTION_WINDOW_MAX_SECONDS,
    PAGE_COMPACT_BODY_CHARS,
    PAGE_COMPACT_MAX_TEXT_CHARS,
    PAGE_FLOW_LIMIT,
    PAGE_MAX_TEXT_CHARS,
    PageTrafficSnapshot,
    build_page_traffic,
)
from ..traffic import extract_share_links, infer_flow_hints, mask_url, render_traffic
from ..traffic_capture import TrafficCapture
from ..wireguard import WireGuardManager
from ..share_policy import (
    classify_session_umo,
    is_recent_interaction,
    is_share_window,
    share_window_text,
)
from ..scheduler import (
    CONTINUOUS_JOB_ORIGIN,
    JOB_ORIGIN as AUTO_BROWSE_JOB_ORIGIN,
    AutoBrowseError,
    AutoBrowseScheduler,
    BackgroundTaskError,
    create_background_task,
    continuous_job_running,
    create_continuous_task,
    delete_plugin_jobs,
    list_background_tasks,
    list_plugin_jobs,
    validate_background_instruction,
)
from ..token_quota import (
    PlayBudget,
    PlayBudgetRegistry,
    TokenQuota,
    estimate_fallback_usage,
    estimate_messages_tokens,
    estimate_tokens,
    usage_breakdown,
    usage_is_empty,
)
from .. import usage_audit
from ..agent_usage_patch import (
    USAGE_EXTRA as AGENT_USAGE_EXTRA,
    install_agent_usage_capture,
    uninstall_agent_usage_capture,
)
from astrbot.core.platform.message_type import MessageType
from ..task_priority import (
    UserTaskPriority,
    should_resume_continuous,
    should_yield_to_user,
)
from ..tools import build_phone_tools, check_admin_permission
from ..media import (
    MediaAudioCapture,
    build_vision_prompt,
    clamp_seconds as clamp_media_seconds,
    clean_transcript as clean_media_transcript,
    plan_frame_times,
    safe_unlink as safe_unlink_media,
)
from ..platform_bins import ensure_bundled_executables
from ..runtime_components import RuntimeComponents, ComponentError
from ..tool_reports import ToolErrorReporter, owner_targets
from ..network_config import migrate_network, normalize_network_patch
from ..telemetry import MetricsStore
from ..ui import dump_ui, render_ui
from .shared import (
    _AutonomousDeviceEvent,
    _HELP_TEXT,
    _PHONE_CONTROL_TOOLS,
    _build_guidance,
    _companion_plugin_available,
    _read_plugin_version,
    PLUGIN_NAME,
    PHONE_KEYWORDS,
    plugin_root_dir,
    _GUIDANCE,
    _format_time,
    _llm_response_text,
    _INBOX_KEYWORDS,
    _INBOX_EXCLUDE_WORDS,
)

from ..service import PhoneService
from ..semantic import SemanticIndex
from ..semantic_recall import (
    SemanticEmbeddingResolver,
    SemanticMaintainer,
    SemanticReader,
)
from ..app_recall import AppRecallMaintainer, AppRecallReader, app_index_root
from ..verification_controller import PhoneVerification
from .compat import plugin_main_module

# 存活的插件实例登记。AstrBot 的 ``PluginManager._terminate_plugin`` 只检查
# 插件类**自身** ``__dict__`` 里是否有 ``terminate``（见
# astrbot/core/star/star_manager.py），定义在 mixin 里的 terminate 不会被探测到，
# 插件 reload/禁用后旧实例的 keepalive 等后台循环仍在运行并操作同一设备。
# 新实例激活时据此先关闭旧实例；模块级弱引用不会阻止旧实例被回收。
LIVE_PLUGIN_INSTANCES: "weakref.WeakSet[Any]" = weakref.WeakSet()
PLUGIN_INSTANCE_CLOSE_TIMEOUT = 20.0
# 插件后台任务统一命名前缀：初始化时取消其它（含历史版本）实例遗留的循环。
PLUGIN_TASK_PREFIX = f"{PLUGIN_NAME}_"


async def close_stale_instances(
    current: Any, *, timeout: float = PLUGIN_INSTANCE_CLOSE_TIMEOUT
) -> int:
    """关闭登记表里仍然存活的旧插件实例；返回处理的实例数。

    正常 reload 路径由 AstrBot 调用 ``terminate``；这里兜底其它未调用 terminate
    的重载路径，避免旧实例的 keepalive/随机/持续刷循环继续访问同一台设备。
    """
    stale = [
        instance
        for instance in list(LIVE_PLUGIN_INSTANCES)
        if instance is not current
    ]
    if not stale:
        return 0
    logger.warning(
        "检测到 %d 个旧插件实例仍存活，先停用旧实例，避免后台循环重复操作设备",
        len(stale),
    )
    for instance in stale:
        terminate = getattr(instance, "terminate", None)
        if not callable(terminate):
            LIVE_PLUGIN_INSTANCES.discard(instance)
            continue
        try:
            await asyncio.wait_for(terminate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "停用旧插件实例超时（%.0f 秒），已取消其终止流程", timeout
            )
        except Exception as exc:  # noqa: BLE001 - 停用旧实例失败不阻塞新实例
            logger.warning(
                "停用旧插件实例失败：%s: %s", type(exc).__name__, exc
            )
        finally:
            LIVE_PLUGIN_INSTANCES.discard(instance)
    logger.info("已停用 %d 个旧插件实例", len(stale))
    return len(stale)


async def cancel_stale_background_tasks(*, timeout: float = 10.0) -> int:
    """取消其它插件实例遗留的后台循环（跨模块版本的兜底）。

    后台任务按当前插件标识的前缀识别；初始化时回收该命名空间的
    keepalive、随机与持续循环，保持每个插件实例的资源归属。
    """
    current = asyncio.current_task()
    stale: list[asyncio.Task] = []
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        try:
            name = task.get_name()
        except Exception:  # noqa: BLE001 - 任务对象异常时跳过
            continue
        if isinstance(name, str) and name.startswith(PLUGIN_TASK_PREFIX):
            stale.append(task)
    if not stale:
        return 0
    owners = {}
    for task in stale:
        frame = getattr(task.get_coro(), "cr_frame", None)
        owner = frame.f_locals.get("self") if frame is not None else None
        if owner is not None and hasattr(owner, "_traffic") and callable(getattr(owner, "terminate", None)):
            owners[id(owner)] = owner
    for owner in owners.values():
        try:
            await asyncio.wait_for(owner.terminate(), timeout=PLUGIN_INSTANCE_CLOSE_TIMEOUT)
        except Exception as exc:
            logger.warning("关闭遗留插件实例失败：%s", exc)
    names = "、".join(sorted({task.get_name() for task in stale}))
    logger.warning("检测到 %d 个遗留插件后台循环，正在停止：%s", len(stale), names)
    for task in stale:
        task.cancel()
    _, pending = await asyncio.wait(stale, timeout=timeout)
    if pending:
        logger.warning("有 %d 个遗留后台循环未在 %.0f 秒内退出", len(pending), timeout)
    stopped = len(stale) - len(pending)
    logger.info("已停止 %d 个遗留插件后台循环", stopped)
    return stopped


class BootstrapMixin:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        super().__init__(context)
        self._terminated = False
        self._live_instance_registered = False
        self.raw_config: Any = config if config is not None else {}
        from ..autonomy_config import migrate_autonomy
        autonomy_changed = migrate_autonomy(self.raw_config)
        from ..flexibility import migrate_flexibility
        flexibility_changed = migrate_flexibility(self.raw_config)
        from ..model_config import migrate_models, populate_embedding_options

        if migrate_models(self.raw_config) or flexibility_changed or autonomy_changed:
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                save()
        populate_embedding_options(self.raw_config, context)
        from ..app_traffic import populate_capture_options
        populate_capture_options(self.raw_config)
        from ..app_schema import populate_connection_options
        populate_connection_options(self.raw_config)
        if migrate_network(self.raw_config):
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                save()
        # 旧版 memory.backend 一次性迁移为独立布尔开关（保持原选择）。
        if migrate_memory_config(self.raw_config):
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                try:
                    save()
                except Exception as exc:  # noqa: BLE001 - 保存失败不影响插件加载
                    logger.warning("迁移记忆存储配置后保存失败：%s", exc)
            memory_raw = self.raw_config.get("memory")
            memory_raw = memory_raw if isinstance(memory_raw, dict) else {}
            logger.info(
                "记忆存储配置已迁移：MemoryCompanion=%s，LivingMemory=%s",
                bool(memory_raw.get("companion_enabled")),
                bool(memory_raw.get("livingmemory_enabled")),
            )
        # 旧版 browse.* 图片保存配置一次性迁移到独立 image 卡片。
        if migrate_image_config(self.raw_config):
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                try:
                    save()
                except Exception as exc:  # noqa: BLE001 - 保存失败不影响插件加载
                    logger.warning("迁移图片保存配置后保存失败：%s", exc)
            logger.info("图片保存配置已迁移到独立卡片")
        initial = PhoneConfig.from_mapping(
            self.raw_config,
            warn=lambda message: logger.warning(message),
        )
        # ADB 地址/端口留空表示未配置：插件以「假开启」状态加载，不连接设备、
        # 不启动任何手机后台任务；面板与配置保持可用。
        self._phone_enabled = initial.adb.configured
        security = self.raw_config.get("security", self.raw_config)
        saved_keep = (
            security.get("background_keep_packages") if isinstance(security, dict) else None
        )
        if (
            isinstance(saved_keep, (list, tuple))
            and all(isinstance(value, str) for value in saved_keep)
            and set(saved_keep) == {"com.wireguard.android", "com.tailscale.ipn"}
        ):
            security["background_keep_packages"] = list(initial.security.background_keep_packages)
            save = getattr(self.raw_config, "save_config", None)
            if callable(save):
                save()
        self.data_dir = plugin_main_module()._resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        adb_home = self.data_dir / "adbhome"
        adb_home.mkdir(parents=True, exist_ok=True)

        self._components = RuntimeComponents(self.data_dir / "runtime")
        self._components_task = None
        self._ensure_bundled_executables()
        adb_path = self._resolve_adb_path(initial) if initial.adb.configured and initial.adb.mode == "direct" else None
        self.adb = AdbClient(
            adb_path,
            initial.adb.host,
            initial.adb.port,
            adb_home,
            serial=initial.adb.serial,
            connect_timeout=initial.adb.connect_timeout,
            command_timeout=initial.adb.command_timeout,
            logger=self._device_log,
            allow_unconfigured=True,
            defer_binary=True,
        )
        from ..app_bridge import AppBridge
        from ..app_adb import AppAdbClient
        self._app_bridge = AppBridge(self.data_dir / "app-bridge", plugin_root_dir() / "bin/android/phone-adb-relay.apk")
        if initial.adb.mode == "app":
            self.adb = AppAdbClient(self._app_bridge, adb_home, command_timeout=initial.adb.command_timeout, logger=self._device_log, screen_source=initial.adb.app_screen_source, video_standby=initial.adb.app_video_standby, frame_max_size=initial.adb.app_frame_max_size, frame_fps=initial.adb.app_frame_fps, frame_quality=initial.adb.app_frame_quality, frame_max_age_ms=initial.adb.app_frame_max_age_ms)
        self.actions = DeviceActions(self.adb, logger=self._device_log)
        self.diary = PhoneDiary(self.data_dir / "diary")
        self.quota = TokenQuota(self.data_dir)
        self.play_budgets = PlayBudgetRegistry()
        self.chat_memory = ChatMemory(self.data_dir / "chat")
        self.interactions = InteractionStore(self.data_dir)
        self.telemetry = MetricsStore(self.data_dir)
        self._started_at = time.time()
        self._scrcpy_warmup_task: asyncio.Task | None = None
        self._last_scrcpy_warmup = 0.0
        self._screenshot_cleanup_task: asyncio.Task | None = None
        self._traffic_keepalive_task: asyncio.Task | None = None
        self._last_traffic_log = 0.0
        self._version = _read_plugin_version()
        # 自主任务模型供应商失效告警去重 / 运行前写入的会话偏好（run 后还原）
        self._task_provider_warned_ids: set[str] = set()
        self._task_provider_restore: dict[str, str] = {}
        # 自主任务（cron 路径）不写 provider_stats，用 runner stats 补精确 usage
        self._agent_usage_patch_installed = install_agent_usage_capture(
            lambda message: logger.info("%s", message),
            should_capture=self._should_capture_agent_usage,
            prepare_runner=self._prepare_phone_runner,
        )
        self.service = PhoneService(
            raw_config=self.raw_config,
            adb=self.adb,
            actions=self.actions,
            diary=self.diary,
            data_dir=self.data_dir,
            device_log=self._device_log,
            quota=self.quota,
            play_budgets=self.play_budgets,
            chat_memory=self.chat_memory,
            interactions=self.interactions,
            telemetry=self.telemetry,
        )
        from ..subagent import BackgroundTaskRegistry

        self.service.background_tasks = BackgroundTaskRegistry()
        self.service.app_context = self.context
        self.service.components = self._components
        self._tool_reports = ToolErrorReporter(
            settings=lambda: self.service.config.report,
            targets=self._tool_error_targets,
            send=self._send_tool_error_report,
            log=lambda message: logger.warning("%s", message),
        )
        from ..mesh import MeshManager
        self._mesh = MeshManager(self.service)
        self.service.mesh = self._mesh
        self._mesh_task = None
        self.service.task_provider_hook = self._apply_task_provider_preference
        self.service.background_submitted_hook = self._background_task_submitted
        self.service.background_cancel_hook = self._abort_running_cron_event
        self.service.background_start_guard = lambda event: self._device_blocking_reason(
            event, "phone_start_background_task", True
        )
        # 流媒体内容识别：设备播放音频采集（scrcpy 音频通道，按需启动独立 server）。
        self._media_capture = MediaAudioCapture(
            adb=self.adb,
            server_jar=lambda: self._scrcpy_server_jar_path(self.service.config),
            data_dir=self.data_dir / "media",
            logger=self._device_log,
        )
        self.service.media = self._media_capture
        if initial.adb.mode == "app":
            from ..app_recording import AppRecording
            self.service.recordings = AppRecording(self._app_bridge, self.service.files)
        # 手机流量抓包（ADB + mitmproxy）：生命周期跟随插件。
        from ..app_traffic import AppTrafficCapture
        capture_class = AppTrafficCapture if initial.adb.mode == "app" else TrafficCapture
        capture_options = {"bridge": self._app_bridge} if initial.adb.mode == "app" else {}
        self._traffic = capture_class(
            adb=self.adb,
            data_dir=self.data_dir,
            logger=logger.info,
            mitmdump=self.service.config.traffic.mitmdump_path,
            keep_packages_provider=lambda: self.service.config.security.background_keep_packages,
            settings_provider=lambda: self.service.config.traffic,
            mesh=self._mesh,
            **capture_options,
        )
        self.service.traffic = self._traffic
        if initial.adb.mode == "app":
            self._app_bridge.on_ready = self._initialize_app_peer
        # WireGuard 组网：userspace helper 生命周期跟随插件；auto_adb 开启时
        # helper 健康后把 ADB 目标切到 WG 隧道本地端口，停止/异常时恢复。
        self._wireguard = WireGuardManager(
            data_dir=self.data_dir,
            app_port_provider=lambda: self.service.config.adb.app_port if self.service.config.adb.mode == "app" else 0,
            settings_provider=lambda: self.service.config.wireguard,
            adb=self.adb,
            adb_target_provider=lambda: {
                "host": self.service.config.adb.host,
                "port": self.service.config.adb.port,
                "serial": self.service.config.adb.serial,
            },
            config_patch=self._apply_config_patch,
        )
        self.service.wireguard = self._wireguard
        # 验证会话/人工接管控制器（先加载持久化状态，再启动调度器）。
        self._verification = PhoneVerification(self)
        self.service.verification = self._verification
        # 统一设备执行入口：命令、工具与定时任务接触设备前都经过它。
        self.device_gate = DeviceExecutionGate(
            debug_log=lambda message: logger.debug("[手机] %s", message),
            authorize=self._device_authorize,
            blocking_reason=self._device_blocking_reason,
            deadline_seconds=self._device_deadline_seconds,
            autonomy_of=self._device_is_autonomous,
            log=self._device_log,
        )
        self.service.gate = self.device_gate
        # 设备动作批次内也复查门禁（暂停/取消代次/期限变化立即中止）。
        self.actions.guard = self._guard_device_action
        # App 使用经验记忆（工具/main 注入共用）与用户任务优先状态。
        self.app_memory = AppMemoryStore(self.data_dir)
        self.service.app_memory = self.app_memory
        self.service.memory_sync_hook = self._sync_memory
        # 聊天语义找回：本地向量索引 + 后台维护。读路径只持有索引/Provider/配置，
        # 不持有 service，普通聊天因此不会触碰 ADB、设备或手机后台任务。
        self.semantic_index = SemanticIndex(self.data_dir / "semantic")
        self.semantic_resolver = SemanticEmbeddingResolver(self.context)
        self.semantic_reader = SemanticReader(
            self.semantic_index,
            self.semantic_resolver,
            lambda: self.service.config.recall,
        )
        self.semantic_maintainer = SemanticMaintainer(
            index=self.semantic_index,
            resolver=self.semantic_resolver,
            config_getter=lambda: self.service.config,
            data_dir=self.data_dir,
        )
        self.service.semantic_refresh_hook = self.semantic_maintainer.request_refresh
        self._semantic_task: asyncio.Task | None = None
        # App 使用经验召回：独立索引目录与独立向量模型配置，普通聊天不读取。
        self.app_recall_index = SemanticIndex(app_index_root(self.data_dir))
        self.app_recall_resolver = SemanticEmbeddingResolver(
            self.context,
            label="App 经验召回",
            config_key="prompt.app_memory_embedding_provider_id",
        )
        self.app_recall_reader = AppRecallReader(
            index=self.app_recall_index,
            resolver=self.app_recall_resolver,
            config_getter=lambda: self.service.config.prompt,
            entry_provider=self.app_memory.entries,
        )
        self.app_recall_maintainer = AppRecallMaintainer(
            index=self.app_recall_index,
            resolver=self.app_recall_resolver,
            config_getter=lambda: self.service.config,
            data_dir=self.data_dir,
        )
        self.service.app_recall_refresh_hook = (
            self.app_recall_maintainer.request_refresh
        )
        self.service.app_recall_reader = self.app_recall_reader
        self._app_recall_task: asyncio.Task | None = None
        # 插件加载时立即按配置清理一次残留截图（可能已占 GB 级磁盘）。
        try:
            self.service.cleanup_screenshots(force=True)
        except Exception:  # noqa: BLE001 - 清理失败不影响插件加载
            pass
        self._memory_companion_bridge_cache: Any = None
        self._user_priority = self.service.user_priority
        self.live_screen = LiveScreenWatcher(
            capture=self._capture_live_frame,
            settings_provider=self._live_screen_settings,
            logger=self._device_log,
            interval_ms=initial.perception.live_screen_interval_ms,
            max_frames=initial.perception.live_screen_frames,
        )
        self.service.live_screen = self.live_screen
        self.backends = BackendManager(
            config_provider=lambda: self.service.config,
            actions=self.actions,
            adb=self.adb,
            data_dir=self.data_dir,
            adb_path=(
                self._resolve_adb_path(initial)
                if initial.adb.configured
                else self._components.resolve("adb")
            ),
            scrcpy_factory=self._create_scrcpy_client,
            minitouch_factory=self._create_minitouch_client,
            log=lambda message: logger.info("%s", message),
            warn=lambda message: logger.warning("%s", message),
            on_started=self._on_backends_started,
        )
        self.service.backends = self.backends
        self._backend_idle_task: asyncio.Task | None = None
        # 实时屏幕按「玩手机/后台任务会话」懒启动、任务结束/空闲自动停止。
        self._live_task_sessions: set[str] = set()
        self._live_last_activity = 0.0
        self._live_idle_task: asyncio.Task | None = None
        self.scheduler = AutoBrowseScheduler(
            self.context,
            config=initial,
            config_provider=lambda: self.service.config,
            run_browse=self._auto_browse,
        )
        self._activity_window_tasks: set[asyncio.Task] = set()
        self._random_task: asyncio.Task | None = None
        self._random_stop = asyncio.Event()
        # 持续刷手机：配置开启时后台循环一轮接一轮创建静默任务；/phone stop 停止。
        self._continuous_enabled = bool(initial.auto.continuous_enabled)
        self._continuous_stop = asyncio.Event()
        self._continuous_task: asyncio.Task | None = None
        self._continuous_active_job_id = ""
        self._continuous_active_since = 0.0
        self._continuous_last_finished = 0.0
        self._continuous_round_index = 0
        self._continuous_missing_learn = False
        # 正在运行的插件任务 Agent 引用（umo -> (task, job_id, origin)），/phone stop 用。
        self._active_agent_tasks: dict[str, tuple[asyncio.Task, str, str]] = {}
        self._background_agent_events: dict[str, Any] = {}
        self._register_tools()
        self._register_dashboard_apis()
        if initial.adb.configured:
            logger.info(
                "%s 已加载：ADB %s，数据目录 %s",
                PLUGIN_NAME,
                initial.adb.target,
                self.data_dir,
            )
        else:
            logger.info(
                "%s 已加载但未启用手机能力（adb.host/adb.port 未配置，假开启）："
                "面板与配置可用；填写 ADB 地址后重新加载插件即可启用。数据目录 %s",
                PLUGIN_NAME,
                self.data_dir,
            )

    async def _cleanup_diary_limits(self) -> None:
        """按配置清理浏览日志上限（不访问设备，失败只记录）。"""
        try:
            report = await self.service.cleanup_diary()
        except Exception as exc:  # noqa: BLE001 - 日志清理失败不影响插件运行
            logger.warning("浏览日志上限清理失败：%s", exc)
            return
        if report.get("removed"):
            logger.info(
                "浏览日志：按上限清理 %s 条（%s → %s 条，保护 %s 轮任务）",
                report.get("removed"),
                report.get("before"),
                report.get("after"),
                len(report.get("protected_rounds") or []),
            )

    async def initialize(self) -> None:
        """插件激活：先停用残留旧实例，再异步同步自主浏览任务。

        AstrBot 在插件 reload 之后才启动 cron 管理器，因此这里用后台任务重试，
        避免 ``add_active_job`` 在调度器尚未 start 时抛错。
        """
        # 纵深防御：AstrBot 只探测插件类自身 __dict__ 里的 terminate，历史实例
        # 可能未被停用；先关闭登记过的旧实例并取消其余遗留后台循环，再启动本实例。
        # 同一实例重复 initialize 时不再重复清理（避免取消自己刚启动的循环）。
        if not getattr(self, "_live_instance_registered", False):
            await close_stale_instances(self)
            await cancel_stale_background_tasks()
            from ..process_lease import reclaim_legacy_processes
            await reclaim_legacy_processes([
                self._traffic.confdir, self._wireguard.config_path, self.data_dir / "scrcpy",
            ])
            LIVE_PLUGIN_INSTANCES.add(self)
            self._live_instance_registered = True
        self._agent_usage_patch_installed = install_agent_usage_capture(
            lambda message: logger.info("%s", message),
            should_capture=self._should_capture_agent_usage,
            prepare_runner=self._prepare_phone_runner,
        )
        # 语义索引维护放在设备相关初始化之前：假开启（未配置 ADB）时也按配置
        # 后台建索引；任务本身只读本地档案，不阻塞 initialize。
        self._start_semantic_maintenance()
        self._start_app_recall_maintenance()
        # 浏览日志上限清理不访问设备：假开启与启用状态都要在启动时执行一次。
        await self._cleanup_diary_limits()
        if self.service.config.adb.mode == "app":
            await self._app_bridge.start(self.service.config.adb.app_port)
        if self._components.needs_prepare(self.service.config):
            self._components_task = asyncio.create_task(
                self._prepare_components_and_start(), name=f"{PLUGIN_NAME}_components"
            )
            return
        await self._components.prepare(self.service.config)
        await self._initialize_device_runtime()

    async def _prepare_components_and_start(self):
        try:
            await self._components.prepare(self.service.config)
        except asyncio.CancelledError:
            raise
        except ComponentError as exc:
            logger.warning("运行组件尚未就绪：%s；修正配置或网络后重新加载插件", exc)
            return
        if not self._terminated:
            try:
                await self._initialize_device_runtime()
            except (PhoneControlError, OSError) as exc:
                logger.warning("设备初始化失败：%s", exc)

    async def _initialize_device_runtime(self):
        if not self.service.configured:
            logger.warning(
                "未配置 ADB 地址/端口，插件保持未启用状态："
                "跳过自主浏览、随机/持续刷、抓包、WireGuard 与后台循环"
            )
            return
        old_task = getattr(self, "_auto_config_task", None)
        if old_task is not None and not old_task.done():
            old_task.cancel()
        self._auto_config_task = asyncio.create_task(
            self._apply_auto_config_when_ready(),
            name=f"{PLUGIN_NAME}_auto_config",
        )
        # 验证状态必须先恢复：存在人工等待/待确认会话时，不启动任何手机调度器。
        try:
            await self._verification.startup()
            self._verification.start_watchdog()
        except Exception as exc:  # noqa: BLE001 - 恢复失败按暂停处理
            logger.warning("验证状态恢复失败：%s", exc)
        if self._verification.paused:
            logger.warning(
                "验证：存在待人工协助/待确认会话，手机自动操作保持暂停（/phone verify status 查看）"
            )
        # 抓包必须先于自刷/随机循环就位：App 进程需在 CA 注入后启动才能被解密。
        if self.service.config.mesh.enabled:
            try:
                await self._mesh.reconcile()
            except Exception:
                self._mesh.last_error = "组网等待连接或授权"
        self._start_mesh_keepalive()
        try:
            await asyncio.wait_for(self._ensure_traffic_started(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("抓包启动超时（60s），先启动自刷循环；后续保活会重试")
        except Exception as exc:  # noqa: BLE001 - 抓包失败不能阻塞插件启用
            logger.warning("抓包启动失败：%s", exc)
        self._start_traffic_keepalive()
        try:
            await asyncio.wait_for(self._ensure_wireguard_started(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("WireGuard 启动超时（30s），保活循环会继续按退避重试")
        except Exception as exc:  # noqa: BLE001 - WireGuard 失败不能阻塞插件启用
            logger.warning("WireGuard 启动失败：%s", exc)
        self._start_wireguard_keepalive()
        self.service.contents
        self._on_backends_started()
        await self._sync_activity_jobs()
        if not self._verification.paused:
            self._start_random_play_loop()
            self._start_screenshot_cleanup_loop()
            if self._continuous_enabled:
                self._start_continuous_loop()
        else:
            logger.warning("验证暂停中：不启动随机/持续刷手机，等待用户处理")
        # 实时屏幕不常驻：等玩手机/后台任务会话第一次调用手机工具时再懒启动。
        logger.debug("实时屏幕：未启动（仅在任务运行期间按需启动）")

    def _start_semantic_maintenance(self) -> None:
        """启动聊天语义找回的后台索引维护任务。

        只在 ``recall.enabled`` 时启动；后台任务先空闲再构建索引，不阻塞插件
        初始化，也不受 ADB 是否配置影响。重复调用幂等。
        """
        maintainer = getattr(self, "semantic_maintainer", None)
        if maintainer is None:
            return
        try:
            if not self.service.config.recall.enabled:
                return
        except Exception as exc:  # noqa: BLE001 - 配置异常时不启动维护任务
            logger.debug("读取 recall 配置失败，跳过语义索引维护：%s", exc)
            return
        task = getattr(self, "_semantic_task", None)
        if task is not None and not task.done():
            return
        self._semantic_task = maintainer.start()

    def _start_app_recall_maintenance(self) -> None:
        """启动 App 经验索引后台维护（与聊天找回独立；未启用时循环空转等待）。

        不依赖 ADB 配置；只在后台读取 app_memory.json 与调用 Embedding Provider，
        失败按退避重试，不影响手机工具与聊天。
        """
        maintainer = getattr(self, "app_recall_maintainer", None)
        if maintainer is None:
            return
        task = getattr(self, "_app_recall_task", None)
        if task is not None and not task.done():
            return
        self._app_recall_task = maintainer.start()

    async def terminate(self) -> None:
        """插件停用/重载：停止后台任务并释放 adb 子进程资源（幂等）。"""
        if getattr(self, "_terminated", False):
            logger.info("插件实例已停用，跳过重复终止")
            return
        self._terminated = True
        self._unregister_web_apis()
        try:
            await self._tool_reports.close()
            await self._terminate_impl()
        finally:
            if hasattr(self, "_app_bridge"):
                await self._app_bridge.close()
            LIVE_PLUGIN_INSTANCES.discard(self)
            self._live_instance_registered = False

    async def _terminate_impl(self) -> None:
        """终止流程实现：取消后台任务、恢复设备状态并关闭资源。"""
        # 语义索引维护只读写插件数据目录：先取消，避免停用过程中继续后台构建。
        maintainer = getattr(self, "semantic_maintainer", None)
        self._semantic_task = None
        if maintainer is not None:
            try:
                await maintainer.stop()
            except Exception as exc:  # noqa: BLE001 - 停用收尾失败只记录
                logger.warning("停止语义索引维护失败：%s", exc)
        app_maintainer = getattr(self, "app_recall_maintainer", None)
        self._app_recall_task = None
        if app_maintainer is not None:
            try:
                await app_maintainer.stop()
            except Exception as exc:  # noqa: BLE001 - 停用收尾失败只记录
                logger.warning("停止 App 经验索引维护失败：%s", exc)
        resources = [getattr(self, "_traffic", None), getattr(self, "_wireguard", None),
                     getattr(self, "_media_capture", None)]
        manager = getattr(self, "backends", None)
        if manager is not None:
            resources.extend([getattr(manager, "_scrcpy_client", None),
                              getattr(manager, "_minitouch_client", None)])
        for resource in resources:
            quiesce = getattr(resource, "quiesce", None)
            if callable(quiesce):
                quiesce()
        heartbeats = []
        for resource in resources:
            for attr in ("_process_heartbeat", "_decoder_heartbeat"):
                task = getattr(resource, attr, None)
                if task is not None:
                    heartbeats.append(task)
            lease = getattr(resource, "_device_lease", None)
            if lease is not None and lease.task is not None:
                heartbeats.append(lease.task)
        for task in heartbeats:
            task.cancel()
        if heartbeats:
            await asyncio.gather(*heartbeats, return_exceptions=True)
        if getattr(self, "_agent_usage_patch_installed", False):
            try:
                uninstall_agent_usage_capture()
            except Exception:  # noqa: BLE001 - 还原失败不影响卸载
                pass
            self._agent_usage_patch_installed = False
        for job_id in self.service.background_tasks.active():
            self.service.background_tasks.update(job_id, "cancelled")
            manager = getattr(self.context, "cron_manager", None)
            if manager is not None:
                try:
                    await manager.delete_job(job_id)
                except Exception as exc:
                    logger.warning("删除后台排程失败，已标记取消并继续回收Agent：%s", exc)
        for _umo, (_task, job_id, origin) in list(self._active_agent_tasks.items()):
            if origin == "phone_background_task":
                await self._abort_running_cron_event(job_id)
        monitors = list(getattr(self, "_activity_window_tasks", ()))
        for monitor in monitors:
            monitor.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        random_task = getattr(self, "_random_task", None)
        self._random_task = None
        self._random_stop.set()
        if random_task is not None and not random_task.done():
            random_task.cancel()
            try:
                await random_task
            except asyncio.CancelledError:
                pass
        self._continuous_stop.set()
        continuous_task = getattr(self, "_continuous_task", None)
        self._continuous_task = None
        if continuous_task is not None and not continuous_task.done():
            continuous_task.cancel()
            try:
                await continuous_task
            except asyncio.CancelledError:
                pass
        task = getattr(self, "_auto_config_task", None)
        self._auto_config_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if getattr(self.service, "recordings", None) is not None:
            try:
                await self.service.recordings.close()
            except Exception as exc:
                logger.warning("手机录制收尾失败，关闭控制连接后由App清理：%s", exc)
        if hasattr(self.service, "_contents"):
            await self.service.contents.close(abnormal=True)
        self._components.cancel()
        components_task = getattr(self, "_components_task", None)
        if components_task is not None and not components_task.done():
            components_task.cancel()
            with suppress(asyncio.CancelledError):
                await components_task
        traffic_task = getattr(self, "_traffic_keepalive_task", None)
        self._traffic_keepalive_task = None
        if traffic_task is not None and not traffic_task.done():
            traffic_task.cancel()
            try:
                await traffic_task
            except asyncio.CancelledError:
                pass
        mesh_task = getattr(self, "_mesh_task", None)
        if mesh_task is not None:
            mesh_task.cancel()
            with suppress(asyncio.CancelledError):
                await mesh_task
        try:
            await self._traffic.stop()
        except Exception as exc:  # noqa: BLE001 - 抓包清理失败不影响停用
            logger.warning("抓包停止失败：%s", exc)
        try:
            if getattr(self, "_mesh", None) is not None:
                await self._mesh.stop()
        except Exception as exc:
            logger.warning("组网停止清理未完成：%s", exc)
        wireguard_task = getattr(self, "_wireguard_keepalive_task", None)
        self._wireguard_keepalive_task = None
        if wireguard_task is not None and not wireguard_task.done():
            wireguard_task.cancel()
            try:
                await wireguard_task
            except asyncio.CancelledError:
                pass
        try:
            wireguard = getattr(self, "_wireguard", None)
            if wireguard is not None:
                await wireguard.stop()
        except Exception as exc:  # noqa: BLE001 - WireGuard 停止失败不影响停用
            logger.warning("WireGuard 停止失败：%s", exc)
        try:
            await self._verification.shutdown()
        except Exception as exc:  # noqa: BLE001 - 验证状态保存失败不影响停用
            logger.warning("验证状态保存失败：%s", exc)
        cleanup_task = getattr(self, "_screenshot_cleanup_task", None)
        self._screenshot_cleanup_task = None
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        backend_task = getattr(self, "_backend_idle_task", None)
        self._backend_idle_task = None
        if backend_task is not None and not backend_task.done():
            backend_task.cancel()
            try:
                await backend_task
            except asyncio.CancelledError:
                pass
        try:
            self.service.user_priority.clear()
            self.device_gate.force_release("plugin_terminate")
        except Exception:  # noqa: BLE001 - 清理失败不影响停用
            pass
        try:
            await self._stop_live_screen("插件停用")
        except Exception as exc:  # noqa: BLE001 - 停止失败只记录
            logger.warning("停止实时屏幕缓存失败：%s", exc)
        warmup_task = getattr(self, "_scrcpy_warmup_task", None)
        self._scrcpy_warmup_task = None
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await warmup_task
        backends = getattr(self, "backends", None)
        if backends is not None:
            try:
                await backends.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止控制后端失败：%s", exc)
        try:
            await self.scheduler.close()
        finally:
            if self.adb is not None:
                try:
                    await self.adb.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败只记录
                    logger.warning("关闭 ADB 客户端失败：%s", exc)

    def _ensure_bundled_executables(self) -> None:
        """修复市场/zip 安装丢失的可执行位（adb / phone-wg 需要 +x 才能启动）。"""
        for relative, ok in ensure_bundled_executables().items():
            if not ok:
                logger.warning("无法设置插件自带二进制的可执行权限：%s", relative)

    def _resolve_adb_path(self, config: PhoneConfig) -> Path:
        """手动路径、持久化组件与系统 PATH 按优先级解析。"""
        return self._components.resolve("adb", config.adb.path)

    def _device_log(self, message: str) -> None:
        """设备层日志回调（受 debug.log_actions 控制）。"""
        try:
            enabled = PhoneConfig.from_mapping(self.raw_config).debug.log_actions
        except Exception:  # noqa: BLE001 - 配置异常时退化为 debug 日志
            enabled = False
        if enabled:
            logger.info("[手机] %s", message)
        else:
            logger.debug("[手机] %s", message)

    def _register_tools(self) -> None:
        """构造并注册全部 LLM 工具。"""
        try:
            tools = build_phone_tools(self.service)
            self.context.add_llm_tools(*tools)
            logger.info("%s 已注册 %d 个 LLM 工具", PLUGIN_NAME, len(tools))
        except Exception:  # noqa: BLE001 - 工具注册失败时命令仍可用，必须留痕
            logger.exception("注册手机 LLM 工具失败")

    def _remember_event_session(self, event: AstrMessageEvent) -> None:
        """把用户会话 UMO 记录到插件数据目录（跳过 cron / webchat 匿名会话）。"""
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            platform = ""
        if platform in {"cron", "webchat"}:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            self.service.remember_session(umo)

    def _start_screenshot_cleanup_loop(self) -> None:
        """启动截图定时清理循环（60 秒一次，读实时配置）。"""
        task = getattr(self, "_screenshot_cleanup_task", None)
        if task is not None and not task.done():
            return
        try:
            self._screenshot_cleanup_task = asyncio.create_task(
                self._screenshot_cleanup_loop(),
                name=f"{PLUGIN_NAME}_screenshot_cleanup",
            )
        except RuntimeError:  # 没有运行中的事件循环（单测直接调用）
            self._screenshot_cleanup_task = None

    async def _screenshot_cleanup_loop(self) -> None:
        """每 60 秒按配置清理一次过期截图（关闭时跳过）。"""
        while True:
            try:
                await asyncio.sleep(60)
                self.service.cleanup_screenshots()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 清理失败只记录
                logger.warning("截图自动清理循环异常：%s", exc)

    def _config_snapshot(self) -> PhoneConfig:
        """把当前内存配置解析为快照（保存前用于比较变化）。"""
        try:
            return PhoneConfig.from_mapping(
                self._plain_config(self.raw_config),
                warn=lambda message: logger.warning(message),
            )
        except Exception as exc:  # noqa: BLE001 - 配置异常时按默认快照比较
            logger.warning("读取当前配置快照失败：%s", exc)
            return PhoneConfig()

    def _persist_config(self, merged: dict[str, Any]) -> None:
        """把完整配置写回（命令/原生配置页共用）。

        优先复用 AstrBot 下发的 ``AstrBotConfig.save_config``：与框架原生保存同格式、
        同原子写、同步 save revision，避免插件直接写文件绕过框架；只有测试或框架下发的
        ``raw_config`` 是普通 dict 时才回退到原子写文件。
        """
        target = self.raw_config
        if isinstance(target, dict):
            target.clear()
            target.update(merged)
        else:  # pragma: no cover - AstrBot 正常下发 dict
            target = merged
            self.raw_config = target
        save = getattr(target, "save_config", None)
        if callable(save):
            try:
                save()
                self.service._raw_config = target
                return
            except Exception as exc:  # noqa: BLE001 - 回退到直接写文件
                logger.warning(
                    "AstrBotConfig.save_config 失败，回退直接写配置文件：%s", exc
                )
        path = self._config_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        self.service._raw_config = self.raw_config

    async def _pause_device_runtime(self) -> None:
        """ADB 配置被清空时停止设备相关后台循环（尽力而为，失败只记录）。"""
        self._continuous_enabled = False
        self._phone_enabled = False
        for attr in (
            "_backend_idle_task",
            "_scrcpy_warmup_task",
            "_traffic_keepalive_task",
            "_wireguard_keepalive_task",
            "_mesh_task",
        ):
            task = getattr(self, attr, None)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        try:
            await self.adb.clear_target()
        except Exception as exc:  # noqa: BLE001 - 清空失败只记录
            logger.warning("清空 ADB 目标失败：%s", exc)
        task = getattr(self, "_auto_config_task", None)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for stop in (
            getattr(self, "_stop_live_screen", None),
        ):
            if callable(stop):
                try:
                    await stop("ADB 配置已清空")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("停止实时屏幕失败：%s", exc)
        try:
            await self.scheduler.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止自主浏览调度失败：%s", exc)
        try:
            await self._traffic.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止抓包失败：%s", exc)
        try:
            await self._wireguard.stop()
            await self._mesh.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止 WireGuard 失败：%s", exc)
        self._random_stop.set()
        monitors = list(getattr(self, "_activity_window_tasks", ()))
        for monitor in monitors:
            monitor.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        random_task = getattr(self, "_random_task", None)
        self._random_task = None
        if random_task is not None and not random_task.done():
            random_task.cancel()
            with suppress(asyncio.CancelledError):
                await random_task
        self._continuous_stop.set()
        continuous_task = getattr(self, "_continuous_task", None)
        self._continuous_task = None
        if continuous_task is not None and not continuous_task.done():
            continuous_task.cancel()
            with suppress(asyncio.CancelledError):
                await continuous_task

    async def _apply_runtime_config(
        self, previous: PhoneConfig, notes: list[str]
    ) -> None:
        """把保存后的配置变化同步应用到已启动的组件（保存即生效）。

        AstrBot 原生插件配置页保存后框架会重载插件；插件运行中的命令（如
        ``/phone continuous``、``/phone traffic``）不能重启自己，因此按
        :mod:`core.config_apply` 的差异清单重建受影响组件：ADB 连接参数、控制/感知
        后端、持续刷手机循环、实时屏幕 watcher、随机玩手机排程与自主浏览 cron。
        其余配置项每次使用时实时读取，无需额外动作。
        """
        try:
            current = self.service.config
        except Exception as exc:  # noqa: BLE001 - 配置异常时只记录
            logger.warning("配置热应用失败（读取新配置）：%s", exc)
            notes.append("配置已保存，但读取新配置失败，请重载插件")
            return
        plan = build_apply_plan(previous, current)
        notes.extend(plan.notes)
        # App 经验语义索引随开关/向量模型变化重新构建（旧索引保留可继续兜底）。
        if (
            previous.prompt.app_memory_enabled,
            previous.prompt.app_memory_embedding_provider_id,
        ) != (
            current.prompt.app_memory_enabled,
            current.prompt.app_memory_embedding_provider_id,
        ):
            self._start_app_recall_maintenance()
            maintainer = getattr(self, "app_recall_maintainer", None)
            if maintainer is not None:
                maintainer.request_refresh()
            notes.append("App 经验召回索引已按新配置排队刷新")
        # 浏览日志上限/保护轮次是实时读取配置，保存后立即按新上限清理一次。
        await self._cleanup_diary_limits()

        if plan.mesh_changed or plan.wireguard_changed:
            # 切换先回收旧连接与代理，缺少新组件时也保持旧服务关闭。
            if plan.mesh_changed and (previous.mesh.enabled or self._mesh.running):
                await self._mesh.stop()
            if plan.wireguard_changed and (previous.wireguard.enabled or self._wireguard.running):
                await self._wireguard.stop()
        if self._components.needs_prepare(current):
            notes.append("运行组件缺失：保存配置后请重新加载插件，组件将从官方来源准备")
            return

        if plan.mesh_changed:
            await self._mesh.reconcile()
            notes.append("组网配置已应用")

        # 0) ADB 未配置（假开启）：只允许实时读取类配置生效，设备相关组件保持停止。
        if not current.adb.configured:
            if previous.adb.configured:
                await self._reset_backends(notes)
                await self._pause_device_runtime()
                notes.append(
                    "ADB 地址/端口已清空：手机后台任务已停止，重新加载插件后保持未启用状态"
                )
            return

        if current.adb.mode == "app" and previous.adb != current.adb:
            raise PhoneControlError("App 采集与连接参数变更需要保存并重新加载插件")

        if previous.adb.mode != current.adb.mode or previous.adb.app_port != current.adb.app_port or previous.adb.app_screen_source != current.adb.app_screen_source:
            raise PhoneControlError("连接方式变更需要先停止任务，再从原生配置页保存并重新加载插件")

        # 1) ADB 连接参数：断开旧目标并按新参数重配，后端一并重置。
        if plan.adb_changed:
            await self._reset_backends(notes)
            if current.adb.configured:
                try:
                    await self.adb.reconfigure(
                        adb_path=self._resolve_adb_path(current),
                        host=current.adb.host,
                        port=current.adb.port,
                        serial=current.adb.serial,
                        connect_timeout=current.adb.connect_timeout,
                        command_timeout=current.adb.command_timeout,
                    )
                    logger.info("配置热应用：ADB 目标已切换为 %s", current.adb.target)
                    if not previous.adb.configured:
                        notes.append(
                            "ADB 地址已填写：重新加载插件后启用手机能力与后台任务"
                        )
                except Exception as exc:  # noqa: BLE001 - 重配失败如实报告
                    logger.warning("配置热应用：重配 ADB 失败：%s", exc)
                    notes.append(f"ADB 连接参数已保存，但重配失败：{exc}（可重载插件）")
            else:
                logger.warning("配置热应用：ADB 地址/端口未配置，手机能力进入未启用状态")
                await self._pause_device_runtime()
                notes.append("ADB 地址/端口已清空：手机后台任务已停止，重新加载插件后保持未启用状态")

        # 2) 控制/感知后端参数变化：重置当前会话，下次操作按新配置重建。
        elif plan.backends_changed:
            await self._reset_backends(notes)

        await self._sync_activity_jobs()

        # 3) 持续刷手机开关。
        if plan.continuous_action == ACTION_START:
            self._continuous_enabled = True
            self._continuous_last_finished = 0.0
            try:
                self._start_continuous_loop()
                logger.info("配置热应用：持续刷手机已开启，后台循环已启动")
            except Exception as exc:  # noqa: BLE001 - 启动失败只记录
                logger.warning("配置热应用：启动持续刷手机失败：%s", exc)
                notes.append(f"持续刷手机已保存，但循环启动失败：{exc}")
        elif plan.continuous_action == ACTION_STOP:
            self._continuous_enabled = False
            logger.info("配置热应用：自动刷已关闭，相关任务已停止")

        # 4) 任务期间持续截图（实时屏幕 watcher）。
        if plan.live_screen_action == ACTION_STOP:
            try:
                await self._stop_live_screen("配置关闭任务期间持续截图")
                logger.info("配置热应用：实时屏幕 watcher 已停止")
            except Exception as exc:  # noqa: BLE001 - 停止失败只记录
                logger.warning("配置热应用：停止实时屏幕失败：%s", exc)
        elif plan.live_screen_action == ACTION_START:
            if self._live_task_sessions:
                try:
                    if not self.live_screen.running:
                        self.live_screen.start()
                        self._start_live_idle_monitor()
                    logger.info("配置热应用：实时屏幕 watcher 已启动")
                except Exception as exc:  # noqa: BLE001 - 启动失败只记录
                    logger.warning("配置热应用：启动实时屏幕失败：%s", exc)
            else:
                notes.append("当前没有运行中的手机任务，持续截图将在下次任务开始时启用")

        # 5) 随机玩手机：按新间隔/概率重新算下一次触发时间。
        if plan.random_changed:
            await self._restart_random_play_loop()

        # 6) 定时模式切换时同步排程与在途任务。
        if plan.scheduler_changed:
            try:
                if current.auto.enabled:
                    await self.scheduler.apply_config()
                else:
                    await self.scheduler.disable()
                    await self.scheduler.close()
                logger.info("配置热应用：自主浏览 cron 任务已按新配置同步")
            except Exception as exc:  # noqa: BLE001 - 同步失败只记录
                logger.warning("配置热应用：同步自主浏览任务失败：%s", exc)
                notes.append(f"自主浏览配置已保存，但同步 cron 任务失败：{exc}")

        # 7) 截图清理参数变化：立即按新保留时长执行一次清理。
        if (
            previous.perception.screenshot_auto_clean
            != current.perception.screenshot_auto_clean
            or previous.perception.screenshot_retention_minutes
            != current.perception.screenshot_retention_minutes
        ):
            try:
                cleaned = self.service.cleanup_screenshots(force=True)
                notes.append(
                    f"截图自动清理：已按保留 {current.perception.screenshot_retention_minutes} "
                    f"分钟清理 {cleaned.get('deleted', 0)} 个旧截图"
                )
            except Exception as exc:  # noqa: BLE001 - 清理失败只记录
                logger.warning("配置热应用：截图清理失败：%s", exc)

        # 8) 流量抓包：开关/端口/正文长度变化 → 停止后按新配置重启。
        if (
            previous.traffic.enabled != current.traffic.enabled
            or previous.traffic.provider != current.traffic.provider
            or previous.traffic.port != current.traffic.port
            or previous.traffic.transport != current.traffic.transport
            or previous.traffic.ca_mode != current.traffic.ca_mode
            or previous.traffic.mitmdump_path != current.traffic.mitmdump_path
            or (plan.mesh_changed and current.traffic.transport == "mesh")
            or previous.traffic.body_chars != current.traffic.body_chars
        ):
            try:
                if current.traffic.enabled:
                    # 配置变化后允许重新尝试（清除确定性不可用/退避状态）。
                    self._traffic.reset_unavailable()
                if self._traffic.running:
                    await self._traffic.stop()
                self._traffic._mitmdump = current.traffic.mitmdump_path
                await self._ensure_traffic_started()
                if current.traffic.enabled and self._traffic.running:
                    notes.append(
                        f"抓包：已按新配置启动（端口 {current.traffic.port}）"
                    )
                elif current.traffic.enabled:
                    status = self._traffic.status()
                    reason = str(
                        status.get("unavailable_reason")
                        or status.get("error")
                        or "未提供原因"
                    )
                    notes.append(f"抓包：启动失败（{reason}）")
                else:
                    notes.append("抓包：已停止")
            except Exception as exc:  # noqa: BLE001 - 抓包重启失败只记录
                logger.warning("配置热应用：抓包重启失败：%s", exc)
                notes.append("抓包：重启失败（详见插件日志）")

        # 9) WireGuard 组网：开关/角色/密钥/端口等变化 → 停止、启动或按新配置重启。
        if plan.wireguard_changed:
            try:
                await self._apply_wireguard_config(plan.wireguard_action, notes)
            except Exception as exc:  # noqa: BLE001 - 重启失败只记录
                logger.warning("配置热应用：WireGuard 应用失败：%s", exc)
                notes.append(f"WireGuard：配置已保存，但 helper 应用失败：{exc}")

        if plan.changed:
            logger.info("配置热应用完成：%s", "；".join(plan.notes) or "运行态已更新")

    async def _reset_backends(self, notes: list[str]) -> None:
        """停止当前控制/感知后端会话，让下次手机操作按新配置重建。"""
        manager = getattr(self, "backends", None)
        if manager is None:
            return
        try:
            await manager.stop()
        except Exception as exc:  # noqa: BLE001 - 重置失败如实报告
            logger.warning("配置热应用：重置控制/感知后端失败：%s", exc)
            notes.append(f"控制/感知后端重置失败：{exc}（可重载插件）")

    def _tool_error_targets(self, event):
        return owner_targets(
            target=self.service.config.report.target,
            owners=self.service.config.security.owner_ids,
            origin=str(getattr(event, "unified_msg_origin", "") or ""),
            last_session=self.service.last_session(),
        )

    async def _send_tool_error_report(self, target, text):
        config = self.service.config
        if not config.report.enabled:
            return False
        if config.report.target:
            allowed = target == config.report.target
        else:
            parts = target.split(":", 2)
            allowed = len(parts) == 3 and parts[1] == "FriendMessage" and parts[2] in config.security.owner_ids
        if not allowed:
            raise PermissionError("错误汇报目标已改变")
        return await self.context.send_message(
            MessageSession.from_str(target), MessageChain().message(text)
        )

    async def _apply_config_patch(
        self, patch: dict[str, Any], *, notes: list[str] | None = None
    ) -> list[str]:
        """把配置补丁持久化并热应用（命令/原生配置页共用的统一入口）。"""
        result: list[str] = notes if notes is not None else []
        saved = False
        try:
            previous = self._config_snapshot()
            current = self._plain_config(self.raw_config)
            from ..autonomy_config import normalize_autonomy_patch
            patch = normalize_autonomy_patch(current, patch)
            patch = normalize_network_patch(current, patch)
            merged = self._deep_merge(current, patch)
            PhoneConfig.from_mapping(merged)
            self._persist_config(merged)
            await self._apply_runtime_config(previous, result)
            saved = True
        except Exception as exc:  # noqa: BLE001 - 持久化失败只记录，内存仍生效
            logger.warning("保存插件配置失败：%s", exc)
            result.append(f"配置保存失败：{exc}")
        if saved:
            result.append("配置已保存并即时生效")
        for note in result:
            logger.info("配置更新：%s", note)
        return result

    def _config(self) -> PhoneConfig:
        return self.service.config

    def _config_file_path(self) -> Path:
        configured_path = getattr(self.raw_config, "config_path", None)
        if configured_path:
            return Path(configured_path)
        try:
            base = Path(get_astrbot_config_path())
        except Exception:  # noqa: BLE001 - 路径异常回退数据目录
            base = self.data_dir
        return base / f"{PLUGIN_NAME}_config.json"

    @staticmethod
    def _plain_config(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): BootstrapMixin._plain_config(item)  # type: ignore[name-defined]
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [BootstrapMixin._plain_config(item) for item in value]  # type: ignore[name-defined]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _deep_merge(base: dict, patch: dict) -> dict:
        result = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = BootstrapMixin._deep_merge(result[key], value)  # type: ignore[name-defined]
            else:
                result[key] = value
        return result
