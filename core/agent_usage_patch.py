"""采集 Agent run 的累计 usage，并按手机工具轮状态交付正文。

AstrBot 4.29.1 中：

* 普通对话走 internal pipeline，run 结束会异步写 ``provider_stats``（含全部工具轮
  的累计 usage），可据此精确对账；
* cron 主动任务（``CronJobManager._woke_main_agent``）直接调用
  ``runner.step_until_done``，**不会写 provider_stats**，因此刷手机/随机/持续刷/
  play/后台任务等自主任务无法从数据库对账。

本模块在进程内对 ``ToolLoopAgentRunner.step`` 做一次透明包装：每个 step 生成器
结束后，把 ``runner.stats.token_usage``（该 run 内每次 ``llm_response.usage`` 的
累计）写到事件的 ``phone_agent_usage`` extra 上，供插件对账使用；只有本次 run
被插件标记为手机相关（``phone_usage_tracked``）时才写，避免影响普通对话。

授权手机会话的文本先缓冲到单轮结束；手机工具轮保持静默，最终回复按交付状态输出。
统计和工具事件原样传递，异常照常抛出。包装只安装一次，卸载时还原原方法。
"""

from __future__ import annotations

from typing import Any, Callable

PATCH_FLAG = "_astrbot_phone_control_usage_patch"
ORIGINAL_ATTR = "_astrbot_phone_control_original_step"
USAGE_EXTRA = "phone_agent_usage"
PREPARE_ATTR = "_astrbot_phone_control_prepare_runner"


def _usage_payload(stats: Any, provider: Any = None) -> dict[str, Any] | None:
    """把 AgentStats.token_usage 转成可序列化字典（异常返回 None）。"""
    try:
        usage = getattr(stats, "token_usage", None)
        if usage is None:
            return None
        input_other = int(getattr(usage, "input_other", 0) or 0)
        input_cached = int(getattr(usage, "input_cached", 0) or 0)
        output = int(getattr(usage, "output", 0) or 0)
        model = ""
        get_model = getattr(provider, "get_model", None)
        if callable(get_model):
            try:
                model = str(get_model() or "")
            except Exception:  # noqa: BLE001 - 模型名仅用于日志
                model = ""
        if input_other + input_cached + output <= 0:
            return None
        return {
            "input_other": input_other,
            "input_cached": input_cached,
            "output": output,
            "model": model,
            "source": "agent_runner_stats",
        }
    except Exception:  # noqa: BLE001 - 统计读取失败返回 None
        return None


def install_agent_usage_capture(
    logger: Callable[[str], None] | None = None,
    should_capture: Callable[[Any], bool] | None = None,
    prepare_runner: Callable[[Any], None] | None = None,
) -> bool:
    """安装 AgentRunner 累计 usage 采集（幂等）；成功返回 True。

    Args:
        logger: 日志回调（可选）。
        prepare_runner: 每次执行前准备当前请求，适用于原生cron与普通对话。
        should_capture: 判断某个事件是否采体的回调；默认只看
            ``phone_usage_tracked`` extra（普通对话路径）。cron 主动任务不经过
            pipeline 的 on_llm_request 钩子，需由插件注入按 cron payload origin
            判断的回调。
    """
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
    except Exception:  # noqa: BLE001 - AstrBot 版本差异时静默降级为 DB 对账
        return False
    if prepare_runner is not None:
        setattr(ToolLoopAgentRunner, PREPARE_ATTR, prepare_runner)
    if getattr(ToolLoopAgentRunner, PATCH_FLAG, False):
        return True
    original = ToolLoopAgentRunner.step
    setattr(ToolLoopAgentRunner, ORIGINAL_ATTR, original)

    async def step_with_usage_capture(self: Any, *args: Any, **kwargs: Any):
        prepare = getattr(ToolLoopAgentRunner, PREPARE_ATTR, None)
        if prepare is not None:
            prepare(self)
        generator = original(self, *args, **kwargs)
        event = getattr(getattr(self.run_context, "context", None), "event", None)
        buffer_output = bool(
            event
            and (
                event.get_extra("phone_quiet_run")
                or event.get_extra("phone_output_candidate")
            )
        )
        pending = []
        try:
            async for item in generator:
                if buffer_output and getattr(item, "type", "") in {
                    "streaming_delta",
                    "llm_result",
                }:
                    pending.append(item)
                else:
                    yield item
            # 工具轮正文是执行过程；只交付已完成任务的最终回复。
            quiet = bool(event and event.get_extra("phone_quiet_run"))
            if pending and (
                not quiet
                or (self.done() and not event.get_extra("phone_shared_current_run"))
            ):
                for item in pending:
                    yield item
        finally:
            try:
                event = getattr(
                    getattr(self.run_context, "context", None), "event", None
                )
                try:
                    capture = event is not None and (
                        bool(should_capture(event))
                        if should_capture is not None
                        else bool(event.get_extra("phone_usage_tracked"))
                    )
                except Exception:  # noqa: BLE001 - 判断失败按不采集处理
                    capture = False
                if capture:
                    payload = _usage_payload(
                        getattr(self, "stats", None),
                        getattr(self, "provider", None),
                    )
                    if payload:
                        event.set_extra(USAGE_EXTRA, payload)
            except Exception:  # noqa: BLE001 - 采集失败不影响 Agent 结果
                pass
            try:
                await generator.aclose()
            except Exception:  # noqa: BLE001 - 已结束的生成器关闭失败可忽略
                pass

    setattr(ToolLoopAgentRunner, "step", step_with_usage_capture)
    setattr(ToolLoopAgentRunner, PATCH_FLAG, True)
    if logger is not None:
        try:
            logger("AgentRunner usage 采集已安装（用于自主任务 token 对账）")
        except Exception:  # noqa: BLE001 - 日志失败不影响安装
            pass
    return True


def uninstall_agent_usage_capture() -> None:
    """还原 AgentRunner.step（插件卸载时调用）；未安装时忽略。"""
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
    except Exception:  # noqa: BLE001
        return
    if not getattr(ToolLoopAgentRunner, PATCH_FLAG, False):
        return
    original = getattr(ToolLoopAgentRunner, ORIGINAL_ATTR, None)
    if original is not None:
        setattr(ToolLoopAgentRunner, "step", original)
    try:
        delattr(ToolLoopAgentRunner, ORIGINAL_ATTR)
    except AttributeError:
        pass
    if hasattr(ToolLoopAgentRunner, PREPARE_ATTR):
        delattr(ToolLoopAgentRunner, PREPARE_ATTR)
    setattr(ToolLoopAgentRunner, PATCH_FLAG, False)
