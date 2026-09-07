$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectDir
$PythonBin = $env:PYTHON_BIN
if (-not $PythonBin) {
    $PythonBin = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonBin)) {
    $VenvDir = Join-Path $ProjectDir ".venv"
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3.11 -m venv $VenvDir 2>$null
    }
    if (-not (Test-Path $PythonBin)) {
        $PythonLauncher = Get-Command python -ErrorAction SilentlyContinue
        if (-not $PythonLauncher) {
            throw "找不到可用的 Python 3.11；请安装 Python 3.11 或设置 PYTHON_BIN。"
        }
        & $PythonLauncher.Source -m venv $VenvDir
    }
}
if (-not (Test-Path $PythonBin)) {
    throw "虚拟环境创建失败，请检查 Python 安装或设置 PYTHON_BIN。"
}
& $PythonBin -m pip install -e ".[dev]"
& $PythonBin -m PyInstaller --noconfirm --clean packaging/xiaobai-connector-windows.spec
if ($LASTEXITCODE -ne 0) {
    throw "Windows EXE 构建失败，退出码：$LASTEXITCODE。请确认 dist/Xiaobai Connector.exe 未被运行中的程序占用。"
}
Write-Host "已生成 dist/Xiaobai Connector.exe"
