"""插件共享常量与提示词（供入口 main.py 与运行逻辑 mixin 共用）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

from ..plugin_shared import (
    _INBOX_EXCLUDE_WORDS,
    _INBOX_KEYWORDS,
    _format_time,
    _llm_response_text,
)
from ..config import PhoneConfig
from ..result_delivery import DELIVERY_GUIDANCE
from ..identity import PLUGIN_NAME
from ..play_flow import LIVE_BROWSE_FLOW, render_flexibility, DIRECT_BROWSE_FLOW, FLOW_RULE, flow_rule_text, break_rule_hint, share_cooldown_text
from ..share_policy import share_window_text


def plugin_root_dir() -> Path:
    """插件根目录（core/plugin/shared.py 向上两级）。"""
    return Path(__file__).resolve().parents[2]





PHONE_KEYWORDS = (
    "手机",
    "截图",
    "点击",
    "点一下",
    "滑动",
    "打开",
    "评论",
    "发帖",
    "弹幕",
    "b站",
    "抖音",
    "小黑盒",
    "后台任务",
    "玩手机",
    "验证码",
    "quota",
    "视频",
    "直播",
    "音频",
    "声音",
)


_PHONE_CONTROL_TOOLS = frozenset(
    {
        "phone_tap",
        "phone_shell",
        "phone_long_press",
        "phone_swipe",
        "phone_input_text",
        "phone_key",
        "phone_open_app",
        "phone_browse",
        "phone_watch_media",
        "phone_content",
        "phone_save_image",
        "phone_saved_images",
    }
)


_GUIDANCE = (
    "【手机操控能力】39 个 phone_* 工具；先 phone_skill 读手册。重复任务可用 phone_task_template 生成参数草案，展示正文和参数后由用户发送确认命令保存；已确认模板可带参数运行。\n应用安全：先 phone_list_apps 查看模型识别类型；金融类应用拒绝打开，类型未确认或模型失败也拒绝。打开应用必须用 phone_open_app/phone_browse，不得改用桌面图标、深链或 shell 绕过分类。\n"
    + DELIVERY_GUIDANCE + "\n"
    "动作结果：已点击/已输入仅说明指令执行。重要操作须传expect结果条件（目标页新文本、提交成功提示或节点文字变化），工具返回execution_succeeded/结果已确认才能认定目标达成；result_unconfirmed时先观察核对，不盲目重放或声称提交成功。failed保留实际执行状态与失败原因。\n"
    "存图：开关开启后图文 read 按兴趣 phone_save_image，填写描述/标签/理由，按配置存服务端/手机/两地；phone_saved_images 可查询服务端/手机本地图片；发帖/回复按语义决定是否配图，相关时 search 阅读描述、gate 预约，再 prepare(kind/reason) 并在 App 选图核对预览，无合适图片用文字。保存与分享独立。\n"
    "静默：仅主人可用；搜索/点按/失败重试过程不发文字，交付形式按用户要求执行。\n"
    "Token 额度按 quota 配置；每日互动 -1 不限、0 禁止、正数限额。\n"
    "App 经验：打开 App/读取屏幕后会自动按当前 App、当前页面召回经验，按召回内容操作；界面与经验不符或操作报错说明经验可能失效，先重新探索，再用 phone_app_remember 覆盖更新（成功后清除失效标记）。\n"
    "内容任务：图文打开前begin，视频确认起点后begin；phone_content 分段read、独立决定续看/看评论/回复，完成finish。看评论不等于回复。\n"
    "任务优先：用户手机任务优先于 AI 自刷/随机/后台任务；自刷被暂停时立即收尾让路。\n"
    "看与分享：" + FLOW_RULE + "看和分享是两次独立判断（先决定看不看、看完再判断分不分享），"
    "只有确实值得对方看才 phone_share_to_user，且必须带内容链接或截图（链接优先：App 分享按钮"
    "或 phone_traffic 的「可分享链接」；复制链接后用 phone_clipboard 读取；拿不到链接必须 "
    "include_screenshot=true），禁止纯文字；帖子默认 content_type=post 回顶后截图，视频传 "
    "video、当前画面传 screen；分享目标先 phone_recent_chat 看最近聊天再按对方喜好选择。\n"
    "新鲜感：同类内容刷多了会看腻（novelty.* 可配，阈值 0=关闭）；提示词会列出已看腻/"
    "接近阈值的主题，刷到同类内容时降低打开/停留概率，优先换主题或换 App；一段时间不看"
    "会自然恢复。\n"
    "页面内容识别：优先 phone_screen（抓包可用时自动附带当前前台 App 的接口内容，"
    "已按最近手机操作与时效过滤）；图文、视频画面、布局等视觉信息看截图/OCR；"
    "抓包不可用、不属于当前 App 或已过期时自动回退屏幕识别；需要原始流量明细或"
    "可分享链接再用 phone_traffic。\n"
    "流媒体内容识别：视频/直播/音乐只有标题和评论不算看懂；刷到值得理解的内容（不是每条）"
    "用一次 phone_watch_media 录一小段（默认 15–30 秒，上限按配置）——它会转写声音并描述关键画面。"
    "识别后把听到/看到写进汇报或 phone_learn；同一段内容不要反复识别；直播间按需限时；"
    "无音频时如实说明只用画面。\n"
    "验证：solver/bypass/普通写操作共享自动尝试次数与总时限（配置 verification.*）；"
    "达到上限会暂停手机并通知用户协助，不要继续刷验证页；收到暂停提示立即收尾。\n"
    "评论/回复：评论区与被动回复一开始必须用 UI 操作（先确认回复对象/楼层/用户，"
    "用 phone_tap/input_text 完成）；不要刚上手就用抓包记录直接调接口发回复。"
    "等熟悉该 App 的回复流程、并积累足够抓包记录后，才可参考之前抓包的请求格式逐步尝试用 API 发送。\n"
    "互动：主动评论=刷帖/视频时自己有感触才发（kind=comment）；被动回复=进入 App 先 "
    "phone_check_inbox 看「回复我的/互动消息」，读内容后按心情与内容决定是否回"
    "（kind=passive_reply），不想回就不回。两者都要先 gate 再 record。\n"
    "多步任务先 phone_start_background_task；控制优先 scrcpy（仅控制通道，低开销），"
    "失败回退 adb；实时/验证码用 phone_live_screen / phone_read_screen 按需取帧。\n"
    "人机验证/做题：优先 phone_solve_verification（一次调用内部两次视觉模型：第一次识图+"
    "判断是否人机验证/做题，第二次重新截图给操作顺序+最终确认点并自动执行）；需要手动精细"
    "控制再用 phone_bypass_verify（capture→drag/tap_points）；答题/测试页不要反复 phone_wait/"
    "phone_screen 干等。\n"
    "学习：持续刷手机每轮结束用 phone_learn 记录 1–2 条真正学到的内容。\n"
    "收尾：玩完/任务结束插件会自动回桌面并清后台，无需手动调 phone_clean_background；"
    "用户 /phone stop 可停止后台/持续刷任务。"
)


def _companion_plugin_available() -> bool:
    """检测「我会永远陪着你」陪伴插件是否可用（用于心情来源降级）。"""
    try:
        return (Path(get_astrbot_plugin_path()) / "astrbot_plugin_private_companion").is_dir()
    except Exception:  # noqa: BLE001 - 检测失败按不可用降级
        return False


def _build_guidance(config: PhoneConfig | None = None) -> str:
    """生成手机能力指南（心情/互动提示按运行时条件追加，保持简短）。"""
    if _companion_plugin_available():
        mood = "心情：按陪伴插件注入的状态表达，不暴露数值。"
    else:
        mood = "心情：按人格与最近记忆推断，不编造数值。"
    enabled = bool(config and config.flexibility.free_browse_active)
    guidance = _GUIDANCE.replace(FLOW_RULE, flow_rule_text(enabled))
    if enabled:
        guidance = guidance.replace(
            "内容任务：图文打开前begin，视频确认起点后begin；phone_content 分段read、独立决定续看/看评论/回复，完成finish。看评论不等于回复。",
            DIRECT_BROWSE_FLOW,
        )
    if config and config.flexibility.interest_active:
        guidance = guidance.replace(
            "刷到同类内容时降低打开/停留概率，优先换主题或换 App",
            "新鲜感阈值作参考，可按真实意愿继续看同类内容",
        )
    parts = [guidance, mood, LIVE_BROWSE_FLOW]
    if config is not None:
        auto = config.auto
        window = share_window_text(
            start_hour=auto.share_start_hour,
            end_hour=auto.share_end_hour,
            tz_name=auto.share_timezone,
        )
        quiet = bool(auto.share_quiet_hours_enabled)
        parts.append(
            "分享：" + flow_rule_text(enabled)
            + "看和分享是两次独立判断（先决定看不看、看完再单独判断分不分享）；"
            "分享必须带链接或截图（链接优先：App 分享按钮，或从 phone_traffic 的"
            "「可分享链接」取 link；拿不到链接用 include_screenshot=true 截图），"
            "禁止纯文字；同一内容只发一次、每轮≤3 条不刷屏；"
            + share_cooldown_text(auto.share_cooldown_minutes)
            + "目标由你根据 phone_recent_chat 的最近聊天内容自己判断（群聊按聊天内容，私聊只分享给"
            f"最近有交流的用户，{auto.share_user_recent_hours} 小时内聊过），不要用固定目标；"
            + (
                f"分享时段 {window}，其余时间为免打扰，自刷任务不主动分享。"
                if quiet
                else "已关闭分享免打扰。"
            )
        )
    security = config.security if config is not None else None
    if security is not None and (security.allow_interactions or security.allow_comments or security.allow_passive_replies or security.allow_posts):
        parts.append(
            "互动：对应开关开启后可 phone_interaction_gate 申请 1 次，"
            "自己完成后 record，评论/回复/发帖每轮最多 1 条。"
        )
    if security is not None:
        from ..interactions import interaction_plan
        parts.append(interaction_plan(security, config.flexibility))
        limit = security.max_likes_per_session
        parts.append(
            f"点赞/收藏：{'已授权' if security.allow_interactions else '未授权'}；"
            f"like/favorite 先 gate 再操作后 record，单轮合计上限 {limit} 次（0=禁止）；"
            "先看清内容并独立决定，按钮已激活时跳过，不要重复切换状态。"
        )
        parts.append(
            "设备 shell：" + ("已授权 phone_shell，仅在 Android 设备当前 ADB 权限下执行前台命令。"
                            if security.allow_shell_tool else "未授权 phone_shell。")
        )
    if enabled:
        parts.append(break_rule_hint(True))
    return render_flexibility("\n".join(parts), config.flexibility if config else None)


_HELP_TEXT = """手机操控插件指令：

/phone help           查看本帮助
/phone status         查看设备连接、前台 App、电量与分辨率
/phone connect        重新连接 ADB 设备
/phone screenshot     截图并发送到当前会话
/phone open <app>     打开通过类型识别的已安装 App（中文名/别名/包名）
/phone browse [app] [条数]  浏览通过类型识别的已安装 App 的信息流并记入记忆
/phone diary [条数|关键词]  查看/检索浏览日志
/phone auto on <app[,app]> [cron]  创建定时自主浏览任务（cron 默认取配置）
/phone auto off [job_id]           取消自主浏览任务
/phone auto list                   列出自主浏览任务
/phone quota [audit [小时]]       查看 token 额度与对账明细（audit=对照 AstrBot 实际 usage）
/phone play [tokens]              立即静默玩手机一轮（可指定本轮 token 预算）
/phone interactions [n]           查看点赞/收藏/评论/回复/发帖的授权、用量与最近记录
/phone continuous on|off|status [间隔秒] [token预算]  持续刷手机（一轮接一轮，可学习内容）
/phone stop [job_id|all]          停止本插件后台/持续刷任务，回桌面并清理后台
/phone learn [关键词|条数]         查看 AI 在手机上学到的内容档案
/phone traffic [status|tail [n]|on|off]  查看抓包状态/最近流量，或开关手机流量抓包
/phone network [status|start|login|stop]  当前所选组网服务的状态与控制（mesh 同义）
/phone wg [status|start|stop|restart|config]  查看/开关 WireGuard 组网，config 显示导入用配置
/phone verify [status|resume <id>|cancel <id>]  查看/恢复/取消验证等待（人工协助）
/phone watch [秒数] [问题]        识别当前正在播放的视频/直播/音乐（声音转写+关键画面）
/phone novelty [status|reset]     查看内容新鲜感/厌倦感状态（阈值、疲劳主题）或清空状态
/phone task <instruction>         把多步任务转入后台静默执行（完成后主动汇报）
/phone template list              查看当前用户会话的参数模板
/phone template confirm <草案ID>    确认保存已审阅的模板
/phone template run <JSON>         带参数运行已确认模板
/phone tasks [list]               查看本人会话任务与当前进度
/phone tasks status <任务ID>      查看等待原因、执行记录和结果证据
/phone tasks cancel <任务ID>      取消任务
/phone tasks evidence <任务ID> <截图ID>  查看关键截图
adb.host / adb.port 留空时插件以未启用状态运行（面板/配置可用）；在插件配置「ADB 连接」填写地址与端口后重新加载插件即可启用手机能力。
可在对话中直接让我使用 phone_* 工具操作手机。"""


def _read_plugin_version() -> str:
    """从 metadata.yaml 读取版本号（失败时回退空串）。"""
    try:
        text = (
            plugin_root_dir() / "metadata.yaml"
        ).read_text(encoding="utf-8")
        match = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
        return match.group(1) if match else ""
    except OSError:
        return ""


class _AutonomousDeviceEvent:
    """降级调度回调使用的合成事件：按 cron 自主任务参与设备门禁。"""

    def __init__(self, umo: str = "") -> None:
        self.unified_msg_origin = str(umo or "")
        self._extras: dict[str, Any] = {
            "cron_payload": {"origin": "phone_background_task"}
        }

    def get_platform_name(self) -> str:
        return "cron"

    def get_sender_id(self) -> str:
        return ""

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value
