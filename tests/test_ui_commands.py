"""实际执行运行时选择脚本，核对分支、退出码和路径传参。"""

import os
import shlex
import subprocess

import pytest

from core.ui_commands import ui_dump_command


@pytest.mark.parametrize(
    "available,supported,expected",
    [(True, True, "32"), (False, True, "native"), (True, False, "native")],
)
def test_runtime_selection_uses_available_system_abi(
    tmp_path, available, supported, expected
):
    runtime = tmp_path / "app process32"
    native = tmp_path / "uiautomator"
    getprop = tmp_path / "getprop"
    runtime.write_text('#!/bin/sh\nprintf "32:%s:%s\\n" "$CLASSPATH" "$*"\n')
    runtime.chmod(0o700 if available else 0o600)
    native.write_text('#!/bin/sh\nprintf "native:%s\\n" "$*"\n')
    native.chmod(0o700)
    getprop.write_text('#!/bin/sh\nprintf "%s\\n" "$UI_TEST_ABI32"\n')
    getprop.chmod(0o700)
    command = ui_dump_command("/data/local/tmp/current view.xml", component="/data/local/tmp/current.jar").replace(
        "/system/bin/app_process32", shlex.quote(str(runtime))
    ).replace("/system/bin/app_process", str(native))
    result = subprocess.run(
        ["sh", "-c", command],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "UI_TEST_ABI32": "x86" if supported else "",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(expected + ":")
    assert result.stdout.rstrip().endswith("fun.acat.uicapture.UiCapture /data/local/tmp/current view.xml")
    if expected == "32":
        assert "/system/framework/uiautomator.jar" in result.stdout
        assert "fun.acat.uicapture.UiCapture" in result.stdout


def test_runtime_crash_exit_is_propagated(tmp_path):
    runtime = tmp_path / "app_process32"
    runtime.write_text(
        '#!/bin/sh\necho "UI hierchary dumped to: /sdcard/window_dump.xml"\nexit 139\n'
    )
    runtime.chmod(0o700)
    command = (
        ui_dump_command("/sdcard/window_dump.xml", component="/data/local/tmp/current.jar")
        .replace("/system/bin/app_process32", shlex.quote(str(runtime)))
        .replace("$(getprop ro.product.cpu.abilist32)", "x86")
    )
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == 139
