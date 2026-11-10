"""应用清单按真实包管理器输出筛选，分页与非法输入均有明确结果。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.errors import PhoneControlError
from core.installed_apps import list_apps


@pytest.mark.parametrize("scope,flag", [("third_party", " -3"), ("system", " -s"), ("all", "")])
def test_scope_reads_current_user_and_filters_without_shell_interpolation(scope, flag):
    async def scenario():
        adb = SimpleNamespace(shell=AsyncMock(return_value="package:com.tencent.mm\npackage:com.other.app\npackage:com.tencent.mm\n"))
        text = await list_apps(adb, scope=scope, query="微信")
        adb.shell.assert_awaited_once_with("pm list packages --user current" + flag, timeout=20)
        assert "共 2 个，匹配 1 个" in text and "微信（com.tencent.mm）" in text
        assert "com.other.app" not in text
        text = await list_apps(adb, query="$(touch /no-such-file);'")
        assert "匹配 0 个" in text
        assert all("touch" not in call.args[0] for call in adb.shell.call_args_list)
    asyncio.run(scenario())


def test_paging_empty_and_unmapped_names():
    async def scenario():
        adb = SimpleNamespace(shell=AsyncMock(return_value="package:com.z.app\npackage:com.a.app\npackage:com.b.app\n"))
        first = await list_apps(adb, limit=2)
        assert first.index("com.a.app") < first.index("com.b.app")
        assert "com.z.app" not in first and "offset=2" in first and "名称未收录" in first
        last = await list_apps(adb, limit=2, offset=2)
        assert "com.z.app" in last and "还有应用" not in last
        assert "当前页没有匹配应用" in await list_apps(adb, offset=50)
        adb.shell.return_value = ""
        assert "共 0 个，匹配 0 个" in await list_apps(adb)
        adb.shell.return_value = "package:com.good.app\nError: user not found"
        with pytest.raises(PhoneControlError, match="无效清单"):
            await list_apps(adb)
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [{"scope": "all; id"}, {"limit": 0}, {"limit": 201}, {"offset": -1}, {"offset": True}, {"query": []}])
def test_invalid_input_never_queries_device(kwargs):
    adb = SimpleNamespace(shell=AsyncMock())
    with pytest.raises(PhoneControlError):
        asyncio.run(list_apps(adb, **kwargs))
    adb.shell.assert_not_awaited()


def test_optional_classification_only_requests_current_page():
    async def scenario():
        adb = SimpleNamespace(shell=AsyncMock(return_value="package:com.a.app\npackage:com.b.app"))
        policy = SimpleNamespace(classify=AsyncMock(side_effect=PhoneControlError("模型未确认")))
        text = await list_apps(adb, offset=1, limit=1, policy=policy)
        policy.classify.assert_awaited_once_with("com.b.app")
        assert "未确认 · 拒绝打开" in text
    asyncio.run(scenario())
