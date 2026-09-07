# PyInstaller spec for the macOS desktop wizard.
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project = Path(SPECPATH).parent
with (project / "pyproject.toml").open("rb") as handle:
    project_version = str(tomllib.load(handle)["project"]["version"])
package_name = "xiaobai_connector"
hiddenimports = collect_submodules("websockets") + ["keyring.backends.macOS"]
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
    excludes=["keyring.backends.Windows"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], [],
    name="Xiaobai Connector",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, exclude_binaries=True,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=True,
    name="Xiaobai Connector",
)
app = BUNDLE(
    coll,
    name="Xiaobai Connector.app",
    icon=None,
    bundle_identifier="com.xiaobaizzf.connector",
    version=project_version,
)
