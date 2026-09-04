# PyInstaller spec for the standalone desktop wizard.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project = Path(SPECPATH).parent
hiddenimports = collect_submodules("websockets") + ["keyring.backends.Windows", "keyring.backends.macOS"]

a = Analysis(
    [str(project / "src" / "xiaobai_connector" / "ui" / "app.py")],
    pathex=[str(project / "src")],
    binaries=[],
    datas=[],
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
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="Xiaobai Connector",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
        console=False,
    )
