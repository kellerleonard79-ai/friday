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
        self.briefing_cals: list[str] = list(
            (self.cfg.get("agent") or {}).get("briefing_calendars") or [])

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)
        self.body = ttk.Frame(container)
        self.body.pack(fill="both", expand=True)

        nav = ttk.Frame(container)
        nav.pack(fill="x", pady=(12, 0))
        self.back_btn = ttk.Button(nav, text="← Back", command=self._back)
        self.back_btn.pack(side="left")
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
        # Per-step "may we advance?" validators, set by each step builder.
        self._validate = lambda: True
        self._show_step()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _show_step(self):
        for w in self.body.winfo_children():
            w.destroy()
        self._validate = lambda: True
        self.steps[self.step_idx]()
        self.back_btn.state(["!disabled"] if self.step_idx > 0 else ["disabled"])
        self.next_btn.config(
            text="Finish" if self.step_idx == len(self.steps) - 1 else "Next →"
        )

    def _next(self):
        if not self._validate():
            return
        if self.step_idx == len(self.steps) - 1:
            self._write_config()
            return
        self.step_idx += 1
        self._show_step()

    def _back(self):
        if self.step_idx > 0:
            self.step_idx -= 1
            self._show_step()

    # ── Small helpers ─────────────────────────────────────────────────────────

    def _heading(self, title: str, sub: str = ""):
        ttk.Label(self.body, text=title, font=("Segoe UI", 16, "bold")).pack(
            anchor="w", pady=(0, 4))
        if sub:
            ttk.Label(self.body, text=sub, wraplength=560,
                      foreground="#555").pack(anchor="w", pady=(0, 12))

    def _entry_row(self, label: str, initial: str = "", show: str = "") -> tk.StringVar:
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=18).pack(side="left")
        var = tk.StringVar(value=initial)
        ttk.Entry(row, textvariable=var, show=show, width=52).pack(
            side="left", fill="x", expand=True)
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

    def _keep(self, attr: str, fallback: str) -> str:
        """Initial value for a re-shown step's entry: whatever the user
        already typed this session wins over the on-disk config."""
        var = getattr(self, attr, None)
        if isinstance(var, tk.StringVar):
            return var.get()
        return fallback

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

        self.tg_token = self._entry_row(
            "Bot token", self._keep("tg_token", self._cfg_get("telegram", "bot_token")))
        self.tg_chat_id = self._entry_row(
            "Chat ID", self._keep("tg_chat_id", str(self._cfg_get("telegram", "chat_id"))))

        status = self._status_label()

        def detect():
            token = self.tg_token.get().strip()
            if not token:
                status.config(text="Paste the bot token first.", foreground="red")
                return
            try:
                me = requests.get(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=10
                ).json()
                if not me.get("ok"):
                    status.config(text=f"Token rejected: {me.get('description')}",
                                  foreground="red")
                    return
                username = me["result"]["username"]
                upd = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates", timeout=10
                ).json()
                chats = {
                    str(u["message"]["chat"]["id"])
                    for u in upd.get("result", []) if "message" in u
                }
                if chats:
                    self.tg_chat_id.set(sorted(chats)[-1])
                    status.config(
                        text=f"✓ Bot @{username} verified — chat ID detected.",
                        foreground="green")
                else:
                    status.config(
                        text=(f"Bot @{username} is valid, but no messages yet. "
                              f"Open t.me/{username}, press Start, send any "
                              f"message, then click Detect again."),
                        foreground="#b07000")
                    webbrowser.open(f"https://t.me/{username}")
            except Exception as e:
                status.config(text=f"Network error: {e}", foreground="red")

        ttk.Button(self.body, text="Detect my chat ID", command=detect).pack(
            anchor="w", pady=4)

        def ok():
            if not self.tg_token.get().strip() or not self.tg_chat_id.get().strip():
                messagebox.showwarning(
                    "Telegram required",
                    "Friday can't run without the bot token and chat ID. "
                    "Use the Detect button after messaging your bot.")
                return False
            return True
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

        self.gemini_key = self._entry_row(
            "API key", self._keep("gemini_key", self._cfg_get("gemini", "api_key")))
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
                status.config(text=f"Network error: {e}", foreground="red")
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
        self.default_cal = tk.StringVar(value=self._keep(
            "default_cal", self._cfg_get("agent", "default_calendar") or ""))
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
            # Snapshot the listbox now — the widget is destroyed when the
            # next step renders, so _write_config can't read it later.
            if self.cal_listbox is not None:
                self.briefing_cals = [self.cal_listbox.get(i)
                                      for i in self.cal_listbox.curselection()]
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
        self.canvas_url = self._entry_row(
            "Calendar feed URL",
            self._keep("canvas_url", self._cfg_get("canvas", "ical_url")))

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
        self.weather_key = self._entry_row(
            "API key", self._keep("weather_key", self._cfg_get("weather", "api_key")))
        self.weather_loc = self._entry_row(
            "Location (City,US)",
            self._keep("weather_loc", self._cfg_get("weather", "location")))

    def _step_schedule(self):
        self._heading(
            "Step 6 — Your schedule",
            "When should Friday brief you, and what timezone are you in?",
        )
        tz_guess = self._cfg_get("agent", "timezone") or _guess_timezone()
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Timezone", width=18).pack(side="left")
        self.tz_var = tk.StringVar(value=tz_guess)
        values = list(_COMMON_TIMEZONES)
        if tz_guess not in values:
            values.insert(0, tz_guess)
        ttk.Combobox(row, textvariable=self.tz_var, values=values,
                     width=30).pack(side="left")

        self.morning_var = self._entry_row(
            "Morning briefing", self._cfg_get("agent", "morning_briefing_time") or "07:00")
        self.evening_var = self._entry_row(
            "Evening briefing", self._cfg_get("agent", "briefing_time") or "20:00")

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
        briefing_cals = self.briefing_cals or list(self.google_calendars)

        agent = cfg.setdefault("agent", {})
        agent.setdefault("name", "Friday")
        agent["timezone"] = self.tz_var.get().strip() or "America/Chicago"
        agent["morning_briefing_time"] = self.morning_var.get().strip()
        agent["briefing_time"] = self.evening_var.get().strip()
        if self.default_cal.get():
            agent["default_calendar"] = self.default_cal.get()
        if briefing_cals:
            agent["briefing_calendars"] = briefing_cals

        cfg["calendar"] = {"backend": "google"}
        cfg["provider"] = "gemini"
        cfg["telegram"] = {
            "bot_token": self.tg_token.get().strip(),
            "chat_id":   self.tg_chat_id.get().strip(),
        }
        gemini = cfg.setdefault("gemini", {})
        gemini["api_key"] = self.gemini_key.get().strip()
        gemini.setdefault("model", _GEMINI_DEFAULT_MODEL)
        gemini.setdefault("max_tokens", 4000)
        cfg.setdefault("canvas", {})["ical_url"] = self.canvas_url.get().strip()
        cfg.setdefault("weather", {})
        cfg["weather"]["api_key"]  = self.weather_key.get().strip()
        cfg["weather"]["location"] = self.weather_loc.get().strip()
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
