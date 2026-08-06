"""
setup_wizard.py
First-run setup wizard for the packaged Windows and macOS builds. Tkinter
(stdlib — freezes cleanly under PyInstaller).

Writes to paths.config_path(): %APPDATA%\\Friday\\friday_config.yaml on
Windows, ~/.friday/friday_config.yaml in a frozen macOS .app, next to
friday.py in a source checkout.

    1. Telegram bot token  (+ automatic chat-id detection via getUpdates)
    2. Gemini API key      (validated against the models endpoint) + model
    3. Google Calendar     (OAuth installed-app flow; saves token; lets the
                            user pick a default calendar + briefing calendars)
                            — skipped on the Apple Calendar backend
    4. Canvas              (optional: iCal URL + access token)
    5. Weather             (optional, OpenWeatherMap)
    6. Schedule            (timezone + briefing times)

Every credential step doubles as a walkthrough: the numbered instructions and
"Open …" buttons are meant to get a user who has never seen BotFather or
Google AI Studio from nothing to a working key without leaving the window.

run(first_run=True) -> bool  — True when a config was written.
"""

import logging
import re
import shutil
import sys
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from tkinter import filedialog, messagebox, ttk

import requests
import yaml

import compat
import paths

# Before the first tk.Tk(), which is what registers this process with the Dock.
compat.set_mac_app_name()

logger = logging.getLogger("friday.wizard")

_COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
    "Pacific/Honolulu", "Europe/London", "Europe/Paris",
]

_GEMINI_DEFAULT_MODEL = "gemma-4-31b-it"

# Offered in the model drop-down. Only models that support BOTH
# systemInstruction and functionDeclarations belong here — agent/core.py sends
# the persona as a system instruction and the calendar tools as function
# declarations on every call, so a model missing either silently breaks
# briefings and calendar writes rather than erroring at startup.
_GEMINI_MODELS = [
    "gemma-4-31b-it",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]

# Base URL for the Telegram API. Module-level so it can be pointed at an
# unreachable host to exercise the network-failure path.
_TELEGRAM_API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_NET_MSG = ("Couldn't reach Telegram — check your internet connection "
            "and try again.")


def _token_format_ok(token: str) -> bool:
    """Cheap local sanity check before spending a network round-trip."""
    return bool(_TOKEN_RE.match((token or "").strip()))


def _chat_id_ok(chat_id: str) -> bool:
    """A chat ID is a bare number — negative for groups, positive for a DM.

    The tempting wrong answer is the bot's own @username, which looks like an
    address and is accepted by every field that takes a string. Telegram then
    rejects it with a 403 on the first sendMessage, which happens at *startup*,
    long after the wizard has closed — so catch it here."""
    return bool(re.match(r"^-?\d+$", (chat_id or "").strip()))


def _mask_token(token: str) -> str:
    """Show only the last 4 characters. The raw token must never reach a
    dialog, status label, or log line."""
    token = (token or "").strip()
    if len(token) <= 4:
        return "•" * len(token)
    return "••••••" + token[-4:]


def _scrub(text: str, secret: str) -> str:
    """Redact a secret that may be embedded in an exception/URL string."""
    secret = (secret or "").strip()
    if secret and secret in text:
        text = text.replace(secret, _mask_token(secret))
    return text


def _telegram_getme(token: str, base: str | None = None, timeout: int = 10):
    """Validate a bot token. Returns exactly one of:
        ("ok", username)      — HTTP 200 and ok:true
        ("auth", description) — reachable but token rejected (ok:false / 401)
        ("network", message)  — could not reach Telegram (secret masked)
    The auth/network split is deliberate: only a genuine rejection should ever
    tell the user their token is bad."""
    base = base or _TELEGRAM_API
    try:
        r = requests.get(f"{base}/bot{token}/getMe", timeout=timeout)
    except requests.RequestException as e:
        return ("network", _scrub(str(e), token))
    try:
        data = r.json()
    except ValueError:
        return ("network", f"Unexpected response from Telegram (HTTP {r.status_code}).")
    if r.status_code == 200 and data.get("ok"):
        return ("ok", (data.get("result") or {}).get("username", ""))
    return ("auth", data.get("description") or f"HTTP {r.status_code}")


def _telegram_getupdates(token: str, base: str | None = None, timeout: int = 10):
    """Fetch the chat IDs the bot can see. Returns exactly one of:
        ("ok", set_of_chat_ids) — HTTP 200 and ok:true (the set may be empty,
                                  meaning the user hasn't messaged the bot yet)
        ("auth", description)   — token rejected
        ("network", message)    — could not reach Telegram (secret masked)"""
    base = base or _TELEGRAM_API
    try:
        r = requests.get(f"{base}/bot{token}/getUpdates", timeout=timeout)
    except requests.RequestException as e:
        return ("network", _scrub(str(e), token))
    try:
        data = r.json()
    except ValueError:
        return ("network", f"Unexpected response from Telegram (HTTP {r.status_code}).")
    if r.status_code == 200 and data.get("ok"):
        chats = {
            str(u["message"]["chat"]["id"])
            for u in data.get("result", []) if "message" in u
        }
        return ("ok", chats)
    return ("auth", data.get("description") or f"HTTP {r.status_code}")


def _canvas_whoami(ical_url: str, token: str, timeout: int = 15):
    """Validate a Canvas access token against the school the feed URL points at.

    Canvas is self-hosted per institution, so there is no single API host to
    check against — the iCal URL the user just pasted is what tells us which
    one to ask. Returns the same three-way result as _telegram_getme()."""
    host = urllib.parse.urlparse((ical_url or "").strip()).netloc
    if not host:
        return ("network", "Add the calendar feed URL first — the token is "
                           "checked against your school's Canvas site.")
    try:
        r = requests.get(f"https://{host}/api/v1/users/self",
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=timeout)
    except requests.RequestException as e:
        return ("network", _scrub(str(e), token))
    if r.status_code in (401, 403):
        return ("auth", f"Canvas rejected that token (HTTP {r.status_code}).")
    if r.status_code != 200:
        return ("network", f"Unexpected response from Canvas (HTTP {r.status_code}).")
    try:
        return ("ok", (r.json() or {}).get("name", ""))
    except ValueError:
        return ("ok", "")


def _guess_timezone() -> str:
    try:
        import tzlocal
        return str(tzlocal.get_localzone())
    except Exception:
        return "America/Chicago"


# ── Platform seams ────────────────────────────────────────────────────────────
# The wizard ships on both platforms, and the differences are all cosmetic or
# calendar-related. Keeping them here stops per-platform branches from spreading
# through the step builders.

IS_MAC = sys.platform == "darwin"

# Segoe UI does not exist on macOS; asking Tk for it silently falls back to a
# default that looks nothing like the rest of the window.
_HEADING_FONT = ("SF Pro Display", 18, "bold") if IS_MAC else ("Segoe UI", 16, "bold")

# macOS's default UI font is wider than Segoe UI, so the same entry rows need
# more room; at 620 the right edge of every text field is cut off.
_WIN_WIDTH = 700 if IS_MAC else 620

# Where the app lives once running, for instructions that point at it.
_APP_SURFACE = "menu bar" if IS_MAC else "system tray"
_MENU_GESTURE = "Click" if IS_MAC else "Right-click"
_MULTISELECT_KEY = "Command-click" if IS_MAC else "Ctrl-click"

# Text colours, keyed by role. The light-mode values are the originals; macOS
# Dark Mode paints the window near-black, where "#555" grey and a plain "red"
# are both unreadable. Filled in from the live theme by _init_palette(), which
# is why every call site reads through this dict instead of a constant.
_PALETTE = {
    "muted":    "#555555",   # sub-headings and asides
    "warn":     "#b07000",   # gotchas the user needs to notice
    "ok":       "green",     # a check that passed
    "err":      "red",       # a check that failed
    # Classic (non-ttk) widgets only — ttk handles its own theming.
    "field_bg": "white",
    "field_fg": "black",
    "sel_bg":   "#0b5fff",
    "sel_fg":   "white",
    "border":   "#c0c0c0",
}


def _init_palette(root: tk.Misc) -> None:
    """Pick colours from the theme's own background luminance.

    Reading it out of Tk rather than shelling out to `defaults read -g
    AppleInterfaceStyle` means this works on every platform and matches
    whatever theme ttk actually resolved, not what the OS says it prefers.
    """
    try:
        bg = ttk.Style(root).lookup("TLabel", "background") or ""
        r, g, b = root.winfo_rgb(bg)          # 16 bits per channel
        if (0.299 * r + 0.587 * g + 0.114 * b) / 65535 >= 0.5:
            return                            # light theme: defaults are right
    except Exception:
        return                                # unknown theme: assume light
    _PALETTE.update(muted="#a8a8a8", warn="#e5b45a",
                    ok="#5dd47f", err="#ff6b6b",
                    field_bg="#2c2c2e", field_fg="#e8e8e8",
                    sel_bg="#0a84ff", sel_fg="#ffffff",
                    border="#4a4a4c")


def _combobox(parent: tk.Misc, **kwargs) -> ttk.Combobox:
    """A ttk.Combobox whose open drop-down tracks the pointer.

    Tk's built-in <Motion> binding on the popdown list only *activates* the row
    under the cursor — on macOS that is an underline nobody can see, so an open
    drop-down looks frozen while the mouse moves over it and the whole control
    reads as broken. Moving the selection instead is also what makes the click
    land where the eye expects: ttk::combobox::LBSelected commits whatever is
    selected, not whatever happens to be under the pointer.

    The popdown is a plain tk.Listbox that ttk does not theme, so it needs the
    same explicit dark-mode colours as the briefing-calendar list.
    """
    combo = ttk.Combobox(parent, **kwargs)
    try:
        popdown = combo.tk.eval(f"ttk::combobox::PopdownWindow {combo}")
        lb = f"{popdown}.f.l"
        combo.tk.call(lb, "configure",
                      "-background", _PALETTE["field_bg"],
                      "-foreground", _PALETTE["field_fg"],
                      "-selectbackground", _PALETTE["sel_bg"],
                      "-selectforeground", _PALETTE["sel_fg"],
                      "-activestyle", "none")
        combo.tk.eval(
            "bind " + lb + " <Motion> {"
            " %W selection clear 0 end;"
            " %W selection set [%W index @%x,%y];"
            " %W activate [%W index @%x,%y] }"
        )
    except tk.TclError as e:
        # Private Tk internals; a future Tk that renames them must not take the
        # wizard down with it. Worst case the drop-down looks the way it did.
        logger.debug(f"Combobox hover highlight unavailable: {e}")
    return combo


def _calendar_backend() -> str:
    """Which calendar Friday will use — the same rule calendars/backend.py
    applies, resolved without importing it (the wizard runs before any config
    exists, and backend.py expects one)."""
    return "google" if sys.platform == "win32" else "apple"


def _macos_foreground(win: tk.Tk) -> None:
    """Put the wizard in front of whatever the user was looking at.

    Setup sends people to a browser several times (BotFather, AI Studio,
    OpenWeatherMap), and the window they have to come back to must not be
    buried behind the browser they were just sent to. Also guards the
    activation policy: only the menu bar process is meant to be an accessory
    (see mac_app._become_accessory), and Tk never puts a window on screen in an
    accessory process, so assert Regular here rather than trust it.
    """
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
    except Exception as e:
        logger.warning(f"Could not promote the wizard to a foreground app: {e}")
    # Raise above the browser, then drop topmost so it does not sit over every
    # other window for the rest of setup.
    win.lift()
    win.attributes("-topmost", True)
    win.after(500, lambda: win.attributes("-topmost", False))


class Wizard(tk.Tk):
    def __init__(self, first_run: bool = True):
        super().__init__()
        self.title("Friday Setup")
        # Tall enough for the longest walkthrough (Telegram, 7 steps) without
        # scrolling. Height is resizable so a short display can still reach the
        # nav buttons; width is fixed because every wraplength is tuned to it.
        self.geometry(f"{_WIN_WIDTH}x680")
        self.resizable(False, True)
        self.minsize(_WIN_WIDTH, 420)
        _init_palette(self)          # before any step builds a coloured label
        self.completed = False

        # Start from the existing config when re-running setup.
        self.cfg: dict = {}
        if paths.config_path().exists():
            try:
                with open(paths.config_path()) as f:
                    self.cfg = yaml.safe_load(f) or {}
            except Exception:
                self.cfg = {}

        # Which event store this machine will use. Honour an existing config
        # (someone may have deliberately put the google backend on a Mac);
        # otherwise take the platform default.
        self.backend = (
            ((self.cfg.get("calendar") or {}).get("backend") or "").strip().lower()
            or _calendar_backend()
        )
        if self.backend not in ("apple", "google"):
            self.backend = _calendar_backend()

        # Calendar names offered in the pickers, from whichever backend is
        # active. Named for its role, not its source. The writable subset is
        # what may become the default calendar — see _calendar_pickers.
        self.available_calendars: list[str] = []
        self._writable_calendars: list[str] = []

        # ── In-memory model: the single source of truth for entered values ──
        # Every field's current contents live here, keyed by field name.
        # Navigating away from a step writes its fields in; navigating into a
        # step reads them back out. Nothing is written to disk until Finish.
        self._values: dict = {}
        # Last value confirmed valid per field. Commit 2's re-validation reads
        # this to skip re-checking a value that hasn't changed since.
        self._validated: dict = {}
        # StringVars for the step currently on screen, snapshotted on nav.
        self._step_vars: dict[str, tk.StringVar] = {}
        self._tg_username = ""       # cached from a successful getMe
        self._seed_values()

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        # Nav is packed against the bottom edge FIRST so pack gives it its space
        # before the body gets any. The walkthrough boxes make some steps taller
        # than others, and a body packed first would push Next off-screen.
        nav = ttk.Frame(container)
        nav.pack(side="bottom", fill="x", pady=(12, 0))
        self.body = ttk.Frame(container)
        self.body.pack(side="top", fill="both", expand=True)

        self.back_btn = ttk.Button(nav, text="← Back", command=self._back)
        self.back_btn.pack(side="left")
        self.reset_btn = ttk.Button(nav, text="Start over", command=self._reset)
        self.reset_btn.pack(side="left", padx=(8, 0))
        self.next_btn = ttk.Button(nav, text="Next →", command=self._next)
        self.next_btn.pack(side="right")

        self.steps = [
            self._step_welcome,
            self._step_telegram,
            self._step_gemini,
            self._step_calendar,
            self._step_canvas,
            self._step_weather,
            self._step_schedule,
            self._step_finish,
        ]
        self.step_idx = 0
        # Per-step hooks, (re)set by _show_step for each step builder:
        #   _validate — may we advance? (may run async and call _advance itself)
        #   _collect  — snapshot this step's fields into self._values
        self._validate = lambda: True
        self._collect = self._collect_step
        self._show_step()

        if IS_MAC:
            _macos_foreground(self)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _seed_values(self):
        """Populate the in-memory model from the on-disk config. Used at
        startup and again after a reset."""
        g = self._cfg_get
        self._values = {
            "tg_token":      g("telegram", "bot_token"),
            "tg_chat_id":    str(g("telegram", "chat_id") or ""),
            "gemini_key":    g("gemini", "api_key"),
            "gemini_model":  g("gemini", "model") or _GEMINI_DEFAULT_MODEL,
            "default_cal":   g("agent", "default_calendar") or "",
            "briefing_cals": list(
                (self.cfg.get("agent") or {}).get("briefing_calendars") or []),
            "canvas_url":    g("canvas", "ical_url"),
            "canvas_token":  g("canvas", "api_token"),
            "weather_key":   g("weather", "api_key"),
            "weather_loc":   g("weather", "location"),
            "tz":            g("agent", "timezone") or _guess_timezone(),
            "morning":       g("agent", "morning_briefing_time") or "07:00",
            "evening":       g("agent", "briefing_time") or "20:00",
        }
        self._validated = {}

    def _show_step(self):
        for w in self.body.winfo_children():
            w.destroy()
        self._step_vars = {}
        self._validate = lambda: True
        self._collect = self._collect_step
        self.steps[self.step_idx]()
        self.back_btn.state(["!disabled"] if self.step_idx > 0 else ["disabled"])
        self.next_btn.config(
            text="Finish" if self.step_idx == len(self.steps) - 1 else "Next →"
        )

    def _collect_step(self):
        """Default collector: snapshot every registered StringVar into the
        model. Steps with non-Entry widgets extend this via self._collect."""
        for key, var in self._step_vars.items():
            self._values[key] = var.get()

    def _next(self):
        if self._validate():
            self._advance()

    def _advance(self):
        """Snapshot the current step into the model and move forward. Steps
        whose validation runs asynchronously call this directly on success."""
        self._collect()
        if self.step_idx == len(self.steps) - 1:
            self._write_config()
            return
        self.step_idx += 1
        self._show_step()

    def _back(self):
        if self.step_idx > 0:
            self._collect()          # preserve edits even when moving backward
            self.step_idx -= 1
            self._show_step()

    def _set_busy(self, busy: bool, status=None, msg: str = "",
                  color: str | None = None):
        """Toggle the 'Checking…' state: disable nav while a network check runs
        so the window never looks frozen, and restore it on the result."""
        nav_state = ["disabled"] if busy else ["!disabled"]
        self.next_btn.state(nav_state)
        self.reset_btn.state(nav_state)
        if busy:
            self.back_btn.state(["disabled"])
        else:
            self.back_btn.state(["!disabled"] if self.step_idx > 0 else ["disabled"])
        if status is not None and msg:
            status.config(text=msg, foreground=color or _PALETTE["muted"])

    def _reset(self):
        """Start over: clear the in-memory model and any stale config on disk so
        a wedged user gets a clean first run without deleting files by hand."""
        if not messagebox.askyesno(
                "Start over",
                "Clear everything entered so far and return to the first step?\n\n"
                "This also removes any saved setup file so you start completely "
                "fresh — nothing new is saved until you click Finish."):
            return
        # Only ever touches Friday's own config path (paths.config_path()).
        try:
            cp = paths.config_path()
            if cp.exists():
                cp.unlink()
                logger.info("Setup reset: removed existing config file.")
        except Exception as e:
            logger.warning(f"Setup reset: could not remove config: {e}")
        self.cfg = {}
        self.available_calendars = []
        self._writable_calendars = []
        self._tg_username = ""
        self._seed_values()          # cfg is empty now, so the model comes up blank
        self.step_idx = 0
        self._show_step()

    # ── Small helpers ─────────────────────────────────────────────────────────

    def _heading(self, title: str, sub: str = ""):
        ttk.Label(self.body, text=title, font=_HEADING_FONT).pack(
            anchor="w", pady=(0, 4))
        if sub:
            ttk.Label(self.body, text=sub, wraplength=560,
                      foreground=_PALETTE["muted"]).pack(anchor="w", pady=(0, 12))

    def _entry_row(self, label: str, key: str, show: str = "") -> tk.StringVar:
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=18).pack(side="left")
        var = tk.StringVar(value=self._values.get(key, ""))
        ttk.Entry(row, textvariable=var, show=show, width=52).pack(
            side="left", fill="x", expand=True)
        self._step_vars[key] = var
        return var

    def _walkthrough(self, steps: list[str], parent=None):
        """Render numbered instructions for creating a credential.

        Every credential step gets one of these. The point is that someone who
        has never heard of BotFather or an API key can follow along without
        opening a browser tab to search for what the wizard means — so the
        wording names the exact button to click on the exact page.
        """
        parent = parent if parent is not None else self.body
        box = ttk.Frame(parent, relief="groove", borderwidth=1, padding=10)
        box.pack(fill="x", pady=(0, 10))
        for i, text in enumerate(steps, 1):
            row = ttk.Frame(box)
            row.pack(fill="x", anchor="w", pady=1)
            ttk.Label(row, text=f"{i}.", width=3).pack(side="left", anchor="n")
            ttk.Label(row, text=text, wraplength=500, justify="left").pack(
                side="left", anchor="w")
        return box

    def _open_button(self, text: str, url: str, parent=None):
        """Big obvious 'take me there' button. Paired with _walkthrough so the
        user never has to find the page themselves."""
        parent = parent if parent is not None else self.body
        btn = ttk.Button(parent, text=text,
                         command=lambda: webbrowser.open(url))
        btn.pack(anchor="w", pady=(0, 8))
        return btn

    def _note(self, text: str, color: str | None = None, parent=None):
        """A gotcha the user will otherwise hit and misdiagnose."""
        parent = parent if parent is not None else self.body
        lbl = ttk.Label(parent, text=text, wraplength=560,
                        foreground=color or _PALETTE["warn"],
                        justify="left")
        lbl.pack(anchor="w", pady=(0, 6))
        return lbl

    def _status_label(self) -> ttk.Label:
        lbl = ttk.Label(self.body, text="", wraplength=560)
        lbl.pack(anchor="w", pady=8)
        return lbl

    def _cfg_get(self, *keys, default=""):
        node = self.cfg
        for k in keys[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                return default
        if not isinstance(node, dict):
            return default
        val = node.get(keys[-1], default)
        return val if val is not None else default

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _step_welcome(self):
        cal = "Apple Calendar" if self.backend == "apple" else "Google Calendar"
        self._heading(
            "Welcome to Friday",
            f"Friday is a personal AI secretary. She watches your Canvas "
            f"assignments, GroupMe chats, weather, and {cal} — and talks to "
            f"you through Telegram with morning/evening briefings and urgent "
            f"alerts.",
        )
        ttk.Label(self.body, wraplength=560, text=(
            "This wizard takes about 10 minutes. You do not need to prepare "
            "anything in advance — each step walks you through creating the "
            "account or key it needs, with a button that opens the right page."
        )).pack(anchor="w", pady=(0, 10))

        needed = [
            "Telegram — a free chat app. Install it on your phone first if you "
            "don't have it; that is the only thing you need before starting.",
            "A Gemini API key — free, no credit card. Step 2 creates one.",
        ]
        if self.backend == "apple":
            needed.append(
                "Apple Calendar — already on this Mac. Step 3 just asks macOS "
                "for permission to use it.")
        else:
            needed.append(
                "A Google account — Step 3 signs you in and picks your "
                "calendars.")
        needed.append(
            "Optional: your Canvas calendar link and a free weather key. Both "
            "steps can be skipped and added later from the dashboard.")

        ttk.Label(self.body, text="What Friday needs:",
                  font=("", 11, "bold")).pack(anchor="w", pady=(0, 4))
        for item in needed:
            row = ttk.Frame(self.body)
            row.pack(fill="x", anchor="w", pady=1)
            ttk.Label(row, text="•", width=2).pack(side="left", anchor="n")
            ttk.Label(row, text=item, wraplength=520, justify="left").pack(
                side="left", anchor="w")

        self._note("Nothing is saved until you click Finish on the last step. "
                   "You can go Back at any point without losing what you typed.",
                   color=_PALETTE["muted"])

    def _step_telegram(self):
        self._heading(
            "Step 1 — Telegram bot",
            "Friday talks to you through a private Telegram bot that you own.",
        )
        self._walkthrough([
            "Install Telegram on your phone or Mac and create an account if "
            "you don't already have one.",
            "Click the button below. It opens a chat with @BotFather — "
            "Telegram's official bot for making other bots.",
            "Press START, then send the message:   /newbot",
            "BotFather asks for a display name. Type anything — \"Friday\" "
            "works.",
            "It then asks for a username, which must be unique across all of "
            "Telegram and must end in \"bot\". Try something like "
            "friday_yourname_bot. If it says the name is taken, just try "
            "another.",
            "BotFather replies with a long token that looks like "
            "123456789:AAExample-Token-Characters. Copy the whole thing and "
            "paste it below.",
            "Send your new bot any message (open the chat BotFather links to "
            "and press START), then click \"Detect my chat ID\". Telegram will "
            "not let a bot message you until you message it first.",
        ])
        self._open_button("Open BotFather in Telegram  →",
                          "https://t.me/BotFather")
        self._note("Treat this token like a password — anyone who has it can "
                   "control your bot. It is stored only on this machine.")

        self.tg_token = self._entry_row("Bot token", "tg_token")
        self.tg_chat_id = self._entry_row("Chat ID", "tg_chat_id")
        self._note("Leave the chat ID blank and press \"Detect my chat ID\" — "
                   "it's a number like 123456789, not your bot's @name.",
                   color=_PALETTE["muted"])

        status = self._status_label()

        # ── Async token/chat validation ────────────────────────────────────
        # getMe/getUpdates run off the Tk thread so the window never freezes;
        # results marshal back via self.after. The token is masked in every
        # message, and a network failure is never reported as a bad token.

        def resolve_chat(token, on_ready):
            """Token is known good. Ensure a chat id exists (auto-detect when
            blank), then call on_ready()."""
            if self.tg_chat_id.get().strip():
                on_ready()
                return
            self._set_busy(True, status, "Looking for your chat…")

            def done(res):
                self._set_busy(False)
                kind, info = res
                if kind == "network":
                    status.config(text=_NET_MSG, foreground=_PALETTE["err"])
                elif kind == "auth":
                    status.config(text=f"Telegram rejected that token "
                                        f"({_mask_token(token)}).", foreground=_PALETTE["err"])
                elif info:
                    self.tg_chat_id.set(sorted(info)[-1])
                    on_ready()
                else:
                    uname = self._tg_username or "your bot"
                    status.config(
                        text=(f"Bot @{uname} works, but you haven't messaged it "
                              f"yet. Open Telegram, press Start on the bot, send "
                              f"it any message, then click Next again."),
                        foreground=_PALETTE["warn"])
                    if self._tg_username:
                        webbrowser.open(f"https://t.me/{self._tg_username}")

            def worker():
                res = _telegram_getupdates(token)
                self.after(0, lambda: done(res))
            threading.Thread(target=worker, daemon=True).start()

        def verify_token(token, on_valid):
            """Validate the token via getMe off-thread, then call on_valid()."""
            self._set_busy(True, status, "Checking with Telegram…")

            def done(res):
                kind, info = res
                if kind == "ok":
                    self._validated["tg_token"] = token
                    self._tg_username = info or ""
                    self._set_busy(False)
                    on_valid()
                    return
                self._set_busy(False)
                if kind == "network":
                    status.config(text=_NET_MSG, foreground=_PALETTE["err"])
                else:
                    status.config(
                        text=(f"Telegram rejected that token ({_mask_token(token)}). "
                              f"Double-check what BotFather gave you."),
                        foreground=_PALETTE["err"])
                    logger.warning(f"Telegram token rejected ({_mask_token(token)})")

            def worker():
                res = _telegram_getme(token)
                self.after(0, lambda: done(res))
            threading.Thread(target=worker, daemon=True).start()

        def detect():
            token = self.tg_token.get().strip()
            if not token:
                status.config(text="Paste the bot token first.", foreground=_PALETTE["err"])
                return
            if not _token_format_ok(token):
                status.config(text="That doesn't look like a bot token "
                                   "(it should look like 123456789:AA…).",
                              foreground=_PALETTE["err"])
                return

            def report_chat():
                if self.tg_chat_id.get().strip():
                    status.config(
                        text=f"✓ Bot @{self._tg_username} verified — chat ID "
                             f"detected.", foreground=_PALETTE["ok"])
            # Verify, then look for a chat id — but Detect never advances.
            verify_token(token, lambda: resolve_chat(token, report_chat))

        ttk.Button(self.body, text="Detect my chat ID", command=detect).pack(
            anchor="w", pady=4)

        def ok():
            token = self.tg_token.get().strip()
            if not token:
                status.config(text="Paste the bot token from BotFather first.",
                              foreground=_PALETTE["err"])
                return False
            if not _token_format_ok(token):
                status.config(
                    text=("That doesn't look like a bot token — it should look "
                          "like 123456789:AA… . Check for a copy/paste slip."),
                    foreground=_PALETTE["err"])
                return False
            chat_id = self.tg_chat_id.get().strip()
            if chat_id and not _chat_id_ok(chat_id):
                status.config(
                    text=("The chat ID must be a number, not a name — @your_bot "
                          "is the bot's address, not the chat's. Clear the field "
                          "and press \"Detect my chat ID\"."),
                    foreground=_PALETTE["err"])
                return False
            # Skip the network round-trip when this exact token already passed.
            if self._validated.get("tg_token") == token:
                resolve_chat(token, self._advance)
            else:
                verify_token(token, lambda: resolve_chat(token, self._advance))
            return False  # advancement happens inside the async callbacks above
        self._validate = ok

    def _step_gemini(self):
        self._heading(
            "Step 2 — Gemini API key",
            "Friday's brain. The free tier is plenty for personal use — "
            "no credit card needed.",
        )
        self._walkthrough([
            "Click the button below to open Google AI Studio, and sign in with "
            "any Google account.",
            "Accept the terms if it asks. You may also be asked to pick or "
            "create a Google Cloud project — take the default it offers; you "
            "never have to touch it again.",
            "Click \"Create API key\" (top right, or the blue button in the "
            "middle of the page).",
            "Copy the key it shows you and paste it below. It starts with "
            "AIza and is about 39 characters.",
        ])
        self._open_button("Open Google AI Studio  →",
                          "https://aistudio.google.com/apikey")
        self._note("The free tier covers normal personal use. Google will ask "
                   "for a card only if you deliberately upgrade — this wizard "
                   "never does.", color=_PALETTE["muted"])

        self.gemini_key = self._entry_row("API key", "gemini_key")

        model_row = ttk.Frame(self.body)
        model_row.pack(fill="x", pady=4)
        ttk.Label(model_row, text="Model", width=18).pack(side="left")
        self.gemini_model = tk.StringVar(
            value=self._values.get("gemini_model") or _GEMINI_DEFAULT_MODEL)
        self._step_vars["gemini_model"] = self.gemini_model
        _combobox(model_row, textvariable=self.gemini_model,
                  values=list(_GEMINI_MODELS), state="readonly",
                  width=30).pack(side="left")
        self._note(f"{_GEMINI_DEFAULT_MODEL} is the default and what Friday is "
                   "tuned against. Leave it alone unless you know you want "
                   "something else.", color=_PALETTE["muted"])

        status = self._status_label()

        def validate_key() -> bool:
            key = self.gemini_key.get().strip()
            if not key:
                messagebox.showwarning("Gemini required",
                                       "Friday needs a Gemini API key to think.")
                return False
            try:
                r = requests.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": key}, timeout=15)
                if r.status_code != 200:
                    status.config(text=f"Key rejected ({r.status_code}). "
                                       "Double-check and try again.",
                                  foreground=_PALETTE["err"])
                    return False
            except Exception as e:
                status.config(text=f"Network error: {_scrub(str(e), key)}",
                              foreground=_PALETTE["err"])
                return False
            # The models endpoint already told us exactly what this key can
            # reach, so a model the key cannot use is caught here rather than
            # at the first briefing. Names come back as "models/<id>".
            chosen = self.gemini_model.get().strip()
            try:
                available = {
                    (m.get("name") or "").split("/")[-1]
                    for m in (r.json() or {}).get("models", [])
                }
            except ValueError:
                available = set()
            if available and chosen not in available:
                status.config(
                    text=f"✓ Key verified, but {chosen} isn't available on it. "
                         f"Pick another model.", foreground=_PALETTE["err"])
                return False
            status.config(text=f"✓ Key verified — {chosen}.",
                          foreground=_PALETTE["ok"])
            return True
        self._validate = validate_key

    def _step_calendar(self):
        """Dispatch on the active backend. Apple needs no accounts or keys —
        just macOS Automation permission — so it gets an entirely different
        page rather than a Google page with the parts greyed out."""
        if self.backend == "apple":
            self._step_calendar_apple()
        else:
            self._step_calendar_google()

    # ── Calendar picker, shared by both backends ─────────────────────────────

    def _calendar_pickers(self, parent):
        """Default-calendar combo + briefing-calendar multiselect. Populated by
        whichever backend fetched the names."""
        self.cal_listbox: tk.Listbox | None = None
        self.default_cal = tk.StringVar(value=self._values.get("default_cal", ""))
        self._step_vars["default_cal"] = self.default_cal

        def collect():
            self._collect_step()
            if self.cal_listbox is not None:
                self._values["briefing_cals"] = [
                    self.cal_listbox.get(i)
                    for i in self.cal_listbox.curselection()]
        self._collect = collect

        # Sized by its contents, never expanded to fill the body. An expanded
        # frame already occupies its final geometry while it is still empty, so
        # the widgets added later change nothing about its size — and on macOS a
        # classic Tk widget added into an already-laid-out frame is never damaged
        # and paints its background but none of its rows. Letting the frame grow
        # when show() populates it is what makes the list visible at all.
        frame = ttk.Frame(parent)
        frame.pack(fill="x")

        def show(names: list[str], writable: list[str] | None = None):
            """`names` is everything Friday can read; `writable` is the subset
            she can add events to. Only the writable ones may be the default
            calendar — a subscribed holiday feed accepts no writes, and picking
            one turns every later "add this to my calendar" into a silent
            failure."""
            self.available_calendars = names
            for w in frame.winfo_children():
                w.destroy()
            if not names:
                return
            targets = writable if writable else names
            ttk.Label(frame, text="Default calendar (new events go here):"
                      ).pack(anchor="w", pady=(8, 2))
            combo = _combobox(frame, textvariable=self.default_cal,
                              values=targets, state="readonly", width=40)
            combo.pack(anchor="w")
            if self.default_cal.get() not in targets:
                combo.set(targets[0])
            ttk.Label(frame,
                      text=f"Calendars Friday includes in briefings "
                           f"({_MULTISELECT_KEY} for several):"
                      ).pack(anchor="w", pady=(10, 2))
            # tk.Listbox is a classic widget, not ttk — it does not follow the
            # macOS appearance and renders dark-on-dark (i.e. invisibly) in
            # Dark Mode unless every colour is given explicitly.
            lb = tk.Listbox(frame, selectmode="multiple", height=6,
                            exportselection=False,
                            background=_PALETTE["field_bg"],
                            foreground=_PALETTE["field_fg"],
                            selectbackground=_PALETTE["sel_bg"],
                            selectforeground=_PALETTE["sel_fg"],
                            highlightthickness=1,
                            highlightbackground=_PALETTE["border"])
            for n in names:
                lb.insert("end", n)
            lb.pack(fill="x")
            saved = self._values.get("briefing_cals")
            if saved:
                for idx, name in enumerate(names):
                    if name in saved:
                        lb.selection_set(idx)
            else:
                lb.selection_set(0, "end")
            self.cal_listbox = lb

        return show

    def _step_calendar_apple(self):
        self._heading(
            "Step 3 — Apple Calendar",
            "Apple Calendar is where Friday keeps your events. She reads your "
            "schedule from it and adds events you ask for. Nothing to sign up "
            "for — it is already on this Mac.",
        )
        self._walkthrough([
            "Click \"Find my calendars\" below.",
            "macOS will ask whether Friday may control Calendar. Click OK — "
            "this is the Automation permission, and Friday cannot read or "
            "write a single event without it.",
            "Pick which calendar new events should go to, and which ones "
            "Friday should read when she briefs you.",
        ])
        # Button and status first: the picker frame expands to fill whatever is
        # left, so anything packed after it gets shoved to the window's bottom
        # edge while the calendars are still unknown.
        load_btn = ttk.Button(self.body, text="Find my calendars")
        load_btn.pack(anchor="w", pady=8)
        status = self._status_label()
        show = self._calendar_pickers(self.body)

        def load():
            load_btn.state(["disabled"])
            status.config(text="Asking Calendar… (approve the macOS prompt if "
                               "one appears)", foreground=_PALETTE["muted"])

            def worker():
                try:
                    from calendars.apple import list_calendars_detailed
                    rows = list_calendars_detailed()
                except Exception as e:
                    logger.error(f"Apple Calendar listing failed: {e}")
                    rows, err = [], str(e)
                else:
                    err = ""
                names = [n for n, _ in rows]
                writable = [n for n, w in rows if w]
                self.after(0, lambda: done(names, writable, err))

            def done(names, writable, err):
                load_btn.state(["!disabled"])
                if names:
                    status.config(text=f"✓ Found {len(names)} calendars, "
                                       f"{len(writable)} she can write to.",
                                  foreground=_PALETTE["ok"])
                    show(names, writable)
                    self._writable_calendars = writable
                    return
                # An empty list almost always means the permission prompt was
                # declined, not that the user has no calendars — say so, since
                # the failure is otherwise completely silent.
                status.config(
                    text=("Couldn't read your calendars. Open System Settings → "
                          "Privacy & Security → Automation, enable Calendar "
                          "under Friday, then click Find my calendars again."
                          + (f"\n({err})" if err else "")),
                    foreground=_PALETTE["err"])
            threading.Thread(target=worker, daemon=True).start()

        load_btn.config(command=load)
        if self.available_calendars:      # returning to this step
            show(self.available_calendars, self._writable_calendars)

        def ok():
            if not self.available_calendars:
                return messagebox.askyesno(
                    "Skip calendar?",
                    "Without calendar access Friday can't manage your "
                    "schedule — briefings will be weather and Canvas only.\n\n"
                    "You can grant it later in System Settings and re-run this "
                    "wizard. Skip for now?")
            return True
        self._validate = ok

    def _step_calendar_google(self):
        self._heading(
            "Step 3 — Connect Google Calendar",
            "Google Calendar is where Friday keeps your events. She reads "
            "your schedule from it and adds events you ask for.",
        )
        self._walkthrough([
            "Click \"Connect Google Calendar\" below. Your browser opens.",
            "Sign in with the Google account whose calendar you use.",
            "Google will warn that the app isn't verified — click Advanced, "
            "then \"Go to Friday (unsafe)\". This is expected for a personal "
            "app that was never submitted for Google review.",
            "Allow calendar access, then come back to this window. It fills in "
            "your calendars by itself.",
        ])
        status = self._status_label()

        secret = paths.google_client_secret_path()
        if not secret.exists():
            ttk.Label(self.body, wraplength=560, foreground=_PALETTE["warn"], text=(
                "This installer is missing its Google credentials file "
                "(google_client_secret.json). Ask whoever sent you Friday for "
                "it, then select it below.")).pack(anchor="w", pady=4)

            def pick():
                p = filedialog.askopenfilename(
                    title="Select google_client_secret.json",
                    filetypes=[("JSON", "*.json")])
                if p:
                    shutil.copyfile(p, paths.data_dir() / "google_client_secret.json")
                    status.config(text="✓ Credentials file installed.",
                                  foreground=_PALETTE["ok"])
            ttk.Button(self.body, text="Select credentials file…",
                       command=pick).pack(anchor="w", pady=4)

        connect_btn = ttk.Button(self.body, text="Connect Google Calendar")
        connect_btn.pack(anchor="w", pady=8)

        show_calendars = self._calendar_pickers(self.body)

        def connect():
            if not paths.google_client_secret_path().exists():
                status.config(text="Select the credentials file first.",
                              foreground=_PALETTE["err"])
                return
            connect_btn.state(["disabled"])
            status.config(text="A browser window is opening — sign in and "
                               "allow calendar access…", foreground=_PALETTE["muted"])

            def worker():
                try:
                    from google_auth_oauthlib.flow import InstalledAppFlow
                    from calendars.google_cal import SCOPES
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(paths.google_client_secret_path()), SCOPES)
                    creds = flow.run_local_server(port=0)
                    paths.google_token_path().write_text(creds.to_json())
                    from calendars import google_cal
                    google_cal.reset()
                    from googleapiclient.discovery import build
                    svc = build("calendar", "v3", credentials=creds,
                                cache_discovery=False)
                    items = svc.calendarList().list().execute().get("items", [])
                    names = [
                        (i.get("summaryOverride") or i.get("summary") or "").strip()
                        for i in items
                        if i.get("accessRole") in ("owner", "writer")
                        or i.get("primary")
                    ]
                    names = [n for n in names if n]
                    self.after(0, lambda: (
                        status.config(text="✓ Google Calendar connected.",
                                      foreground=_PALETTE["ok"]),
                        show_calendars(names),
                        connect_btn.state(["!disabled"]),
                    ))
                except Exception as e:
                    logger.error(f"Google OAuth failed: {e}")
                    self.after(0, lambda e=e: (
                        status.config(text=f"Connection failed: {e}",
                                      foreground=_PALETTE["err"]),
                        connect_btn.state(["!disabled"]),
                    ))
            threading.Thread(target=worker, daemon=True).start()

        connect_btn.config(command=connect)
        if self.available_calendars:  # returning to this step after connecting
            show_calendars(self.available_calendars)

        def ok():
            # briefing_cals is snapshotted by this step's collector (which runs
            # while the listbox still exists), so we only gate here.
            if not paths.google_token_path().exists():
                return messagebox.askyesno(
                    "Skip calendar?",
                    "Without Google Calendar, Friday can't manage your "
                    "schedule — briefings will be weather and Canvas only. "
                    "Skip anyway?")
            return True
        self._validate = ok

    def _step_canvas(self):
        self._heading(
            "Step 4 — Canvas (optional)",
            "If your school uses Canvas, Friday can track assignment due "
            "dates and put them on your calendar automatically.",
        )
        self._walkthrough([
            "Open Canvas in a browser and sign in — the desktop site, not the "
            "phone app. It's usually yourschool.instructure.com.",
            "Click Calendar in the left sidebar.",
            "Scroll to the bottom right of the calendar and click "
            "\"Calendar Feed\".",
            "A box appears with a long link ending in .ics. Copy the whole "
            "thing and paste it below.",
        ])
        self._note("Leave this blank to skip — you can add it later from the "
                   "dashboard.", color=_PALETTE["muted"])
        self.canvas_url = self._entry_row("Calendar feed URL", "canvas_url")
        self._note("Anyone with this link can read your assignment schedule, "
                   "so don't post it anywhere public.")

        ttk.Label(self.body, text="Access token (optional)",
                  font=(_HEADING_FONT[0], 12, "bold")).pack(
            anchor="w", pady=(14, 2))
        self._walkthrough([
            "Still in Canvas, click Account in the far-left sidebar → "
            "Settings.",
            "Scroll down to \"Approved Integrations\" and click "
            "\"+ New Access Token\".",
            "Put \"Friday\" as the purpose and leave the expiry blank so it "
            "never stops working. Click Generate Token.",
            "Copy the token it shows you — Canvas shows it exactly once — and "
            "paste it below.",
        ])
        self.canvas_token = self._entry_row("Access token", "canvas_token",
                                            show="•")
        self._note("Optional. Without it the feed still works; with it, Friday "
                   "can tell \"Canvas rejected me\" apart from \"nothing is "
                   "due\" instead of quietly reporting an empty week. This "
                   "token can read your whole Canvas account, so treat it like "
                   "your password.")

        status = self._status_label()

        def check():
            token = self.canvas_token.get().strip()
            if not token:
                status.config(text="Paste an access token first.",
                              foreground=_PALETTE["err"])
                return
            url = self.canvas_url.get().strip()
            self._set_busy(True, status, "Checking with Canvas…")

            def done(res):
                self._set_busy(False)
                kind, info = res
                if kind == "ok":
                    self._validated["canvas_token"] = token
                    who = f" — signed in as {info}" if info else ""
                    status.config(text=f"✓ Token verified{who}.",
                                  foreground=_PALETTE["ok"])
                elif kind == "auth":
                    status.config(text=f"{info} Check what you pasted "
                                       f"({_mask_token(token)}).",
                                  foreground=_PALETTE["err"])
                    logger.warning(f"Canvas token rejected ({_mask_token(token)})")
                else:
                    status.config(text=info, foreground=_PALETTE["err"])

            def worker():
                res = _canvas_whoami(url, token)
                self.after(0, lambda: done(res))
            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(self.body, text="Check token", command=check).pack(
            anchor="w", pady=4)

        def ok():
            url = self.canvas_url.get().strip()
            token = self.canvas_token.get().strip()
            if token and not url:
                messagebox.showwarning(
                    "Feed URL missing",
                    "The access token on its own doesn't tell Friday which "
                    "assignments are yours — paste the calendar feed URL too, "
                    "or clear the token.")
                return False
            if url and ".ics" not in url:
                return messagebox.askyesno(
                    "Unusual URL",
                    "That doesn't look like a Canvas calendar feed "
                    "(no .ics). Use it anyway?")
            return True
        self._validate = ok

    def _step_weather(self):
        self._heading(
            "Step 5 — Weather (optional)",
            "Friday includes a weather note in briefings. Uses a free "
            "OpenWeatherMap key. Leave blank to skip.",
        )
        self._walkthrough([
            "Click the button below to open OpenWeatherMap, then Create an "
            "Account (email and password — no card).",
            "Confirm the email they send you, then sign in.",
            "Click your username at the top right → \"My API keys\".",
            "A key is already there under Key. Copy it and paste it below.",
        ])
        self._open_button("Open OpenWeatherMap  →",
                          "https://home.openweathermap.org/api_keys")
        self.weather_key = self._entry_row("API key", "weather_key")
        self.weather_loc = self._entry_row("Location (City,US)", "weather_loc")
        self._note("A brand-new key takes up to two hours to start working. "
                   "If weather is missing from your first briefings, that's "
                   "why — it fixes itself.")

    def _step_schedule(self):
        self._heading(
            "Step 6 — Your schedule",
            "When should Friday brief you, and what timezone are you in?",
        )
        tz_guess = self._values.get("tz") or _guess_timezone()
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Timezone", width=18).pack(side="left")
        self.tz_var = tk.StringVar(value=tz_guess)
        self._step_vars["tz"] = self.tz_var
        values = list(_COMMON_TIMEZONES)
        if tz_guess not in values:
            values.insert(0, tz_guess)
        _combobox(row, textvariable=self.tz_var, values=values,
                  width=30).pack(side="left")

        self.morning_var = self._entry_row("Morning briefing", "morning")
        self.evening_var = self._entry_row("Evening briefing", "evening")

        def ok():
            for v in (self.morning_var.get(), self.evening_var.get()):
                try:
                    h, m = v.strip().split(":")
                    assert 0 <= int(h) < 24 and 0 <= int(m) < 60
                except Exception:
                    messagebox.showwarning(
                        "Bad time", f"'{v}' isn't a valid 24-hour HH:MM time.")
                    return False
            return True
        self._validate = ok

    def _step_finish(self):
        self._heading(
            "All set!",
            f"Click Finish to save. Friday will start in your {_APP_SURFACE} "
            "and send you a hello on Telegram.",
        )
        ttk.Label(self.body, wraplength=560, text=(
            "Things to try once she's online:\n\n"
            "  •  Send \"brief me\" on Telegram\n"
            "  •  \"Add dentist appointment Friday at 2pm\"\n"
            "  •  \"What's my week look like?\"\n\n"
            f"{_MENU_GESTURE} the {_APP_SURFACE} icon for Brief Me Now, Pause, "
            "the dashboard, or to re-run this wizard."
        )).pack(anchor="w")

        if IS_MAC:
            self._note("macOS will ask for permission the first time Friday touches "
                  "your calendar — click OK on the “Friday wants access to "
                  "Calendar” prompt, or she'll come up with an empty schedule.",
                  parent=self.body)

    # ── Config write ──────────────────────────────────────────────────────────

    def _write_config(self):
        cfg = self.cfg or {}
        v = self._values
        briefing_cals = v.get("briefing_cals") or list(self.available_calendars)

        agent = cfg.setdefault("agent", {})
        agent.setdefault("name", "Friday")
        agent["timezone"] = v.get("tz", "").strip() or "America/Chicago"
        agent["morning_briefing_time"] = v.get("morning", "").strip()
        agent["briefing_time"] = v.get("evening", "").strip()
        if v.get("default_cal"):
            agent["default_calendar"] = v["default_cal"]
        if briefing_cals:
            agent["briefing_calendars"] = briefing_cals

        cfg["calendar"] = {"backend": self.backend}
        cfg["provider"] = "gemini"
        cfg["telegram"] = {
            "bot_token": v.get("tg_token", "").strip(),
            "chat_id":   v.get("tg_chat_id", "").strip(),
        }
        gemini = cfg.setdefault("gemini", {})
        gemini["api_key"] = v.get("gemini_key", "").strip()
        # Assigned, not setdefault: the model is a field the user just chose on
        # the Gemini step, so a re-run of the wizard must be able to change it.
        gemini["model"] = v.get("gemini_model", "").strip() or _GEMINI_DEFAULT_MODEL
        gemini.setdefault("max_tokens", 4000)
        canvas = cfg.setdefault("canvas", {})
        canvas["ical_url"]  = v.get("canvas_url", "").strip()
        canvas["api_token"] = v.get("canvas_token", "").strip()
        cfg.setdefault("weather", {})
        cfg["weather"]["api_key"]  = v.get("weather_key", "").strip()
        cfg["weather"]["location"] = v.get("weather_loc", "").strip()
        cfg.setdefault("memory", {"db_path": "memory/friday_memory.db",
                                  "short_term_turns": 20})

        with open(paths.config_path(), "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=True)
        logger.info(f"Config written: {paths.config_path()}")
        self.completed = True
        self.destroy()


def run(first_run: bool = True) -> bool:
    """Show the wizard. Returns True when the config was written."""
    wiz = Wizard(first_run=first_run)
    wiz.mainloop()
    return wiz.completed


if __name__ == "__main__":
    # Run as its own process by the macOS menu bar — Tkinter and rumps both
    # insist on owning the main thread, so they cannot share one.
    sys.exit(0 if run(first_run="--first-run" in sys.argv) else 1)
