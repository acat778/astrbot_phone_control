"""core/ui.py 单元测试。

真实设备用例标记 ``integration``，仅在设置 ``PHONE_ADB_HOST`` 环境变量时执行。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adb import AdbClient  # noqa: E402
from core.errors import AdbCommandError, UiDumpError  # noqa: E402
from core.models import Rect, ScreenSnapshot, UiNode  # noqa: E402
from core.ui import (  # noqa: E402
    LOADING_PAGE_TEXT,
    NO_READABLE_NODES_TEXT,
    assess_observation,
    dump_ui,
    find_nodes,
    parse_ui_xml,
    render_ui,
    screen_signature,
)
from core.verification import (  # noqa: E402
    OBSERVATION_EMPTY,
    OBSERVATION_LOADING,
    OBSERVATION_NO_TEXT,
    OBSERVATION_OK,
    OBSERVATION_SYSTEM_ONLY,
)

FOCUS_OUTPUT = (
    "  mCurrentFocus=Window{94cd8c9 u0 com.example.app/com.example.app.MainActivity}\n"
    "  mFocusedApp=ActivityRecord{5d013ca u0 com.example.app/.MainActivity t7}\n"
)

SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][900,1600]">
    <node index="0" text="首页" resource-id="com.example.app:id/tab_home" class="android.widget.TextView" package="com.example.app" content-desc="主页按钮" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[0,1400][300,1600]" />
    <node index="1" text="标题 &amp; 特殊字符 &quot;引号&quot;" resource-id="com.example.app:id/title" class="android.widget.TextView" package="com.example.app" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" bounds="[0,100][900,200]">
      <node index="0" text="不换行&#160;空格" resource-id="" class="android.widget.TextView" package="com.example.app" content-desc="" bounds="[10,110][500,180]" />
    </node>
    <node index="2" text="   " resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="false" enabled="true" bounds="bad-bounds" />
    <node index="3" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" content-desc="" clickable="true" enabled="false" bounds="[100,200][200,300]" />
  </node>
</hierarchy>"""

TTY_OUTPUT = SAMPLE_XML + "UI hierchary dumped to: /dev/tty\n"


def run(coro):
    """在独立事件循环中执行协程（测试环境未安装 pytest-asyncio）。"""
    return asyncio.run(coro)


@pytest.mark.parametrize("rect,expected", [
    (Rect(-500, 20, 40, 60), (20, 40)),
    (Rect(80, 180, 200, 400), (90, 190)),
    (Rect(100, 0, 140, 40), None),
    (Rect(10, 10, 10, 80), None),
    (Rect(10, 20, 30, 60), (20, 40)),
])
def test_click_center_uses_visible_intersection(rect, expected):
    assert rect.visible_center(100, 200) == expected


def test_empty_web_input_is_visible_to_agent_even_without_clickable_flag():
    snapshot = ScreenSnapshot(width=720, height=1280, nodes=[
        UiNode(ref=1, class_name="android.widget.EditText", bounds=Rect(40, 702, 664, 758)),
        UiNode(ref=2, class_name="android.widget.FrameLayout", bounds=Rect(0, 0, 720, 1280)),
    ])
    text = render_ui(snapshot)
    assert "[1] editable unfocused" in text and "@(352,730)" in text
    assert "[2]" not in text


class FakeAdb:
    """最小化的 AdbClient 鸭子类型，用于 dump_ui 测试。"""

    def __init__(self, *, tty_data: bytes | None = TTY_OUTPUT.encode(), tty_error=None,
                 cat_data: bytes | None = None, shell_error=None) -> None:
        self.tty_data = tty_data
        self.tty_error = tty_error
        self.cat_data = cat_data if cat_data is not None else SAMPLE_XML.encode()
        self.shell_error = shell_error
        self.exec_out_calls: list[tuple[str, ...]] = []
        self.shell_calls: list[str] = []

    async def exec_out(self, *args, timeout=None):
        self.exec_out_calls.append(args)
        if args[:3] == ("uiautomator", "dump", "/dev/tty"):
            if self.tty_error is not None:
                raise self.tty_error
            return self.tty_data
        if args and args[0] == "cat":
            return self.cat_data
        raise AssertionError(f"意外的 exec_out 调用：{args}")

    async def shell(self, command, *, timeout=None, retry=True):
        self.shell_calls.append(command)
        if self.shell_error is not None:
            raise self.shell_error
        if "mCurrentFocus" in command:
            return FOCUS_OUTPUT
        if command.startswith("uiautomator dump"):
            return "UI hierchary dumped to: /sdcard/window_dump.xml"
        return ""


# ----------------------------------------------------------------------
# parse_ui_xml
# ----------------------------------------------------------------------
def test_parse_ui_xml_ref_and_depth():
    nodes = parse_ui_xml(SAMPLE_XML)
    assert [node.ref for node in nodes] == list(range(len(nodes)))
    assert len(nodes) == 6
    assert nodes[0].depth == 0
    assert nodes[1].depth == 1
    assert nodes[2].depth == 1
    assert nodes[3].depth == 2  # 标题节点的子节点
    assert nodes[4].depth == 1
    assert nodes[5].depth == 1


def test_parse_ui_xml_fields():
    nodes = parse_ui_xml(SAMPLE_XML)
    root = nodes[0]
    assert root.class_name == "android.widget.FrameLayout"
    assert root.package == "com.example.app"
    assert root.bounds == Rect(0, 0, 900, 1600)
    home = nodes[1]
    assert home.text == "首页"
    assert home.desc == "主页按钮"
    assert home.resource_id == "com.example.app:id/tab_home"
    assert home.clickable is True
    assert home.long_clickable is True
    assert home.enabled is True
    assert home.bounds == Rect(0, 1400, 300, 1600)
    assert home.label == "首页"


def test_parse_ui_xml_special_characters_and_nbsp():
    nodes = parse_ui_xml(SAMPLE_XML)
    assert nodes[2].text == '标题 & 特殊字符 "引号"'
    assert nodes[3].text == "不换行 空格"  # 不换行空格已归一为普通空格


def test_parse_ui_xml_empty_and_malformed_bounds():
    nodes = parse_ui_xml(SAMPLE_XML)
    whitespace_node = nodes[4]
    assert whitespace_node.text == ""
    assert whitespace_node.bounds == Rect(0, 0, 0, 0)  # bounds 非法时回退全零


def test_parse_ui_xml_boolean_attrs():
    nodes = parse_ui_xml(SAMPLE_XML)
    clickable_empty = nodes[5]
    assert clickable_empty.clickable is True
    assert clickable_empty.enabled is False
    assert clickable_empty.text == ""


def test_parse_ui_xml_default_package():
    xml = (
        '<hierarchy rotation="0"><node text="hi" class="android.widget.TextView" '
        'clickable="true" bounds="[0,0][100,100]" /></hierarchy>'
    )
    nodes = parse_ui_xml(xml, package="com.fallback")
    assert nodes[0].package == "com.fallback"
    assert nodes[0].enabled is True
    assert nodes[0].clickable is True


def test_parse_ui_xml_empty_hierarchy_returns_empty_list():
    assert parse_ui_xml('<hierarchy rotation="0"></hierarchy>') == []


@pytest.mark.parametrize("bad_xml", ["", "   ", "not xml", "<foo><node/></foo>", "<hierarchy>"])
def test_parse_ui_xml_invalid_raises(bad_xml):
    with pytest.raises(UiDumpError):
        parse_ui_xml(bad_xml)


def test_parse_ui_xml_quotes_and_entities():
    xml = (
        '<hierarchy rotation="0"><node text="a &lt; b &amp; c &gt; d" '
        'content-desc="单个\'引号" bounds="[1,2][3,4]" /></hierarchy>'
    )
    node = parse_ui_xml(xml)[0]
    assert node.text == "a < b & c > d"
    assert node.desc == "单个'引号"
    assert node.bounds == Rect(1, 2, 3, 4)


# ----------------------------------------------------------------------
# dump_ui
# ----------------------------------------------------------------------
def test_dump_ui_tty_path_builds_snapshot():
    adb = FakeAdb()
    snapshot = run(dump_ui(adb))
    assert snapshot.width == 900
    assert snapshot.height == 1600
    assert snapshot.package == "com.example.app"
    assert snapshot.activity == "com.example.app.MainActivity"
    assert len(snapshot.nodes) == 6
    assert snapshot.raw_xml_size > 0
    assert len(snapshot.screen_id) == 16
    assert snapshot.captured_at > 0
    # 主路径成功时不应走文件回退
    assert all(call[:2] != ("cat", "/sdcard/window_dump.xml") for call in adb.exec_out_calls)


def test_dump_ui_falls_back_to_file_dump():
    adb = FakeAdb(tty_error=AdbCommandError("uiautomator dump 失败"))
    snapshot = run(dump_ui(adb))
    assert len(snapshot.nodes) == 6
    assert any(call[:2] == ("cat", "/sdcard/window_dump.xml") for call in adb.exec_out_calls)
    assert any(command.startswith("uiautomator dump /sdcard") for command in adb.shell_calls)


def test_dump_ui_retries_then_raises():
    adb = FakeAdb(tty_error=AdbCommandError("dump 失败"))
    adb.cat_data = b"garbage output"
    with pytest.raises(UiDumpError):
        run(dump_ui(adb, max_retries=1))
    # 2 次尝试 × （tty 失败 + 文件回退失败）
    tty_calls = [call for call in adb.exec_out_calls if call[0] == "uiautomator"]
    assert len(tty_calls) == 2


def test_dump_ui_single_attempt_when_no_retry():
    adb = FakeAdb(tty_error=AdbCommandError("dump 失败"))
    adb.cat_data = b"garbage output"
    with pytest.raises(UiDumpError):
        run(dump_ui(adb, max_retries=0))
    assert len([call for call in adb.exec_out_calls if call[0] == "uiautomator"]) == 1
    assert len(adb.shell_calls) == 1  # 仅文件回退的 dump 命令，未做第二次尝试


def test_dump_ui_activity_failure_is_tolerated():
    adb = FakeAdb()
    adb.shell_error = RuntimeError("焦点查询不可用")
    snapshot = run(dump_ui(adb))
    assert snapshot.activity == ""
    assert len(snapshot.nodes) == 6


def test_dump_ui_empty_dump_raises():
    adb = FakeAdb(tty_data=b"", cat_data=b"")
    with pytest.raises(UiDumpError):
        run(dump_ui(adb, max_retries=0))


# ----------------------------------------------------------------------
# render_ui
# ----------------------------------------------------------------------
def make_node(ref: int, **kwargs) -> UiNode:
    data = dict(
        text="",
        desc="",
        class_name="android.widget.TextView",
        package="com.example.app",
        bounds=Rect(0, 0, 100, 100),
    )
    data.update(kwargs)
    return UiNode(ref=ref, **data)


def make_snapshot(nodes: list[UiNode], **kwargs) -> ScreenSnapshot:
    data = dict(
        package="com.example.app",
        activity="com.example.app.MainActivity",
        width=900,
        height=1600,
        nodes=nodes,
        captured_at=1_700_000_000.0,
    )
    data.update(kwargs)
    return ScreenSnapshot(**data)


def test_render_ui_header_and_skip_empty_nodes():
    nodes = [
        make_node(0, text="标题一", bounds=Rect(10, 10, 110, 60)),
        make_node(1),  # 空文本/空 desc 且不可点击 -> 跳过
        make_node(2, clickable=True, bounds=Rect(100, 200, 300, 400), class_name="android.widget.Button"),
    ]
    text = render_ui(make_snapshot(nodes))
    assert "屏幕 900x1600" in text
    assert "包 com.example.app" in text
    assert "节点 3" in text
    assert "时间 " in text
    assert 'text="标题一"' in text
    assert "clickable@(200,300)" in text
    lines = [line for line in text.splitlines() if line.startswith("[")]
    assert len(lines) == 2


def test_render_ui_truncates_text_and_nodes():
    nodes = [
        make_node(0, text="abcdefghij", bounds=Rect(0, 0, 10, 10)),
        make_node(1, text="第二条", bounds=Rect(0, 20, 10, 30)),
    ]
    text = render_ui(make_snapshot(nodes), max_nodes=1, max_text_len=5)
    assert "已截断" in text
    assert 'text="abcde" text_truncated=true' in text
    assert "第二条" not in text


def test_render_ui_include_system():
    nodes = [
        make_node(0, text="12:00", package="com.android.systemui"),
        make_node(1, text="正文"),
    ]
    without_system = render_ui(make_snapshot(nodes))
    assert "12:00" not in without_system
    with_system = render_ui(make_snapshot(nodes), include_system=True)
    assert "12:00" in with_system


def test_render_ui_flags_and_escaping():
    nodes = [
        make_node(
            0,
            text='带"引号"\n换行',
            desc="描述",
            scrollable=True,
            checkable=True,
            checked=True,
            selected=True,
            enabled=False,
            bounds=Rect(5, 6, 25, 26),
        )
    ]
    text = render_ui(make_snapshot(nodes))
    assert 'desc="描述"' in text
    assert "scrollable" in text
    assert "checked" in text
    assert "selected" in text
    assert "disabled" in text
    assert "@(15,16)" in text
    assert "\\" in text  # 引号已转义
    assert "\\n换行" in text  # JSON 转义保留原始换行


def test_render_ui_empty_snapshot():
    text = render_ui(make_snapshot([]))
    assert "没有可读节点" in text


def test_render_ui_empty_snapshot_uses_shared_marker():
    """空树渲染标记与判定器兜底文案保持一致（防止两处文案漂移）。"""
    text = render_ui(make_snapshot([]))
    assert NO_READABLE_NODES_TEXT in text
    assert "当前界面没有可读节点" in text


def test_render_ui_loading_page_marks_unobservable():
    nodes = [
        make_node(0, text="加载中…"),
        make_node(1, class_name="android.widget.ProgressBar", clickable=False),
    ]
    text = render_ui(make_snapshot(nodes))
    assert LOADING_PAGE_TEXT in text
    assert "界面仍在加载" in text


def test_render_ui_normal_page_has_no_unobservable_marker():
    text = render_ui(make_snapshot([make_node(0, text="首页推荐视频列表")]))
    assert NO_READABLE_NODES_TEXT not in text
    assert LOADING_PAGE_TEXT not in text


# ----------------------------------------------------------------------
# assess_observation（观察质量）
# ----------------------------------------------------------------------
def test_assess_observation_empty_tree():
    assert assess_observation(make_snapshot([])) == OBSERVATION_EMPTY
    assert assess_observation(None) == OBSERVATION_EMPTY


def test_assess_observation_system_only():
    nodes = [
        make_node(0, text="12:00", package="com.android.systemui"),
        make_node(1, package="android", clickable=True),
    ]
    assert assess_observation(make_snapshot(nodes)) == OBSERVATION_SYSTEM_ONLY


def test_assess_observation_no_readable_text():
    nodes = [make_node(0, clickable=True), make_node(1, resource_id="com.example.app:id/icon")]
    assert assess_observation(make_snapshot(nodes)) == OBSERVATION_NO_TEXT


def test_assess_observation_loading_page():
    nodes = [make_node(0, text="正在加载"), make_node(1, text="请稍候")]
    assert assess_observation(make_snapshot(nodes)) == OBSERVATION_LOADING


def test_assess_observation_ok_with_readable_content():
    assert (
        assess_observation(make_snapshot([make_node(0, text="首页推荐视频列表")]))
        == OBSERVATION_OK
    )
    # 有可读内容时，混入加载提示也属于可观察页面
    nodes = [make_node(0, text="加载中"), make_node(1, text="首页推荐视频列表")]
    assert assess_observation(make_snapshot(nodes)) == OBSERVATION_OK


# ----------------------------------------------------------------------
# find_nodes
# ----------------------------------------------------------------------
def _find_fixture() -> ScreenSnapshot:
    nodes = [
        make_node(0, text="Hello World", clickable=True, bounds=Rect(0, 0, 10, 10)),
        make_node(
            1,
            text="hello",
            desc="点赞按钮",
            resource_id="com.example.app:id/btn_like",
            clickable=False,
        ),
        make_node(2, text="其他内容", resource_id="com.example.app:id/title", clickable=True),
    ]
    return make_snapshot(nodes)


def test_find_nodes_text_substring_case_insensitive():
    snapshot = _find_fixture()
    assert [node.ref for node in find_nodes(snapshot, text="hello")] == [0, 1]
    assert [node.ref for node in find_nodes(snapshot, text="HELLO")] == [0, 1]


def test_find_nodes_exact_match():
    snapshot = _find_fixture()
    assert [node.ref for node in find_nodes(snapshot, text="hello", contains=False)] == [1]
    assert find_nodes(snapshot, text="hell", contains=False) == []


def test_find_nodes_desc_and_resource_id():
    snapshot = _find_fixture()
    assert [node.ref for node in find_nodes(snapshot, desc="点赞")] == [1]
    assert [node.ref for node in find_nodes(snapshot, resource_id="btn_like")] == [1]
    assert [node.ref for node in find_nodes(snapshot, resource_id="com.example.app:id/title")] == [2]


def test_find_nodes_clickable_filter():
    snapshot = _find_fixture()
    assert [node.ref for node in find_nodes(snapshot, clickable=True)] == [0, 2]
    assert [node.ref for node in find_nodes(snapshot, clickable=False)] == [1]


def test_find_nodes_sorted_by_ref():
    nodes = [
        make_node(5, text="目标"),
        make_node(2, text="目标"),
        make_node(9, text="目标"),
    ]
    assert [node.ref for node in find_nodes(make_snapshot(nodes), text="目标")] == [2, 5, 9]


# ----------------------------------------------------------------------
# screen_signature
# ----------------------------------------------------------------------
def test_screen_signature_stable_and_order_independent():
    nodes = [make_node(0, text="A"), make_node(1, text="B")]
    signature_one = screen_signature(make_snapshot(nodes))
    signature_two = screen_signature(make_snapshot(list(reversed(nodes))))
    assert signature_one == signature_two
    assert len(signature_one) == 16


def test_screen_signature_changes_with_content():
    base = screen_signature(make_snapshot([make_node(0, text="A")]))
    changed_text = screen_signature(make_snapshot([make_node(0, text="B")]))
    changed_bounds = screen_signature(
        make_snapshot([make_node(0, text="A", bounds=Rect(0, 0, 50, 50))])
    )
    changed_package = screen_signature(
        make_snapshot([make_node(0, text="A")], package="com.other.app")
    )
    assert len({base, changed_text, changed_bounds, changed_package}) == 4


def test_screen_signature_ignores_system_ui_nodes():
    first = make_snapshot(
        [
            make_node(0, text="12:00", package="com.android.systemui"),
            make_node(1, text="正文"),
        ]
    )
    second = make_snapshot(
        [
            make_node(0, text="12:01", package="com.android.systemui"),
            make_node(1, text="正文"),
        ]
    )
    assert screen_signature(first) == screen_signature(second)


def test_screen_signature_empty_snapshot_deterministic():
    first = screen_signature(make_snapshot([]))
    second = screen_signature(make_snapshot([]))
    assert first == second


# ----------------------------------------------------------------------
# 真实设备集成测试（默认跳过）
# ----------------------------------------------------------------------
ADB_HOST = os.environ.get("PHONE_ADB_HOST", "")
ADB_PORT = int(os.environ.get("PHONE_ADB_PORT", "16448"))
ADB_PATH = os.environ.get(
    "PHONE_ADB_PATH",
    "/home/dev/acat-workspace/.cache/work/android-platform-tools/platform-tools/adb",
)
ADB_HOME = os.environ.get(
    "PHONE_ADB_HOME",
    "/home/dev/acat-workspace/.cache/work/android-home",
)

integration = pytest.mark.skipif(not ADB_HOST, reason="需要 PHONE_ADB_HOST 环境变量指向真实设备")


@pytest.mark.integration
@integration
def test_real_device_dump_and_render():
    async def scenario():
        client = AdbClient(ADB_PATH, ADB_HOST, ADB_PORT, ADB_HOME, connect_timeout=20.0)
        try:
            await client.connect()
            snapshot = await dump_ui(client, timeout=20.0)
            return snapshot
        finally:
            await client.close()

    snapshot = run(scenario())
    assert snapshot.width > 0 and snapshot.height > 0
    assert snapshot.nodes, "真实设备 dump 应解析出节点"
    rendered = render_ui(snapshot)
    assert "屏幕" in rendered
    assert f"{snapshot.width}x{snapshot.height}" in rendered


def test_editable_hint_and_focus_are_not_presented_as_old_input():
    nodes = parse_ui_xml('<hierarchy><node class="android.widget.EditText" text="热门推荐" hint="热门推荐" showing-hint-text="true" focused="true" bounds="[0,0][100,40]" /></hierarchy>')
    text = render_ui(ScreenSnapshot(nodes=nodes))
    assert 'placeholder="热门推荐"' in text
    assert 'editable focused' in text
    assert 'text="热门推荐"' not in text


@pytest.mark.parametrize("failure", ["ERROR: could not get idle state.", ""])
def test_file_dump_failure_does_not_read_previous_xml(failure):
    class FailedDump(FakeAdb):
        async def shell(self, command, *, timeout=None, retry=True):
            self.shell_calls.append(command)
            if command.startswith("uiautomator dump"):
                return failure
            return ""

    adb = FailedDump(tty_error=AdbCommandError("tty 不可用"), cat_data=SAMPLE_XML.encode())
    with pytest.raises(UiDumpError, match="未生成当前结果"):
        run(dump_ui(adb, max_retries=0))
    assert not any(call[0] == "cat" for call in adb.exec_out_calls)


def test_dump_uses_client_runtime_capture_without_legacy_tty():
    class RuntimeAdb(FakeAdb):
        async def dump_ui_output(self, path, *, timeout=None):
            assert path == "/dev/tty"
            return TTY_OUTPUT.encode()

    adb = RuntimeAdb()
    snapshot = run(dump_ui(adb, max_retries=0))
    assert snapshot.nodes and snapshot.package == "com.example.app"
    assert not adb.exec_out_calls
