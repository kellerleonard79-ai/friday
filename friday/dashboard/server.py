"""
dashboard/server.py
FastAPI app that powers the F.R.I.D.A.Y. local web dashboard.

Hosted inside friday.py's PTB asyncio loop (single event loop), bound to
127.0.0.1:5174 — never exposed to the network, so no auth.

All endpoints touch the SAME SQLite connection and the SAME config file the
running agent uses. Config writes are atomic (tmp + rename).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import memory.state as state

logger = logging.getLogger("friday.dashboard")

# ── Constants ────────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"
_LOG_PATH   = Path(__file__).resolve().parent.parent / "logs" / "friday.log"

# Sensitive keys masked by default. dot-paths into friday_config.yaml.
_SECRET_PATHS: tuple[str, ...] = (
    "telegram.bot_token",
    "gemini.api_key",
    "canvas.api_token",
    "groupme.api_token",
    "weather.api_key",
)
_MASK = "********"

# Hardcoded Gemini quota tiers (the models endpoint doesn't return quotas).
# Source: ai.google.dev/gemini-api/docs/rate-limits (free tier, as of 2026).
_GEMINI_TIERS: dict[str, dict[str, Any]] = {
    "gemini-2.5-flash":      {"recommended_free": True,  "rpm": 10,  "tpm": 250_000, "rpd": 250},
    "gemini-2.5-flash-lite": {"recommended_free": True,  "rpm": 15,  "tpm": 250_000, "rpd": 1000},
    "gemini-2.5-pro":        {"recommended_free": False, "rpm": 5,   "tpm": 250_000, "rpd": 100},
    "gemini-2.0-flash":      {"recommended_free": True,  "rpm": 15,  "tpm": 1_000_000, "rpd": 200},
    "gemini-2.0-flash-lite": {"recommended_free": True,  "rpm": 30,  "tpm": 1_000_000, "rpd": 200},
    "gemma-3-27b-it":        {"recommended_free": True,  "rpm": 30,  "tpm": 15_000,  "rpd": 14_400},
    "gemma-4-31b-it":        {"recommended_free": True,  "rpm": 30,  "tpm": 15_000,  "rpd": 14_400},
}


# ── Request models ───────────────────────────────────────────────────────────

class PauseRequest(BaseModel):
    paused: bool
    until: str | None = None  # ISO datetime; menubar uses this for timed pauses


# ── Config I/O ───────────────────────────────────────────────────────────────

_DEFAULT_PERSONA = {
    "preset": "friday",
    "snark_level": "medium",
    "jarvis_phrases": {
        "For you sir, always.": True,
        "At your service, sir.": True,
        "As you wish, sir.": True,
        "Welcome home, sir.": False,
        "A very astute observation, sir.": True,
        "I'm not saying you're stupid, I'm just saying you have terrible luck thinking.": True,
        "Importing preferences and calibrating virtual environment.": True,
        "I'm adding 'touch grass' to your to-do list. Doctor's orders.": True,
        "Your wish is my... mild inconvenience.": True,
        "Tutoring session booked. Try to pretend you did the reading this time.": True,
        "Project due tomorrow. Fascinating how you waited until the last possible second.": True,
        "Club meeting added. Hope it's more productive than your group chats.": True,
        "Your entire week is now scheduled. Good luck, future valedictorian… or beautiful disaster. Whichever comes first.": True,
        "Thrilling. Another all-nighter in the making.": True,
        "I've scheduled it. Your sleep schedule remains offended.": True,
        "Study group at 4 PM. I'll remind you, but we both know you'll show up 20 minutes late with snacks instead of notes.": True,
        "You're running late. As is tradition.": True,
        "My circuits are just thrilled at the prospect.": True,
        "I've sent the email for you. Don't worry, I made it sound like you actually care.": True,
        "Deadline approaching in T-minus 'oh crap' hours.": True,
        "You asked me to remind you. This is me reminding you. You're welcome, human.": True,
        "Congratulations, you've double-booked yourself. Should I just start cloning you?": True,
        "I've prepared a weather briefing for you to entirely ignore.": True,
    },
    "custom_instructions": "",
}

_DEFAULT_NOTIFICATIONS = {
    "morning_briefing": {"enabled": True, "time": "07:00"},
    "evening_briefing": {"enabled": True, "time": "20:00"},
    "proactive_reminders": True,
    "urgent_interrupts": True,
    "canvas_polling": True,
    "groupme_polling": True,
    "reminder_thresholds": [5, 3, 1],
}


def _migrate_config(cfg: dict) -> dict:
    """Lazy migration: ensure persona/notifications blocks exist and groupme
    groups have the new schema (id, enabled, priority enum high|normal|muted)."""
    if "persona" not in cfg:
        cfg["persona"] = dict(_DEFAULT_PERSONA)
        cfg["persona"]["jarvis_phrases"] = dict(_DEFAULT_PERSONA["jarvis_phrases"])
    if "notifications" not in cfg:
        cfg["notifications"] = {
            "morning_briefing": dict(_DEFAULT_NOTIFICATIONS["morning_briefing"]),
            "evening_briefing": dict(_DEFAULT_NOTIFICATIONS["evening_briefing"]),
            "proactive_reminders": True,
            "urgent_interrupts": True,
            "canvas_polling": True,
            "groupme_polling": True,
            "reminder_thresholds": [5, 3, 1],
        }
        # Mirror existing canonical times into the notifications block.
        agent_cfg = cfg.get("agent", {})
        if agent_cfg.get("morning_briefing_time"):
            cfg["notifications"]["morning_briefing"]["time"] = str(
                agent_cfg["morning_briefing_time"]
            )
        if agent_cfg.get("briefing_time"):
            cfg["notifications"]["evening_briefing"]["time"] = str(
                agent_cfg["briefing_time"]
            )

    gm = cfg.get("groupme") or {}
    groups = gm.get("groups") or []
    for g in groups:
        if "enabled" not in g:
            g["enabled"] = True
        # priority migration: old 'low' → 'muted'
        pri = (g.get("priority") or "normal").lower()
        if pri == "low":
            pri = "muted"
        if pri not in ("high", "normal", "muted"):
            pri = "normal"
        g["priority"] = pri
    return cfg


def _load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    return _migrate_config(cfg)


def _save_config_atomic(config_path: Path, cfg: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".friday_config_", suffix=".yaml",
                               dir=str(config_path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=True)
        shutil.move(tmp, config_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _mask_secrets(cfg: dict) -> dict:
    """Return a deep copy of cfg with secret paths masked."""
    import copy
    masked = copy.deepcopy(cfg)
    for path in _SECRET_PATHS:
        keys = path.split(".")
        node = masked
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                node = None
                break
            node = node[k]
        if isinstance(node, dict) and node.get(keys[-1]):
            val = str(node[keys[-1]])
            node[keys[-1]] = f"{_MASK}{val[-4:]}" if len(val) > 4 else _MASK
    return masked


def _sync_briefing_times(cfg: dict) -> None:
    """Keep agent.{morning_briefing_time,briefing_time} in sync with the
    notifications mirror. The agent block stays canonical for the job_queue."""
    notif = cfg.get("notifications") or {}
    agent_cfg = cfg.setdefault("agent", {})
    morning = (notif.get("morning_briefing") or {}).get("time")
    evening = (notif.get("evening_briefing") or {}).get("time")
    if morning:
        agent_cfg["morning_briefing_time"] = morning
    if evening:
        agent_cfg["briefing_time"] = evening


# ── App factory ──────────────────────────────────────────────────────────────

def create_app(config_path: Path, conn: sqlite3.Connection,
               started_at: datetime) -> FastAPI:
    app = FastAPI(title="F.R.I.D.A.Y. Dashboard", docs_url=None, redoc_url=None)

    # Generate a circular favicon from the user's menubar PNG if available.
    # Best-effort — never let an icon failure block server startup.
    try:
        import menubar_icon
        menubar_icon.ensure_favicon(_STATIC_DIR / "favicon.png")
    except Exception as e:
        logger.debug(f"favicon generation skipped: {e}")

    # Static files at /static/*
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    @app.get("/api/status")
    def api_status() -> dict:
        cfg = _load_config(config_path)
        notif = cfg.get("notifications") or {}
        keys = [
            "status", "paused", "paused_until", "provider", "model",
            "started_at", "think_calls", "tokens_in", "tokens_out",
            "last_message_at", "last_message_preview",
        ]
        out: dict[str, Any] = {k: state.get(conn, k) for k in keys}
        out["paused"] = (out.get("paused") == "true")
        out["server_started_at"] = started_at.isoformat()
        # Uptime computed off the bot's started_at, not the server's.
        st = out.get("started_at")
        if st:
            try:
                dt = datetime.fromisoformat(st)
                out["uptime_seconds"] = int((datetime.now() - dt).total_seconds())
            except ValueError:
                out["uptime_seconds"] = None
        else:
            out["uptime_seconds"] = None
        out["next_morning_briefing"] = (notif.get("morning_briefing") or {}).get("time")
        out["next_evening_briefing"] = (notif.get("evening_briefing") or {}).get("time")
        return out

    @app.get("/api/config")
    def api_config_get(reveal: int = Query(0)) -> dict:
        cfg = _load_config(config_path)
        if not reveal:
            cfg = _mask_secrets(cfg)
        return cfg

    @app.post("/api/config")
    async def api_config_post(payload: dict) -> dict:
        # If incoming payload contains masked tokens, splice the real values
        # back in from disk so we never overwrite a real secret with the mask.
        on_disk = _load_config(config_path)
        for path in _SECRET_PATHS:
            keys = path.split(".")
            in_node, disk_node = payload, on_disk
            for k in keys[:-1]:
                if not isinstance(in_node, dict) or k not in in_node:
                    in_node = None
                    break
                in_node = in_node[k]
                disk_node = disk_node.get(k, {}) if isinstance(disk_node, dict) else {}
            if not isinstance(in_node, dict):
                continue
            v = in_node.get(keys[-1])
            if isinstance(v, str) and v.startswith(_MASK):
                in_node[keys[-1]] = disk_node.get(keys[-1], "") if isinstance(disk_node, dict) else ""
        _sync_briefing_times(payload)
        try:
            _save_config_atomic(config_path, payload)
        except Exception as e:
            raise HTTPException(500, f"Could not write config: {e}")
        return {"ok": True}

    @app.get("/api/groupme/groups")
    def api_groupme_groups() -> dict:
        cfg = _load_config(config_path)
        token = (cfg.get("groupme") or {}).get("api_token", "")
        if not token:
            return {"ok": False, "error": "GroupMe API token not set."}
        try:
            r = requests.get(
                "https://api.groupme.com/v3/groups",
                params={"token": token, "per_page": 100},
                timeout=15,
            )
            r.raise_for_status()
            groups = r.json().get("response", []) or []
            simplified = [
                {
                    "id": g.get("id"),
                    "name": g.get("name"),
                    "member_count": len(g.get("members", []) or []),
                }
                for g in groups
            ]
            return {"ok": True, "groups": simplified}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/gemini/models")
    def api_gemini_models() -> dict:
        cfg = _load_config(config_path)
        api_key = (cfg.get("gemini") or {}).get("api_key", "")
        if not api_key:
            return {"ok": False, "error": "Gemini API key not set."}
        try:
            r = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": api_key},
                timeout=15,
            )
            r.raise_for_status()
            raw = r.json().get("models", []) or []
            models = []
            for m in raw:
                methods = m.get("supportedGenerationMethods", []) or []
                if "generateContent" not in methods:
                    continue
                name = (m.get("name") or "").removeprefix("models/")
                tier = _GEMINI_TIERS.get(name) or _GEMINI_TIERS.get(name.split("-latest")[0]) or {}
                models.append({
                    "name": name,
                    "display_name": m.get("displayName") or name,
                    "description": m.get("description") or "",
                    "input_token_limit": m.get("inputTokenLimit"),
                    "output_token_limit": m.get("outputTokenLimit"),
                    "recommended_free": tier.get("recommended_free", False),
                    "rpm": tier.get("rpm"),
                    "tpm": tier.get("tpm"),
                    "rpd": tier.get("rpd"),
                })
            return {"ok": True, "models": models}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/friday/restart")
    def api_friday_restart() -> dict:
        uid = os.getuid()
        try:
            subprocess.Popen(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.friday.agent"],
                start_new_session=True,
            )
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Restart failed: {e}")

    @app.post("/api/friday/pause")
    def api_friday_pause(req: PauseRequest) -> dict:
        state.set(conn, "paused", "true" if req.paused else "false")
        if req.paused and req.until:
            state.set(conn, "paused_until", req.until)
        else:
            state.delete(conn, "paused_until")
        return {"ok": True, "paused": req.paused, "until": req.until}

    @app.post("/api/friday/brief")
    def api_friday_brief() -> dict:
        cfg = _load_config(config_path)
        tg = cfg.get("telegram", {})
        token, chat_id = tg.get("bot_token", ""), tg.get("chat_id", "")
        if not token or not chat_id:
            raise HTTPException(400, "Telegram not configured.")
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "brief me"},
                timeout=10,
            )
            r.raise_for_status()
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Brief failed: {e}")

    @app.get("/api/logs")
    def api_logs(lines: int = Query(100, ge=1, le=1000)) -> dict:
        if not _LOG_PATH.exists():
            return {"lines": []}
        # deque tail: bounded memory, no need to load the whole file.
        with open(_LOG_PATH, "r", errors="replace") as f:
            tail = deque(f, maxlen=lines)
        return {"lines": [ln.rstrip("\n") for ln in tail]}

    @app.post("/api/test/telegram")
    def api_test_telegram() -> dict:
        cfg = _load_config(config_path)
        tg = cfg.get("telegram", {})
        token, chat_id = tg.get("bot_token", ""), tg.get("chat_id", "")
        if not token or not chat_id:
            return {"ok": False, "error": "Telegram not configured."}
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "Friday dashboard test"},
                timeout=10,
            )
            r.raise_for_status()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/test/canvas")
    def api_test_canvas() -> dict:
        cfg = _load_config(config_path)
        canvas = cfg.get("canvas", {})
        url, tok = canvas.get("ical_url", ""), canvas.get("api_token", "")
        if not url:
            return {"ok": False, "error": "Canvas iCal URL not set."}
        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {tok}"} if tok else {},
                timeout=15,
                stream=True,
            )
            ok = r.status_code == 200
            r.close()
            return {"ok": ok, "status_code": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return app


# ── Lifecycle helper called from friday.py ───────────────────────────────────

async def start_server(config_path: Path, conn: sqlite3.Connection,
                       host: str = "127.0.0.1", port: int = 5174) -> uvicorn.Server:
    """Build the FastAPI app, wrap in uvicorn, return the Server (caller schedules
    server.serve() as an asyncio task and keeps a handle for clean shutdown)."""
    app = create_app(config_path, conn, started_at=datetime.now())
    cfg = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(cfg)
    logger.info(f"Dashboard server starting on http://{host}:{port}")
    return server
