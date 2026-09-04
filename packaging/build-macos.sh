#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3.11}"
cd "$project_dir"
"$python_bin" -m pip install -e '.[dev]'
"$python_bin" -m PyInstaller --noconfirm --clean packaging/xiaobai-connector.spec
if command -v hdiutil >/dev/null 2>&1; then
  hdiutil create -volname "Xiaobai Connector" -srcfolder "dist/Xiaobai Connector.app" \
    -ov -format UDZO "dist/Xiaobai-Connector-macos.dmg" >/dev/null
fi
echo "已生成 dist/Xiaobai Connector.app"
