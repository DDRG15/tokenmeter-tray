# PyInstaller recipe for the desktop .exe.
#
#   python -m PyInstaller tokenmeter.spec --noconfirm
#
# Produces `dist/TokenMeter.exe`, a single file that does not need Python.

from pathlib import Path

PROJECT = Path(SPECPATH)

a = Analysis(
    ["launcher.py"],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=[
        ("tokenmeter/assets/tokenmeter.ico", "tokenmeter/assets"),
        # The web interface is read from disk at runtime.
        ("tokenmeter/ui/web/index.html", "tokenmeter/ui/web"),
    ],
    # pystray and PIL load their backends by name at runtime, so static
    # analysis does not see them and they have to be declared.
    hiddenimports=[
        "pystray._win32",
        "PIL._tkinter_finder",
        # The window process (`--window`) imports pywebview lazily.
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ],
    hookspath=[],
    runtime_hooks=[],
    # aiohttp drags in a test client that is not used here.
    excludes=["pytest", "matplotlib", "numpy", "aiohttp.pytest_plugin"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TokenMeter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # No console: this is a tray app, not a terminal tool.
    console=False,
    icon=str(PROJECT / "tokenmeter" / "assets" / "tokenmeter.ico"),
)
