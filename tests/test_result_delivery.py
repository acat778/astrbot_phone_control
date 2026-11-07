"""多条用户任务在任务包装、指引与真实分享回执间保持一致。"""
import asyncio

from core.result_delivery import DELIVERY_GUIDANCE, SHARE_RECEIPT
from core.scheduler import build_background_task_note


def test_background_payload_preserves_requested_results_and_default_is_conditional():
    instruction='搜索节点定位，分别整理前三条，每条标题和完整原文链接独立交付。'
    note=build_background_task_note(instruction,'webchat:FriendMessage:1')
    assert instruction in note
    assert DELIVERY_GUIDANCE in note
    assert '未指定交付形式时' in note
    assert '完整原文链接单独放 link' in note
    assert '完成后用 phone_share_to_user 汇报结果' not in note


def test_child_request_and_global_guidance_share_same_delivery_contract(tmp_path,monkeypatch):
    from test_main_0130_features import FakeEvent,_cleanup,_make_plugin
    from test_subagent import configure,request
    async def run():
        plugin=_make_plugin(tmp_path,monkeypatch);configure(plugin)
        try:
            child=FakeEvent(platform='cron',umo='cron:OtherMessage:delivery-test',
                            payload={'cron_payload':{'origin':'phone_background_task','work_as_subagent':True,'play_task':True}})
            req=request(plugin);await plugin.inject_phone_guidance(child,req)
            parts=''.join(str(getattr(p,'text',p)) for p in req.extra_user_content_parts)
            assert DELIVERY_GUIDANCE in parts
            shared=plugin.inject_phone_guidance.__globals__.get('_GUIDANCE')
            if shared is None:
                from core.plugin.shared import _GUIDANCE
                shared=_GUIDANCE
            assert DELIVERY_GUIDANCE in shared and '完成后仅一次简短结果' not in shared
        finally:await _cleanup(plugin)
    asyncio.run(run())


def test_multiple_actual_shares_each_acknowledge_only_its_own_delivery(tmp_path,monkeypatch):
    from test_main_0130_features import FakeEvent,FakeShareToolContext,FakeStarContext,_cleanup,_make_plugin
    async def run():
        plugin=_make_plugin(tmp_path,monkeypatch)
        try:
            event=FakeEvent()
            star=FakeStarContext()
            context=FakeShareToolContext(event,star)
            tool=next(t for t in plugin.context.tools if t.name=='phone_share_to_user')
            for index in range(3):
                result=await tool.call(context,text=f'第{index+1}条',link=f'https://example.com/result/{index}',include_screenshot=False)
                assert str(result).startswith('已分享'),result
                assert SHARE_RECEIPT in result and '本条结果已送达' in result
            assert len(star.sent)==3
        finally:await _cleanup(plugin)
    asyncio.run(run())
