"""App 控制传输、配置兼容和取消的回归。"""

import asyncio
import base64
import pytest
from core.config import PhoneConfig
from core.app_bridge import AppBridge
from core.app_adb import AppAdbClient
from core.errors import ActionFailedError, DeviceOfflineError


def test_authenticated_traffic_status_preserves_tls_diagnostic_without_rpc_failure(tmp_path):
    import ssl
    from core.app_traffic import AppTrafficCapture

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        await bridge.start(0)
        ctx = ssl.create_default_context(cafile=str(tmp_path / "server.pem"))
        ctx.check_hostname = False
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port, ssl=ctx)
        try:
            await bridge.send(writer, {"v": 1, "token": bridge._state["token"], "device_id": "capture-status"})
            await bridge.read(reader)
            config = PhoneConfig.from_mapping({"adb": {"mode": "app"}, "traffic": {"provider": "app", "ca_mode": "manual"}})
            capture = AppTrafficCapture(adb=None, bridge=bridge, data_dir=tmp_path, settings_provider=lambda: config.traffic)
            capture._active = True
            capture._session_id = bridge.session
            capture._capture_id = "a" * 32
            diagnostic = "SSLHandshakeException: SSLV3_ALERT_CERTIFICATE_UNKNOWN"
            for _ in range(2):
                pending = asyncio.create_task(capture.ensure_device())
                request = await asyncio.wait_for(bridge.read(reader), 2)
                assert request["op"] == "traffic_status"
                await bridge.send(writer, {"type": "result", "id": request["id"], "rpc_ok": True, "running": True,
                                           "capture_id": capture._capture_id, "failures": 3, "dropped": 1, "error": diagnostic})
                await asyncio.wait_for(pending, 2)
                status = capture.status()
                assert status["running"] and status["error"] == diagnostic
                assert status["failures"] == 3 and status["dropped"] == 1
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("response", [{"rpc_ok": False, "error": "ADB disconnected"}, {"error": "ADB disconnected"},
                                     {"rpc_ok": "true", "error": "ADB disconnected"}, {"rpc_ok": False}])
def test_rpc_failure_is_reported_once_and_never_replayed(tmp_path, response):
    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        bridge.writer = object()
        sent = []

        async def send(writer, request):
            sent.append(request)
            bridge.pending[request["id"]].set_result(response)

        bridge.send = send
        with pytest.raises(ActionFailedError, match="App 操作失败"):
            await bridge.call("shell", command="input tap 1 1")
        assert len(sent) == 1 and sent[0]["op"] == "shell"
        assert not bridge.pending

    asyncio.run(run())


def test_existing_adb_defaults_and_hidden_values():
    direct = PhoneConfig.from_mapping({"adb": {"host": "device", "port": 5555}})
    app = PhoneConfig.from_mapping(
        {"adb": {"mode": "app", "host": "device", "port": 5555}}
    )
    assert direct.adb.mode == "direct" and direct.adb.target == "device:5555"
    assert app.adb.configured and app.adb.host == "device" and app.adb.port == 5555
    assert app.adb.target == "Relay App"


def test_app_never_runs_host_adb(tmp_path):
    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        adb = AppAdbClient(bridge, tmp_path)
        with pytest.raises(DeviceOfflineError):
            await adb.shell("input tap 1 1")
        with pytest.raises(ActionFailedError, match="文件"):
            await adb.run(["push", "a", "b"])
        with pytest.raises(ActionFailedError):
            await adb.exec_out("cat", "/data/private")

    asyncio.run(run())


def test_app_shell_preserves_exit_code_without_retry(tmp_path):
    class Bridge:
        calls = 0

        async def call(self, op, **params):
            self.calls += 1
            return {
                "rc": 7,
                "stdout": base64.b64encode(b"out").decode(),
                "stderr": base64.b64encode(b"failed").decode(),
            }

    async def run():
        bridge = Bridge()
        adb = AppAdbClient(bridge, tmp_path)
        result = await adb.shell_result("false", timeout=1)
        assert result == {"exit_code": 7, "stdout": "out", "stderr": "failed"}
        assert bridge.calls == 1

    asyncio.run(run())


def test_app_timeout_sends_cancel_without_replay(tmp_path):
    class Writer:
        pass

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        bridge.writer = Writer()
        sent = []

        async def send(writer, value):
            sent.append(value)

        bridge.send = send
        with pytest.raises(ActionFailedError, match="不重试"):
            await bridge.call("shell", timeout=0.02, command="input tap 1 1")
        assert [x["type"] for x in sent] == ["call", "cancel"]
        assert not bridge.pending

    asyncio.run(run())


def test_tls_identity_binding_and_disconnect(tmp_path):
    import ssl

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        await bridge.start(0)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        async def connect(token, device):
            r, w = await asyncio.open_connection("127.0.0.1", bridge.port, ssl=ctx)
            import hashlib

            assert (
                hashlib.sha256(
                    w.get_extra_info("ssl_object").getpeercert(binary_form=True)
                ).hexdigest()
                == bridge.fingerprint
            )
            await bridge.send(w, {"v": 1, "token": token, "device_id": device})
            return r, w

        try:
            r, w = await connect("wrong", "device")
            assert await r.read() == b""
            w.close()
            await w.wait_closed()
            r, w = await connect(bridge._state["token"], "device")
            assert (await bridge.read(r))["type"] == "ready"
            assert bridge._state["device_id"] == "device"
            call = asyncio.create_task(bridge.call("shell", command="id"))
            request = await bridge.read(r)
            assert request["params"]["command"] == "id"
            w.close()
            await w.wait_closed()
            with pytest.raises(DeviceOfflineError):
                await call
            r, w = await connect(bridge._state["token"], "another-device")
            assert await r.read() == b""
            w.close()
            await w.wait_closed()
        finally:
            await bridge.close()

    asyncio.run(run())


def test_app_nodes_use_single_dump(tmp_path):
    class Bridge:
        async def call(self, op, **params):
            assert op == "nodes"
            return {"rc": 0, "stdout": base64.b64encode(b"<hierarchy/>").decode()}

    async def run():
        client = AppAdbClient(Bridge(), tmp_path)
        assert (
            await client.exec_out("uiautomator", "dump", "/dev/tty") == b"<hierarchy/>"
        )

    asyncio.run(run())


def test_bridge_close_with_live_tls_connection(tmp_path):
    import ssl

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        await bridge.start(0)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", bridge.port, ssl=ctx
        )
        await bridge.send(
            writer, {"v": 1, "token": bridge._state["token"], "device_id": "device"}
        )
        assert (await bridge.read(reader))["type"] == "ready"
        await asyncio.wait_for(bridge.close(), 3)
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
        assert not bridge.tasks and bridge.server is None

    asyncio.run(run())


def test_request_sequences_continue_and_reset_on_reconnect(tmp_path):
    import ssl

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        await bridge.start(0)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        async def connect():
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", bridge.port, ssl=ctx
            )
            await bridge.send(
                writer,
                {"v": 1, "token": bridge._state["token"], "device_id": "device"},
            )
            ready = await bridge.read(reader)
            return reader, writer, ready["session"]

        async def respond(reader, writer, count, session):
            ids = set()
            for sequence in range(1, count + 1):
                request = await bridge.read(reader)
                assert request["seq"] == sequence
                assert request["session"] == session
                assert request["id"] not in ids
                ids.add(request["id"])
                await bridge.send(
                    writer, {"type": "result", "id": request["id"], "rc": 0}
                )

        try:
            reader, writer, session = await connect()
            peer = asyncio.create_task(respond(reader, writer, 1100, session))
            for _ in range(275):
                await asyncio.gather(
                    *(bridge.call("shell", command="id") for _ in range(4))
                )
            await peer
            writer.close()
            await writer.wait_closed()
            while bridge.writer is not None:
                await asyncio.sleep(0)
            reader, writer, new_session = await connect()
            assert new_session != session
            peer = asyncio.create_task(respond(reader, writer, 1, new_session))
            await bridge.call("shell", command="id")
            await peer
            writer.close()
            await writer.wait_closed()
        finally:
            await bridge.close()

    asyncio.run(run())


def test_phone_video_frame_uses_authenticated_app_channel(tmp_path):
    from PIL import Image
    import io

    image = io.BytesIO()
    Image.new("RGB", (16, 32), "blue").save(image, format="JPEG")

    class Bridge:
        calls = []

        async def call(self, operation, **params):
            self.calls.append(operation)
            return {
                "image": base64.b64encode(image.getvalue()).decode(),
                "format": "jpeg",
                "source": "app_scrcpy",
                "age_ms": 1,
            }

    async def scenario():
        bridge = Bridge()
        adb = AppAdbClient(bridge, tmp_path)
        data, fmt, duration = await adb.capture_screen()
        assert data == image.getvalue() and fmt == "jpeg" and duration >= 0
        assert bridge.calls == ["screen"] and adb.screen_source == "app_scrcpy"
        assert (
            PhoneConfig.from_mapping(
                {"adb": {"app_screen_source": "按需截图"}}
            ).adb.app_screen_source
            == "screencap"
        )
        assert (
            AppAdbClient(bridge, tmp_path, screen_source="screencap").screen_source
            == "app_screencap"
        )

    asyncio.run(scenario())


def test_mitm_secondary_tls_channel_requires_current_control_session(tmp_path):
    import ssl
    import secrets

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        await bridge.start(0)
        ssl_client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_client.check_hostname = False
        ssl_client.verify_mode = ssl.CERT_NONE

        async def echo(reader, writer):
            try:
                while chunk := await reader.read(16384):
                    writer.write(chunk)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        bridge.proxy_port = upstream.sockets[0].getsockname()[1]
        bridge.proxy_capture_id = secrets.token_hex(16)
        main_writer = None
        try:
            main_reader, main_writer = await asyncio.open_connection(
                "127.0.0.1", bridge.port, ssl=ssl_client
            )
            import hashlib

            assert (
                hashlib.sha256(
                    main_writer.get_extra_info("ssl_object").getpeercert(
                        binary_form=True
                    )
                ).hexdigest()
                == bridge.fingerprint
            )
            await bridge.send(
                main_writer,
                {"v": 1, "token": bridge._state["token"], "device_id": "fixture"},
            )
            ready = await bridge.read(main_reader)
            bridge.proxy_session = bridge.session
            hello = {
                "v": 1,
                "type": "proxy",
                "token": bridge._state["token"],
                "device_id": "fixture",
                "session": ready["session"],
                "capture_id": bridge.proxy_capture_id,
            }
            for key in ("token", "session", "capture_id", "device_id"):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", bridge.port, ssl=ssl_client
                )
                await bridge.send(writer, {**hello, key: "wrong"})
                assert await asyncio.wait_for(reader.read(), 2) == b""
                writer.close()
                await writer.wait_closed()
                assert bridge.writer is not None
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", bridge.port, ssl=ssl_client
            )
            await bridge.send(writer, hello)
            assert (await bridge.read(reader))["type"] == "proxy_ready"
            writer.write(b"proxy-data")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(10), 2) == b"proxy-data"
            main_writer.close()
            await main_writer.wait_closed()
            main_writer = None
            assert await asyncio.wait_for(reader.read(), 3) == b""
            writer.close()
            await writer.wait_closed()
            assert not bridge.proxy_tasks
        finally:
            if main_writer is not None:
                main_writer.close()
                await main_writer.wait_closed()
            await bridge.close()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(run())


def test_legacy_app_preserves_on_demand_screenshot_until_upgrade(tmp_path):
    from PIL import Image
    import io

    class Bridge:
        peer = {"version": "0.3.7"}

        async def call(self, operation, **params):
            raise AssertionError(
                "legacy screenshot should use the established screencap contract"
            )

    async def run():
        adb = AppAdbClient(Bridge(), tmp_path)
        image = io.BytesIO()
        Image.new("RGB", (720, 1280), "blue").save(image, format="PNG")

        async def exec_out(*args, **params):
            assert args == ("screencap", "-p")
            return image.getvalue()

        adb.exec_out = exec_out
        data, fmt, _ = await adb.capture_screen()
        assert data == image.getvalue() and fmt == "png"
        assert adb.screen_source == "app_screencap"
        adb.bridge.peer = {"version": "0.4.2"}
        assert adb.screen_source == "app_scrcpy"

    asyncio.run(run())


def test_ready_callback_can_request_device_without_blocking_socket_reader(tmp_path):
    import ssl

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        completed = asyncio.Event()

        async def ready():
            result = await bridge.call("traffic_status", timeout=2)
            assert result["running"] is False
            completed.set()

        bridge.on_ready = ready
        await bridge.start(0)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", bridge.port, ssl=ctx
        )
        try:
            await bridge.send(
                writer,
                {"v": 1, "token": bridge._state["token"], "device_id": "fixture"},
            )
            assert (await bridge.read(reader))["type"] == "ready"
            request = await asyncio.wait_for(bridge.read(reader), 2)
            assert request["op"] == "traffic_status"
            await bridge.send(
                writer, {"type": "result", "id": request["id"], "running": False}
            )
            await asyncio.wait_for(completed.wait(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(bridge.close(), 3)
        assert not bridge.tasks

    asyncio.run(run())


def test_typed_foreground_query_preserves_frame_operation_generation(tmp_path):
    from core.actions import DeviceActions
    class Bridge:
        peer={'features':['frame_stream']}
        async def call(self,operation,**params):
            assert operation=='foreground'
            return {'rc':0,'stdout':base64.b64encode(b'mCurrentFocus=Window{test u0 org.example/.Main}').decode(),'video_generation':7}
    async def run():
        adb=AppAdbClient(Bridge(),tmp_path);adb._connected=True;adb._video_generation=7
        package,_=await DeviceActions(adb).foreground()
        assert package=='org.example' and adb._video_generation==7
    asyncio.run(run())


def test_authenticated_frame_options_reach_phone_without_losing_capability(tmp_path):
    import ssl
    import time
    async def run():
        bridge=AppBridge(tmp_path,tmp_path/'app.apk');await bridge.start(0)
        ctx=ssl.create_default_context(cafile=str(tmp_path/'server.pem'))
        ctx.check_hostname=False
        reader,writer=await asyncio.open_connection('127.0.0.1',bridge.port,ssl=ctx)
        try:
            await bridge.send(writer,{'v':1,'token':bridge._state['token'],'device_id':'profile-test','version':'0.7.1',
                                      'features':['video_standby','frame_options'],'sdk_int':35,'adb_uid':2000,'su_present':False,'endpoint_host':'127.0.0.1','endpoint_port':45077,'relay_host':'100.64.0.5','relay_port':6190})
            assert (await bridge.read(reader))['type']=='ready'
            assert 'frame_options' in bridge.peer['features']
            adb=AppAdbClient(bridge,tmp_path,frame_max_size=720,frame_fps=5,frame_quality=65)
            task=asyncio.create_task(adb.configure_video(enabled=True))
            request=await bridge.read(reader)
            assert request['op']=='screen_config'
            assert request['params']=={'enabled':True,'standby':True,'max_size':720,'fps':5,'quality':65}
            await bridge.send(writer,{'type':'result','id':request['id'],'session':bridge.session,'ok':True,'elapsed_ms':int(time.monotonic()*1000)})
            await task
            assert bridge.peer['adb_uid']==2000 and bridge.peer['sdk_int']==35
            assert bridge.peer['relay_host']=='100.64.0.5' and bridge.peer['relay_port']==6190
        finally:
            writer.close();await writer.wait_closed();await bridge.close()
    asyncio.run(run())


def test_unsupported_phone_quality_settings_require_upgrade_but_stopping_works(tmp_path):
    class Bridge:
        peer={'features':['video_standby']}
        session='session'
        calls=[]
        async def call(self,operation,**params):
            self.calls.append((operation,params));return {'ok':True}
    async def run():
        bridge=Bridge();adb=AppAdbClient(bridge,tmp_path,frame_fps=5)
        with pytest.raises(ActionFailedError,match='升级'):await adb.configure_video(enabled=True)
        assert not bridge.calls
        await adb.configure_video(enabled=False)
        assert bridge.calls==[('screen_config',{'enabled':False,'standby':True})]
    asyncio.run(run())
