"""
tests/test_gating.py
The gating table, every cell. Plain asserts, no test framework.

    python3 tests/test_gating.py       (from the friday/ package directory)

The inferred row has no live producer today — the only input channel is the
user typing. It is tested anyway, because the connectors that will produce
inferred facts arrive later and that is the wrong moment to be deciding what
the gate means.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy.gating import Action, decide, needs_card  # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


# ── The table ────────────────────────────────────────────────────────────────
#
#                       | Reversible (create, update) | Irreversible (delete)
#   User stated         | AUTO                        | GATED
#   Friday inferred     | GATED                       | GATED

check("user-stated create is AUTO",
      decide(Action("create", "user_stated")) == "AUTO")
check("user-stated update with a proven target is AUTO",
      decide(Action("update", "user_stated", target_proven=True)) == "AUTO")
check("user-stated delete is GATED",
      decide(Action("delete", "user_stated")) == "GATED")

check("inferred create is GATED",
      decide(Action("create", "inferred")) == "GATED")
check("inferred update is GATED even with a proven target",
      decide(Action("update", "inferred", target_proven=True)) == "GATED")
check("inferred delete is GATED",
      decide(Action("delete", "inferred")) == "GATED")


# ── AUTO_UPDATE needs the target proven by a read IN THE SAME TURN ───────────
#
# The user stated the change. They did not state WHICH EVENT. A target the
# model produced from its memory of the conversation rather than from a read
# is an inference about which event was meant, and inferences are gated.

check("user-stated update with an UNPROVEN target is GATED",
      decide(Action("update", "user_stated", target_proven=False)) == "GATED")
check("proving the target is what flips it",
      decide(Action("update", "user_stated", target_proven=False)) == "GATED"
      and decide(Action("update", "user_stated", target_proven=True)) == "AUTO")

# A create needs no proven target and must not accidentally acquire one:
# creating does not require having looked first.
check("a create is AUTO without a proven target",
      decide(Action("create", "user_stated", target_proven=False)) == "AUTO")

# Irreversibility outranks everything. There is no combination that lets a
# delete through without a card.
check("no combination makes a delete AUTO",
      all(decide(Action("delete", p, target_proven=t)) == "GATED"
          for p in ("user_stated", "inferred") for t in (True, False)))


# ── needs_card is the same answer, phrased for a caller ──────────────────────

check("needs_card agrees with decide",
      needs_card(Action("create", "inferred"))
      and not needs_card(Action("create", "user_stated")))


print()
if _failures:
    print(f"{len(_failures)} FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
