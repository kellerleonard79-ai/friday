"""
tests/test_effects.py
Tests for the effects layer. Plain asserts, no test framework.

    python3 tests/test_effects.py      (from the friday/ package directory)

The ordering test is why this file exists. "Permission cards are emitted
first" was a rule people had to remember in Phase II; here it is a property of
one function, and a property with no test is a comment.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects import runner                                    # noqa: E402
from tools.types import (CancelScheduled, Effect, ScheduleItem,  # noqa: E402
                         SendMessage, SendPermissionCard)

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


class FakeChannel:
    """Records the order things were sent in. Nothing else."""

    def __init__(self, fail_on: str = ""):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def send(self, text: str) -> bool:
        self.calls.append(("send", text))
        return self.fail_on != "send"

    def send_permission_request(self, proposal: str, pending_key: str) -> bool:
        self.calls.append(("card", proposal))
        return self.fail_on != "card"


# ── A card emitted last is executed first ────────────────────────────────────
#
# THE TEST THIS FILE EXISTS FOR. Invariant 3: a permission card is emitted
# first, and nothing may delay, editorialize on, or bury one. The batch below
# puts the card dead last and surrounds it with plausible chatter, which is
# exactly how the invariant got broken in Phase II.

card = SendPermissionCard(proposal="Add to calendar?\nLunch",
                          pending_key="k1", tool="add_calendar_event",
                          arguments={"title": "Lunch"})
batch = [
    SendMessage(text="first message"),
    SendMessage(text="second message"),
    ScheduleItem(kind="reminder", when="2026-09-01"),
    card,
]

ch = FakeChannel()
report = runner.run(batch, ch)

check("a card emitted LAST in the batch is executed FIRST",
      ch.calls[0] == ("card", "Add to calendar?\nLunch"))
check("the messages behind it still run, in their original order",
      [c[1] for c in ch.calls[1:]] == ["first message", "second message"])
check("the report counts the card", report.cards_sent == 1)
check("the report counts every executed effect", report.executed == 3)
check("a batch with no failures reports ok", report.ok)

# The same guarantee without a channel: ordering is a property of the sort.
check("sort_effects puts the card first regardless of input position",
      isinstance(runner.sort_effects(batch)[0], SendPermissionCard))
check("sort_effects is stable for equal-priority effects",
      [e.text for e in runner.sort_effects(batch) if isinstance(e, SendMessage)]
      == ["first message", "second message"])

# Two cards keep their relative order. Not a scenario today — one card per turn
# — but a stable sort is what makes the answer predictable if it ever happens.
two = [SendMessage(text="m"),
       SendPermissionCard(proposal="A", pending_key="a", tool="t"),
       SendPermissionCard(proposal="B", pending_key="b", tool="t")]
check("two cards run before the message, in emission order",
      [e[1] for e in (lambda c: (runner.run(two, c), c.calls)[1])(FakeChannel())]
      == ["A", "B", "m"])


# ── Nothing is prepended to a card ───────────────────────────────────────────

ch = FakeChannel()
runner.run([SendPermissionCard(proposal="Add to calendar?\nTennis",
                               pending_key="k2", tool="t")], ch)
check("the card's proposal reaches the channel verbatim",
      ch.calls == [("card", "Add to calendar?\nTennis")])


# ── One failing effect does not stop the rest ────────────────────────────────
#
# A batch is not a transaction and cannot be made into one: a message that
# already went out cannot be recalled. The alternative to continuing is a
# failed quip suppressing the card behind it.

ch = FakeChannel(fail_on="send")
report = runner.run([SendMessage(text="doomed"), card], ch)
check("a failed send does not stop the card",
      ("card", "Add to calendar?\nLunch") in ch.calls)
check("a failed send is reported, not swallowed",
      report.failed == ("SendMessage",) and not report.ok)


class ExplodingChannel(FakeChannel):
    def send(self, text: str) -> bool:
        raise RuntimeError("transport is on fire")


ch = ExplodingChannel()
report = runner.run([SendMessage(text="boom"), card], ch)
check("an effect that RAISES does not stop the card",
      ("card", "Add to calendar?\nLunch") in ch.calls)
check("a raising effect is reported", len(report.failed) == 1)


# ── Effects carry data, not behavior ─────────────────────────────────────────

check("an unknown effect sorts last rather than raising",
      runner.order_key(Effect()) == runner._LAST)

_unknown = Effect()
ch = FakeChannel()
report = runner.run([_unknown, card], ch)
check("an unknown effect cannot suppress the card",
      ch.calls[0][0] == "card")

check("effects declared-but-unused are ignored, not run",
      runner.run([ScheduleItem(kind="k", when="w"),
                  CancelScheduled(kind="k", key="x")],
                 FakeChannel()).executed == 0)


# ── Quips are rendered by the runner, and never on a card ───────────────────

import phrases  # noqa: E402

_success = set(phrases._group("commit_calendar_event", "success")[0])

ch = FakeChannel()
runner.run([SendMessage(text="Lunch added.",
                        quip_key="commit_calendar_event:success")], ch)
sent = ch.calls[0][1]
check("a quip is appended to the message", sent != "Lunch added.")
check("the message text still leads", sent.startswith("Lunch added. "))
check("the appended quip comes from the right group",
      sent[len("Lunch added. "):] in _success)

# An empty group appends nothing. Silence beats the wrong vibe.
ch = FakeChannel()
runner.run([SendMessage(text="That didn't work.",
                        quip_key="commit_calendar_event:failure")], ch)
check("an empty quip group appends nothing",
      ch.calls[0][1] == "That didn't work.")

# A key that does not resolve is silence, not a random line.
ch = FakeChannel()
runner.run([SendMessage(text="Plain.", quip_key="no_such_tool:success")], ch)
check("an unresolvable quip key appends nothing", ch.calls[0][1] == "Plain.")

ch = FakeChannel()
runner.run([SendMessage(text="Plain.")], ch)
check("a message with no quip key is sent verbatim", ch.calls[0][1] == "Plain.")

# A card cannot carry a quip: there is nowhere to put one.
check("SendPermissionCard has no quip_key field",
      not hasattr(SendPermissionCard(proposal="p", pending_key="k", tool="t"),
                  "quip_key"))

ch = FakeChannel()
runner.run([SendPermissionCard(proposal="Add to calendar?\nLunch",
                               pending_key="k", tool="t")], ch)
check("a card reaches the channel with nothing appended",
      ch.calls[0][1] == "Add to calendar?\nLunch")

# No-repeat memory: over a full rotation of a group, nothing repeats.
_n = len(_success)
picks = [phrases.quip_for("commit_calendar_event", "success") for _ in range(_n)]
check("a full rotation of a group produces no repeat",
      len(set(picks)) == _n)


print()
if _failures:
    print(f"{len(_failures)} FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
