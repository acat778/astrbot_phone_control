# 手机操控 · astrbot_phone_control_mirror

[![GitHub](https://img.shields.io/badge/GitHub-acat778%2Fastrbot__phone__control-181717.svg)](https://github.com/acat778/astrbot_phone_control)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

让 AstrBot 操作一台安卓模拟器：查看屏幕、浏览应用、理解图文与视频、记录学习，并按授权完成互动。

**版本：`0.2.1`** · **AstrBot：`>=4.27.1,<5`** · **Python：`>=3.10`**

[快速开始](#快速开始) · [功能](#功能) · [监控面板](#监控与资料管理) · [配置](#配置) · [组网与抓包](#组网与抓包)

[常用命令](#常用命令) · [停止与数据](#停止与数据) · [常见问题](#安全与常见问题) · [使用手册](docs/使用手册.md) · [更新记录](CHANGELOG.md)

## 快速开始

### 环境要求

| 项目 | 要求 |
| --- | --- |
| 宿主 | AstrBot `>=4.27.1,<5`，Python 3.10+；完整功能以 Linux x86_64 为验证平台 |
| 设备 | 开启 ADB 调试的安卓模拟器或真机；直连方式要求 ADB 可达，App 方式要求手机能访问插件接入端口 |
| ADB / scrcpy | ADB 缺失时从 Google 官方下载；scrcpy-server 5.0.1 随包提供 |
| 视频与中文输入 | 可选 PyAV/ffmpeg、ADBKeyboard、minitouch；实时解码和抓包依赖相应宿主组件 |
| 抓包 | 独立 venv 安装 mitmproxy，配置 `traffic.mitmdump_path`；系统 CA 模式需要 `adb root` |
| 组网 | WireGuard、Tailscale、ZeroTier 三选一；ZeroTier SDK 动态库需手动提供 |

### 连接方式

「ADB 连接」可选择「直连」或「使用 App 控制」，各自参数按选择显示，隐藏参数保留。已有配置使用直连，现有音视频、文件及抓包能力可继续使用。

App 模式由 [Phone ADB Relay](helper/android-relay/README.md) 内置客户端连接手机本机 adbd，再主动连接插件的 TLS 接入端口。支持状态、shell、应用操作、手势、中文输入、剪贴板、截图和节点；本机 TCP 调试或 Android 11+ 无线配对需先授权。切换前停止手机任务，保存后重载插件。

App 画面可选择手机端 scrcpy 视频取帧或按需截图。启用流量抓包后选择 mitmproxy；使用 App 控制时还可选择「App 抓包」，由手机端 HTTP(S) 代理采集并上传。App 模式的插件端 scrcpy 保留直连参数，直连模式按配置启用。

独立的「Relay App」页面提供 App 配对码、连接状态和 APK 下载。App 可手动检查已连接插件提供的新版 APK，校验后由用户确认系统安装；APK 与插件 metadata 独立版本化。详细网络配置、配对和覆盖升级见组件说明。

### 安装与首次连接

1. 在 AstrBot 安装插件，仓库地址为 `https://github.com/acat778/astrbot_phone_control`；也可上传插件 ZIP，或将插件放入 `data/plugins/astrbot_phone_control_mirror/`。
2. 打开「ADB 连接」，填写可达的主机与端口，保存后重载插件。容器中的 `127.0.0.1` 指容器自身。
3. 发送 `/phone status` 检查设备，再尝试 `/phone browse bilibili 3` 或 `/phone task 打开小黑盒并查看最新帖子`。
4. 按需开启图片保存、记忆、互动和组网；先核对「权限与安全」中的主人、白名单及互动开关。

ADB 地址与端口留空时，插件可安装和加载，面板与配置可用；手机能力保持未启用，不启动设备后台任务。

`/phone status` 与 `/phone help` 默认输出图片，分别由 `command.status_as_image`、`command.help_as_image` 控制。关闭后输出文字；图片渲染失败会提示并附上文字。

## 功能

| 能力 | 说明 |
| --- | --- |
| 手机控制 | 38 个 `phone_*` 工具，支持状态、UI 树、截图、点击、滑动、输入、按键与后台清理；按场景选择 ADB、scrcpy、minitouch |
| 内容理解 | 图文任务关联原图与正文，视频/直播结合播放音频、关键帧、STT 与视觉 Provider；看评论、回复、分享分别判断 |
| 自主浏览 | 定时、随机和持续任务，按兴趣选择内容；用户任务优先，过程静默，结束简短汇报 |
| 互动与配图 | 点赞、收藏、评论、被动回复、发帖分别授权并记账；按语义选用服务器或手机保存的图片 |
| 记忆与经验 | 浏览日志、学习档案、App 使用经验、本地语义召回，可选同步 MemoryCompanion / LivingMemory |
| 监控与管理 | Dashboard 查看运行指标、记录、图片、记忆索引与相似关系图谱，支持删除和筛选 |
| 组网与抓包 | WireGuard、Tailscale、ZeroTier 接入；流量可通过 ADB reverse 或组网 IP 进入 mitmproxy |
| 授权与用量 | 设备统一执行门禁、验证码人工接管、Token 额度及 AstrBot `provider_stats` 用量对账 |

### 自主浏览与规则弹性

支持定时浏览、随机玩手机和持续刷手机。AI 按自身兴趣选择内容，观看、分享与互动分别判断；用户手机任务优先，后台过程保持静默。

`flexibility.enabled` 默认关闭，在「自主规则弹性」中单独设置。可分别开启自由浏览、兴趣与新鲜感弹性、单任务预算延展以及互动冷却弹性。自由浏览开启后，可以直接打开贴吧、小黑盒、抖音、快手等 App 看屏幕、滑动或分段观看直播，精读流程与学习记录按需使用；也可按心情继续低兴趣、已看腻的内容，单次任务 Token 预算最多延展 20%，曝光与用量正常记账。

弹性模式的评论、被动回复和发帖按本组每日次数执行（-1 不限、0 禁止）；互动授权、总额度、分享时段与冷却、金融应用限制，以及用户优先、取消、暂停和执行期限按当前配置生效。

### 应用类型与启动限制

模型根据包名、名称和安装版本识别应用主要用途，`phone_list_apps` 展示类型及是否允许打开。

- 银行、支付钱包、证券、基金、保险、贷款、理财、加密资产交易等金融类应用拒绝打开。
- 类型未知、置信度不足或模型识别失败时拒绝启动。
- 命令、工具和信息流浏览共用启动校验；设备、版本变化后重新识别。
- 识别使用「自主任务模型」指定的 Provider，留空使用当前会话模型；仅发送应用元数据，识别计入模型用量。

内置抖音、哔哩哔哩、小黑盒的浏览画像；其他获准启动的应用使用通用文本抽取与屏幕识别。

### 互动、保存图片与分享

互动默认关闭。点赞/收藏共用单轮上限，评论、被动回复、发帖分别设置每日限额与冷却；先申请 `phone_interaction_gate`，完成后用 `phone_interaction_record` 记录真实结果。实际点击与输入由 Agent 根据界面执行。

开启 `image.auto_save_images` 后，可保存喜欢的原图和表情包到服务器、手机或两地，记录描述、标签、来源与哈希。发帖/回复按上下文决定是否配图：

1. `phone_saved_images(action="search", storage="all")` 查询并阅读候选描述，也可按 `server`、`phone` 筛选。
2. 获得对应互动预约后，用 `prepare` 提交图片编号、互动类型和语义理由。
3. 在 App 中选择已核验的相册图片，检查预览与正文，再完成发布并记账。无相关图片时使用文字。

准备图片不执行发布、不计作互动成功。分享须带内容链接或截图；看与分享分别判断，同一目标会话支持分享冷却。

### 记忆、经验与图谱

`memory.local_enabled` 控制本地学习、浏览与聊天记忆保存。App 经验保存在 `plugin_data/astrbot_phone_control_mirror/app_memory.json`，打开应用或读取屏幕后按当前 App/页面召回；操作失败会标记经验可能失效，重新探索后可覆盖更新。

App 经验使用独立索引 `semantic_app/`，聊天学习与浏览使用 `semantic/`。向量模型留空时使用 AstrBot 默认 Embedding Provider。聊天语义召回只读本地索引，不连接设备；查看索引与图谱不调用向量模型。

MemoryCompanion、LivingMemory 使用独立同步开关。LivingMemory 写入复用其访问规则、身份别名和作用域：`user` 随同一用户跨会话共享，`session` 与隔离会话按来源保存，`global` 按全局规则处理。用户任务按发起人归属，自主学习与用户记忆分别管理；同步失败会返回原因。

## 监控与资料管理

在插件详情中打开 Dashboard，按用途进入对应页面。

| 页面 | 用途 |
| --- | --- |
| 监控面板 | 查看后端、截图、流量、互动与模型用量指标 |
| Relay App | 查看手机连接、获取配对码、下载 APK 与重置配对 |
| 浏览与学习 | 内容顶部 Tab 切换浏览日志、学习记录，支持搜索与删除 |
| 记忆索引 | 切换 App/聊天索引，查看模型、正文和实际向量；相似图谱支持筛选、缩放、拖动与节点详情 |
| 组网接入 | 启动/停止节点、查看组网地址与授权状态、打开 Tailscale 登录链接、下载公开抓包 CA |
| 保存的图片 | 查看、搜索、编辑图片语义及删除库记录 |

删除原始日志或学习记录后，关联索引立即失效，日志截图保留。删除「记忆索引」中的单条向量会保留原始档案，删除结果跨重载和后台重建持续生效。图谱连线表示同一向量空间内的余弦相似关系。

## 配置

以 AstrBot 插件配置页及[使用手册](docs/使用手册.md#配置)为准；相关字段按开关条件显示。原生配置保存后重载插件，命令补丁即时应用。

| 分组 | 主要内容 |
| --- | --- |
| ADB 连接 | 主机、端口、序列号、adb 路径与超时 |
| 权限与安全 | 主人/管理员/白名单、设备 Shell、后台保留、互动许可/限额/冷却 |
| 信息流浏览、图片保存 | 默认应用、条数、节奏、截图、日志保护轮次、图片保存位置 |
| 自主浏览、随机/持续刷 | 定时、间隔、概率、偏好应用、规则弹性、单轮预算、分享时段与冷却 |
| 任务与媒体模型、Token 额度 | 自主任务 / STT / 视频 Provider、视频音轨单独识别开关、滚动额度、预算与用量审计；额度默认不限制 |
| 兴趣与记忆 | 内容新鲜感、本地保存、记忆插件同步、学习与经验召回 |
| 提示词与命令输出 | 使用指南、经验注入、状态/帮助图片开关 |
| 屏幕感知、控制后端 | UI、截图、实时屏幕、OCR、scrcpy 与 minitouch |
| 组网 | 一个开关与服务选择器；按所选 WireGuard / Tailscale / ZeroTier 显示参数 |
| 流媒体、流量抓包 | 完整点播开关与大小阈值、音频/视频识别、mitmproxy 路径、传输通道与 CA 信任方式 |
| 工具异常汇报 | 手机工具报错自动通知主人，包含静默自主任务；按目标合并并限频 |
| 验证与人工协助、调试 | 自动尝试与等待时限、通知目标、动作日志 |

## 组网与抓包

### 选择接入方式

| 方式 | 服务端 | 手机端 | 关键配置 |
| --- | --- | --- | --- |
| WireGuard | 插件内置用户态 helper，可作 client/server | 官方 WireGuard 客户端 | Peer/密钥、隧道地址、端口；`/phone wg config` 提供配置片段 |
| Tailscale | 插件管理独立官方用户态节点，无宿主 DNS/路由变更 | 官方 Tailscale，加入同一 Tailnet | Auth Key 或登录链接、手机组网 IPv4、节点 ACL |
| ZeroTier | 插件独立进程运行官方 Sockets SDK，无需 TUN/NET_ADMIN | 官方 ZeroTier，加入同一私有网络 | 16 位 Network ID、控制台成员授权、手机组网 IPv4 |

在「组网」开启开关并选择一种服务，保存后重载。对应参数位于 `network.wireguard`、`network.tailscale`、`network.zerotier`；切换保留各服务的参数与身份，关闭恢复原 ADB 目标。配置加载保留各组网服务的当前参数。Android 同时启用一种 VPN，组网成员与访问端口在控制台授权。

### Android 按应用 VPN 与 App 控制

手机 VPN 只承载选中应用、系统 adbd 无法使用隧道时，可选择「使用 App 控制」，并在 VPN 客户端允许 Phone Relay。App 以自身 UID 主动连接插件，内置 ADB 客户端连接本机 adbd；本机可使用已经开启的 TCP ADB，也可使用 Android 11+ 无线调试的配对和 TLS 连接端口。系统 ADB 授权、VPN 成员授权、插件 App 配对分别完成。

| 插件内置组网 | App 控制需要填写 | 手机连接插件的地址 | 容器网络要求 |
| --- | --- | --- | --- |
| WireGuard | 隧道及 Peer 参数、手机隧道 IPv4 | 插件的 WireGuard IPv4 与 App 接入端口 | 服务端模式发布配置的 WireGuard UDP 监听端口 |
| Tailscale | Auth Key 或登录授权、Tailnet ACL | 插件 Tailscale 节点 IPv4 与 App 接入端口 | 用户态节点可以访问 Tailscale 服务；ACL 允许 App 端口 |
| ZeroTier | Network ID、SDK 动态库、两端成员授权、手机组网 IPv4 | 插件 ZeroTier IPv4 与 App 接入端口 | SDK 节点能访问 ZeroTier 网络，手机地址用于入口来源校验 |

内置组网把虚拟网 App 接入端口连接到插件回环，默认 TCP `6190`。外部系统 VPN 或局域网连接使用主机可达地址，并发布对应 App TCP 端口。「Relay App」页面显示建议入口、实际连接目标、本机 ADB 端点和能力；组网节点在线后仍需确认手机实际回连。

App 控制中的截图、节点、音频、媒体录制、文件及抓包经 App 连接传输，插件端 scrcpy 按直连模式配置启动。App 画质参数位于「ADB 连接」；直连参数和组网 ADB 转发参数按已保存的连接方式显示，切换后保存并重载，隐藏值保留。低带宽下可调整推帧尺寸、频率和 JPEG 质量；画面年龄上限代表允许的真实画面陈旧程度，超过上限拒绝。实时 PCM 音频约需192KB/s，带宽不足时可使用手机录制 AAC 片段后回传。详细配置见 [Phone Relay 使用说明](helper/android-relay/README.md)。

手机端的自动恢复默认关闭，依赖 Android 对开机广播和后台前台服务的准入。系统或模拟器屏蔽开机广播时，可打开 Phone Relay 手动启动；本机无线端口变化可由已启用的端口发现使用既有身份验证。

ZeroTier 默认由 SDK 自动选择传输路径。节点在线但两端 UDP 互访受限时，可在 ZeroTier 参数开启「强制 TCP 中继」（`zerotier_force_tcp_relay`），保存并重载后使用官方 TCP 中继。组网页显示所选策略；成员授权、手机源 IP 校验和 App TLS 认证保持执行，中继延迟和吞吐受网络条件影响。

ZeroTier 节点重建后的路径收敛还取决于官方客户端，节点就绪而手机仍停在 TCP 建链时，可在手机重新连接 ZeroTier 网络，再检查 Relay 页面报告的实际目标。

### Tailscale / ZeroTier 设置

1. 在「组网」开启开关，选择 Tailscale 或 ZeroTier，再填写对应参数。
2. Tailscale 可填写 Auth Key，或留空后在组网页启动并打开登录链接；ZeroTier 填写 Network ID，启动后在控制台授权显示的 10 位节点 ID。
3. 手机官方客户端加入同一网络。直连模式填写手机组网 IPv4；App 模式下 ZeroTier 需填写手机 IPv4 校验来源，Tailscale 由手机主动回连。Tailscale 使用 `100.64.0.0/10` 地址。
4. 按需开启组网 ADB。无 root Android 配合 [Phone ADB Relay](helper/android-relay/README.md)（[APK](bin/android/phone-adb-relay.apk)）将 App UID 的 `15555` 转入本机 adbd；系统级 VPN 对端可直接填写实际 adbd 端口。
5. 停止组网会关闭该开关并恢复原 ADB 目标，私有身份文件保留，重启可复用。

### 运行组件准备

启用对应功能并保存、重载后，缺失的 ADB、Tailscale CLI/daemon 会从官方来源下载到插件数据目录 `runtime/<系统-架构>/`。已有文件与 PATH 程序直接复用，手动路径优先；停用相关功能不会下载。下载过程在后台执行，组网页、概览接口与日志提供准备状态；失败后修正网络/路径并重载即可重试。组件缓存与组网身份均随插件数据保留。

| 组件 | 官方自动下载范围 | 手动方式 |
| --- | --- | --- |
| ADB | Linux x86_64、Windows x86/x86_64、macOS x86_64/arm64 | 填写 `adb.path`；Google 平台包保留配套文件 |
| Tailscale CLI/daemon | Linux x86_64、x86、arm64、armv7、riscv64 | 填写两个程序路径；其他平台安装官方客户端并确认用户态参数兼容 |
| ZeroTier SDK | 手动提供 | 填写 `network.zerotier.zerotier_library`，要求架构和 SDK API 兼容 |

ADB 从 [Google 官方平台包](https://developer.android.com/tools/releases/platform-tools) 读取校验信息；Tailscale 使用 [官方稳定源](https://pkgs.tailscale.com/stable/) 的 SHA256。程序经安全解压、格式/架构检查后原子写入，同一目录并发准备使用文件锁。详细来源及 ZeroTier 构建见 [用户态组网组件](helper/mesh/README.md)。

### 抓包通道与证书

| 配置 | 行为 |
| --- | --- |
| `traffic.transport=adb` | 手机经回环代理与对应 `adb reverse` 访问服务端 |
| `traffic.transport=mesh` | 手机代理指向服务端组网 IPv4 与 对应服务的 `proxy_port`，经 Tailscale/ZeroTier 进入本地 mitmproxy |
| `traffic.ca_mode=system` | 通过 `adb root` 注入系统 CA |
| `traffic.ca_mode=manual` | 从组网页下载公开 CA，由手机或 App 配置信任；无 root 可捕获 HTTP，HTTPS 需 App 信任此 CA |

抓包适用于遵循系统 HTTP 代理的应用请求。mitmproxy 只监听本地回环。ZeroTier 抓包入口仅接受配置的手机 IP；Tailscale 访问由 Tailnet ACL 控制。组网中断会撤回手机代理；停止抓包恢复原代理，保留其他 reverse 和使用者后续改动。

部分 App 使用证书固定或额外加密，无法由普通 CA 信任实现解密，此时使用屏幕/OCR 识别。流量日志做敏感参数打码、正文截断和轮转。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/phone help`、`/phone status` | 帮助与设备状态，可切换图片/文字 |
| `/phone browse [app] [条数]` | 浏览信息流并记录理解 |
| `/phone task <instruction>`、`/phone tasks` | 后台多步任务与列表 |
| `/phone play [tokens]` | 开始一轮静默玩手机 |
| `/phone continuous on\|off\|status` | 持续浏览开关与状态 |
| `/phone traffic [status\|tail\|on\|off]` | 抓包状态、近期流量与开关 |
| `/phone network [status\|start\|login\|stop]` | Tailscale/ZeroTier 状态与接入控制 |
| `/phone wg [status\|start\|stop\|restart\|config]` | WireGuard 控制与配置输出 |
| `/phone verify [status\|resume\|cancel <id>]` | 验证与人工协助 |
| `/phone quota [audit [小时]]` | 额度与实际用量对账 |
| `/phone novelty [status\|reset]` | 新鲜感状态 |
| `/phone stop [job_id\|all]` | 停手机任务、抓包和自主循环，回桌面清理后台 |

完整命令、工具与行为手册见[使用手册](docs/使用手册.md#llm-工具)。

## 停止与数据

### 停止与保活

`/phone stop` 关闭手机任务、抓包、自主调度、随机/持续刷、实时屏幕和控制后端，返回桌面并清理后台。默认保留官方 WireGuard、Tailscale、ZeroTier、Phone ADB Relay，以及桌面和当前输入法。

组网身份和运行开关通过组网页或 `/phone network stop` 管理。抓包、组网及解码进程使用心跳监护，手机端控制服务使用设备看门狗；失联后回收，重载时检查前任租约。

截图、日志、学习及图片保存在 `data/plugin_data/astrbot_phone_control_mirror/`。网络私钥与身份在私有数据目录保留，文件按私密权限保存；CA 下载只返回公开证书。

## 安全与常见问题

- **权限**：默认仅管理员、白名单或定时任务可操作；`owner_ids` 非空时只认指定主人。
- **设备 Shell**：默认关闭，启用后受设备门禁、超时与输出上限约束。
- **验证码与身份操作**：短信/邮箱验证码、人脸及实名认证交使用者处理；自动验证尝试有次数与时限，失败暂停并通知。
- **未启用/连接失败**：检查 ADB 主机、端口、网络可达性与调试授权；容器回环不是模拟器宿主。
- **中文输入无效**：安装并启用 ADBKeyboard，或按工具反馈切换输入方式。
- **组网等待授权**：在对应控制台批准节点，核对两端网络与手机 IP；不要同时开启多个 Android VPN。
- **抓包为空**：核对传输通道、节点连通、代理端口与 CA 信任；系统 CA 需要 root，手动 CA 需要目标 App 接受证书。
- **工具报错汇报**：默认开启；`report.target` 可指定通知会话，留空按实际平台私聊 `security.owner_ids` 中的主人；后续错误默认每 300 秒合并发送，失败有界重试。
- **媒体接口报错**：核对视觉模型真实音频能力和 Provider 模态配置；失败清理后的内容任务需重新 `begin`。
- **用量不一致**：插件统计手机相关运行，AstrBot 面板统计全部请求；以运行结束后 `provider_stats` 对账为准。

## 开发与许可

```bash
pip install -r requirements.txt
python3 -m pytest tests -q
```

纯逻辑测试可在本地运行；依赖真实 AstrBot 的用例在框架环境执行。可选抓包依赖与平台组件安装见使用手册。

| 路径 | 内容 |
| --- | --- |
| `main.py`、`core/plugin/` | 入口、生命周期、命令、任务、监控与同步 |
| `core/service.py`、`core/tools/` | 能力门面与 38 个工具 |
| `core/app_policy.py` | 应用类型模型分类及启动限制 |
| `core/mesh*.py`、`core/wireguard.py` | 用户态组网、TCP 转发与节点生命周期 |
| `views/dashboard/` | 监控与资料管理页面 |
| `helper/`、`bin/` | 组件源码、构建说明、随包二进制与许可 |
| `skills/`、`docs/使用手册.md` | Agent 行为手册与完整使用说明 |

插件本体采用 [MIT License](LICENSE)，Copyright © 2026 ACAT。随包及运行时第三方组件按各自许可证使用，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、[Tailscale 许可](bin/linux-x86_64/TAILSCALE-LICENSE.txt)、[ZeroTier SDK 许可](bin/linux-x86_64/ZEROTIER-LICENSE.txt)。本项目与相关平台、应用或组件维护者无隶属或背书关系。

## 使用风险

使用本插件的自动化操作、互动发布、组网与抓包功能，使用者须自行承担全部风险，包括账号限流、封禁、设备级封锁及其他损失；请自行确认平台规则与操作授权。

### 自主活动的每日时间段

随机玩使用 `auto.random_windows`，持续自动刷使用 `auto.continuous_windows`。在列表中逐项填写 `08:00-10:00`、`19:00-22:00`，每天可以设置多段，也支持 `23:00-02:00` 跨午夜。空列表表示全天。按 `auto.play_timezone`（默认 `Asia/Shanghai`）判定，包含开始、不包含结束；开始和结束不能相同。允许时段外等待，到结束时间中止本类自主任务并释放设备。保存后即时采用新时段，用户直接发起的手机任务独立执行。

### 自主活动模式

在自主行为配置中用 `auto.mode` 下拉选择「随机玩」「自动刷」「定时玩」，对应子项随选择显示。`auto.activity_enabled` 控制是否运行当前模式。随机玩与自动刷分别保存各自时间段，切换后仍保留设置；定时玩使用 cron、App 与汇报会话。保存模式后取消其他自主模式的在途任务，定时排程随模式同步。配置加载保留当前模式及活动开关。`/phone continuous on` 选择自动刷，`/phone auto on` 选择定时玩，`/phone stop` 关闭自主活动。

App 模式支持手机播放音频及连续片段采集，数据通过绑定控制会话的二进制子连接传送。双向文件传输按64 KiB分块，单文件上限128 MiB，大小和SHA256核对成功后提交目标文件。

手机端录制使用 `phone_record_media` 的 start/status/stop/cancel，支持声音、画面或音视频，最大900秒/128 MiB。手机以H264/AAC封装MP4/M4A，stop下载并校验成品。`phone_files` 支持当前聊天附件上传、手机下载、缓存列表和清理；文件可按需发回当前聊天。

App抓包的系统CA模式需要已授权root；手动CA模式下代理可启动，HTTPS解密仍取决于目标App信任。Relay App支持导出自身公开CA并打开系统证书设置，Dashboard可下载当前抓包方式的CA。

保持手机视频就绪时，手机通过独立认证连接推送JPEG，插件只在内存保留最新一帧；截图按250毫秒画面年龄和操作代次校验。画面年龄采用请求往返时钟的保守下界计算，网络排队不会把旧帧视为新帧；截图耗时与画面年龄在面板分别显示。关闭保持就绪后按需采集，90秒空闲释放手机视频资源。
