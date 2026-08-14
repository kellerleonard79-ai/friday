"""
power.py
Passing-period wake scheduling: keeping the Mac awake with the lid shut
during the ~5-minute windows derived from the bell schedule (schedule.py),
and — the rest of the day — leaving it free to sleep.

TWO SEPARATE MECHANISMS, because macOS makes them separate:
  - staying awake with the lid shut on battery needs `sudo pmset -a
    disablesleep 1`, held only while a block is active and cleared the
    moment it ends. Normal wake assertions do not override clamshell sleep
    on battery; only this does.
  - waking a SLEEPING Mac needs one-off `sudo pmset schedule wakeorpoweron
    "<date/time>"` entries, materialized a few days ahead by a daily job,
    because `pmset repeat` supports exactly one repeating wake and one
    repeating sleep — nowhere near seven blocks a day.

Both need root. See sudoers_line() for the narrowest NOPASSWD rule this
needs. This module never edits /etc/sudoers or runs visudo — that line is
reported for Keller to add by hand.

NEVER LEFT ON. disablesleep is reconciled on a minute-resolution job
(reconcile()) against current wall-clock state rather than scheduled
per-block with timers, so it self-heals after a sleep, a crash, or a config
edit mid-block — and it is cleared unconditionally on daemon start and on
daemon shutdown (startup_clear / shutdown_clear), so a flag stranded by a
previous run or a killed process cannot survive past the next boot. A
machine that cannot sleep because of a stranded config flag is a dead
battery in a backpack, and that failure mode is worse than a missed wake.

OWNERSHIP OF SCHEDULED WAKES. pmset's scheduled-event list carries no tag or
label this code controls — it is just a (type, timestamp) pair, and cancel
matches on that pair alone. So "only remove entries this feature created" is
enforced by never touching anything pmset itself reports: this module keeps
its OWN record of which timestamps it created (system_state key
"power.materialized_wakes", a JSON list) and only ever cancels timestamps
that appear in that record. If something else schedules a wake at the exact
same minute this module would have chosen, cancelling our own entry later
would take theirs too — an acknowledged, documented limitation of what
pmset's API exposes, not something this module can detect or avoid.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import date as date_cls, datetime, timedelta

import compat
import schedule
import memory.state as state

logger = logging.getLogger("friday.power")

_DISABLESLEEP_RE = re.compile(r"^\s*disablesleep\s+(\d+)", re.MULTILINE)
_BATT_PCT_RE = re.compile(r"(\d{1,3})%")
_BATT_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")

_MATERIALIZED_KEY = "power.materialized_wakes"
_MANUAL_HOLD_KEY = "power.manual_hold_until"
_MANUAL_REASON_KEY = "power.manual_hold_reason"

_PMSET_DATETIME_FMT = "%m/%d/%y %H:%M:%S"

# Rate-limits the "sudoers rule missing" warning to once per failure streak
# rather than once a minute — the reconcile job runs every 60s and would
# otherwise flood the log with the same diagnosis.
_warned_no_sudo = False


def _pmset_path() -> str:
    return shutil.which("pmset") or "/usr/bin/pmset"


def sudoers_line() -> str:
    """The narrowest NOPASSWD rule this feature needs. Reported, never
    applied — see the module docstring. Wildcards are required on the
    schedule subcommands because the timestamp argument is different on
    every call; sudo matches command lines with shell-style globbing, so `*`
    here is deliberate and is as narrow as the schedule commands allow."""
    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    p = _pmset_path()
    return (
        f"{user} ALL=(root) NOPASSWD: "
        f"{p} -a disablesleep 0, {p} -a disablesleep 1, "
        f"{p} schedule wakeorpoweron *, {p} schedule cancel wakeorpoweron *"
    )


def _pmset_sudo(args: list[str]) -> bool:
    """Runs `sudo -n pmset <args>`. -n (non-interactive) is load-bearing: a
    missing sudoers rule must fail in under a second, not hang the
    minute-resolution reconcile job on a password prompt nobody can answer
    from a headless daemon."""
    global _warned_no_sudo
    if not compat.IS_MACOS:
        return False
    cmd = ["sudo", "-n", _pmset_path(), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.warning(f"power: pmset invocation failed: {e}")
        return False
    if r.returncode != 0:
        if not _warned_no_sudo:
            logger.warning(
                f"power: 'sudo pmset {' '.join(args)}' failed "
                f"({r.stderr.strip() or r.returncode}) — the sudoers rule is "
                f"probably missing. See power.sudoers_line() for the line to "
                f"add. Until it is, the wake schedule cannot hold the "
                f"machine awake or wake it."
            )
            _warned_no_sudo = True
        return False
    _warned_no_sudo = False
    return True


def sudo_configured() -> bool:
    """Whether the most recent `sudo pmset ...` call succeeded. Best-effort
    and in-process only (resets to True on restart, so a fresh process gives
    the sudoers rule the benefit of the doubt until it actually tries and
    fails) — this is what the dashboard's status panel uses to decide
    whether to show the setup hint, rather than re-deriving the same
    diagnosis from a status call of its own."""
    return not _warned_no_sudo


def read_disablesleep() -> bool | None:
    """Best-effort disablesleep state for STATUS DISPLAY ONLY — never gate a
    safety decision on this. `disablesleep` is undocumented (absent from
    `man pmset`) and, measured on this machine, does not appear in
    `pmset -g` / `-g custom` / `-g live` at all at its default value; whether
    it appears once set to 1 was not verified here (verifying required the
    sudoers rule this module reports but does not install). A None return
    is therefore ambiguous between "definitely 0" and "set, but this parse
    can't see it" — which is exactly why startup_clear(), shutdown_clear()
    and reconcile() do not read-then-set: they always issue the command for
    the state they want, unconditionally. A `pmset -a disablesleep 0` call
    when it is already 0 is a harmless no-op; a skipped clear because a read
    silently missed a stuck 1 is a dead battery in a backpack."""
    if not compat.IS_MACOS:
        return None
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception as e:
        logger.debug(f"power: could not read pmset -g: {e}")
        return None
    m = _DISABLESLEEP_RE.search(out)
    return bool(int(m.group(1))) if m else None


def set_disablesleep(enabled: bool) -> bool:
    return _pmset_sudo(["-a", "disablesleep", "1" if enabled else "0"])


def battery_status() -> dict:
    """{"percent": int|None, "source": "AC Power"|"Battery Power"|None}.
    Best-effort — a machine with no battery (or pmset unavailable) reports
    both as None rather than raising."""
    if not compat.IS_MACOS:
        return {"percent": None, "source": None}
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception as e:
        logger.debug(f"power: could not read pmset -g batt: {e}")
        return {"percent": None, "source": None}
    pm = _BATT_PCT_RE.search(out)
    sm = _BATT_SOURCE_RE.search(out)
    return {
        "percent": int(pm.group(1)) if pm else None,
        "source": sm.group(1) if sm else None,
    }


# ── Blocks: derived + custom, combined and resolved for one day ──────────────

def _derived_id(block: dict) -> str:
    return f"derived:{block['start']}-{block['end']}"


def combined_blocks(cfg: dict, day: date_cls) -> list[dict]:
    """Every wake block for `day` — derived from the bell schedule plus any
    custom blocks — each carrying `enabled` (per-block, see wake_schedule.
    derived_overrides and each custom block's own `enabled` key) and a
    stable `id` the dashboard and materialize_wakes() key off. Sorted by
    start time. Does not itself check the master on/off switch — callers
    that need that check `wake_schedule.enabled` themselves, because "give
    me the blocks" and "should any of them run" are different questions
    (mirrors policy/'s gating-vs-visibility split)."""
    sched = cfg.get("schedule") or {}
    wake_cfg = cfg.get("wake_schedule") or {}
    lead = int(wake_cfg.get("lead_minutes", 2))
    max_gap = int(wake_cfg.get("max_passing_minutes", schedule.DEFAULT_MAX_PASSING_MINUTES))
    overrides = wake_cfg.get("derived_overrides") or {}

    out: list[dict] = []
    for b in schedule.passing_period_blocks(sched, day, lead_minutes=lead,
                                            max_gap_minutes=max_gap):
        key = f"{b['start']}-{b['end']}"
        out.append({**b, "source": "derived", "id": _derived_id(b),
                    "enabled": bool(overrides.get(key, True))})

    weekday = day.weekday()
    for i, c in enumerate(wake_cfg.get("custom_blocks") or []):
        days = c.get("weekdays")
        if days is not None and weekday not in days:
            continue
        s = schedule._parse_hhmm(c.get("start"))
        e = schedule._parse_hhmm(c.get("end"))
        if not s or not e or s >= e:
            continue
        wake_dt = datetime.combine(day, s) - timedelta(minutes=lead)
        out.append({
            "start": c.get("start"), "end": c.get("end"),
            "wake_at": wake_dt.time().strftime("%H:%M"),
            "label": c.get("label") or f"Custom block {i + 1}",
            "source": "custom", "id": f"custom:{i}",
            "enabled": bool(c.get("enabled", True)),
        })

    out.sort(key=lambda b: b["start"])
    return out


def _block_covers(block: dict, day: date_cls, now: datetime) -> bool:
    """True from wake_at (the early wake) through the block's own end — not
    just start..end — because the hold has to already be in effect by the
    time the OS wakes the machine, or a lid-closed wake goes straight back
    to sleep before this process gets a chance to set it."""
    wake_at = schedule._parse_hhmm(block.get("wake_at"))
    end = schedule._parse_hhmm(block.get("end"))
    if not wake_at or not end:
        return False
    start_dt = datetime.combine(day, wake_at)
    end_dt = datetime.combine(day, end)
    return start_dt <= now < end_dt


# ── Manual override ───────────────────────────────────────────────────────

def set_manual_hold(conn: sqlite3.Connection, minutes: int, reason: str = "") -> str:
    """Force the hold on for `minutes` from now. Expiry is a stored
    timestamp, not a timer — a timer dies with the process, and a hold that
    silently outlived a restart would be the exact stranded-flag failure
    this module exists to prevent."""
    until = (datetime.now() + timedelta(minutes=minutes)).isoformat()
    state.set(conn, _MANUAL_HOLD_KEY, until)
    state.set(conn, _MANUAL_REASON_KEY, reason or "manual")
    return until


def clear_manual_hold(conn: sqlite3.Connection) -> None:
    state.delete(conn, _MANUAL_HOLD_KEY)
    state.delete(conn, _MANUAL_REASON_KEY)


def manual_hold_status(conn: sqlite3.Connection, now: datetime | None = None) -> dict | None:
    now = now or datetime.now()
    raw = state.get(conn, _MANUAL_HOLD_KEY)
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if now >= until:
        return None
    return {"until": raw, "reason": state.get(conn, _MANUAL_REASON_KEY) or "manual"}


# ── Reconciliation: is the hold supposed to be on right now? ─────────────────

def active_hold(cfg: dict, conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Whether disablesleep should be on right now, and why. Evaluated fresh
    against wall-clock state on every call rather than driven by per-block
    timers — see the module docstring on why that self-heals.

    The manual hold is checked BEFORE the master switch, deliberately: it is
    described as "overriding the schedule," and a toggle a user just pressed
    that silently no-ops because the schedule happens to be off is a toggle
    that looks broken. The master switch governs the derived/custom blocks
    it names; it was never meant to gate an explicit, one-off request."""
    now = now or datetime.now()

    manual = manual_hold_status(conn, now)
    if manual:
        return {"active": True, "reason": "manual", "block": None,
                "expires_at": manual["until"]}

    wake_cfg = cfg.get("wake_schedule") or {}
    if not wake_cfg.get("enabled", True):
        return {"active": False, "reason": None, "block": None}

    today = now.date()
    for b in combined_blocks(cfg, today):
        if b.get("enabled", True) and _block_covers(b, today, now):
            return {"active": True, "reason": b["id"], "block": b}

    return {"active": False, "reason": None, "block": None}


_COMMANDED_KEY = "power.disablesleep_commanded"


def _last_commanded(conn: sqlite3.Connection) -> bool | None:
    """What THIS module last successfully told pmset to set disablesleep to
    — the source of truth for "changed" detection and dashboard status,
    deliberately not `read_disablesleep()`. Persisted (system_state) rather
    than in-process, so a restart does not forget it mid-block and log a
    spurious "changed" on the first tick after."""
    raw = state.get(conn, _COMMANDED_KEY)
    if raw is None:
        return None
    return raw == "1"


def _set_and_record(conn: sqlite3.Connection, enabled: bool) -> bool:
    ok = set_disablesleep(enabled)
    if ok:
        state.set(conn, _COMMANDED_KEY, "1" if enabled else "0")
    return ok


def reconcile(cfg: dict, conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The minute-resolution job body. Sets disablesleep to match
    active_hold()'s verdict and nothing else — no scheduling, no wake
    materialization.

    UNCONDITIONAL, not read-then-set: every tick issues `pmset -a
    disablesleep <desired>` regardless of what read_disablesleep() reports,
    because that read cannot be trusted to see a stuck 1 (see its
    docstring). Setting a value it already has is a harmless no-op on
    pmset's side; skipping a needed clear because a read missed it is not.
    `changed` is computed against this module's own record of what it last
    commanded, which is the only state here actually under our control."""
    hold = active_hold(cfg, conn, now)
    previous = _last_commanded(conn)
    ok = _set_and_record(conn, hold["active"])
    return {**hold, "disablesleep": hold["active"] if ok else previous,
           "changed": ok and previous != hold["active"], "commanded_ok": ok}


def startup_clear(conn: sqlite3.Connection) -> None:
    """Clear disablesleep unconditionally on daemon start — not gated on
    read_disablesleep(), for the same reason reconcile() is not: a flag
    stranded by a previous run's crash must not survive because a read
    happened not to see it. Deliberately does NOT touch the
    materialized-wakes record — those are OS-level scheduled events meant to
    survive a daemon restart (that is the whole point of scheduling them
    ahead rather than per-block), so forgetting them here would mean losing
    a real wake on every restart. materialize_wakes() reconciles that record
    on its own schedule."""
    if not compat.IS_MACOS:
        return
    if _set_and_record(conn, False):
        logger.info("power: disablesleep cleared at startup (clearing any stranded flag).")
    else:
        logger.warning("power: could not clear disablesleep at startup — "
                       "see power.sudoers_line().")


def shutdown_clear(conn: sqlite3.Connection) -> None:
    if not compat.IS_MACOS:
        return
    _set_and_record(conn, False)


# ── Materializing wakeorpoweron entries ───────────────────────────────────

def _load_ours(conn: sqlite3.Connection) -> set[str]:
    raw = state.get(conn, _MATERIALIZED_KEY)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set()


def _save_ours(conn: sqlite3.Connection, stamps: set[str]) -> None:
    state.set(conn, _MATERIALIZED_KEY, json.dumps(sorted(stamps)))


def _desired_stamps(cfg: dict, now: datetime) -> set[str]:
    wake_cfg = cfg.get("wake_schedule") or {}
    desired: set[str] = set()
    if not wake_cfg.get("enabled", True):
        return desired   # master off — nothing desired, see materialize_wakes()
    days_ahead = int(wake_cfg.get("wake_days_ahead", 3))
    for d in range(days_ahead + 1):
        day = now.date() + timedelta(days=d)
        for b in combined_blocks(cfg, day):
            if not b.get("enabled", True):
                continue
            wake_at = schedule._parse_hhmm(b.get("wake_at"))
            if not wake_at:
                continue
            dt = datetime.combine(day, wake_at)
            if dt <= now:
                continue   # already passed — nothing to schedule
            desired.add(dt.strftime(_PMSET_DATETIME_FMT))
    return desired


def materialize_wakes(cfg: dict, conn: sqlite3.Connection,
                      now: datetime | None = None) -> dict:
    """The daily job body. Diffs the desired wakeorpoweron timestamps for the
    next `wake_schedule.wake_days_ahead` days against what this module
    previously recorded creating, cancels what is no longer desired, adds
    what is newly desired. Idempotent: run twice with no config change in
    between and both `added` and `cancelled` come back empty on the second
    run, because desired == ours already.

    Master off collapses `desired` to nothing, which cancels every wake this
    module owns and adds none — the same code path as a normal materialize,
    not a special case."""
    now = now or datetime.now()
    if not compat.IS_MACOS:
        return {"skipped": "not macOS", "added": [], "cancelled": [],
               "failed_add": [], "failed_cancel": []}

    desired = _desired_stamps(cfg, now)
    ours = _load_ours(conn)

    to_cancel = ours - desired
    to_add = desired - ours

    cancelled, failed_cancel = [], []
    for ts in sorted(to_cancel):
        if _pmset_sudo(["schedule", "cancel", "wakeorpoweron", ts]):
            cancelled.append(ts)
        else:
            failed_cancel.append(ts)

    added, failed_add = [], []
    for ts in sorted(to_add):
        if _pmset_sudo(["schedule", "wakeorpoweron", ts]):
            added.append(ts)
        else:
            failed_add.append(ts)

    # A failed cancel/add stays (or fails to leave) `ours` exactly where it
    # actually is, not where this run wanted it — next run's diff against
    # `desired` will retry it rather than silently drifting the record.
    now_ours = (ours - set(cancelled)) | set(added)
    _save_ours(conn, now_ours)

    return {"desired": sorted(desired), "added": added, "cancelled": cancelled,
           "failed_add": failed_add, "failed_cancel": failed_cancel}


def next_scheduled_wake(conn: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """The earliest wake this module still has recorded as ours and in the
    future — for the dashboard's "next scheduled wake" line. Not a fresh
    `pmset -g sched` read: that would also show anything else's scheduled
    events, which is a different question from "what will wake it for this
    feature.\""""
    now = now or datetime.now()
    ours = _load_ours(conn)
    upcoming = []
    for ts in ours:
        try:
            dt = datetime.strptime(ts, _PMSET_DATETIME_FMT)
        except ValueError:
            continue
        if dt > now:
            upcoming.append(dt)
    if not upcoming:
        return None
    return min(upcoming).isoformat()
