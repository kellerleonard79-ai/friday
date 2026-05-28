"""
connectors/weather.py
Stateless weather fetch. No storage, no side effects.
Called on demand from on_message and injected into the evening briefing.
"""

import logging
import os

import requests

logger = logging.getLogger("friday.weather")

_URL_CURRENT  = "https://api.openweathermap.org/data/2.5/weather"
_URL_FORECAST = "https://api.openweathermap.org/data/2.5/forecast"


def fetch(cfg: dict) -> str:
    """Return a formatted current-conditions + near-term forecast string, or '' on failure."""
    api_key  = cfg.get("api_key") or os.environ.get("WEATHER_API_KEY", "")
    location = (cfg.get("location") or os.environ.get("WEATHER_LOCATION", "")).strip()
    if not api_key or not location:
        return ""
    try:
        params = {"q": location, "appid": api_key, "units": "imperial"}

        r = requests.get(_URL_CURRENT, params=params, timeout=10)
        r.raise_for_status()
        data    = r.json()
        city    = data.get("name", location)
        country = data.get("sys", {}).get("country", "")
        source  = f"{city}, {country}" if country else city
        desc    = data["weather"][0]["description"]
        temp    = data["main"]["temp"]
        feels   = data["main"]["feels_like"]
        humidity = data["main"]["humidity"]
        wind    = data["wind"]["speed"]

        # Current precipitation (only present if actively precipitating)
        precip_parts = []
        rain = data.get("rain", {}).get("1h", 0)
        snow = data.get("snow", {}).get("1h", 0)
        if rain:
            precip_parts.append(f"Rain {rain:.1f} mm/hr")
        if snow:
            precip_parts.append(f"Snow {snow:.1f} mm/hr")
        precip = (", ".join(precip_parts) + ". ") if precip_parts else ""

        # Precipitation probability from forecast (next 12 hours, 4 × 3-hr slots)
        pop_str = ""
        try:
            rf = requests.get(_URL_FORECAST, params={**params, "cnt": 4}, timeout=10)
            rf.raise_for_status()
            slots = rf.json().get("list", [])
            if slots:
                max_pop = max(s.get("pop", 0) for s in slots)
                pop_str = f" Chance of rain next 12 hrs: {max_pop * 100:.0f}%."
        except Exception:
            pass

        lines = [
            f"Weather source: OpenWeatherMap ({source})",
            f"Temperature: {temp:.0f}°F (feels like {feels:.0f}°F)",
            f"Conditions: {desc}",
            f"Humidity: {humidity}%",
            f"Wind: {wind:.0f} mph",
        ]
        if precip_parts:
            lines.append(f"Current precipitation: {', '.join(precip_parts)}")
        if pop_str:
            lines.append(f"Chance of rain next 12 hrs: {max_pop * 100:.0f}%")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return ""
