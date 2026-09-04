# Xiaobai Connector

小白 Connector 是一个独立的桌面程序：安装到另一台 Mac 或 Windows 电脑后，自动寻找本机的 Codex、Claude Code、Hermes，用户勾选要开放给手机的 Agent，再用手机里的「设置 → 添加电脑」完成一次性配对。

## 使用流程

1. 下载并打开 `Xiaobai Connector`。
2. 点击「扫描本机 Agent」，勾选要连接的 Agent。
3. 选择本机工作目录和安全策略。
4. 程序显示 `pairing ID` 和 6 位配对码。
5. 在小白 App 的「设置 → 添加电脑」输入这两项。
6. 手机确认后，桌面程序自动交换长期凭据并保持连接；Agent 会出现在手机名录里。

长期 Connector token 只保存在 macOS Keychain 或 Windows Credential Manager，不写入配置文件、日志或 Git 仓库。服务器只接收 Agent 的声明和必要的消息内容；本机 Agent 的账号、密钥和完整环境不会上传。

## 开发运行

需要 Python 3.11 或更新版本。Tkinter 是桌面界面依赖，Windows 的官方 Python 安装包和 macOS 的 Python.org 安装包通常自带它。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m xiaobai_connector
```

也可以直接运行：

```bash
.venv/bin/xiaobai-connector
```

开发环境中可用 `XIAOBAI_CONNECTOR_SERVER_URL` 覆盖默认服务器地址。配对服务使用同一地址的 HTTP 入口（例如 `wss://api.xiaobaizzf.com/agent/connect` 对应 `https://api.xiaobaizzf.com`）。

## 构建安装包

构建脚本在 `packaging/`：

```bash
./packaging/build-macos.sh
# Windows PowerShell:
./packaging/build-windows.ps1
```

仓库提供 macOS 和 Windows 的构建脚本；Windows 需要在 Windows 电脑或 CI runner 上运行，PyInstaller 不能从 macOS 直接生成可用的 Windows 安装包。签名、公证和 Windows 代码签名需要在发布时补充各平台的证书，不把证书或 token 放进仓库。

## 安全边界

- Connector 只主动向服务器建立出站 WSS，不要求在单位电脑开放入站端口。
- pairing ID + 6 位码只能使用一次，默认 5 分钟过期。
- 服务器批准配对后，Connector 以一次性 proof 换取设备 token；手机永远看不到这个长期 token。
- 默认沙箱是 `workspace-write`。只有用户主动选择时才允许 `danger-full-access`。
- 自动发现只检查已知命令名和常见安装目录，不扫描或上传任意文件内容。

这是本项目的自定义 Connector，不等同于 Codex 官方 Remote Connections 功能；手机端 Agent 名录和家庭群路由仍由本项目自己的 Agent Gateway 管理。
