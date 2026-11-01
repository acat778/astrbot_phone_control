"""模型候选、隔离、冷却、取消与主模型定时恢复。"""

import asyncio
from types import SimpleNamespace as NS
import pytest
from functools import wraps


def run_async(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapped


from core.model_failover import FailoverPolicy, ModelFailover


def setup(mode="models"):
    def provider(name, source):
        async def test():
            calls.append("probe:" + name)
            if failing[0]:
                raise RuntimeError("unavailable")

        return NS(provider_config={"id": name, "provider_source_id": source}, test=test)

    calls, failing, now = [], [True], [0]
    providers = [
        provider("main", "a"),
        provider("b1", "b"),
        provider("b2", "b"),
        provider("c1", "c"),
    ]
    ctx = NS(
        get_all_providers=lambda: providers,
        get_all_stt_providers=lambda: [],
        get_all_embedding_providers=lambda: [],
        get_provider_by_id=lambda key: next(
            (p for p in providers if p.provider_config["id"] == key), None
        ),
    )
    raw = {
        "model": {
            "task_failover": {
                "mode": mode,
                "models": ["b1", "b2", "c1"],
                "suppliers": ["b"],
                "recovery_seconds": 5,
            }
        }
    }
    manager = ModelFailover(ctx, lambda: raw, clock=lambda: now[0])
    return manager, calls, failing, now, raw


@run_async
async def test_failure_sticks_and_timed_probe_restores_primary():
    m, calls, bad, now, _ = setup()

    async def call(name):
        calls.append(name)
        if name in {"main", "b1"} and bad[0]:
            raise RuntimeError("quota")
        return name

    assert await m.run("task", "main", call) == "b2"
    assert await m.run("task", "main", call) == "b2"
    assert calls == ["main", "b1", "b2", "b2"]
    await m.probe_due()
    assert "probe:main" not in calls
    now[0] = 5
    await m.probe_due()
    assert m.current("task", "main") == "b2"
    bad[0] = False
    now[0] = 10
    await m.probe_due()
    assert m.current("task", "main") == "main"
    assert await m.run("task", "main", call) == "main"


def test_supplier_candidates_and_capability_isolation():
    m, *_ = setup("suppliers")
    assert m.candidates("task", "main") == ["main", "b1", "b2"]
    assert m.candidates("embedding", "embed") == ["embed"]


@run_async
async def test_cancellation_does_not_try_backup_or_mark_failure():
    m, calls, *_ = setup()

    async def call(name):
        calls.append(name)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await m.run("task", "main", call)
    assert calls == ["main"] and not m.state("task", "main").failed


@run_async
async def test_each_attempt_has_timeout_and_all_errors_are_visible():
    m, calls, *_ = setup()

    async def call(name):
        calls.append(name)
        if name == "main":
            await asyncio.sleep(10)
        raise RuntimeError("secret must not be in public error")

    with pytest.raises(Exception, match="main: TimeoutError") as error:
        await m.run("task", "main", call, timeout=0.01)
    assert calls == ["main", "b1", "b2", "c1"] and "secret" not in str(error.value)


@run_async
async def test_policy_change_resets_selection_and_disabled_preserves_error():
    m, calls, bad, now, raw = setup()

    async def call(name):
        if name == "main":
            raise RuntimeError("quota")
        return name

    assert await m.run("task", "main", call) == "b1"
    raw["model"]["task_failover"]["mode"] = "disabled"
    assert m.current("task", "main") == "main"
    with pytest.raises(RuntimeError, match="quota"):
        await m.run("task", "main", call)


@run_async
async def test_concurrent_failure_only_attempts_bad_primary_once():
    m, calls, *_ = setup()

    async def call(name):
        calls.append(name)
        await asyncio.sleep(0)
        if name == "main":
            raise RuntimeError("down")
        return name

    assert (
        await asyncio.gather(*(m.run("task", "main", call) for _ in range(4)))
        == ["b1"] * 4
    )
    assert calls.count("main") == 1


@pytest.mark.parametrize("value", [0, True, 4, 86401, "bad"])
def test_invalid_recovery_interval_is_rejected(value):
    with pytest.raises(ValueError):
        FailoverPolicy.parse({"recovery_seconds": value})
