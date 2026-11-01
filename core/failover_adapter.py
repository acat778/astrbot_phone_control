"""故障切换在模型请求边界的适配；设备工具保持独立执行。"""

from __future__ import annotations

import asyncio
from copy import deepcopy

from .errors import PhoneControlError
from .model_failover import ModelFailover, provider_id, valid_chat


def manager_for(service):
    manager = getattr(service, "model_failover", None)
    if manager is None:
        from astrbot.api import logger

        manager = ModelFailover(
            getattr(service, "app_context", None),
            lambda: getattr(service, "_raw_config", {}),
            log=lambda message: logger.info("%s", message),
        )
        service.model_failover = manager
    return manager


async def generate(service, slot, *, timeout=30, app_context=None, **kwargs):
    context = app_context or service.app_context
    manager = manager_for(service) if service is not None else None
    if manager is None or manager.policy(slot).mode == "disabled":
        return await asyncio.wait_for(context.llm_generate(**kwargs), timeout=timeout)
    primary = kwargs["chat_provider_id"]

    async def request(chosen):
        payload = deepcopy(kwargs)
        payload["chat_provider_id"] = chosen
        return await context.llm_generate(**payload)

    return await manager.run(
        slot, primary, request, timeout=timeout, validate=valid_chat
    )


class FailoverChatProvider:
    """仅代理Agent当前模型请求，不重放工具；共享Provider实例保持原配置。"""

    def __init__(self, manager, primary, *, primary_id="", timeout=60):
        self.manager, self.primary, self.timeout = manager, primary, timeout
        self.primary_id = primary_id or provider_id(primary)

    def __getattr__(self, name):
        current = self.manager.current("task", self.primary_id)
        provider = self.manager.context.get_provider_by_id(current) or self.primary
        return getattr(provider, name)

    async def text_chat(self, **kwargs):
        main = self.primary_id

        async def call(chosen):
            provider = self.manager.context.get_provider_by_id(chosen)
            if provider is None:
                raise PhoneControlError("任务模型不存在")
            payload = dict(kwargs)
            for key in ("contexts", "image_urls", "audio_urls"):
                if key in payload:
                    payload[key] = deepcopy(payload[key])
            # model 是请求级参数，备用使用自身已配置的模型。
            if chosen != main:
                payload.pop("model", None)
            return await provider.text_chat(**payload)

        return await self.manager.run(
            "task", main, call, timeout=self.timeout, validate=valid_chat
        )

    async def text_chat_stream(self, **kwargs):
        # 每次先完成完整模型响应，再交付给Agent；失败响应不泄漏半段正文或工具调用。
        main = self.primary_id

        async def call(chosen):
            provider = self.manager.context.get_provider_by_id(chosen)
            if provider is None:
                raise PhoneControlError("任务模型不存在")
            payload = dict(kwargs)
            for key in ("contexts", "image_urls", "audio_urls"):
                if key in payload:
                    payload[key] = deepcopy(payload[key])
            if chosen != main:
                payload.pop("model", None)
            chunks = []
            stream = provider.text_chat_stream(**payload)
            try:
                async for chunk in stream:
                    chunks.append(chunk)
            finally:
                await stream.aclose()
            if not chunks:
                raise PhoneControlError("模型流式响应为空")
            valid_chat(chunks[-1])
            return chunks

        chunks = await self.manager.run("task", main, call, timeout=self.timeout)
        for chunk in chunks:
            yield chunk


class EmbeddingModelChanged(PhoneControlError):
    """向量模型已切换，当前构建或查询须使用新身份重新开始。"""


class PinnedEmbedding:
    def __init__(self, manager, slot, primary, pinned):
        self.manager, self.slot, self.primary, self.pinned = (
            manager,
            slot,
            primary,
            pinned,
        )

    def __getattr__(self, name):
        return getattr(self.pinned, name)

    @property
    def batch_timeout(self):
        return 120 * max(1, len(self.manager.candidates(self.slot, self.primary))) + 1

    async def get_embedding(self, text):
        raw = self.manager.config_getter()
        budget = (
            float(raw.get("recall", {}).get("timeout_seconds", 3))
            if self.slot == "embedding"
            else 3.0
        )
        attempts = max(1, len(self.manager.candidates(self.slot, self.primary)))
        return (
            await self.get_embeddings(
                [text], timeout=max(0.05, budget / (attempts + 1))
            )
        )[0]

    async def get_embeddings(self, texts, *, timeout=120):
        from .semantic import normalize_vector

        async def call(chosen):
            provider = self.manager.context.get_provider_by_id(chosen)
            if provider is None:
                raise PhoneControlError("向量模型不存在")
            many = getattr(provider, "get_embeddings", None)
            values = (
                await many(list(texts))
                if callable(many)
                else [await provider.get_embedding(text) for text in texts]
            )
            if len(values) != len(texts) or any(
                len(normalize_vector(v)) != int(provider.get_dim()) for v in values
            ):
                raise PhoneControlError("向量模型返回非法数量、维度或数值")
            return chosen, values

        chosen, values = await self.manager.run(
            self.slot, self.primary, call, timeout=timeout
        )
        if chosen != provider_id(self.pinned):
            raise EmbeddingModelChanged("向量模型已切换，按新模型身份重建索引")
        return values
