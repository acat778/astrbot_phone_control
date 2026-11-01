"""媒体模型传输：保持模型身份，为原音频选择可表达该模态的协议。"""

from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from .errors import PhoneControlError


def audio_adapter_config(provider):
    """小米 Token Plan 原生音频使用同源 OpenAI 协议，保留认证和模型参数。"""
    config = deepcopy(provider.provider_config)
    parts = urlsplit(str(config.get("api_base") or ""))
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not parts.hostname.endswith(".xiaomimimo.com")
        or parts.path.rstrip("/") != "/anthropic"
    ):
        raise PhoneControlError("小米音频接口地址未通过同源协议校验")
    config["api_base"] = urlunsplit((parts.scheme, parts.netloc, "/v1", "", ""))
    config["type"] = "openai_chat_completion"
    extra = deepcopy(config.get("custom_extra_body") or {})
    extra["thinking"] = {"type": "disabled"}
    config["custom_extra_body"] = extra
    return config


def audio_transport(provider):
    config = getattr(provider, "provider_config", {})
    modalities = config.get("modalities")
    if isinstance(modalities, list) and "audio" not in modalities:
        return "transcript"
    names = {base.__name__ for base in type(provider).__mro__}
    if "ProviderXiaomiTokenPlan" in names:
        return "xiaomi_openai"
    config = getattr(provider, "provider_config", {})
    host = urlsplit(str(config.get("api_base") or "")).hostname or ""
    if "ProviderOpenAIOfficial" in names and (
        host == "dashscope.aliyuncs.com"
        or host.endswith(".dashscope.aliyuncs.com")
        or host.endswith(".maas.aliyuncs.com")
    ):
        return "aliyun_openai"
    if "ProviderAnthropic" in names:
        return "transcript"
    return "native"


class MediaProviders:
    def __init__(self, context, service=None):
        self.context = context
        self.service = service
        self.adapters = {}

    def provider(self, provider_id):
        getter = getattr(self.context, "get_provider_by_id", None)
        provider = getter(provider_id) if callable(getter) else None
        if provider is None and self.service is not None:
            from .failover_adapter import manager_for
            from types import SimpleNamespace

            manager = manager_for(self.service)
            model = self.service.config.model
            slots = (("video", model.video_provider_id), ("asr", model.asr_provider_id))
            if any(
                provider_id == primary
                and manager.policy(slot).mode != "disabled"
                and len(manager.candidates(slot, primary)) > 1
                for slot, primary in slots
            ):
                return SimpleNamespace(provider_config={"id": provider_id})
        return provider

    async def generate(self, **kwargs):
        from .failover_adapter import manager_for
        from .model_failover import valid_chat

        slot = kwargs.pop(
            "_slot",
            "video"
            if kwargs.get("video_path")
            else "asr"
            if kwargs.get("audio_urls") and not kwargs.get("image_urls")
            else "vision",
        )
        if (
            self.service is None
            or manager_for(self.service).policy(slot).mode == "disabled"
        ):
            return await self._generate_one(**kwargs)
        manager = manager_for(self.service)
        primary = kwargs["chat_provider_id"]
        timeout = (
            self.service.config.media.asr_timeout_seconds
            if slot == "asr"
            else self.service.config.media.vision_timeout_seconds
        )

        async def call(chosen):
            payload = deepcopy(kwargs)
            payload["chat_provider_id"] = chosen
            return await self._generate_one(**payload)

        return await manager.run(
            slot, primary, call, timeout=timeout, validate=valid_chat
        )

    async def transcribe(self, provider, audio):
        from .failover_adapter import manager_for
        from .model_failover import provider_id

        primary = provider_id(provider)
        if (
            self.service is None
            or manager_for(self.service).policy("asr").mode == "disabled"
        ):
            return await provider.get_text(str(audio))

        async def call(chosen):
            selected = self.provider(chosen)
            if selected is None:
                raise PhoneControlError("语音模型不存在")
            get_text = getattr(selected, "get_text", None)
            if callable(get_text):
                result = await get_text(str(audio))
            else:
                response = await self._generate_one(
                    chat_provider_id=chosen,
                    prompt="请准确转写音频中的人声，不补充未听到的内容。",
                    audio_urls=[str(audio)],
                )
                result = getattr(response, "completion_text", "")
            if not str(result or "").strip():
                raise PhoneControlError("语音模型返回空转写")
            return result

        return await manager_for(self.service).run(
            "asr", primary, call, timeout=self.service.config.media.asr_timeout_seconds
        )

    async def _generate_one(self, **kwargs):
        provider_id = kwargs["chat_provider_id"]
        selected = self.provider(provider_id)
        video_path = kwargs.pop("video_path", None)
        if video_path is not None:
            from pathlib import Path
            from .media_video import video_part

            names = {base.__name__ for base in type(selected).__mro__}
            if "ProviderOpenAIOfficial" not in names:
                raise PhoneControlError(
                    "视频识别模型需使用支持 video_url 的 OpenAI 兼容 Provider"
                )
            kwargs["contexts"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": kwargs.pop("prompt", "")},
                        video_part(Path(video_path)),
                    ],
                }
            ]
            kwargs["prompt"] = None
        transport = audio_transport(selected)
        if kwargs.get("audio_urls") and transport == "transcript":
            raise PhoneControlError("候选模型不支持原音频输入")
        if (kwargs.get("audio_urls") or video_path is not None) and transport in {
            "xiaomi_openai",
            "aliyun_openai",
        }:
            adapter = self.adapters.get(provider_id)
            if adapter is None:
                from astrbot.core.provider.sources.openai_source import (
                    ProviderOpenAIOfficial,
                )

                if transport == "aliyun_openai":

                    class AliyunAudioProvider(ProviderOpenAIOfficial):
                        async def _resolve_audio_part(self, audio_ref):
                            from astrbot.core.provider.sources.openai_source import (
                                resolve_media_ref_to_base64_data,
                            )

                            audio = await resolve_media_ref_to_base64_data(
                                audio_ref, media_type="audio", strict=True
                            )
                            if not audio or not audio.format:
                                raise PhoneControlError("阿里云原音频预处理结果为空")
                            return {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:audio/{audio.format};base64,{audio.base64_data}",
                                    "format": audio.format,
                                },
                            }

                    config = deepcopy(selected.provider_config)
                    extra = deepcopy(config.get("custom_extra_body") or {})
                    extra["modalities"] = ["text"]
                    config["custom_extra_body"] = extra
                    adapter = AliyunAudioProvider(
                        config, getattr(selected, "provider_settings", {})
                    )
                else:
                    adapter = ProviderOpenAIOfficial(
                        audio_adapter_config(selected),
                        getattr(selected, "provider_settings", {}),
                    )
                self.adapters[provider_id] = adapter
            payload = {k: v for k, v in kwargs.items() if k != "chat_provider_id"}
            return await adapter.text_chat(**payload)
        return await self.context.llm_generate(**kwargs)

    async def close(self):
        adapters = list(self.adapters.values())
        self.adapters.clear()
        for adapter in adapters:
            await adapter.terminate()
