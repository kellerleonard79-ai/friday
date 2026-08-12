"""
tests/test_pending.py
The permission card's lifecycle. Plain asserts, no test framework.

    python3 tests/test_pending.py      (from the friday/ package directory)

Runs against an in-memory SQLite built from memory/db.py's own schema, so a
column added there and not here fails loudly rather than being mocked away.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects import pending                                   # noqa: E402
from tools.registry import tool as _tool                      # noqa: E402
from tools.types import (SendMessage, ToolError, ToolResult,   # noqa: E402
                         WriteConfirmation)

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


class FakeChannel:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def send(self, text: str) -> bool:
        self.calls.append(("send", text))
        return True

    def send_permission_request(self, proposal: str, key: str) -> bool:
        self.calls.append(("card", proposal))
        return True


# The schema constant's name is not part of any contract, so find it by content
# rather than assume it. Using the real schema means a column added in
# memory/db.py and forgotten here fails loudly instead of being mocked away.
import memory.db as _db  # noqa: E402

_schema_sql = next(
    v for k, v in vars(_db).items()
    if isinstance(v, str) and "CREATE TABLE IF NOT EXISTS pending_actions" in v
)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_schema_sql)
    for col in ("resolved_at", "tool_name", "arguments_json", "proposal",
                "expires_at", "turn_id"):
        conn.execute(f"ALTER TABLE pending_actions ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


# ── A tool that records exactly what arguments it was handed ─────────────────

_seen: list[dict] = []


@_tool(name="_p_write", description="Test write tool.",
       scope=("test_pending",), effect="write")
def _p_write(title: str, day: str) -> ToolResult:
    """test only"""
    _seen.append({"title": title, "day": day})
    return ToolResult(
        data={"title": title},
        effects=(SendMessage(text=f"{title} added."),),
        write=WriteConfirmation(status="written", day=date(2026, 9, 20),
                                fingerprint="fp", identifier="UID-9",
                                verified=True),
    )


@_tool(name="_p_fails", description="Test write tool that fails.",
       scope=("test_pending",), effect="write")
def _p_fails(title: str) -> ToolError:
    """test only"""
    _seen.append({"title": title})
    return ToolError(kind="unavailable",
                     message="internal detail the user must never see")


def stage(conn, key="k1", tool="_p_write", args=None, ttl=60):
    return pending.stage(
        conn, key=key, tool=tool,
        arguments=args if args is not None else {"title": "Lunch", "day": "2026-09-20"},
        proposal="Add to calendar?\nLunch", turn_id="t1", ttl_min=ttl)


def status_of(conn, key):
    return conn.execute(
        "SELECT status FROM pending_actions WHERE id = ?", (key,)).fetchone()[0]


# ── Confirm runs the STORED arguments ────────────────────────────────────────

_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
result = pending.confirm("k1", conn, ch)
check("confirm runs the tool", _seen == [{"title": "Lunch", "day": "2026-09-20"}])
check("confirm reports confirmed", result == "confirmed")
check("confirm marks the row confirmed", status_of(conn, "k1") == "confirmed")
check("the tool's effects reach the channel",
      ("send", "Lunch added.") in ch.calls)

# The arguments that run are the ones on the row, even if the row is edited
# behind the tool's back. Nothing re-extracts them and no model is consulted.
_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn, args={"title": "Tennis Practice", "day": "2026-10-01"})
pending.confirm("k1", conn, ch)
check("confirm uses the stored arguments verbatim",
      _seen == [{"title": "Tennis Practice", "day": "2026-10-01"}])


# ── Cancel runs nothing ──────────────────────────────────────────────────────

_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
result = pending.cancel("k1", conn, ch)
check("cancel does not run the tool", _seen == [])
check("cancel marks the row cancelled", status_of(conn, "k1") == "cancelled")
check("cancel tells the user nothing happened",
      any("Cancelled" in c[1] for c in ch.calls))
check("cancel reports cancelled", result == "cancelled")


# ── A resolved card cannot run twice ─────────────────────────────────────────

_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
pending.confirm("k1", conn, ch)
pending.confirm("k1", conn, ch)
check("a double tap runs the tool once", len(_seen) == 1)

_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
pending.cancel("k1", conn, ch)
pending.confirm("k1", conn, ch)
check("confirming a cancelled card runs nothing", _seen == [])


# ── Expiry REFUSES; it does not silently run ─────────────────────────────────

_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
conn.execute("UPDATE pending_actions SET expires_at = ? WHERE id = 'k1'",
             ((datetime.now() - timedelta(minutes=1)).isoformat(),))
conn.commit()
result = pending.confirm("k1", conn, ch)
check("an expired card does not run the tool", _seen == [])
check("an expired card reports expired", result == "expired")
check("an expired card is marked expired", status_of(conn, "k1") == "expired")
check("an expired card SAYS SO rather than failing silently",
      any("expired" in c[1].lower() for c in ch.calls))

# A row from before the TTL column existed is honoured, not refused. Refusing
# a card the user is looking at because of a migration is the worse failure.
_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
conn.execute("UPDATE pending_actions SET expires_at = NULL WHERE id = 'k1'")
conn.commit()
check("a row with no expiry is honoured", pending.confirm("k1", conn, ch) == "confirmed")


# ── An unknown key answers rather than going quiet ───────────────────────────

conn, ch = db(), FakeChannel()
check("an unknown key reports unknown", pending.confirm("nope", conn, ch) == "unknown")
check("an unknown key still tells the user something", len(ch.calls) == 1)


# ── A failed tool never leaks its message to the user ────────────────────────

_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn, tool="_p_fails", args={"title": "Doomed"})
result = pending.confirm("k1", conn, ch)
check("a failed tool leaves the row failed", status_of(conn, "k1") == "failed")
check("a failed tool reports failed", result == "failed")
check("the tool's internal message is NOT shown to the user",
      not any("internal detail" in c[1] for c in ch.calls))
check("the user is still told it did not work",
      any("didn't go through" in c[1] for c in ch.calls))


# ── The row carries what a card has to carry ─────────────────────────────────

conn = db()
stage(conn)
row = conn.execute(
    "SELECT tool_name, arguments_json, proposal, turn_id, created_at, "
    "       expires_at, status FROM pending_actions WHERE id='k1'").fetchone()
check("the row carries the tool name", row[0] == "_p_write")
check("the row carries the arguments",
      json.loads(row[1]) == {"title": "Lunch", "day": "2026-09-20"})
check("the row carries the card text the user saw",
      row[2] == "Add to calendar?\nLunch")
check("the row carries the proposing turn", row[3] == "t1")
check("the row carries a created timestamp and a TTL", bool(row[4]) and bool(row[5]))
check("a new row starts pending", row[6] == "pending")


# ── The confirm path closes the loop in conversation_history ─────────────────
#
# It did not, and that is what produced three cards from one message: the
# proposing turn recorded "I put a confirmation card in front of you for X",
# the reply saying X was added went only to Telegram, and a model looking at
# an unresolved proposal proposes it again.

def history(conn):
    return [r[0] for r in conn.execute(
        "SELECT content FROM conversation_history ORDER BY id")]


_seen.clear()
conn, ch = db(), FakeChannel()
stage(conn)
pending.confirm("k1", conn, ch)
check("a confirmed write's reply reaches conversation_history",
      any("Lunch added." in h for h in history(conn)))

conn, ch = db(), FakeChannel()
stage(conn)
pending.cancel("k1", conn, ch)
check("a cancellation reaches conversation_history",
      any("Cancelled" in h for h in history(conn)))

conn, ch = db(), FakeChannel()
stage(conn, tool="_p_fails", args={"title": "Doomed"})
pending.confirm("k1", conn, ch)
check("a failure reaches conversation_history too",
      any("didn't go through" in h for h in history(conn)))
check("the tool's internal message never reaches history",
      not any("internal detail" in h for h in history(conn)))


# ── Idempotency: the same write twice inside the TTL happens once ────────────
#
# The check has to be LOCAL. The case it exists for is a write whose service
# call timed out, and a retry cannot rely on reaching the thing that just
# failed to answer.

from memory import writes as recent  # noqa: E402

conn = db()
conn.executescript("""
CREATE TABLE IF NOT EXISTS recent_writes (
    fingerprint TEXT PRIMARY KEY, created_at TEXT, expires_at TEXT,
    status TEXT, identifier TEXT, detail TEXT);
""")

check("an unseen fingerprint has no prior write",
      recent.find(conn, "fp-1") is None)

recent.reserve(conn, "fp-1")
prior = recent.find(conn, "fp-1")
check("a reserved fingerprint is found immediately", prior is not None)
check("a reservation starts as unknown, not written",
      prior.status == "unknown" and not prior.confirmed)

recent.settle(conn, "fp-1", status="written", identifier="UID-7")
prior = recent.find(conn, "fp-1")
check("settling records the identifier",
      prior.confirmed and prior.identifier == "UID-7")

# A refused write RELEASES the fingerprint — the service said it did not
# happen, so a second attempt after the user fixes the calendar name must work.
recent.reserve(conn, "fp-2")
recent.settle(conn, "fp-2", status="refused")
check("a refused write releases its fingerprint",
      recent.find(conn, "fp-2") is None)

# An unknown write KEEPS its row. This is the whole point: it is the only
# evidence a retry has.
recent.reserve(conn, "fp-3", detail="osascript timed out")
recent.settle(conn, "fp-3", status="unknown", detail="osascript timed out")
prior = recent.find(conn, "fp-3")
check("an unknown write keeps its row for a retry to find",
      prior is not None and prior.status == "unknown")

# Past the TTL it stops blocking.
conn.execute("UPDATE recent_writes SET expires_at = ? WHERE fingerprint='fp-1'",
             ((datetime.now() - timedelta(minutes=1)).isoformat(),))
conn.commit()
check("a prior write outside the TTL no longer blocks", recent.find(conn, "fp-1") is None)
check("prune removes the expired row", recent.prune(conn) >= 1)


print()
if _failures:
    print(f"{len(_failures)} FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
