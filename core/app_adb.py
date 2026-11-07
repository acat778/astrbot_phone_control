"""复用设备控制契约，通过 Relay App 的本机 ADB 执行。"""

from __future__ import annotations
import asyncio
import base64
import shlex
import io
import time
from .adb import AdbClient
from .app_bridge import APP_UNSUPPORTED
from .errors import ActionFailedError, AdbCommandError, DeviceOfflineError


class AppAdbClient(AdbClient):
    app_mode = True

    def __init__(
        self,
        bridge,
        home_dir,
        *,
        command_timeout=30,
        logger=None,
        screen_source="scrcpy",
        video_standby=True,
        frame_max_size=0, frame_fps=10, frame_quality=85, frame_max_age_ms=250,
    ):
        super().__init__(
            None,
            "relay-app",
            1,
            home_dir,
            defer_binary=True,
            command_timeout=command_timeout,
            logger=logger,
        )
        self.bridge = bridge
        self._video_screen = screen_source == "scrcpy"
        self._forwards = {}
        self._video_standby = video_standby
        self._frame_options = {"max_size": frame_max_size, "fps": frame_fps, "quality": frame_quality}
        self._frame_max_age_ms = frame_max_age_ms
        self._video_session = ""
        self._video_enabled = False
        self._video_generation = 0
        self._video_clock_offset_ms = None
        self._video_clock_synced_at = 0
        self.last_screen_meta = {}
        from .app_frames import AppFrames
        self._frames = AppFrames(self)

    @property
    def screen_source(self):
        version = str(getattr(self.bridge, "peer", {}).get("version", ""))
        parts = version.split(".")
        legacy = (
            len(parts) >= 2
            and parts[0] == "0"
            and parts[1].isdigit()
            and int(parts[1]) < 4
        )
        return "app_scrcpy" if self._video_screen and not legacy else "app_screencap"

    async def capture_screen(self, *, fast=False, timeout=None):
        if self.screen_source != "app_scrcpy":
            return await super().capture_screen(fast=fast, timeout=timeout)
        started = time.perf_counter()
        if "video_standby" in getattr(self.bridge, "peer", {}).get("features", []) and (self._video_session != self.bridge.session or not self._video_enabled):
            await self.configure_video(enabled=True)
        if self._video_standby and "frame_stream" in getattr(self.bridge,"peer",{}).get("features",[]):
            return await self._frames.capture(timeout or self._command_timeout)
        value = await self.bridge.call(
            "screen", timeout=timeout or self._command_timeout
        )
        data = base64.b64decode(value.get("image", ""), validate=True)
        from PIL import Image

        age = value.get("age_ms")
        if (
            value.get("source") != "app_scrcpy"
            or value.get("format") != "jpeg"
            or len(data) > 8 * 1024 * 1024
            or type(age) is not int
            or not 0 <= age <= 2000
        ):
            raise ActionFailedError("手机 scrcpy 视频帧格式无效，请检查 App 版本")
        with Image.open(io.BytesIO(data)) as frame:
            if frame.width * frame.height > 9000000 or frame.format != "JPEG":
                raise ActionFailedError("手机 scrcpy 视频帧尺寸无效")
            frame.load()
        captured=value.get("captured_at_ms")
        if type(captured) is int and self._video_clock_offset_ms is not None:
            age=max(age,time.monotonic()*1000-(captured+self._video_clock_offset_ms))
        else:
            age += (time.perf_counter()-started)*1000
        if age > self._frame_max_age_ms:
            raise ActionFailedError("手机视频帧超过可接受画面年龄")
        data=await asyncio.to_thread(self._frames.restore_size,data,value)
        self.last_screen_meta={"frame_age_ms":round(age,1),"frame_age_limit_ms":self._frame_max_age_ms}
        return data, "jpeg", round((time.perf_counter() - started) * 1000)

    async def configure_video(self, *, enabled):
        if "video_standby" not in getattr(self.bridge, "peer", {}).get("features", []):
            return
        if enabled and "frame_options" not in self.bridge.peer.get("features",[]) and self._frame_options!={"max_size":0,"fps":10,"quality":85}:
            raise ActionFailedError("当前App不支持所选画质参数，请从Relay App页面升级手机App")
        await self._frames.close()
        sent=time.monotonic()*1000
        options = self._frame_options if "frame_options" in self.bridge.peer.get("features", []) else {}
        value=await self.bridge.call("screen_config", enabled=enabled, standby=self._video_standby, **options)
        if "frame_stream" in self.bridge.peer.get("features",[]):
            self._set_video_clock(value,sent)
        if self._video_session != self.bridge.session:
            self._video_generation = 0
        self._video_session = self.bridge.session
        self._video_enabled = enabled
        if enabled and self._video_standby and "frame_stream" in self.bridge.peer.get("features",[]):
            self._frames.start()

    def _set_video_clock(self, value, sent):
        elapsed=value.get("elapsed_ms") if isinstance(value,dict) else None
        if type(elapsed) is not int or elapsed<0:raise ActionFailedError("手机视频时钟无效")
        # 以请求发出时刻作为保守下界，网络往返不会把旧帧误算成新帧。
        self._video_clock_rtt_ms=round(time.monotonic()*1000-sent,1)
        self._video_clock_offset_ms=sent-elapsed
        self._video_clock_synced_at=time.monotonic()

    async def refresh_video_clock(self):
        sent=time.monotonic()*1000
        value=await self.bridge.call("clock",timeout=10)
        self._set_video_clock(value,sent)

    async def stop_video(self):
        await self._frames.close()
        if self.bridge.writer is not None:
            await self.configure_video(enabled=False)
        self._video_enabled = False

    async def screenshot_png(self, *, timeout=None):
        if self.screen_source != "app_scrcpy":
            return await super().screenshot_png(timeout=timeout)
        data, _format, _duration = await self.capture_screen(timeout=timeout)
        from PIL import Image

        out = io.BytesIO()
        with Image.open(io.BytesIO(data)) as frame:
            frame.save(out, format="PNG")
        return out.getvalue()

    async def _is_online_raw(self):
        return self.bridge.writer is not None

    async def _do_connect(self, force=False):
        if not await self._is_online_raw():
            raise DeviceOfflineError("Relay App 未连接")
        info = await self._device_info(retry=False)
        self._connected = True
        return info

    async def _run_adb(self, argv, *, timeout=None, output_limit=None):
        args = list(argv)[3:]  # _base_command 的程序、-s 和 serial。
        if args[:3] == ["exec-out", "head", "-c"] and len(args) == 5:
            from .app_files import AppFiles
            try:
                maximum = int(args[3])
            except ValueError as exc:
                raise ActionFailedError("手机文件读取上限无效") from exc
            return 0, await AppFiles(self).read(args[4], maximum), b""
        if args and args[0] in ("push", "pull"):
            if len(args) != 3:
                raise ActionFailedError("文件传输需指定一个源文件和目标路径")
            from .app_files import AppFiles
            files = AppFiles(self)
            size = await (files.push(args[1], args[2]) if args[0] == "push" else files.pull(args[1], args[2]))
            return 0, (str(size) + " bytes transferred").encode(), b""
        if args and args[0] == "forward":
            return 0, (await self._forward(args[1:])).encode(), b""
        if not args or args[0] not in ("shell", "exec-out"):
            raise ActionFailedError(APP_UNSUPPORTED)
        if args[0] == "exec-out" and args[1:2] not in (
            ["screencap"],
            ["uiautomator"],
            ["cat"],
        ):
            raise ActionFailedError(APP_UNSUPPORTED)
        if (
            args[0] == "exec-out"
            and args[1:2] == ["cat"]
            and not all(
                x.startswith("/sdcard/") and x.endswith(".xml") for x in args[2:]
            )
        ):
            raise ActionFailedError(APP_UNSUPPORTED)
        command = args[1] if args[0] == "shell" else shlex.join(args[1:])
        if args == ["exec-out", "uiautomator", "dump", "/dev/tty"]:
            value = await self.bridge.call(
                "nodes", limit=8 * 1024 * 1024, timeout=timeout or self._command_timeout
            )
        else:
            value = await self.bridge.call(
                "shell",
                command=command,
                limit=output_limit or 8 * 1024 * 1024,
                timeout=timeout or self._command_timeout,
            )
        self._observe_generation(value)
        return (
            int(value["rc"]),
            base64.b64decode(value.get("stdout", ""), validate=True),
            base64.b64decode(value.get("stderr", ""), validate=True),
        )

    async def _execute(self, argv, *, timeout=None, retry=True):
        rc, out, err = await self._run_adb(argv, timeout=timeout)
        if rc:
            raise AdbCommandError(
                "App ADB 命令失败",
                returncode=rc,
                detail=err.decode("utf-8", "replace")[:500],
            )
        return rc, out, err

    async def _forward(self, args):
        from .app_streams import valid_service
        if args == ["--list"]:
            return "\n".join(f"relay-app tcp:{port} {entry['service']}" for port, entry in self._forwards.items())
        if args and args[0] == "--remove":
            if len(args) != 2 or not args[1].startswith("tcp:"):
                raise ActionFailedError("手机数据转发参数无效")
            entry = self._forwards.pop(int(args[1][4:]), None)
            if entry:
                entry["server"].close()
                tasks = list(entry["tasks"])
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await entry["server"].wait_closed()
            return ""
        if len(args) != 2 or not args[0].startswith("tcp:") or not valid_service(args[1]) or not args[1].startswith("localabstract:"):
            raise ActionFailedError("手机转发仅接受本机TCP与scrcpy数据流")
        port = int(args[0][4:])
        if not 0 <= port <= 65535 or port in self._forwards or len(self._forwards) >= 8:
            raise ActionFailedError("手机数据转发端口或并发无效")
        entry = {"tasks": set(), "service": args[1]}

        async def serve(reader, writer):
            task = asyncio.current_task(); entry["tasks"].add(task)
            copies = []
            try:
                async with self.bridge.streams.connect(args[1]) as (remote, upstream):
                    async def copy(source, target):
                        while chunk := await source.read(65536):
                            target.write(chunk)
                            await asyncio.wait_for(target.drain(), 15)
                    copies = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(remote, writer))]
                    done, _ = await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
                    for completed in done:
                        completed.result()
            except (OSError, ActionFailedError, DeviceOfflineError) as exc:
                self._log(f"手机数据转发结束：{exc}")
            finally:
                for child in copies:
                    child.cancel()
                await asyncio.gather(*copies, return_exceptions=True)
                writer.close(); entry["tasks"].discard(task)
        server = await asyncio.start_server(serve, "127.0.0.1", port)
        entry["server"] = server
        actual = server.sockets[0].getsockname()[1]
        self._forwards[actual] = entry
        return str(actual) if port == 0 else ""

    async def _close_forwards(self):
        for port in list(self._forwards):
            await self._forward(["--remove", f"tcp:{port}"])

    async def foreground_output(self):
        from .adb import FOCUS_DUMP_COMMAND
        if "frame_stream" not in getattr(self.bridge,"peer",{}).get("features",[]):
            return await self.shell(FOCUS_DUMP_COMMAND)
        value=await self.bridge.call("foreground",timeout=self._command_timeout)
        self._observe_generation(value)
        if value.get("rc") != 0:raise ActionFailedError("手机前台查询失败")
        return base64.b64decode(value.get("stdout",""),validate=True).decode("utf-8","replace")

    async def read_clipboard(self):
        value = await self.bridge.call("clipboard", timeout=self._command_timeout)
        return value.get("text", "")

    def _observe_generation(self, value):
        if "frame_stream" not in getattr(self.bridge,"peer",{}).get("features",[]):return
        generation=value.get("video_generation")
        if type(generation) is not int or generation<0:
            raise ActionFailedError("手机操作代次无效")
        self._video_generation=max(self._video_generation,generation)

    async def input_text(self, text, *, press_enter=False):
        value=await self.bridge.call("input",text=text,press_enter=press_enter,timeout=self._command_timeout)
        self._observe_generation(value)

    async def disconnect(self):
        await self._frames.close()
        await self._close_forwards()
        self._connected = False

    async def close(self):
        await self._frames.close()
        await self._close_forwards()
        await self.bridge.close()
        self._connected = False

    async def reconfigure(self, **kwargs):
        raise ActionFailedError("App 连接配置已保存，请停止任务并重新加载插件")
