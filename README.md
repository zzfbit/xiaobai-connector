# Xiaobai Connector

小白 Connector 是一个独立的桌面程序：安装到一台 Mac 或 Windows 电脑后，自动寻找本机的 Codex、Claude Code、Hermes，用户勾选要开放给手机的 Agent，再用手机里的「设置 → 添加电脑」完成一次性配对。

## 下载

从 [GitHub Releases](https://github.com/zzfbit/xiaobai-connector/releases/latest) 下载最新版本：macOS 提供 DMG 和 App 压缩包，Windows 提供 EXE 和 ZIP。Release 由 GitHub Actions 在版本 tag 上自动构建。

## 使用流程

1. 下载并打开 `Xiaobai Connector`，自动「扫描本机 Agent」，勾选要连接的 Agent。
2. 程序显示 `pairing ID` 和 6 位配对码，在 App 的「设置 → 添加电脑」输入这两项。
3. 手机确认后，桌面程序自动交换长期凭据并保持连接；Agent 会出现在手机名录里。

