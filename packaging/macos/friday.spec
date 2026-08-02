# PyInstaller spec for the macOS build of Friday.
# Build from the repo root on a Mac:
#   pyinstaller packaging/macos/friday.spec --noconfirm
# or, preferably, packaging/macos/build.sh, which also makes the icon and dmg.
#
# Produces dist/Friday.app — a windowed (LSUIElement) menu bar app.
#   Friday.app            → menu bar + supervisor (+ first-run setup wizard)
#   Friday.app --core     → the agent itself (spawned by the supervisor)
#   Friday.app --setup    → the setup wizard alone (re-run from the menu)

import importlib.util
import os

SPEC_DIR = os.path.abspath(SPECPATH)
REPO_ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))

# Same VERSION build.sh names the .dmg with, so the bundle does not report a
# different version than the file it shipped in.
VERSION = os.environ.get("VERSION", "1.0.0")
SRC_DIR = os.path.join(REPO_ROOT, "friday")

datas = [
    (os.path.join(SRC_DIR, "AGENTS.md"), "."),
    (os.path.join(SRC_DIR, "quips.yaml"), "."),
    (os.path.join(SRC_DIR, "Soul.md"), "."),
    (os.path.join(SRC_DIR, "dashboard", "static"), os.path.join("dashboard", "static")),
]

# Optional: ship the Google OAuth client so the google calendar backend works
# without the user creating their own. Absent on an Apple-backend-only build,
# which is the macOS default — see BUILD_MACOS.md.
_client_secret = os.path.join(SPEC_DIR, "google_client_secret.json")
if os.path.exists(_client_secret):
    datas.append((_client_secret, "."))

# The voice satellite's TCC wrapper. Shipped inside Resources so the installed
# app can register it; macos_setup.voice_app_path() looks for it there.
_voice_app = os.path.join(REPO_ROOT, "FridayVoice.app")
if os.path.isdir(_voice_app):
    for _root, _dirs, _files in os.walk(_voice_app):
        _rel = os.path.relpath(_root, REPO_ROOT)
        for _f in _files:
            datas.append((os.path.join(_root, _f), _rel))

_icon = os.path.join(SPEC_DIR, "friday.icns")

hiddenimports = [
    "rumps",
    # Imported lazily inside apple_calendar so a missing framework degrades to
    # the JXA reader rather than crashing — which also means PyInstaller's
    # static analysis never sees it.
    "EventKit",
    # Same story in connectors/location.py — lazy import, degrades to IP
    # geolocation, invisible to static analysis.
    "CoreLocation",
    "menubar",
    "menubar_icon",
    "macos_setup",
    "setup_wizard",
    "friday",
    "tzdata",
]

# The Google Calendar backend is optional on macOS — the default here is
# Apple, and requirements.txt does not pull the Google client libraries in.
# Listing them unconditionally makes PyInstaller print two ERROR lines on an
# otherwise clean build, so only claim them when they are actually installed.
for _mod in ("googleapiclient.discovery", "google_auth_oauthlib.flow"):
    if importlib.util.find_spec(_mod.split(".")[0]) is not None:
        hiddenimports.append(_mod)

a = Analysis(
    [os.path.join(SRC_DIR, "mac_app.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Windows-only pieces
        "pystray", "velopack", "tray",
        # The voice satellite runs from its own bundle and venv — never
        # imported by the core, so its heavy deps must not be pulled in here.
        "pyaudio", "whisper", "openwakeword", "torch",
        # Transitive dependencies of the above that PyInstaller finds sitting
        # in site-packages and bundles anyway. llvmlite alone is 111 MB and
        # nothing in the core imports any of these — verified by building
        # without them and exercising every entry point.
        "numba", "llvmlite", "scipy", "pandas", "matplotlib",
        "sklearn", "IPython", "notebook", "pytest",
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
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,  # matches the building machine; see BUILD_MACOS.md
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="Friday",
)

app = BUNDLE(
    coll,
    name="Friday.app",
    icon=_icon if os.path.exists(_icon) else None,
    bundle_identifier="com.friday.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "Friday",
        "CFBundleDisplayName": "F.R.I.D.A.Y.",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        # NOTE: deliberately NOT LSUIElement. Friday is a menu bar app, but
        # declaring it here makes the process an accessory from launch, and Tk
        # never orders a window on screen in an accessory process — the setup
        # wizard would run completely invisibly. mac_app.py sets the accessory
        # activation policy at runtime instead, in the menu bar process only,
        # so the wizard still gets a real window and the bar still gets no
        # Dock tile. See _become_accessory() there.
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # friday.py itself may open an input stream to probe the mic; without
        # this key macOS kills the process instead of showing a prompt.
        "NSMicrophoneUsageDescription":
            "Friday captures audio for voice commands and transcription.",
        "NSAppleEventsUsageDescription":
            "Friday reads and writes events in your Apple Calendar.",
        "NSCalendarsUsageDescription":
            "Friday reads and writes events in your Apple Calendar.",
        # macOS 14 split calendar access in two. Without the full-access key
        # the EventKit request silently resolves to write-only, which reads as
        # "granted" but returns zero events — so briefings fall back to the
        # JXA reader and take minutes instead of milliseconds. Both keys are
        # required: the plain one still covers macOS 13 and earlier.
        "NSCalendarsFullAccessUsageDescription":
            "Friday reads your calendar to assemble briefings and reminders.",
        # Without this key CoreLocation resolves to denied and never prompts,
        # so location questions silently fall back to IP geolocation.
        "NSLocationWhenInUseUsageDescription":
            "Friday tells you where this Mac is when you ask.",
    },
)
