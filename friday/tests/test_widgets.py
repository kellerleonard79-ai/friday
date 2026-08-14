"""
tests/test_widgets.py
The dashboard's weather and markets widgets. Plain asserts, no test framework.

    python3 tests/test_widgets.py       (from the friday/ package directory)

NO NETWORK. Every fetch is stubbed. The live endpoints were verified by hand
when this was built and a test that depends on them fails on school Wi-Fi,
which is the seven hours a day the dashboard exists for.

What is actually worth asserting here is the SYMBOL VALIDATION and the TWO-AGE
CACHE, because both are load-bearing and neither is visible from the UI:

  - symbols reach a URL path and come from a config the dashboard lets the
    user edit, so normalize() is the boundary between a text input and an
    outbound request;
  - the two ages are what stop a half-hour-old price rendering as a live one.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors import stocks                                   # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


# ── normalize(): the boundary between a text input and a URL path ────────────

print("\n-- stocks.normalize --")

check("upper-cases and trims",
      stocks.normalize([" aapl ", "msft"]) == ["AAPL", "MSFT"])
check("accepts a comma-separated string (what the dashboard input produces)",
      stocks.normalize("aapl, msft,nvda") == ["AAPL", "MSFT", "NVDA"])
check("accepts newlines too",
      stocks.normalize("AAPL\nMSFT") == ["AAPL", "MSFT"])
check("de-duplicates, keeping order",
      stocks.normalize(["AAPL", "MSFT", "aapl"]) == ["AAPL", "MSFT"])
check("keeps Yahoo's own punctuation — dots, carets, dashes, equals",
      stocks.normalize(["BRK.B", "^GSPC", "BTC-USD", "ES=F"])
      == ["BRK.B", "^GSPC", "BTC-USD", "ES=F"])

# The reason this function exists at all.
check("drops a path traversal", stocks.normalize(["../../etc/passwd"]) == [])
check("drops a slash", stocks.normalize(["AAPL/quote"]) == [])
check("drops a query string", stocks.normalize(["AAPL?x=1"]) == [])
check("drops whitespace inside a symbol", stocks.normalize(["A APL"]) == [])
check("drops something absurdly long", stocks.normalize(["A" * 40]) == [])
check("caps the list at twelve", len(stocks.normalize([f"SYM{i}" for i in range(30)])) == 12)
check("a non-list, non-string is empty rather than an exception",
      stocks.normalize(None) == [] and stocks.normalize(42) == [])


# ── _spark(): the sparkline series ───────────────────────────────────────────

print("\n-- stocks._spark --")

check("short series passes through", stocks._spark([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0])
check("long series is downsampled to a fixed length",
      len(stocks._spark([float(i) for i in range(500)])) == stocks._SPARK_POINTS)
# Yahoo emits null for a minute with no trade. Interpolating would put a
# fabricated point on a chart of real ones.
check("nulls are dropped, not interpolated",
      stocks._spark([1.0, None, 3.0]) == [1.0, 3.0])
check("an all-null series is empty, not zeros",
      stocks._spark([None, None]) == [])


# ── quotes(): the two-age cache ──────────────────────────────────────────────

print("\n-- stocks.quotes: the cache --")

_calls: list[str] = []


def _stub_ok(symbol: str):
    _calls.append(symbol)
    return {"symbol": symbol, "name": symbol, "price": 100.0, "previous_close": 99.0,
            "change": 1.0, "change_pct": 1.01, "currency": "USD", "exchange": "",
            "market_time": 0, "day_high": None, "day_low": None, "spark": []}


def _stub_dead(symbol: str):
    _calls.append(symbol)
    return None


_real_fetch = stocks._fetch_one
try:
    stocks._cache.clear()
    _calls.clear()
    stocks._fetch_one = _stub_ok

    r = stocks.quotes(["AAPL"])
    check("a live fetch answers ok", r["ok"] and r["quotes"][0]["symbol"] == "AAPL")
    check("nothing is marked stale on a live fetch", r["stale"] == [])

    _calls.clear()
    stocks.quotes(["AAPL"])
    check("a second call inside the fresh window does not refetch", _calls == [])

    # Age the entry past _FRESH_S but well inside _MAX_AGE_S, then kill the
    # fetch. The cached price is the best available and is served — labelled.
    ts, q = stocks._cache["AAPL"]
    stocks._cache["AAPL"] = (time.time() - (stocks._FRESH_S + 5), q)
    stocks._fetch_one = _stub_dead
    r = stocks.quotes(["AAPL"])
    check("a failed fetch inside the hard bound serves the cache",
          r["ok"] and r["quotes"][0]["symbol"] == "AAPL")
    check("...and says so via `stale`", r["stale"] == ["AAPL"])

    # Past _MAX_AGE_S the entry does not exist. This is the whole point of the
    # second age: a stale price rendered confidently is worse than no widget.
    stocks._cache["AAPL"] = (time.time() - (stocks._MAX_AGE_S + 5), q)
    r = stocks.quotes(["AAPL"])
    check("past the hard bound the cache is NOT served", not r["ok"])
    check("...and the entry is dropped rather than left to rot",
          "AAPL" not in stocks._cache)

    # A batch must not be sunk by one bad symbol.
    stocks._cache.clear()
    stocks._fetch_one = lambda s: _stub_ok(s) if s != "DEAD" else _stub_dead(s)
    r = stocks.quotes(["AAPL", "DEAD", "MSFT"])
    check("one dead symbol does not fail the batch",
          r["ok"] and [q["symbol"] for q in r["quotes"]] == ["AAPL", "MSFT"])
    check("...and the dead one is named in `error`", "DEAD" in r["error"])

    check("no configured tickers is a clean not-ok, not an exception",
          stocks.quotes([])["ok"] is False)
finally:
    stocks._fetch_one = _real_fetch
    stocks._cache.clear()


# ── weather.snapshot(): configuration and failure ────────────────────────────

print("\n-- weather.snapshot --")

import os                                                       # noqa: E402
from connectors import location, weather                        # noqa: E402

# The env fallbacks would make "is it configured" depend on the shell this ran
# from, so they are cleared for the duration.
_saved_env = {k: os.environ.pop(k, None) for k in ("WEATHER_API_KEY", "WEATHER_LOCATION")}
try:
    weather._snapshot_cache.clear()
    r = weather.snapshot({})
    check("an unconfigured weather block is not-ok rather than an exception",
          r["ok"] is False)
    check("...and the error names the fix rather than the failure",
          "Integrations" in r["error"])
    check("a key with no location is still not-ok",
          weather.snapshot({"api_key": "x"})["ok"] is False)

    # A configured-but-unreachable service must fail soft, not raise. The
    # widget renders whatever comes back and there is no try/except above it.
    _real_get = weather.requests.get

    def _boom(*a, **kw):
        raise OSError("network is unreachable")

    weather.requests.get = _boom
    try:
        r = weather.snapshot({"api_key": "x", "location": "Nowhere,US"})
        check("an unreachable service is a clean not-ok", r["ok"] is False)
        check("...with a message the panel can render",
              isinstance(r.get("error"), str) and bool(r["error"]))
    finally:
        weather.requests.get = _real_get
finally:
    for k, v in _saved_env.items():
        if v is not None:
            os.environ[k] = v
    weather._snapshot_cache.clear()


# ── weather._here(): which location wins ─────────────────────────────────────

print("\n-- weather._here: source precedence --")

_real_cached = location.cached
_saved_wx_env = {k: os.environ.pop(k, None) for k in ("WEATHER_API_KEY", "WEATHER_LOCATION")}
try:
    # A live machine fix, toggle on (the default) -> lat/lon wins.
    location.cached = lambda: {"lat": 30.4, "lon": -87.2, "place": "Somewhere",
                                "source": "corelocation"}
    here = weather._here({"location": "Pensacola,US", "use_machine_location": True})
    check("a live fix wins when the toggle is on",
          here is not None and here[0] == {"lat": 30.4, "lon": -87.2})
    check("...and reports its real source, not a guess",
          here[3] == "corelocation")

    # Toggle off -> the configured string wins even though a fix exists.
    here = weather._here({"location": "Pensacola,US", "use_machine_location": False})
    check("toggle off pins the configured string despite a live fix",
          here is not None and here[0] == {"q": "Pensacola,US"} and here[3] == "configured")

    # Toggle on, no fix yet -> falls back to the configured string rather
    # than failing. A cold boot must answer with the old behavior.
    location.cached = lambda: None
    here = weather._here({"location": "Pensacola,US", "use_machine_location": True})
    check("no fix yet falls back to the configured string",
          here is not None and here[0] == {"q": "Pensacola,US"} and here[3] == "configured")

    # Neither a fix nor a configured string -> unconfigured, not a KeyError.
    here = weather._here({"use_machine_location": True})
    check("no fix and no configured string is None, not an exception", here is None)

    # Two fixes a few metres apart must not fragment the cache — that would
    # mean an always-on Mac refetches every poll because Wi-Fi positioning
    # jittered the fourth decimal.
    location.cached = lambda: {"lat": 30.42879, "lon": -87.17999, "place": "P",
                                "source": "ip"}
    key_a = weather._here({"use_machine_location": True})[1]
    location.cached = lambda: {"lat": 30.42881, "lon": -87.18001, "place": "P",
                                "source": "ip"}
    key_b = weather._here({"use_machine_location": True})[1]
    check("sub-100m jitter in the fix does not fragment the cache key", key_a == key_b)
finally:
    location.cached = _real_cached
    for k, v in _saved_wx_env.items():
        if v is not None:
            os.environ[k] = v


print(f"\n{'ALL PASS' if not _failures else 'FAILURES: ' + ', '.join(_failures)}")
sys.exit(1 if _failures else 0)
