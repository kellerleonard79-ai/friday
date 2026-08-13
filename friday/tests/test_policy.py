"""
tests/test_policy.py
Visibility and suppression. Plain asserts, no test framework.

    python3 tests/test_policy.py       (from the friday/ package directory)

Two rules here have live consumers and two do not, and the file says which is
which. A module with one real function and a documented plan is better than a
module with invented rules, because inventing them now means step 8 inherits
decisions it never made.

    LIVE:  the GroupMe briefing tiers, the Canvas briefing urgencies, and the
           already-alerted latch. All three were SQL literals, two of them
           duplicated across surfaces.
    NOT:   the windowed briefing-echo rule and the completed-item rule. Both
           are specified, both are tested, and their consumer is the to-do
           list in step 8.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy import suppression, visibility                      # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


# ── Visibility: the GroupMe tiers (LIVE) ─────────────────────────────────────

print("\n-- visibility: groupme tiers --")

check("high surfaces in a briefing", visibility.surfaces_in_briefing("high"))
check("normal surfaces in a briefing", visibility.surfaces_in_briefing("normal"))
# The entire meaning of the tier: ingested for history, never surfaced.
check("muted never surfaces", not visibility.surfaces_in_briefing("muted"))
check("an unset tier does not surface", not visibility.surfaces_in_briefing(None))
check("an unrecognised tier does not surface",
      not visibility.surfaces_in_briefing("shouty"))
check("tier matching is case- and whitespace-insensitive",
      visibility.surfaces_in_briefing("  HIGH "))

# THE DRIFT GUARD. policy/ must not import connectors/, so the tier names are
# restated here — and a restated list is a list that goes stale. This is what
# makes renaming a tier fail a test instead of quietly turning the briefing
# filter into a pass-through, which is what it would have done before step 6
# when the tags were two literal LIKE patterns.
from connectors.groupme import PRIORITIES                       # noqa: E402

check("every surfacing tier is a real tier",
      set(visibility.BRIEFING_TIERS) <= set(PRIORITIES))
check("muted is a real tier that is deliberately not surfaced",
      "muted" in PRIORITIES and "muted" not in visibility.BRIEFING_TIERS)

check("the body tags are derived from the tiers, not restated",
      visibility.briefing_tier_tags()
      == tuple(f"[priority={t}]" for t in visibility.BRIEFING_TIERS))


# ── Visibility: the completed rule (NO CONSUMER YET) ─────────────────────────

print("\n-- visibility: completed items (no consumer until step 8) --")

for surface in ("briefing", "telegram", "dashboard"):
    check(f"a completed item is invisible on {surface}",
          not visibility.visible_on(surface, completed=True))

# The record still has to exist — "did I already do that" is a real question.
check("a completed item stays in dashboard history",
      visibility.visible_on("dashboard_history", completed=True))

for surface in ("briefing", "telegram", "dashboard", "dashboard_history"):
    check(f"an open item is visible on {surface}",
          visibility.visible_on(surface, completed=False))


# ── Suppression: the latch (LIVE) ────────────────────────────────────────────

print("\n-- suppression: the already-alerted latch --")

check("an alerted event is suppressed", suppression.already_alerted(1))
check("an un-alerted event is not", not suppression.already_alerted(0))
check("SQLite's NULL reads as not alerted", not suppression.already_alerted(None))


# ── Suppression: the window (NO CONSUMER YET) ────────────────────────────────

print("\n-- suppression: the briefing-echo window (no consumer until step 8) --")

NOW = datetime(2026, 8, 12, 20, 0)

check("an item surfaced an hour ago suppresses its reminder",
      suppression.recently_surfaced(NOW - timedelta(hours=1), NOW))
check("an item surfaced beyond the window does not",
      suppression.recently_surfaced(NOW - timedelta(hours=13), NOW) is False)
check("the window boundary is exclusive",
      not suppression.recently_surfaced(
          NOW - timedelta(hours=suppression.DEFAULT_BRIEFING_WINDOW_HOURS), NOW))
check("the window is overridable per caller",
      suppression.recently_surfaced(NOW - timedelta(hours=13), NOW,
                                    within_hours=24))

# NEVER-SURFACED SUPPRESSES NOTHING. The opposite of a precondition: blocking
# on missing data would silence the FIRST reminder for every item, which is
# the only one that matters.
check("an item never surfaced suppresses nothing",
      not suppression.recently_surfaced(None, NOW))
check("an unparseable timestamp suppresses nothing",
      not suppression.recently_surfaced("not a date", NOW))
check("a zero window suppresses nothing",
      not suppression.recently_surfaced(NOW, NOW, within_hours=0))

check("an ISO string is accepted, like a SQLite row gives it",
      suppression.recently_surfaced((NOW - timedelta(hours=2)).isoformat(), NOW))

# Naive and aware datetimes both reach this module — SQLite gives back
# whatever was written, clock.local_now() is aware. Guessing a timezone for
# the naive one is how an off-by-five-hours window is born.
aware = NOW.replace(tzinfo=timezone.utc)
check("a naive/aware mismatch answers False rather than raising",
      suppression.recently_surfaced(NOW - timedelta(hours=1), aware) is False)
check("two aware datetimes compare normally",
      suppression.recently_surfaced(aware - timedelta(hours=1), aware))


# ── The two questions stay separate ──────────────────────────────────────────

print("\n-- visibility is not suppression --")

# Folded together they become one predicate that is false for two unrelated
# reasons, and the first bug report is "why did it stop telling me about X".
check("suppression does not answer a visibility question",
      not hasattr(suppression, "visible_on"))
check("visibility does not answer a suppression question",
      not hasattr(visibility, "recently_surfaced"))
# Policy decides and acts on nothing — same contract as gating.py.
for mod in (visibility, suppression):
    src = Path(mod.__file__).read_text()
    check(f"{mod.__name__} performs no I/O",
          ".execute(" not in src and "conn." not in src
          and "import sqlite3" not in src and "requests" not in src)


if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
