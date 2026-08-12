"""
tests/test_entry.py
The one door effects go out of. Plain asserts, no test framework.

    python3 tests/test_entry.py       (from the friday/ package directory)

WHAT THIS FILE IS FOR. Two call sites execute effects — the turn loop, via
channels/telegram.py, and the confirm path in effects/pending.py — and they
must produce the same history for the same batch. They did not: one wrote
history inline and skipped the prose a tool emitted, the other wrapped its
channel. This file asserts they now agree, and that a card is not logged as
prose in either.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects import entry, pending                            # noqa: E402
from tools.registry import tool as _tool                      # noqa: E402
from tools.types import (SendMessage, SendPermissionCard,      # noqa: E402
                         ToolResult, WriteConfirmation)

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


class FakeChannel:
    name = "fake"

    def __init__(self, fail_on: str = ""):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def send(self, text: str) -> bool:
        self.calls.append(("send", text))
        return self.fail_on != "send"

    def send_permission_request(self, proposal: str, key: str) -> bool:
        self.calls.append(("card", proposal))
        return self.fail_on != "card"


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


def history(conn) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT role, content FROM conversation_history ORDER BY id")]


# ── The turn-loop caller ─────────────────────────────────────────────────────

print("\n-- the turn loop's batch --")
conn = db()
ch = FakeChannel()
batch = [SendMessage(text="Tennis added."),
         SendPermissionCard(proposal="Add to calendar?\nLunch", pending_key="k1",
                            tool="_e_write", arguments={"title": "Lunch"})]
report = entry.deliver(batch, ch, conn)

check("card still went first", ch.calls[0][0] == "card")
check("the batch executed", report.executed == 2)
check("prose reached history", history(conn) == [("assistant", "Tennis added.")])
check("the card did NOT reach history",
      all("Lunch" not in c for _r, c in history(conn)))
check("the card text came back on the report",
      report.card_texts == ("Add to calendar?\nLunch",))


# ── The confirm-path caller ──────────────────────────────────────────────────

@_tool(name="_e_write", description="Test write tool.",
       scope=("test_entry",), effect="write")
def _e_write(title: str) -> ToolResult:
    """test only"""
    return ToolResult(
        data={"title": title},
        effects=(SendMessage(text=f"{title} added."),),
        write=WriteConfirmation(status="written", day=date(2026, 9, 20),
                                identifier="x1", fingerprint="fp1"),
    )


print("\n-- the confirm path's batch --")
conn2 = db()
ch2 = FakeChannel()
pending.stage(conn2, key="c1", tool="_e_write",
              arguments={"title": "Tennis"}, proposal="Add to calendar?\nTennis")
status = pending.confirm("c1", conn2, ch2)

check("the write ran", status == "confirmed")
check("the SAME history shape as the turn loop",
      history(conn2) == [("assistant", "Tennis added.")])


# ── A failed send is not history ─────────────────────────────────────────────

print("\n-- a send that failed --")
conn3 = db()
ch3 = FakeChannel(fail_on="send")
entry.deliver([SendMessage(text="never arrived")], ch3, conn3)
check("a message that did not reach the user is not in history",
      history(conn3) == [])


# ── Wrapping is idempotent ───────────────────────────────────────────────────

print("\n-- double wrapping --")
conn4 = db()
ch4 = FakeChannel()
wrapped = entry.as_history_channel(ch4, conn4)
twice = entry.as_history_channel(wrapped, conn4)
check("wrapping a wrapped channel returns it unchanged", twice is wrapped)
entry.deliver([SendMessage(text="once")], twice, conn4)
check("one send is one history row", history(conn4) == [("assistant", "once")])
check("the wrapper is transparent to other attributes", twice.name == "fake")


print()
if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
