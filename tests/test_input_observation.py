"""文字输入后的节点核验失败保留动作事实，避免再次输入或直接提交。"""

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from test_main_0130_features import FakeEvent, FakeToolContext, _tools_ns

PhoneInputTextTool = _tools_ns["PhoneInputTextTool"]
errors = importlib.import_module(PhoneInputTextTool.__module__.rsplit(".", 2)[0] + ".errors")


def fixture(monkeypatch, *, fail_at=0, failure=None, input_failure=None, matches=True):
    calls = []
    reads = 0

    async def guard(*args):
        pass

    async def clear():
        calls.append("clear")

    async def input_text(text, **kwargs):
        calls.append("input")
        assert kwargs == {"press_enter": False}
        if input_failure:
            raise input_failure

    async def screen():
        nonlocal reads
        reads += 1
        calls.append("screen")
        if reads == fail_at:
            raise failure or errors.UiDumpError("could not get idle state")
        node = SimpleNamespace(
            focused=True, showing_hint=False, class_name="android.widget.EditText",
            text="测试文字" if matches else "", bounds=SimpleNamespace(
                width=100, height=40, center_x=50, center_y=20,
            ),
        )
        return "", SimpleNamespace(nodes=[node])

    async def tap(*args):
        calls.append("tap")

    async def wait(**kwargs):
        calls.append("wait")

    async def key(*args):
        calls.append("enter")

    monkeypatch.setattr(PhoneInputTextTool, "_guard", guard)
    service = SimpleNamespace(
        config=SimpleNamespace(model=SimpleNamespace(work_as_subagent=False)),
        actions=SimpleNamespace(clear_text=clear), input_text=input_text,
        screen=screen, tap=tap, wait=wait, press_key=key,
    )
    return PhoneInputTextTool(service=service), calls


@pytest.mark.parametrize("fail_at", [1, 2, 3])
@pytest.mark.parametrize("replace", [False, True])
def test_ui_failure_after_input_reports_unconfirmed_without_more_writes(monkeypatch, fail_at, replace):
    tool, calls = fixture(monkeypatch, fail_at=fail_at, matches=False)
    result = asyncio.run(tool.call(
        FakeToolContext(FakeEvent()), text="测试文字", replace=replace, press_enter=True,
    ))
    assert not result.startswith("error:")
    assert "输入操作已执行" in result and "未确认" in result
    assert "could not get idle state" in result
    assert "不要重复输入" in result and 'mode="image"' in result
    assert "未回车" in result and "enter" not in calls
    assert calls[-1] == "screen"
    assert calls.count("input") == (2 if fail_at == 3 else 1)


@pytest.mark.parametrize("kind", ["offline", "cancelled", "input_failed"])
def test_transport_cancellation_and_input_failure_keep_failure_semantics(monkeypatch, kind):
    exc = asyncio.CancelledError() if kind == "cancelled" else errors.DeviceOfflineError("device offline")
    tool, calls = fixture(
        monkeypatch, fail_at=1, failure=exc,
        input_failure=errors.UiDumpError("input itself failed") if kind == "input_failed" else None,
    )
    call = tool.call(FakeToolContext(FakeEvent()), text="测试文字", press_enter=True)
    if kind == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(call)
    else:
        assert asyncio.run(call).startswith("error:")
    assert calls.count("input") == 1 and "enter" not in calls


@pytest.mark.parametrize("matches", [False, True])
def test_enter_requires_confirmed_input(monkeypatch, matches):
    tool, calls = fixture(monkeypatch, matches=matches)
    result = asyncio.run(tool.call(FakeToolContext(FakeEvent()), text="测试文字", press_enter=True))
    assert ("已核对输入关键词" in result) == matches
    assert ("enter" in calls) == matches
