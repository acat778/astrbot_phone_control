"""core.agent_usage_patch 单元测试：runner stats → 事件 extra 的累计 usage 转换。"""

from __future__ import annotations

from core import agent_usage_patch as patch


class _FakeUsage:
    def __init__(self, input_other=0, input_cached=0, output=0):
        self.input_other = input_other
        self.input_cached = input_cached
        self.output = output


class _FakeStats:
    def __init__(self, usage):
        self.token_usage = usage


class _FakeProvider:
    def __init__(self, model="mimo-v2.6-flash"):
        self._model = model

    def get_model(self):
        return self._model


def test_usage_payload_from_agent_stats():
    payload = patch._usage_payload(_FakeStats(_FakeUsage(100, 20, 5)), _FakeProvider())
    assert payload == {
        "input_other": 100,
        "input_cached": 20,
        "output": 5,
        "model": "mimo-v2.6-flash",
        "source": "agent_runner_stats",
    }


def test_usage_payload_empty_returns_none():
    assert patch._usage_payload(_FakeStats(_FakeUsage())) is None
    assert patch._usage_payload(None) is None


def test_usage_payload_handles_missing_attrs():
    class _BrokenStats:
        token_usage = object()

    assert patch._usage_payload(_BrokenStats()) is None


def test_install_is_idempotent_and_uninstall_safe():
    # 本地无 astrbot 时安装降级返回 False；容器有 astrbot 时返回 True。
    # 两种情况都不能抛异常，且重复安装/卸载必须安全。
    installed = patch.install_agent_usage_capture()
    assert installed in (True, False)
    assert patch.install_agent_usage_capture() is installed
    patch.uninstall_agent_usage_capture()


def test_phone_tool_round_text_is_buffered_and_only_final_delivered(monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace

    class Event:
        def __init__(self):
            self.extra = {"phone_quiet_run": True}

        def get_extra(self, key):
            return self.extra.get(key)

        def set_extra(self, key, value):
            self.extra[key] = value

    class Runner:
        def __init__(self, event):
            self.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
            self.finished = False
            self.stats = None

        def done(self):
            return self.finished

        async def step(self):
            yield SimpleNamespace(type="streaming_delta", text="我去搜一下")
            yield SimpleNamespace(type="llm_result", text="我去搜一下")
            yield SimpleNamespace(type="agent_stats", text="usage")

    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.agent.runners.tool_loop_agent_runner",
        SimpleNamespace(ToolLoopAgentRunner=Runner),
    )
    assert patch.install_agent_usage_capture()

    async def run():
        event = Event()
        runner = Runner(event)
        first = [item async for item in runner.step()]
        assert [item.type for item in first] == ["agent_stats"]
        runner.finished = True
        final = [item async for item in runner.step()]
        assert [item.type for item in final] == [
            "agent_stats",
            "streaming_delta",
            "llm_result",
        ]
        event.extra["phone_shared_current_run"] = True
        shared = [item async for item in runner.step()]
        assert [item.type for item in shared] == ["agent_stats"]
        event.extra["phone_quiet_run"] = False
        regular = [item async for item in runner.step()]
        assert [item.type for item in regular] == [
            "streaming_delta",
            "llm_result",
            "agent_stats",
        ]

    try:
        asyncio.run(run())
    finally:
        patch.uninstall_agent_usage_capture()


def test_first_phone_tool_round_is_quiet_even_without_keyword_marker(monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace

    class Event:
        extra = {"phone_output_candidate": True}

        def get_extra(self, key):
            return self.extra.get(key)

        def set_extra(self, key, value):
            self.extra[key] = value

    class Runner:
        def __init__(self):
            self.run_context = SimpleNamespace(context=SimpleNamespace(event=Event()))

        def done(self):
            return False

        async def step(self):
            yield SimpleNamespace(type="streaming_delta", text="我先去搜一下")
            self.run_context.context.event.set_extra("phone_quiet_run", True)
            yield SimpleNamespace(type="llm_result", text="我先去搜一下")

    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.agent.runners.tool_loop_agent_runner",
        SimpleNamespace(ToolLoopAgentRunner=Runner),
    )
    assert patch.install_agent_usage_capture()

    async def run():
        assert [x async for x in Runner().step()] == []

    try:
        asyncio.run(run())
    finally:
        patch.uninstall_agent_usage_capture()


def test_runner_preparation_covers_direct_cron_entry_and_updates_on_reload(monkeypatch):
    """原生cron直接step时也在模型调用前准备工具；重载采用当前实例回调。"""
    import asyncio
    import sys
    from types import SimpleNamespace

    class Event:
        def get_extra(self, key):
            return None

    class Runner:
        def __init__(self):
            self.run_context = SimpleNamespace(context=SimpleNamespace(event=Event()))
            self.req = SimpleNamespace(prepared='')

        async def step(self):
            assert self.req.prepared
            yield SimpleNamespace(type='tool_call', prepared=self.req.prepared)

    monkeypatch.setitem(sys.modules, 'astrbot.core.agent.runners.tool_loop_agent_runner',
                        SimpleNamespace(ToolLoopAgentRunner=Runner))
    try:
        assert patch.install_agent_usage_capture(prepare_runner=lambda runner: setattr(runner.req, 'prepared', 'first'))
        async def invoke():
            return [item.prepared async for item in Runner().step()]
        assert asyncio.run(invoke()) == ['first']
        assert patch.install_agent_usage_capture(prepare_runner=lambda runner: setattr(runner.req, 'prepared', 'current'))
        assert asyncio.run(invoke()) == ['current']
    finally:
        patch.uninstall_agent_usage_capture()
    assert not hasattr(Runner, patch.PREPARE_ATTR)
