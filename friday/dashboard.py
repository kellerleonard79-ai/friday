"""
dashboard.py
Friday Mission Control — standalone Tkinter status dashboard.

Run independently:  python3 dashboard.py
"""

import json
import os
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "state.json")
CONFIG_FILE = os.path.join(_HERE, "friday_config.yaml")
REFRESH_MS = 5000


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Friday — Mission Control")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")
        self._build_ui()
        self._refresh()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        pad = dict(padx=12, pady=6)

        # ── Status panel ──────────────────────────────────────────────────────
        status_frame = tk.LabelFrame(self, text="Status", bg="#1e1e2e", fg="#cdd6f4",
                                     font=("Menlo", 11, "bold"))
        status_frame.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)

        self._vars = {}
        rows = [
            ("status",       "Status"),
            ("pid",          "PID"),
            ("provider",     "Provider"),
            ("model",        "Model"),
            ("started_at",   "Started"),
            ("last_poll_at", "Last state write"),
        ]
        for i, (key, label) in enumerate(rows):
            tk.Label(status_frame, text=f"{label}:", bg="#1e1e2e", fg="#a6adc8",
                     font=("Menlo", 10), anchor="e", width=18).grid(row=i, column=0, sticky="e")
            var = tk.StringVar(value="—")
            self._vars[key] = var
            tk.Label(status_frame, textvariable=var, bg="#1e1e2e", fg="#cdd6f4",
                     font=("Menlo", 10), anchor="w").grid(row=i, column=1, sticky="w")

        # ── Stats panel ───────────────────────────────────────────────────────
        stats_frame = tk.LabelFrame(self, text="Stats", bg="#1e1e2e", fg="#cdd6f4",
                                    font=("Menlo", 11, "bold"))
        stats_frame.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)

        stat_rows = [
            ("think_calls",          "LLM calls"),
            ("tokens_in",            "Tokens in"),
            ("tokens_out",           "Tokens out"),
            ("last_message_at",      "Last message"),
            ("last_message_preview", "Preview"),
        ]
        for i, (key, label) in enumerate(stat_rows):
            tk.Label(stats_frame, text=f"{label}:", bg="#1e1e2e", fg="#a6adc8",
                     font=("Menlo", 10), anchor="e", width=18).grid(row=i, column=0, sticky="e")
            var = tk.StringVar(value="—")
            self._vars[key] = var
            tk.Label(stats_frame, textvariable=var, bg="#1e1e2e", fg="#cdd6f4",
                     font=("Menlo", 10), anchor="w", wraplength=380).grid(row=i, column=1, sticky="w")

        # ── Config editor ─────────────────────────────────────────────────────
        cfg_frame = tk.LabelFrame(self, text="Config", bg="#1e1e2e", fg="#cdd6f4",
                                  font=("Menlo", 11, "bold"))
        cfg_frame.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        self._cfg_entries = {}
        cfg_fields = [
            ("telegram.bot_token", "Bot token"),
            ("telegram.chat_id",   "Chat ID"),
            ("provider",           "Provider (ollama/gemini)"),
            ("ollama.model",       "Ollama model"),
            ("gemini.model",       "Gemini model"),
            ("gemini.api_key",     "Gemini API key"),
            ("agent.briefing_time","Briefing time (HH:MM)"),
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

    # ── State refresh ─────────────────────────────────────────────────────────

    def _refresh(self):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)

            status = state.get("status", "unknown")
            color = "#a6e3a1" if status == "running" else "#f38ba8"
            self._vars["status"].set(status.upper())

            self._vars["pid"].set(str(state.get("pid", "—")))
            self._vars["provider"].set(state.get("provider", "—"))
            self._vars["model"].set(state.get("model", "—"))
            self._vars["started_at"].set(self._fmt_time(state.get("started_at")))
            self._vars["last_poll_at"].set(self._fmt_time(state.get("last_poll_at")))
            self._vars["think_calls"].set(str(state.get("think_calls", 0)))
            self._vars["tokens_in"].set(str(state.get("tokens_in", 0)))
            self._vars["tokens_out"].set(str(state.get("tokens_out", 0)))
            self._vars["last_message_at"].set(self._fmt_time(state.get("last_message_at")))
            preview = state.get("last_message_preview") or "—"
            self._vars["last_message_preview"].set(preview[:60])

        except (FileNotFoundError, json.JSONDecodeError):
            self._vars["status"].set("STOPPED")

        self.after(REFRESH_MS, self._refresh)

    @staticmethod
    def _fmt_time(iso: str) -> str:
        if not iso:
            return "—"
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%H:%M:%S")
        except ValueError:
            return iso

    # ── Config I/O ────────────────────────────────────────────────────────────

    def _load_config_into_form(self):
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return

        def _get(path: str):
            keys = path.split(".")
            node = cfg
            for k in keys:
                if not isinstance(node, dict):
                    return ""
                node = node.get(k, "")
            return str(node) if node is not None else ""

        for key, entry in self._cfg_entries.items():
            entry.delete(0, tk.END)
            entry.insert(0, _get(key))

    def _save_config(self):
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
        except FileNotFoundError:
            cfg = {}

        def _set(path: str, value: str):
            keys = path.split(".")
            node = cfg
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = value

        for key, entry in self._cfg_entries.items():
            val = entry.get().strip()
            if val:
                _set(key, val)

        with open(CONFIG_FILE, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

        messagebox.showinfo("Saved", "Config saved. Restart Friday to apply changes.")


if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
