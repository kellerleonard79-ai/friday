"""
connectors/weather.py
Stateless weather fetch. No storage, no side effects.
Called on demand from on_message and injected into the evening briefing.
"""

import logging
import os
import re
from datetime import datetime, timedelta

import requests

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


def respond(cfg: dict, query: str = "") -> str:
    """Return a natural-language answer to a weather query, or '' on failure."""
    api_key  = cfg.get("api_key") or os.environ.get("WEATHER_API_KEY", "")
    location = (cfg.get("location") or os.environ.get("WEATHER_LOCATION", "")).strip()
    if not api_key or not location:
        return ""
    try:
        params = {"q": location, "appid": api_key, "units": "imperial"}
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
