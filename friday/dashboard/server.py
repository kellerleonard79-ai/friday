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
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import deque
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import requests
import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import compat
import memory.state as state
import paths

logger = logging.getLogger("friday.dashboard")

# ── Constants ────────────────────────────────────────────────────────────────

_STATIC_DIR = paths.resource_path("dashboard", "static")
_LOG_PATH   = paths.log_dir() / "friday.log"
_VOICE_LOG_PATH = paths.log_dir() / "voice.err"
# voice/listen.py touches this for the duration of every PTT/wake session.
_LISTENING_FLAG = compat.listening_flag_path()

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


_NO_CACHE = "no-cache, no-store, must-revalidate"


class _NoCacheStatic(StaticFiles):
    """StaticFiles that disables conditional/304 caching so edited assets are
    always re-fetched. Localhost dashboard — no bandwidth concern."""

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = _NO_CACHE
        return resp


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


# ── Today surface: activity feed, stats, what's-next ─────────────────────────
# All timestamps in the activity tables are local-naive ISO strings, so the
# day boundary is a simple lexicographic compare against today's midnight.

def _today_start_iso() -> str:
    return datetime.combine(date.today(), dtime.min).isoformat()


def _kind_for_tool(tool_name: str) -> str:
    return "CAL+" if tool_name == "add_calendar_event" else "TOOL"


def _build_activity_feed(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Unified chronological (newest-first) list of everything Friday did today,
    drawn from briefings_sent, tool_calls, urgent_alerts_sent, conversation_history,
    and freshly-ingested events. Each entry: timestamp, kind, summary, details."""
    since = _today_start_iso()
    feed: list[dict] = []

    def _add(ts, kind, summary, details=""):
        feed.append({"timestamp": ts, "kind": kind,
                     "summary": summary, "details": details or ""})

    # Briefings sent
    try:
        for ts, slot, body_full, kind in conn.execute(
            "SELECT timestamp, slot, body_full, on_time_vs_catchup FROM briefings_sent "
            "WHERE timestamp >= ? ORDER BY timestamp", (since,)
        ).fetchall():
            label = f"{(slot or '').capitalize()} briefing sent"
            if kind == "catchup":
                label += " (catch-up)"
            elif kind == "override":
                label += " (rescheduled)"
            _add(ts, "BRIEF", label, body_full)
    except Exception as e:
        logger.debug(f"feed briefings failed: {e}")

    # Tool calls (calendar writes surface as [CAL+])
    try:
        for ts, name, args_json, result_preview, dur in conn.execute(
            "SELECT timestamp, tool_name, args_json, result_preview, duration_ms "
            "FROM tool_calls WHERE timestamp >= ? ORDER BY timestamp", (since,)
        ).fetchall():
            kind = _kind_for_tool(name)
            summary = f"{name} ({dur}ms)"
            if name == "add_calendar_event":
                try:
                    a = json.loads(args_json or "{}")
                    title = a.get("title", "event")
                    when = a.get("date", "")
                    cal = a.get("calendar") or "default calendar"
                    summary = f'Added: "{title}"' + (f" on {when}" if when else "") + f" → {cal}"
                except Exception:
                    summary = "Calendar event added"
            details = f"args: {args_json}\nresult: {result_preview}"
            _add(ts, kind, summary, details)
    except Exception as e:
        logger.debug(f"feed tool_calls failed: {e}")

    # Urgent alerts fired
    try:
        for ts, source, body_preview in conn.execute(
            "SELECT timestamp, source, body_preview FROM urgent_alerts_sent "
            "WHERE timestamp >= ? ORDER BY timestamp", (since,)
        ).fetchall():
            first_line = (body_preview or "").split("\n")[0][:90]
            _add(ts, "ALERT", f"{source}: {first_line}", body_preview)
    except Exception as e:
        logger.debug(f"feed urgent_alerts failed: {e}")

    # Conversation turns
    try:
        for role, content, ts in conn.execute(
            "SELECT role, content, created_at FROM conversation_history "
            "WHERE created_at >= ? ORDER BY created_at", (since,)
        ).fetchall():
            who = "You" if role == "user" else "Friday"
            preview = " ".join((content or "").split())[:90]
            _add(ts, "MSG", f"{who}: {preview}", content)
    except Exception as e:
        logger.debug(f"feed conversation failed: {e}")

    # Freshly ingested events (Canvas due dates, GroupMe messages)
    try:
        for source, title, urgency, created_at in conn.execute(
            "SELECT source, title, urgency, created_at FROM events "
            "WHERE created_at >= ? ORDER BY created_at", (since,)
        ).fetchall():
            kind = "GROUPME" if source == "groupme" else (source or "EVENT").upper()
            tag = f"[{urgency}] " if urgency and urgency != "NORMAL" else ""
            _add(created_at, kind, f"{tag}{title}", "")
    except Exception as e:
        logger.debug(f"feed events failed: {e}")

    feed.sort(key=lambda e: e["timestamp"], reverse=True)
    return feed[:limit]


def _today_stats(conn: sqlite3.Connection, config_path: Path) -> dict:
    """Per-day LLM usage computed from llm_exchanges rows, plus a cost/quota
    estimate from the current model. Free-tier Gemini models report % of daily
    request quota; everything else reports tokens only."""
    since = _today_start_iso()
    calls = tin = tout = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) "
            "FROM llm_exchanges WHERE timestamp >= ?", (since,)
        ).fetchone()
        calls, tin, tout = int(row[0]), int(row[1]), int(row[2])
    except Exception as e:
        logger.debug(f"today_stats query failed: {e}")

    model = state.get(conn, "model") or ""
    provider = state.get(conn, "provider") or ""
    cost: dict[str, Any] = {"model": model, "provider": provider, "dollars": None,
                            "free_tier": False, "rpd": None, "pct_of_daily_quota": None}
    tier = _GEMINI_TIERS.get(model)
    if provider == "gemini" and tier and tier.get("recommended_free") and tier.get("rpd"):
        rpd = tier["rpd"]
        cost["free_tier"] = True
        cost["rpd"] = rpd
        cost["pct_of_daily_quota"] = round(calls / rpd * 100, 1) if rpd else None
    return {"llm_calls": calls, "tokens_in": tin, "tokens_out": tout, "cost": cost}


def _next_briefing(config_path: Path) -> dict | None:
    """Next upcoming briefing (morning or evening) from the notifications times."""
    cfg = _load_config(config_path)
    notif = cfg.get("notifications") or {}
    now = datetime.now()
    candidates = []
    for slot in ("morning_briefing", "evening_briefing"):
        block = notif.get(slot) or {}
        if not block.get("enabled", True):
            continue
        t = block.get("time")
        if not t:
            continue
        try:
            hh, mm = (int(x) for x in str(t).split(":"))
        except ValueError:
            continue
        fire = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fire <= now:
            fire += timedelta(days=1)
        candidates.append((fire, slot.replace("_briefing", ""), t))
    if not candidates:
        return None
    fire, slot, t = min(candidates, key=lambda c: c[0])
    return {"slot": slot, "time": t, "in_minutes": int((fire - now).total_seconds() // 60)}


def _whats_next(conn: sqlite3.Connection, config_path: Path) -> dict:
    """Read-only forward look: today's remaining calendar events, the next
    briefing, and pending Canvas URGENT/SOON items."""
    cfg = _load_config(config_path)
    remaining = []
    try:
        from calendars import backend as calendar_backend
        now = datetime.now().astimezone()
        for ev in calendar_backend.events_for_day(cfg, date.today()):
            start = ev.get("start_iso", "")
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone()
            except (ValueError, AttributeError):
                dt = None
            if dt and dt < now:
                continue
            remaining.append({
                "time": compat.strftime(dt, "%-I:%M %p") if dt else "",
                "title": ev.get("title", "(untitled)"),
                "calendar": ev.get("calendar", ""),
            })
    except Exception as e:
        logger.debug(f"whats_next calendar failed: {e}")

    canvas = []
    try:
        for title, due_at, urgency in conn.execute(
            "SELECT title, due_at, urgency FROM events "
            "WHERE source='canvas' AND urgency IN ('URGENT','SOON') AND notified=0 "
            "ORDER BY due_at"
        ).fetchall():
            canvas.append({"title": title, "due_at": due_at, "urgency": urgency})
    except Exception as e:
        logger.debug(f"whats_next canvas failed: {e}")

    return {
        "remaining_events": remaining,
        "next_briefing": _next_briefing(config_path),
        "canvas_pending": canvas,
    }


def _pending_approvals(conn: sqlite3.Connection) -> list[dict]:
    """Pending approval rows with the full draft text decoded from payload JSON."""
    out = []
    try:
        rows = conn.execute(
            "SELECT id, action_type, payload, created_at FROM pending_actions "
            "WHERE status='pending' ORDER BY created_at DESC"
        ).fetchall()
    except Exception as e:
        logger.debug(f"pending_approvals query failed: {e}")
        return out
    for pid, action_type, payload, created_at in rows:
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {"raw": payload}
        draft = data
        if action_type == "calendar_add" and isinstance(data, dict):
            draft = {
                "title": data.get("title", ""),
                "date": data.get("date", ""),
                "start_time": data.get("start_time") or data.get("time", ""),
                "end_time": data.get("end_time", ""),
                "calendar": data.get("calendar", ""),
                "notes": data.get("notes", ""),
            }
        out.append({"id": pid, "action_type": action_type,
                    "created_at": created_at, "draft": draft})
    return out


# ── App factory ──────────────────────────────────────────────────────────────

# ── Endpoint index ────────────────────────────────────────────────────────────
#   GET  /                       → dashboard SPA (index.html)
#   GET  /api/status             → live state, uptime, token/call stats
#   GET/POST /api/config         → read (secrets masked) / write (atomic) config
#   GET  /api/groupme/groups     → list account's GroupMe groups (for the picker)
#   GET  /api/gemini/models      → list Gemini models + free-tier quota hints
#   POST /api/friday/restart     → kickstart agent (launchd) / SIGINT (Windows tray)
#   POST /api/friday/pause       → pause/resume (+ timed paused_until)
#   POST /api/friday/brief       → trigger an on-demand "brief me"
#   GET  /api/voice/status       → voice LaunchAgent state + listening flag
#   POST /api/voice/{wake,restart}, GET /api/voice/logs → voice controls/logs
#   GET  /api/logs               → tail friday.log
#   POST /api/test/{telegram,canvas} → connectivity self-tests
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

    # Static files at /static/* — served no-cache so a freshly edited app.js /
    # style.css is always picked up. This is a localhost-only dashboard, so the
    # revalidation cost is irrelevant, and it prevents stale-asset blank pages
    # (a cached index.html paired with new routing, or vice-versa).
    if _STATIC_DIR.exists():
        app.mount("/static", _NoCacheStatic(directory=str(_STATIC_DIR)), name="static")

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"),
                            headers={"Cache-Control": _NO_CACHE})

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
        if compat.IS_WINDOWS:
            # The tray supervisor (tray.py) relaunches the core whenever it
            # exits, so a restart is just a graceful shutdown. raise_signal
            # delivers SIGINT to the main thread, which PTB's run_polling
            # handles as a clean stop.
            try:
                threading.Timer(
                    0.3, lambda: signal.raise_signal(signal.SIGINT)
                ).start()
                return {"ok": True}
            except Exception as e:
                raise HTTPException(500, f"Restart failed: {e}")
        uid = os.getuid()
        try:
            subprocess.Popen(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.friday.agent"],
                start_new_session=True,
            )
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Restart failed: {e}")

    @app.get("/api/voice/status")
    def api_voice_status() -> dict:
        """Operational state of the voice satellite (com.friday.voice
        LaunchAgent). agent_loaded reflects whether launchd has the job in a
        running state; listening reflects the transient PTT/wake-session flag
        voice/listen.py touches at /tmp/friday_listening."""
        if compat.IS_WINDOWS:
            # No voice satellite on Windows — report a clean "not running".
            return {
                "agent_loaded": False,
                "listening": False,
                "session_present": False,
                "wake_enabled": False,
            }
        uid = os.getuid()
        agent_loaded = False
        try:
            out = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/com.friday.voice"],
                capture_output=True, text=True, timeout=2,
            )
            # `launchctl print` returns rc=0 with "state = running" when up;
            # rc != 0 when the job isn't loaded at all.
            agent_loaded = (out.returncode == 0 and "state = running" in out.stdout)
        except Exception as e:
            logger.debug(f"voice status launchctl probe failed: {e}")
        cfg = _load_config(config_path)
        session_present = bool((cfg.get("telegram") or {}).get("telethon_session"))
        voice_cfg = cfg.get("voice") or {}
        return {
            "agent_loaded": agent_loaded,
            "listening": _LISTENING_FLAG.exists(),
            "session_present": session_present,
            "wake_enabled": bool(voice_cfg.get("wake_enabled", False)),
        }

    @app.post("/api/voice/wake")
    def api_voice_wake(payload: dict) -> dict:
        """Flip voice.wake_enabled in friday_config.yaml and kick the voice
        LaunchAgent so the change takes effect. PTT is unaffected — it works
        in both modes."""
        if compat.IS_WINDOWS:
            raise HTTPException(400, "Voice is not available on Windows.")
        enabled = bool(payload.get("enabled"))
        cfg = _load_config(config_path)
        voice_cfg = cfg.setdefault("voice", {})
        voice_cfg["wake_enabled"] = enabled
        try:
            _save_config_atomic(config_path, cfg)
        except Exception as e:
            raise HTTPException(500, f"Could not write config: {e}")
        uid = os.getuid()
        try:
            subprocess.Popen(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.friday.voice"],
                start_new_session=True,
            )
        except Exception as e:
            raise HTTPException(500, f"Voice restart failed: {e}")
        return {"ok": True, "wake_enabled": enabled}

    @app.post("/api/voice/restart")
    def api_voice_restart() -> dict:
        if compat.IS_WINDOWS:
            raise HTTPException(400, "Voice is not available on Windows.")
        uid = os.getuid()
        try:
            subprocess.Popen(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.friday.voice"],
                start_new_session=True,
            )
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Voice restart failed: {e}")

    @app.get("/api/voice/logs")
    def api_voice_logs(lines: int = Query(100, ge=1, le=1000)) -> dict:
        if not _VOICE_LOG_PATH.exists():
            return {"lines": []}
        with open(_VOICE_LOG_PATH, "r", errors="replace") as f:
            tail = deque(f, maxlen=lines)
        return {"lines": [ln.rstrip("\n") for ln in tail]}

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

    # ── Today surface ─────────────────────────────────────────────────────────

    @app.get("/api/today")
    def api_today() -> dict:
        """Single bundle powering the Today tab: status, next briefing, pending
        count, activity feed (today, newest-first, capped at 100), what's-next,
        and per-day stats. Polled every 5s by the dashboard."""
        pending = _pending_approvals(conn)
        return {
            "status": state.get(conn, "status"),
            "paused": state.get(conn, "paused") == "true",
            "pending_approvals_count": len(pending),
            "next_briefing": _next_briefing(config_path),
            "activity_feed": _build_activity_feed(conn, limit=100),
            "whats_next": _whats_next(conn, config_path),
            "today_stats": _today_stats(conn, config_path),
        }

    @app.get("/api/llm/last")
    def api_llm_last() -> dict:
        """Most recent LLM exchange in full (prompt, response, tokens, duration,
        model) plus the tool calls that ran inside it. Fetched on-demand when the
        developer panel expands — not part of the 5s Today poll."""
        row = conn.execute(
            "SELECT id, timestamp, model, tokens_in, tokens_out, duration_ms, "
            "triggered_by, full_prompt, full_response FROM llm_exchanges "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"present": False}
        (ex_id, ts, model, tin, tout, dur, trig, prompt, response) = row
        # Tool calls for this exchange ran during generate_content, so their
        # rows were written after the previous exchange and before this one.
        prev = conn.execute(
            "SELECT timestamp FROM llm_exchanges WHERE id < ? ORDER BY id DESC LIMIT 1",
            (ex_id,)
        ).fetchone()
        prev_ts = prev[0] if prev else ""
        tool_rows = conn.execute(
            "SELECT timestamp, tool_name, args_json, result_preview, duration_ms "
            "FROM tool_calls WHERE timestamp <= ? AND timestamp > ? ORDER BY timestamp",
            (ts, prev_ts)
        ).fetchall()
        tools = [{"timestamp": t[0], "tool_name": t[1], "args_json": t[2],
                  "result_preview": t[3], "duration_ms": t[4]} for t in tool_rows]
        return {
            "present": True, "timestamp": ts, "model": model,
            "tokens_in": tin, "tokens_out": tout, "duration_ms": dur,
            "triggered_by": trig, "prompt": prompt, "response": response,
            "tool_calls": tools,
        }

    @app.get("/api/pending-approvals")
    def api_pending_approvals() -> dict:
        return {"pending": _pending_approvals(conn)}

    @app.post("/api/pending-approvals/{pid}/{verb}")
    def api_pending_approval_action(
        pid: str, verb: str, body: dict = Body(default={})
    ) -> dict:
        """Confirm / edit / cancel a pending action through the SAME pipeline
        Telegram uses (actions/calendar.py). Runs in FastAPI's threadpool, so the
        synchronous calendar write does not block the event loop."""
        if verb not in ("confirm", "edit", "cancel"):
            raise HTTPException(400, "verb must be confirm, edit, or cancel")
        row = conn.execute(
            "SELECT action_type, payload, status FROM pending_actions WHERE id = ?",
            (pid,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Unknown or expired approval.")
        action_type, payload, status = row
        if status != "pending":
            raise HTTPException(409, f"Already {status}.")

        # A lightweight TelegramHandler (send-only) mirrors the confirmation the
        # user would have seen had they tapped the inline button in Telegram.
        cfg = _load_config(config_path)
        from channels.telegram import TelegramHandler
        tg = TelegramHandler(cfg, agent=None, conn=conn)

        if action_type != "calendar_add":
            raise HTTPException(400, f"Unsupported action type: {action_type}")
        from actions import calendar as cal_action

        if verb == "edit":
            edited = (body or {}).get("edited_body")
            if not edited:
                raise HTTPException(400, "edit requires edited_body.")
            try:
                event = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                event = {}
            # edited_body may be a full JSON event object, or a plain new title.
            try:
                parsed = json.loads(edited)
                if isinstance(parsed, dict):
                    event.update(parsed)
                else:
                    event["title"] = str(edited)
            except (json.JSONDecodeError, TypeError):
                event["title"] = str(edited)
            conn.execute("UPDATE pending_actions SET payload = ? WHERE id = ?",
                         (json.dumps(event), pid))
            conn.commit()
            return {"ok": True, "status": "pending",
                    "draft": _pending_approvals(conn)}

        if verb == "confirm":
            ok = cal_action.confirm_pending(pid, conn, tg)
            return {"ok": bool(ok), "status": "confirmed" if ok else "failed"}

        cal_action.cancel_pending(pid, conn, tg)
        return {"ok": True, "status": "cancelled"}

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
