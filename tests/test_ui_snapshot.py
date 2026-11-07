"""编号定位绑定真实渲染、会话与观察，拒绝任何失效目标。"""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.models import Rect, ScreenSnapshot, UiNode
from core.ui import render_ui
from core.ui_snapshot import SnapshotTargetError, SnapshotTargets


def page():
    return ScreenSnapshot(package='com.example', activity='Main', width=720, height=1280, nodes=[
        UiNode(ref=1, package='com.example', text='同名"按钮\\'+'长文'*100, clickable=True, bounds=Rect(10,100,310,200)),
        UiNode(ref=21, package='com.example', text='同名"按钮\\'+'长文'*100, clickable=True, bounds=Rect(10,300,310,400)),
    ])


def test_exposed_refs_bound_to_owner_device_and_exact_page():
    now=[0.0];store=SnapshotTargets(clock=lambda:now[0],limit=2)
    snapshot=page();owner=('session','user');device=('device',)
    store.expose(snapshot,render_ui(snapshot,max_nodes=1),owner,device)
    record,node=store.get(snapshot.snapshot_id,1,owner,device)
    assert node.text==snapshot.nodes[0].text
    with pytest.raises(SnapshotTargetError,match='未在此快照中展示'):
        store.get(snapshot.snapshot_id,21,owner,device)
    for other_owner,other_device in [(('session','other'),device),(('other','user'),device),(owner,('other',))]:
        with pytest.raises(SnapshotTargetError,match='不属于'):
            store.get(snapshot.snapshot_id,1,other_owner,other_device)
    store.validate(snapshot.snapshot_id,record,copy.deepcopy(snapshot))
    changed=copy.deepcopy(snapshot);changed.nodes.reverse()
    with pytest.raises(SnapshotTargetError,match='页面已变化'):
        store.validate(snapshot.snapshot_id,record,changed)
    store.expose(snapshot,render_ui(snapshot),owner,device);now[0]=61
    with pytest.raises(SnapshotTargetError,match='过期'):
        store.get(snapshot.snapshot_id,1,owner,device)


@pytest.mark.parametrize('ref',[True,-1,'21',2.1])
def test_invalid_node_number_rejected(ref):
    with pytest.raises(SnapshotTargetError,match='非负整数'):
        SnapshotTargets().get('unknown',ref,('s','u'),('d',))


def test_records_bounded_and_ids_unique():
    store=SnapshotTargets(limit=2);snapshots=[page() for _ in range(3)]
    assert len({p.snapshot_id for p in snapshots})==3
    for snapshot in snapshots:store.expose(snapshot,render_ui(snapshot),('s','u'),('d',))
    assert len(store.records)==2 and snapshots[0].snapshot_id not in store.records


@pytest.mark.parametrize('long_press',[False,True])
def test_tool_same_name_uses_selected_number_and_consumes_snapshot(tmp_path,monkeypatch,long_press):
    from core.service import PhoneService
    from core.tools.device import PhoneScreenTool,PhoneTapTool,PhoneLongPressTool
    from test_main_0130_features import FakeEvent,FakeToolContext
    async def run():
        actions=SimpleNamespace(screen_size=AsyncMock(return_value=(720,1280)),tap=AsyncMock(),long_press=AsyncMock())
        adb=SimpleNamespace(ensure_ready=AsyncMock())
        service=PhoneService(raw_config={'adb':{'host':'dummy','port':5555}},adb=adb,actions=actions,diary=None,data_dir=tmp_path)
        service._control_backend_or_none=AsyncMock(return_value=None)
        current=page();monkeypatch.setattr('core.service.dump_ui',AsyncMock(return_value=current))
        context=FakeToolContext(FakeEvent())
        output=await PhoneScreenTool(service=service).call(context,include_traffic=False,include_screenshot=False)
        assert 'snapshot_id='+current.snapshot_id in output
        tool=(PhoneLongPressTool if long_press else PhoneTapTool)(service=service)
        result=await tool.call(context,snapshot_id=current.snapshot_id,node_ref=21)
        assert not str(result).startswith('error:'),result
        action=actions.long_press if long_press else actions.tap
        assert action.await_args.args==(160,350)
        again=await tool.call(context,snapshot_id=current.snapshot_id,node_ref=21)
        assert str(again).startswith('error:') and action.await_count==1
    asyncio.run(run())


@pytest.mark.parametrize('change',['label','position','foreground','removed','concurrent','during_backend'])
def test_changed_page_never_taps(tmp_path,monkeypatch,change):
    from core.service import PhoneService
    from core.tools.device import PhoneTapTool
    from test_main_0130_features import FakeEvent,FakeToolContext
    from core.ui_snapshot import event_owner
    async def run():
        actions=SimpleNamespace(screen_size=AsyncMock(return_value=(720,1280)),tap=AsyncMock())
        service=PhoneService(raw_config={'adb':{'host':'dummy','port':5555}},adb=object(),actions=actions,diary=None,data_dir=tmp_path)
        context=FakeToolContext(FakeEvent());snapshot=page()
        service.expose_snapshot(snapshot,render_ui(snapshot),event_owner(context.context.event))
        current=copy.deepcopy(snapshot)
        if change=='label': current.nodes[0].text='换了页面'
        if change=='position': current.nodes[1].bounds=Rect(10,500,310,600)
        if change=='foreground': current.package='other'
        if change=='removed':current.nodes.pop()
        async def dump(*args,**kwargs):
            if change=='concurrent': service._invalidate_ui_cache()
            return current
        monkeypatch.setattr('core.service.dump_ui',dump)
        if change=='during_backend':
            async def backend():
                service._invalidate_ui_cache()
                return None
            service._control_backend_or_none=backend
        result=await PhoneTapTool(service=service).call(context,snapshot_id=snapshot.snapshot_id,node_ref=21)
        assert str(result).startswith('error:'),result
        actions.tap.assert_not_awaited()
    asyncio.run(run())
