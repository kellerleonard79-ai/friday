"""
agent/preprocess.py
0-token date pre-processing. Resolves relative temporal expressions in message
text to ISO-8601 strings locally before any text reaches the Gemini API.

Public API:
    annotate_with_resolved_dates(text, now=None) -> str
        Returns text unchanged if no temporal expressions found.
        Appends [Resolved dates: ...] block otherwise.
"""

import re
from datetime import datetime, timedelta

_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Named time-of-day defaults (used when no explicit clock time given)
_TIME_OF_DAY = {
    "tonight":   "19:00",
    "evening":   "18:00",
    "afternoon": "14:00",
    "noon":      "12:00",
    "morning":   "09:00",
    "midnight":  "00:00",
}


def _next_weekday(now: datetime, weekday: int, modifier: str = "next") -> datetime:
    """Return the nearest future occurrence of a weekday (0=Mon, 6=Sun)."""
    days_ahead = weekday - now.weekday()
    if modifier == "next":
        if days_ahead <= 0:
            days_ahead += 7
    else:  # "this"
        if days_ahead < 0:
            days_ahead += 7
    return now + timedelta(days=days_ahead)


def extract_temporal_expressions(text: str, now: datetime) -> list:
    """
    Scan text for temporal expressions and resolve each to an ISO-8601 string.
    Returns list of (matched_expression, resolved_iso_string) tuples.
    Duplicate expressions are deduplicated.
    """
    tl = text.lower()
    seen = set()
    resolved = []

    def add(expr, iso):
        key = expr.strip().lower()
        if key not in seen:
            seen.add(key)
            resolved.append((expr.strip(), iso))

    # ── Simple relative days ───────────────────────────────────────────────────
    if re.search(r'\btoday\b', tl):
        add("today", now.strftime("%Y-%m-%d"))
    if re.search(r'\btomorrow\b', tl):
        add("tomorrow", (now + timedelta(days=1)).strftime("%Y-%m-%d"))
    if re.search(r'\byesterday\b', tl):
        add("yesterday", (now - timedelta(days=1)).strftime("%Y-%m-%d"))

    # ── Named time-of-day (resolve to today's date + time) ────────────────────
    for word, time_str in _TIME_OF_DAY.items():
        if re.search(rf'\b{word}\b', tl):
            add(word, f"{now.strftime('%Y-%m-%d')}T{time_str}")

    # ── "next week" / "next month" ────────────────────────────────────────────
    if re.search(r'\bnext\s+week\b', tl):
        next_mon = now + timedelta(days=(7 - now.weekday()))
        add("next week", f"week of {next_mon.strftime('%Y-%m-%d')}")
    if re.search(r'\bnext\s+month\b', tl):
        if now.month == 12:
            add("next month", f"{now.year + 1}-01")
        else:
            add("next month", f"{now.year}-{now.month + 1:02d}")

    # ── "next/this + weekday" (takes priority over bare weekday match) ─────────
    mod_day = re.compile(
        r'\b(next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
        re.IGNORECASE
    )
    modifier_covered = set()
    for m in mod_day.finditer(tl):
        modifier, day_name = m.group(1).lower(), m.group(2).lower()
        resolved_dt = _next_weekday(now, _WEEKDAY_MAP[day_name], modifier)
        add(m.group(0), resolved_dt.strftime("%Y-%m-%d"))
        modifier_covered.add(day_name)

    # ── Bare weekday names (only if not already covered by next/this) ──────────
    bare_day = re.compile(
        r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
        re.IGNORECASE
    )
    for m in bare_day.finditer(tl):
        day_name = m.group(1).lower()
        if day_name not in modifier_covered:
            resolved_dt = _next_weekday(now, _WEEKDAY_MAP[day_name], "next")
            add(day_name, resolved_dt.strftime("%Y-%m-%d"))

    # ── Specific times: "8 PM", "7:30 AM", "8:00 pm" ─────────────────────────
    clock_pat = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)
    for m in clock_pat.finditer(tl):
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3).lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        add(m.group(0), f"{hour:02d}:{minute:02d}")

    # ── Month name + day: "May 5", "March 3rd", "Jan 15th" ────────────────────
    month_day = re.compile(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'\s+(\d{1,2})(?:st|nd|rd|th)?\b',
        re.IGNORECASE
    )
    for m in month_day.finditer(tl):
        month_key = m.group(1)[:3].lower()
        month_int = _MONTH_MAP.get(month_key)
        day_int = int(m.group(2))
        if not month_int:
            continue
        try:
            target = datetime(now.year, month_int, day_int)
            if target.date() < now.date():
                target = datetime(now.year + 1, month_int, day_int)
            add(m.group(0), target.strftime("%Y-%m-%d"))
        except ValueError:
            continue

    # ── Numeric date: "5/9", "5/9/26", "5-9" ─────────────────────────────────
    num_date = re.compile(r'\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b')
    for m in num_date.finditer(text):
        try:
            month, day = int(m.group(1)), int(m.group(2))
            if month > 12 or day > 31:
                continue
            year_raw = m.group(3)
            year = now.year
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            target = datetime(year, month, day)
            add(m.group(0), target.strftime("%Y-%m-%d"))
        except ValueError:
            continue

    return resolved


def annotate_with_resolved_dates(text: str, now: datetime = None) -> str:
    """
    Append a [Resolved dates: ...] block to text when temporal expressions are found.
    Returns text unchanged if nothing resolves.

    Example:
        Input:  "Let's meet tomorrow at 8 PM"
        Output: "Let's meet tomorrow at 8 PM
                 [Resolved dates: \"tomorrow\" → 2026-05-27, \"8 PM\" → 20:00]"
    """
    if not text or not text.strip():
        return text
    if now is None:
        now = datetime.now()
    resolved = extract_temporal_expressions(text, now)
    if not resolved:
        return text
    parts = [f'"{expr}" → {iso}' for expr, iso in resolved]
    return text + "\n[Resolved dates: " + ", ".join(parts) + "]"
