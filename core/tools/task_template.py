"""用户确认任务模板的查询、草案与参数调用。"""
from __future__ import annotations

from pydantic import Field
from pydantic.dataclasses import dataclass

from ..errors import PhoneControlError
from ..ui_snapshot import event_owner
from .base import _PhoneToolBase, _PermissionDenied, _event_of, _is_cron_event, _json_text, _wrap_call, check_admin_permission
from .task import PhoneStartBackgroundTaskTool


def public_template(item):
    return {key:item[key] for key in ('name','instruction','parameters','revision','confirmed_at','status','confirm_command') if key in item}


@dataclass
class PhoneTaskTemplateTool(_PhoneToolBase):
    name: str = 'phone_task_template'
    description: str = (
        '管理当前用户会话的参数任务模板。list/show查询；propose传name/instruction/parameters生成草案，'
        '须把完整正文、参数默认值和确认命令展示给用户，由用户发送该命令保存。'
        'render传name/values只预览；run把已确认模板提交手机后台任务并返回job_id；delete删除。'
        '占位符写{app}/{topic}/{count}；parameters为参数名到type/description/default等定义的对象。'
    )
    parameters: dict = Field(default_factory=lambda:{
        'type':'object',
        'properties':{
            'action':{'type':'string','enum':['list','show','propose','render','run','delete'],'default':'list'},
            'name':{'type':'string','description':'模板名，当前用户会话内唯一，1–40字。'},
            'instruction':{'type':'string','description':'1–400字模板，参数写{app}；执行后须观察结果并按用户要求附真实链接。'},
            'parameters':{'type':'object','description':'参数名映射到定义；type为string/integer/boolean，可设description/default/minimum/maximum/max_length。','additionalProperties':{'type':'object','properties':{
                'type':{'type':'string','enum':['string','integer','boolean']},
                'description':{'type':'string'},'default':{},'minimum':{'type':'integer'},'maximum':{'type':'integer'},'max_length':{'type':'integer'},
            },'required':['type'],'additionalProperties':False}},
            'values':{'type':'object','description':'实际参数值对象，未传项使用模板默认值。'},
            'offset':{'type':'integer','minimum':0,'default':0,'description':'list 分页起点。'},
            'limit':{'type':'integer','minimum':1,'maximum':50,'default':20,'description':'list 返回数量。'},
        },
    })

    @_wrap_call
    async def call(self,context,**kwargs):
        event=_event_of(context)
        denied=check_admin_permission(event,self.service.config)
        if denied:raise _PermissionDenied(denied)
        if _is_cron_event(event) or self.service.is_task_session(event):
            raise PhoneControlError('任务模板由发起用户会话管理；当前手机任务请直接完成已收到的指令')
        owner=event_owner(event);store=self.service.task_templates
        action=str(kwargs.get('action') or 'list').strip().lower()
        name=kwargs.get('name') or ''
        if action=='list':
            offset,limit=kwargs.get('offset',0),kwargs.get('limit',20)
            if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:
                raise PhoneControlError('list 的 offset 须非负，limit 须为 1–50 的整数')
            items=store.list(owner)
            return _json_text({'templates':[public_template(t) for t in items[offset:offset+limit]],'total':len(items)})
        if action=='show':return _json_text(public_template(store.show(owner,name)))
        if action=='propose':
            draft=store.propose(owner,name,kwargs.get('instruction'),kwargs.get('parameters',{}))
            return _json_text(dict(public_template(draft),message='请向用户完整展示草案、参数与默认值。用户发送确认命令后才可调用；草案24小时有效。'))
        if action=='delete':
            store.delete(owner,name)
            return _json_text({'status':'deleted','name':name})
        if action not in {'render','run'}:raise PhoneControlError('action只能是list/show/propose/render/run/delete；保存须由用户发送确认命令')
        instruction=store.render(owner,name,kwargs.get('values',{}))
        if action=='render':return _json_text({'status':'preview','name':name,'instruction':instruction})
        return await PhoneStartBackgroundTaskTool(service=self.service).call(context,instruction=instruction)
