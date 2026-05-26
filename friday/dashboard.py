"""
dashboard.py
Friday Mission Control — standalone Tkinter dashboard.

Run independently of the background daemon:
    cd friday && python3 dashboard.py

Reads state.json every 5 seconds for live status.
Config edits are saved back to friday_config.yaml on "Save Config".
No extra dependencies — uses stdlib tkinter and the existing pyyaml install.
"""

import json
import os
import tkinter as tk
from tkinter import ttk, messagebox
import yaml
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "state.json")
CONFIG_FILE = os.path.join(_HERE, "friday_config.yaml")

REFRESH_MS = 5000  # re-read state.json every 5 s

# ── Colour palette (Catppuccin Mocha) ─────────────────────────────────────────
BG        = "#1e1e2e"
SURFACE   = "#313244"
TEXT      = "#cdd6f4"
SUBTEXT   = "#a6adc8"
BLUE      = "#89b4fa"
GREEN     = "#a6e3a1"
RED       = "#f38ba8"
YELLOW    = "#f9e2af"


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Friday Mission Control")
        self.configure(bg=BG)
        self.resizable(True, False)
        self.minsize(520, 0)

        self._build_status_section()
        self._build_config_section()
        self._build_buttons()

        self._load_config_into_form()
        self._refresh_status()   # starts the 5-second polling loop

    # ── Status section ────────────────────────────────────────────────────────

    def _build_status_section(self):
        frame = tk.LabelFrame(
            self, text="  System Status  ",
            bg=BG, fg=BLUE, font=("Helvetica", 11, "bold"),
            bd=1, relief="groove", padx=8, pady=6,
        )
        frame.pack(fill="x", padx=12, pady=(12, 4))

        # Status dot + line
        top = tk.Frame(frame, bg=BG)
        top.pack(fill="x", pady=(0, 4))
        self._dot = tk.Label(top, text="●", font=("Helvetica", 16), bg=BG, fg=SUBTEXT)
        self._dot.pack(side="left")
        self._status_label = tk.Label(top, text="Checking…", bg=BG,
                                      fg=SUBTEXT, font=("Helvetica", 11, "bold"))
        self._status_label.pack(side="left", padx=6)

        # Key-value ticker rows
        self._tickers: dict[str, tk.Label] = {}
        rows = [
            ("provider",     "Provider / Model"),
            ("last_poll",    "Last Poll"),
            ("last_message", "Last Message"),
            ("think_calls",  "Think Calls"),
            ("tokens",       "Tokens  (in / out)"),
        ]
        grid = tk.Frame(frame, bg=BG)
        grid.pack(fill="x")
        for i, (key, label) in enumerate(rows):
            tk.Label(grid, text=f"{label}:", bg=BG, fg=SUBTEXT,
                     font=("Helvetica", 10), anchor="e", width=18).grid(
                row=i, column=0, sticky="e", pady=1, padx=(0, 6))
            lbl = tk.Label(grid, text="—", bg=BG, fg=TEXT,
                           font=("Helvetica", 10), anchor="w")
            lbl.grid(row=i, column=1, sticky="w", pady=1)
            self._tickers[key] = lbl

    def _refresh_status(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self._apply_state(state)
            except Exception:
                self._dot.config(fg=RED)
                self._status_label.config(text="state.json unreadable", fg=RED)
        else:
            self._dot.config(fg=SUBTEXT)
            self._status_label.config(
                text="Daemon not running  (start friday.py first)", fg=SUBTEXT)
            for lbl in self._tickers.values():
                lbl.config(text="—")

        self.after(REFRESH_MS, self._refresh_status)

    def _apply_state(self, s: dict):
        self._dot.config(fg=GREEN)
        pid = s.get("pid", "?")
        started = _fmt_time(s.get("started_at"))
        self._status_label.config(
            text=f"Running  ·  PID {pid}  ·  Started {started}", fg=GREEN)

        self._tickers["provider"].config(
            text=f"{s.get('provider', '?')}  ·  {s.get('model', '?')}")
        self._tickers["last_poll"].config(text=_fmt_time(s.get("last_poll_at")))

        msg_time = _fmt_time(s.get("last_message_at"))
        src = s.get("last_message_source") or ""
        preview = s.get("last_message_preview") or ""
        if preview:
            short = (preview[:52] + "…") if len(preview) > 53 else preview
            self._tickers["last_message"].config(
                text=f'{msg_time}  [{src}]  “{short}”')
        else:
            self._tickers["last_message"].config(text="—")

        self._tickers["think_calls"].config(
            text=str(s.get("think_calls_total", 0)))

        tok_in  = s.get("tokens_in_total",  0)
        tok_out = s.get("tokens_out_total", 0)
        self._tickers["tokens"].config(text=f"{tok_in:,}  /  {tok_out:,}")

    # ── Config section ────────────────────────────────────────────────────────

    def _build_config_section(self):
        frame = tk.LabelFrame(
            self, text="  Configuration  ",
            bg=BG, fg=BLUE, font=("Helvetica", 11, "bold"),
            bd=1, relief="groove", padx=8, pady=6,
        )
        frame.pack(fill="x", padx=12, pady=4)
        frame.columnconfigure(1, weight=1)

        lbl_kw = dict(bg=BG, fg=SUBTEXT, font=("Helvetica", 10), anchor="e", width=18)
        ent_kw = dict(bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                      relief="flat", font=("Helvetica", 10))

        def label(r, text):
            tk.Label(frame, text=text, **lbl_kw).grid(
                row=r, column=0, sticky="e", pady=3, padx=(0, 6))

        def entry(r, **kw):
            e = tk.Entry(frame, **ent_kw, **kw)
            e.grid(row=r, column=1, sticky="ew", pady=3)
            return e

        # Provider dropdown
        label(0, "Provider:")
        self._provider_var = tk.StringVar(value="gemini")
        ttk.Combobox(
            frame, textvariable=self._provider_var,
            values=["gemini", "ollama"], state="readonly",
            font=("Helvetica", 10), width=14,
        ).grid(row=0, column=1, sticky="w", pady=3)

        label(1, "Model:")
        self._model_entry = entry(1, width=44)

        # API key with show/hide toggle
        label(2, "API Key:")
        key_frame = tk.Frame(frame, bg=BG)
        key_frame.grid(row=2, column=1, sticky="ew", pady=3)
        key_frame.columnconfigure(0, weight=1)
        self._api_key_entry = tk.Entry(key_frame, **ent_kw, show="*")
        self._api_key_entry.grid(row=0, column=0, sticky="ew")
        self._show_key = False
        tk.Button(
            key_frame, text="show", bg=SURFACE, fg=SUBTEXT,
            relief="flat", font=("Helvetica", 9), cursor="hand2",
            command=self._toggle_key,
        ).grid(row=0, column=1, padx=(4, 0))

        label(3, "Poll interval (s):")
        self._poll_entry = entry(3, width=8)

        label(4, "Briefing time:")
        self._briefing_entry = entry(4, width=8)

        # GroupMe groups — use sticky="ne" so multi-line label aligns to top
        tk.Label(frame, text="GroupMe groups\n(one per line):", **lbl_kw).grid(
            row=5, column=0, sticky="ne", pady=3, padx=(0, 6))
        self._groups_text = tk.Text(
            frame, height=5, **ent_kw, width=1)
        self._groups_text.grid(row=5, column=1, sticky="ew", pady=3)

        # iMessage contacts
        tk.Label(frame, text="iMessage contacts\n(one per line):", **lbl_kw).grid(
            row=6, column=0, sticky="ne", pady=3, padx=(0, 6))
        self._contacts_text = tk.Text(
            frame, height=4, **ent_kw, width=1)
        self._contacts_text.grid(row=6, column=1, sticky="ew", pady=3)

    def _toggle_key(self):
        self._show_key = not self._show_key
        self._api_key_entry.config(show="" if self._show_key else "*")

    # ── Buttons ───────────────────────────────────────────────────────────────

    def _build_buttons(self):
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="x", padx=12, pady=(4, 14))
        btn = dict(relief="flat", cursor="hand2", font=("Helvetica", 11, "bold"),
                   padx=14, pady=6)
        tk.Button(frame, text="Save Config", bg=BLUE, fg=BG,
                  command=self._save_config, **btn).pack(side="left")
        tk.Button(frame, text="Reload Form", bg=SURFACE, fg=TEXT,
                  command=self._load_config_into_form, **btn).pack(side="left", padx=(8, 0))
        tk.Button(frame, text="Refresh Status", bg=SURFACE, fg=TEXT,
                  command=self._refresh_status, **btn).pack(side="right")

    # ── Config I/O ────────────────────────────────────────────────────────────

    def _load_config_into_form(self):
        cfg = self._read_config()
        if cfg is None:
            return

        provider = cfg.get("provider", "gemini")
        self._provider_var.set(provider)

        provider_cfg = cfg.get(provider, {})
        _set_entry(self._model_entry,    provider_cfg.get("model", ""))
        _set_entry(self._api_key_entry,  provider_cfg.get("api_key", ""))

        agent_cfg = cfg.get("agent", {})
        _set_entry(self._poll_entry,     str(agent_cfg.get("poll_interval_seconds", 300)))
        _set_entry(self._briefing_entry, agent_cfg.get("briefing_time", "21:00"))

        groups = cfg.get("groupme", {}).get("approved_groups", [])
        _set_text(self._groups_text, "\n".join(groups))

        contacts = cfg.get("imessage", {}).get("approved_contacts", [])
        _set_text(self._contacts_text, "\n".join(contacts))

    def _save_config(self):
        cfg = self._read_config()
        if cfg is None:
            return

        provider = self._provider_var.get()
        cfg["provider"] = provider

        cfg.setdefault(provider, {})
        cfg[provider]["model"] = self._model_entry.get().strip()
        api_key = self._api_key_entry.get().strip()
        if api_key:
            cfg[provider]["api_key"] = api_key
        elif "api_key" in cfg.get(provider, {}):
            del cfg[provider]["api_key"]

        cfg.setdefault("agent", {})
        try:
            cfg["agent"]["poll_interval_seconds"] = int(self._poll_entry.get().strip())
        except ValueError:
            pass
        cfg["agent"]["briefing_time"] = self._briefing_entry.get().strip()

        raw_groups = self._groups_text.get("1.0", "end").strip()
        cfg.setdefault("groupme", {})["approved_groups"] = (
            [g.strip() for g in raw_groups.splitlines() if g.strip()]
        )

        raw_contacts = self._contacts_text.get("1.0", "end").strip()
        cfg.setdefault("imessage", {})["approved_contacts"] = (
            [c.strip() for c in raw_contacts.splitlines() if c.strip()]
        )

        try:
            with open(CONFIG_FILE, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            messagebox.showinfo(
                "Saved", "Config saved.\nRestart friday.py to apply changes.")
        except Exception as e:
            messagebox.showerror("Save error", str(e))

    def _read_config(self) -> dict | None:
        if not os.path.exists(CONFIG_FILE):
            messagebox.showerror("Error", f"Config not found:\n{CONFIG_FILE}")
            return None
        try:
            with open(CONFIG_FILE) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            messagebox.showerror("Config read error", str(e))
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%b %d  %I:%M:%S %p")
    except Exception:
        return str(iso)


def _set_entry(widget: tk.Entry, value: str):
    widget.delete(0, "end")
    widget.insert(0, value)


def _set_text(widget: tk.Text, value: str):
    widget.delete("1.0", "end")
    widget.insert("1.0", value)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
