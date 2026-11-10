"""将 PCM WAV 按时长和编码后体积约束分片，完整保留采样顺序。"""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import wave

from .errors import PhoneControlError


@contextmanager
def audio_parts(audio):
    """每片最多 30 秒、6 MiB 原始数据，Base64 后小于 10 MB。"""
    path = Path(audio)
    if not path.is_file():
        yield [path]
        return
    with path.open("rb") as handle:
        header = handle.read(12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        yield [path]
        return
    try:
        with wave.open(str(path), "rb") as source:
            frame_size = source.getnchannels() * source.getsampwidth()
            frames_per_part = min(source.getframerate() * 30, (6 * 1024 * 1024 - 44) // frame_size)
            if frames_per_part <= 0:
                raise PhoneControlError("语音音频采样格式无效")
            if source.getnframes() <= frames_per_part:
                yield [path]
                return
            with tempfile.TemporaryDirectory(prefix="asr-parts-", dir=path.parent) as directory:
                def parts():
                    remaining = source.getnframes()
                    index = 0
                    while remaining:
                        count = min(remaining, frames_per_part)
                        data = source.readframes(count)
                        if len(data) != count * frame_size:
                            raise PhoneControlError("语音 WAV 数据不完整")
                        target = Path(directory) / f"part-{index}.wav"
                        with wave.open(str(target), "wb") as output:
                            output.setparams(source.getparams())
                            output.writeframes(data)
                        yield target
                        target.unlink(missing_ok=True)
                        remaining -= count
                        index += 1
                yield parts()
    except (wave.Error, EOFError) as exc:
        raise PhoneControlError("语音 WAV 格式无效") from exc


async def transcribe_audio(provider, audio):
    """顺序转写全部分片，任一分片失败时保留失败语义。"""
    texts = []
    with audio_parts(audio) as parts:
        for part in parts:
            text = await provider.get_text(str(part))
            if not str(text or "").strip():
                raise PhoneControlError("语音模型返回空转写")
            texts.append(str(text).strip())
    return "\n".join(texts)
