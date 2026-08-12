"""
tests/test_conversation.py
One user message, on any channel. Plain asserts, no test framework.

    python3 tests/test_conversation.py    (from the friday/ package directory)

What this file is really asserting is that there is only ONE of this path.
The dashboard was the moment the Telegram handler's insides stopped being
Telegram's, and the tests below drive the identical function with two
different channels and require the same rows out of it.

run_turn is stubbed. This is not a test of the turn loop — tests/test_tools.py
is — it is a test of what happens around one.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import conversation                             # noqa: E402
from channels.base import Channel                             # noqa: E402
from channels.dashboard import DashboardChannel               # noqa: E402
from agent.turn import TurnResult                             # noqa: E402
from tools.types import SendMessage, SendPermissionCard        # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


import memory.db as _db  # noqa: E402

_schema_sql = next(
    v for k, v in vars(_db).items()
    if isinstance(v, str) and "CREATE TABLE IF NOT EXISTS conversation_history" in v
)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(_schema_sql)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_history)")}
    if "channel" not in cols:
        conn.execute("ALTER TABLE conversation_history ADD COLUMN channel TEXT")
    conn.commit()
    return conn


def history(conn):
    return conn.execute(
        "SELECT role, content, channel FROM conversation_history ORDER BY id"
    ).fetchall()


class Fake(Channel):
    def __init__(self, name):
        self.name = name
        self.sent: list[str] = []
        self.cards: list[str] = []

    def send(self, text): self.sent.append(text); return True
    def send_permission_request(self, proposal, key):
        self.cards.append(proposal); return True
    def notify(self, title, text): return True


from llm import profiles as _profiles  # noqa: E402

_PROFILE_NAMES = _profiles.names()

_CONFIG = {
    "memory": {"short_term_turns": 20},
    # A model name is required to build the registry. Nothing here calls it —
    # run_turn is stubbed — but handle() builds a real LLMRequest, and the
    # registry refusing to install without a model is the behaviour that keeps
    # a typo'd profile from becoming a bigger bill.
    "profiles": {name: {"model": "test-model"} for name in _PROFILE_NAMES},
}

# handle() builds a real LLMRequest before it ever reaches run_turn, and
# profiles.get() raises on an uninstalled registry — deliberately, so a typo'd
# profile is a bug rather than a bigger bill. Install the real table.
_profiles.install(_CONFIG)


def stub(result: TurnResult):
    conversation.run_turn = lambda request, conn=None: result


def run(text, channel, conn):
    return asyncio.run(conversation.handle(text, channel, conn, _CONFIG))


# ── The same path, two channels ──────────────────────────────────────────────

print("\n-- the same rows from either channel --")
stub(TurnResult(text="Nothing until Thursday, sir."))

rows = {}
for name in ("telegram", "dashboard"):
    conn = db()
    ch = Fake(name)
    reply = run("what's on today", ch, conn)
    rows[name] = history(conn)
    check(f"{name}: the answer was sent through the channel",
          ch.sent == ["Nothing until Thursday, sir."])
    check(f"{name}: the reply carries the text", reply.text == "Nothing until Thursday, sir.")

check("both channels write the same roles and content",
      [(r[0], r[1]) for r in rows["telegram"]] ==
      [(r[0], r[1]) for r in rows["dashboard"]])
check("each row records the channel it came from",
      [r[2] for r in rows["telegram"]] == ["telegram", "telegram"] and
      [r[2] for r in rows["dashboard"]] == ["dashboard", "dashboard"])


# ── The card case: nothing goes in the assistant slot ────────────────────────

print("\n-- a turn whose only output is a card --")
stub(TurnResult(text="", effects=(
    SendPermissionCard(proposal="Add to calendar?\nLunch", pending_key="k1",
                       tool="add_calendar_event", arguments={}),)))
conn = db()
ch = Fake("dashboard")
reply = run("add lunch thursday", ch, conn)
check("the card went out", ch.cards == ["Add to calendar?\nLunch"])
check("nothing was said after it", ch.sent == [])
check("the reply reports the card", reply.cards == ("Add to calendar?\nLunch",))
check("history holds the user turn and NOTHING in the assistant slot",
      history(conn) == [("user", "add lunch thursday", "dashboard")])


# ── Prose after a card is suppressed ─────────────────────────────────────────
#
# Invariant 3: nothing may delay, editorialize on, or bury a card. The model
# narrating its own tool call underneath one is the commonest way to do it, and
# the branch used to run only when the model happened to say nothing.

print("\n-- the model talks over its own card --")
stub(TurnResult(text="I have called the tool; please confirm the card below.",
                effects=(SendPermissionCard(
                    proposal="Add to calendar?\nLunch", pending_key="k2",
                    tool="add_calendar_event", arguments={}),)))
conn = db()
ch = Fake("dashboard")
reply = run("add lunch", ch, conn)
check("the card went out", ch.cards == ["Add to calendar?\nLunch"])
check("the model's trailing prose was NOT sent", ch.sent == [])
check("the reply reports nothing said", reply.text == "")
check("and nothing reached history either",
      history(conn) == [("user", "add lunch", "dashboard")])

# An error on a turn that emitted a card: the card stands, the error line does
# not. A card the user is looking at must not be followed by "LLM error, sir".
stub(TurnResult(text="", error_kind="transient", error_message="503",
                effects=(SendPermissionCard(
                    proposal="Add to calendar?\nLunch", pending_key="k3",
                    tool="add_calendar_event", arguments={}),)))
conn = db()
ch = Fake("dashboard")
run("add lunch", ch, conn)
check("an error after a card is not read out over it", ch.sent == [])


# ── A tool's own message is logged, on both channels ─────────────────────────

print("\n-- a tool that speaks --")
stub(TurnResult(text="", effects=(SendMessage(text="Tennis added."),)))
for name in ("telegram", "dashboard"):
    conn = db()
    ch = Fake(name)
    run("add tennis", ch, conn)
    check(f"{name}: the tool's message is in history",
          history(conn) == [("user", "add tennis", name),
                            ("assistant", "Tennis added.", name)])


# ── Errors are the same sentence on both surfaces ────────────────────────────

print("\n-- a blocked network --")
stub(TurnResult(error_kind="network", error_message="dns"))
conn = db()
ch = Fake("dashboard")
reply = run("hello", ch, conn)
check("a network failure says it cannot reach the model",
      ch.sent == ["I can't reach the model from this network."])
check("the reply carries the kind, not just the words",
      reply.error_kind == "network")


# ── The gate is one gate ─────────────────────────────────────────────────────

print("\n-- the gate --")
order: list[str] = []


async def _both():
    conn = db()

    def slow(request, conn=None):
        # Runs in an executor thread; the point is that the SECOND turn must
        # not start until the first has finished.
        order.append(f"start:{request.prompt}")
        import time
        time.sleep(0.05)
        order.append(f"end:{request.prompt}")
        return TurnResult(text="ok")

    conversation.run_turn = slow
    await asyncio.gather(
        conversation.handle("A", Fake("telegram"), conn, _CONFIG),
        conversation.handle("B", Fake("dashboard"), conn, _CONFIG),
    )
    return conn


conn = asyncio.run(_both())
check("a telegram turn and a dashboard turn never interleave",
      order in (["start:A", "end:A", "start:B", "end:B"],
                ["start:B", "end:B", "start:A", "end:A"]))
check("both turns' rows are in the one transcript", len(history(conn)) == 4)
check("the transcript records both surfaces",
      {r[2] for r in history(conn)} == {"telegram", "dashboard"})


# ── The dashboard channel is a channel ───────────────────────────────────────

print("\n-- the dashboard channel --")
stub(TurnResult(text="Right away, sir."))
conn = db()
dash = DashboardChannel()
reply = run("hello", dash, conn)
check("DashboardChannel satisfies the contract", isinstance(dash, Channel))
check("its events carry what was said",
      [e["text"] for e in dash.events] == ["Right away, sir."])
check("it writes history as 'dashboard'",
      history(conn) == [("user", "hello", "dashboard"),
                        ("assistant", "Right away, sir.", "dashboard")])


print()
if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
