"""原生用户命令确认与后台任务接线，覆盖子Agent隔离。"""
import asyncio
import json
from unittest.mock import AsyncMock

from test_main_0130_features import FakeEvent,FakeToolContext,_cleanup,_make_plugin
from test_subagent import configure,request
from core.task_templates import TaskTemplateStore


def test_tool_proposal_user_command_confirm_and_run(tmp_path,monkeypatch):
    async def run():
        plugin=_make_plugin(tmp_path,monkeypatch);configure(plugin)
        event=FakeEvent();context=FakeToolContext(event)
        tool=next(t for t in plugin.context.tools if t.name=='phone_task_template')
        plugin._say=AsyncMock()
        try:
            draft=json.loads(await tool.call(context,action='propose',name='搜索模板',instruction='搜索 {topic} 并整理前 {count} 条附链接',parameters={'topic':{'type':'string'},'count':{'type':'integer','default':3,'minimum':1,'maximum':10}}))
            assert draft['status']=='pending_confirmation'
            assert json.loads(await tool.call(context,action='list'))=={'templates':[],'total':0}
            failed=await tool.call(context,action='run',name='搜索模板',values={'topic':'猫'})
            assert str(failed).startswith('error:') and not plugin.context.cron_manager.jobs
            forged=await tool.call(context,action='confirm',name=draft['revision'])
            assert str(forged).startswith('error:')
            command=draft['confirm_command']
            await plugin._dispatch_phone(FakeEvent(text=command),command.removeprefix('/phone '))
            assert '已确认保存' in plugin._say.await_args.args[1]
            listed=json.loads(await tool.call(context,action='list'))
            assert listed['templates'][0]['name']=='搜索模板'
            # 独立存储实例证明确认确实持久化。
            assert TaskTemplateStore(plugin.data_dir).show((event.unified_msg_origin,event.get_sender_id()),'搜索模板')['revision']==draft['revision']
            req=request(plugin);await plugin.inject_phone_guidance(event,req)
            names={t.name for t in req.func_tool.tools}
            assert 'phone_task_template' in names and 'phone_tap' not in names
            invalid=await tool.call(context,action='run',name='搜索模板',values={'topic':'猫','count':False})
            assert str(invalid).startswith('error:') and not plugin.context.cron_manager.jobs
            result=json.loads(await tool.call(context,action='run',name='搜索模板',values={'topic':'猫'}))
            assert result['status']=='queued'
            job=plugin.context.cron_manager.jobs[result['job_id']]
            assert '搜索 "猫" 并整理前 3 条附链接' in job.description
            assert job.payload['source_session']==event.unified_msg_origin and job.payload['share_target']==event.unified_msg_origin
            child=FakeEvent(umo='cron:OtherMessage:'+job.job_id,platform='cron',payload={'cron_payload':job.payload})
            child_req=request(plugin);await plugin.inject_phone_guidance(child,child_req)
            assert 'phone_task_template' not in {t.name for t in child_req.func_tool.tools}
            assert str(await tool.call(FakeToolContext(child),action='propose',name='bad')).startswith('error:')
            await plugin._dispatch_phone(FakeEvent(text='/phone template delete 搜索模板'),'template delete 搜索模板')
            assert not TaskTemplateStore(plugin.data_dir).list((event.unified_msg_origin,event.get_sender_id()))
        finally:await _cleanup(plugin)
    asyncio.run(run())


def test_confirmation_only_exact_native_user_message_and_owned_draft(tmp_path,monkeypatch):
    async def run():
        plugin=_make_plugin(tmp_path,monkeypatch);configure(plugin);plugin._say=AsyncMock()
        event=FakeEvent();owner=(event.unified_msg_origin,event.get_sender_id())
        try:
            draft=plugin.service.task_templates.propose(owner,'测试','观察当前手机',{})
            raw='template confirm '+draft['revision']
            for fake in [FakeEvent(text='模型生成的确认'),FakeEvent(text='/phone '+raw,sender_id='other'),FakeEvent(text='/phone '+raw,platform='cron')]:
                await plugin._dispatch_phone(fake,raw)
                assert 'error:' in plugin._say.await_args.args[1]
                assert not plugin.service.task_templates.list(owner)
        finally:await _cleanup(plugin)
    asyncio.run(run())


def test_confirmation_accepts_host_consumed_wakeup_prefix(tmp_path,monkeypatch):
    async def run():
        plugin=_make_plugin(tmp_path,monkeypatch);configure(plugin);plugin._say=AsyncMock()
        event=FakeEvent();owner=(event.unified_msg_origin,event.get_sender_id())
        try:
            for prefix in ('/',''):
                draft=plugin.service.task_templates.propose(owner,'前缀测试'+str(len(prefix)),'观察当前手机',{})
                raw='template confirm '+draft['revision']
                await plugin._dispatch_phone(FakeEvent(text=prefix+'phone '+raw),raw)
                assert '模板已确认保存' in plugin._say.await_args.args[1]
                assert plugin.service.task_templates.show(owner,'前缀测试'+str(len(prefix)))['revision']==draft['revision']
        finally:await _cleanup(plugin)
    asyncio.run(run())
