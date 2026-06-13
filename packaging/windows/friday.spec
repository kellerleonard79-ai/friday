# PyInstaller spec for the Windows build of Friday.
# Build from the repo root on a Windows machine (or CI):
#   pyinstaller packaging/windows/friday.spec --noconfirm
#
# Produces dist/Friday/Friday.exe — a windowed (no console) onedir app.
#   Friday.exe          → tray + supervisor (+ first-run setup wizard)
#   Friday.exe --core   → the agent itself (spawned by the tray)

import os

SPEC_DIR = os.path.abspath(SPECPATH)
REPO_ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))
SRC_DIR = os.path.join(REPO_ROOT, "friday")

datas = [
    (os.path.join(SRC_DIR, "AGENTS.md"), "."),
    (os.path.join(SRC_DIR, "quips.yaml"), "."),
    (os.path.join(SRC_DIR, "dashboard", "static"), os.path.join("dashboard", "static")),
]

# Ship the Google OAuth client with the installer when the builder provides
# it (see BUILD_WINDOWS.md). Without it, the wizard asks for the file.
_client_secret = os.path.join(SPEC_DIR, "google_client_secret.json")
if os.path.exists(_client_secret):
    datas.append((_client_secret, "."))

_icon = os.path.join(SPEC_DIR, "friday.ico")

a = Analysis(
    [os.path.join(SRC_DIR, "tray.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "pystray._win32",
        "googleapiclient.discovery",
        "google_auth_oauthlib.flow",
        "tzdata",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # macOS-only / out-of-scope pieces
        "rumps", "AppKit", "Foundation", "objc", "PyObjCTools",
        "menubar", "menubar_icon",
        "pyaudio", "whisper", "openwakeword",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Friday",
    icon=_icon if os.path.exists(_icon) else None,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="Friday",
)
