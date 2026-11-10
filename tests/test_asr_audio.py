"""重放长录音的实际请求体大小，验证采样完整性和失败清理。"""

import asyncio
import base64
from types import SimpleNamespace
import wave

import pytest

from core.asr_audio import transcribe_audio
from core.media_provider import MediaProviders


def recording(path, seconds):
    data = b"\x12\x34\x56\x78" * (48000 * seconds)
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(data)
    return data


@pytest.mark.parametrize("seconds", [40, 60])
@pytest.mark.parametrize("entry", ["transcribe", "generate"])
def test_long_stt_requests_keep_all_samples_under_encoded_limit(tmp_path, seconds, entry):
    async def scenario():
        path = tmp_path / "clip.wav"
        data = recording(path, seconds)
        assert len(base64.b64encode(path.read_bytes())) > 10_000_000
        samples, paths = [], []

        async def get_text(filename):
            part = type(path)(filename)
            paths.append(part)
            assert len(base64.b64encode(part.read_bytes())) < 10_000_000
            with wave.open(filename, "rb") as source:
                samples.append(source.readframes(source.getnframes()))
            return f"片段{len(samples)}"

        provider = SimpleNamespace(get_text=get_text, provider_config={"id": "asr"})
        manager = MediaProviders(SimpleNamespace(get_provider_by_id=lambda _: provider))
        if entry == "transcribe":
            text = await manager.transcribe(provider, path)
        else:
            result = await manager.generate(chat_provider_id="asr", audio_urls=[str(path)])
            text = result.completion_text
        assert text == "片段1\n片段2"
        assert b"".join(samples) == data
        assert all(not part.exists() for part in paths)
        assert list(tmp_path.iterdir()) == [path]
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), asyncio.CancelledError()])
def test_failure_and_cancel_remove_parts_without_returning_partial_text(tmp_path, failure):
    async def scenario():
        path = tmp_path / "clip.wav"
        recording(path, 60)
        calls = []

        async def get_text(filename):
            calls.append(filename)
            if len(calls) == 2:
                raise failure
            return "第一段"

        with pytest.raises(type(failure)):
            await transcribe_audio(SimpleNamespace(get_text=get_text), path)
        assert len(calls) == 2
        assert list(tmp_path.iterdir()) == [path]
    asyncio.run(scenario())
