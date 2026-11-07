"""插件公共数据模型。

核心模块（adb/ui/actions/browse/diary/tools）共享的类型定义；保持纯标准库，
不导入 astrbot，便于本地单测与集成测试。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Rect:
    """屏幕矩形区域（像素，原点在左上角）。"""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center_x(self) -> int:
        return self.left + self.width // 2

    @property
    def center_y(self) -> int:
        return self.top + self.height // 2

    @property
    def center(self) -> tuple[int, int]:
        return self.center_x, self.center_y

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def visible_center(self, width: int, height: int) -> tuple[int, int] | None:
        """返回屏幕内交集中心；完全不可见或空矩形返回 None。"""
        left, top = max(0, self.left), max(0, self.top)
        right, bottom = min(width, self.right), min(height, self.bottom)
        if left >= right or top >= bottom:
            return None
        return (left + right) // 2, (top + bottom) // 2

    def to_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]


@dataclass
class DeviceInfo:
    """设备与前台状态快照。"""

    serial: str = ""
    model: str = ""
    manufacturer: str = ""
    android_version: str = ""
    sdk: int = 0
    width: int = 0
    height: int = 0
    density: int = 0
    battery_level: int | None = None
    battery_status: str = ""
    current_package: str = ""
    current_activity: str = ""
    screen_on: bool | None = None
    adb_connected: bool = False
    device_kind: str = "unknown"
    keyboard_visible: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "android_version": self.android_version,
            "sdk": self.sdk,
            "width": self.width,
            "height": self.height,
            "density": self.density,
            "battery_level": self.battery_level,
            "battery_status": self.battery_status,
            "current_package": self.current_package,
            "current_activity": self.current_activity,
            "screen_on": self.screen_on,
            "adb_connected": self.adb_connected,
            "device_kind": self.device_kind,
            "keyboard_visible": self.keyboard_visible,
        }


@dataclass
class UiNode:
    """uiautomator 节点（扁平化，ref 为本次 dump 内顺序号）。"""

    ref: int
    text: str = ""
    desc: str = ""
    class_name: str = ""
    resource_id: str = ""
    package: str = ""
    bounds: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    enabled: bool = True
    focused: bool = False
    depth: int = 0
    hint: str = ""
    showing_hint: bool | None = None

    @property
    def label(self) -> str:
        """可读标签：文本优先，其次 content-desc，再次 resource-id 短名。"""
        if self.text:
            return self.text
        if self.desc:
            return self.desc
        if self.resource_id:
            return self.resource_id.rsplit("/", 1)[-1]
        return self.class_name.rsplit(".", 1)[-1] or "node"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "text": self.text,
            "desc": self.desc,
            "class": self.class_name,
            "id": self.resource_id,
            "package": self.package,
            "bounds": self.bounds.to_list(),
            "clickable": self.clickable,
            "long_clickable": self.long_clickable,
            "scrollable": self.scrollable,
            "checked": self.checked,
            "selected": self.selected,
            "enabled": self.enabled,
            "focused": self.focused,
            "depth": self.depth,
            "hint": self.hint,
            "showing_hint": self.showing_hint,
        }


@dataclass
class ScreenSnapshot:
    """一次 UI dump 的解析结果。"""

    package: str = ""
    activity: str = ""
    width: int = 0
    height: int = 0
    nodes: list[UiNode] = field(default_factory=list)
    screen_id: str = ""
    captured_at: float = field(default_factory=time.time)
    screenshot_path: str | None = None
    raw_xml_size: int = 0
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def find(
        self,
        *,
        text: str | None = None,
        desc: str | None = None,
        resource_id: str | None = None,
        clickable: bool | None = None,
        contains: bool = True,
    ) -> list[UiNode]:
        """按文本/描述/资源 id/可点击性筛选节点。"""
        needle_t = (text or "").strip().lower()
        needle_d = (desc or "").strip().lower()
        needle_r = (resource_id or "").strip()
        out: list[UiNode] = []
        for node in self.nodes:
            if needle_t:
                hay = node.text.lower()
                if not ((needle_t in hay) if contains else (needle_t == hay)):
                    continue
            if needle_d:
                hay = node.desc.lower()
                if not ((needle_d in hay) if contains else (needle_d == hay)):
                    continue
            if needle_r:
                if node.resource_id != needle_r and not node.resource_id.endswith(needle_r):
                    continue
            if clickable is not None and node.clickable != clickable:
                continue
            out.append(node)
        return out


@dataclass
class FeedItem:
    """信息流中的一条内容。"""

    app: str
    signature: str
    texts: list[str] = field(default_factory=list)
    title: str = ""
    author: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "signature": self.signature,
            "title": self.title,
            "author": self.author,
            "texts": list(self.texts),
            "meta": dict(self.meta),
            "screenshot": self.screenshot_path,
            "captured_at": self.captured_at,
        }


@dataclass
class BrowseRequest:
    """一次浏览请求。"""

    app: str
    items: int = 5
    collect_screenshots: bool = True
    interact: Literal["none", "like"] = "none"
    remember: bool = True


@dataclass
class BrowseResult:
    """一次浏览结果。"""

    app: str = ""
    items: list[FeedItem] = field(default_factory=list)
    steps: int = 0
    elapsed: float = 0.0
    started_at: float = field(default_factory=time.time)
    errors: list[str] = field(default_factory=list)
    diary_recorded: bool = False
    # 本次浏览中属于「已看腻」主题的标签（新鲜感/厌倦感机制填充，供工具提示）。
    novelty_labels: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        """生成给聊天/模型看的中文摘要。"""
        lines = [f"浏览 {self.app} 共采集 {len(self.items)} 条内容（{self.elapsed:.1f}s）"]
        for idx, item in enumerate(self.items, 1):
            head = item.title or (item.texts[0] if item.texts else "(无文本)")
            line = f"{idx}. {head}"
            if item.author and item.author not in head:
                line += f" —— {item.author}"
            lines.append(line)
        if self.errors:
            lines.append("异常：" + "；".join(self.errors[:3]))
        return "\n".join(lines)


@dataclass
class ActionResult:
    """设备动作结果（工具层统一返回文本前使用）。"""

    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
