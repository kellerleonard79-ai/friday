"""
connectors/weather.py
Stateless weather fetch. No storage, no side effects.
Called on demand from on_message and injected into the evening briefing.
"""

import logging
import os

import requests

logger = logging.getLogger("friday.weather")

_URL = "https://api.openweathermap.org/data/2.5/weather"


def fetch(cfg: dict) -> str:
    """Return a formatted current-conditions string, or '' on failure/missing config."""
    api_key  = cfg.get("api_key") or os.environ.get("WEATHER_API_KEY", "")
    location = (cfg.get("location") or os.environ.get("WEATHER_LOCATION", "")).strip()
    if not api_key or not location:
        return ""
    try:
        r = requests.get(
            _URL,
            params={"q": location, "appid": api_key, "units": "imperial"},
            timeout=10,
        )
        r.raise_for_status()
        data     = r.json()
        desc     = data["weather"][0]["description"]
        temp     = data["main"]["temp"]
        humidity = data["main"]["humidity"]
        wind     = data["wind"]["speed"]
        return f"{temp:.0f}°F, {desc}. Humidity {humidity}%. Wind {wind:.0f} mph."
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return ""
