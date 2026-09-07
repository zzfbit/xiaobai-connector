# Connector

安装到 Mac 或 Windows 后，自动寻找本机的Agent，包括 Codex、Claude Code、Hermes等，勾选要连接到手机的 Agent，再用配对码完成和手机的配对。

## 下载

从 [GitHub Releases](https://github.com/zzfbit/xiaobai-connector/releases/latest) 下载最新版本。

## 使用流程

1. 下载并打开 `Xiaobai Connector`，自动「扫描本机 Agent」，勾选要连接的 Agent。
2. 程序显示 `pairing ID` 和 6 位配对码，在 App 的「设置 → 添加电脑」输入这两项。
3. 电脑端的Agent 会出现在手机列表里。

长期 Connector token 只保存在 macOS Keychain 或 Windows Credential Manager，不写入配置文件、日志或 Git 仓库。服务器只接收 Agent 的声明和必要的消息内容；本机 Agent 的账号、密钥和完整环境不会上传。

## 开发运行

需要 Python 3.11 或更新版本。Tkinter 是桌面界面依赖，Windows 的官方 Python 安装包和 macOS 的 Python.org 安装包通常自带它。

```bash
# macOS
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m xiaobai_connector
```

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m xiaobai_connector_windows
```

也可以直接运行：

```bash
# macOS
.venv/bin/xiaobai-connector-macos
# Windows PowerShell
.venv\Scripts\xiaobai-connector-windows.exe
```

代码按平台分开维护：`src/xiaobai_connector` 是 macOS 端，
`src/xiaobai_connector_windows` 是 Windows 端独立副本。Windows 构建只分析
`xiaobai_connector_windows`，macOS 构建只分析 `xiaobai_connector`；修改一端时不应
改动另一端的专属代码。

Windows 端保留了 macOS 端的 Codex 桌面桥接：检测到 Windows ChatGPT/Codex 桌面端的
`codex-app-tools` 命名管道时，手机消息先复用桌面端现有会话 writer，并保留
`send_message_to_thread` 的可见插话行为；桌面端未运行或桥接不可用时，自动回退到
`thread/queue/add` 持久队列。桥接只读取桌面端 app-server 子进程的本地启动参数，不会
把 macOS 路径或 macOS Keychain 代码带进 Windows 包。

Windows 端会优先使用 ChatGPT/Codex 桌面端随附的 `codex.exe`，而不是 PATH 中可能过期的
`codex.cmd`。历史通过本机 app-server 和 `%USERPROFILE%\.codex` 只读读取；JSON-RPC 固定按
UTF-8 解码，避免中文 Windows 的 GBK 系统编码导致会话历史为空。

开发环境中可用 `XIAOBAI_CONNECTOR_SERVER_URL` 覆盖默认服务器地址。配对服务使用同一地址的 HTTP 入口（例如 `wss://api.xiaobaizzf.com/agent/connect` 对应 `https://api.xiaobaizzf.com`）。

## 从 voice 同步并更新安装包

独立版和 /Users/a123456/voice/integrations/connector 的运行时目录结构不同，不能直接把整个目录覆盖过去。仓库提供一键同步检查、回归测试和 macOS 打包命令：

~~~bash
./packaging/sync-from-voice.sh
~~~

默认读取相邻的 ../voice，也可以指定来源：

~~~bash
VOICE_REPO=/path/to/voice ./packaging/sync-from-voice.sh
~~~

脚本会阻止尚未移植的运行时代码变更，避免生成表面成功但不可运行的安装包；这类变更需要先把独立版适配层更新完，再在同一个改动中更新 packaging/voice-sync.lock。本地开发有未提交改动时可加 --allow-dirty，正式同步不建议使用。

## 构建安装包

构建脚本在 `packaging/`：

```bash
./packaging/build-macos.sh
# Windows PowerShell:
./packaging/build-windows.ps1
```

两个构建脚本使用各自的 PyInstaller spec：macOS 使用
`packaging/xiaobai-connector-macos.spec`，Windows 使用
`packaging/xiaobai-connector-windows.spec`。Windows 需要在 Windows 电脑或 CI runner
上运行，PyInstaller 不能从 macOS 直接生成可用的 Windows 安装包。签名、公证和
Windows 代码签名需要在发布时补充各平台的证书，不把证书或 token 放进仓库。

## 安全边界

- Connector 只主动向服务器建立出站 WSS，不要求在单位电脑开放入站端口。
- pairing ID + 6 位码只能使用一次，默认 5 分钟过期。
- 服务器批准配对后，Connector 以一次性 proof 换取设备 token；手机永远看不到这个长期 token。
- 默认沙箱是 `workspace-write`。只有用户主动选择时才允许 `danger-full-access`。
- 自动发现只检查已知命令名和常见安装目录，不扫描或上传任意文件内容。

这是本项目的自定义 Connector，不等同于 Codex 官方 Remote Connections 功能；手机端 Agent 名录和家庭群路由仍由本项目自己的 Agent Gateway 管理。
