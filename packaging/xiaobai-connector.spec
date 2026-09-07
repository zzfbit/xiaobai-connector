# PyInstaller spec for the standalone desktop wizard.
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project = Path(SPECPATH).parent
with (project / "pyproject.toml").open("rb") as handle:
    project_version = str(tomllib.load(handle)["project"]["version"])
hiddenimports = collect_submodules("websockets") + ["keyring.backends.Windows", "keyring.backends.macOS"]
asset_dir = project / "src" / "xiaobai_connector" / "assets"

a = Analysis(
    [str(project / "src" / "xiaobai_connector" / "ui" / "app.py")],
    pathex=[str(project / "src")],
    binaries=[],
    datas=[
        (str(asset_dir), "xiaobai_connector/assets"),
        # Claude Code starts this file as a separate stdio MCP child in source
        # builds. Frozen builds use the --message-agent-mcp entry point, but
        # keeping the module as data also supports one-folder/debug builds.
        (str(project / "src" / "xiaobai_connector" / "message_agent_mcp.py"),
         "xiaobai_connector"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
if sys.platform == "darwin":
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
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="Xiaobai Connector",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
        console=False,
    )
