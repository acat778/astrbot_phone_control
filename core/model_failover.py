"""按用途隔离的模型故障切换、候选选择与主模型恢复探测。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import PhoneControlError

SLOTS = {
    "task": ("model", "task_failover", "chat"),
    "vision": ("model", "vision_failover", "chat"),
    "video": ("model", "video_failover", "chat"),
    "asr": ("model", "asr_failover", "stt"),
    "app_embedding": ("model", "app_embedding_failover", "embedding"),
    "embedding": ("memory", "embedding_failover", "embedding"),
}


@dataclass(frozen=True)
class FailoverPolicy:
    mode: str = "disabled"
    suppliers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    recovery_seconds: int = 300

    @classmethod
    def parse(cls, raw):
        raw = raw if isinstance(raw, Mapping) else {}
        mode = str(raw.get("mode", "disabled"))
        if mode not in {"disabled", "suppliers", "models"}:
            raise ValueError("故障切换方式须为 disabled、suppliers 或 models")

        def choices(key):
            values = raw.get(key, [])
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(x, str) for x in values
            ):
                raise ValueError("故障切换候选必须是字符串列表")
            return tuple(dict.fromkeys(x.strip() for x in values if x.strip()))

        seconds = raw.get("recovery_seconds", 300)
        if (
            isinstance(seconds, bool)
            or not str(seconds).isdigit()
            or not 5 <= int(seconds) <= 86400
        ):
            raise ValueError("主模型恢复探测周期须为 5–86400 秒的整数")
        return cls(mode, choices("suppliers"), choices("models"), int(seconds))


def provider_id(provider):
    config = getattr(provider, "provider_config", {})
    return str(config.get("id") or getattr(provider, "provider_id", "") or "")


def supplier_id(provider):
    config = getattr(provider, "provider_config", {})
    return str(config.get("provider_source_id") or config.get("id") or "")


def providers_for(context, kind):
    name = {
        "chat": "get_all_providers",
        "stt": "get_all_stt_providers",
        "embedding": "get_all_embedding_providers",
    }[kind]
    getter = getattr(context, name, None)
    return list(getter() or []) if callable(getter) else []


def valid_chat(result):
    if getattr(result, "role", "") == "err" or not (
        str(getattr(result, "completion_text", "") or "").strip()
        or getattr(result, "tools_call_name", None)
        or getattr(result, "tools_call_ids", None)
    ):
        raise PhoneControlError("模型未返回有效正文或工具调用")
    return result


@dataclass
class RouteState:
    policy: FailoverPolicy
    primary: str
    active: str = ""
    retry_at: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failed: dict[str, float] = field(default_factory=dict)


class ModelFailover:
    def __init__(self, context, config_getter, *, log=None, clock=time.monotonic):
        self.context = context
        self.config_getter = config_getter
        self.log = log or (lambda message: None)
        self.clock = clock
        self.routes = {}
        self.on_change = None
        self._task = None

    def policy(self, slot):
        group, key, _ = SLOTS[slot]
        return FailoverPolicy.parse(self.config_getter().get(group, {}).get(key, {}))

    def state(self, slot, primary):
        policy = self.policy(slot)
        key = (slot, primary)
        state = self.routes.get(key)
        if state is None or state.policy != policy:
            state = self.routes[key] = RouteState(policy, primary, primary)
        return state

    def candidates(self, slot, primary, policy=None):
        policy = policy or self.policy(slot)
        if policy.mode == "disabled":
            return [primary]
        available = providers_for(self.context, SLOTS[slot][2])
        if slot == "asr":
            available += providers_for(self.context, "chat")
        ids = {provider_id(p) for p in available}
        if policy.mode == "models":
            extra = [x for x in policy.models if x in ids]
        else:
            extra = [
                provider_id(p)
                for source in policy.suppliers
                for p in available
                if supplier_id(p) == source
            ]
        return list(dict.fromkeys(x for x in [primary, *extra] if x))

    def current(self, slot, primary):
        state = self.state(slot, primary)
        candidates = self.candidates(slot, primary, state.policy)
        return state.active if state.active in candidates else primary

    def _changed(self, slot, state, chosen):
        previous = state.active
        state.active = chosen
        if chosen != state.primary and not state.retry_at:
            state.retry_at = self.clock() + state.policy.recovery_seconds
        if chosen == state.primary:
            state.retry_at = 0
        if previous != chosen:
            self.log(f"模型切换 [{slot}] {previous} → {chosen}")
            if self.on_change:
                self.on_change(slot)

    async def run(self, slot, primary, call, *, timeout=30, validate=None):
        state = self.state(slot, primary)
        async with state.lock:
            candidates = self.candidates(slot, primary, state.policy)
            if state.active in candidates and state.active != primary:
                candidates.remove(state.active)
                candidates.insert(0, state.active)
                if primary in candidates and self.clock() < state.retry_at:
                    candidates.remove(primary)
            last, errors = None, []
            for chosen in candidates:
                if (
                    chosen != state.active
                    and state.failed.get(chosen, 0) > self.clock()
                ):
                    continue
                try:
                    value = await asyncio.wait_for(
                        call(chosen), timeout=max(0.01, timeout)
                    )
                    if validate:
                        validate(value)
                except Exception as exc:
                    last = exc
                    errors.append(f"{chosen}: {type(exc).__name__}")
                    state.failed[chosen] = self.clock() + state.policy.recovery_seconds
                    if chosen == primary:
                        state.retry_at = self.clock() + state.policy.recovery_seconds
                    if state.policy.mode == "disabled":
                        raise
                    self.log(f"模型调用失败 [{slot}] {chosen}: {type(exc).__name__}")
                    continue
                state.failed.pop(chosen, None)
                self._changed(slot, state, chosen)
                return value
            raise PhoneControlError(
                "配置的模型候选均不可用：" + ("；".join(errors) or "候选处于故障冷却期")
            ) from last

    async def probe_due(self):
        for (slot, primary), state in list(self.routes.items()):
            if (
                self.state(slot, primary) is not state
                or state.policy.mode == "disabled"
            ):
                continue
            if (
                state.active == primary
                or self.clock() < state.retry_at
                or state.lock.locked()
            ):
                continue
            async with state.lock:
                try:
                    provider = self.context.get_provider_by_id(primary)
                    if provider is None:
                        raise PhoneControlError("主模型不存在")

                    async def check():
                        if SLOTS[slot][2] == "chat" and callable(
                            getattr(provider, "text_chat", None)
                        ):
                            valid_chat(
                                await provider.text_chat(
                                    prompt="Reply OK only.", request_max_retries=1
                                )
                            )
                        elif SLOTS[slot][2] == "embedding":
                            from .semantic import normalize_vector

                            vector = await provider.get_embedding("health")
                            if len(normalize_vector(vector)) != int(provider.get_dim()):
                                raise PhoneControlError("主向量模型探测结果非法")
                        else:
                            await provider.test()

                    await asyncio.wait_for(
                        check(), timeout=min(30, state.policy.recovery_seconds)
                    )
                except Exception as exc:
                    self.log(f"主模型探测失败 [{slot}] {primary}: {type(exc).__name__}")
                    state.retry_at = self.clock() + state.policy.recovery_seconds
                else:
                    state.failed.pop(primary, None)
                    self._changed(slot, state, primary)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch(), name="phone_model_recovery")

    async def _watch(self):
        while True:
            await asyncio.sleep(1)
            await self.probe_due()

    async def close(self):
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.routes.clear()
