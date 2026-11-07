"""会话内屏幕编号：有界保存已展示节点，动作前重新核对整页观察。"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import re
import time
from typing import Any

from .errors import PhoneControlError
from .models import ScreenSnapshot, UiNode


class SnapshotTargetError(PhoneControlError):
    """编号缺失、失效或与当前页面不符；调用者需要重新观察。"""


def event_owner(event: Any) -> tuple[str, str]:
    session = str(getattr(event, "unified_msg_origin", "") or "")
    sender = str(event.get_sender_id() or "")
    if not session or not sender:
        raise SnapshotTargetError("快照定位需要有效的用户会话")
    return session, sender


def fingerprint(snapshot: ScreenSnapshot):
    return (snapshot.package, snapshot.activity, snapshot.width, snapshot.height,
            [node.to_dict() for node in snapshot.nodes])


@dataclass
class SnapshotRecord:
    snapshot: ScreenSnapshot
    owner: tuple[str, str]
    device: tuple
    refs: frozenset[int]
    created: float


class SnapshotTargets:
    def __init__(self, *, ttl=60.0, limit=16, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.records: dict[str, SnapshotRecord] = {}

    def clear(self):
        self.records.clear()

    def expose(self, snapshot, rendered, owner, device):
        self.records = {key: item for key, item in self.records.items()
                        if self.clock() - item.created <= self.ttl}
        while len(self.records) >= self.limit:
            self.records.pop(next(iter(self.records)))
        self.records[snapshot.snapshot_id] = SnapshotRecord(
            copy.deepcopy(snapshot), owner, device,
            frozenset(int(ref) for ref in re.findall(r'^\[(\d+)\]', rendered, re.M)), self.clock(),
        )

    def get(self, snapshot_id, node_ref, owner, device) -> tuple[SnapshotRecord, UiNode]:
        if type(node_ref) is not int or node_ref < 0:
            raise SnapshotTargetError("node_ref 必须是屏幕上显示的非负整数")
        item = self.records.get(snapshot_id)
        if item is None or self.clock() - item.created > self.ttl:
            self.records.pop(snapshot_id, None)
            raise SnapshotTargetError("快照已过期或失效，请重新 phone_screen 获取 snapshot_id 和节点编号")
        if item.owner != owner or item.device != device:
            raise SnapshotTargetError("快照不属于当前用户会话或设备，请重新 phone_screen")
        if node_ref not in item.refs:
            raise SnapshotTargetError("该节点未在此快照中展示，请重新 phone_screen 获取目标编号")
        node = next((node for node in item.snapshot.nodes if node.ref == node_ref), None)
        if node is None:
            raise SnapshotTargetError("快照没有此节点，请重新 phone_screen")
        return item, node

    def validate(self, snapshot_id, item, current):
        if (self.records.get(snapshot_id) is not item
                or self.clock() - item.created > self.ttl
                or fingerprint(current) != fingerprint(item.snapshot)):
            self.records.pop(snapshot_id, None)
            raise SnapshotTargetError("页面已变化或快照失效，未执行点击；请重新 phone_screen 获取编号")
