#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x "$project_dir/.venv/bin/python" ]]; then
  python_bin="$project_dir/.venv/bin/python"
else
  bootstrap_python="$(command -v python3.11 || command -v python3)"
  "$bootstrap_python" -m venv "$project_dir/.venv"
  python_bin="$project_dir/.venv/bin/python"
fi
cd "$project_dir"
"$python_bin" -m pip install -e '.[dev]'
"$python_bin" -m PyInstaller --noconfirm --clean packaging/xiaobai-connector-macos.spec
if command -v hdiutil >/dev/null 2>&1; then
  hdiutil create -volname "Xiaobai Connector" -srcfolder "dist/Xiaobai Connector.app" \
    -ov -format UDZO "dist/Xiaobai-Connector-macos.dmg" >/dev/null
fi
echo "已生成 dist/Xiaobai Connector.app"
