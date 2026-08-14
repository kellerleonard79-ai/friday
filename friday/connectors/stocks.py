"""
connectors/stocks.py
Stateless quote fetch for the dashboard's markets widget. No storage, no side
effects, no LLM — a quote is a number with a timestamp, and there is nothing
here for a model to decide.

KEYLESS ON PURPOSE. Every other market data provider (Alpha Vantage, Finnhub,
Polygon) wants an API key, which means a signup, a secret in
friday_config.yaml, and a free tier measured in calls per day that a dashboard
polling every minute would exhaust before lunch. Yahoo's chart endpoint needs
none of that. The cost is that it is undocumented and may change shape without
warning, which is why every field is read defensively and a symbol that fails
is dropped rather than failing the batch.

Read by dashboard/server.py only. Nothing here is injected into a prompt and
nothing here is a tool — see the INJECT, NEVER FETCH note in llm/dispatch.py
for why that distinction matters. This module never reaches a model at all.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

logger = logging.getLogger("friday.stocks")

# TWO HOSTS, tried in order. They are independent frontends onto the same
# data, and a 429 from one is routinely answered by the other on the next
# request — see the UA note below for why 429 is the failure to plan for.
_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
_URL = "https://{host}/v8/finance/chart/{symbol}"

# ⚠️ THE USER-AGENT IS LOAD-BEARING AND SHORTER IS BETTER. Measured against
# the live endpoint, three UAs, same second, same IP:
#
#     python-requests default ............... 429 every time
#     full Chrome 122 UA string ............. 429 on 2 of 3 requests
#     bare "Mozilla/5.0" .................... 200 every time
#
# The obvious "look like a real browser" fix is the one that fails: the
# complete Chrome UA is what every scraper sends, so it is the string Yahoo
# throttles hardest. Do not "improve" this by expanding it, and do not add an
# `Accept: application/json` header — that was measured too and it 429s.
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_TIMEOUT_S = 8
_MAX_SYMBOLS = 12       # the widget is a glance, not a portfolio tracker
_SPARK_POINTS = 32      # downsampled intraday closes for the mini chart

# ── The cache ────────────────────────────────────────────────────────────────
#
# TWO AGES, for the same reason router/fastpath.py's weather cache has two.
# Under _FRESH_S a cached quote is served without a fetch. Past _MAX_AGE_S it
# is not served AT ALL, even when the fetch fails — a price is a claim about
# right now, and a confidently-rendered half-hour-old number is worse than an
# empty widget saying it could not reach the market.
_FRESH_S = 60.0
_MAX_AGE_S = 900.0
_cache: dict[str, tuple[float, dict]] = {}

# Symbols arrive from friday_config.yaml, which the dashboard lets the user
# edit — so they are user input, and they are interpolated into a URL path.
# Yahoo's own symbols use dots (BRK.B), carets (^GSPC), equals (ES=F) and
# dashes (BTC-USD); nothing else is a symbol, and a slash would be a different
# endpoint entirely.
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")


def normalize(tickers: Any) -> list[str]:
    """Config value → a clean, de-duplicated, upper-case symbol list.

    Accepts a list or a comma-separated string, because the dashboard's text
    input produces the second and the YAML produces the first.
    """
    if isinstance(tickers, str):
        raw = tickers.replace("\n", ",").split(",")
    elif isinstance(tickers, (list, tuple)):
        raw = [str(t) for t in tickers]
    else:
        return []

    out: list[str] = []
    for t in raw:
        s = t.strip().upper()
        if not s or not _SYMBOL_RE.match(s):
            continue
        if s not in out:
            out.append(s)
    return out[:_MAX_SYMBOLS]


def _spark(closes: list) -> list[float]:
    """Downsample intraday closes to a fixed-length series for the sparkline.

    Nulls are dropped rather than interpolated: Yahoo emits them for minutes
    with no trade, and inventing a price to keep the array length tidy would
    put a fabricated point on a chart of real ones.
    """
    pts = [float(c) for c in closes if isinstance(c, (int, float))]
    if len(pts) <= _SPARK_POINTS:
        return [round(p, 4) for p in pts]
    step = len(pts) / _SPARK_POINTS
    return [round(pts[min(int(i * step), len(pts) - 1)], 4) for i in range(_SPARK_POINTS)]


def _fetch_one(symbol: str) -> dict | None:
    """One quote, or None. Never raises — a dead symbol must not sink a batch."""
    payload = None
    for host in _HOSTS:
        try:
            r = requests.get(
                _URL.format(host=host, symbol=symbol),
                params={"range": "1d", "interval": "5m"},
                headers=_HEADERS,
                timeout=_TIMEOUT_S,
            )
            if r.status_code == 429:
                # Not a backoff loop and deliberately not one: the second host
                # answers immediately or the symbol waits for the next refresh.
                logger.debug(f"{host} rate-limited {symbol}; trying the next host.")
                continue
            r.raise_for_status()
            payload = r.json()
            break
        except Exception as e:
            logger.debug(f"Quote fetch failed for {symbol} via {host}: {e}")

    if payload is None:
        return None

    try:
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}

        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if not isinstance(price, (int, float)) or not isinstance(prev, (int, float)) or not prev:
            return None

        closes: list = []
        try:
            closes = (result[0]["indicators"]["quote"][0].get("close") or [])
        except (KeyError, IndexError, TypeError):
            closes = []

        change = price - prev
        return {
            "symbol": meta.get("symbol") or symbol,
            "name": meta.get("shortName") or meta.get("longName") or symbol,
            "price": round(float(price), 2),
            "previous_close": round(float(prev), 2),
            "change": round(float(change), 2),
            "change_pct": round(float(change) / float(prev) * 100.0, 2),
            "currency": meta.get("currency") or "USD",
            "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
            "market_time": meta.get("regularMarketTime"),
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "spark": _spark(closes),
        }
    except Exception as e:
        logger.debug(f"Malformed quote payload for {symbol}: {e}")
        return None


def quotes(tickers: Any) -> dict:
    """Quotes for `tickers`.

    Returns {"ok": bool, "quotes": [...], "stale": [...], "error": str}.

    `ok` is True when at least one symbol answered, because a widget showing
    five of six tickers is useful and a widget showing an error because the
    sixth is delisted is not. `stale` names the symbols served from a cache
    older than _FRESH_S, so the UI can say so rather than implying freshness
    it does not have.
    """
    symbols = normalize(tickers)
    if not symbols:
        return {"ok": False, "quotes": [], "stale": [], "error": "No tickers configured."}

    now = time.time()
    fresh: dict[str, dict] = {}
    to_fetch: list[str] = []
    for s in symbols:
        hit = _cache.get(s)
        if hit and (now - hit[0]) < _FRESH_S:
            fresh[s] = hit[1]
        else:
            to_fetch.append(s)

    if to_fetch:
        # Concurrent because these are independent round trips and the widget
        # is refreshed on a timer: six sequential 8s timeouts on a dead network
        # is 48 seconds of a held request, which the browser gives up on first.
        with ThreadPoolExecutor(max_workers=min(6, len(to_fetch))) as pool:
            for symbol, quote in zip(to_fetch, pool.map(_fetch_one, to_fetch)):
                if quote:
                    _cache[symbol] = (now, quote)
                    fresh[symbol] = quote

    out: list[dict] = []
    stale: list[str] = []
    for s in symbols:
        if s in fresh:
            out.append(fresh[s])
            continue
        # The fetch failed. Fall back to the cache only inside the hard bound —
        # past it the entry does not exist, per the two-age rule above.
        hit = _cache.get(s)
        if hit and (now - hit[0]) < _MAX_AGE_S:
            out.append(hit[1])
            stale.append(s)
        else:
            _cache.pop(s, None)

    if not out:
        return {"ok": False, "quotes": [], "stale": [],
                "error": "Could not reach the market data service."}

    missing = [s for s in symbols if s not in {q["symbol"] for q in out}]
    return {
        "ok": True,
        "quotes": out,
        "stale": stale,
        "error": f"No data for {', '.join(missing)}." if missing else "",
    }
