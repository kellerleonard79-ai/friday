"""
macos_setup.py
Generates Friday's macOS LaunchAgents and the voice launcher config.

WHY THIS EXISTS
The repo used to ship LaunchAgent plists and a C launcher with absolute paths
baked in for one developer's home directory (/Users/keller/..., Python 3.14).
Nothing in that set could launch on any other machine. The plists are now
templates with @PLACEHOLDER@ variables, rendered at setup time against paths
resolved on the machine actually running Friday.

The templates live here as string constants rather than as files under
packaging/ so that a frozen .app and a source checkout resolve them the same
way — there is no bundle path to plumb through and nothing to drift.

Everything here is a no-op off macOS.
"""

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import paths

logger = logging.getLogger("friday.macos_setup")

PKG_DIR = Path(__file__).resolve().parent
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

CORE_LABEL = "com.friday.core"
MENUBAR_LABEL = "com.friday.menubar"
VOICE_LABEL = "com.friday.voice"

# Kept off the PATH template: whatever interpreter we resolve gets its own bin
# directory prepended, so a venv's console scripts are reachable.
_BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>@LABEL@</string>

    <key>ProgramArguments</key>
    <array>
@PROGRAM_ARGS@
    </array>

    <key>WorkingDirectory</key>
    <string>@APP_DIR@</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>@LOG_DIR@/@LABEL@.log</string>

    <key>StandardErrorPath</key>
    <string>@LOG_DIR@/@LABEL@.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>@PATH@</string>
        <key>FRIDAY_PYTHON</key>
        <string>@PYTHON@</string>
    </dict>
</dict>
</plist>
"""


def is_macos() -> bool:
    return sys.platform == "darwin"


def resolve_python() -> str:
    """The interpreter LaunchAgents should use.

    Never a hardcoded framework version — that is precisely what broke before.
    A venv next to friday.py wins, then the running interpreter, then PATH.
    Under a frozen .app sys.executable is the app itself, which is correct:
    the agent should re-launch the app, not a loose python.
    """
    venv = PKG_DIR / ".venv" / "bin" / "python3"
    if venv.exists():
        return str(venv)
    if sys.executable:
        return sys.executable
    found = shutil.which("python3")
    if found:
        return found
    raise RuntimeError("No python3 interpreter found for the LaunchAgent.")


def _render(label: str, program_args: list[str], python: str) -> str:
    args_xml = "\n".join(
        f"        <string>{_xml_escape(a)}</string>" for a in program_args
    )
    py_bin = str(Path(python).parent)
    return (
        _PLIST_TEMPLATE
        .replace("@LABEL@", label)
        .replace("@PROGRAM_ARGS@", args_xml)
        .replace("@APP_DIR@", _xml_escape(str(PKG_DIR)))
        .replace("@LOG_DIR@", _xml_escape(str(paths.log_dir())))
        .replace("@PATH@", _xml_escape(f"{py_bin}:{_BASE_PATH}"))
        .replace("@PYTHON@", _xml_escape(python))
    )


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _write_agent(label: str, program_args: list[str], python: str) -> Path:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    target = LAUNCH_AGENTS / f"{label}.plist"
    text = _render(label, program_args, python)
    # Parse what we generated before installing it: a malformed plist is
    # rejected silently by launchd, which is miserable to debug.
    plistlib.loads(text.encode())
    target.write_text(text)
    logger.info(f"Wrote LaunchAgent {target}")
    return target


def install_agents(voice: bool = False, menubar: bool = True) -> list[Path]:
    """Write (and reload) Friday's LaunchAgents. Returns the paths written."""
    if not is_macos():
        return []
    python = resolve_python()
    written = [_write_agent(CORE_LABEL, [python, str(PKG_DIR / "friday.py")], python)]

    if menubar:
        written.append(
            _write_agent(MENUBAR_LABEL, [python, str(PKG_DIR / "menubar.py")], python)
        )

    if voice:
        # The voice agent launches the .app wrapper, not python directly — TCC
        # grants the microphone to the bundle, not to the interpreter. See the
        # header comment in FridayVoice_launcher.c.
        app = voice_app_path()
        if app is not None:
            written.append(
                _write_agent(VOICE_LABEL,
                             [str(app / "Contents" / "MacOS" / "FridayVoice")],
                             python)
            )
        else:
            logger.warning("FridayVoice.app not found — skipping the voice agent.")

    for p in written:
        reload_agent(p.stem)
    return written


def voice_app_path() -> Path | None:
    """Locate FridayVoice.app in a source checkout or inside a frozen bundle."""
    candidates = [
        PKG_DIR.parent / "FridayVoice.app",              # source checkout
        paths.resource_path("FridayVoice.app"),          # bundled resource
        Path("/Applications/FridayVoice.app"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def write_voice_launcher_conf(python: str | None = None) -> Path:
    """Write the two-line conf the C launcher reads (interpreter, then script).

    This is the substitution step that replaces what used to be a hardcoded
    path inside the compiled binary.
    """
    interp = python or resolve_python()
    # The voice satellite has its own venv when one exists — it pulls in heavy
    # audio deps the core does not need.
    voice_venv = PKG_DIR / "voice" / ".venv" / "bin" / "python3"
    if voice_venv.exists():
        interp = str(voice_venv)
    script = PKG_DIR / "voice" / "listen.py"

    conf = paths.data_dir() / "voice_launcher.conf"
    conf.write_text(f"{interp}\n{script}\n")
    logger.info(f"Wrote voice launcher conf: {conf}")
    return conf


def reload_agent(label: str) -> None:
    """bootout + bootstrap so an edited plist actually takes effect."""
    if not is_macos():
        return
    uid = os.getuid()
    target = f"gui/{uid}"
    plist = LAUNCH_AGENTS / f"{label}.plist"
    # bootout fails when it was never loaded; that is fine.
    subprocess.run(["launchctl", "bootout", f"{target}/{label}"],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", target, str(plist)],
                       capture_output=True, check=False)
    if r.returncode != 0:
        logger.warning(
            f"launchctl bootstrap {label} failed ({r.returncode}): "
            f"{r.stderr.decode(errors='replace').strip()}"
        )


def uninstall_agents() -> list[str]:
    """Unload and remove every Friday LaunchAgent. Returns labels removed."""
    if not is_macos():
        return []
    removed = []
    uid = os.getuid()
    for label in (CORE_LABEL, MENUBAR_LABEL, VOICE_LABEL):
        plist = LAUNCH_AGENTS / f"{label}.plist"
        if not plist.exists():
            continue
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True, check=False)
        plist.unlink()
        removed.append(label)
    logger.info(f"Removed LaunchAgents: {removed}")
    return removed
