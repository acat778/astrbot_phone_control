"""原生应用查询和工具共用权限、设备门禁及确定性读取路径。"""

import asyncio
from unittest.mock import AsyncMock

from test_main_0130_features import FakeEvent, FakeToolContext, _cleanup, _make_plugin, _tools_ns


def test_command_and_tool_read_without_model_quota_and_keep_permission(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        say = AsyncMock()
        monkeypatch.setattr(plugin, "_say", say)
        monkeypatch.setattr(plugin.service, "quota_error_text", lambda: "额度已用完")
        shell = AsyncMock(return_value="package:com.tencent.mm\npackage:com.android.settings")
        monkeypatch.setattr(plugin.service.adb, "shell", shell)
        classify = AsyncMock(side_effect=AssertionError("查询清单不得消耗模型"))
        monkeypatch.setattr(plugin.service.app_policy, "classify", classify)
        tool = _tools_ns["PhoneListAppsTool"](service=plugin.service)
        event = FakeEvent()
        event.stop_event = lambda: None
        try:
            await plugin.phone_command(event, "apps all 微信")
            assert "微信（com.tencent.mm）" in say.call_args.args[1]
            assert not plugin.device_gate.busy
            result = await tool.call(FakeToolContext(event), scope="system", query="ANDROID")
            assert "com.android.settings" in result and "额度已用完" not in result
            assert not plugin.device_gate.busy
            result = await tool.call(FakeToolContext(event), classify=True)
            assert result == "额度已用完"
            count = shell.await_count
            denied = FakeEvent(role="member", sender_id="other")
            denied.stop_event = lambda: None
            await plugin.phone_command(denied, "apps all")
            assert "权限不足" in say.call_args.args[1]
            assert "权限不足" in await tool.call(FakeToolContext(denied))
            assert shell.await_count == count
            classify.assert_not_awaited()
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())
