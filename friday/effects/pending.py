"""
effects/pending.py
The permission card's life: proposed, then confirmed or cancelled or expired.

A card is a proposal to run ONE TOOL with ONE SET OF ARGUMENTS. The row holds
the tool's name, its arguments, the card text the user saw, the turn that
proposed it, and an expiry.

THE STORED ARGUMENTS RUN. Not re-extracted ones, not the model asked again.
The user approved specific values — a specific title, a specific day, a
specific calendar — and re-deriving them at confirm time means they could
approve one event and receive a different one. The model is not consulted on
this path at all; confirm is deterministic.

THE CONFIRM PATH IS ITS OWN ENTRY POINT INTO THE EFFECTS RUNNER. It arrives
from an inline-button callback, not from a turn: there is no LLMRequest, no
ledger with a turn's reads in it, no hop budget. Routing it back through
agent/turn.py would mean inventing a fake turn around a decision that has
already been made. So it builds a one-call ledger, runs the tool through the
same tools/executor.py every turn uses, and hands the resulting effects to the
same effects/runner.py. Same machinery, different door.

EXPIRY REFUSES, IT DOES NOT SILENTLY RUN. A tap on a stale card gets a clear
message. The alternative — honouring it — is a button that still works days
after the user has forgotten what it was for, which is the failure a TTL
exists to prevent. The alternative to THAT, ignoring the tap, is worse: the
user tapped a button and nothing happened, so they tap it again.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

from effects import runner as effects_runner
from tools import executor, registry
from tools.ledger import Ledger
from tools.types import ToolError, ToolResult

logger = logging.getLogger("friday.effects.pending")

# How long a card stays tappable. A day, because "I'll deal with that in the
# morning" is ordinary and a card sent at 11pm has to survive until 8am. Not
# indefinite, because a week-old button that still writes to the calendar is
# not a permission gate, it is a landmine.
DEFAULT_TTL_MINUTES = 24 * 60

_ACTION_TYPE = "tool_call"


def ttl_minutes(config: dict | None) -> int:
    try:
        value = int(((config or {}).get("agent") or {})
                    .get("pending_action_ttl_minutes", DEFAULT_TTL_MINUTES))
        return value if value > 0 else DEFAULT_TTL_MINUTES
    except (TypeError, ValueError):
        return DEFAULT_TTL_MINUTES


def new_key() -> str:
    return uuid.uuid4().hex[:12]


def stage(conn, *, key: str, tool: str, arguments: dict, proposal: str,
          turn_id: str = "", ttl_min: int = DEFAULT_TTL_MINUTES) -> bool:
    """Record a proposal. Returns False if it could not be stored.

    STORED BEFORE THE CARD IS SENT, never after. A card whose row does not
    exist is a button that answers "unknown or expired" the moment it is
    tapped, and the user has no way to tell that from a bug.
    """
    now = datetime.now()
    try:
        conn.execute(
            "INSERT INTO pending_actions "
            "(id, action_type, payload, status, created_at, tool_name, "
            " arguments_json, proposal, expires_at, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, _ACTION_TYPE, json.dumps(arguments, default=str), "pending",
             now.isoformat(), tool, json.dumps(arguments, default=str),
             proposal, (now + timedelta(minutes=ttl_min)).isoformat(), turn_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.exception(f"Could not stage pending action {key}: {e}")
        return False


def _load(conn, key: str):
    row = conn.execute(
        "SELECT action_type, tool_name, arguments_json, proposal, status, "
        "       expires_at, turn_id "
        "FROM pending_actions WHERE id = ?", (key,)
    ).fetchone()
    return row


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        # Rows staged before the TTL column existed. Treated as live rather
        # than as expired: refusing a card the user is looking at, because of a
        # migration, is worse than honouring an old one.
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now()
    except ValueError:
        return False


def confirm(key: str, conn, channel) -> str:
    """Run the proposal. Returns a short status string for the log.

    Never raises. Every failure path tells the user something, because the
    user has just pressed a button and silence reads as a broken bot.
    """
    row = _load(conn, key)
    if row is None:
        channel.send("I can't find that request any more, sir. Ask me again?")
        return "unknown"
    action_type, tool_name, arguments_json, proposal, status, expires_at, turn_id = row

    if action_type != _ACTION_TYPE:
        logger.warning(f"confirm — {key} is a {action_type}, not a tool call")
        return "wrong_type"
    if status != "pending":
        # Already resolved. Silent: this is a double-tap, and the user has
        # already seen the outcome of the first one.
        logger.info(f"confirm — {key} is already {status}, ignoring")
        return status
    if _expired(expires_at):
        _resolve(conn, key, "expired")
        channel.send(
            "That request has expired, sir — I didn't act on it. "
            "Tell me again if you still want it."
        )
        return "expired"

    if not registry.has(tool_name):
        _resolve(conn, key, "failed")
        logger.error(f"confirm — {key} names unknown tool {tool_name!r}")
        channel.send("I can't carry that out any more, sir.")
        return "unknown_tool"

    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        _resolve(conn, key, "failed")
        logger.error(f"confirm — {key} has unreadable arguments")
        channel.send("I couldn't read back what you approved, sir.")
        return "bad_arguments"

    # The confirm path's own ledger. It starts empty and holds exactly what
    # this one call establishes — it is NOT the proposing turn's ledger, which
    # died with that turn. That is honest: the reads that justified the
    # proposal happened minutes or hours ago and are not facts about now.
    ledger = Ledger()
    spec = registry.get(tool_name)
    outcome, duration_ms = executor.run(
        spec, dict(arguments), ledger=ledger, store={})

    committed = isinstance(outcome, ToolResult) and outcome.committed
    _resolve(conn, key, "confirmed" if committed else "failed")

    # THE ACTUAL WRITE HAPPENS HERE, so this is where its tool_calls row has to
    # be written. The proposing turn logged add_calendar_event, which wrote
    # nothing; without this row the only record of the write itself would be
    # the calendar.
    #
    # The ledger recorded is the one this call built, which is empty of reads
    # and holds only what the write established. That is not a gap — it is the
    # honest answer. The reads that justified the proposal belonged to a turn
    # that ended, possibly hours ago, and are not facts about the moment the
    # user tapped Confirm. `turn_id` is on the pending_actions row for anyone
    # who needs to walk back to them.
    _log(conn, tool_name, arguments, outcome, duration_ms, ledger, turn_id)

    if isinstance(outcome, ToolResult) and outcome.effects:
        effects_runner.run(outcome.effects, channel)
    elif isinstance(outcome, ToolError):
        # The tool's message is written to the MODEL and must never be shown
        # verbatim (tools/types.py). The user gets a plain sentence.
        logger.error(f"confirm — {tool_name} failed: {outcome.kind}: {outcome.message}")
        channel.send("That didn't go through, sir.")

    logger.info(
        f"confirm {key}: {tool_name} -> committed={committed} in {duration_ms}ms")
    return "confirmed" if committed else "failed"


def cancel(key: str, conn, channel) -> str:
    """Mark a proposal cancelled. NOTHING RUNS."""
    row = _load(conn, key)
    if row is None:
        return "unknown"
    action_type, tool_name, _args, _proposal, status, _expires, _turn = row
    if action_type != _ACTION_TYPE or status != "pending":
        return status or "unknown"
    _resolve(conn, key, "cancelled")
    channel.send("Cancelled, sir — I haven't done anything.")
    return "cancelled"


def _resolve(conn, key: str, status: str) -> None:
    try:
        conn.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.now().isoformat(), key),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Could not resolve pending action {key}: {e}")


def _log(conn, tool_name: str, arguments: dict, outcome, duration_ms: int,
         ledger: Ledger, turn_id: str) -> None:
    """One tool_calls row for the confirm-path write. Wrapped — instrumentation
    never fails a write the user has already approved."""
    try:
        import json as _json

        import memory.activity as activity
        payload = (outcome.as_content() if isinstance(outcome, ToolError)
                   else outcome.data)
        activity.record_tool_call(
            conn,
            tool_name=tool_name,
            args_json=_json.dumps(arguments, default=str),
            result_preview=_json.dumps(payload, default=str)[:400],
            duration_ms=duration_ms,
            triggered_by=f"card_confirm:{turn_id}" if turn_id else "card_confirm",
            hop=0,
            outcome=outcome.kind if isinstance(outcome, ToolError) else "ok",
            ledger_json=_json.dumps(ledger.summary(), default=str),
        )
    except Exception as e:
        logger.debug(f"confirm-path tool_call logging failed: {e}")
