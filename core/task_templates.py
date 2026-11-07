"""参数化手机任务的草案、用户确认与持久化。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import string
import time
import uuid

from .config_storage import ConfigStorageError, MAX_CONFIG_BYTES, _atomic_write, _read
from .errors import PhoneControlError

_FORMAT = string.Formatter()
_KEY = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,31}$')


class TaskTemplateError(PhoneControlError):
    """模板、参数或确认请求无效。"""


def owner_key(owner: tuple[str, str]) -> str:
    if len(owner) != 2 or not all(owner):
        raise TaskTemplateError('任务模板需要有效的用户会话')
    return hashlib.sha256(json.dumps(owner, ensure_ascii=False).encode()).hexdigest()


def _value(name, value, spec):
    kind = spec['type']
    if kind == 'string':
        if not isinstance(value, str) or not value.strip() or len(value) > spec.get('max_length', 100):
            raise TaskTemplateError(f'参数 {name} 需要非空字符串，长度上限 {spec.get("max_length",100)}')
    elif kind == 'integer':
        if type(value) is not int or not spec.get('minimum', -1000000) <= value <= spec.get('maximum', 1000000):
            raise TaskTemplateError(f'参数 {name} 需要在声明范围内的整数')
    elif kind == 'boolean' and type(value) is not bool:
        raise TaskTemplateError(f'参数 {name} 需要布尔值')
    return value


def validate_definition(name, instruction, parameters):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or any(ord(c) < 32 for c in name):
        raise TaskTemplateError('模板名称需要 1–40 个可见字符')
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 400:
        raise TaskTemplateError('模板正文需要 1–400 字')
    if not isinstance(parameters, dict) or len(parameters) > 12:
        raise TaskTemplateError('parameters 需要对象，最多 12 个参数')
    parameters = copy.deepcopy(parameters)
    for key, spec in parameters.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(spec, dict):
            raise TaskTemplateError('参数名须为字母开头的标识符，参数定义须为对象')
        if set(spec) - {'type','description','default','minimum','maximum','max_length'}:
            raise TaskTemplateError(f'参数 {key} 包含未知声明')
        if spec.get('type') not in {'string','integer','boolean'}:
            raise TaskTemplateError(f'参数 {key} 的 type 需要 string/integer/boolean')
        if not isinstance(spec.get('description',''), str) or len(spec.get('description','')) > 200:
            raise TaskTemplateError(f'参数 {key} 的说明过长')
        if any(type(spec[k]) is not int for k in ('minimum','maximum','max_length') if k in spec):
            raise TaskTemplateError(f'参数 {key} 的约束需要整数')
        if ('max_length' in spec and (spec['type'] != 'string' or not 1 <= spec['max_length'] <= 200)
                or any(k in spec for k in ('minimum','maximum')) and spec['type'] != 'integer'
                or spec.get('minimum', -1000000) > spec.get('maximum', 1000000)):
            raise TaskTemplateError(f'参数 {key} 的约束无效')
        if 'default' in spec: _value(key, spec['default'], spec)
    try:
        fields = set()
        for _, field, fmt, conversion in _FORMAT.parse(instruction):
            if field is not None:
                if not _KEY.fullmatch(field) or fmt or conversion:
                    raise TaskTemplateError('占位符只支持 {参数名}，不支持属性、格式或转换表达式')
                fields.add(field)
    except ValueError as exc:
        raise TaskTemplateError('模板占位符格式错误') from exc
    if fields != set(parameters):
        raise TaskTemplateError('正文占位符与 parameters 声明必须一致')
    return {'name':name.strip(), 'instruction':instruction.strip(), 'parameters':parameters}


def render_instruction(template, values):
    definition = validate_definition(template['name'],template['instruction'],template['parameters'])
    if not isinstance(values, dict) or set(values) - set(definition['parameters']):
        raise TaskTemplateError('values 必须是对象且只能包含已声明参数')
    bound = {}
    for key,spec in definition['parameters'].items():
        if key not in values and 'default' not in spec:
            raise TaskTemplateError(f'缺少参数 {key}')
        value = values[key] if key in values else spec['default']
        bound[key] = json.dumps(_value(key,value,spec), ensure_ascii=False)
    output = definition['instruction'].format_map(bound)
    if not 1 <= len(output) <= 500:
        raise TaskTemplateError('展开后的任务须在 500 字以内，请缩短参数或模板正文')
    return output


class TaskTemplateStore:
    def __init__(self, data_dir: Path, *, clock=time.time):
        self.path = Path(data_dir)/'task_templates'/'templates.json'
        self.clock = clock

    def _load(self):
        try:
            state,_ = _read(self.path, optional=True)
        except ConfigStorageError as exc:
            raise TaskTemplateError(f'读取任务模板失败：{exc}') from exc
        if not state:return {'version':1,'templates':{},'drafts':{}}
        if state.get('version') != 1 or not all(isinstance(state.get(k),dict) for k in ('templates','drafts')):
            raise TaskTemplateError('任务模板存储结构无效，请检查数据文件')
        return state

    def _save(self,state):
        body=(json.dumps(state,ensure_ascii=False,indent=2)+'\n').encode()
        if len(body)>MAX_CONFIG_BYTES:raise TaskTemplateError('任务模板存储已满，请删除不需要的模板')
        try:_atomic_write(self.path,body)
        except OSError as exc:raise TaskTemplateError('保存任务模板失败，请检查数据目录权限') from exc

    def list(self,owner):
        scope=owner_key(owner)
        return [copy.deepcopy(t) for t in self._load()['templates'].values() if t['owner']==scope]

    def show(self,owner,name):
        item=next((t for t in self.list(owner) if t['name']==name),None)
        if item is None:raise TaskTemplateError('当前用户会话没有此已确认模板')
        return item

    def propose(self,owner,name,instruction,parameters):
        definition=validate_definition(name,instruction,parameters)
        scope=owner_key(owner);state=self._load()
        state['drafts']={k:v for k,v in state['drafts'].items() if self.clock()-v['created'] <= 86400}
        if len(state['drafts'])>=256:raise TaskTemplateError('待确认草案已满，请先处理已有草案')
        previous=next((t for t in state['templates'].values() if t['owner']==scope and t['name']==definition['name']),None)
        key=uuid.uuid4().hex[:16]
        draft=dict(definition,owner=scope,created=self.clock(),revision=key,
                   expected_revision=previous['revision'] if previous else '')
        state['drafts'][key]=draft;self._save(state)
        return dict(copy.deepcopy(draft),status='pending_confirmation',
                    confirm_command='/phone template confirm '+key)

    def confirm(self,owner,draft_id):
        scope=owner_key(owner);state=self._load();draft=state['drafts'].get(draft_id)
        if draft is None or draft['owner']!=scope or self.clock()-draft['created']>86400:
            raise TaskTemplateError('草案不存在、已过期或不属于当前用户会话')
        validate_definition(draft['name'],draft['instruction'],draft['parameters'])
        key=scope+':'+draft['name'];previous=state['templates'].get(key)
        if (previous['revision'] if previous else '') != draft['expected_revision']:
            raise TaskTemplateError('同名模板已变化，请重新生成草案并确认')
        if previous is None and len(state['templates'])>=500:
            raise TaskTemplateError('模板数量已满，请先删除不需要的模板')
        saved={k:copy.deepcopy(v) for k,v in draft.items() if k!='expected_revision'}
        saved['confirmed_at']=self.clock();state['templates'][key]=saved
        del state['drafts'][draft_id];self._save(state)
        return saved

    def delete(self,owner,name):
        scope=owner_key(owner);state=self._load();key=scope+':'+name
        if key not in state['templates']:raise TaskTemplateError('当前用户会话没有此已确认模板')
        del state['templates'][key]
        state['drafts']={k:v for k,v in state['drafts'].items() if not (v['owner']==scope and v['name']==name)}
        self._save(state)

    def render(self,owner,name,values):
        return render_instruction(self.show(owner,name),values)
