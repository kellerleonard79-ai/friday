"""
connectors/weather.py
Stateless weather fetch. No storage, no side effects.
Called on demand from on_message and injected into the evening briefing.
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any

import requests

import connectors.location as location

logger = logging.getLogger("friday.weather")

_URL_CURRENT  = "https://api.openweathermap.org/data/2.5/weather"
_URL_FORECAST = "https://api.openweathermap.org/data/2.5/forecast"

_RAIN_WORDS = {"rain", "precipitation", "umbrella", "drizzle", "storm", "wet", "shower", "showers", "snowing", "snow"}
_TEMP_WORDS = {"temperature", "temp", "hot", "cold", "warm", "cool", "feel", "feels", "degrees"}


def _intent(query: str) -> str:
    q = query.lower()
    if any(w in q for w in _RAIN_WORDS):
        return "rain"
    if any(w in q for w in _TEMP_WORDS):
        return "temp"
    return "general"


def _parse_time(query: str, now: datetime):
    """Return (start_hour, end_hour, label, tomorrow) or None for no specific time."""
    q = query.lower()

    # Explicit hour: "at 3pm", "at 3", "3pm", "3 pm"
    m = re.search(r'\bat\s+(\d{1,2})\s*(am|pm)?\b', q) or re.search(r'\b(\d{1,2})\s*(am|pm)\b', q)
    if m:
        hour = int(m.group(1))
        meridiem = (m.group(2) or "").strip()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        label = f"{m.group(1)}{meridiem or ''}"
        return (hour, hour + 3, label, "tomorrow" in q)

    tomorrow = "tomorrow" in q
    if "tonight" in q:
        return (18, 24, "tonight", False)
    if "this afternoon" in q or "afternoon" in q:
        return (12, 18, "this afternoon", tomorrow)
    if "this morning" in q or "morning" in q:
        return (6, 12, "this morning", tomorrow)
    if "this evening" in q or "evening" in q:
        return (17, 21, "this evening", tomorrow)
    if tomorrow:
        return (6, 21, "tomorrow", True)

    return None


def _slot_label(dt: datetime) -> str:
    h = dt.hour
    if h == 0:
        return "midnight"
    if h < 12:
        return f"{h}am"
    if h == 12:
        return "noon"
    return f"{h - 12}pm"


# ── Where "here" means ───────────────────────────────────────────────────────
#
# Two sources compete for "here", and the config decides which wins:
#
#   - `weather.location` — a "City,CC" string the user typed once.
#   - connectors/location.py — the machine's own fix. CoreLocation on macOS
#     (tens of metres) if the TCC grant is live, IP geolocation (city-level)
#     otherwise. Warmed every 15 minutes by the poll job, so reading it here
#     is location.cached() — no I/O, never the ~25s fetch() path. That
#     matters on both callers: respond() runs while router/fastpath.py holds
#     TURN_GATE, and snapshot() runs on this process's shared PTB loop.
#
# `weather.use_machine_location` defaults to True. False pins the configured
# string — for a laptop Friday travels with, or anyone who would rather type
# a city once than trust Wi-Fi positioning.
#
# Falls back to the configured string whenever there is no live fix yet (the
# poll job hasn't warmed the cache, both location backends are down), so a
# cold boot answers with the old behavior rather than nothing.
def _here(cfg: dict) -> tuple[dict, str, str, str] | None:
    """(request_params, cache_key, display_name, source) for "here", or None
    if neither a machine fix nor a configured location is available.

    `source` is "corelocation" (device-level, tens of metres), "ip"
    (device-level lookup, city-level precision) or "configured" (the typed
    string) — surfaced in the widget so a user who assumes the toggle bought
    GPS-grade accuracy can see that, absent the TCC grant, it bought the same
    city-level fix a typed string would have given them.
    """
    use_machine = cfg.get("use_machine_location", True)
    if use_machine:
        fix = location.cached()
        if fix is not None:
            lat, lon = fix["lat"], fix["lon"]
            key = f"geo:{lat:.3f},{lon:.3f}"
            return ({"lat": lat, "lon": lon}, key, fix.get("place") or "",
                    fix.get("source") or "ip")

    loc = (cfg.get("location") or os.environ.get("WEATHER_LOCATION", "")).strip()
    if loc:
        return ({"q": loc}, f"city:{loc}", loc, "configured")
    return None


def respond(cfg: dict, query: str = "") -> str:
    """Return a natural-language answer to a weather query, or '' on failure."""
    api_key = cfg.get("api_key") or os.environ.get("WEATHER_API_KEY", "")
    here = _here(cfg)
    if not api_key or here is None:
        return ""
    try:
        params = {**here[0], "appid": api_key, "units": "imperial"}
        now    = datetime.now()
        intent = _intent(query)

        # Current conditions
        r = requests.get(_URL_CURRENT, params=params, timeout=10)
        r.raise_for_status()
        cur     = r.json()
        temp    = cur["main"]["temp"]
        feels   = cur["main"]["feels_like"]
        desc    = cur["weather"][0]["description"]
        src     = "(OpenWeatherMap)"

        if intent == "temp":
            return f"It's {temp:.0f}°F right now, feels like {feels:.0f}°F. {src}"

        if intent == "general":
            return f"Currently {temp:.0f}°F, {desc}. Feels like {feels:.0f}°F. {src}"

        # Rain intent — fetch 24-hour forecast (8 × 3-hr slots)
        rf = requests.get(_URL_FORECAST, params={**params, "cnt": 8}, timeout=10)
        rf.raise_for_status()
        slots = rf.json().get("list", [])

        time_range = _parse_time(query, now)

        if time_range:
            start_h, end_h, label, use_tomorrow = time_range
            target_date = (now + timedelta(days=1)).date() if use_tomorrow else now.date()

            matching = [
                s for s in slots
                if datetime.fromtimestamp(s["dt"]).date() == target_date
                and start_h <= datetime.fromtimestamp(s["dt"]).hour < end_h
            ]

            if not matching:
                # Nearest slot to target window
                target_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=start_h)
                matching = sorted(slots, key=lambda s: abs(datetime.fromtimestamp(s["dt"]) - target_dt))[:1]

            if matching:
                best  = max(matching, key=lambda s: s.get("pop", 0))
                pop   = round(best.get("pop", 0) * 100)
                slot_dt = datetime.fromtimestamp(best["dt"])
                slot_lbl = _slot_label(slot_dt)
                if pop >= 20:
                    return f"There's a {pop}% chance of rain {label} (around {slot_lbl}). {src}"
                else:
                    return f"No significant rain expected {label}. {src}"

        # No specific time — find next notable rain window today
        today_slots = [s for s in slots if datetime.fromtimestamp(s["dt"]).date() == now.date()]
        rainy = [s for s in today_slots if s.get("pop", 0) >= 0.3]
        if rainy:
            best   = max(rainy, key=lambda s: s.get("pop", 0))
            pop    = round(best.get("pop", 0) * 100)
            lbl    = _slot_label(datetime.fromtimestamp(best["dt"]))
            return f"Rain likely around {lbl} today ({pop}% chance). {src}"

        max_pop = max((s.get("pop", 0) for s in today_slots), default=0)
        if max_pop > 0:
            return f"Low chance of rain today (up to {round(max_pop * 100)}%). {src}"
        return f"No rain expected today. {src}"

    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return ""


# Keep fetch() as an alias so the evening briefing can still call it for a full summary
def fetch(cfg: dict) -> str:
    return respond(cfg, "")


# ── The dashboard snapshot ───────────────────────────────────────────────────
#
# respond() above answers a QUESTION and returns a SENTENCE, because its caller
# is router/fastpath.py and its output goes to a person reading a chat message.
# The dashboard widget needs neither: it needs numbers it can lay out itself.
#
# So this is a second entry point rather than a parse of the first. Rendering a
# sentence and then picking it apart with a regex to find the temperature is
# how a display ends up depending on the exact wording of a chat reply — and
# that wording is the one thing in this file most likely to be edited.
#
# respond() is deliberately untouched. It has a live consumer in the router's
# tier 1, which catches 13.7% of real traffic, and this widget is not a reason
# to reshape it.

_SNAP_TIMEOUT_S = 10

# The snapshot cache. Same two-age rule as router/fastpath.py's weather cache
# and connectors/stocks.py's quote cache — under _SNAP_FRESH_S serve the cache,
# past _SNAP_MAX_AGE_S the entry does not exist even when the fetch fails.
# A dashboard is glanced at, not read, which is exactly the reading where a
# stale number gets believed.
_SNAP_FRESH_S = 600.0       # 10 minutes
_SNAP_MAX_AGE_S = 3600.0    # 1 hour
_snapshot_cache: dict[str, tuple[float, dict]] = {}


def _slot_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts)


def snapshot(cfg: dict) -> dict:
    """Structured current conditions plus a short forecast, for the dashboard.

    Returns {"ok": bool, ...}. Never raises — a widget that throws takes the
    page down with it, and the weather is the least important thing on it.
    """
    api_key = cfg.get("api_key") or os.environ.get("WEATHER_API_KEY", "")
    here = _here(cfg)
    if not api_key or here is None:
        return {"ok": False, "error": "Weather is not configured — set a location and an "
                                      "OpenWeatherMap key under Integrations."}
    here_params, cache_key, fallback_name, source = here

    now_ts = time.time()
    hit = _snapshot_cache.get(cache_key)
    if hit and (now_ts - hit[0]) < _SNAP_FRESH_S:
        return {**hit[1], "age_seconds": int(now_ts - hit[0])}

    params = {**here_params, "appid": api_key, "units": "imperial"}
    try:
        r = requests.get(_URL_CURRENT, params=params, timeout=_SNAP_TIMEOUT_S)
        r.raise_for_status()
        cur = r.json()

        main = cur.get("main") or {}
        weather0 = (cur.get("weather") or [{}])[0]
        wind = cur.get("wind") or {}
        sys_ = cur.get("sys") or {}

        out: dict[str, Any] = {
            "ok": True,
            "error": "",
            "location": cur.get("name") or fallback_name,
            "location_source": source,
            "temp": round(float(main.get("temp", 0))),
            "feels_like": round(float(main.get("feels_like", 0))),
            "description": (weather0.get("description") or "").title(),
            "icon": weather0.get("icon") or "",
            "condition": weather0.get("main") or "",
            "humidity": main.get("humidity"),
            "wind_mph": round(float(wind.get("speed", 0))),
            "sunrise": sys_.get("sunrise"),
            "sunset": sys_.get("sunset"),
            "observed_at": cur.get("dt"),
            "high": None,
            "low": None,
            "forecast": [],
        }

        # The daily high/low comes from the FORECAST, not from the current
        # endpoint's main.temp_min / temp_max. Those two look like a daily
        # range and are not — for a single station they are the current
        # reading, so they render as "H 88 L 87" on a day that will reach 95.
        try:
            rf = requests.get(_URL_FORECAST, params={**params, "cnt": 8},
                              timeout=_SNAP_TIMEOUT_S)
            rf.raise_for_status()
            slots = rf.json().get("list", []) or []
        except Exception as e:
            logger.debug(f"Forecast leg of the snapshot failed: {e}")
            slots = []

        today = datetime.now().date()
        today_temps = [
            s["main"]["temp"] for s in slots
            if _slot_dt(s["dt"]).date() == today and (s.get("main") or {}).get("temp") is not None
        ]
        if today_temps:
            out["high"] = round(max([*today_temps, main.get("temp", today_temps[0])]))
            out["low"] = round(min([*today_temps, main.get("temp", today_temps[0])]))

        for s in slots[:5]:
            sw = (s.get("weather") or [{}])[0]
            out["forecast"].append({
                "label": _slot_label(_slot_dt(s["dt"])),
                "temp": round(float((s.get("main") or {}).get("temp", 0))),
                "icon": sw.get("icon") or "",
                "condition": sw.get("main") or "",
                "pop": round(float(s.get("pop", 0)) * 100),
            })

        _snapshot_cache[cache_key] = (now_ts, out)
        return {**out, "age_seconds": 0}

    except Exception as e:
        logger.warning(f"Weather snapshot failed: {e}")
        # Inside the hard bound a cached snapshot is better than nothing, and
        # it is labelled with its age so the widget can say so.
        if hit and (now_ts - hit[0]) < _SNAP_MAX_AGE_S:
            return {**hit[1], "age_seconds": int(now_ts - hit[0])}
        _snapshot_cache.pop(cache_key, None)
        return {"ok": False, "error": "Could not reach the weather service."}
