"""屏幕中的定位文本可原样用于当前节点查询。"""

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.models import Rect, ScreenSnapshot, UiNode
from core.service import PhoneService
from core.ui import render_ui


@pytest.mark.parametrize("field", ["text", "desc"])
@pytest.mark.parametrize("value,limit", [
    ("绯恒 12小时前·回复了我的评论 " + "这是一段很长的评论" * 20, 100),
    ('第一行\n第二行\t带"引号"和\\路径', 100),
    ("原文就有…省略号" + "继续阅读" * 30, 12),
    ("短文本", 100),
])
def test_displayed_locator_resolves_original_node(tmp_path, field, value, limit):
    async def scenario():
        target = UiNode(ref=1, **{field: value}, clickable=True,
                        bounds=Rect(20, 100, 320, 200))
        other = UiNode(ref=2, **{field: "另一个控件"}, clickable=True,
                       bounds=Rect(20, 300, 320, 400))
        snapshot = ScreenSnapshot(width=720, height=1280, nodes=[target, other])
        rendered = render_ui(snapshot, max_text_len=limit)
        line = next(line for line in rendered.splitlines() if line.startswith("[1]"))
        encoded = re.search(r'\b' + field + r'=("(?:[^"\\]|\\.)*")', line)
        assert encoded, line
        locator = json.loads(encoded.group(1))
        assert locator and locator in value
        assert len(locator) <= limit
        service = PhoneService(raw_config={}, adb=None,
                               actions=SimpleNamespace(screen_size=AsyncMock(return_value=(720, 1280))),
                               diary=None, data_dir=tmp_path)
        service.screen = AsyncMock(return_value=(rendered, snapshot))
        found = await service.find_node(**{field: locator})
        assert found is target
        assert await service.node_click_point(found) == (170, 150)
        assert (field + "_truncated=true" in line) == (len(value) > limit)

    asyncio.run(scenario())
