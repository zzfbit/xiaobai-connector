$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "py -3.11" }
Set-Location $ProjectDir
Invoke-Expression "$PythonBin -m pip install -e .[dev]"
Invoke-Expression "$PythonBin -m PyInstaller --noconfirm --clean packaging/xiaobai-connector.spec"
Write-Host "已生成 dist/Xiaobai Connector.exe"
