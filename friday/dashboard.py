"""
dashboard.py
Friday Mission Control — standalone Tkinter status dashboard.
Reads live data from SQLite system_state table.

Run independently:  python3 dashboard.py
"""

import os
import sqlite3
import subprocess
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

import yaml

_HERE    = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_HERE, "memory", "friday_memory.db")
_CFG     = os.path.join(_HERE, "friday_config.yaml")
REFRESH_MS = 5000

_STATE_KEYS = [
    ("status",               "Status"),
    ("provider",             "Provider"),
    ("model",                "Model"),
    ("started_at",           "Started"),
    ("think_calls",          "LLM calls"),
    ("tokens_in",            "Tokens in"),
    ("tokens_out",           "Tokens out"),
    ("last_message_at",      "Last message"),
    ("last_message_preview", "Preview"),
]


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Friday — Mission Control")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")
        self._conn = self._open_db()
        self._build_ui()
        self._refresh()

    def _open_db(self) -> sqlite3.Connection | None:
        if os.path.exists(_DB_PATH):
            return sqlite3.connect(_DB_PATH, check_same_thread=False)
        return None

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        pad = dict(padx=12, pady=6)

        status_frame = tk.LabelFrame(self, text="Status", bg="#1e1e2e", fg="#cdd6f4",
                                     font=("Menlo", 11, "bold"))
        status_frame.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        self._vars = {}
        for i, (key, label) in enumerate(_STATE_KEYS):
            tk.Label(status_frame, text=f"{label}:", bg="#1e1e2e", fg="#a6adc8",
                     font=("Menlo", 10), anchor="e", width=18).grid(row=i, column=0, sticky="e")
            var = tk.StringVar(value="—")
            self._vars[key] = var
            tk.Label(status_frame, textvariable=var, bg="#1e1e2e", fg="#cdd6f4",
                     font=("Menlo", 10), anchor="w", wraplength=380).grid(row=i, column=1, sticky="w")

        cfg_frame = tk.LabelFrame(self, text="Config", bg="#1e1e2e", fg="#cdd6f4",
                                  font=("Menlo", 11, "bold"))
        cfg_frame.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)

        self._cfg_entries = {}
        cfg_fields = [
            ("telegram.bot_token",  "Bot token"),
            ("telegram.chat_id",    "Chat ID"),
            ("provider",            "Provider (ollama/gemini)"),
            ("ollama.model",        "Ollama model"),
            ("gemini.model",        "Gemini model"),
            ("gemini.api_key",      "Gemini API key"),
            ("agent.morning_briefing_time", "Morning briefing (HH:MM)"),
            ("agent.briefing_time",         "Evening briefing (HH:MM)"),
        ]
        for i, (key, label) in enumerate(cfg_fields):
            tk.Label(cfg_frame, text=f"{label}:", bg="#1e1e2e", fg="#a6adc8",
                     font=("Menlo", 10), anchor="e", width=24).grid(row=i, column=0, sticky="e")
            entry = tk.Entry(cfg_frame, font=("Menlo", 10), width=38,
                             bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                             relief="flat")
            entry.grid(row=i, column=1, sticky="w", padx=4, pady=2)
            self._cfg_entries[key] = entry

        tk.Button(cfg_frame, text="Save Config", command=self._save_config,
                  bg="#89b4fa", fg="#1e1e2e", font=("Menlo", 10, "bold"),
                  relief="flat", padx=10).grid(
            row=len(cfg_fields), column=0, columnspan=2, pady=8
        )
        self._load_config_into_form()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self):
        if self._conn is None:
            self._conn = self._open_db()

        if self._conn:
            for key, _ in _STATE_KEYS:
                row = self._conn.execute(
                    "SELECT value FROM system_state WHERE key = ?", (key,)
                ).fetchone()
                val = row[0] if row else "—"
                if key in ("started_at", "last_message_at"):
                    val = self._fmt_time(val)
                elif key == "last_message_preview" and val:
                    val = val[:60]
                self._vars[key].set(val or "—")
        else:
            for key, _ in _STATE_KEYS:
                self._vars[key].set("—")
            self._vars["status"].set("STOPPED")

        self.after(REFRESH_MS, self._refresh)

    @staticmethod
    def _fmt_time(iso: str) -> str:
        if not iso or iso == "—":
            return "—"
        try:
            return datetime.fromisoformat(iso).strftime("%H:%M:%S")
        except ValueError:
            return iso

    # ── Config I/O ────────────────────────────────────────────────────────────

    def _load_config_into_form(self):
        try:
            with open(_CFG) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return

        def _get(path):
            node = cfg
            for k in path.split("."):
                if not isinstance(node, dict):
                    return ""
                node = node.get(k, "")
            return str(node) if node is not None else ""

        for key, entry in self._cfg_entries.items():
            entry.delete(0, tk.END)
            entry.insert(0, _get(key))

    def _save_config(self):
        try:
            with open(_CFG) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            cfg = {}

        def _set(path, value):
            node = cfg
            keys = path.split(".")
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = value

        for key, entry in self._cfg_entries.items():
            val = entry.get().strip()
            if val:
                _set(key, val)

        with open(_CFG, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

        restart_sh = os.path.join(_HERE, "restart.sh")
        subprocess.Popen(["bash", restart_sh], start_new_session=True)
        messagebox.showinfo("Saved", "Config saved. Friday is restarting…")


if __name__ == "__main__":
    Dashboard().mainloop()
