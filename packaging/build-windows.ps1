$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectDir
$PythonBin = $env:PYTHON_BIN
if (-not $PythonBin) {
    $PythonBin = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonBin)) {
    & py -3.11 -m venv (Join-Path $ProjectDir ".venv")
}
& $PythonBin -m pip install -e ".[dev]"
& $PythonBin -m PyInstaller --noconfirm --clean packaging/xiaobai-connector.spec
Write-Host "已生成 dist/Xiaobai Connector.exe"
