"""
power.py
Scheduling the Mac awake for the things Friday has to do while it is shut,
and — the rest of the time — leaving it free to sleep.

TWO PRODUCERS, GATED SEPARATELY, and the asymmetry is the design:

  - BRIEFING WAKES (briefing_wakes, default ON). The primary use. A briefing
    fired into a sleeping machine is worth nothing, so each scheduled
    briefing time gets a wake a few minutes ahead of it and a hold that ends
    the moment the briefing sends — see the "Briefing wakes" section below
    for why that release reads the briefing's own lock rather than a timer.
    Two wakes a day, each self-limiting.

  - PASSING-PERIOD BLOCKS (enabled, default OFF). Keeping the Mac reachable
    with the lid shut during the ~5-minute windows derived from the bell
    schedule (schedule.py). Opt-in because it is the half that costs real
    battery — seven holds a day rather than two — and the half whose value
    is speculative.

Turning the block schedule off does not touch briefing wakes. Folding them
under one switch would ship the feature that matters disabled behind a toggle
named for the one that does not.

TWO SEPARATE MECHANISMS, because macOS makes them separate:
  - staying awake with the lid shut on battery needs `sudo pmset -a
    disablesleep 1`, held only while a block is active and cleared the
    moment it ends. Normal wake assertions do not override clamshell sleep
    on battery; only this does.
  - waking a SLEEPING Mac needs one-off `sudo pmset schedule wakeorpoweron
    "<date/time>"` entries, materialized a few days ahead by a daily job,
    because `pmset repeat` supports exactly one repeating wake and one
    repeating sleep — nowhere near seven blocks a day.

Both need root, and THEY ARE GRANTED SEPARATELY AND FAIL SEPARATELY. On this
machine the installed rule covers `pmset schedule *` but not
`pmset -a disablesleep`, so wakes materialize and the hold never engages —
a real, useful, half-working state, not a contradiction. sudo_capabilities()
reports the two halves apart for that reason; a single "is sudo configured"
boolean describes this machine wrongly in both directions.

See sudoers_line() for the narrowest NOPASSWD rule this needs. This module
never edits /etc/sudoers or runs visudo — that line is reported for Keller to
add by hand.

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

# THE TWO HALVES FAIL SEPARATELY, AND ON THIS MACHINE THEY DO.
# The sudoers rule actually installed here grants `pmset schedule *` and
# `pmset -g sched` but NOT `pmset -a disablesleep` — so scheduled wakes work
# and the hold does not. A single boolean ("is sudo configured") reports that
# as a flat failure and sends the user to re-add a line that is already half
# right. Tracked per capability instead:
#
#   "wake" → pmset schedule wakeorpoweron / cancel   (materialize_wakes)
#   "hold" → pmset -a disablesleep                   (reconcile, *_clear)
#
# None means "not tried yet this process" and is deliberately distinct from
# False: a fresh process must not accuse the sudoers rule of being wrong
# before it has actually attempted anything.
_CAPS: dict[str, bool | None] = {"wake": None, "hold": None}


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


def _pmset_sudo(args: list[str], capability: str = "wake") -> bool:
    """Runs `sudo -n pmset <args>`. -n (non-interactive) is load-bearing: a
    missing sudoers rule must fail in under a second, not hang the
    minute-resolution reconcile job on a password prompt nobody can answer
    from a headless daemon.

    `capability` records which half of the feature this call proves or
    disproves — see _CAPS. It is recorded from the REAL call and never from
    `sudo -l`: with a `(ALL) ALL` line present (the ordinary admin default on
    macOS), `sudo -n -l /usr/bin/pmset -a disablesleep 1` reports the command
    as permitted even though running it prompts for a password. Verified on
    this machine — a `-l` probe called the hold available while the actual
    call failed. Only the attempt itself is evidence."""
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
        _CAPS[capability] = False
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
    _CAPS[capability] = True
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


def sudo_capabilities() -> dict:
    """Which half of the feature root actually permits, as observed by the
    real calls this process has made. `{"wake": True, "hold": False}` is a
    genuine and currently-live state, not a contradiction: the two need
    different sudoers commands and only one of them is granted here.

    None for either means untried this process. The dashboard renders that
    as "not verified yet" rather than as a fault — see sudo_configured()'s
    note on giving a fresh process the benefit of the doubt."""
    return dict(_CAPS)


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
    return _pmset_sudo(["-a", "disablesleep", "1" if enabled else "0"],
                       capability="hold")


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


# ── Briefing wakes ──────────────────────────────────────────────────────────
#
# THE PRIMARY USE, and the one that is not speculative: a briefing is worth
# nothing if it fires into a sleeping machine. Each scheduled briefing time
# gets a wakeorpoweron entry a few minutes ahead of it, and a hold that lasts
# only until the briefing actually sends.
#
# RELEASE IS BY THE BRIEFING'S OWN LOCK, NOT A TIMER. friday.py claims
# `last_<slot>_briefing_sent` (a date string in system_state) BEFORE it starts
# composing, and the missed-briefing catch-up net reads that same key to decide
# whether a slot still needs firing. Reading it here means the hold is released
# by the exact event that means "the briefing happened", with no second source
# of truth to drift, and it self-heals: reconcile() re-evaluates every 60s.
#
# The grace window is the backstop. A briefing that never sends (Telegram down,
# a compose that throws) releases its lock, and without an upper bound the hold
# would then last until midnight — the stranded-flag failure this module's
# docstring calls a dead battery in a backpack. Whichever comes first wins.
#
# NOT GATED BY wake_schedule.enabled. That switch governs the passing-period
# block schedule and defaults off; briefing wakes are the primary use and have
# their own switch, defaulting on. Folding them together would ship the feature
# that matters disabled behind a toggle named for the one that doesn't.

_BRIEFING_SENT_KEYS = {
    "morning": "last_morning_briefing_sent",
    "evening": "last_evening_briefing_sent",
}
_BRIEFING_LABELS = {"morning": "Morning briefing", "evening": "Evening briefing"}


def briefing_wake_times(cfg: dict, day: date_cls,
                        conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every briefing slot on `day`, each with the time it is due (`at`) and
    the time the machine should wake for it (`wake_at`). Sorted by wake time.

    A one-shot override in system_state (`<slot>_briefing_override`, set from
    the dashboard) replaces the configured time for the day it names. Only
    consulted when `conn` is given, so the pure-config callers stay pure."""
    agent_cfg = cfg.get("agent") or {}
    wake_cfg = cfg.get("wake_schedule") or {}
    lead = int(wake_cfg.get("briefing_lead_minutes",
                            wake_cfg.get("lead_minutes", 2)))

    configured = {
        "morning": agent_cfg.get("morning_briefing_time", "08:00"),
        "evening": agent_cfg.get("briefing_time", "21:45"),
    }

    out: list[dict] = []
    for slot in ("morning", "evening"):
        at = schedule._parse_hhmm(str(configured[slot]))
        if conn is not None:
            raw = state.get(conn, f"{slot}_briefing_override")
            if raw:
                try:
                    ovr = datetime.fromisoformat(raw)
                    if ovr.date() == day:
                        at = ovr.time().replace(second=0, microsecond=0)
                except ValueError:
                    pass
        if not at:
            continue
        wake_dt = datetime.combine(day, at) - timedelta(minutes=lead)
        if wake_dt.date() != day:
            # A lead that walks the wake back past midnight would schedule it
            # on the wrong day; clamp rather than silently wake at 23:5x the
            # evening before.
            wake_dt = datetime.combine(day, at)
        out.append({
            "slot": slot,
            "label": _BRIEFING_LABELS[slot],
            "at": at.strftime("%H:%M"),
            "wake_at": wake_dt.time().strftime("%H:%M"),
        })
    out.sort(key=lambda b: b["wake_at"])
    return out


def briefing_wakes_enabled(cfg: dict) -> bool:
    return bool((cfg.get("wake_schedule") or {}).get("briefing_wakes", True))


def _briefing_hold(cfg: dict, conn: sqlite3.Connection,
                   now: datetime) -> dict | None:
    """The hold covering a briefing that is due and has not sent yet. None the
    moment it sends — which is what lets the machine go back to sleep instead
    of staying up for the rest of the window."""
    if not briefing_wakes_enabled(cfg):
        return None
    wake_cfg = cfg.get("wake_schedule") or {}
    grace = int(wake_cfg.get("briefing_hold_grace_minutes", 10))
    today = now.date()
    for b in briefing_wake_times(cfg, today, conn):
        start = datetime.combine(today, schedule._parse_hhmm(b["wake_at"]))
        end = datetime.combine(today, schedule._parse_hhmm(b["at"])) + \
            timedelta(minutes=grace)
        if not (start <= now < end):
            continue
        if state.get(conn, _BRIEFING_SENT_KEYS[b["slot"]]) == today.isoformat():
            continue   # already sent — release, let it sleep
        return {"active": True, "reason": f"briefing:{b['slot']}",
                "block": {**b, "source": "briefing", "id": f"briefing:{b['slot']}",
                          "start": b["wake_at"], "end": b["at"], "enabled": True},
                "expires_at": end.isoformat()}
    return None


# ── Blocks: derived + custom, combined and resolved for one day ──────────────

def _derived_id(block: dict) -> str:
    return f"derived:{block['start']}-{block['end']}"


def combined_blocks(cfg: dict, day: date_cls) -> list[dict]:
    """Every wake block for `day` — derived from the bell schedule plus any
    custom blocks — each carrying `enabled` (per-block, see wake_schedule.
    derived_overrides and each custom block's own `enabled` key) and a
    stable `id` the dashboard and materialize_wakes() key off. Sorted by
    start time. Does not itself check the master on/off switch — callers
    that need that check `wake_schedule.enabled` themselves (it defaults
    OFF), because "give me the blocks" and "should any of them run" are
    different questions (mirrors policy/'s gating-vs-visibility split).
    Briefing wakes are NOT included here — they are not part of the block
    schedule this switch governs; see briefing_wake_times()."""
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

    # Above the master switch, like the manual hold and for the same reason:
    # `enabled` governs the passing-period block schedule and defaults off.
    # A briefing wake is not part of that schedule.
    briefing = _briefing_hold(cfg, conn, now)
    if briefing:
        return briefing

    wake_cfg = cfg.get("wake_schedule") or {}
    if not wake_cfg.get("enabled", False):
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


def _desired_stamps(cfg: dict, now: datetime,
                    conn: sqlite3.Connection | None = None) -> set[str]:
    """Every wakeorpoweron timestamp that should exist right now, from both
    producers. The two are gated independently — briefing wakes by
    `briefing_wakes` (default on), the block schedule by `enabled` (default
    off) — so turning the passing-period schedule off does not silently stop
    the machine waking for its briefings."""
    wake_cfg = cfg.get("wake_schedule") or {}
    desired: set[str] = set()
    days_ahead = int(wake_cfg.get("wake_days_ahead", 3))

    def _want(day: date_cls, hhmm: str | None) -> None:
        t = schedule._parse_hhmm(hhmm)
        if not t:
            return
        dt = datetime.combine(day, t)
        if dt <= now:
            return   # already passed — nothing to schedule
        desired.add(dt.strftime(_PMSET_DATETIME_FMT))

    for d in range(days_ahead + 1):
        day = now.date() + timedelta(days=d)

        if briefing_wakes_enabled(cfg):
            for b in briefing_wake_times(cfg, day, conn):
                _want(day, b["wake_at"])

        if wake_cfg.get("enabled", False):
            for b in combined_blocks(cfg, day):
                if b.get("enabled", True):
                    _want(day, b.get("wake_at"))

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

    desired = _desired_stamps(cfg, now, conn)
    ours = _load_ours(conn)

    to_cancel = ours - desired
    to_add = desired - ours

    cancelled, failed_cancel = [], []
    for ts in sorted(to_cancel):
        if _pmset_sudo(["schedule", "cancel", "wakeorpoweron", ts],
                       capability="wake"):
            cancelled.append(ts)
        else:
            failed_cancel.append(ts)

    added, failed_add = [], []
    for ts in sorted(to_add):
        if _pmset_sudo(["schedule", "wakeorpoweron", ts], capability="wake"):
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


def next_scheduled_wake_detail(cfg: dict, conn: sqlite3.Connection,
                               now: datetime | None = None) -> dict | None:
    """The next wake this module owns, plus what it is for — so the status
    panel can say "06:55, Morning briefing" rather than a bare timestamp.

    The TIME still comes from the materialized record (what is actually
    scheduled with pmset); only the LABEL is re-derived by matching that
    timestamp against today's and tomorrow's producers. A label that cannot
    be matched degrades to None rather than guessing, which keeps a stale
    record from being described as something it is not."""
    now = now or datetime.now()
    at = next_scheduled_wake(conn, now)
    if not at:
        return None
    when = datetime.fromisoformat(at)

    label = None
    day = when.date()
    hhmm = when.strftime("%H:%M")
    for b in briefing_wake_times(cfg, day, conn):
        if b["wake_at"] == hhmm:
            label = b["label"]
            break
    if label is None:
        for b in combined_blocks(cfg, day):
            if b.get("wake_at") == hhmm:
                label = b.get("label")
                break
    return {"at": at, "label": label}
