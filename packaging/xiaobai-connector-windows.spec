# PyInstaller spec for the Windows desktop wizard.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project = Path(SPECPATH).parent
package_name = "xiaobai_connector_windows"
hiddenimports = collect_submodules("websockets") + ["keyring.backends.Windows"]
asset_dir = project / "src" / package_name / "assets"

a = Analysis(
    [str(project / "src" / package_name / "ui" / "app.py")],
    pathex=[str(project / "src")],
    binaries=[],
    datas=[
        (str(asset_dir), package_name + "/assets"),
        # Claude Code starts this file as a separate stdio MCP child in source
        # builds. Frozen builds use the --message-agent-mcp entry point, but
        # keeping the module as data also supports one-folder/debug builds.
        (str(project / "src" / package_name / "message_agent_mcp.py"),
         package_name),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["keyring.backends.macOS", "keyring.backends.macOS.api"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="Xiaobai Connector",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False,
)
