"""
tests/test_tools.py
Tests for the tool layer. Plain asserts, no test framework — the repo has no
test dependency and adding one to run nine assertions is a bad trade.

    python3 tests/test_tools.py        (from the friday/ package directory)

The precondition and ledger tests are the reason this file exists. Step 4's
first gated write depends on that mechanism, and debugging it while pointed at
a real calendar is the wrong time to find out it does not work.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ledger import CalendarReadFor, Ledger          # noqa: E402
from tools.registry import ToolSpec, _build_parameters    # noqa: E402
from tools.types import ToolError, ToolResult             # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


# ── The ledger records coverage, not that a read happened ────────────────────

led = Ledger()
led.record_calendar_read(date(2026, 8, 17), date(2026, 8, 24))

check("a read of next week does not cover today",
      not led.covers_calendar(date(2026, 8, 11), date(2026, 8, 12)))
check("a read of next week covers a day inside it",
      led.covers_calendar(date(2026, 8, 18), date(2026, 8, 19)))
check("a read does not cover a range extending past its end",
      not led.covers_calendar(date(2026, 8, 23), date(2026, 8, 26)))
check("an empty ledger covers nothing",
      not Ledger().covers_calendar(date(2026, 8, 11), date(2026, 8, 12)))

# Adjacent reads are deliberately not stitched: a gap between two spans must
# not become invisible, and being strict costs one tool call while being wrong
# costs a double-booked calendar.
split = Ledger()
split.record_calendar_read(date(2026, 8, 11), date(2026, 8, 12))
split.record_calendar_read(date(2026, 8, 12), date(2026, 8, 13))
check("two adjacent reads are not stitched into one span",
      not split.covers_calendar(date(2026, 8, 11), date(2026, 8, 13)))

# ── Preconditions ────────────────────────────────────────────────────────────

pre = CalendarReadFor(date_field="date")
check("precondition fails when the day was not read",
      pre.check(led, {"date": "2026-08-11"}) is not None)
check("precondition passes when the day was read",
      pre.check(led, {"date": "2026-08-18"}) is None)
check("precondition reports a missing date field",
      pre.check(led, {}) is not None)
check("precondition reports an unparseable date",
      pre.check(led, {"date": "next tuesday"}) is not None)

# ── Schema derivation ────────────────────────────────────────────────────────

from typing import Annotated  # noqa: E402


def _sample(alpha: Annotated[str, "the first one"], beta: int = 3) -> ToolResult:
    """when to call it"""
    return ToolResult()


params = _build_parameters(_sample, "sample")
by_name = {p.name: p for p in params}
check("a parameter without a default is required", by_name["alpha"].required)
check("a parameter with a default is not required", not by_name["beta"].required)
check("Annotated metadata becomes the description",
      by_name["alpha"].description == "the first one")
check("int maps to the integer JSON type", by_name["beta"].json_type == "integer")


def _unannotated(alpha) -> ToolResult:
    """when to call it"""
    return ToolResult()


try:
    _build_parameters(_unannotated, "unannotated")
    check("an unannotated parameter is a registration error", False)
except TypeError:
    check("an unannotated parameter is a registration error", True)

# ── Validation: the missing-parameter gate ───────────────────────────────────

import yaml  # noqa: E402

from calendars import backend as _backend  # noqa: E402
from tools import calendar_read as _cal, executor, registry  # noqa: E402

_cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "friday_config.yaml"))
_backend.init(_cfg)
_cal.configure(_cfg)

_schedule = registry.get("get_schedule")
_free = registry.get("find_free_blocks")

# The tool must not run. If it did, this would take ~30s against the real
# calendar rather than milliseconds — so the timing is itself the assertion.
import time as _time  # noqa: E402

_t = _time.monotonic()
_out, _ms = executor.run(_schedule, {"date_from": "2026-08-12"})
_elapsed = _time.monotonic() - _t

check("a missing required parameter yields missing_parameter",
      isinstance(_out, ToolError) and _out.kind == "missing_parameter")
check("the missing-parameter error names the field",
      isinstance(_out, ToolError) and _out.field == "date_to")
check("the tool never executed (no calendar read occurred)", _elapsed < 1.0)

_clean, _err = executor.validate(_free, {"date": "2026-08-12", "min_minutes": "90"})
check("a numeric string coerces to int", _err is None and _clean["min_minutes"] == 90)

_clean, _err = executor.validate(_free, {"date": "2026-08-12", "bogus": 1})
check("an undeclared argument is dropped, not passed through",
      _err is None and "bogus" not in _clean)

_clean, _err = executor.validate(_free, {"date": "2026-08-12", "min_minutes": "soon"})
check("an uncoercible argument yields invalid_argument",
      isinstance(_err, ToolError) and _err.kind == "invalid_argument")

check("bool coercion does not treat 'false' as True",
      executor._coerce("false", "boolean") is False)


# ── ToolError shape ──────────────────────────────────────────────────────────

err = ToolError(kind="missing_parameter", message="date_from is required", field="date_from")
check("a ToolError names its field in the payload",
      err.as_content()["field"] == "date_from")
check("a ToolError payload carries its kind",
      err.as_content()["error"] == "missing_parameter")

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    sys.exit(1)
print("all passed")
