"""根据设备 ABI 调用随包节点采集入口。"""

import shlex


def ui_dump_command(path: str, *, component: str) -> str:
    target = shlex.quote(path)
    classpath = shlex.quote(component + ":/system/framework/uiautomator.jar")
    return (
        'if [ -x /system/bin/app_process32 ] && [ -n "$(getprop ro.product.cpu.abilist32)" ]; '
        "then acat_ui_runtime=/system/bin/app_process32; else acat_ui_runtime=/system/bin/app_process; fi; "
        f"CLASSPATH={classpath} "
        '"$acat_ui_runtime" /system/bin fun.acat.uicapture.UiCapture ' + target
    )
