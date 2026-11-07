"""LLM 工具层：把手机操控能力注册成 AstrBot 的 Function-calling 工具。

39 个工具（status/screen/traffic/wireguard/clipboard/shell/tap/long_press/swipe/input_text/key/open_app/
check_inbox/list_apps/screenshot/bypass_verify/solve_verification/share_to_user/
browse/learn/recall_diary/recent_chat/wait/clean_background/skill/
interaction_gate/interaction_record/start_background_task/task_template/live_screen/read_screen/
app_memory/app_remember/watch_media/content/files/record_media）全部是 :class:`FunctionTool` 子类，构造时注入
:class:`PhoneService`（``core/service.py`` 中的能力门面）。
行为规则主要写在插件 ``skills/*/SKILL.md`` 里，由 ``phone_skill`` 工具按需读取。

约定：

* 所有可预期失败都转成 ``"error: ..."`` 文本返回给模型，不向上抛；
* 管理员门禁：``event.role == "admin"``、``allowed_user_ids`` 白名单、cron 主动任务
  事件（平台名 ``cron`` / ``CronMessageEvent``）放行；
* 截图通过 ``mcp.types.CallToolResult`` 的 ``ImageContent`` 回传模型，与内置 CUA
  截图工具同模式；发给用户走 ``event.send(MessageChain().file_image(path))``。"""

from __future__ import annotations

from .. import skills as skills_mod
from .. import verification as verification_mod
from ..apps import app_label_for_package, parse_package_list
from ..device_gate import ExecutionCancelled, GateError
from ..page_content import page_traffic_footer
from ..traffic import DEFAULT_LIMIT as TRAFFIC_DEFAULT_LIMIT, render_traffic
from ..memory_sync import (
    build_learning_payload,
    wants_companion,
    wants_livingmemory,
)
from ..config import UNCONFIGURED_MESSAGE, PhoneConfig
from ..errors import AdbNotConfiguredError, PhoneControlError
from ..live_screen import LiveFrame
from ..scheduler import (
    BackgroundTaskError,
    create_background_task,
    validate_background_instruction,
)
from ..interactions import (
    KIND_COMMENT,
    KIND_LABELS,
    KIND_PASSIVE_REPLY,
    InteractionError,
    evaluate_gate,
    normalize_kind,
    normalize_summary,
)
from ..share_content import join_share_text, pick_share_link, plan_share

from .base import (
    _as_int,
    _as_float,
    _json_text,
    _result_text,
    _error,
    _novelty_bored_note,
    _event_of,
    _is_cron_event,
    _agent_run_active,
    _sender_id_of,
    check_admin_permission,
    _ok,
    _image_result,
    _normalize_points,
    _PhoneToolBase,
    _PermissionDenied,
    _ToolNotice,
    _gate_error_text,
    _wrap_call,
)
from .device import (
    PhoneStatusTool,
    _SCREEN_QUIZ_WORDS,
    _SCREEN_QUIZ_HINT,
    _screen_quiz_hint,
    PhoneScreenTool,
    PhoneTapTool,
    PhoneLongPressTool,
    PhoneSwipeTool,
    PhoneInputTextTool,
    PhoneKeyTool,
    PhoneScreenshotTool,
    PhoneWaitTool,
)
from .app_tools import (
    PhoneOpenAppTool,
    PhoneCheckInboxTool,
    PhoneListAppsTool,
    _CLEAN_PACKAGE_RE,
    _MEMINFO_RE,
    _MEMINFO_COMMAND,
    _parse_meminfo,
    _meminfo_delta,
    _read_meminfo,
    PhoneCleanBackgroundTool,
    PhoneSkillTool,
)
from .share import PhoneShareToUserTool
from .clipboard import PhoneClipboardTool
from .shell import PhoneShellTool
from .content_task import PhoneContentTool
from .content import (
    PhoneBrowseTool,
    PhoneLearnTool,
    PhoneRecallDiaryTool,
    PhoneRecentChatTool,
    PhoneAppMemoryTool,
    PhoneAppRememberTool,
)
from .interaction import PhoneInteractionGateTool, PhoneInteractionRecordTool
from .task import PhoneStartBackgroundTaskTool
from .task_template import PhoneTaskTemplateTool
from .perception import (
    DEFAULT_OCR_PROMPT,
    _ocr_caption_settings,
    _resolve_ocr_provider,
    _live_frame,
    _frame_mime,
    _record_internal_usage,
)
from .verification_tools import (
    _VERIFY_ACTIONS,
    PhoneBypassVerifyTool,
    _CAPTCHA_TYPES,
    _CAPTCHA_TYPE_ALIASES,
    _VERIFY_MODES,
    _JSON_BLOCK_RE,
    _SOLVE_FIRST_PROMPT,
    _solve_second_prompt,
    _json_object_end,
    _parse_json_object,
    _extract_json,
    _verify_timeout_seconds,
    _capture_mime,
    _vision_response_text,
    _vision_json,
    _clean_solve_text,
    _as_bool,
    _normalize_captcha_type,
    _normalize_verify_ops,
    _normalize_confirm,
    _SolveRun,
    _append_text,
    _attempt_remaining,
    _attempt_summary,
    _outcome_label,
    _outcome_summary,
)
from .solve import PhoneSolveVerificationTool
from .screen_ai import PhoneLiveScreenTool, PhoneReadScreenTool, PhoneWatchMediaTool
from .traffic import PhoneTrafficTool
from .wireguard import PhoneWireguardTool
from .registry import (
    TOOL_NAMES,
    _TOOL_DESC_COMPACT,
    LAST_TOOL_SCHEMA_STATS,
    _compact_plain_text,
    _compact_parameters,
    _tool_schema_chars,
    _compact_tool_schemas,
    build_phone_tools,
)

from .save_image import PhoneSaveImageTool, PhoneSavedImagesTool
from .files import PhoneFilesTool, PhoneRecordMediaTool
