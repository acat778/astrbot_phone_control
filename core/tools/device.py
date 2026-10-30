"""基础设备操作工具：状态、屏幕读取、点击/长按/滑动、文本输入、按键、截图与等待。"""

from __future__ import annotations

import base64

import mcp
from mcp.types import ContentBlock
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain

from ..page_content import page_traffic_footer
from ..errors import PhoneControlError, UiDumpError, UiTargetNotFoundError

from .base import _PhoneToolBase, _as_float, _as_int, _json_text, _ok, _wrap_call


@dataclass
class PhoneStatusTool(_PhoneToolBase):
    """查看手机模拟器的连接、前台 App、电量与分辨率。"""

    name: str = "phone_status"
    description: str = (
        "查看受控安卓手机/模拟器的状态：ADB 连接、设备型号、分辨率、电量、"
        "当前前台 App。执行任何手机操作前可先调用本工具确认设备可用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        return _ok(await self.service.status_text())


# 页面文本里出现这些词时，提示模型优先用两阶段识图工具处理验证码/做题。
_SCREEN_QUIZ_WORDS = ("答题", "题目", "考试", "测试", "验证", "滑块", "拖动", "人机")


_SCREEN_QUIZ_HINT = (
    "检测到答题/验证页面：优先 phone_solve_verification（两阶段识图+自动执行），"
    "不要反复 phone_wait/phone_screen 干等"
)


def _screen_quiz_hint(text: str) -> str:
    """检查屏幕文本是否含答题/验证关键词，命中返回提示文本，否则返回空串。"""
    value = str(text or "")
    if any(word in value for word in _SCREEN_QUIZ_WORDS):
        return _SCREEN_QUIZ_HINT
    return ""


@dataclass
class PhoneScreenTool(_PhoneToolBase):
    """读取当前界面：UI 树 / 快速截图 / 两者，抓包可用时附带当前页面内容。"""

    name: str = "phone_screen"
    description: str = (
        "读取手机当前界面。mode=ui（默认）读取 UI 树（文本/描述/可点击坐标/控件 id），"
        "可用 include_screenshot 附带截图；mode=image 不跑 uiautomator dump、只做快速截图"
        "把画面回传模型（适合视频/直播等实时页面，避免 dump 期间画面已变化）；"
        "mode=both 同时给 UI 树和快速截图。抓包可用时自动附带当前前台 App 的最近接口"
        "内容（include_traffic=false 可关闭），模型不必再单独调 phone_traffic；图文、"
        "视频画面等视觉信息仍看截图。点击文本可用 phone_tap 的 text/desc 参数。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["ui", "image", "both"],
                    "description": "ui（默认，dump UI 树）/ image（只快速截图看画面）/ both。",
                    "default": "ui",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "description": "ui 模式下是否同时把当前屏幕截图回传给模型。",
                    "default": False,
                },
                "include_traffic": {
                    "type": "boolean",
                    "description": "抓包可用时是否附带当前前台 App 的页面内容（默认 true）。",
                    "default": True,
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "ui 模式最多渲染多少个节点，0 表示使用插件配置的默认上限。",
                    "default": 0,
                },
            },
        }
    )

    @_wrap_call
    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        await self._guard(context)
        mode = str(kwargs.get("mode") or "ui").strip().lower()
        if mode not in {"ui", "image", "both"}:
            return f"error: 不支持的 mode {mode!r}，只能是 ui / image / both"
        max_nodes = _as_int(kwargs.get("max_nodes"), 0)
        include_screenshot = kwargs.get("include_screenshot")
        if include_screenshot is None:
            include_screenshot = (
                self.service.config.perception.include_screenshot_by_default
            )
        include_screenshot = bool(include_screenshot)
        include_traffic = kwargs.get("include_traffic")
        include_traffic = True if include_traffic is None else bool(include_traffic)
        config = self.service.config

        sections: list[str] = []
        # 答题/验证提示只根据 UI 文本判断：抓包 JSON 里的普通标题可能含「测试」等词，
        # 混入判断会误触发答题提示。
        hint_source = ""
        image_data: bytes | None = None
        foreground_package = ""
        foreground_activity = ""
        want_image = mode in {"image", "both"} or (
            mode == "ui" and include_screenshot
        )
        if mode in {"ui", "both"}:
            try:
                ui_text, _snapshot = await self.service.screen(max_nodes=max_nodes)
            except UiDumpError as exc:
                if not want_image:
                    raise
                sections.append(
                    f"UI 节点不可用：{exc}。本次仅取得截图；"
                    "请按实际画面观察，后续可用 mode=image；文本/节点定位需要重新采集成功。"
                )
            else:
                sections.append(ui_text)
                hint_source += ui_text

        if want_image:
            if mode in {"image", "both"}:
                try:
                    foreground_package, foreground_activity = (
                        await self.service.actions.foreground()
                    )
                except PhoneControlError:
                    foreground_package, foreground_activity = "", ""
            data, _path, meta = await self.service.capture(
                expected_package=foreground_package
            )
            image_data = data
            method = meta.get("method", "png")
            elapsed = meta.get("elapsed_ms")
            elapsed_text = (
                f"{float(elapsed):.1f}" if isinstance(elapsed, (int, float)) else "未知"
            )
            if mode in {"image", "both"}:
                header = (
                    f"当前前台：{foreground_package or '未知'}"
                    + (f"/{foreground_activity}" if foreground_activity else "")
                    + f"；截图完成：method={method}，耗时={elapsed_text} ms"
                )
                sections.insert(0, header)
                if mode == "image":
                    sections.append(
                        "（image 模式：未执行 uiautomator dump，只看了当前画面）"
                    )
            else:
                sections.append(
                    f"附带截图：method={method}，耗时={elapsed_text} ms"
                )
            if isinstance(meta.get("frame_age_ms"), (int,float)):
                sections.append(f"画面年龄：{meta['frame_age_ms']} ms；允许上限：{meta.get('frame_age_limit_ms',250)} ms")
            if meta.get("fallback"):
                sections.append("（快速截图失败，已回退普通截图）")
            slow_ms = config.perception.screenshot_slow_ms
            if (
                isinstance(elapsed, (int, float))
                and float(elapsed) > slow_ms
                and mode != "ui"
            ):
                sections.append(
                    f"（截图较慢：超过阈值 screenshot_slow_ms={slow_ms}ms）"
                )

        # 抓包页面内容与屏幕结果同一次返回：抓包可用时模型不必再单独调
        # phone_traffic；选择口径（前台 App/动作窗口/时效）在 core.page_content。
        if include_traffic:
            snapshot = await self.service.page_traffic(
                foreground_package=foreground_package,
                activity=foreground_activity,
                compact=True,
            )
            if snapshot.has_content:
                traffic_block = (
                    "【抓包页面内容】\n"
                    + snapshot.text
                    + "\n"
                    + page_traffic_footer(snapshot)
                )
                position = (
                    1 if mode in {"image", "both"} and want_image else 0
                )
                sections.insert(position, traffic_block)

        text = "\n\n".join(section for section in sections if section)
        hint = _screen_quiz_hint(hint_source)
        if hint:
            text = f"{text}\n\n{hint}" if text else hint
        if image_data is None:
            return text or "（没有可返回的内容）"
        content: list[ContentBlock] = [
            mcp.types.TextContent(type="text", text=text or "截图完成"),
            mcp.types.ImageContent(
                type="image",
                data=base64.b64encode(image_data).decode("ascii"),
                mimeType="image/png",
            ),
        ]
        return mcp.types.CallToolResult(content=content)


@dataclass
class PhoneTapTool(_PhoneToolBase):
    """按坐标或按文本点击。"""

    name: str = "phone_tap"
    description: str = (
        "点击手机屏幕。可传坐标 (x, y)，也可传 text/desc 让插件先在当前 UI 树里"
        "定位控件中心再点击；text/desc 优先于坐标。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "点击横坐标（像素）。", "default": 0},
                "y": {"type": "integer", "description": "点击纵坐标（像素）。", "default": 0},
                "text": {
                    "type": "string",
                    "description": "要点击控件上的文本（子串匹配，大小写不敏感）。",
                    "default": "",
                },
                "desc": {
                    "type": "string",
                    "description": "要点击控件的 content-desc。",
                    "default": "",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        x = _as_int(kwargs.get("x"), 0)
        y = _as_int(kwargs.get("y"), 0)
        text = str(kwargs.get("text") or "").strip()
        desc = str(kwargs.get("desc") or "").strip()
        if text or desc:
            node = await self.service.find_node(text=text, desc=desc)
            if node is None:
                raise UiTargetNotFoundError(f"当前界面没有找到 text={text!r} desc={desc!r} 的控件")
            x, y = await self.service.node_click_point(node)
            label = node.label
        elif x == 0 and y == 0:
            return "error: 请提供 text/desc 定位控件，或提供有效的 x/y 坐标"
        else:
            label = f"({x},{y})"
        await self.service.tap(x, y)
        result = f"已点击 {label} @({x},{y})"
        # 按 text/desc 点击通常意味着「点开感兴趣的标题/卡片」：记录为一次内容曝光，
        # 同类内容反复点击会累计疲劳度（新鲜感/厌倦感机制）。
        if text or desc:
            app = str(getattr(node, "package", "") or "")
            match = self._record_novelty(app, text or desc, weight=1.2)
            bored_note = self._novelty_bored_note(match)
            if bored_note:
                result += f"\n（{bored_note}）"
        return _ok(result)


@dataclass
class PhoneLongPressTool(_PhoneToolBase):
    """按坐标或按文本长按。"""

    name: str = "phone_long_press"
    description: str = (
        "长按手机屏幕。可传坐标 (x, y)，也可传 text/desc 定位控件；duration_ms 为按住时长。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "长按横坐标（像素）。", "default": 0},
                "y": {"type": "integer", "description": "长按纵坐标（像素）。", "default": 0},
                "text": {
                    "type": "string",
                    "description": "要长按控件上的文本（子串匹配）。",
                    "default": "",
                },
                "desc": {
                    "type": "string",
                    "description": "要长按控件的 content-desc。",
                    "default": "",
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "按住时长（毫秒），默认 800。",
                    "default": 800,
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        x = _as_int(kwargs.get("x"), 0)
        y = _as_int(kwargs.get("y"), 0)
        text = str(kwargs.get("text") or "").strip()
        desc = str(kwargs.get("desc") or "").strip()
        duration_ms = _as_int(kwargs.get("duration_ms"), 800)
        if text or desc:
            node = await self.service.find_node(text=text, desc=desc)
            if node is None:
                raise UiTargetNotFoundError(f"当前界面没有找到 text={text!r} desc={desc!r} 的控件")
            x, y = await self.service.node_click_point(node)
            label = node.label
        elif x == 0 and y == 0:
            return "error: 请提供 text/desc 定位控件，或提供有效的 x/y 坐标"
        else:
            label = f"({x},{y})"
        await self.service.long_press(x, y, duration_ms=duration_ms)
        return _ok(f"已长按 {label} @({x},{y})，{duration_ms}ms")


@dataclass
class PhoneSwipeTool(_PhoneToolBase):
    """按方向滑动屏幕。"""

    name: str = "phone_swipe"
    description: str = (
        "按方向滑动屏幕，用于翻页/浏览信息流：up（手指上滑，看我下面的内容）、"
        "down、left、right。duration_ms 为滑动时长，越大越慢。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "滑动方向。",
                    "default": "up",
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "滑动时长（毫秒），默认 400。",
                    "default": 400,
                },
            },
            "required": ["direction"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        direction = str(kwargs.get("direction") or "up").strip().lower()
        if direction not in {"up", "down", "left", "right"}:
            return f"error: 不支持的方向 {direction!r}，只能是 up/down/left/right"
        duration_ms = _as_int(kwargs.get("duration_ms"), 400)
        await self.service.swipe_direction(direction, duration_ms=duration_ms)
        return _ok(f"已向 {direction} 滑动（{duration_ms}ms）")


@dataclass
class PhoneInputTextTool(_PhoneToolBase):
    """向输入框输入文本。"""

    name: str = "phone_input_text"
    description: str = (
        "向当前聚焦的输入框输入文本（支持中文；App 模式通过本机控制通道，直连使用 ADBKeyboard 或 scrcpy）；"
        "先点击编辑框使其聚焦。replace=true 替换实际输入，placeholder 不是旧内容。"
        "输入后核对实际关键词；press_enter 仅验证后回车，App 有搜索按钮时保持 false。模拟器无需软键盘。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本。"},
                "replace": {
                    "type": "boolean",
                    "description": "是否清空当前聚焦框的实际输入后替换；默认追加。",
                    "default": False,
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "输入完成后是否按回车。",
                    "default": False,
                },
            },
            "required": ["text"],
        }
    )

    @staticmethod
    def _editable(node) -> bool:
        return node.showing_hint is not True and (
            "EditText" in node.class_name or "AutoCompleteTextView" in node.class_name
        )

    @classmethod
    def _verified(cls, snapshot, text: str, replace: bool) -> bool:
        return any(
            node.focused and cls._editable(node)
            and (node.text == text if replace else text in node.text)
            for node in snapshot.nodes
        )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        text = str(kwargs.get("text") or "")
        if not text:
            return "error: text 不能为空"
        press_enter = bool(kwargs.get("press_enter", False))
        replace = bool(kwargs.get("replace", False))
        if replace:
            await self.service.actions.clear_text()
        await self.service.input_text(text, press_enter=False)
        _rendered, snapshot = await self.service.screen()
        verified = self._verified(snapshot, text, replace)
        if not verified:
            # 部分输入法或网页输入框在一次粘贴后失去编辑焦点，导致后续粘贴无效。
            # 按实际恢复顺序（清空 → 重新点击聚焦框 → 输入）重试一次。
            if replace:
                await self.service.actions.clear_text()
            _rendered, snapshot = await self.service.screen()
            target = next(
                (
                    node for node in snapshot.nodes
                    if node.focused and self._editable(node)
                    and node.bounds.width > 0 and node.bounds.height > 0
                ),
                None,
            )
            if target is not None:
                await self.service.tap(target.bounds.center_x, target.bounds.center_y)
                await self.service.wait(seconds=0.4)
            await self.service.input_text(text, press_enter=False)
            _rendered, snapshot = await self.service.screen()
            verified = self._verified(snapshot, text, replace)
        if not verified:
            return "文字输入命令已执行，但实际输入值未确认；先看当前截图核对聚焦和关键词，再提交搜索。"
        if press_enter:
            await self.service.press_key("enter")
        return _ok(f"已核对输入关键词（{len(text)} 字）" + ("并回车；请核对搜索结果" if press_enter else "；按当前 App 的按钮或回车逻辑提交"))


@dataclass
class PhoneKeyTool(_PhoneToolBase):
    """发送按键事件。"""

    name: str = "phone_key"
    description: str = (
        "发送安卓按键：back（返回）/home（回桌面）/enter（回车）/recents（任务列表）/"
        "volume_up/volume_down/power/tab/delete/escape 等。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "按键名，如 back/home/enter/recents/volume_up。",
                },
            },
            "required": ["key"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        key = str(kwargs.get("key") or "").strip().lower()
        if not key:
            return "error: key 不能为空"
        await self.service.press_key(key)
        return _ok(f"已发送按键 {key}")


@dataclass
class PhoneScreenshotTool(_PhoneToolBase):
    """截图：默认只回传模型，不发用户。"""

    name: str = "phone_screenshot"
    description: str = (
        "截取手机当前屏幕（默认只回传给模型、不发给用户；只有 send_to_user=true 才发到"
        "当前聊天）。返回文本带截图方式与耗时（method/ms），便于观察性能。fast=true 时"
        "优先快速截图，expected_package 可提示期望的前台包（防止截到切屏过程）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "send_to_user": {
                    "type": "boolean",
                    "description": "是否把截图发送到当前聊天（默认 false，静默模式）。",
                    "default": False,
                },
                "return_image": {
                    "type": "boolean",
                    "description": "是否把截图回传给模型。",
                    "default": True,
                },
                "fast": {
                    "type": "boolean",
                    "description": "是否优先使用快速截图（默认按插件 screenshot_mode 配置）。",
                    "default": False,
                },
                "expected_package": {
                    "type": "string",
                    "description": "期望的当前前台包名（可留空，插件会自行查询）。",
                    "default": "",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = await self._guard(context)
        send_to_user = bool(kwargs.get("send_to_user", False))
        return_image = bool(kwargs.get("return_image", True))
        fast = bool(kwargs.get("fast", False))
        expected_package = str(kwargs.get("expected_package") or "").strip()
        data, path, meta = await self.service.capture(
            fast=fast, expected_package=expected_package
        )
        method = meta.get("method", "png")
        elapsed = meta.get("elapsed_ms")
        elapsed_text = (
            f"{float(elapsed):.1f}" if isinstance(elapsed, (int, float)) else "未知"
        )
        summary = f"截图完成：method={method}，耗时={elapsed_text} ms"
        if isinstance(meta.get("frame_age_ms"), (int,float)):
            summary += f"，画面年龄={meta['frame_age_ms']} ms（上限 {meta.get('frame_age_limit_ms',250)} ms）"
        if meta.get("fallback"):
            summary += "（快速截图失败，已回退普通截图）"
        if meta.get("mismatch"):
            summary += "（截图前后前台包不一致，可能截到切屏瞬间）"
        slow_ms = self.service.config.perception.screenshot_slow_ms
        if isinstance(elapsed, (int, float)) and float(elapsed) > slow_ms:
            summary += f"（较慢：超过阈值 screenshot_slow_ms={slow_ms}ms）"
        payload = {
            "success": True,
            "path": str(path),
            "method": method,
            "elapsed_ms": elapsed,
            "fast": bool(meta.get("fast")),
            "size": len(data),
        }
        if send_to_user:
            await event.send(MessageChain().file_image(str(path)))
            payload["sent_to_user"] = True
        if not return_image:
            suffix = "，并已发送给用户" if send_to_user else ""
            return _ok(f"{summary}\n已保存：{path}{suffix}")
        content: list[ContentBlock] = [
            mcp.types.TextContent(
                type="text", text=f"{summary}\n{_json_text(payload)}"
            ),
            mcp.types.ImageContent(
                type="image",
                data=base64.b64encode(data).decode("ascii"),
                mimeType="image/png",
            ),
        ]
        return mcp.types.CallToolResult(content=content)


@dataclass
class PhoneWaitTool(_PhoneToolBase):
    """等待指定秒数或等待界面出现文本。"""

    name: str = "phone_wait"
    description: str = (
        "等待：seconds 为等待秒数，不传或传 0 时取插件配置 "
        "perception.wait_default_seconds（默认 2 秒，面板可配置），单次上限为 "
        "perception.wait_max_seconds（默认 60 秒，可配置）；for_text 非空时改为轮询等待"
        "该文本出现（最长等待 seconds 秒，秒数同样受配置默认/上限约束）。用于等待页面"
        "加载、广告结束；遇到验证/答题页面不要反复 phone_wait 空等，先试 "
        "phone_solve_verification。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": (
                        "等待秒数；0 表示使用配置 perception.wait_default_seconds。"
                    ),
                    "default": 0,
                },
                "for_text": {
                    "type": "string",
                    "description": "等待出现的文本；留空表示单纯等待。",
                    "default": "",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        seconds = _as_float(kwargs.get("seconds"), 0)
        for_text = str(kwargs.get("for_text") or "").strip()
        return _ok(await self.service.wait(seconds=seconds, for_text=for_text))
