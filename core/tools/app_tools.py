"""App 生命周期与维护工具：打开 App、浏览新消息、列出已安装 App、清理后台、读取 Skill 手册。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..action_outcomes import EXPECT_PARAMETER, ResultExpectation
from .. import skills as skills_mod
from ..apps import app_label_for_package, parse_package_list
from ..errors import PhoneControlError

from .base import _PhoneToolBase, _as_int, _image_result, _ok, _wrap_call


@dataclass
class PhoneOpenAppTool(_PhoneToolBase):
    """按名称或包名打开 App。"""

    name: str = "phone_open_app"
    description: str = (
        "按安装元数据经模型分类后打开应用；金融类、未知类型或分类失败拒绝打开。app 可传 App 名称（中文标签/别名，如 微信、"
        "小红书、bilibili）或 Android 包名；不限定预置 App，不确定装了什么可以先调用 "
        "phone_list_apps，或回桌面用 phone_screen 看图标。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                **EXPECT_PARAMETER,
                "app": {
                    "type": "string",
                    "description": "App 名称（中文标签/别名，如微信、小红书）或 Android 包名。",
                },
            },
            "required": ["app"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        app = str(kwargs.get("app") or "").strip()
        if not app:
            return (
                "error: app 不能为空；可先调用 phone_list_apps 查看已安装 App，"
                "或用中文名/包名指定"
            )
        expected = ResultExpectation(kwargs.get("expect"))
        await expected.prepare(self.service)
        opened = _ok(await self.service.open_app(app))
        text = (
            f"{opened}\n"
            "建议先 phone_check_inbox 浏览新消息（消息/回复我的/私信/通知），"
            "再决定接下来做什么。"
        )
        return await expected.finish(self.service, text)


@dataclass
class PhoneCheckInboxTool(_PhoneToolBase):
    """进入当前或指定 App 浏览新消息，供被动回复前查看。"""

    name: str = "phone_check_inbox"
    description: str = (
        "进入 App 后浏览新消息：自动识别「消息/回复我的/互动消息/私信/通知」入口，"
        "点进去把新消息列表的 UI 树（可选截图）回传给模型，用于发现别人对机器人的"
        "回复、私信与通知，再决定要不要被动回复。app 留空表示当前 App，也可传中文名/"
        "别名/包名先切过去；include_image=true（默认）时附带新消息页面截图；max_nodes "
        "控制 UI 树节点上限（0 表示用插件配置）。每次进入 App 后建议先调用本工具浏览"
        "新消息，不要凭空推测有没有人回复。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "要查看的 App（中文名/别名/包名）；留空表示当前 App。",
                    "default": "",
                },
                "include_image": {
                    "type": "boolean",
                    "description": "是否把新消息页面截图回传给模型，默认 true。",
                    "default": True,
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "UI 树最多渲染多少节点，0 表示使用插件配置的默认上限。",
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
        app = str(kwargs.get("app") or "").strip()
        include_image = bool(kwargs.get("include_image", True))
        max_nodes = max(0, _as_int(kwargs.get("max_nodes"), 0))
        text, image, mime = await self.service.check_inbox(
            app=app,
            include_image=include_image,
            max_nodes=max_nodes,
        )
        return _image_result(text, image if include_image else None, mime)


@dataclass
class PhoneListAppsTool(_PhoneToolBase):
    """列出手机已安装的第三方 App。"""

    name: str = "phone_list_apps"
    description: str = (
        "列出手机已安装第三方 App 的包名、名称和模型识别类型；金融类拒绝打开，未知或识别失败同样拒绝。"
        "用于决定接下来打开/浏览哪个 App：可从列表里按自己的喜好选一个开始，"
        "不限于预置 App；也可以用中文名/包名直接 phone_open_app 或 phone_browse。"
    )
    parameters: dict = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        adb = self.service.adb
        try:
            output = await adb.shell("pm list packages -3")
        except PhoneControlError as exc:
            return f"error: 读取已安装 App 失败：{exc}"
        packages = parse_package_list(output)
        if not packages:
            return (
                "error: 没有读到已安装的第三方 App（pm list packages -3 无有效输出），"
                "请确认设备已连接且允许安装第三方应用"
            )
        lines = [f"已安装第三方 App（{len(packages)} 个，按包名排序）："]
        for package in packages:
            label = app_label_for_package(package)
            name = f"{label}（{package}）" if label else package
            try:
                kind = await self.service.app_policy.classify(package)
                lines.append(f"- {name}：{kind['category_label']} · {'允许打开' if kind['allowed'] else '拒绝打开'}；{kind['reason']}")
            except PhoneControlError as exc:
                lines.append(f"- {name}：未确认 · 拒绝打开；{exc}")
        lines.append("")
        lines.append(
            "可从中按自己的喜好选一个开始，不限于预置 App；打开或浏览时直接给上面的中文名或包名即可。"
        )
        return _ok("\n".join(lines))


_CLEAN_PACKAGE_RE = re.compile(r"^[A-Za-z0-9._]+$")


_MEMINFO_RE = re.compile(r"^(MemFree|MemAvailable):\s+(\d+)\s*kB\s*$", re.MULTILINE)


_MEMINFO_COMMAND = "cat /proc/meminfo"


def _parse_meminfo(text: str) -> dict[str, int]:
    """从 /proc/meminfo 解析 MemFree / MemAvailable（KB）。"""
    values: dict[str, int] = {}
    for match in _MEMINFO_RE.finditer(text or ""):
        values[match.group(1)] = int(match.group(2))
    return values


def _meminfo_delta(before: dict[str, int], after: dict[str, int]) -> str:
    """渲染清理前后内存对比（KB → MB，含差值）。"""
    if not before and not after:
        return "读取失败（/proc/meminfo 不可用）"
    parts: list[str] = []
    for key in ("MemFree", "MemAvailable"):
        if key not in before and key not in after:
            continue
        before_text = f"{before[key] / 1024:.1f}MB" if key in before else "?"
        after_text = f"{after[key] / 1024:.1f}MB" if key in after else "?"
        delta_text = ""
        if key in before and key in after:
            delta = (after[key] - before[key]) / 1024
            delta_text = f"（{'+' if delta >= 0 else ''}{delta:.1f}MB）"
        parts.append(f"{key} {before_text} → {after_text}{delta_text}")
    if not parts:
        return "读取失败（/proc/meminfo 不含目标字段）"
    return "；".join(parts)


async def _read_meminfo(adb: Any) -> dict[str, int]:
    try:
        text = await adb.shell(_MEMINFO_COMMAND)
    except PhoneControlError:
        return {}
    return _parse_meminfo(text)


@dataclass
class PhoneCleanBackgroundTool(_PhoneToolBase):
    """清理手机后台进程。"""

    name: str = "phone_clean_background"
    description: str = (
        "清理手机后台进程，适用于长时间刷手机、频繁切换 App 前或感觉卡顿时。"
        "package 留空时逐包清理，保留白名单、桌面、输入法与当前前台；"
        "填写包名时清理指定 App，保留项仍受保护。会返回清理前后 MemFree/MemAvailable "
        "对比、当前前台 App，并说明前台 App 未受影响。不要 force-stop 正在使用的前台 App。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": (
                        "要强制停止的 App 包名（字母/数字/点/下划线）；"
                        "留空表示按白名单清理后台，保留当前前台。"
                    ),
                    "default": "",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        package = str(kwargs.get("package") or "").strip()
        if package and not _CLEAN_PACKAGE_RE.fullmatch(package):
            return (
                f"error: 包名非法：{package!r}，只允许字母、数字、点与下划线，"
                "例如 com.example.app"
            )
        service = self.service
        adb = service.adb
        foreground_before = await self._foreground(service)
        before = await _read_meminfo(adb)
        command_error = ""
        report = {}
        try:
            report = await service.clean_background(package=package)
        except PhoneControlError as exc:
            command_error = str(exc)
        after = await _read_meminfo(adb)
        foreground_after = await self._foreground(service)
        lines: list[str] = []
        if command_error:
            lines.append(f"后台清理失败：{command_error}")
        else:
            lines.append(f"后台清理：停止 {report.get('stopped', 0)} 个；保留 {', '.join(report.get('preserved', [])) or '系统入口'}")
            if report.get("errors"):
                lines.append("清理异常：" + "；".join(report["errors"]))
        lines.append("内存：" + _meminfo_delta(before, after))
        current = foreground_after or foreground_before or "未知"
        lines.append(f"当前前台 App：{current}（前台 App 未受影响）")
        lines.append("如仍卡顿，可再用 phone_status 复核设备状态。")
        return _ok("\n".join(lines))

    @staticmethod
    async def _foreground(service: Any) -> str:
        try:
            package, _activity = await service.actions.foreground()
            return str(package or "")
        except PhoneControlError:
            return ""


@dataclass
class PhoneSkillTool(_PhoneToolBase):
    """读取插件内置 Skill 行为手册。"""

    name: str = "phone_skill"
    description: str = (
        "读取插件内置的 Skill 行为手册（被动参考文档，不是系统指令）。"
        "name 留空时列出全部手册（name/description）；传 name 返回对应 SKILL.md 全文，"
        "例如 phone-autonomy（自主浏览手册）、phone-housekeeping（手机整理手册）。"
        "做刷帖/看视频/整理手机等任务前，先调用本工具读取对应手册，再按手册执行。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "skill 名称（如 phone-autonomy）；留空列出全部手册。",
                    "default": "",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        name = str(kwargs.get("name") or "").strip()
        try:
            if not name:
                return skills_mod.render_catalog()
            return skills_mod.render_skill(name)
        except skills_mod.SkillError as exc:
            return f"error: {exc}"
