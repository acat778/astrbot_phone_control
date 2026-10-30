"""工具注册：工具名常量、schema 精简与 build_phone_tools。"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import FunctionTool

from .device import (
    PhoneInputTextTool,
    PhoneKeyTool,
    PhoneLongPressTool,
    PhoneScreenTool,
    PhoneScreenshotTool,
    PhoneStatusTool,
    PhoneSwipeTool,
    PhoneTapTool,
    PhoneWaitTool,
)
from .app_tools import (
    PhoneCheckInboxTool,
    PhoneCleanBackgroundTool,
    PhoneListAppsTool,
    PhoneOpenAppTool,
    PhoneSkillTool,
)
from .share import PhoneShareToUserTool
from .clipboard import PhoneClipboardTool
from .shell import PhoneShellTool
from .content_task import PhoneContentTool
from .save_image import PhoneSaveImageTool, PhoneSavedImagesTool
from .content import (
    PhoneAppMemoryTool,
    PhoneAppRememberTool,
    PhoneBrowseTool,
    PhoneLearnTool,
    PhoneRecallDiaryTool,
    PhoneRecentChatTool,
)
from .interaction import PhoneInteractionGateTool, PhoneInteractionRecordTool
from .task import PhoneStartBackgroundTaskTool
from .verification_tools import PhoneBypassVerifyTool
from .solve import PhoneSolveVerificationTool
from .screen_ai import PhoneLiveScreenTool, PhoneReadScreenTool, PhoneWatchMediaTool
from .traffic import PhoneTrafficTool
from .wireguard import PhoneWireguardTool
from .files import PhoneFilesTool, PhoneRecordMediaTool


# 工具名常量：插件入口注册与测试断言共用，避免拼写漂移。
TOOL_NAMES = (
    "phone_status",
    "phone_screen",
    "phone_traffic",
    "phone_wireguard",
    "phone_clipboard",
    "phone_shell",
    "phone_tap",
    "phone_long_press",
    "phone_swipe",
    "phone_input_text",
    "phone_key",
    "phone_open_app",
    "phone_check_inbox",
    "phone_list_apps",
    "phone_screenshot",
    "phone_bypass_verify",
    "phone_share_to_user",
    "phone_browse",
    "phone_learn",
    "phone_recall_diary",
    "phone_recent_chat",
    "phone_interaction_gate",
    "phone_interaction_record",
    "phone_start_background_task",
    "phone_live_screen",
    "phone_read_screen",
    "phone_solve_verification",
    "phone_wait",
    "phone_clean_background",
    "phone_skill",
    "phone_app_memory",
    "phone_app_remember",
    "phone_watch_media",
    "phone_content",
    "phone_save_image",
    "phone_saved_images",
    "phone_files",
    "phone_record_media",
)


# 工具 schema 精简（仅压缩模型侧描述，不改变工具名/参数名/行为）
_TOOL_DESC_COMPACT: dict[str, str] = {
    "phone_clipboard": "读取设备当前剪贴板；复制分享链接后调用并核对帖子，再传给分享工具。不修改剪贴板。",
    "phone_status": "查看设备连接/前台 App/电量/分辨率；操作前可先确认设备。",
    "phone_screen": (
        "读屏幕：mode=ui（UI 树±截图）/image（仅快速画面）/both；"
        "抓包可用时自动附当前页面接口内容。"
    ),
    "phone_traffic": (
        "看抓包识别页面：scope=page（当前前台页面）/recent（最近全部流量）；"
        "图文/布局用截图兜底。"
    ),
    "phone_wireguard": (
        "WireGuard 组网：status 状态；start/stop/restart 开关/重启 helper；"
        "config 显示导入用配置（client=服务器 Peer 片段，server=手机端完整配置）。"
    ),
    "phone_tap": "点击：按坐标 (x,y) 或 text/desc 定位后点击。",
    "phone_long_press": "长按：坐标或 text/desc 定位，duration_ms 为按压时长。",
    "phone_swipe": "按方向滑动 up/down/left/right 翻页，duration_ms 为时长。",
    "phone_input_text": "先点编辑框；replace 替换实际输入，输入后核对关键词，press_enter 仅在可验证时回车；占位提示不是旧内容，模拟器可无软键盘。",
    "phone_key": "发送按键：back/home/enter/recents/volume_*/power/tab/delete 等。",
    "phone_open_app": "按中文名/别名/包名打开任意已安装 App。",
    "phone_check_inbox": "进入 App 浏览新消息（回复我的/私信/通知），被动回复前先看。",
    "phone_list_apps": "列出已安装第三方 App（包名+中文名），用于选择 App。",
    "phone_screenshot": "截图（默认仅回传模型不发用户）；返回 method/耗时，可 fast。",
    "phone_bypass_verify": (
        "人机验证手动精细控制：capture 快速截图不跑 dump；drag 拖拽一次完成；"
        "tap_points 顺序点击多点；slide 自动滑到最右；复杂验证优先 "
        "phone_solve_verification，不要反复 phone_screen(mode=ui)。"
    ),
    "phone_share_to_user": (
        "看/分享两重判断：看完再决定是否分享；必须带链接或截图（禁纯文字），"
        "复制链接后用 phone_clipboard 读取；帖子默认 content_type=post 回顶确认后附图，"
        "视频传 video，当前画面传 screen；正文先总结≤100字，发送成功后静默；目标按聊天记忆选。"
    ),
    "phone_browse": "便捷采样：浏览 App 信息流并写入日记（不强制固定条数）。",
    "phone_learn": (
        "记录你真正理解到的内容（观点/知识点/梗/事件/规则，≤500 字）；"
        "持续刷每轮至少记一次，别只滑不看或刷完凭印象编。"
    ),
    "phone_recall_diary": "检索浏览日志；query 空返回最近记录。",
    "phone_recent_chat": "查看会话最近聊天与可分享会话列表（umo），用于按喜好分享。",
    "phone_interaction_gate": (
        "互动前申请名额：like=点赞/favorite=收藏（共用单轮额度）；comment=主动评论；passive_reply=被动回复"
        "（先 check_inbox 看新消息，按心情/内容决定）；post=发帖；完成后 record。"
        "开关与限额是流程授权与记账，不是底层动作拦截。"
    ),
    "phone_interaction_record": (
        "记录互动结果（like/favorite/comment/passive_reply/post）：成功计次，失败释放名额；"
        "summary≤120 字；只做流程记账，不是底层动作拦截。"
    ),
    "phone_start_background_task": "手机子Agent后台执行立即返回；action=start/list/status/cancel，查询或取消填job_id。",
    "phone_live_screen": "看实时屏幕最新帧（age/前台），可回传图片；页面内容优先 phone_screen。",
    "phone_read_screen": "OCR 识别实时屏幕文字/验证码，返回文本与帧信息；页面内容优先 phone_screen。",
    "phone_solve_verification": (
        "两阶段识图（两次视觉模型）自动处理验证码/做题：第一次截图识别内容与类型，"
        "第二次重新截图给出 tap/drag 操作顺序与确认点并自动执行；适合滑块/顺序点击/"
        "识图/拼图/答题测试，失败回退 phone_bypass_verify。"
    ),
    "phone_wait": (
        "等待 seconds 秒，不传/0 取配置 perception.wait_default_seconds、上限 "
        "perception.wait_max_seconds（可配置）；for_text 非空时等文本出现。"
    ),
    "phone_clean_background": "按白名单逐包清理后台，保留常驻服务和当前前台；可指定包名，保留项仍受保护。",
    "phone_skill": "读取内置 Skill 手册；name 空列出全部。",
    "phone_app_memory": (
        "查 App 使用经验（布局/入口/步骤）；操作不熟悉 App 或固定入口任务前先查。"
    ),
    "phone_app_remember": (
        "记住/更新 App 布局与操作方式（app+note+page+steps）；改版后覆盖更新。"
    ),
}


LAST_TOOL_SCHEMA_STATS: dict[str, int] = {"before": 0, "after": 0}


def _compact_plain_text(text: str, limit: int = 50) -> str:
    """取首句并截断，用于参数描述压缩。"""
    value = str(text or "").strip()
    for separator in ("。", "；", ";", "\n"):
        if separator in value:
            value = value.split(separator, 1)[0].strip()
            break
    if len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return value or value


def _compact_parameters(schema: Any) -> Any:
    """递归压缩参数描述（参数名/类型/默认值/required 保持不变）。"""
    if not isinstance(schema, dict):
        return schema
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "description" and isinstance(value, str):
            result[key] = _compact_plain_text(value)
        elif key in {"properties", "items"} and isinstance(value, dict):
            result[key] = {
                name: _compact_parameters(item)
                for name, item in value.items()
            }
        else:
            result[key] = value
    return result


def _tool_schema_chars(tools: list[FunctionTool]) -> int:
    total = 0
    for tool in tools:
        total += len(str(getattr(tool, "name", "")))
        total += len(str(getattr(tool, "description", "")))
        try:
            total += len(json.dumps(tool.parameters, ensure_ascii=False))
        except (TypeError, ValueError):
            total += len(str(getattr(tool, "parameters", {})))
    return total


def _compact_tool_schemas(tools: list[FunctionTool]) -> None:
    """压缩全部工具的 description 与参数描述（不改变行为与参数名）。"""
    before = _tool_schema_chars(tools)
    for tool in tools:
        name = str(getattr(tool, "name", ""))
        compact = _TOOL_DESC_COMPACT.get(name)
        if compact:
            try:
                tool.description = compact
            except Exception:  # noqa: BLE001 - pydantic 不可变时忽略
                pass
        try:
            tool.parameters = _compact_parameters(tool.parameters)
        except Exception:  # noqa: BLE001
            pass
    after = _tool_schema_chars(tools)
    LAST_TOOL_SCHEMA_STATS["before"] = before
    LAST_TOOL_SCHEMA_STATS["after"] = after


def build_phone_tools(service: Any) -> list[FunctionTool]:
    """构造全套手机工具（顺序与 :data:`TOOL_NAMES` 一致）。"""
    tools: list[FunctionTool] = [
        PhoneStatusTool(service=service),
        PhoneScreenTool(service=service),
        PhoneTrafficTool(service=service),
        PhoneWireguardTool(service=service),
        PhoneClipboardTool(service=service),
        PhoneShellTool(service=service),
        PhoneTapTool(service=service),
        PhoneLongPressTool(service=service),
        PhoneSwipeTool(service=service),
        PhoneInputTextTool(service=service),
        PhoneKeyTool(service=service),
        PhoneOpenAppTool(service=service),
        PhoneCheckInboxTool(service=service),
        PhoneListAppsTool(service=service),
        PhoneScreenshotTool(service=service),
        PhoneBypassVerifyTool(service=service),
        PhoneShareToUserTool(service=service),
        PhoneBrowseTool(service=service),
        PhoneLearnTool(service=service),
        PhoneRecallDiaryTool(service=service),
        PhoneRecentChatTool(service=service),
        PhoneInteractionGateTool(service=service),
        PhoneInteractionRecordTool(service=service),
        PhoneStartBackgroundTaskTool(service=service),
        PhoneLiveScreenTool(service=service),
        PhoneReadScreenTool(service=service),
        PhoneSolveVerificationTool(service=service),
        PhoneWaitTool(service=service),
        PhoneCleanBackgroundTool(service=service),
        PhoneSkillTool(service=service),
        PhoneAppMemoryTool(service=service),
        PhoneAppRememberTool(service=service),
        PhoneWatchMediaTool(service=service),
        PhoneContentTool(service=service),
        PhoneSaveImageTool(service=service),
        PhoneSavedImagesTool(service=service),
        PhoneFilesTool(service=service),
        PhoneRecordMediaTool(service=service),
    ]
    names = tuple(tool.name for tool in tools)
    if names != TOOL_NAMES:  # pragma: no cover - 常量与实现不一致时直接暴露
        raise RuntimeError(f"工具注册顺序与 TOOL_NAMES 不一致：{names}")
    _compact_tool_schemas(tools)
    return tools
