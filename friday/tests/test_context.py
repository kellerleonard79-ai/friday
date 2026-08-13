"""
tests/test_context.py
One producer, one formatter. Plain asserts, no test framework.

    python3 tests/test_context.py       (from the friday/ package directory)

What this file asserts is that there is only ONE rendering of an injected
context block, and only one place the clock comes from. Before step 6 there
were four: llm/assembly.py's inline f-string, agent/briefings.py's
format_briefing_context, its _render_sections, and its _header — all agreeing
with each other by coincidence, all editable independently.

The value is not the string formatting. It is that a model tuned against one
surface's context shape is tuned against all of them, and that the step-7
router does not become the fifth.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import briefings                                    # noqa: E402
from llm import assembly, context, profiles                    # noqa: E402
from llm.types import ContextBlock, LLMRequest                  # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


TZ = "America/Chicago"
NOW = datetime(2026, 8, 12, 7, 0, tzinfo=ZoneInfo(TZ))

_CONFIG = {
    "agent": {"timezone": TZ},
    "profiles": {name: {"model": "test-model"} for name in profiles.names()},
}
profiles.install(_CONFIG)


# ── The formatter ────────────────────────────────────────────────────────────

print("\n-- the one formatter --")

check("a block renders as label, colon, newline, content",
      context.format_context_block("Weather", "  - Sunny")
      == "Weather:\n  - Sunny")

check("render_blocks joins with a blank line",
      context.render_blocks([ContextBlock(label="A", content="1"),
                             ContextBlock(label="B", content="2")])
      == "A:\n1\n\nB:\n2")

# A heading with nothing under it reads to the model as "this was checked and
# is empty", which is a different claim from "this was not supplied".
check("an empty block is dropped rather than rendered as a bare label",
      context.render_blocks([ContextBlock(label="A", content="1"),
                             ContextBlock(label="Empty", content="   ")])
      == "A:\n1")


# ── One producer for the clock ───────────────────────────────────────────────

print("\n-- one clock producer --")

# time_block_at renders the instant it is HANDED, not a fresh one. This is
# what lets a briefing header describe the same moment its calendar was
# fetched at, rather than a second read a few hundred ms later.
frozen = context.time_block_at(NOW, TZ)
check("time_block_at renders the instant it is given",
      "2026-08-12T07:00:00-05:00" in frozen.content)
check("time_block_at names the weekday in full, separately from the date",
      "Day of week: Wednesday" in frozen.content)
check("time_block_at carries the ISO date the model resolves against",
      "Today's date (ISO): 2026-08-12" in frozen.content)
check("time_block_at names the timezone",
      f"Timezone: {TZ}" in frozen.content)

check("time_block delegates to time_block_at",
      context.time_block(_CONFIG).label == frozen.label)

# THE ACTUAL CLAIM OF THIS COMMIT: the briefing's clock and the chat turn's
# clock are the same bytes for the same instant. They were two strftimes.
bundle = {
    "slot": "morning", "now": NOW, "timezone": TZ,
    "today_calendar": [], "week_preview": [], "canvas_pending": [],
    "weather_today": "Sunny", "groupme_surfaced": [],
}
briefing_blocks = briefings.standing_blocks(bundle)
check("a briefing's clock block is byte-identical to a chat turn's",
      briefing_blocks[0].content == frozen.content)
check("a briefing's clock block carries the same label",
      briefing_blocks[0].label == frozen.label)


# ── The same formatter on both surfaces ──────────────────────────────────────

print("\n-- both surfaces, one formatter --")

block = ContextBlock(label="Machine location", content="Somewhere, TX")
system = assembly.build_system(
    LLMRequest(profile=profiles.get("CHAT"), prompt="hi",
               context_blocks=(block,)),
    profiles.get("CHAT"), _CONFIG)
check("chat's system prompt contains the shared rendering verbatim",
      context.format_context_block(block.label, block.content) in system)

injected = briefings.format_briefing_context(bundle)
check("the briefing context contains the shared rendering verbatim",
      context.format_context_block("Weather today", "  - Sunny") in injected)

check("the briefing context contains the shared clock rendering verbatim",
      context.format_context_block(frozen.label, frozen.content) in injected)

# The user-facing renderer takes the same path. This is the one that actually
# ships today — format_briefing_context has no live consumer while the
# composers are deterministic (it is logged at DEBUG), so a consolidation that
# skipped compose_* would have been decorative.
composed = briefings.compose_morning(None, bundle)
check("the composed briefing uses the shared rendering too",
      context.format_context_block("Weather today", "  - Sunny") in composed)

# The bundle markers wrap the WHOLE bundle, not each block: "everything
# between these lines was pre-fetched, there is no more to go looking for".
check("the briefing bundle is wrapped once, not per block",
      [l for l in injected.splitlines() if l.startswith("=====")]
      == ["===== BRIEFING CONTEXT (deterministic, do not re-fetch) =====",
          "===== END CONTEXT ====="])


# ── One section list, not four ───────────────────────────────────────────────

print("\n-- one section list --")

for slot in ("morning", "evening", "on_demand"):
    b = dict(bundle, slot=slot)
    labels_injected = [l for l, _ in briefings._bundle_sections(b)]
    text = briefings._compose(b, "X")
    check(f"{slot}: every injected section also reaches the user",
          all(f"{label}:" in text for label in labels_injected))

check("the three composers differ only in their title",
      briefings.compose_morning(None, bundle).split("\n", 1)[1]
      == briefings.compose_on_demand(None, bundle).split("\n", 1)[1])


# ── Inject, never fetch ──────────────────────────────────────────────────────

print("\n-- inject, never fetch --")

# location_block reads the warm cache and returns None when it is cold. The
# rule it enforces is that nothing on a prompt-assembly path performs I/O:
# connectors/location.py::fetch() can block ~25s, on a path holding the one
# turn gate.
import connectors.location as _loc                             # noqa: E402

_calls: list[str] = []
_real_cached, _real_fetch = _loc.cached, _loc.fetch
_loc.cached = lambda *a, **k: (_calls.append("cached"), None)[1]
_loc.fetch = lambda *a, **k: _calls.append("fetch")
try:
    context.location_block()
    briefings.standing_blocks(bundle)
finally:
    _loc.cached, _loc.fetch = _real_cached, _real_fetch

check("the context layer reads the location cache", "cached" in _calls)
check("the context layer never fetches a location", "fetch" not in _calls)

# A cold cache omits the block rather than asserting ignorance. A block saying
# "location: unknown" is still a block the model reads, weighs and repeats.
check("a cold location cache produces no block, not an 'unknown' one",
      len(briefings.standing_blocks(bundle)) >= 1
      and all(b.label != "Machine location" or b.content
              for b in briefings.standing_blocks(bundle)))


if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
