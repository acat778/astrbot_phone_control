"""UI 树采集、解析与渲染工具。

负责把 uiautomator dump 的 XML 转成扁平 ``UiNode`` 列表，并生成适合大模型阅读的
紧凑文本。仅依赖标准库；不导入 astrbot。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from .adb import FOCUS_DUMP_COMMAND, AdbClient, parse_foreground_dump
from .errors import PhoneControlError, UiDumpError
from .models import Rect, ScreenSnapshot, UiNode
from .verification import (
    OBSERVATION_EMPTY,
    OBSERVATION_LOADING,
    OBSERVATION_NO_TEXT,
    OBSERVATION_OK,
    OBSERVATION_SYSTEM_ONLY,
)

# bounds="[left,top][right,bottom]"
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 系统界面节点包名（状态栏、导航栏、系统弹窗等），默认不展示给模型
_SYSTEM_PACKAGES = ("com.android.systemui", "android")

# 加载过渡页的节点文案：整页只有这类提示时不能作为验证结果证据
_LOADING_TEXT_KEYWORDS = (
    "加载中",
    "正在加载",
    "请稍候",
    "loading",
)

# 渲染文本中的观察不足标记；``core.verification`` 按同样文案兜底判定
NO_READABLE_NODES_TEXT = "（当前界面没有可读节点）"
LOADING_PAGE_TEXT = "（界面仍在加载，暂无可读内容）"


def _clean_text(value: str | None) -> str:
    """归一化节点文本：把不换行空格替换为普通空格后 strip。"""
    if not value:
        return ""
    return value.replace("\u00a0", " ").strip()


def _parse_bounds(value: str | None) -> Rect:
    """解析 uiautomator 的 bounds 属性；非法值回退为全零矩形。"""
    match = _BOUNDS_RE.search(value or "")
    if not match:
        return Rect(0, 0, 0, 0)
    left, top, right, bottom = (int(group) for group in match.groups())
    return Rect(left, top, right, bottom)


def _attr_bool(attrs: dict[str, str], name: str, default: bool = False) -> bool:
    """解析布尔属性（兼容 "true"/"false"/缺省）。"""
    raw = attrs.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() == "true"


def _node_from_attrs(attrs: dict[str, str], *, ref: int, depth: int, default_package: str = "") -> UiNode:
    """把 XML 节点属性转换为 ``UiNode``。"""
    return UiNode(
        ref=ref,
        text=_clean_text(attrs.get("text")),
        desc=_clean_text(attrs.get("content-desc")),
        class_name=(attrs.get("class") or "").strip(),
        resource_id=(attrs.get("resource-id") or "").strip(),
        package=(attrs.get("package") or default_package or "").strip(),
        bounds=_parse_bounds(attrs.get("bounds")),
        clickable=_attr_bool(attrs, "clickable"),
        long_clickable=_attr_bool(attrs, "long-clickable"),
        scrollable=_attr_bool(attrs, "scrollable"),
        checkable=_attr_bool(attrs, "checkable"),
        checked=_attr_bool(attrs, "checked"),
        selected=_attr_bool(attrs, "selected"),
        enabled=_attr_bool(attrs, "enabled", default=True),
        focused=_attr_bool(attrs, "focused"),
        depth=depth,
        hint=_clean_text(attrs.get("hint") or attrs.get("hint-text")),
        showing_hint=(
            _attr_bool(attrs, "showing-hint-text")
            if "showing-hint-text" in attrs else None
        ),
    )


def parse_ui_xml(xml_text: str, *, package: str = "", activity: str = "") -> list[UiNode]:
    """解析 uiautomator dump 的 XML 文本，返回按文档顺序编号的节点列表。

    - ``ref`` 从 0 开始按文档顺序递增；
    - ``depth`` 从根节点 0 开始逐层递增；
    - ``package`` 参数作为节点缺少 package 属性时的兜底值。

    XML 为空或非法时抛 ``UiDumpError``。
    """
    if not xml_text or not xml_text.strip():
        raise UiDumpError("UI XML 为空")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise UiDumpError("UI XML 解析失败", detail=str(exc)) from exc

    nodes: list[UiNode] = []

    def walk(element: ET.Element, depth: int) -> None:
        for child in list(element):
            if child.tag != "node":
                continue
            nodes.append(
                _node_from_attrs(child.attrib, ref=len(nodes), depth=depth, default_package=package)
            )
            walk(child, depth + 1)

    if root.tag == "hierarchy":
        walk(root, 0)
    elif root.tag == "node":
        nodes.append(_node_from_attrs(root.attrib, ref=0, depth=0, default_package=package))
        walk(root, 1)
    else:
        raise UiDumpError("UI XML 根节点不是 hierarchy", detail=f"root=<{root.tag}>")
    return nodes


def _extract_xml_document(raw: str) -> str:
    """从 dump 输出中截取 XML 文档。

    ``uiautomator dump /dev/tty`` 会在 XML 之后追加一行
    ``UI hierchary dumped to: /dev/tty``，需要截断到 ``</hierarchy>`` 为止。
    """
    if not raw or not raw.strip():
        raise UiDumpError("uiautomator dump 输出为空")
    start = raw.find("<?xml")
    if start < 0:
        start = raw.find("<hierarchy")
    if start < 0:
        raise UiDumpError("uiautomator dump 未返回 XML", detail=raw.strip()[:200])
    end = raw.rfind("</hierarchy>")
    if end < 0:
        raise UiDumpError("uiautomator dump 返回的 XML 不完整", detail=raw.strip()[-200:])
    end += len("</hierarchy>")
    return raw[start:end]


async def _current_activity(adb: AdbClient, *, timeout: float | None) -> str:
    """尽力查询前台 Activity；查询失败返回空串（不阻塞 dump 主流程）。"""
    try:
        text = await adb.shell(FOCUS_DUMP_COMMAND, timeout=timeout)
    except Exception:  # noqa: BLE001 - 附加信息查询失败不影响 dump 结果
        return ""
    _package, activity = parse_foreground_dump(text)
    return activity


async def _dump_with_tty(adb: AdbClient, *, timeout: float | None) -> tuple[str, list[UiNode]]:
    """主路径：使用设备系统运行时将当前节点导出到 TTY。"""
    dump = getattr(adb, "dump_ui_output", None)
    raw = (
        await dump("/dev/tty", timeout=timeout)
        if dump is not None
        else await adb.exec_out("uiautomator", "dump", "/dev/tty", timeout=timeout)
    )
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    xml_text = _extract_xml_document(text)
    nodes = parse_ui_xml(xml_text)
    if not nodes:
        raise UiDumpError("uiautomator dump 没有解析出任何节点")
    return xml_text, nodes


async def _dump_with_file(adb: AdbClient, *, timeout: float | None) -> tuple[str, list[UiNode]]:
    """文件路径：确认本次采集完成，再取回当前 XML。"""
    remote_path = "/sdcard/window_dump.xml"
    dump = getattr(adb, "dump_ui_output", None)
    output = (
        await dump(remote_path, timeout=timeout)
        if dump is not None
        else await adb.shell(f"uiautomator dump {remote_path}", timeout=timeout)
    )
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    if f"dumped to: {remote_path}" not in output or "ERROR:" in output:
        raise UiDumpError("UI 节点采集未生成当前结果", detail=str(output).strip()[:300])
    raw = await adb.exec_out("cat", remote_path, timeout=timeout)
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    xml_text = _extract_xml_document(text)
    nodes = parse_ui_xml(xml_text)
    if not nodes:
        raise UiDumpError("uiautomator dump 文件没有解析出任何节点")
    return xml_text, nodes


def _build_snapshot(
    xml_text: str,
    nodes: list[UiNode],
    *,
    activity: str,
    package: str,
) -> ScreenSnapshot:
    """根据解析结果构造 ``ScreenSnapshot``，屏幕尺寸取节点边界最大值。"""
    width = max((node.bounds.right for node in nodes), default=0)
    height = max((node.bounds.bottom for node in nodes), default=0)
    resolved_package = package or next((node.package for node in nodes if node.package), "")
    snapshot = ScreenSnapshot(
        package=resolved_package,
        activity=activity,
        width=width,
        height=height,
        nodes=nodes,
        captured_at=time.time(),
        screenshot_path=None,
        raw_xml_size=len(xml_text.encode("utf-8", "replace")),
    )
    snapshot.screen_id = screen_signature(snapshot)
    return snapshot


async def dump_ui(
    adb: AdbClient,
    *,
    timeout: float | None = None,
    max_retries: int = 0,
) -> ScreenSnapshot:
    """采集当前界面的 UI 树。

    优先 ``exec-out uiautomator dump /dev/tty``；失败时回退到
    ``uiautomator dump /sdcard/window_dump.xml`` + ``exec-out cat``；
    每次尝试内部按 ``max_retries`` 重试，全部失败抛 ``UiDumpError``。

    默认不重试：持续刷新动画的页面（如信息流首页）``waitForIdle`` 必然超时，
    多轮重试只会把一次失败拖成数十秒，不如尽快降级到截图。
    """
    attempts = max(0, int(max_retries)) + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(0.4)
        for source in (_dump_with_tty, _dump_with_file):
            try:
                xml_text, nodes = await source(adb, timeout=timeout)
            except PhoneControlError as exc:
                last_error = exc
                continue
            activity = await _current_activity(adb, timeout=timeout)
            snapshot = _build_snapshot(xml_text, nodes, activity=activity, package="")
            return snapshot
    detail = str(last_error) if last_error is not None else "未知错误"
    raise UiDumpError("uiautomator dump 失败", detail=detail)


def _shorten(value: str, max_len: int) -> str:
    """按最大长度截断文本，超出时以省略号结尾。"""
    if max_len <= 0 or len(value) <= max_len:
        return value
    return value[: max(1, max_len - 1)] + "…"


def _quote(value: str) -> str:
    """把文本包成双引号形式，转义引号并压平换行。"""
    flat = value.replace("\r", " ").replace("\n", " ").replace('"', '\\"')
    return f'"{flat}"'


def _is_loading_text(text: str) -> bool:
    """节点文案是否只是加载提示（用于识别加载过渡页）。"""
    content = str(text or "").strip().lower()
    if not content:
        return False
    return any(keyword in content for keyword in _LOADING_TEXT_KEYWORDS)


def assess_observation(snapshot: ScreenSnapshot) -> str:
    """评估 UI 快照的观察质量，返回 ``OBSERVATION_*`` 等级。

    UI dump 成功不等于页面内容可观察：WebView、自绘界面与加载过渡页可能没有
    可读节点。只有 ``OBSERVATION_OK`` 表示页面有可读内容，其余等级都不能作为
    「验证挑战已解除」的证据。
    """
    if snapshot is None:
        return OBSERVATION_EMPTY
    nodes = list(getattr(snapshot, "nodes", None) or [])
    if not nodes:
        return OBSERVATION_EMPTY
    visible = [node for node in nodes if node.package not in _SYSTEM_PACKAGES]
    if not visible:
        return OBSERVATION_SYSTEM_ONLY
    readable = [node for node in visible if (node.text or node.desc)]
    if not readable:
        return OBSERVATION_NO_TEXT
    if all(_is_loading_text(node.text or node.desc) for node in readable):
        return OBSERVATION_LOADING
    return OBSERVATION_OK


def render_ui(
    snapshot: ScreenSnapshot,
    *,
    max_nodes: int = 120,
    max_text_len: int = 100,
    include_system: bool = False,
) -> str:
    """把快照渲染成大模型友好的紧凑文本（每行一个可交互/可读节点）。"""
    captured = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.captured_at or time.time()))
    header = (
        f"屏幕 {snapshot.width}x{snapshot.height} | 包 {snapshot.package or '未知'}"
    )
    if snapshot.activity:
        header += f" | Activity {snapshot.activity}"
    header += f" | 节点 {len(snapshot.nodes)} | 时间 {captured}"
    lines = [header]

    rendered = 0
    truncated = False
    for node in snapshot.nodes:
        if not include_system and node.package in _SYSTEM_PACKAGES:
            continue
        editable = "EditText" in node.class_name or "AutoCompleteTextView" in node.class_name
        # 输入框即使为空且未标记 clickable，也需要展示位置与焦点状态。
        if not node.text and not node.desc and not node.hint and not node.clickable and not editable:
            continue
        if rendered >= max(0, max_nodes):
            truncated = True
            break
        rendered += 1
        parts = [f"[{node.ref}]"]
        if node.text:
            label = "placeholder" if node.showing_hint is True else "text"
            parts.append(f"{label}={_quote(_shorten(node.text, max_text_len))}")
        if node.hint:
            parts.append(f"hint={_quote(_shorten(node.hint, max_text_len))}")
        if editable:
            parts.append("editable")
            parts.append("focused" if node.focused else "unfocused")
            if node.showing_hint is None:
                parts.append("输入值/占位提示未区分，输入后核对实际关键词再提交")
        if node.desc:
            parts.append(f"desc={_quote(_shorten(node.desc, max_text_len))}")
        if node.clickable:
            parts.append(f"clickable@({node.bounds.center_x},{node.bounds.center_y})")
        elif node.long_clickable:
            parts.append(f"long_clickable@({node.bounds.center_x},{node.bounds.center_y})")
        else:
            parts.append(f"@({node.bounds.center_x},{node.bounds.center_y})")
        parts.append(f"class={node.class_name or 'unknown'}")
        if node.resource_id:
            parts.append(f"id={node.resource_id}")
        if node.scrollable:
            parts.append("scrollable")
        if node.checkable:
            parts.append("checked" if node.checked else "unchecked")
        if node.selected:
            parts.append("selected")
        if not node.enabled:
            parts.append("disabled")
        lines.append(" ".join(parts))

    if truncated:
        lines.append(f"…（已截断，仅显示前 {max(0, max_nodes)} 个节点，共 {len(snapshot.nodes)} 个）")
    if rendered == 0:
        lines.append(NO_READABLE_NODES_TEXT)
    elif assess_observation(snapshot) == OBSERVATION_LOADING:
        # 加载过渡页即使有「加载中」等节点文本，也不构成页面内容证据
        lines.append(LOADING_PAGE_TEXT)
    return "\n".join(lines)


def find_nodes(
    snapshot: ScreenSnapshot,
    *,
    text: str | None = None,
    desc: str | None = None,
    resource_id: str | None = None,
    clickable: bool | None = None,
    contains: bool = True,
) -> list[UiNode]:
    """在快照中按文本/描述/资源 id/可点击性筛选节点，结果按 ref 排序。"""
    matched = snapshot.find(
        text=text,
        desc=desc,
        resource_id=resource_id,
        clickable=clickable,
        contains=contains,
    )
    return sorted(matched, key=lambda node: node.ref)


def screen_signature(snapshot: ScreenSnapshot) -> str:
    """计算界面内容指纹，用于稳定性判断与重复去重。

    指纹与节点顺序无关（对节点指纹排序后哈希），并排除系统栏节点
    （状态栏时间/电量会持续变化），同一界面重复 dump 应得到相同值。
    """
    digest = hashlib.sha1()
    digest.update(f"{snapshot.package}|{snapshot.width}x{snapshot.height}".encode("utf-8", "replace"))
    fingerprints: list[str] = []
    for node in snapshot.nodes:
        # 系统栏（时间/电量等）会持续变化，排除后稳定性判断更可靠
        if node.package in _SYSTEM_PACKAGES:
            continue
        if not (node.text or node.desc or node.resource_id):
            continue
        fingerprints.append(
            "\x1f".join(
                (
                    node.text,
                    node.desc,
                    node.resource_id,
                    node.class_name,
                    ",".join(str(value) for value in node.bounds.to_list()),
                )
            )
        )
    for fingerprint in sorted(fingerprints):
        digest.update(fingerprint.encode("utf-8", "replace"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]
