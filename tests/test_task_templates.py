"""模板确认、严格参数展开和重载持久化。"""
import json

import pytest
from core.task_templates import TaskTemplateError,TaskTemplateStore,render_instruction,validate_definition

OWNER=('webchat:FriendMessage:1','1')
PARAMS={'app':{'type':'string'},'topic':{'type':'string'},
        'count':{'type':'integer','default':3,'minimum':1,'maximum':10}}
BODY='去 {app} 搜 {topic}，整理前 {count} 条并附真实链接'


def test_proposal_requires_confirmation_and_survives_reload(tmp_path):
    store=TaskTemplateStore(tmp_path)
    draft=store.propose(OWNER,'搜索整理',BODY,PARAMS)
    assert not store.list(OWNER)
    with pytest.raises(TaskTemplateError,match='已确认'):store.render(OWNER,'搜索整理',{'app':'B站','topic':'猫'})
    saved=TaskTemplateStore(tmp_path).confirm(OWNER,draft['revision'])
    output=TaskTemplateStore(tmp_path).render(OWNER,'搜索整理',{'app':'B站','topic':'猫"\\\n{count}'})
    assert output=='去 "B站" 搜 '+json.dumps('猫"\\\n{count}',ensure_ascii=False)+'，整理前 3 条并附真实链接'
    assert saved['confirmed_at'] and len(store.list(OWNER))==1
    with pytest.raises(TaskTemplateError,match='草案'):store.confirm(OWNER,draft['revision'])
    with pytest.raises(TaskTemplateError):store.show(('webchat:FriendMessage:2','1'),'搜索整理')
    with pytest.raises(TaskTemplateError):store.show((OWNER[0],'other'),'搜索整理')
    store.delete(OWNER,'搜索整理')
    assert not TaskTemplateStore(tmp_path).list(OWNER)


def test_expired_wrong_owner_and_stale_replacement_rejected(tmp_path):
    now=[1000.0];store=TaskTemplateStore(tmp_path,clock=lambda:now[0])
    draft=store.propose(OWNER,'搜索整理',BODY,PARAMS)
    with pytest.raises(TaskTemplateError):store.confirm(('other','user'),draft['revision'])
    now[0]+=86401
    with pytest.raises(TaskTemplateError,match='过期'):store.confirm(OWNER,draft['revision'])
    first=store.propose(OWNER,'搜索整理',BODY,PARAMS)
    second=store.propose(OWNER,'搜索整理',BODY,PARAMS)
    store.confirm(OWNER,first['revision'])
    with pytest.raises(TaskTemplateError,match='已变化'):store.confirm(OWNER,second['revision'])
    third=store.propose(OWNER,'搜索整理',BODY+'。核对来源',PARAMS)
    store.confirm(OWNER,third['revision'])
    assert store.show(OWNER,'搜索整理')['instruction'].endswith('核对来源')


@pytest.mark.parametrize('values',[
    {'app':'B站'}, {'app':'B站','topic':'猫','extra':1},
    {'app':'B站','topic':'猫','count':True}, {'app':'B站','topic':'猫','count':'3'},
    {'app':'B站','topic':'猫','count':0}, {'app':'B站','topic':'猫','count':11},
    {'app':'B站','topic':{'command':'foo'}}, {'app':'B站','topic':'x'*101},
])
def test_invalid_parameters_are_never_rendered(values):
    with pytest.raises(TaskTemplateError):render_instruction({'name':'搜索','instruction':BODY,'parameters':PARAMS},values)


@pytest.mark.parametrize('body,params',[
    ('{app.__class__}',{'app':{'type':'string'}}), ('{app[0]}',{'app':{'type':'string'}}),
    ('{app!r}',{'app':{'type':'string'}}), ('{app:10}',{'app':{'type':'string'}}),
    ('{app}',{}),('固定正文',{'app':{'type':'string'}}), ('{app',PARAMS),
    ('{app}',{'app':{'type':'integer','default':True}}),
    ('{app}',{'app':{'type':'string','max_length':0}}),
])
def test_invalid_definition_rejected(body,params):
    with pytest.raises(TaskTemplateError):validate_definition('模板',body,params)


def test_bad_storage_not_overwritten(tmp_path):
    store=TaskTemplateStore(tmp_path);store.path.parent.mkdir(parents=True);store.path.write_text('{broken')
    with pytest.raises(TaskTemplateError,match='读取'):store.propose(OWNER,'搜索整理',BODY,PARAMS)
    assert store.path.read_text()=='{broken'
