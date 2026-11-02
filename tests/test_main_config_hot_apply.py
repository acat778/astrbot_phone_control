"""main.PhoneControlPlugin 配置补丁热应用测试（容器内跑，本地无 astrbot 自动跳过）。

运行中改配置走命令/原生配置页重载后的内部补丁路径
``plugin._apply_config_patch``（``/phone continuous``、``/phone traffic``
等共用）。本文件用假 Context 重放该路径，断言持续刷手机、实时屏幕、后端重置等运行态
动作确实被触发，覆盖「配置保存后没有即时生效」的回归场景。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_astrbot = pytest.importorskip("astrbot")
if getattr(_astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot，跳过容器级用例", allow_module_level=True)


def _load_plugin_main():
    """把插件目录当作包导入（main.py 使用相对导入，不能按顶层模块导入）。"""
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_under_test"
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


class FakeContext:
    """最小 Star Context：只实现插件装配需要的接口。"""

    cron_manager = None

    def __init__(self) -> None:
        self.tools = []
        self.web_routes: list[tuple[str, tuple[str, ...]]] = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods=None, desc=None) -> None:
        self.web_routes.append((str(route), tuple(methods or [])))


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _raw_config(**overrides) -> dict:
    """按完整卡片结构构造插件配置（默认值 + 分组覆盖）。"""
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "127.0.0.1",
        "port": 5555,
        "path": sys.executable,
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }
    return _deep_merge(raw, overrides)


def _make_plugin(tmp_path, monkeypatch) -> "plugin_main.PhoneControlPlugin":
    """实例化插件并让配置写回落到临时目录。"""
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    plugin = plugin_main.PhoneControlPlugin(FakeContext(), _raw_config())
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


async def _save(plugin, config: dict) -> list[str]:
    """走运行中配置补丁路径（与命令侧相同）。"""
    return await plugin._apply_config_patch(config)


async def _cleanup(plugin) -> None:
    try:
        await plugin.terminate()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言结果
        pass


def test_patch_starts_continuous_play_immediately(tmp_path, monkeypatch):
    """补丁开启 auto.continuous_enabled=true 必须立即启动持续刷循环。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            assert plugin._continuous_enabled is False
            notes = "；".join(
                await _save(plugin, _raw_config(auto={"continuous_enabled": True}))
            )
            assert plugin._continuous_enabled is True
            assert plugin._continuous_task is not None
            assert not plugin._continuous_task.done()
            assert "持续刷手机：已开启" in notes
            assert "配置已保存并即时生效" in notes
            saved = json.loads(
                (tmp_path / "plugin_config.json").read_text(encoding="utf-8")
            )
            assert saved["auto"]["mode"] == "自动刷"
            assert saved["auto"]["activity_enabled"] is True
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_stops_continuous_play(tmp_path, monkeypatch):
    """补丁关闭 auto.continuous_enabled 后不再开新轮。"""

    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(), _raw_config(auto={"continuous_enabled": True})
        )
        monkeypatch.setattr(
            plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
        )
        try:
            assert plugin._continuous_enabled is True
            notes = "；".join(
                await _save(
                    plugin, _raw_config(auto={"continuous_enabled": False})
                )
            )
            assert plugin._continuous_enabled is False
            assert plugin._continuous_task is None
            assert "持续刷手机：已关闭" in notes
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_live_screen_enable_without_task(tmp_path, monkeypatch):
    """开启任务期间持续截图：无运行中任务时记录为下次任务生效，不常驻 watcher。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            notes = "；".join(
                await _save(plugin, _raw_config(perception={"live_screen_enabled": True}))
            )
            assert "任务期间持续截图：已开启" in notes
            assert "下次任务开始时启用" in notes
            assert plugin.live_screen.running is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_backend_change_resets_backends(tmp_path, monkeypatch):
    """控制后端变化：重置当前控制/感知后端会话。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        stop_calls = []

        async def fake_stop():
            stop_calls.append(True)

        monkeypatch.setattr(plugin.backends, "stop", fake_stop)
        try:
            notes = "；".join(
                await _save(plugin, _raw_config(control={"backend": "adb"}))
            )
            assert stop_calls == [True]
            assert "控制/感知后端" in notes
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_adb_change_reconfigures_client(tmp_path, monkeypatch):
    """ADB 参数变化：调用 AdbClient.reconfigure 换目标，并重建后端。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        recorded: dict = {}

        async def fake_reconfigure(**kwargs):
            recorded.update(kwargs)
            return {"changed": True}

        async def fake_stop():
            recorded["stopped"] = True

        monkeypatch.setattr(plugin.adb, "reconfigure", fake_reconfigure)
        monkeypatch.setattr(plugin.backends, "stop", fake_stop)
        try:
            config = _raw_config()
            config["adb"].update({"host": "127.0.0.2", "port": 6666})
            notes = "；".join(await _save(plugin, config))
            assert recorded.get("host") == "127.0.0.2"
            assert recorded.get("port") == 6666
            assert recorded.get("stopped") is True
            assert "ADB 连接参数已更新" in notes
            assert "127.0.0.2:6666" in notes
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_accepts_debug_section(tmp_path, monkeypatch):
    """完整配置包含 debug 段时按正常配置处理。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            notes = "；".join(
                await _save(plugin, _raw_config(debug={"log_actions": False}))
            )
            assert "忽略未知配置项" not in notes
            assert plugin.raw_config["debug"]["log_actions"] is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_apply_config_patch_partial_patch(tmp_path, monkeypatch):
    """命令侧传部分补丁（/phone continuous on 等）也走同一管道并即时生效。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            await plugin._apply_config_patch(
                {"auto": {"continuous_enabled": True}}
            )
            assert plugin._continuous_enabled is True
            assert plugin._continuous_task is not None
            assert not plugin._continuous_task.done()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_traffic_log_lines_masks_sensitive_params_and_truncates():
    """抓包日志摘要：敏感参数打码、正文截断、带页面推断与时间。"""
    flows = [
        {
            "ts": 1792460000,
            "method": "GET",
            "url": "https://api.xiaoheihe.cn/bbs/app/feed?sign=secret&page=1",
            "status": 200,
            "body": "x" * 500,
        }
    ]
    lines = plugin_main.PhoneControlPlugin._traffic_log_lines(
        flows, body_chars=100, limit=3
    )
    text = "\n".join(lines)
    assert "sign=***" in text
    assert "小黑盒" in text
    assert "响应：" in text
    assert len(lines[1]) < 130


def test_agent_lifecycle_resets_light_interaction_quota_and_releases_pending(tmp_path, monkeypatch):
    from types import SimpleNamespace

    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        event = SimpleNamespace(unified_msg_origin="webchat:FriendMessage:light-uat", role="admin")
        extra = {}
        event.get_extra = lambda key, default=None: extra.get(key, default)
        event.set_extra = lambda key, value: extra.__setitem__(key, value)
        event.get_sender_id = lambda: "1"
        event.get_platform_name = lambda: "webchat"
        event.get_message_str = lambda: ""
        tools = {t.name: t for t in plugin.context.tools}
        assert "phone_shell" in tools
        assert "phone_shell" in plugin_main._PHONE_CONTROL_TOOLS
        try:
            plugin.raw_config["security"].update(allow_interactions=True, max_likes_per_session=1)
            ctx = SimpleNamespace(context=SimpleNamespace(event=event))
            await plugin.track_task_agent_begin(event)
            first = extra["phone_interaction_run"]
            assert "已预留" in await tools["phone_interaction_gate"].call(ctx, kind="like")
            assert "名额已满" in await tools["phone_interaction_gate"].call(ctx, kind="favorite")
            await plugin.on_task_agent_done(event)
            assert not plugin.interactions.reservations()
            await plugin.track_task_agent_begin(event)
            assert first != extra["phone_interaction_run"]
            assert "已预留" in await tools["phone_interaction_gate"].call(ctx, kind="favorite")
            await plugin.on_task_agent_done(event)
            assert not plugin.interactions.reservations()
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize('origin', ['phone_random_play', 'phone_continuous'])
def test_activity_window_stops_model_run_on_boundary(tmp_path, monkeypatch, origin):
    """结束时段可取消正在等待模型的 run，且无需等下一次手机工具调用。"""
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        taskmod = sys.modules[plugin_main.PhoneControlPlugin._watch_activity_window.__module__]
        allowed = True
        monkeypatch.setattr(taskmod, 'activity_allowed', lambda *args: allowed)
        async def finish(_event):
            return None
        monkeypatch.setattr(plugin, 'on_task_agent_done', finish)
        event = types.SimpleNamespace(get_extra=lambda key: {'origin': origin, 'play_task': True} if key == 'cron_payload' else None)
        cleaned = asyncio.Event()
        started = asyncio.Event()
        async def model_run():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        run = asyncio.create_task(model_run())
        await started.wait()
        try:
            plugin._track_activity_window(event, run)
            await asyncio.sleep(0)
            assert not run.done()
            allowed = False
            await asyncio.wait_for(cleaned.wait(), 2)
            await asyncio.gather(run, return_exceptions=True)
            assert run.cancelled()
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            await plugin.terminate()
        assert not plugin._activity_window_tasks
    asyncio.run(scenario())


def test_manual_play_is_not_tracked_by_activity_window(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        event = types.SimpleNamespace(get_extra=lambda key: {'origin':'phone_play','play_task':True} if key == 'cron_payload' else None)
        try:
            plugin._track_activity_window(event, asyncio.current_task())
            assert not plugin._activity_window_tasks
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_invalid_activity_window_keeps_saved_and_running_config(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        before = plugin._plain_config(plugin.raw_config)
        try:
            notes = await plugin._apply_config_patch({'auto':{'random_windows':['25:00-26:00']}})
            assert any('时间段无效' in note for note in notes)
            assert plugin._plain_config(plugin.raw_config) == before
            assert not plugin._config_file_path().exists()
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_mode_switch_removes_pending_other_jobs_and_keeps_user_play(tmp_path, monkeypatch):
    from types import SimpleNamespace
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        origins=['phone_random_play','phone_continuous',plugin_main.AUTO_BROWSE_JOB_ORIGIN,'phone_play']
        pending=[SimpleNamespace(job_id=str(i),name='test',payload={'origin':origin}) for i,origin in enumerate(origins)]
        deleted=[]
        class Manager:
            async def list_jobs(self,*args):
                return list(pending)
            async def delete_job(self,job_id):
                deleted.append(job_id)
                pending[:]=[job for job in pending if job.job_id!=job_id]
        plugin.context.cron_manager=Manager()
        plugin.raw_config['auto'].update(mode='随机玩',activity_enabled=True)
        try:
            await plugin._sync_activity_jobs()
            assert deleted==['1','2']
            assert [job.payload['origin'] for job in pending]==['phone_random_play','phone_play']
            plugin.raw_config['auto']['activity_enabled']=False
            await plugin._sync_activity_jobs()
            assert [job.payload['origin'] for job in pending]==['phone_play']
        finally:
            plugin.context.cron_manager=None
            await plugin.terminate()
    asyncio.run(scenario())


def test_agent_begin_rejects_inactive_mode_before_model(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        event=types.SimpleNamespace(get_extra=lambda key: {'origin':'phone_continuous','play_task':True} if key=='cron_payload' else None)
        try:
            with pytest.raises(asyncio.CancelledError,match='自主模式已关闭'):
                await plugin.track_task_agent_begin(event)
            assert not plugin._active_agent_tasks
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize('origin', [
    'phone_random_play', 'phone_continuous', plugin_main.AUTO_BROWSE_JOB_ORIGIN,
    'phone_play',
])
def test_terminate_cancels_running_phone_cron_before_closing_resources(tmp_path, monkeypatch, origin):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        modes = {'phone_continuous': '自动刷', plugin_main.AUTO_BROWSE_JOB_ORIGIN: '定时玩'}
        plugin.raw_config['auto'].update(activity_enabled=True,
            mode=modes.get(origin, '随机玩'))
        extra = {'cron_payload': {'origin': origin, 'play_task': True},
                 'cron_job': {'id': 'reload-test'}}
        event = types.SimpleNamespace(unified_msg_origin='cron:OtherMessage:reload-test',
            get_extra=lambda key, default=None: extra.get(key, default),
            set_extra=lambda key, value: extra.__setitem__(key, value),
            get_platform_name=lambda: 'cron', get_sender_id=lambda: '1')
        ready = asyncio.Event()
        async def agent():
            await plugin.track_task_agent_begin(event)
            ready.set()
            await asyncio.Event().wait()
        run = asyncio.create_task(agent())
        unrelated = asyncio.create_task(asyncio.Event().wait())
        await asyncio.wait_for(ready.wait(), 2)
        async def close_models():
            assert run.cancelled(), 'Agent must stop before shared resources close'
        monkeypatch.setattr(plugin.service.model_failover, 'close', close_models)
        try:
            await plugin.terminate()
            assert run.cancelled()
            assert not plugin._active_agent_tasks
            assert not unrelated.done()
            with pytest.raises(asyncio.CancelledError, match='实例已关闭'):
                await plugin.track_task_agent_begin(event)
            assert not plugin._active_agent_tasks
        finally:
            run.cancel()
            unrelated.cancel()
            await asyncio.gather(run, unrelated, return_exceptions=True)
            await plugin.terminate()
    asyncio.run(scenario())


@pytest.mark.parametrize('origin,mode',[('phone_random_play','随机玩'),('phone_continuous','自动刷')])
@pytest.mark.parametrize('swallow_cancel',[False,True])
def test_cancelled_activity_releases_lease_without_framework_done_hook(tmp_path, monkeypatch, origin, mode, swallow_cancel):
    """真实任务取消路径没有框架结束钩子时，仍释放设备并只收尾一次。"""
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        plugin.raw_config['auto'].update(mode=mode,activity_enabled=True)
        extra={'cron_payload':{'origin':origin,'play_task':True},'cron_job':{'id':'cancel-uat'}}
        event=types.SimpleNamespace(unified_msg_origin='cron:OtherMessage:cancel-uat',role='admin',get_extra=lambda key,default=None:extra.get(key,default),set_extra=lambda key,value:extra.__setitem__(key,value),get_platform_name=lambda:'cron',get_sender_id=lambda:'1',get_message_str=lambda:'')
        cleanups=[]
        async def cleanup(e):
            cleanups.append(e)
        monkeypatch.setattr(plugin,'_auto_cleanup_after_task',cleanup)
        ready=asyncio.Event()
        async def model_run():
            await plugin.track_task_agent_begin(event)
            plugin.device_gate.enter(event,purpose='uat')
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow_cancel:
                    raise
        run=asyncio.create_task(model_run())
        await ready.wait()
        assert plugin.device_gate.busy
        try:
            plugin.raw_config['auto']['activity_enabled']=False
            await asyncio.wait_for(asyncio.gather(run,return_exceptions=True),2)
            monitors=list(plugin._activity_window_tasks)
            if monitors:
                await asyncio.wait_for(asyncio.gather(*monitors),2)
            assert run.done()
            assert run.cancelled() is (not swallow_cancel)
            assert not plugin.device_gate.busy
            assert not plugin._active_agent_tasks
            assert len(cleanups)==1
            await plugin.on_task_agent_done(event)
            assert len(cleanups)==1
        finally:
            run.cancel()
            await asyncio.gather(run,return_exceptions=True)
            await plugin.terminate()
    asyncio.run(scenario())
