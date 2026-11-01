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


def test_all_six_native_selects_and_typed_policies():
    import json
    from pathlib import Path
    from core.config import PhoneConfig
    from core.model_config import populate_failover_options
    from core.model_failover import SLOTS

    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    m, *_ = setup()
    config = NS(schema=schema)
    populate_failover_options(config, m.context)
    for slot, (group, key, kind) in SLOTS.items():
        fields = schema[group]["items"][key]["items"]
        assert fields["mode"]["options"] == ["disabled", "suppliers", "models"]
        for mode, field in [("suppliers", "suppliers"), ("models", "models")]:
            assert fields[field]["type"] == "list"
            assert fields[field]["condition"] == {"mode": mode}
        raw = {
            group: {key: {"mode": "models", "models": ["b1"], "recovery_seconds": 60}}
        }
        typed = getattr(getattr(PhoneConfig.from_mapping(raw), group), key)
        assert typed.models == ("b1",) and typed.recovery_seconds == 60
    assert schema["model"]["items"]["task_failover"]["items"]["suppliers"][
        "options"
    ] == ["a", "b", "c"]
    assert (
        schema["memory"]["items"]["embedding_failover"]["items"]["models"]["options"]
        == []
    )


@run_async
async def test_error_and_empty_responses_switch_without_mutating_original_request():
    from core.failover_adapter import generate

    m, calls, bad, now, raw = setup()
    raw["model"]["vision_failover"] = raw["model"]["task_failover"].copy()
    payload = {
        "contexts": [{"role": "user", "content": "original"}],
        "image_urls": ["image.png"],
        "audio_urls": ["audio.wav"],
    }

    async def llm_generate(**kw):
        calls.append(
            (
                kw["chat_provider_id"],
                kw["contexts"][0]["content"],
                kw["image_urls"][:],
                kw["audio_urls"][:],
            )
        )
        kw["contexts"][0]["content"] = "mutated"
        return NS(
            role="err" if kw["chat_provider_id"] == "main" else "assistant",
            completion_text="ok" if kw["chat_provider_id"] == "b2" else "",
            tools_call_name=[],
        )

    m.context.llm_generate = llm_generate
    service = NS(app_context=m.context, model_failover=m)
    value = await generate(service, "vision", chat_provider_id="main", **payload)
    assert value.completion_text == "ok"
    assert [x[0] for x in calls] == ["main", "b1", "b2"]
    assert all(x[1:] == ("original", ["image.png"], ["audio.wav"]) for x in calls)
    assert payload["contexts"][0]["content"] == "original"


@run_async
async def test_agent_switch_preserves_tool_context_and_shared_models():
    from core.failover_adapter import FailoverChatProvider

    m, calls, *_ = setup()
    for provider in m.context.get_all_providers():
        name = provider.provider_config["id"]

        async def text_chat(_name=name, **kw):
            calls.append((_name, kw))
            if _name == "main":
                raise RuntimeError("quota")
            return NS(
                role="assistant", completion_text="", tools_call_name=["phone_status"]
            )

        provider.text_chat = text_chat
    primary = m.context.get_provider_by_id("main")
    proxy = FailoverChatProvider(m, primary)
    event = asyncio.Event()
    result = await proxy.text_chat(
        model="primary-model",
        abort_signal=event,
        contexts=[{"role": "tool", "content": "action already completed"}],
    )
    assert result.tools_call_name == ["phone_status"]
    assert calls[1][1]["contexts"] == [
        {"role": "tool", "content": "action already completed"}
    ]
    assert calls[1][1]["abort_signal"] is event and "model" not in calls[1][1]
    assert primary.provider_config == {"id": "main", "provider_source_id": "a"}


@run_async
async def test_agent_stream_failure_discards_partial_output_and_closes_stream():
    from core.failover_adapter import FailoverChatProvider

    m, calls, *_ = setup()
    for provider in m.context.get_all_providers():
        name = provider.provider_config["id"]

        async def stream(_name=name, **kw):
            try:
                yield NS(
                    role="assistant",
                    completion_text="partial" if _name == "main" else "backup",
                    is_chunk=True,
                )
                if _name == "main":
                    raise RuntimeError("connection lost")
                yield NS(
                    role="assistant", completion_text="backup complete", is_chunk=False
                )
            finally:
                calls.append("closed:" + _name)

        provider.text_chat_stream = stream
    proxy = FailoverChatProvider(m, m.context.get_provider_by_id("main"))
    result = [x async for x in proxy.text_chat_stream(contexts=[])]
    assert [x.completion_text for x in result] == ["backup", "backup complete"]
    assert calls == ["closed:main", "closed:b1"]


@run_async
async def test_embedding_switch_never_returns_backup_vectors_under_primary_identity():
    from core.failover_adapter import PinnedEmbedding, EmbeddingModelChanged
    from core.semantic_recall import SemanticEmbeddingResolver

    m, calls, bad, now, raw = setup()
    raw["memory"] = {
        "embedding_failover": {
            "mode": "models",
            "models": ["b1"],
            "recovery_seconds": 5,
        }
    }
    providers = m.context.get_all_providers()
    for provider in providers:
        name = provider.provider_config["id"]
        provider.get_dim = lambda: 2
        provider.get_model = lambda _name=name: _name

        async def embed(texts, _name=name):
            calls.append(_name)
            if _name == "main":
                raise RuntimeError("quota")
            return [[1.0, 0.0] for _ in texts]

        provider.get_embeddings = embed
    m.context.get_all_embedding_providers = lambda: providers
    resolver = SemanticEmbeddingResolver(m.context, failover=m)
    first = resolver.resolve("main")
    with pytest.raises(EmbeddingModelChanged):
        await first.provider.get_embeddings(["doc"])
    second = resolver.resolve("main")
    assert second.provider_id == "b1" and second.key != first.key
    assert await second.provider.get_embeddings(["doc"]) == [[1, 0]]
    assert calls == ["main", "b1", "b1"]


@run_async
async def test_media_backup_retains_audio_and_closes_adapters():
    from core.media_provider import MediaProviders
    from core.config import PhoneConfig

    m, calls, bad, now, raw = setup()
    raw["model"]["asr_failover"] = raw["model"]["task_failover"].copy()

    async def generate(**kw):
        calls.append(kw)
        if kw["chat_provider_id"] == "main":
            raise RuntimeError("quota")
        return NS(role="assistant", completion_text="transcript")

    m.context.llm_generate = generate
    service = NS(
        app_context=m.context, model_failover=m, config=PhoneConfig.from_mapping(raw)
    )
    media = MediaProviders(m.context, service)
    result = await media.generate(
        chat_provider_id="main", prompt="transcribe", audio_urls=["audio.wav"]
    )
    assert result.completion_text == "transcript"
    assert [x["audio_urls"] for x in calls] == [["audio.wav"], ["audio.wav"]]
    await media.close()


@run_async
async def test_probe_error_response_does_not_restore_and_stop_cancels_timer():
    m, calls, bad, now, raw = setup()

    async def call(name):
        if name == "main":
            raise RuntimeError("down")
        return name

    await m.run("task", "main", call)

    async def err(**kw):
        return NS(role="err", completion_text="error")

    m.context.get_provider_by_id("main").text_chat = err
    now[0] = 5
    await m.probe_due()
    assert m.current("task", "main") == "b1"
    m.start()
    task = m._task
    await m.close()
    assert task.done() and not m.routes


def test_app_recall_cache_checks_model_identity_even_at_same_dimension(tmp_path):
    from core.app_recall import AppRecallReader
    from core.semantic_recall import ResolvedEmbedding

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    p1 = ResolvedEmbedding(NS(), "main", "model-a", 2)
    p2 = ResolvedEmbedding(NS(), "b1", "model-b", 2)
    old = NS(provider_key=p1.key)
    reads = []

    def load_snapshot(*, provider_key):
        reads.append(provider_key)
        return old if provider_key == p1.key else None

    index = NS(manifest_path=manifest, load_snapshot=load_snapshot)
    reader = AppRecallReader(
        index=index,
        resolver=None,
        config_getter=lambda: None,
        entry_provider=lambda: [],
    )
    assert reader._snapshot(p1) is old
    assert reader._snapshot(p2) is None
    assert reads == [p1.key, p2.key]


@run_async
async def test_app_index_restarts_entire_build_after_switch_and_restoration(tmp_path):
    from core.app_recall import AppRecallMaintainer, app_index_root
    from core.semantic import SemanticIndex
    from core.semantic_recall import SemanticEmbeddingResolver
    from core.config import PhoneConfig
    from test_app_recall import _store

    m, calls, bad, now, raw = setup()
    raw["model"]["app_embedding_failover"] = {
        "mode": "models",
        "models": ["b1"],
        "recovery_seconds": 5,
    }
    raw["model"]["app_memory_embedding_provider_id"] = "main"
    providers = m.context.get_all_providers()
    for p in providers:
        name = p.provider_config["id"]
        dim = 2 if name == "main" else 3
        p.get_dim = lambda _dim=dim: _dim
        p.get_model = lambda _name=name: _name + "-model"

        async def many(texts, _name=name, _dim=dim):
            if _name == "main" and bad[0]:
                raise RuntimeError("down")
            return [[1.0] + [0.0] * (_dim - 1) for _ in texts]

        async def one(text, _many=many):
            return (await _many([text]))[0]

        p.get_embeddings = many
        p.get_embedding = one
    m.context.get_all_embedding_providers = lambda: providers
    resolver = SemanticEmbeddingResolver(m.context, failover=m, slot="app_embedding")
    index = SemanticIndex(app_index_root(tmp_path))
    _store(tmp_path)
    maintainer = AppRecallMaintainer(
        index=index,
        resolver=resolver,
        config_getter=lambda: PhoneConfig.from_mapping(raw),
        data_dir=tmp_path,
    )
    assert await maintainer.refresh(force=True)
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert (
        snapshot.dim == 3
        and snapshot.provider_key == resolver.resolve("main").key
        and snapshot.count == 2
    )
    bad[0] = False
    now[0] = 5
    await m.probe_due()
    assert await maintainer.refresh(force=True)
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert (
        snapshot.dim == 2
        and snapshot.provider_key == resolver.resolve("main").key
        and snapshot.count == 2
    )


@run_async
async def test_asr_chat_primary_can_switch_to_native_stt():
    from core.config import PhoneConfig
    from core.media_provider import MediaProviders

    m, calls, bad, now, raw = setup()
    raw["model"]["asr_failover"] = {
        "mode": "models",
        "models": ["stt"],
        "recovery_seconds": 5,
    }

    async def transcribe(audio):
        calls.append(audio)
        return "actual transcript"

    stt = NS(provider_config={"id": "stt"}, get_text=transcribe)
    original_get = m.context.get_provider_by_id
    m.context.get_provider_by_id = lambda key: (
        stt if key == "stt" else original_get(key)
    )
    m.context.get_all_stt_providers = lambda: [stt]

    async def broken(**kw):
        raise RuntimeError("down")

    m.context.llm_generate = broken
    service = NS(
        app_context=m.context, model_failover=m, config=PhoneConfig.from_mapping(raw)
    )
    media = MediaProviders(m.context, service)
    result = await media.generate(
        _slot="asr",
        chat_provider_id="main",
        prompt="transcribe",
        audio_urls=["one.wav"],
    )
    assert result.completion_text == "actual transcript" and calls == ["one.wav"]


@run_async
async def test_asr_native_stt_can_switch_to_chat_and_records_real_usage():
    from core.config import PhoneConfig
    from core.media_provider import MediaProviders

    m, calls, bad, now, raw = setup()
    raw["model"]["asr_failover"] = {
        "mode": "models",
        "models": ["b1"],
        "recovery_seconds": 5,
    }

    async def broken(audio):
        raise RuntimeError("down")

    primary = m.context.get_provider_by_id("main")
    primary.get_text = broken

    async def llm_generate(**kw):
        return NS(
            role="assistant", completion_text="actual transcript", usage={"input": 10}
        )

    m.context.llm_generate = llm_generate
    service = NS(
        app_context=m.context,
        model_failover=m,
        config=PhoneConfig.from_mapping(raw),
        record_internal_usage=lambda usage, **kw: calls.append((usage, kw)),
    )
    result = await MediaProviders(m.context, service).transcribe(
        primary, "one.wav", event="event"
    )
    assert (
        result == "actual transcript"
        and calls[0][0] == {"input": 10}
        and calls[0][1]["event"] == "event"
    )
