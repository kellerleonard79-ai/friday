"""
menubar.py
Friday menu bar app — standalone rumps app, never imported by friday.py.
Reads friday_config.yaml directly. Communicates with Friday via Telegram API.
"""

import os
import subprocess
import sys

import requests
import rumps
import yaml

_HERE      = os.path.dirname(os.path.abspath(__file__))
_CONFIG    = os.path.join(_HERE, "friday_config.yaml")
_DASHBOARD = os.path.join(_HERE, "dashboard.py")
_PYTHON    = sys.executable


def _load_config() -> dict:
    with open(_CONFIG) as f:
        return yaml.safe_load(f) or {}


def _save_provider(provider: str) -> None:
    cfg = _load_config()
    cfg["provider"] = provider
    with open(_CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


def _friday_running() -> bool:
    result = subprocess.run(["pgrep", "-f", "friday.py"], capture_output=True)
    return result.returncode == 0


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )


class FridayMenuBar(rumps.App):
    def __init__(self):
        super().__init__("Friday", quit_button=None)
        self._update_title()

        self._brief    = rumps.MenuItem("Brief Me Now", callback=self.brief_me)
        self._provider = rumps.MenuItem("Provider")
        self._ollama   = rumps.MenuItem("Ollama",  callback=self.set_ollama)
        self._gemini   = rumps.MenuItem("Gemini",  callback=self.set_gemini)
        self._provider.update([self._ollama, self._gemini])
        self._dashboard = rumps.MenuItem("Open Dashboard", callback=self.open_dashboard)
        self._quit      = rumps.MenuItem("Quit Friday Bar", callback=rumps.quit_application)

        self.menu = [
            self._brief,
            None,
            self._provider,
            None,
            self._dashboard,
            None,
            self._quit,
        ]

        self._update_provider_checks()
        rumps.Timer(self._tick, 30).start()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_title(self):
        dot = "●" if _friday_running() else "○"
        self.title = f"Friday {dot}"

    def _update_provider_checks(self):
        cfg = _load_config()
        provider = cfg.get("provider", "ollama")
        self._ollama.state = 1 if provider == "ollama" else 0
        self._gemini.state = 1 if provider == "gemini" else 0

    def _tick(self, _):
        self._update_title()

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def brief_me(self, _):
        cfg = _load_config()
        tg  = cfg.get("telegram", {})
        token   = tg.get("bot_token", "")
        chat_id = tg.get("chat_id", "")
        if not token or not chat_id:
            rumps.alert("Friday", "Telegram not configured in friday_config.yaml.")
            return
        try:
            _send_telegram(token, chat_id, "brief me")
        except Exception as e:
            rumps.alert("Friday", f"Failed to send: {e}")

    def _restart(self):
        _restart_sh = os.path.join(_HERE, "restart.sh")
        subprocess.Popen(["bash", _restart_sh], start_new_session=True)

    def set_ollama(self, _):
        _save_provider("ollama")
        self._update_provider_checks()
        self._restart()

    def set_gemini(self, _):
        cfg = _load_config()
        if not cfg.get("gemini", {}).get("api_key"):
            rumps.alert("Friday", "Gemini API key not set in friday_config.yaml.")
            return
        _save_provider("gemini")
        self._update_provider_checks()
        self._restart()

    def open_dashboard(self, _):
        subprocess.Popen([_PYTHON, _DASHBOARD])


if __name__ == "__main__":
    FridayMenuBar().run()
