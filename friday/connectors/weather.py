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
        city     = data.get("name", location)
        country  = data.get("sys", {}).get("country", "")
        source   = f"{city}, {country}" if country else city
        desc     = data["weather"][0]["description"]
        temp     = data["main"]["temp"]
        feels    = data["main"]["feels_like"]
        humidity = data["main"]["humidity"]
        wind     = data["wind"]["speed"]

        precip_parts = []
        rain = data.get("rain", {}).get("1h", 0)
        snow = data.get("snow", {}).get("1h", 0)
        if rain:
            precip_parts.append(f"Rain {rain:.1f} mm/hr")
        if snow:
            precip_parts.append(f"Snow {snow:.1f} mm/hr")
        precip = (", ".join(precip_parts) + ". ") if precip_parts else ""

        return (
            f"[{source} via OpenWeatherMap] "
            f"{temp:.0f}°F (feels like {feels:.0f}°F), {desc}. "
            f"{precip}"
            f"Humidity {humidity}%. Wind {wind:.0f} mph."
        )
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return ""
