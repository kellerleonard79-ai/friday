"""
setup_wizard.py
First-run setup wizard for the Windows build. Tkinter (stdlib — freezes
cleanly under PyInstaller).

Collects everything Friday needs into %APPDATA%\\Friday\\friday_config.yaml:

    1. Telegram bot token  (+ automatic chat-id detection via getUpdates)
    2. Gemini API key      (validated against the models endpoint)
    3. Google Calendar     (OAuth installed-app flow; saves token; lets the
                            user pick a default calendar + briefing calendars)
    4. Canvas iCal URL     (optional)
    5. Weather             (optional, OpenWeatherMap)
    6. Schedule            (timezone + briefing times)

run(first_run=True) -> bool  — True when a config was written.
"""

import logging
import re
import shutil
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import requests
import yaml

import paths

logger = logging.getLogger("friday.wizard")

_COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
    "Pacific/Honolulu", "Europe/London", "Europe/Paris",
]

_GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"

# Base URL for the Telegram API. Module-level so it can be pointed at an
# unreachable host to exercise the network-failure path.
_TELEGRAM_API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_NET_MSG = ("Couldn't reach Telegram — check your internet connection "
            "and try again.")


def _token_format_ok(token: str) -> bool:
    """Cheap local sanity check before spending a network round-trip."""
    return bool(_TOKEN_RE.match((token or "").strip()))


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


def _guess_timezone() -> str:
    try:
        import tzlocal
        return str(tzlocal.get_localzone())
    except Exception:
        return "America/Chicago"


class Wizard(tk.Tk):
    def __init__(self, first_run: bool = True):
        super().__init__()
        self.title("Friday Setup")
        self.geometry("620x540")
        self.resizable(False, False)
        self.completed = False

        # Start from the existing config when re-running setup.
        self.cfg: dict = {}
        if paths.config_path().exists():
            try:
                with open(paths.config_path()) as f:
                    self.cfg = yaml.safe_load(f) or {}
            except Exception:
                self.cfg = {}

        self.google_calendars: list[str] = []

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
        self.body = ttk.Frame(container)
        self.body.pack(fill="both", expand=True)

        nav = ttk.Frame(container)
        nav.pack(fill="x", pady=(12, 0))
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
            self._step_google,
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

    # ── Navigation ────────────────────────────────────────────────────────────

    def _seed_values(self):
        """Populate the in-memory model from the on-disk config. Used at
        startup and again after a reset."""
        g = self._cfg_get
        self._values = {
            "tg_token":      g("telegram", "bot_token"),
            "tg_chat_id":    str(g("telegram", "chat_id") or ""),
            "gemini_key":    g("gemini", "api_key"),
            "default_cal":   g("agent", "default_calendar") or "",
            "briefing_cals": list(
                (self.cfg.get("agent") or {}).get("briefing_calendars") or []),
            "canvas_url":    g("canvas", "ical_url"),
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

    def _set_busy(self, busy: bool, status=None, msg: str = "", color: str = "#555"):
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
            status.config(text=msg, foreground=color)

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
        self.google_calendars = []
        self._tg_username = ""
        self._seed_values()          # cfg is empty now, so the model comes up blank
        self.step_idx = 0
        self._show_step()

    # ── Small helpers ─────────────────────────────────────────────────────────

    def _heading(self, title: str, sub: str = ""):
        ttk.Label(self.body, text=title, font=("Segoe UI", 16, "bold")).pack(
            anchor="w", pady=(0, 4))
        if sub:
            ttk.Label(self.body, text=sub, wraplength=560,
                      foreground="#555").pack(anchor="w", pady=(0, 12))

    def _entry_row(self, label: str, key: str, show: str = "") -> tk.StringVar:
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=18).pack(side="left")
        var = tk.StringVar(value=self._values.get(key, ""))
        ttk.Entry(row, textvariable=var, show=show, width=52).pack(
            side="left", fill="x", expand=True)
        self._step_vars[key] = var
        return var

    def _link(self, text: str, url: str):
        lbl = ttk.Label(self.body, text=text, foreground="#0b5fff",
                        cursor="hand2")
        lbl.pack(anchor="w", pady=2)
        lbl.bind("<Button-1>", lambda e: webbrowser.open(url))

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
        self._heading(
            "Welcome to Friday",
            "Friday is a personal AI secretary. She watches your Canvas "
            "assignments, GroupMe chats, weather, and Google Calendar — and "
            "talks to you through Telegram with morning/evening briefings and "
            "urgent alerts.",
        )
        ttk.Label(self.body, wraplength=560, text=(
            "This wizard takes about 5 minutes. You'll need:\n\n"
            "  •  The Telegram app on your phone\n"
            "  •  A Google account (for your calendar)\n"
            "  •  A free Gemini API key (we'll show you where to get one)\n\n"
            "Optional: your Canvas calendar link and a free weather API key."
        )).pack(anchor="w")

    def _step_telegram(self):
        self._heading(
            "Step 1 — Telegram bot",
            "Friday talks to you through a private Telegram bot that you own.",
        )
        ttk.Label(self.body, wraplength=560, text=(
            "1. In Telegram, open @BotFather and send:  /newbot\n"
            "2. Give it a name (e.g. \"Friday\") and a unique username\n"
            "3. Copy the token BotFather gives you and paste it below"
        )).pack(anchor="w", pady=(0, 8))
        self._link("Open BotFather in Telegram →", "https://t.me/BotFather")

        self.tg_token = self._entry_row("Bot token", "tg_token")
        self.tg_chat_id = self._entry_row("Chat ID", "tg_chat_id")

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
                    status.config(text=_NET_MSG, foreground="red")
                elif kind == "auth":
                    status.config(text=f"Telegram rejected that token "
                                        f"({_mask_token(token)}).", foreground="red")
                elif info:
                    self.tg_chat_id.set(sorted(info)[-1])
                    on_ready()
                else:
                    uname = self._tg_username or "your bot"
                    status.config(
                        text=(f"Bot @{uname} works, but you haven't messaged it "
                              f"yet. Open Telegram, press Start on the bot, send "
                              f"it any message, then click Next again."),
                        foreground="#b07000")
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
                    status.config(text=_NET_MSG, foreground="red")
                else:
                    status.config(
                        text=(f"Telegram rejected that token ({_mask_token(token)}). "
                              f"Double-check what BotFather gave you."),
                        foreground="red")
                    logger.warning(f"Telegram token rejected ({_mask_token(token)})")

            def worker():
                res = _telegram_getme(token)
                self.after(0, lambda: done(res))
            threading.Thread(target=worker, daemon=True).start()

        def detect():
            token = self.tg_token.get().strip()
            if not token:
                status.config(text="Paste the bot token first.", foreground="red")
                return
            if not _token_format_ok(token):
                status.config(text="That doesn't look like a bot token "
                                   "(it should look like 123456789:AA…).",
                              foreground="red")
                return

            def report_chat():
                if self.tg_chat_id.get().strip():
                    status.config(
                        text=f"✓ Bot @{self._tg_username} verified — chat ID "
                             f"detected.", foreground="green")
            # Verify, then look for a chat id — but Detect never advances.
            verify_token(token, lambda: resolve_chat(token, report_chat))

        ttk.Button(self.body, text="Detect my chat ID", command=detect).pack(
            anchor="w", pady=4)

        def ok():
            token = self.tg_token.get().strip()
            if not token:
                status.config(text="Paste the bot token from BotFather first.",
                              foreground="red")
                return False
            if not _token_format_ok(token):
                status.config(
                    text=("That doesn't look like a bot token — it should look "
                          "like 123456789:AA… . Check for a copy/paste slip."),
                    foreground="red")
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
        ttk.Label(self.body, wraplength=560, text=(
            "1. Open Google AI Studio (link below) and sign in\n"
            "2. Click \"Create API key\"\n"
            "3. Paste the key here"
        )).pack(anchor="w", pady=(0, 8))
        self._link("Open Google AI Studio →", "https://aistudio.google.com/apikey")

        self.gemini_key = self._entry_row("API key", "gemini_key")
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
                                  foreground="red")
                    return False
            except Exception as e:
                status.config(text=f"Network error: {_scrub(str(e), key)}",
                              foreground="red")
                return False
            status.config(text="✓ Key verified.", foreground="green")
            return True
        self._validate = validate_key

    def _step_google(self):
        self._heading(
            "Step 3 — Connect Google Calendar",
            "Google Calendar is where Friday keeps your events. She reads "
            "your schedule from it and adds events you ask for.",
        )
        status = self._status_label()

        secret = paths.google_client_secret_path()
        if not secret.exists():
            ttk.Label(self.body, wraplength=560, foreground="#b07000", text=(
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
                                  foreground="green")
            ttk.Button(self.body, text="Select credentials file…",
                       command=pick).pack(anchor="w", pady=4)

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

        connect_btn = ttk.Button(self.body, text="Connect Google Calendar")
        connect_btn.pack(anchor="w", pady=8)

        cal_frame = ttk.Frame(self.body)
        cal_frame.pack(fill="both", expand=True)

        def show_calendars(names: list[str]):
            self.google_calendars = names
            for w in cal_frame.winfo_children():
                w.destroy()
            ttk.Label(cal_frame, text="Default calendar (new events go here):"
                      ).pack(anchor="w", pady=(8, 2))
            combo = ttk.Combobox(cal_frame, textvariable=self.default_cal,
                                 values=names, state="readonly", width=40)
            combo.pack(anchor="w")
            if names and self.default_cal.get() not in names:
                combo.set(names[0])
            ttk.Label(cal_frame, text="Calendars Friday includes in briefings "
                                      "(Ctrl-click for several):"
                      ).pack(anchor="w", pady=(10, 2))
            lb = tk.Listbox(cal_frame, selectmode="multiple", height=6,
                            exportselection=False)
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

        def connect():
            if not paths.google_client_secret_path().exists():
                status.config(text="Select the credentials file first.",
                              foreground="red")
                return
            connect_btn.state(["disabled"])
            status.config(text="A browser window is opening — sign in and "
                               "allow calendar access…", foreground="#555")

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
                                      foreground="green"),
                        show_calendars(names),
                        connect_btn.state(["!disabled"]),
                    ))
                except Exception as e:
                    logger.error(f"Google OAuth failed: {e}")
                    self.after(0, lambda e=e: (
                        status.config(text=f"Connection failed: {e}",
                                      foreground="red"),
                        connect_btn.state(["!disabled"]),
                    ))
            threading.Thread(target=worker, daemon=True).start()

        connect_btn.config(command=connect)
        if self.google_calendars:  # returning to this step after connecting
            show_calendars(self.google_calendars)

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
        ttk.Label(self.body, wraplength=560, text=(
            "In Canvas (web): Calendar → \"Calendar Feed\" (bottom right) → "
            "copy the link. It ends in .ics. Leave blank to skip."
        )).pack(anchor="w", pady=(0, 8))
        self.canvas_url = self._entry_row("Calendar feed URL", "canvas_url")

        def ok():
            url = self.canvas_url.get().strip()
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
        self._link("Get a free OpenWeatherMap key →",
                   "https://home.openweathermap.org/api_keys")
        self.weather_key = self._entry_row("API key", "weather_key")
        self.weather_loc = self._entry_row("Location (City,US)", "weather_loc")

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
        ttk.Combobox(row, textvariable=self.tz_var, values=values,
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
            "Click Finish to save. Friday will start in your system tray "
            "and send you a hello on Telegram.",
        )
        ttk.Label(self.body, wraplength=560, text=(
            "Things to try once she's online:\n\n"
            "  •  Send \"brief me\" on Telegram\n"
            "  •  \"Add dentist appointment Friday at 2pm\"\n"
            "  •  \"What's my week look like?\"\n\n"
            "Right-click the tray icon for Brief Me Now, Pause, the "
            "dashboard, or to re-run this wizard."
        )).pack(anchor="w")

    # ── Config write ──────────────────────────────────────────────────────────

    def _write_config(self):
        cfg = self.cfg or {}
        v = self._values
        briefing_cals = v.get("briefing_cals") or list(self.google_calendars)

        agent = cfg.setdefault("agent", {})
        agent.setdefault("name", "Friday")
        agent["timezone"] = v.get("tz", "").strip() or "America/Chicago"
        agent["morning_briefing_time"] = v.get("morning", "").strip()
        agent["briefing_time"] = v.get("evening", "").strip()
        if v.get("default_cal"):
            agent["default_calendar"] = v["default_cal"]
        if briefing_cals:
            agent["briefing_calendars"] = briefing_cals

        cfg["calendar"] = {"backend": "google"}
        cfg["provider"] = "gemini"
        cfg["telegram"] = {
            "bot_token": v.get("tg_token", "").strip(),
            "chat_id":   v.get("tg_chat_id", "").strip(),
        }
        gemini = cfg.setdefault("gemini", {})
        gemini["api_key"] = v.get("gemini_key", "").strip()
        gemini.setdefault("model", _GEMINI_DEFAULT_MODEL)
        gemini.setdefault("max_tokens", 4000)
        cfg.setdefault("canvas", {})["ical_url"] = v.get("canvas_url", "").strip()
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
