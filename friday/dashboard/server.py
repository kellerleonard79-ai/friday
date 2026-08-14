"""
dashboard/server.py
FastAPI app that powers the F.R.I.D.A.Y. local web dashboard.

Hosted inside friday.py's PTB asyncio loop (single event loop). Bound to
127.0.0.1:5174 and, when Tailscale is running, this machine's tailnet
address specifically — never 0.0.0.0. Every route requires the shared
token; see dashboard/auth.py.

All endpoints touch the SAME SQLite connection and the SAME config file the
running agent uses. Config writes are atomic (tmp + rename).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
from collections import deque
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import requests
import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import compat
import memory.state as state
import power
import schedule
from dashboard import auth, stream, tls_certs
import paths
import self_edit
from connectors import stocks as stocks_conn
from connectors import weather as weather_conn
from connectors.groupme import normalize_priority
from policy import visibility
from self_edit import sync_briefing_times as _sync_briefing_times

logger = logging.getLogger("friday.dashboard")

# ── Constants ────────────────────────────────────────────────────────────────

_STATIC_DIR = paths.resource_path("dashboard", "static")
_LOG_PATH   = paths.log_dir() / "friday.log"
_VOICE_LOG_PATH = paths.log_dir() / "voice.err"
# voice/listen.py touches this for the duration of every PTT/wake session.
_LISTENING_FLAG = compat.listening_flag_path()

# Seeded into a config that has no stocks block yet. Not a hard-coded
# portfolio — the dashboard writes over this list the first time the user
# edits it, and connectors/stocks.py is the thing that validates a symbol.
_DEFAULT_TICKERS = ("AAPL", "MSFT", "NVDA", "GOOGL", "TSLA", "SPY")

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


class PowerManualRequest(BaseModel):
    minutes: int | None = None    # required unless clear=True
    clear: bool = False


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

    # The bell schedule. Owned by schedule.py so the defaults and the
    # backfill live next to the code that reads them, rather than becoming a
    # third copy of the period list in this file.
    schedule.ensure(cfg)

    # Machine-location weather. A config predating it has no key at all, and
    # an absent key must read as "on" in the dashboard checkbox — the default
    # bindInput() applies to a MISSING value is whatever the checkbox's own
    # HTML says (unchecked), which would show OFF for a feature that is
    # actually on. See connectors/weather.py::_here for the behavior itself.
    wx = cfg.setdefault("weather", {})
    if "use_machine_location" not in wx:
        wx["use_machine_location"] = True

    # The markets widget. A config predating it has no stocks block at all,
    # and an absent block must mean "the defaults" rather than "no widget" —
    # otherwise the feature ships invisible to every existing install.
    st = cfg.setdefault("stocks", {})
    if "enabled" not in st:
        st["enabled"] = True
    if not st.get("tickers"):
        st["tickers"] = list(_DEFAULT_TICKERS)

    # Passing-period wake schedule. Owned by power.py the same way the bell
    # schedule is owned by schedule.py — the defaults live next to the code
    # that reads them. A config predating this feature gets it off, not on:
    # a machine that starts holding disablesleep the moment it happens to
    # read a new key is the opposite of the "never left on" invariant.
    ws = cfg.setdefault("wake_schedule", {})
    ws.setdefault("enabled", False)
    ws.setdefault("lead_minutes", 2)
    ws.setdefault("max_passing_minutes", schedule.DEFAULT_MAX_PASSING_MINUTES)
    ws.setdefault("wake_days_ahead", 3)
    ws.setdefault("derived_overrides", {})
    ws.setdefault("custom_blocks", [])

    gm = cfg.get("groupme") or {}
    groups = gm.get("groups") or []
    for g in groups:
        if "enabled" not in g:
            g["enabled"] = True
        # Tier vocabulary (including the legacy 'low' → 'muted' migration) is
        # owned by connectors.groupme so the reader and the writer can't drift.
        g["priority"] = normalize_priority(g.get("priority"), g.get("name", ""))
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


# _sync_briefing_times now lives in self_edit.py and is imported above. The
# update_setting tool writes briefing times too, so a mirror maintained by only
# one of the two writers goes stale as soon as the other is used.


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
        # Same rule as the briefing bundle, from the same place — the urgency
        # list is policy/visibility.py's. These two queries were byte-identical
        # copies, which is how two surfaces come to disagree about what
        # "pending" means the first time one of them is edited.
        placeholders = ",".join("?" * len(visibility.BRIEFING_URGENCIES))
        for title, due_at, urgency in conn.execute(
            f"SELECT title, due_at, urgency FROM events "
            f"WHERE source='canvas' AND urgency IN ({placeholders}) "
            f"AND notified = 0 "
            f"ORDER BY due_at",
            visibility.BRIEFING_URGENCIES,
        ).fetchall():
            canvas.append({"title": title, "due_at": due_at, "urgency": urgency})
    except Exception as e:
        logger.debug(f"whats_next canvas failed: {e}")

    return {
        "remaining_events": remaining,
        "next_briefing": _next_briefing(config_path),
        "canvas_pending": canvas,
    }


class ChatIn(BaseModel):
    """The chat request body.

    MODULE SCOPE, NOT INSIDE create_app(). `from __future__ import annotations`
    turns every annotation into a string and FastAPI resolves them against the
    module namespace — a model defined in a function body is invisible there,
    and the parameter degrades into a required query argument with no error
    until the first request.
    """
    text: str


def _publish_pending(broadcaster, pid: str, status: str) -> None:
    """Tell every attached browser that a card is no longer live.

    ONLY COVERS RESOLUTIONS THAT HAPPEN HERE. A card confirmed by tapping the
    Telegram button is resolved inside effects/pending.py, which must not
    import a dashboard — that is the layering the whole rebuild exists to
    protect. The browser catches those on its pending poll a few seconds
    later, which is the honest cost of not cross-wiring two channels together.
    """
    try:
        broadcaster.publish({"kind": "pending", "id": pid, "status": status})
    except Exception as e:
        logger.debug(f"pending broadcast failed: {e}")


def _pending_approvals(conn: sqlite3.Connection) -> list[dict]:
    """Pending approval rows with the full draft text decoded from payload JSON."""
    out = []
    try:
        rows = conn.execute(
            "SELECT id, action_type, payload, created_at, tool_name, "
            "       arguments_json, proposal, expires_at "
            "FROM pending_actions WHERE status='pending' "
            "ORDER BY created_at DESC"
        ).fetchall()
    except Exception as e:
        logger.debug(f"pending_approvals query failed: {e}")
        return out
    for (pid, action_type, payload, created_at, tool_name,
         arguments_json, proposal, expires_at) in rows:
        if action_type == "tool_call":
            # A card is a proposal to run one tool with one set of arguments.
            # The draft is the arguments themselves, and `proposal` is the card
            # text exactly as it went to Telegram — shown rather than rebuilt
            # so the dashboard cannot drift from what the user actually saw.
            try:
                args = json.loads(arguments_json or "{}")
            except json.JSONDecodeError:
                args = {"raw": arguments_json}
            out.append({"id": pid, "action_type": action_type,
                        "created_at": created_at, "expires_at": expires_at,
                        "tool": tool_name, "proposal": proposal or "",
                        "draft": args})
            continue
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
                    "created_at": created_at, "expires_at": expires_at,
                    "draft": draft})
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
def restart_daemon() -> dict:
    """Restart the whole daemon. Used by the /api/friday/restart route and,
    standalone, by the Tailscale-cert renewal job — a renewed cert needs a
    fresh process to load it, since a live asyncio SSL context has no
    reload path.

    Two deployments supervise the core and relaunch it whenever it exits —
    tray.py on Windows, mac_app.CoreSupervisor inside Friday.app. For both,
    a restart is just a graceful shutdown. raise_signal sets the flag that
    CPython services on the main thread, so PTB's run_polling sees the
    SIGINT it already handles as a clean stop.
    """
    def self_restart() -> dict:
        try:
            threading.Timer(
                0.3, lambda: signal.raise_signal(signal.SIGINT)
            ).start()
            return {"ok": True}
        except Exception as e:
            raise HTTPException(500, f"Restart failed: {e}")

    if compat.IS_WINDOWS:
        return self_restart()

    # macOS runs either way: under the com.friday.agent LaunchAgent, or as
    # a supervised child of Friday.app with no LaunchAgent at all. Probe
    # before assuming — `launchctl kickstart` against a label launchd has
    # never heard of fails silently, and Popen only reports that launchctl
    # itself started, so the endpoint used to answer ok:true while nothing
    # restarted.
    uid = os.getuid()
    label = f"gui/{uid}/com.friday.agent"
    try:
        installed = subprocess.run(
            ["launchctl", "print", label],
            capture_output=True, timeout=5).returncode == 0
    except Exception as e:
        logger.debug(f"launchctl print {label} failed: {e}")
        installed = False
    if not installed:
        return self_restart()
    try:
        # start_new_session so kickstart -k tearing down this job does not
        # also kill the launchctl doing the tearing down.
        subprocess.Popen(
            ["launchctl", "kickstart", "-k", label],
            start_new_session=True,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"Restart failed: {e}")


def check_and_renew_tailscale_cert() -> None:
    """Daily job body: renew the tailnet HTTPS cert if Tailscale decides
    it's close enough to its ~90-day expiry, and restart the daemon so the
    new cert actually gets loaded. The common case is a no-op renewal
    (tailscale cert only renews in roughly the last third of the window),
    which changes nothing on disk and never restarts.

    Best-effort like the rest of the daily jobs: Tailscale being
    unreachable here is not a reason to fail a scheduled job.
    """
    try:
        dom = tls_certs.domain()
        if dom is None:
            return
        if tls_certs.renew_if_needed(dom):
            logger.info(f"Tailscale HTTPS cert for {dom} renewed — restarting to load it.")
            restart_daemon()
    except Exception as e:
        logger.warning(f"Tailscale cert renewal check failed: {e}")


def create_app(config_path: Path, conn: sqlite3.Connection,
               started_at: datetime,
               on_demand_briefing: Callable[[], Awaitable[str]] | None = None,
               port_hint: int = 5174,
               ) -> FastAPI:
    app = FastAPI(title="F.R.I.D.A.Y. Dashboard", docs_url=None, redoc_url=None)

    # ── Auth ─────────────────────────────────────────────────────────────────
    #
    # EVERY route. Not "every API route" — the SPA itself, app.js, the stream,
    # the card endpoints. /api/config alone hands back the Telegram bot token
    # and the Gemini key in plaintext, and the card endpoints write to the
    # user's calendar.
    #
    # This lands while the bind is still loopback, on purpose: the moment the
    # address changes, "only this machine can reach it" stops being true
    # everywhere at once, and adding auth after that has a window in it.
    cfg_at_boot = _load_config(config_path)
    auth_token, generated = auth.resolve(cfg_at_boot)
    if generated:
        # Persisted through the same atomic save every config write uses, so a
        # crash mid-write cannot leave a truncated config and a locked-out user.
        try:
            cfg_at_boot.setdefault("dashboard", {})["auth_token"] = auth_token
            _save_config_atomic(config_path, cfg_at_boot)
        except Exception as e:
            logger.error(f"Could not persist the dashboard auth token: {e}")
    auth.announce(auth_token, generated, port_hint)

    @app.middleware("http")
    async def _require_token(request, call_next):
        path = request.url.path
        if auth.is_open_path(path) or auth.authorized(request, auth_token):
            return await call_next(request)

        # ?token=… — the first visit, and the form that makes this usable from
        # a phone. The cookie is set and the browser is sent to the bare path,
        # so the secret does not linger in the address bar or the history.
        presented = request.query_params.get(auth.QUERY)
        if presented and secrets.compare_digest(str(presented), auth_token):
            clean = str(request.url.replace(query=""))
            response = RedirectResponse(clean, status_code=303)
            response.set_cookie(
                auth.COOKIE, auth_token,
                max_age=auth.COOKIE_MAX_AGE,
                httponly=True,      # no JS on this page needs to read it
                samesite="lax",     # a cross-site POST must not carry it
                # secure=False: there is no TLS on a tailnet HTTP origin, and a
                # Secure cookie would simply never be sent. The transport's
                # confidentiality is Tailscale's, not this cookie's.
            )
            return response

        logger.info(f"Unauthenticated request to {path} — refused.")
        return JSONResponse(
            {"detail": "Not authorized. Open the dashboard with ?token=… once; "
                       "the token is in friday_config.yaml under "
                       "dashboard.auth_token."},
            status_code=401,
        )

    # Server→client push. One per app. It binds itself to the running loop
    # when the first browser subscribes, which is the earliest moment it can
    # possibly be needed — publish() with no subscribers is a no-op.
    broadcaster = stream.Broadcaster()
    app.state.broadcaster = broadcaster

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

    @app.get("/sw.js")
    def service_worker() -> FileResponse:
        # Served from the origin root rather than under /static/ so its
        # default scope is "/" — a script registered from /static/sw.js would
        # only ever see requests under /static/*, which cannot intercept
        # navigation to "/" or the /api/schedule and /api/after-school reads
        # the offline shell depends on. Service-Worker-Allowed is the
        # header-based escape hatch for the same rule and costs nothing to
        # set alongside actually serving it from the root.
        return FileResponse(str(_STATIC_DIR / "sw.js"),
                            media_type="application/javascript",
                            headers={"Cache-Control": _NO_CACHE,
                                    "Service-Worker-Allowed": "/"})

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

    # ── Quips ────────────────────────────────────────────────────────────
    # Friday adds quips over Telegram with no approval card, so this is the
    # undo path: a phrase mangled by voice transcription can be deleted here
    # instead of by hand-editing friday_voice.yaml.

    @app.get("/api/quips")
    def api_quips() -> dict:
        return self_edit.list_phrases()

    @app.post("/api/quips")
    def api_quips_add(payload: dict = Body(...)) -> dict:
        result = self_edit.add_phrase(payload.get("text", ""),
                                      payload.get("target", "both"))
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "Could not add phrase"))
        return result

    @app.delete("/api/quips")
    def api_quips_remove(text: str = Query(...),
                         target: str = Query("both")) -> dict:
        result = self_edit.remove_phrase(text, target)
        if not result.get("ok"):
            raise HTTPException(404, result.get("error", "Could not remove phrase"))
        return result

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

    # ── Widgets: weather and markets ─────────────────────────────────────
    #
    # Both are sync defs, so FastAPI runs them in its threadpool rather than on
    # the event loop. That is load-bearing, not incidental: this server shares
    # the PTB loop with every Telegram handler and every scheduled job, and a
    # blocking requests.get on that loop stalls the whole agent — the exact
    # shape of the July 9 outage. Neither connector is async, and making them
    # async would mean a second HTTP client for no gain.
    #
    # Both connectors cache and both fail soft, so these routes are thin on
    # purpose. Neither touches SQLite and neither reaches a model.

    @app.get("/api/weather")
    def api_weather() -> dict:
        cfg = _load_config(config_path)
        return weather_conn.snapshot(cfg.get("weather") or {})

    @app.get("/api/stocks")
    def api_stocks() -> dict:
        cfg = _load_config(config_path)
        st = cfg.get("stocks") or {}
        if not st.get("enabled", True):
            return {"ok": False, "quotes": [], "stale": [], "disabled": True,
                    "error": "The markets widget is switched off."}
        return stocks_conn.quotes(st.get("tickers") or [])

    @app.post("/api/friday/restart")
    def api_friday_restart() -> dict:
        return restart_daemon()

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
    async def api_friday_brief() -> dict:
        # Runs the briefing in-process and pushes the result to Telegram.
        #
        # This used to POST the text "brief me" to sendMessage instead, on the
        # assumption it would come back around as a user message. It does not:
        # Telegram never delivers a bot's own messages to that bot through
        # getUpdates, so on_message() never fired, no briefing was ever
        # composed, and the button reported ok:true while doing nothing.
        #
        # We share friday.py's event loop, so the coroutine can simply be
        # awaited. It bypasses on_message()'s semaphore — same as the scheduled
        # briefing jobs, which are the precedent here — but takes its own lock
        # so repeat clicks queue instead of racing.
        if on_demand_briefing is None:
            raise HTTPException(
                503, "Briefings unavailable — dashboard is running standalone.")
        cfg = _load_config(config_path)
        tg = cfg.get("telegram", {})
        if not tg.get("bot_token") or not tg.get("chat_id"):
            raise HTTPException(400, "Telegram not configured.")
        try:
            text = await on_demand_briefing()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"On-demand briefing failed: {e}")
            raise HTTPException(500, f"Brief failed: {e}")
        return {"ok": True, "text": text}

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
            "last_message_at": state.get(conn, "last_message_at"),
            "last_message_preview": state.get(conn, "last_message_preview"),
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

    @app.get("/api/calendar/sync-status")
    def api_calendar_sync_status() -> dict:
        """Most-recent Google → Apple sync time per calendar, from synced_events.
        Powers the .last-sync slot on each Calendar-tab row."""
        last_sync: dict[str, str] = {}
        try:
            for name, ts in conn.execute(
                "SELECT calendar_name, MAX(synced_at) FROM synced_events "
                "GROUP BY calendar_name"
            ).fetchall():
                if name:
                    last_sync[name] = ts
        except Exception as e:
            logger.debug(f"sync-status query failed: {e}")
        return {"last_sync": last_sync}

    # ── Chat ─────────────────────────────────────────────────────────────────
    #
    # THE SAME PIPELINE TELEGRAM RUNS. Not a similar one — the same
    # channels/conversation.py::handle(), the same TURN_GATE, the same
    # conversation_history rows, the same effects entry point. Invariant 9
    # says the dashboard is a channel adapter and not a second chat
    # implementation, and the way that is enforced here is that there is
    # nothing below these lines to fork: everything this route knows how to do
    # is construct a DashboardChannel and hand it over.
    #
    # Async, on the shared loop, which is what lets it take the same gate as
    # the Telegram handler. A dashboard turn and a Telegram turn cannot
    # interleave against the same history.

    @app.get("/api/stream")
    async def api_stream() -> StreamingResponse:
        """The live channel: replies, cards appearing, cards resolved
        elsewhere. See dashboard/stream.py for why this is SSE and not a
        WebSocket, and why it is not a replay log.

        X-Accel-Buffering is for the proxy this does not have yet and will one
        day sit behind; without it an nginx in front would buffer the stream
        into uselessness.
        """
        return StreamingResponse(
            broadcaster.subscribe(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat")
    async def api_chat(payload: ChatIn) -> dict:
        from channels import conversation
        from channels.dashboard import DashboardChannel
        # The sink is the live stream; the events also come back on this
        # response. Both, deliberately: the stream is what makes a second tab
        # (or a card resolved from Telegram) update, and the response is what
        # makes the browser that asked immune to having missed it.
        channel = DashboardChannel(sink=broadcaster.publish)
        reply = await conversation.handle(
            payload.text, channel, conn, _load_config(config_path))
        # The events are what the channel was told to say, in order.
        return {
            "paused": reply.paused,
            "error_kind": reply.error_kind,
            "stopped_on": reply.stopped_on,
            "events": channel.events,
        }

    @app.get("/api/chat/history")
    def api_chat_history(limit: int = Query(60)) -> dict:
        """The transcript, oldest-first, UNFILTERED BY CHANNEL.

        A message typed in the dashboard and a message sent over Telegram are
        one conversation with one person. The channel column says where a line
        came from; it is not a partition, and rendering only this surface's
        half would show the user a conversation they never had.
        """
        rows = conn.execute(
            "SELECT role, content, created_at, channel FROM conversation_history "
            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
        return {"messages": [
            {"role": r[0], "content": r[1], "at": r[2], "channel": r[3] or ""}
            for r in reversed(rows)
        ]}

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
            # THE SAME SENTENCE TELEGRAM GIVES. A card resolved in the other
            # channel leaves a live-looking button here; the tap has to fail
            # closed AND say why. A bare status code was the silent half.
            from effects import pending as _pending
            raise HTTPException(409, _pending.refusal_message(status))

        # THE CHANNEL THAT HANDLED THE TAP IS THE CHANNEL THAT ANSWERS.
        #
        # This used to build a send-only TelegramHandler, so a card confirmed
        # in the dashboard produced a confirmation that went to Telegram. On
        # school Wi-Fi — the entire reason the dashboard exists — that
        # confirmation went nowhere at all: the user tapped Confirm, the event
        # was written, and nothing came back. Worse, the write's own history
        # row was the one message they never saw.
        #
        # effects/entry.py takes effects and a channel for exactly this. The
        # confirm path has always been channel-agnostic; it was only ever
        # handed a Telegram.
        from channels.dashboard import DashboardChannel
        channel = DashboardChannel(sink=broadcaster.publish)

        if action_type == "tool_call":
            # INVARIANT 9: the dashboard is a channel adapter, not a second
            # chat implementation. It resolves through exactly the same
            # effects/pending.py the Telegram button does — same executor, same
            # stored arguments, same idempotency check — and differs only in
            # which channel the confirmation is sent to.
            if verb == "edit":
                # Editing a tool call's arguments is a real feature and not
                # this step's. Refused rather than half-implemented: an edit
                # that silently dropped a field would change what the user
                # approved, which is the one thing this whole path protects.
                raise HTTPException(
                    400, "Editing a tool proposal isn't supported yet — "
                         "cancel it and ask again.")
            from effects import pending
            fn = pending.confirm if verb == "confirm" else pending.cancel
            status = fn(pid, conn, channel)
            # A stale card is re-proposed rather than run — the row is
            # superseded and a NEW card comes back on this response and on the
            # stream. Reported as not-ok because nothing was written.
            _publish_pending(broadcaster, pid, status)
            return {"ok": status in ("confirmed", "cancelled"),
                    "status": status, "events": channel.events}

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
            ok = cal_action.confirm_pending(pid, conn, channel)
            _publish_pending(broadcaster, pid, "confirmed" if ok else "failed")
            return {"ok": bool(ok), "status": "confirmed" if ok else "failed",
                    "events": channel.events}

        cal_action.cancel_pending(pid, conn, channel)
        _publish_pending(broadcaster, pid, "cancelled")
        return {"ok": True, "status": "cancelled", "events": channel.events}

    # ── Schedule and the period card ─────────────────────────────────────────
    #
    # THE SERVER ANSWERS WHAT IT KNOWS AND THE BROWSER ANSWERS WHAT TIME IT IS.
    # This endpoint returns the schedule, today's letter and the assignments —
    # facts that change at most once a day — and never "which period is it
    # now". The client computes that from the clock it already has, so the card
    # stays correct when the daemon is unreachable, which on school Wi-Fi is
    # most of the day. See app.js's period card for the other half.

    @app.get("/api/canvas/courses")
    def api_canvas_courses() -> dict:
        """The cached course list for the picker. Reads SQLite, never Canvas —
        see connectors/canvas.py on why the network is poll-cycle only."""
        from connectors import canvas as canvas_connector
        return {
            "courses": canvas_connector.courses(conn),
            "cache": canvas_connector.cache_status(conn),
        }

    @app.get("/api/schedule")
    def api_schedule() -> dict:
        """Everything the period card needs except the current time.

        Assignments come back grouped by course id so the card can render a
        period without scanning a flat list, and every course the schedule
        names gets a key even when it has nothing posted — an empty list is
        "nothing posted yet", which early in a semester is most of them, and
        a missing key is indistinguishable from a bug.
        """
        from connectors import canvas as canvas_connector
        cfg = _load_config(config_path)
        sched = cfg.get("schedule") or {}
        today = date.today()
        letter = schedule.letter_for(sched, today)

        cycle = sched.get("ab_cycle") or {}
        override = cycle.get("manual_override")
        override_live = bool(override) and (
            not cycle.get("manual_override_date")
            or str(cycle.get("manual_override_date")) == today.isoformat())
        if override_live:
            source = "override"
        elif letter:
            source = "counted"
        else:
            source = "unresolved"

        courses = {c["id"]: c for c in canvas_connector.courses(conn)}
        periods = schedule.periods_with_courses(sched, letter)

        # Every course id the schedule mentions, resolved or not — an
        # unresolved alternating period needs both of its courses' work.
        named: set[str] = set()
        for p in periods:
            if p.get("course_id"):
                named.add(str(p["course_id"]))
            for a in p.get("alternates") or []:
                if a.get("course_id"):
                    named.add(str(a["course_id"]))

        by_course: dict[str, list] = {cid: [] for cid in named}
        if named:
            horizon = (today + timedelta(days=21)).isoformat()
            for a in canvas_connector.assignments(
                conn, course_ids=sorted(named),
                due_after=today.isoformat(), due_before=horizon, limit=500,
            ):
                by_course.setdefault(str(a["course_id"]), []).append(a)

        return {
            "schedule": sched,
            "today": today.isoformat(),
            "is_school_day": schedule.is_school_day(today),
            "letter": letter,
            "letter_source": source,
            "periods": periods,
            "courses": courses,
            "assignments_by_course": by_course,
            "cache": canvas_connector.cache_status(conn),
        }

    @app.post("/api/commitment")
    def api_commitment(payload: dict = Body(...)) -> dict:
        """Quick-add a commitment from the after-school card.

        NO NEW WRITE PATH. This calls the SAME add_calendar_event tool the
        model calls, and ships its effects through the SAME effects/entry.py
        door, so it produces the same permission card, staged in the same
        pending_actions row, resolvable from either surface by the same
        button. The endpoint's entire job is to turn four form fields into the
        tool's arguments.

        It exists because extracurriculars are unpredictable one-offs entered
        constantly, and switching to chat to add the thing you are looking at
        is friction — not because the write needed to be different. If this
        ever needs its own write, that is the signal to stop, not to add one.

        The tool is what forces the card: it overrides policy/gating.py's AUTO
        for a user-stated create. Nothing here can skip it, and nothing here
        should be able to.
        """
        from channels.dashboard import DashboardChannel
        from effects import entry as effects_entry
        from tools.calendar_write import add_calendar_event
        from tools.types import ToolError as _ToolError

        title = str(payload.get("title") or "").strip()
        day = str(payload.get("date") or "").strip()
        if not title:
            raise HTTPException(400, "A commitment needs a title.")
        if not day:
            day = date.today().isoformat()

        out = add_calendar_event(
            title=title,
            date=day,
            start_time=str(payload.get("start_time") or "").strip(),
            end_time=str(payload.get("end_time") or "").strip(),
            calendar=str(payload.get("calendar") or "").strip(),
        )
        # The tool validates its own arguments — a bad time never reaches the
        # calendar, and the message it produces is the one the model would
        # have been given.
        if isinstance(out, _ToolError):
            raise HTTPException(400, out.message)

        channel = DashboardChannel(sink=broadcaster.publish)
        effects_entry.deliver(out.effects, channel, conn)
        return {"ok": True, "events": channel.events}

    @app.get("/api/after-school")
    def api_after_school() -> dict:
        """The card the period card becomes after the last bell.

        Three sections, ordered by how much choice there is about them: fixed
        commitments, then what is due, then what is left. The last one is the
        point — "you have 90 minutes" is the thing currently discovered at
        11pm.
        """
        from connectors import canvas as canvas_connector
        cfg = _load_config(config_path)
        sched = cfg.get("schedule") or {}
        now = datetime.now()
        today = now.date()

        bedtime_raw = str(sched.get("bedtime") or "23:00")
        try:
            bh, bm = (int(x) for x in bedtime_raw.split(":", 1))
            bedtime = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
        except ValueError:
            bedtime = now.replace(hour=23, minute=0, second=0, microsecond=0)

        # ── Fixed commitments: the same read the model uses ───────────────────
        # get_schedule is the registered tool; the decorator returns the
        # function unchanged, so this is the one calendar read path rather
        # than a second one that could disagree with it. Extracurriculars are
        # ordinary calendar events and need no model of their own.
        commitments: list[dict] = []
        cal_error = ""
        try:
            from tools.calendar_read import get_schedule
            from tools.types import ToolError as _ToolError
            out = get_schedule(date_from=today.isoformat(),
                               date_to=today.isoformat())
            if isinstance(out, _ToolError):
                cal_error = out.message
            else:
                for e in out.data.get("events", []):
                    if e.get("all_day"):
                        continue        # an all-day event blocks no time
                    try:
                        start = datetime.fromisoformat(e["start"])
                        end = datetime.fromisoformat(e["end"])
                    except (ValueError, KeyError):
                        continue
                    if end <= now:
                        continue        # already over
                    commitments.append({
                        "title": e.get("title", ""),
                        "calendar": e.get("calendar", ""),
                        "location": e.get("location", ""),
                        "start": start.isoformat(timespec="minutes"),
                        "end": end.isoformat(timespec="minutes"),
                        "minutes": int((end - start).total_seconds() // 60),
                    })
        except Exception as e:
            cal_error = str(e)
            logger.debug(f"after-school calendar read failed: {e}")
        commitments.sort(key=lambda c: c["start"])

        # ── Due tonight or tomorrow ───────────────────────────────────────────
        # The upper bound is the START of the day after tomorrow, not
        # tomorrow's 23:59:59. due_at now carries a local offset
        # ("2026-08-13T23:59:59-05:00") and these are string comparisons: a
        # longer string sharing a prefix sorts GREATER, so a <= against
        # "2026-08-13T23:59:59" would exclude an assignment due at exactly
        # 11:59pm — which is when essentially every Canvas assignment is due.
        due = [a for a in canvas_connector.assignments(
                   conn, due_after=today.isoformat(),
                   due_before=(today + timedelta(days=2)).isoformat(), limit=100)
               if not a.get("submitted")]
        courses = {c["id"]: c for c in canvas_connector.courses(conn)}
        for a in due:
            c = courses.get(str(a["course_id"]))
            a["course_name"] = c["name"] if c else ""

        # ── The free-time math ────────────────────────────────────────────────
        #
        # MEASURED FROM THE END OF THE LAST COMMITMENT, not from now and not
        # from the final bell. "You have four hours" is wrong if practice runs
        # until 5:30, and it is wrong in the direction that gets someone to
        # 11pm with the work not started.
        last_end = now
        for c in commitments:
            end = datetime.fromisoformat(c["end"])
            if end > last_end:
                last_end = end
        free_minutes = int((bedtime - last_end).total_seconds() // 60)
        committed = sum(c["minutes"] for c in commitments)

        return {
            "now": now.isoformat(timespec="minutes"),
            "bedtime": bedtime.isoformat(timespec="minutes"),
            "commitments": commitments,
            "calendar_error": cal_error,
            "due": due,
            "free_minutes": free_minutes,
            "free_from": last_end.isoformat(timespec="minutes"),
            "committed_minutes": committed,
            # Stated rather than left for the client to infer from a sign: the
            # negative case is the single most useful thing this card reports.
            "overcommitted": free_minutes < 0,
            "cache": canvas_connector.cache_status(conn),
        }

    @app.post("/api/schedule/override")
    def api_schedule_override(payload: dict = Body(default={})) -> dict:
        """Force today's letter, or clear the force.

        THE DATE IS STAMPED HERE, NOT SUPPLIED BY THE CALLER. An override is
        only ever for today; letting a client name the day would be letting it
        set one that never expires, which is the failure the expiry exists to
        prevent.
        """
        letter = payload.get("letter")
        cfg = _load_config(config_path)
        sched = cfg.setdefault("schedule", {})
        cycle = sched.setdefault("ab_cycle", {})
        pattern = [str(p).upper() for p in (cycle.get("pattern") or ["A", "B"])]

        if letter in (None, "", "clear"):
            cycle["manual_override"] = None
            cycle["manual_override_date"] = None
        else:
            letter = str(letter).strip().upper()
            if letter not in pattern:
                raise HTTPException(
                    400, f"{letter!r} is not one of {pattern}.")
            cycle["manual_override"] = letter
            cycle["manual_override_date"] = date.today().isoformat()

        try:
            _save_config_atomic(config_path, cfg)
        except Exception as e:
            raise HTTPException(500, f"Could not write config: {e}")
        return {"ok": True,
                "letter": schedule.letter_for(sched, date.today()),
                "override": cycle["manual_override"]}

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

    # ── Passing-period wake schedule ────────────────────────────────────────
    # Everything here READS through power.py's own combined_blocks/active_hold
    # (never re-derives locally, which would be a second copy of the
    # bell-schedule-gap logic free to disagree with the one power_reconcile_job
    # actually acts on) and WRITES only the two things that are not ordinary
    # config edits: the manual hold (a timestamp, not a config value) and
    # nothing else — custom_blocks and derived_overrides are plain
    # wake_schedule keys, edited the same way GroupMe groups and schedule
    # periods are: mutate CONFIG client-side, POST the whole document.

    @app.get("/api/power/status")
    def api_power_status() -> dict:
        cfg = _load_config(config_path)
        now = datetime.now()
        hold = power.active_hold(cfg, conn, now)
        return {
            "enabled": bool((cfg.get("wake_schedule") or {}).get("enabled", False)),
            "active": hold["active"],
            "reason": hold.get("reason"),
            "block": hold.get("block"),
            "manual": power.manual_hold_status(conn, now),
            "battery": power.battery_status(),
            "next_wake": power.next_scheduled_wake(conn, now),
            "blocks_today": power.combined_blocks(cfg, now.date()),
            "blocks_tomorrow": power.combined_blocks(cfg, now.date() + timedelta(days=1)),
            "sudoers_line": power.sudoers_line(),
            # Best-effort only — see read_disablesleep()'s docstring on why
            # this is informational and never what reconcile() acts on.
            "disablesleep_reported": power.read_disablesleep(),
            "sudo_configured": power.sudo_configured(),
        }

    @app.post("/api/power/manual")
    def api_power_manual(payload: PowerManualRequest) -> dict:
        if payload.clear:
            power.clear_manual_hold(conn)
            return {"ok": True, "active": False}
        minutes = payload.minutes or 0
        if minutes <= 0:
            raise HTTPException(400, "minutes must be positive.")
        if minutes > 480:
            raise HTTPException(400, "That's a long hold — cap it at 8 hours (480 minutes).")
        until = power.set_manual_hold(conn, minutes, reason="dashboard")
        return {"ok": True, "active": True, "until": until}

    return app


# ── Lifecycle helper called from friday.py ───────────────────────────────────

def _tailscale_address() -> str | None:
    """This machine's tailnet IPv4 address, or None if Tailscale isn't
    installed, isn't running, or isn't logged in.

    Resolved live via the CLI rather than read from config: the address can
    change (re-auth, a different tailnet) and a stale value saved once would
    bind to an address that no longer belongs to this machine — or silently
    stop binding to the one that does. tls_certs.run() carries the PATH
    fallback (see its module docstring for the LaunchAgent PATH trap).
    """
    result = tls_certs.run(["ip", "-4"])
    if result is None:
        logger.info("Tailscale address not available; dashboard stays on loopback only.")
        return None
    addr = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
    return addr or None


async def start_server(config_path: Path, conn: sqlite3.Connection,
                       host: str = "127.0.0.1", port: int = 5174,
                       on_demand_briefing: Callable[[], Awaitable[str]] | None = None
                       ) -> list[tuple[uvicorn.Server, list[socket.socket]]]:
    """Build the FastAPI app, wrap in uvicorn, return one (Server, sockets)
    pair per protocol group — the caller schedules each as its own
    server.serve(sockets=sockets) task and keeps the list for clean
    shutdown.

    Binds `host` (loopback, always plain HTTP — sitting at the Mac must
    never be forced onto TLS) and, when Tailscale is running, this
    machine's tailnet address — specifically, never 0.0.0.0, so nothing on
    school Wi-Fi or any other network this machine joins can see the port.
    Loopback stays bound alongside it rather than being replaced: a socket
    bound only to the tailnet address is unreachable from 127.0.0.1, and
    menubar.py / mac_app.py / tray.py all poll that address.

    The tailnet bind is HTTPS when a Tailscale-issued cert is available
    (tls_certs.ensure_cert) and plain HTTP otherwise. That split exists for
    the offline dashboard: a service worker refuses to register over
    anything but localhost or HTTPS, so the phone needs a real cert, not
    just the network-level privacy Tailscale already gave it. A single
    uvicorn.Server applies one SSL setting to every socket it serves, so
    HTTP-loopback and HTTPS-tailnet have to be two Server instances (two
    groups) rather than two sockets under one.

    on_demand_briefing composes a briefing and sends it to Telegram; friday.py
    supplies it so /api/friday/brief can run the real thing on the shared loop.
    Omitted, the Brief button reports 503 rather than silently doing nothing.
    """
    app = create_app(config_path, conn, started_at=datetime.now(),
                     on_demand_briefing=on_demand_briefing, port_hint=port)

    http_hosts = [host]
    tailnet_ip = _tailscale_address()
    https_host: str | None = None
    cert: tuple[Path, Path] | None = None
    if tailnet_ip:
        dom = tls_certs.domain()
        cert = tls_certs.ensure_cert(dom) if dom else None
        if cert:
            https_host = tailnet_ip
        elif tailnet_ip not in http_hosts:
            # No cert available — fall back to the old plain-HTTP tailnet
            # bind rather than dropping tailnet reachability entirely.
            http_hosts.append(tailnet_ip)

    groups: list[tuple[uvicorn.Server, list[socket.socket]]] = []

    http_cfg = uvicorn.Config(
        app,
        host=http_hosts[0],
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    http_sockets = [http_cfg.bind_socket()]
    for extra_host in http_hosts[1:]:
        extra_cfg = uvicorn.Config(app, host=extra_host, port=port, log_level="warning")
        http_sockets.append(extra_cfg.bind_socket())
    groups.append((uvicorn.Server(http_cfg), http_sockets))
    for h in http_hosts:
        logger.info(f"Dashboard server starting on http://{h}:{port}")

    if https_host and cert:
        cert_path, key_path = cert
        https_cfg = uvicorn.Config(
            app,
            host=https_host,
            port=port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="warning",
            access_log=False,
            loop="asyncio",
        )
        https_sockets = [https_cfg.bind_socket()]
        groups.append((uvicorn.Server(https_cfg), https_sockets))
        logger.info(f"Dashboard server starting on https://{https_host}:{port}")

    return groups
