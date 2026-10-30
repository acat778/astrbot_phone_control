"""节点采集失败时，显式请求的截图仍作为独立观察结果交付。"""

import asyncio
import base64
import importlib

import pytest

from test_main_0130_features import (
    FakeEvent, FakeToolContext, PhoneScreenTool, _cleanup, _make_plugin,
)

errors = importlib.import_module(PhoneScreenTool.__module__.rsplit('.', 2)[0] + '.errors')


@pytest.mark.parametrize('mode,include_screenshot', [('both', False), ('ui', True)])
def test_ui_failure_keeps_requested_image(tmp_path, monkeypatch, mode, include_screenshot):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        captures = []

        async def screen(**kwargs):
            raise errors.UiDumpError('uiautomator dump 失败', detail='could not get idle state')

        async def capture(**kwargs):
            captures.append(kwargs)
            return b'actual-frame', tmp_path / 'frame.png', {'method': 'png', 'elapsed_ms': 1}

        async def foreground():
            return 'com.example.test', '.Main'

        monkeypatch.setattr(plugin.service, 'screen', screen)
        monkeypatch.setattr(plugin.service, 'capture', capture)
        monkeypatch.setattr(plugin.service.actions, 'foreground', foreground)
        try:
            result = await PhoneScreenTool(service=plugin.service).call(
                FakeToolContext(FakeEvent()), mode=mode,
                include_screenshot=include_screenshot, include_traffic=False,
            )
            assert len(captures) == 1
            assert base64.b64decode(next(c.data for c in result.content if c.type == 'image')) == b'actual-frame'
            text = '\n'.join(c.text for c in result.content if c.type == 'text')
            assert 'could not get idle state' in text
            assert 'UI 节点不可用' in text
            assert 'mode=image' in text
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['ui_only', 'offline', 'cancelled'])
def test_ui_failure_preserves_error_and_cancellation(tmp_path, monkeypatch, kind):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        captures = []
        exc = {
            'ui_only': errors.UiDumpError('UI unavailable'),
            'offline': errors.DeviceOfflineError('device offline'),
            'cancelled': asyncio.CancelledError(),
        }[kind]

        async def screen(**kwargs):
            raise exc

        async def capture(**kwargs):
            captures.append(1)
            raise AssertionError('此路径必须保持错误或取消')

        monkeypatch.setattr(plugin.service, 'screen', screen)
        monkeypatch.setattr(plugin.service, 'capture', capture)
        try:
            call = PhoneScreenTool(service=plugin.service).call(
                FakeToolContext(FakeEvent()), mode='ui' if kind == 'ui_only' else 'both',
                include_screenshot=False, include_traffic=False,
            )
            if kind == 'cancelled':
                with pytest.raises(asyncio.CancelledError):
                    await call
            else:
                assert (await call).startswith('error:')
            assert not captures
        finally:
            await _cleanup(plugin)
    asyncio.run(scenario())
