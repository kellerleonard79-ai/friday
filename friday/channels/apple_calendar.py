"""
channels/apple_calendar.py
Apple Calendar adapter for Project Friday.
Reads, creates, edits, and deletes events using macOS AppleScript.
No OAuth required — works with iCloud and any locally synced calendar.
"""

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
        """Run an AppleScript and return stdout."""
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
        Used for conversational queries like "what's my tennis match this week?"
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

        date_str: natural language or formatted date e.g. "May 9, 2026", "tomorrow"
        time_str: e.g. "8:00 AM", "14:30" — leave empty for all-day event
        duration_minutes: length in minutes (default 60)
        calendar_name: specific calendar to add to, or empty for first writable calendar
        """
        if not self.enabled:
            logger.warning("Apple Calendar not enabled — cannot create event")
            return False

        safe_title = title.replace('"', '\\"')
        safe_date = date_str.replace('"', '\\"')
        safe_time = time_str.replace('"', '\\"')

        if calendar_name:
            safe_cal = calendar_name.replace('"', '\\"')
            cal_clause = f'set targetCal to first calendar whose name is "{safe_cal}"'
        else:
            cal_clause = "set targetCal to first calendar whose writable is true"

        if time_str:
            script = f'''
set eventDate to date "{safe_date} at {safe_time}"
set eventEnd to eventDate + ({duration_minutes} * minutes)
tell application "Calendar"
    {cal_clause}
    tell targetCal
        make new event with properties {{summary:"{safe_title}", start date:eventDate, end date:eventEnd}}
    end tell
    reload calendars
end tell
return "success"
'''
        else:
            script = f'''
set eventDate to date "{safe_date}"
tell application "Calendar"
    {cal_clause}
    tell targetCal
        make new event with properties {{summary:"{safe_title}", start date:eventDate, end date:eventDate, allday event:true}}
    end tell
    reload calendars
end tell
return "success"
'''
        result = self._run_applescript(script)
        if "success" in result.lower() or result == "":
            logger.info(f"Calendar event created: '{title}' on {date_str} {time_str}")
            return True
        else:
            logger.error(f"Failed to create calendar event: {result}")
            return False

    # ── Edit ─────────────────────────────────────────────────────────────────

    def edit_event(self, title_search: str, new_title: str = "",
                   new_date_str: str = "", new_time_str: str = "",
                   new_duration_minutes: int = 0, new_location: str = "",
                   days_ahead: int = 30) -> bool:
        """
        Find an event by title keyword and edit its properties.
        Only fields with non-empty values are updated.

        title_search: keyword to find the event (case-insensitive partial match)
        new_title: replacement title, or "" to keep existing
        new_date_str: new date string, or "" to keep existing
        new_time_str: new time string, or "" to keep existing
        new_duration_minutes: new duration, or 0 to keep existing
        new_location: new location, or "" to keep existing
        """
        if not self.enabled:
            logger.warning("Apple Calendar not enabled — cannot edit event")
            return False

        safe_search = title_search.replace('"', '\\"').lower()

        # Build property update clauses
        update_clauses = []
        if new_title:
            safe_new_title = new_title.replace('"', '\\"')
            update_clauses.append(f'set summary of targetEvent to "{safe_new_title}"')
        if new_date_str:
            safe_new_date = new_date_str.replace('"', '\\"')
            if new_time_str:
                safe_new_time = new_time_str.replace('"', '\\"')
                update_clauses.append(
                    f'set newStart to date "{safe_new_date} at {safe_new_time}"\n'
                    f'        set start date of targetEvent to newStart'
                )
                if new_duration_minutes > 0:
                    update_clauses.append(
                        f'set end date of targetEvent to newStart + ({new_duration_minutes} * minutes)'
                    )
                else:
                    update_clauses.append(
                        f'set end date of targetEvent to newStart + (60 * minutes)'
                    )
            else:
                update_clauses.append(
                    f'set newStart to date "{safe_new_date}"\n'
                    f'        set start date of targetEvent to newStart'
                )
        if new_location:
            safe_loc = new_location.replace('"', '\\"')
            update_clauses.append(f'set location of targetEvent to "{safe_loc}"')

        if not update_clauses:
            logger.warning("edit_event called with no changes specified")
            return False

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
            set evTitle to summary of ev
            if (evTitle as string) contains searchTerm then
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
        else:
            logger.error(f"Calendar edit failed: {result}")
            return False

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_event(self, title_search: str, days_ahead: int = 30) -> bool:
        """
        Find and delete an event by title keyword.
        Deletes the first matching upcoming event.

        title_search: keyword to match against event title (case-insensitive)
        days_ahead: how far ahead to search (default 30 days)
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
            set evTitle to summary of ev
            if (evTitle as string) contains searchTerm then
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
        else:
            logger.error(f"Calendar delete failed: {result}")
            return False