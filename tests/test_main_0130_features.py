"""App 记忆 / 配额不限额 / 用户任务优先 / 偏好 App 中文名等容器内测试。

依赖 AstrBot（本地无 astrbot 时自动跳过）：在 AstrBot 容器内跑 ``pytest tests`` 时，
用假 Context/事件直接验证 main.py + tools.py 的接线（不连接真实设备）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import os
import sys
import time
import types
from pathlib import Path

import pytest

_astrbot = pytest.importorskip("astrbot")
if getattr(_astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot，跳过容器级用例", allow_module_level=True)


def _load_plugin_main():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_under_test_0130"
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", plugin_dir / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin_main = _load_plugin_main()
PhoneConfig = plugin_main.PhoneConfig
# main 只导入 build_phone_tools，工具类从同一模块命名空间取（避免重复导入 core.tools）
_tools_ns = plugin_main.build_phone_tools.__globals__
PhoneAppMemoryTool = _tools_ns["PhoneAppMemoryTool"]
PhoneAppRememberTool = _tools_ns["PhoneAppRememberTool"]
PhoneShareToUserTool = _tools_ns["PhoneShareToUserTool"]
PhoneTrafficTool = _tools_ns["PhoneTrafficTool"]
PhoneRecentChatTool = _tools_ns["PhoneRecentChatTool"]
PhoneScreenTool = _tools_ns["PhoneScreenTool"]


class FakeContext:
    cron_manager = None

    def __init__(self) -> None:
        self.tools = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, *args, **kwargs) -> None:
        pass


class FakeEvent:
    """最小事件：覆盖工具门禁与用户任务优先需要的事件接口。"""

    def __init__(
        self,
        *,
        umo: str = "webchat:FriendMessage:1",
        text: str = "",
        platform: str = "webchat",
        role: str = "admin",
        payload: dict | None = None,
        sender_id: str = "1",
    ) -> None:
        self.unified_msg_origin = umo
        self._text = text
        self._platform = platform
        self.role = role
        self._payload = payload or {}
        self._extras: dict = {}
        self._sender_id = sender_id

    def get_platform_name(self):
        return self._platform

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "tester"

    def get_self_id(self):
        return "bot"

    def get_message_type(self):
        return types.SimpleNamespace(name="FRIEND_MESSAGE")

    def get_session_id(self):
        return "1"

    def get_extra(self, key, default=None):
        if key == "cron_payload":
            return self._payload.get("cron_payload", default)
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra_key(self, key, default=None):
        return self.get_extra(key, default)


class FakeToolContext:
    """工具调用上下文（ContextWrapper 的最小替身）。"""

    def __init__(self, event: FakeEvent) -> None:
        self.context = types.SimpleNamespace(event=event)


def _raw_config(**overrides) -> dict:
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "127.0.0.1",
        "port": 5555,
        "path": "",
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }

    def merge(base: dict, patch: dict) -> dict:
        result = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = value
        return result

    return merge(raw, overrides)


def _make_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    plugin = plugin_main.PhoneControlPlugin(FakeContext(), _raw_config())
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


async def _cleanup(plugin) -> None:
    try:
        await plugin.terminate()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言
        pass


# ----------------------------------------------------------------------
# 1. 配额默认不限制
# ----------------------------------------------------------------------


def test_quota_defaults_unlimited_and_no_budget_block(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            config = plugin.service.config
            assert config.quota.enabled is False
            assert config.quota.max_tokens == 0
            assert config.quota.play_task_tokens == 0
            assert plugin.service.quota_error_text() is None
            # play 任务预算为 0 时不登记预算，避免「预算已完成」提前收尾
            play_event = FakeEvent(payload={"cron_payload": {
                "origin": "phone_play", "play_task": True, "target_tokens": 0,
            }})
            assert plugin.service.ensure_play_budget(play_event) is None
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 2. App 记忆工具与提示词注入
# ----------------------------------------------------------------------


def test_app_memory_tools_and_prompt_injection(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            event = FakeEvent()
            wrapper = FakeToolContext(event)
            remember = PhoneAppRememberTool(service=plugin.service)
            text = await remember.call(
                wrapper,
                app="B站",
                note="首页底部导航依次是 首页/动态/会员购/我的；视频详情页返回用 back",
                page="首页",
                steps=["底部 tab 切首页", "点卡片进详情", "back 返回"],
            )
            assert "已记住" in text
            lookup = PhoneAppMemoryTool(service=plugin.service)
            found = await lookup.call(wrapper, app="哔哩哔哩", page="")
            assert "哔哩哔哩" in found or "B站" in found
            assert "首页" in found
            # 注入提示词（跳过 scrcpy 预热与计时细节）
            monkeypatch.setattr(plugin, "_maybe_warm_scrcpy", lambda *a, **k: None)
            monkeypatch.setattr(plugin, "_begin_llm_metric", lambda *a, **k: None)
            monkeypatch.setattr(plugin, "_record_request_tokens", lambda *a, **k: None)
            # 注入判定按请求文本区分手机请求与普通对话：手机请求才带手机活动记忆
            req = types.SimpleNamespace(
                system_prompt="",
                prompt="打开B站看视频",
                contexts=[],
                extra_user_content_parts=[],
            )
            await plugin.inject_phone_guidance(event, req)
            injected = "\n".join(
                str(getattr(part, "text", ""))
                for part in req.extra_user_content_parts
            )
            assert req.system_prompt == ""
            assert "【App 使用经验】" in injected
            assert "【手机操控能力】" in injected
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_app_memory_can_be_disabled(tmp_path, monkeypatch):
    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(),
            _raw_config(prompt={"app_memory_enabled": False, "app_memory_limit": 0}),
        )
        monkeypatch.setattr(plugin, "_config_file_path", lambda: tmp_path / "c.json")
        try:
            plugin.app_memory.remember(app="抖音", note="底部导航：首页/朋友/消息/我")
            monkeypatch.setattr(plugin, "_maybe_warm_scrcpy", lambda *a, **k: None)
            monkeypatch.setattr(plugin, "_begin_llm_metric", lambda *a, **k: None)
            monkeypatch.setattr(plugin, "_record_request_tokens", lambda *a, **k: None)
            event = FakeEvent(text="随便聊聊")
            req = types.SimpleNamespace(
                system_prompt="", prompt="", contexts=[], extra_user_content_parts=[]
            )
            await plugin.inject_phone_guidance(event, req)
            injected = "\n".join(
                str(getattr(part, "text", ""))
                for part in req.extra_user_content_parts
            )
            assert req.system_prompt == ""
            assert "【App 使用经验】" not in injected
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 3. 用户任务优先（抢占 + 自动恢复）
# ----------------------------------------------------------------------


def test_user_priority_preempts_and_resumes_continuous(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            plugin._continuous_enabled = True
            preempted: list[int] = []
            resumed: list[bool] = []

            async def fake_preempt() -> int:
                preempted.append(1)
                return 2

            def fake_start_continuous() -> None:
                resumed.append(True)

            monkeypatch.setattr(plugin, "_preempt_autonomous_tasks", fake_preempt)
            monkeypatch.setattr(plugin, "_start_continuous_loop", fake_start_continuous)
            event = FakeEvent(text="帮我用手机打开B站看两条视频")
            assert plugin._looks_like_user_phone_request(event) is True

            await plugin._enter_user_priority(event)
            assert plugin.service.user_priority.active is True
            assert preempted == [1]

            # 用户任务期间自刷工具的调用让路（cron 任务 origin）
            cron_event = FakeEvent(
                platform="cron",
                payload={"cron_payload": {"origin": "phone_continuous", "play_task": True}},
            )
            assert plugin.service.should_yield_to_user(cron_event) is True
            assert plugin.service.should_yield_to_user(event) is False

            plugin._exit_user_priority(event)
            assert plugin.service.user_priority.active is False
            assert plugin._continuous_last_finished == 0.0
            assert resumed == [True]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_user_priority_ignores_plain_chat_and_respects_switch(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            assert plugin._looks_like_user_phone_request(FakeEvent(text="今天天气怎么样")) is False
            assert plugin._looks_like_user_phone_request(FakeEvent(text="/phone status")) is True
            # 关闭用户任务优先后不抢占
            plugin.raw_config["auto"]["user_priority"] = False
            assert plugin._looks_like_user_phone_request(FakeEvent(text="打开B站")) is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 4. 持续刷偏好 App 中文名解析
# ----------------------------------------------------------------------


def test_continuous_preferred_apps_chinese_resolution(tmp_path, monkeypatch):
    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(), _raw_config(auto={"continuous_apps": "B站,抖音,没装的应用"})
        )
        monkeypatch.setattr(plugin, "_config_file_path", lambda: tmp_path / "c.json")

        async def fake_shell(command, **kwargs):
            assert "pm list packages" in command
            return "package:tv.danmaku.bili\npackage:com.tencent.mm\n"

        monkeypatch.setattr(plugin.adb, "shell", fake_shell)
        try:
            packages = await plugin.service.installed_packages()
            assert "tv.danmaku.bili" in packages
            status = plugin._continuous_status_text()
            assert "哔哩哔哩（tv.danmaku.bili，已安装）" in status
            assert "抖音（未安装，跳过）" in status
            assert "没装的应用" in status
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 5. 记忆/学习同步到 MemoryCompanion（选中存储去向时）
# ----------------------------------------------------------------------


class FakeMemoryBridge:
    """假 MemoryCompanion 桥接：记录 record_external_memory 调用。"""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []
        self.bot_actions: list[str] = []

    async def record_external_memory(self, **kwargs):
        self.calls.append(kwargs)
        if self.ok:
            return {"ok": True, "memory_id": "mem-1", "state": "stored"}
        return {"ok": False, "error_code": "external_records_disabled"}

    async def record_bot_action(self, *, content: str, **kwargs) -> str:
        self.bot_actions.append(content)
        return "action-1"


def _make_plugin_with_memory_backend(tmp_path, monkeypatch, backend="companion"):
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    plugin = plugin_main.PhoneControlPlugin(
        FakeContext(),
        _raw_config(memory={"backend": backend, "companion_user_id": ""}),
    )
    monkeypatch.setattr(plugin, "_config_file_path", lambda: tmp_path / "c.json")
    bridge = FakeMemoryBridge()
    monkeypatch.setattr(plugin, "_get_memory_companion_bridge", lambda: bridge)
    return plugin, bridge


def test_phone_learn_syncs_to_memory_companion(tmp_path, monkeypatch):
    """phone_learn 在 memory.backend=both 时同时写入 MemoryCompanion。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            event = FakeEvent(text="学习一下")
            result = await learn_tool.call(
                FakeToolContext(event),
                content="B站转正答题需要 60 分",
                source="哔哩哔哩",
                tags=["B站"],
            )
            assert "已计入用户信息（MemoryCompanion）" in result
            assert len(bridge.calls) == 1
            call = bridge.calls[0]
            assert call["user_id"] == "1"  # 用户会话用发送者 ID
            assert call["memory_type"] == "phone_learning"
            assert "【手机学习】" in call["content"]
            assert call["source_plugin"] == plugin_main.PLUGIN_NAME
            # 本地也写入（both）
            assert plugin.service.learning.recent(1)
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_app_experience_is_local_only(tmp_path, monkeypatch):
    """App 经验只写插件本地档案，学习记录的全局记忆配置独立生效。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        try:
            tool = PhoneAppRememberTool(service=plugin.service)
            event = FakeEvent(text="记住B站布局")
            result = await tool.call(
                FakeToolContext(event),
                app="B站",
                page="首页",
                note="底部导航五项",
                steps=["打开B站"],
            )
            assert "保存在插件本地" in result
            assert bridge.calls == []
            assert plugin.app_memory.lookup("B站", limit=5)
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_memory_sync_failure_reports_and_keeps_local(tmp_path, monkeypatch):
    """记忆插件拒绝写入时如实提示，学习记录仍保存在本地。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        bridge.ok = False
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            event = FakeEvent(text="学习一下")
            result = await learn_tool.call(
                FakeToolContext(event), content="记住这条", source="B站", tags=[]
            )
            assert "记忆插件同步失败" in result
            assert "已保存在插件本地" in result
            assert plugin.service.learning.recent(1)
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_memory_sync_skipped_when_backend_local(tmp_path, monkeypatch):
    """memory.backend=local 时不调用记忆插件桥接。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(
            tmp_path, monkeypatch, backend="local"
        )
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            event = FakeEvent(text="学习一下")
            result = await learn_tool.call(
                FakeToolContext(event), content="本地记一条", source="B站", tags=[]
            )
            assert "MemoryCompanion" not in result
            assert bridge.calls == []
            assert plugin.service.learning.recent(1)
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


class FakeLivingMemoryEngine:
    """假 LivingMemory 记忆引擎：记录 add_memory 调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def add_memory(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return 101


class FakeLivingMemoryPlugin:
    def __init__(self) -> None:
        self.engine = FakeLivingMemoryEngine()
        self.initializer = types.SimpleNamespace(memory_engine=self.engine)


def test_phone_learn_syncs_to_livingmemory(tmp_path, monkeypatch):
    """memory.backend=livingmemory 时写入 LivingMemory 记忆引擎。"""

    async def scenario() -> None:
        plugin, _bridge = _make_plugin_with_memory_backend(
            tmp_path, monkeypatch, backend="livingmemory"
        )
        lm = FakeLivingMemoryPlugin()
        lm.config_manager = {}
        scope_module = types.ModuleType("test_livingmemory.core.memory_scope")
        scope_module.is_event_memory_allowed = lambda config, event: True
        scope_module.resolve_memory_scope = lambda config, event: "livingmemory:user:webchat:1"
        monkeypatch.setitem(sys.modules, scope_module.__name__, scope_module)
        monkeypatch.setattr(FakeLivingMemoryPlugin, "__module__", "test_livingmemory.main")
        monkeypatch.setattr(plugin, "_get_livingmemory_plugin", lambda: lm)
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            event = FakeEvent(text="学习一下")
            result = await learn_tool.call(
                FakeToolContext(event),
                content="LivingMemory 同步测试",
                source="B站",
                tags=["测试"],
            )
            assert "已计入用户信息（LivingMemory）" in result
            assert len(lm.engine.calls) == 1
            call = lm.engine.calls[0]
            assert "LivingMemory 同步测试" in call["content"]
            assert call["session_id"] == "livingmemory:user:webchat:1"
            assert call["metadata"]["phone_original_session_id"] == event.unified_msg_origin
            assert call["metadata"]["source_plugin"] == plugin_main.PLUGIN_NAME
            assert call["metadata"]["memory_type"] == "GENERAL"
            assert call["metadata"]["phone_memory_type"] == "phone_learning"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_bot_action_memory_optional_for_autonomous_play(tmp_path, monkeypatch):
    """开启 memory.bot_action_enabled 后，自行刷手机的学习记忆额外计入个人记忆-行动。"""

    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(),
            _raw_config(
                memory={
                    "backend": "companion",
                    "bot_action_enabled": True,
                    "companion_user_id": "75782383",
                },
            ),
        )
        monkeypatch.setattr(plugin, "_config_file_path", lambda: tmp_path / "c.json")
        bridge = FakeMemoryBridge()
        monkeypatch.setattr(plugin, "_get_memory_companion_bridge", lambda: bridge)
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            cron_event = FakeEvent(
                platform="cron",
                payload={
                    "cron_payload": {
                        "origin": "phone_continuous",
                        "play_task": True,
                    }
                },
            )
            result = await learn_tool.call(
                FakeToolContext(cron_event),
                content="自行刷手机学到的内容",
                source="B站",
                tags=[],
            )
            assert "已计入个人记忆-行动（MemoryCompanion）" in result
            assert bridge.bot_actions
            assert bridge.bot_actions[0].startswith("【自行刷手机】")
            assert "自行刷手机学到的内容" in bridge.bot_actions[0]
            # 自行唤醒学习不计入用户记忆（不调用用户侧写入）。
            assert bridge.calls == []
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_bot_action_memory_disabled_by_default(tmp_path, monkeypatch):
    """默认 bot_action_enabled=false 时不计入个人记忆-行动。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        try:
            assert plugin.service.config.memory.bot_action_enabled is False
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            cron_event = FakeEvent(
                platform="cron",
                payload={"cron_payload": {"origin": "phone_continuous", "play_task": True}},
            )
            result = await learn_tool.call(
                FakeToolContext(cron_event), content="默认不计", source="B站", tags=[]
            )
            assert bridge.bot_actions == []
            assert bridge.calls == []
            assert "自行唤醒学习不计入用户记忆" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def _learn_event(payload: dict | None = None, *, sender_id: str = "1", platform="cron"):
    return FakeEvent(
        platform=platform,
        payload=payload or {},
        sender_id=sender_id,
    )


def test_user_session_learning_counts_into_user_memory(tmp_path, monkeypatch):
    """用户指定使用手机的学习计入用户信息（发送者 ID）。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            event = _learn_event(platform="webchat")
            result = await learn_tool.call(
                FakeToolContext(event), content="用户让我刷到的内容", source="B站", tags=[]
            )
            assert "已计入用户信息（MemoryCompanion）" in result
            assert bridge.calls and bridge.calls[0]["user_id"] == "1"
            record = plugin.service.learning.recent(1)[0]
            assert record.get("attribution") == "user"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_user_background_task_learning_counts_for_requester(tmp_path, monkeypatch):
    """用户发起的后台任务：计入创建者（requested_by）的用户信息。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            event = FakeEvent(
                platform="cron",
                sender_id="",
                payload={
                    "cron_payload": {
                        "origin": "phone_background_task",
                        "requested_by": "75782383",
                    }
                },
            )
            result = await learn_tool.call(
                FakeToolContext(event), content="给用户找的攻略", source="B站", tags=[]
            )
            assert "已计入用户信息（MemoryCompanion）" in result
            assert bridge.calls and bridge.calls[0]["user_id"] == "75782383"
            record = plugin.service.learning.recent(1)[0]
            assert record.get("attribution") == "user"
            assert record.get("requested_by") == "75782383"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_autonomous_origins_never_write_user_memory(tmp_path, monkeypatch):
    """随机玩/持续刷/定时自主浏览的学习都不计入用户记忆。"""

    async def scenario() -> None:
        plugin, bridge = _make_plugin_with_memory_backend(tmp_path, monkeypatch)
        try:
            learn_tool = _tools_ns["PhoneLearnTool"](service=plugin.service)
            for origin in (
                "phone_random_play",
                "phone_continuous",
                plugin_main.PLUGIN_NAME,  # 定时自主浏览
            ):
                event = FakeEvent(
                    platform="cron",
                    payload={"cron_payload": {"origin": origin, "play_task": True}},
                )
                result = await learn_tool.call(
                    FakeToolContext(event),
                    content=f"自行唤醒学习-{origin}",
                    source="B站",
                    tags=[],
                )
                assert "自行唤醒学习不计入用户记忆" in result
            assert bridge.calls == []
            assert bridge.bot_actions == []
            records = plugin.service.learning.recent(3)
            assert all(record.get("attribution") == "autonomous" for record in records)
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 6. 截图自动清理
# ----------------------------------------------------------------------


def test_service_cleanup_screenshots_removes_expired(tmp_path, monkeypatch):
    """PhoneService.cleanup_screenshots 按保留分钟数清理旧截图、保留新截图。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            screenshots = plugin.service.screenshots_dir
            screenshots.mkdir(parents=True, exist_ok=True)
            now = time.time()
            old = screenshots / "shot-old.jpg"
            old.write_bytes(b"old" * 10)
            os.utime(old, (now - 600, now - 600))
            fresh = screenshots / "shot-fresh.jpg"
            fresh.write_bytes(b"new")
            result = plugin.service.cleanup_screenshots(force=True)
            assert result["enabled"] is True
            assert result["deleted"] == 1
            assert not old.exists()
            assert fresh.exists()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_service_cleanup_respects_retention_config(tmp_path, monkeypatch):
    """保留分钟数可配置：默认 3 分钟不删 2 分钟前的截图，改成 1 分钟后删除。"""

    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(),
            _raw_config(perception={"screenshot_retention_minutes": 3}),
        )
        monkeypatch.setattr(plugin, "_config_file_path", lambda: tmp_path / "c.json")
        try:
            screenshots = plugin.service.screenshots_dir
            screenshots.mkdir(parents=True, exist_ok=True)
            now = time.time()
            file = screenshots / "shot-in-between.jpg"
            file.write_bytes(b"data")
            os.utime(file, (now - 120, now - 120))
            # 默认 3 分钟：保留
            assert plugin.service.cleanup_screenshots(force=True)["deleted"] == 0
            # 改配置为 1 分钟：清掉
            plugin.raw_config["perception"]["screenshot_retention_minutes"] = 1
            result = plugin.service.cleanup_screenshots(force=True)
            assert result["deleted"] == 1
            assert not file.exists()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_service_cleanup_disabled_keeps_files(tmp_path, monkeypatch):
    """关闭自动清理后 force 清理也不删文件（失败/关闭时不误删）。"""

    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(),
            _raw_config(perception={"screenshot_auto_clean": False}),
        )
        monkeypatch.setattr(plugin, "_config_file_path", lambda: tmp_path / "c.json")
        try:
            screenshots = plugin.service.screenshots_dir
            screenshots.mkdir(parents=True, exist_ok=True)
            old = screenshots / "shot-old.jpg"
            old.write_bytes(b"old")
            os.utime(old, (time.time() - 600, time.time() - 600))
            result = plugin.service.cleanup_screenshots(force=True)
            assert result["deleted"] == 0
            assert old.exists()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 7. 重启后遗留持续刷轮次的恢复（启动后不自动玩手机的回归）
# ----------------------------------------------------------------------


def test_continuous_recovers_stale_round_job(tmp_path, monkeypatch):
    """AstrBot 重启后遗留的 running 持续刷任务应被清理，否则循环永远等一个不存在的轮次。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            plugin._continuous_active_job_id = ""
            deleted: list[str] = []

            class FakeJob:
                job_id = "stale-continuous-job"
                name = "phone-continuous"
                status = "running"
                created_at = None
                next_run_time = None
                description = "持续刷手机（一轮）"
                payload = {"origin": "phone_continuous", "play_task": True}

            class FakeCronManager:
                async def list_jobs(self, *args, **kwargs):
                    return [FakeJob()]

                async def delete_job(self, job_id):
                    deleted.append(job_id)

            recovered = await plugin._recover_stale_continuous_job(FakeCronManager())
            assert recovered == "stale-continuous-job"
            assert deleted == ["stale-continuous-job"]
            assert plugin._continuous_active_job_id == ""

            # 没有遗留任务时不删除任何东西
            class EmptyCronManager:
                async def list_jobs(self, *args, **kwargs):
                    return []

                async def delete_job(self, job_id):  # pragma: no cover - 不应被调用
                    raise AssertionError("不应删除任务")

            assert await plugin._recover_stale_continuous_job(EmptyCronManager()) == ""
            assert await plugin._recover_stale_continuous_job(None) == ""
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 8. 主动分享：目标由 AI 根据最近聊天判断 + 白天免打扰
# ----------------------------------------------------------------------


class FakeStarContext:
    """最小星上下文：记录 send_message 调用的目标会话。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, session, chain) -> bool:
        self.sent.append(str(session))
        return True


class FakeShareToolContext(FakeToolContext):
    """带星上下文的工具上下文（供 phone_share_to_user 发送）。"""

    def __init__(self, event: FakeEvent, star_context=None) -> None:
        super().__init__(event)
        self.context.context = star_context


def _seed_share_chat(plugin) -> None:
    memory = plugin.service.chat_memory
    now = time.time()
    memory.record(
        umo="webchat:GroupMessage:100",
        text="群里在聊原神新活动",
        session_id="g",
        platform="webchat",
        sender_id="9",
        sender_name="群友A",
        is_group=True,
        ts=now - 120,
    )
    memory.record(
        umo="webchat:FriendMessage:456",
        text="最近在追番",
        session_id="u",
        platform="webchat",
        sender_id="456",
        sender_name="小明",
        is_group=False,
        ts=now - 300,
    )
    memory.record(
        umo="webchat:FriendMessage:789",
        text="很久以前聊过",
        session_id="u2",
        platform="webchat",
        sender_id="789",
        sender_name="旧友",
        is_group=False,
        ts=now - 10 * 86400,
    )


def test_share_target_validation_recent_user_only(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            service = plugin.service
            assert service.validate_share_target("webchat:GroupMessage:100") == ""
            assert service.validate_share_target("webchat:FriendMessage:456") == ""
            stale = service.validate_share_target("webchat:FriendMessage:789")
            assert "只分享给最近有交流的用户" in stale
            unknown = service.validate_share_target("webchat:FriendMessage:999")
            assert "没有最近的聊天记录" in unknown
            assert "不是可识别的群聊/私聊会话" in service.validate_share_target("cron:active_agent:x")
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_autonomous_requires_target(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            monkeypatch.setattr(
                plugin.service,
                "share_allowed_now",
                lambda: (True, "08:00–22:00（中国时区）"),
            )
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(
                platform="cron",
                payload={"cron_payload": {"origin": "phone_continuous", "play_task": True}},
            )
            result = await tool.call(FakeToolContext(event), text="分享点东西")
            assert "必须自己选 target" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_blocks_quiet_hours(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            monkeypatch.setattr(
                plugin.service,
                "share_allowed_now",
                lambda: (False, "08:00–22:00（中国时区）"),
            )
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(
                platform="cron",
                payload={"cron_payload": {"origin": "phone_continuous", "play_task": True}},
            )
            result = await tool.call(
                FakeToolContext(event), text="分享点东西", target="webchat:GroupMessage:100"
            )
            assert "免打扰时段" in result
            assert "保持静默收尾" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_sends_to_group_and_recent_user(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            monkeypatch.setattr(
                plugin.service,
                "share_allowed_now",
                lambda: (True, "08:00–22:00（中国时区）"),
            )
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(
                platform="cron",
                payload={"cron_payload": {"origin": "phone_continuous", "play_task": True}},
            )
            # 自刷分享必须依据聊天记忆：先调用过 phone_recent_chat（工具会打标）
            event.set_extra("phone_chat_memory_checked", True)
            star = FakeStarContext()
            context = FakeShareToolContext(event, star)
            result = await tool.call(
                context,
                text="群内容",
                link="https://example.com/group-item",
                target="webchat:GroupMessage:100",
            )
            assert "已分享" in result
            assert star.sent == ["webchat:GroupMessage:100"]
            result = await tool.call(
                context,
                text="私聊内容",
                link="https://example.com/friend-item",
                target="webchat:FriendMessage:456",
            )
            assert "已分享" in result
            assert star.sent[-1] == "webchat:FriendMessage:456"
            # 过期私聊被拒绝，不会发送
            result = await tool.call(
                context, text="旧人", link="https://example.com/old", target="webchat:FriendMessage:789"
            )
            assert "只分享给最近有交流的用户" in result
            assert len(star.sent) == 2
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_autonomous_requires_chat_memory(tmp_path, monkeypatch):
    """自刷任务分享前必须先查聊天记忆（未查则拒绝，不允许无依据目标）。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            monkeypatch.setattr(
                plugin.service,
                "share_allowed_now",
                lambda: (True, "08:00–22:00（中国时区）"),
            )
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(
                platform="cron",
                payload={"cron_payload": {"origin": "phone_continuous", "play_task": True}},
            )
            star = FakeStarContext()
            result = await tool.call(
                FakeShareToolContext(event, star),
                text="内容",
                link="https://example.com/item",
                target="webchat:GroupMessage:100",
            )
            assert "phone_recent_chat" in result
            assert "聊天记忆" in result
            assert star.sent == []
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_user_task_defaults_current_session(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(umo="webchat:FriendMessage:1", text="分享给我")
            star = FakeStarContext()
            result = await tool.call(
                FakeShareToolContext(event, star),
                text="用户直接任务分享",
                link="https://example.com/item",
            )
            assert "已分享" in result
            assert star.sent == ["webchat:FriendMessage:1"]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_text_only_auto_screenshot(tmp_path, monkeypatch):
    """纯文字分享不允许：没有链接时自动补帖子顶部截图（截图失败才报错）。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            shot = tmp_path / "share-shot.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\n")

            async def fake_capture(*args, **kwargs):
                return b"\x89PNG\r\n\x1a\n", shot, {}

            monkeypatch.setattr(plugin.service, "capture_post_top", fake_capture)
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(umo="webchat:FriendMessage:1", text="分享给我")
            star = FakeStarContext()
            result = await tool.call(
                FakeShareToolContext(event, star), text="只有文字也应该附截图"
            )
            assert "已分享" in result
            assert "截图" in result
            assert star.sent == ["webchat:FriendMessage:1"]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_post_share_ignores_middle_screenshot_and_refuses_unconfirmed_top(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            middle = tmp_path / "middle.png"
            middle.write_bytes(b"middle")
            calls = []
            async def fail_top():
                calls.append("top")
                raise plugin_main.PhoneControlError("未确认帖子顶部")
            monkeypatch.setattr(plugin.service, "capture_post_top", fail_top)
            tool = PhoneShareToUserTool(service=plugin.service)
            star = FakeStarContext()
            event = FakeEvent(umo="webchat:FriendMessage:1")
            result = await tool.call(FakeShareToolContext(event,star), text="帖子", screenshot_path=str(middle))
            assert "未确认帖子顶部" in result
            assert calls==["top"] and star.sent==[]
        finally:await _cleanup(plugin)
    asyncio.run(scenario())


def test_video_share_preserves_existing_current_frame(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            frame = tmp_path / "video.png"
            frame.write_bytes(b"video")
            async def forbidden_top():raise AssertionError("video must not scroll")
            monkeypatch.setattr(plugin.service, "capture_post_top", forbidden_top)
            tool = PhoneShareToUserTool(service=plugin.service)
            star = FakeStarContext()
            result = await tool.call(FakeShareToolContext(FakeEvent(umo="webchat:FriendMessage:1"),star),content_type="video",screenshot_path=str(frame))
            assert "已分享" in result and star.sent==["webchat:FriendMessage:1"]
        finally:await _cleanup(plugin)
    asyncio.run(scenario())


def test_recent_chat_tool_marks_chat_memory_checked(tmp_path, monkeypatch):
    """phone_recent_chat 成功后打标，供分享工具校验「先看聊天再选目标」。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            tool = PhoneRecentChatTool(service=plugin.service)
            event = FakeEvent(umo="webchat:GroupMessage:100")
            await tool.call(FakeToolContext(event))
            assert event.get_extra("phone_chat_memory_checked") is True
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_recent_chat_tool_filters_stale_users(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            _seed_share_chat(plugin)
            tool = PhoneRecentChatTool(service=plugin.service)
            result = await tool.call(
                FakeToolContext(FakeEvent(umo="webchat:GroupMessage:100"))
            )
            assert "webchat:GroupMessage:100" in result
            assert "webchat:FriendMessage:456" in result
            assert "webchat:FriendMessage:789" not in result
            assert "分享时段" in result
            assert "私聊只选最近有交流的用户" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_policy_hint_lets_ai_choose(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            monkeypatch.setattr(
                plugin.service,
                "share_allowed_now",
                lambda: (True, "08:00–22:00（中国时区）"),
            )
            hint = plugin.service.share_policy_hint()
            assert "phone_recent_chat" in hint
            assert "不要使用固定目标" in hint
            assert "最近有交流" in hint
            assert "08:00–22:00" in hint
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 9. 流量抓包：phone_traffic 工具渲染与未就绪降级
# ----------------------------------------------------------------------


def test_phone_traffic_tool_renders_decrypted_flows(tmp_path, monkeypatch):
    """抓包运行中：phone_traffic 返回接口 URL/状态/JSON 片段与页面推断。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            now = time.time()

            class FakeCapture:
                def status(self):
                    return {
                        "running": True,
                        "port": 18080,
                        "flows": 2,
                        "error": "",
                    }

                def status_text(self):
                    return "抓包：已开启（端口 18080，已记录 2 条流量）"

                def recent(self, *, limit=20, since_seconds=0):
                    return [
                        {
                            "ts": now - 5,
                            "method": "GET",
                            "url": "https://api.xiaoheihe.cn/bbs/app/feed/list?page=1&hkey=SECRET",
                            "status": 200,
                            "ctype": "application/json",
                            "reqbody": "",
                            "body": '{"result":{"links":[{"title":"测试帖子"}]}}',
                        }
                    ]

            async def fake_foreground():
                return "com.max.xiaoheihe", ".MainActivity"

            plugin.service.traffic = FakeCapture()
            monkeypatch.setattr(
                plugin.service.actions, "foreground", fake_foreground
            )
            tool = PhoneTrafficTool(service=plugin.service)
            result = await tool.call(FakeToolContext(FakeEvent(text="看抓包")))
            assert "抓包：已开启" in result
            assert "当前页面抓包内容" in result
            assert "当前前台：小黑盒（com.max.xiaoheihe）" in result
            assert "小黑盒·社区信息流" in result
            assert "测试帖子" in result
            assert "hkey=***" in result
            assert "SECRET" not in result
            assert "多模态截图" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_phone_screen_includes_current_page_traffic(tmp_path, monkeypatch):
    """屏幕读取自动合并当前前台 App 的抓包内容，无需再单独调 phone_traffic。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            now = time.time()

            class FakeCapture:
                def status(self):
                    return {"running": True, "port": 18080, "flows": 1, "error": ""}

                def status_text(self):
                    return "抓包：已开启（端口 18080，已记录 1 条流量）"

                def recent(self, *, limit=20, since_seconds=0):
                    return [
                        {
                            "ts": now - 3,
                            "method": "GET",
                            "url": "https://api.xiaoheihe.cn/bbs/app/feed?page=1",
                            "status": 200,
                            "ctype": "application/json",
                            "reqbody": "",
                            "body": '{"result":{"links":[{"title":"当前页面帖子"}]}}',
                        }
                    ]

            plugin.service.traffic = FakeCapture()

            async def fake_foreground():
                return "com.max.xiaoheihe", ".MainActivity"

            async def fake_screen(max_nodes=0):
                from core.models import ScreenSnapshot
                return "UI 树：当前页面", ScreenSnapshot(package="com.max.xiaoheihe")

            monkeypatch.setattr(
                plugin.service.actions, "foreground", fake_foreground
            )
            monkeypatch.setattr(plugin.service, "screen", fake_screen)
            # 模拟刚刚有设备写动作：只展示动作之后的流量
            plugin.service._page_changed_at = now - 10
            tool = PhoneScreenTool(service=plugin.service)
            result = await tool.call(
                FakeToolContext(FakeEvent(text="看当前页面"))
            )
            assert "UI 树：当前页面" in result
            assert "【抓包页面内容】" in result
            assert "当前页面帖子" in result
            assert "截图/OCR" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_phone_traffic_tool_unavailable_falls_back(tmp_path, monkeypatch):
    """抓包未就绪：工具返回可操作错误并提示回退 UI/截图。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            class FakeCapture:
                def status(self):
                    return {"running": False, "error": "设备无法获取 root"}

            plugin.service.traffic = FakeCapture()
            tool = PhoneTrafficTool(service=plugin.service)
            result = await tool.call(FakeToolContext(FakeEvent(text="看抓包")))
            assert "抓包未开启或未就绪" in result
            assert "设备无法获取 root" in result
            assert "phone_screen" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_share_tool_supports_link_and_falls_back_to_screenshot(tmp_path, monkeypatch):
    """分享优先带链接；非法链接被忽略后自动补截图，仍不可纯文字。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            shot = tmp_path / "bad-link-shot.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\n")

            async def fake_capture(*args, **kwargs):
                return b"\x89PNG\r\n\x1a\n", shot, {}

            monkeypatch.setattr(plugin.service, "capture_post_top", fake_capture)
            tool = PhoneShareToUserTool(service=plugin.service)
            event = FakeEvent(umo="webchat:FriendMessage:1", text="分享给我")
            star = FakeStarContext()
            context = FakeShareToolContext(event, star)
            result = await tool.call(
                context,
                text="这个小黑盒帖子有意思",
                link="https://www.xiaoheihe.cn/app/bbs/link/123",
            )
            assert "已分享" in result
            assert "链接（手动提供）" in result
            assert star.sent == ["webchat:FriendMessage:1"]
            # 非法链接被忽略，自动补截图兜底（必须带链接或截图）
            result = await tool.call(
                context, text="x", link="not-a-link"
            )
            assert "已分享" in result
            assert "截图" in result
            assert "链接不是有效" in result or "忽略" in result
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 10. 验证会话：有限自动尝试 + 人工协助暂停/恢复/取消
# ----------------------------------------------------------------------


class FakeBotContext(FakeContext):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def send_message(self, session, chain) -> bool:
        self.sent.append(str(session))
        return True


def _verification_plugin(tmp_path, monkeypatch, **overrides):
    plugin = _make_plugin(tmp_path, monkeypatch)
    plugin.context = FakeBotContext()
    return plugin


def test_verification_attempts_and_manual_pause(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _verification_plugin(tmp_path, monkeypatch)
        try:
            controller = plugin._verification
            event = FakeEvent(text="验证码")
            ok, reason = await controller.reserve_attempt(event, kind="solver")
            assert ok and reason == ""
            state = controller.state
            assert state["attempts"] == 1
            assert state["status"] == "attempting"
            # 观察中：只读工具放行，普通写操作统一拦截
            assert controller.guard_tool("phone_screen", write=False) == ""
            assert controller.guard_tool("phone_solve_verification", write=False) == ""
            blocked = controller.guard_tool("phone_tap", write=True)
            assert "普通写操作已统一交给验证控制器" in blocked
            await controller.finish_attempt(event, passed=False)
            assert controller.state["status"] == "observing"
            for _ in range(2):
                ok, _reason = await controller.reserve_attempt(event, kind="bypass")
                assert ok
                await controller.finish_attempt(event, passed=False)
            assert controller.paused is True
            assert controller.state["status"] == "awaiting_user"
            await asyncio.sleep(0.05)
            assert plugin.context.sent  # 已发送（或尝试发送）求助通知
            paused_blocked = controller.guard_tool("phone_solve_verification", write=False)
            assert "不能继续自动尝试" in paused_blocked
            assert "手机自动操作已暂停" in controller.guard_tool("phone_tap", write=True)
            # 用户完成验证：resume 先观察，页面无验证关键词则通过
            async def fake_screen(max_nodes=0):
                return ("首页推荐视频列表", None)

            monkeypatch.setattr(plugin.service, "screen", fake_screen)
            result = await controller.resume(controller.state["id"], event)
            assert "已确认通过" in result
            assert controller.paused is False
            assert not controller.active
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_verification_cancel_and_restart_recovery(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _verification_plugin(tmp_path, monkeypatch)
        controller = plugin._verification
        event = FakeEvent(text="验证码")
        for _ in range(3):
            ok, _ = await controller.reserve_attempt(event, kind="solver")
            assert ok
            await controller.finish_attempt(event, passed=False)
        assert controller.paused is True
        session_id = controller.state["id"]
        # 模拟插件重启：新实例加载同一持久化状态 → 保持暂停
        await plugin.terminate()
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin2 = plugin_main.PhoneControlPlugin(FakeContext(), _raw_config())
        plugin2.context = FakeBotContext()
        monkeypatch.setattr(
            plugin2, "_config_file_path", lambda: tmp_path / "plugin_config.json"
        )
        try:
            await plugin2._verification.startup()
            assert plugin2._verification.paused is True
            assert plugin2._verification.state["id"] == session_id
            result = await plugin2._verification.cancel(session_id)
            assert "已取消验证等待" in result
            assert plugin2._verification.paused is False
        finally:
            await _cleanup(plugin2)

    asyncio.run(scenario())


def test_verification_zero_attempts_direct_manual(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(), _raw_config(verification={"max_attempts": 0})
        )
        plugin.context = FakeBotContext()
        monkeypatch.setattr(
            plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
        )
        try:
            controller = plugin._verification
            ok, reason = await controller.reserve_attempt(
                FakeEvent(text="验证码"), kind="solver"
            )
            assert not ok
            assert controller.paused is True
            assert controller.state["attempts"] == 0
            assert "0/0" in reason
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_verification_total_timeout_watchdog(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _verification_plugin(tmp_path, monkeypatch)
        try:
            controller = plugin._verification
            event = FakeEvent(text="验证码")
            ok, _ = await controller.reserve_attempt(event, kind="solver")
            assert ok
            # 把开始时间拨到 20 分钟前：总时限（默认 120s）应触发人工协助
            controller.state["started_at"] -= 1200
            await controller._tick()
            assert controller.paused is True
            assert controller.state["status"] == "awaiting_user"
            assert controller.state["last_reason"] == "timeout"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())



def test_phone_stop_cancels_fallback_scheduler_and_running_browse(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def browse(_app, _session):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        async def cleanup(**_kwargs):
            return {"stopped": 0, "errors": []}
        async def say(*_args):
            pass
        monkeypatch.setattr(plugin, "_cleanup_phone_home", cleanup)
        monkeypatch.setattr(plugin, "_say", say)
        plugin.raw_config["auto"]["enabled"] = True
        plugin.scheduler.run_browse = browse
        plugin.scheduler._start_fallback(session="webchat:FriendMessage:1",
            apps=["bilibili"], cron="* * * * *", max_items=1, prompt="")
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            await plugin._cmd_stop(FakeEvent(), [])
            assert cancelled.is_set()
            assert plugin.scheduler._fallback_task is None
            assert not plugin.scheduler._fallback_runs
            assert plugin.service.config.auto.enabled is False
            await asyncio.sleep(0)
            assert plugin.scheduler._fallback_jobs_data == []
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_break_rule_runtime_guidance_and_budget_toggle(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            raw = plugin.service._raw_config
            raw["flexibility"] = {"enabled": True}
            text = plugin_main._build_guidance(plugin.service.config)
            assert "自由浏览已开启" in text and "20%" in text
            assert "中途不感兴趣直接退出" not in text
            assert "刷到同类内容时降低打开/停留概率" not in text
            raw["flexibility"]["enabled"] = False
            assert "自由浏览已开启" not in plugin_main._build_guidance(plugin.service.config)
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


def test_watch_finish_stops_owner_without_recording_or_device_access(tmp_path, monkeypatch):
    import json
    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            event = FakeEvent()
            async def foreground(): return 'fixture.app', 'Test'
            monkeypatch.setattr(plugin.service.actions, 'foreground', foreground)
            task = await plugin.service.contents.begin(event, kind='post', content_id='fixture')
            async def unexpected(*a, **k): raise AssertionError('结束观看不应访问设备或采集')
            monkeypatch.setattr(plugin.service, 'watch_media', unexpected)
            monkeypatch.setattr(plugin.service.adb, 'ensure_ready', unexpected)
            tool = _tools_ns['PhoneWatchMediaTool'](service=plugin.service)
            result = await tool.call(FakeToolContext(event), action='finish')
            assert json.loads(result)['status'] == 'completed'
            assert not plugin.service.contents.active and task['status']=='completed'
        finally:
            await _cleanup(plugin)
    asyncio.run(run())
