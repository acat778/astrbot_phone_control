# 当前节点采集组件

`UiCapture` 在设备 shell 身份下连接系统 `UiAutomation`，读取当前窗口可见节点；文本、描述、范围、状态、输入框提示以 XML 输出。连接在采集结束时释放，空根节点及树大小超过上限以失败报告。

运行要求 Android 8+，依赖设备 `/system/framework/uiautomator.jar` 提供的 shell 连接器。插件根据设备 ABI 选择 `app_process32` 或原生运行时；Java 入口与组件在同一进程执行。

构建时设置 `JAVA_HOME`（JDK 17+）、`ANDROID_HOME`（SDK 35）与 `OUT_DIR`（缓存目录），执行 `bash helper/ui-capture/build.sh`。将输出 `phone-ui-capture.jar` 放入 `bin/android/`；插件调用前核验设备文件摘要。
