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
same tools/executor.py every turn uses, and hands the resulting effects to
effects/entry.py — the same door channels/telegram.py and the dashboard use.
Same machinery, different entrance. It does NOT call effects/runner.py
directly; nothing outside entry.py does, which is what makes the history
logging that must accompany a batch impossible to forget.

EXPIRY REFUSES, IT DOES NOT SILENTLY RUN. A tap on a stale card gets a clear
message. The alternative — honouring it — is a button that still works days
after the user has forgotten what it was for, which is the failure a TTL
exists to prevent. The alternative to THAT, ignoring the tap, is worse: the
user tapped a button and nothing happened, so they tap it again.

TWO THRESHOLDS, BECAUSE THEY ANSWER DIFFERENT QUESTIONS.

    expires_at   (TTL, 24h)   — after this the card is dead.
    stale        (30m)        — after this the card still works, but one tap
                                is no longer enough.

The TTL is long on purpose: a card sent at 11pm has to survive until morning,
and a two-hour TTL would silently break the ordinary "I'll deal with that
when I wake up" case. But a card that has been sitting there for hours is
exactly the one that gets tapped reflexively — and the model occasionally
proposes a card nobody asked for, so some of those taps are on proposals the
user never made. Shortening the TTL would trade a real case for a rare one.

So a stale card is RE-PROPOSED rather than refused. The old row is resolved
`superseded`, a fresh row with THE SAME STORED ARGUMENTS is staged, and a new
card goes out saying how old the original was. The second confirmation is
therefore a tap on a DIFFERENT BUTTON — which is the whole point. Setting a
flag on the row and honouring the next tap of the same button would be
defeated by exactly the reflex it exists to catch: a double-tap.

RESOLVED IS NEVER SILENT. A tap on a card that was already confirmed,
cancelled or superseded — from the other channel, or a second time in this
one — says so. It cannot execute twice; the status check is the fail-closed
half and was already here. The silence was the bug: the user pressed a button
and nothing visible happened, which reads as broken and invites another tap.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

from effects import entry as effects_entry
from tools import executor, registry
from tools.ledger import Ledger
from tools.types import ToolError, ToolResult

logger = logging.getLogger("friday.effects.pending")

# How long a card stays tappable. A day, because "I'll deal with that in the
# morning" is ordinary and a card sent at 11pm has to survive until 8am. Not
# indefinite, because a week-old button that still writes to the calendar is
# not a permission gate, it is a landmine.
DEFAULT_TTL_MINUTES = 24 * 60

# How long one tap is enough for. Past this the card still works, but it is
# re-proposed and the user confirms the new one. Half an hour is about the
# span in which "the thing I just asked for" is still what the button means.
DEFAULT_STALE_MINUTES = 30

_ACTION_TYPE = "tool_call"

_config: dict = {}


def configure(config: dict) -> None:
    """Install the config. Called once at startup, beside the tools'.

    confirm() is reached from a button tap and is handed a key, a connection
    and a channel — there is nowhere on that path for a config to travel, and
    threading one through every caller for two integers is worse than this.
    """
    global _config
    _config = config or {}


def _minutes(key: str, default: int) -> int:
    try:
        value = int(((_config.get("agent") or {}).get(key, default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def ttl_minutes(config: dict | None = None) -> int:
    try:
        value = int((((config if config is not None else _config).get("agent") or {})
                     .get("pending_action_ttl_minutes", DEFAULT_TTL_MINUTES)))
        return value if value > 0 else DEFAULT_TTL_MINUTES
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_TTL_MINUTES


def stale_minutes() -> int:
    return _minutes("pending_action_stale_minutes", DEFAULT_STALE_MINUTES)


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
        "       expires_at, turn_id, created_at "
        "FROM pending_actions WHERE id = ?", (key,)
    ).fetchone()
    return row


def _age_minutes(created_at: str | None) -> float | None:
    """How old the proposal is, or None if we cannot tell. None is treated as
    NOT stale — a row whose timestamp we cannot read is a migration artefact,
    and re-proposing a card the user is looking at because of a parse failure
    is the wrong way to be careful."""
    if not created_at:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(created_at)).total_seconds() / 60.0
    except ValueError:
        return None


def _age_phrase(minutes: float) -> str:
    """Plain English, deliberately. This line is the whole content of the
    re-confirmation — a user who cannot tell at a glance how old the proposal
    is has been asked to confirm nothing."""
    if minutes < 90:
        n = max(1, int(round(minutes)))
        return f"{n} minute{'s' if n != 1 else ''} ago"
    hours = minutes / 60.0
    if hours < 36:
        n = int(round(hours))
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(round(hours / 24.0))
    return f"{n} day{'s' if n != 1 else ''} ago"


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


_ALREADY = {
    "confirmed":  "That one's already done, sir — I haven't done it twice.",
    "cancelled":  "That one was already cancelled, sir. Nothing happened.",
    "expired":    "That request had already expired, sir. Nothing happened.",
    "superseded": "That card's been replaced, sir — the newer one is the live one.",
    "failed":     "That one already failed, sir. Nothing happened just now.",
}
_ALREADY_DEFAULT = "That's already been dealt with, sir — nothing happened just now."


def refusal_message(status: str) -> str:
    """What a tap on an already-resolved card is told. Public so the dashboard
    surfaces the SAME sentence its 409 used to hide behind a status code — the
    two channels have to agree about what a dead button means."""
    return _ALREADY.get(status, _ALREADY_DEFAULT)


def _repropose(conn, channel, *, old_key: str, tool_name: str,
               arguments_json: str, proposal: str, turn_id: str,
               age: float) -> str:
    """Retire a stale card and put a fresh one in its place.

    THE ARGUMENTS ARE COPIED, NOT REBUILT. The whole reason this is safe is
    that the new card proposes exactly what the old one did — the model is not
    consulted, the text is not regenerated, and the only thing that changes is
    which button the user is looking at.

    The age line goes FIRST and that is not a violation of invariant 3. What
    invariant 3 forbids is delaying, editorializing on, or burying a card:
    persona, preamble, a paragraph that talks the user into it. "Proposed 3
    hours ago" is none of those. It comes from the clock rather than the
    model, it is the one fact that makes a second confirmation mean anything,
    and burying it under the proposal would be the failure this exists to
    prevent.
    """
    _resolve(conn, old_key, "superseded")
    new_key_ = new_key()
    text = (f"Proposed {_age_phrase(age)} — confirm again?\n\n"
            f"{proposal or '(the original wording is gone)'}")
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        arguments = {}
    if not stage(conn, key=new_key_, tool=tool_name, arguments=arguments,
                 proposal=text, turn_id=turn_id, ttl_min=ttl_minutes()):
        channel.send("That request is old and I couldn't re-check it, sir. "
                     "Tell me again if you still want it.")
        return "stale_failed"
    if not channel.send_permission_request(text, new_key_):
        # The row exists and the card did not go out. Resolved rather than
        # left pending: a row nobody was shown is a button that cannot be
        # tapped, and it would sit in the dashboard's pending list forever.
        _resolve(conn, new_key_, "failed")
        channel.send("That request is old, sir, and I couldn't re-ask. "
                     "Tell me again if you still want it.")
        return "stale_failed"
    logger.info(f"confirm — {old_key} was {age:.0f}m old; re-proposed as {new_key_}")
    return "stale"


def confirm(key: str, conn, channel) -> str:
    """Run the proposal. Returns a short status string for the log.

    Never raises. Every failure path tells the user something, because the
    user has just pressed a button and silence reads as a broken bot.
    """
    channel = effects_entry.as_history_channel(channel, conn)
    row = _load(conn, key)
    if row is None:
        channel.send("I can't find that request any more, sir. Ask me again?")
        return "unknown"
    (action_type, tool_name, arguments_json, proposal, status, expires_at,
     turn_id, created_at) = row

    if action_type != _ACTION_TYPE:
        logger.warning(f"confirm — {key} is a {action_type}, not a tool call")
        return "wrong_type"
    if status != "pending":
        # ALREADY RESOLVED, AND IT SAYS SO. This is the cross-channel case as
        # much as the double-tap one: a card confirmed in the dashboard leaves
        # a live-looking button in Telegram, and tapping it must fail closed
        # AND be visibly refused. It cannot execute twice — that is this
        # branch — but silence here reads as a broken button, which is what
        # earns the second tap.
        logger.info(f"confirm — {key} is already {status}, refusing")
        channel.send(refusal_message(status))
        return status
    if _expired(expires_at):
        _resolve(conn, key, "expired")
        channel.send(
            "That request has expired, sir — I didn't act on it. "
            "Tell me again if you still want it."
        )
        return "expired"

    # STALE: still valid, but one tap is no longer enough. Re-proposed rather
    # than refused — see the module docstring. Nothing runs on this path.
    age = _age_minutes(created_at)
    if age is not None and age > stale_minutes():
        return _repropose(conn, channel, old_key=key, tool_name=tool_name,
                          arguments_json=arguments_json, proposal=proposal,
                          turn_id=turn_id, age=age)

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
        effects_entry.deliver(outcome.effects, channel, conn)
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
    channel = effects_entry.as_history_channel(channel, conn)
    row = _load(conn, key)
    if row is None:
        return "unknown"
    action_type, tool_name, _args, _proposal, status, _expires, _turn, _made = row
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
