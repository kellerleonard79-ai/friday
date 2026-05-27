"""
channels/apple_calendar.py
Apple Calendar adapter for Project Friday.
Reads, creates, edits, and deletes events using macOS AppleScript.
No OAuth required — works with iCloud and any locally synced calendar.
"""

import re
import subprocess
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("friday.apple_calendar")


class AppleCalendarChannel:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get("enabled", False)

    # ── AppleScript runner ────────────────────────────────────────────────────

    def _run_applescript(self, script: str) -> str:
        """Run an AppleScript and return stdout, or empty string on error."""
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode != 0:
                logger.error(f"AppleScript error: {result.stderr.strip()}")
                return ""
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("AppleScript timed out")
            return ""
        except Exception as e:
            logger.error(f"Failed to run AppleScript: {e}")
            return ""

    # ── Date/time parser ─────────────────────────────────────────────────────

    def _parse_event_datetime(self, date_str: str, time_str: str = "") -> datetime | None:
        """
        Parse date_str + optional time_str into a datetime.
        Handles ISO dates (2026-05-29), natural language (May 29 2026),
        and relative terms (today, tomorrow, Thursday).
        Returns None if the date cannot be parsed.
        """
        date_str = (date_str or "").strip()
        time_str = (time_str or "").strip()

        dt = None

        # ISO date: "2026-05-29"
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            pass

        # Natural language with year: "May 29 2026", "May 29, 2026"
        if dt is None:
            for fmt in ("%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y"):
                try:
                    dt = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    pass

        # Natural language without year: "May 29", "May 29th" — assume current/next year
        if dt is None:
            clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str, flags=re.I)
            for fmt in ("%B %d", "%b %d"):
                try:
                    parsed = datetime.strptime(clean, fmt)
                    year = datetime.now().year
                    dt = parsed.replace(year=year)
                    if dt.date() < datetime.now().date():
                        dt = dt.replace(year=year + 1)
                    break
                except ValueError:
                    pass

        # Relative terms
        if dt is None:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            lower = date_str.lower()
            if lower == "today":
                dt = today
            elif lower == "tomorrow":
                dt = today + timedelta(days=1)
            else:
                days_map = {
                    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                    "friday": 4, "saturday": 5, "sunday": 6,
                }
                for name, num in days_map.items():
                    if name in lower:
                        ahead = (num - today.weekday()) % 7 or 7
                        dt = today + timedelta(days=ahead)
                        break

        if dt is None:
            logger.error(f"Cannot parse date: '{date_str}'")
            return None

        # Parse time
        if time_str:
            # Normalize: "8am" → "8 AM", "8:30am" → "8:30 AM"
            normalized = re.sub(r'([ap])m', r' \1m', time_str, flags=re.I).upper().strip()
            for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
                try:
                    t = datetime.strptime(normalized, fmt)
                    dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                    return dt
                except ValueError:
                    pass
            logger.warning(f"Cannot parse time '{time_str}' — creating all-day event")

        return dt

    def _as_script(self, dt: datetime) -> str:
        """Return AppleScript lines that set a variable 'd' to the given datetime."""
        seconds = dt.hour * 3600 + dt.minute * 60
        return (
            f"set year of d to {dt.year}\n"
            f"    set month of d to {dt.month}\n"
            f"    set day of d to {dt.day}\n"
            f"    set time of d to {seconds}"
        )

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_events(self, days_ahead: int = 2) -> list:
        """
        Fetch upcoming events from now through days_ahead days.
        Returns list of dicts: title, start, end, calendar, location.
        """
        if not self.enabled:
            return []

        script = f'''
set eventList to {{}}
set startDate to current date
set endDate to startDate + ({days_ahead} * days)

tell application "Calendar"
    repeat with cal in calendars
        set calName to name of cal
        set theEvents to (every event of cal whose start date >= startDate and start date <= endDate)
        repeat with ev in theEvents
            set evTitle to summary of ev
            set evStart to start date of ev
            set evEnd to end date of ev
            set evLocation to ""
            try
                set evLocation to location of ev
            end try
            set evEntry to evTitle & "||" & (evStart as string) & "||" & (evEnd as string) & "||" & calName & "||" & evLocation
            set end of eventList to evEntry
        end repeat
    end repeat
end tell

set AppleScript's text item delimiters to "~~"
set output to eventList as string
set AppleScript's text item delimiters to ""
return output
'''
        raw = self._run_applescript(script)

        if not raw:
            logger.info("No calendar events returned (calendar may be empty or disabled)")
            return []

        events = []
        for entry in raw.split("~~"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split("||")
            if len(parts) >= 4:
                events.append({
                    "title": parts[0].strip(),
                    "start": parts[1].strip(),
                    "end": parts[2].strip(),
                    "calendar": parts[3].strip(),
                    "location": parts[4].strip() if len(parts) > 4 else ""
                })

        events.sort(key=lambda e: e["start"])
        logger.info(f"Apple Calendar: found {len(events)} upcoming event(s)")
        return events

    def find_events(self, query: str, days_ahead: int = 14) -> list:
        """
        Search for events matching a keyword within the next days_ahead days.
        Returns a filtered list of event dicts.
        """
        all_events = self.get_events(days_ahead=days_ahead)
        query_lower = query.lower()
        matches = [e for e in all_events if query_lower in e["title"].lower()
                   or query_lower in e["location"].lower()
                   or query_lower in e["calendar"].lower()]
        logger.info(f"Calendar search '{query}': {len(matches)} match(es)")
        return matches

    def format_for_briefing(self, days_ahead: int = 2) -> str:
        """Return a formatted string of upcoming events for the evening briefing."""
        events = self.get_events(days_ahead=days_ahead)
        if not events:
            return "No upcoming calendar events found."
        lines = []
        for ev in events:
            location = f" @ {ev['location']}" if ev['location'] else ""
            lines.append(
                f"- {ev['title']} | {ev['start']} → {ev['end']}{location} [{ev['calendar']}]"
            )
        return "\n".join(lines)

    # ── Create ────────────────────────────────────────────────────────────────

    def create_event(self, title: str, date_str: str, time_str: str = "",
                     duration_minutes: int = 60, calendar_name: str = "") -> bool:
        """
        Create a new event in Apple Calendar.

        date_str: ISO "2026-05-29", natural "May 29 2026", or relative "Thursday"
        time_str: "8:00 AM", "14:30", "8am" — leave empty for all-day event
        duration_minutes: length in minutes (default 60)
        calendar_name: specific calendar to add to, or empty for first writable calendar
        """
        if not self.enabled:
            logger.warning("Apple Calendar not enabled — cannot create event")
            return False

        dt = self._parse_event_datetime(date_str, time_str)
        if dt is None:
            logger.error(f"create_event: unparseable date '{date_str}' time '{time_str}'")
            return False

        end_dt = dt + timedelta(minutes=duration_minutes)
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')

        if calendar_name:
            safe_cal = calendar_name.replace('"', '\\"')
            cal_clause = f'set targetCal to first calendar whose name is "{safe_cal}"'
        else:
            cal_clause = "set targetCal to first calendar whose writable is true"

        script = f'''
tell application "Calendar"
    {cal_clause}
    set d to current date
    {self._as_script(dt)}
    set startDate to d
    set d to current date
    set year of d to {end_dt.year}
    set month of d to {end_dt.month}
    set day of d to {end_dt.day}
    set time of d to {end_dt.hour * 3600 + end_dt.minute * 60}
    set endDate to d
    tell targetCal
        make new event with properties {{summary:"{safe_title}", start date:startDate, end date:endDate}}
    end tell
    reload calendars
end tell
return "success"
'''
        result = self._run_applescript(script)
        if "success" in result.lower():
            logger.info(f"Calendar event created: '{title}' on {dt.isoformat()}")
            return True
        logger.error(f"Failed to create calendar event: result='{result}'")
        return False

    # ── Edit ─────────────────────────────────────────────────────────────────

    def edit_event(self, title_search: str, new_title: str = "",
                   new_date_str: str = "", new_time_str: str = "",
                   new_duration_minutes: int = 0, new_location: str = "",
                   days_ahead: int = 30) -> bool:
        """
        Find an event by title keyword and edit its properties.
        Only fields with non-empty/non-zero values are updated.
        """
        if not self.enabled:
            logger.warning("Apple Calendar not enabled — cannot edit event")
            return False

        update_clauses = []

        if new_title:
            safe_new_title = new_title.replace('"', '\\"')
            update_clauses.append(f'set summary of targetEvent to "{safe_new_title}"')

        if new_date_str or new_time_str:
            new_dt = self._parse_event_datetime(new_date_str or "today", new_time_str)
            if new_dt:
                dur = new_duration_minutes if new_duration_minutes > 0 else 60
                end_dt = new_dt + timedelta(minutes=dur)
                update_clauses.append(
                    f"set d to current date\n"
                    f"        set year of d to {new_dt.year}\n"
                    f"        set month of d to {new_dt.month}\n"
                    f"        set day of d to {new_dt.day}\n"
                    f"        set time of d to {new_dt.hour * 3600 + new_dt.minute * 60}\n"
                    f"        set start date of targetEvent to d\n"
                    f"        set d to current date\n"
                    f"        set year of d to {end_dt.year}\n"
                    f"        set month of d to {end_dt.month}\n"
                    f"        set day of d to {end_dt.day}\n"
                    f"        set time of d to {end_dt.hour * 3600 + end_dt.minute * 60}\n"
                    f"        set end date of targetEvent to d"
                )
            else:
                logger.warning(f"edit_event: could not parse new date '{new_date_str}' '{new_time_str}'")

        if new_location:
            safe_loc = new_location.replace('"', '\\"')
            update_clauses.append(f'set location of targetEvent to "{safe_loc}"')

        if not update_clauses:
            logger.warning("edit_event called with no changes specified")
            return False

        safe_search = title_search.replace('"', '\\"').lower()
        update_block = "\n        ".join(update_clauses)

        script = f'''
set searchTerm to "{safe_search}"
set foundIt to false
set startDate to current date
set endDate to startDate + ({days_ahead} * days)

tell application "Calendar"
    repeat with cal in calendars
        set theEvents to (every event of cal whose start date >= startDate and start date <= endDate)
        repeat with ev in theEvents
            if (summary of ev as string) contains searchTerm then
                set targetEvent to ev
                {update_block}
                set foundIt to true
                exit repeat
            end if
        end repeat
        if foundIt then exit repeat
    end repeat
    reload calendars
end tell

if foundIt then
    return "success"
else
    return "not_found"
end if
'''
        result = self._run_applescript(script)
        if "success" in result.lower():
            logger.info(f"Calendar event edited: search='{title_search}'")
            return True
        elif "not_found" in result.lower():
            logger.warning(f"Calendar edit: no event found matching '{title_search}'")
            return False
        logger.error(f"Calendar edit failed: result='{result}'")
        return False

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_event(self, title_search: str, days_ahead: int = 30) -> bool:
        """
        Find and delete an event by title keyword.
        Deletes the first matching upcoming event.
        """
        if not self.enabled:
            logger.warning("Apple Calendar not enabled — cannot delete event")
            return False

        safe_search = title_search.replace('"', '\\"').lower()

        script = f'''
set searchTerm to "{safe_search}"
set foundIt to false
set startDate to current date
set endDate to startDate + ({days_ahead} * days)

tell application "Calendar"
    repeat with cal in calendars
        set theEvents to (every event of cal whose start date >= startDate and start date <= endDate)
        repeat with ev in theEvents
            if (summary of ev as string) contains searchTerm then
                delete ev
                set foundIt to true
                exit repeat
            end if
        end repeat
        if foundIt then exit repeat
    end repeat
    reload calendars
end tell

if foundIt then
    return "success"
else
    return "not_found"
end if
'''
        result = self._run_applescript(script)
        if "success" in result.lower():
            logger.info(f"Calendar event deleted: search='{title_search}'")
            return True
        elif "not_found" in result.lower():
            logger.warning(f"Calendar delete: no event found matching '{title_search}'")
            return False
        logger.error(f"Calendar delete failed: result='{result}'")
        return False
