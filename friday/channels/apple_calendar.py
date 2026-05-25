"""
channels/apple_calendar.py
Apple Calendar adapter for Project Friday.
Reads upcoming events using macOS AppleScript — no OAuth required.
Works with iCloud calendars and any locally synced calendar.
"""

import subprocess
import logging
import json
from datetime import datetime, timedelta

logger = logging.getLogger("friday.apple_calendar")


class AppleCalendarChannel:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get("enabled", False)

    def _run_applescript(self, script: str) -> str:
        """Run an AppleScript and return stdout."""
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode != 0:
                logger.error(f"AppleScript error: {result.stderr}")
                return ""
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("AppleScript timed out reading calendar")
            return ""
        except Exception as e:
            logger.error(f"Failed to run AppleScript: {e}")
            return ""

    def get_events(self, days_ahead: int = 2) -> list:
        """
        Fetch upcoming calendar events for the next `days_ahead` days.
        Returns a list of event dicts with title, start, end, calendar, location.
        """
        if not self.enabled:
            return []

        # AppleScript to get events from now through days_ahead
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

        # Sort by start time string (works for macOS date strings)
        events.sort(key=lambda e: e["start"])

        logger.info(f"Apple Calendar: found {len(events)} upcoming event(s)")
        return events

    def create_event(self, title: str, date_str: str, time_str: str = "",
                     duration_minutes: int = 60, calendar_name: str = "") -> bool:
        """
        Create a new event in Apple Calendar using AppleScript.
        date_str: human-readable date like "tomorrow", "May 9", "2026-05-09"
        time_str: human-readable time like "8:00 AM", "14:00"
        """
        if not self.enabled:
            logger.error("Apple Calendar is disabled in config")
            return False

        # Build the AppleScript date string
        # We use a two-step approach: first resolve the date via osascript date parsing
        time_part = f" at {time_str}" if time_str else " at 12:00 PM"
        cal_clause = f'set theCal to calendar "{calendar_name}"' if calendar_name \
            else "set theCal to default calendar"

        script = f'''
set eventTitle to "{title.replace('"', '\\"')}"
set startDateStr to "{date_str}{time_part}"
set eventDuration to {duration_minutes} * minutes

tell application "Calendar"
    {cal_clause}
    set startDate to (current date)
    set startDate to date startDateStr
    set endDate to startDate + eventDuration
    set newEvent to make new event at end of events of theCal with properties {{summary:eventTitle, start date:startDate, end date:endDate}}
end tell
return "success"
'''
        result = self._run_applescript(script)
        if result == "success" or result == "":
            logger.info(f"Created calendar event: {title} on {date_str} {time_str}")
            return True
        else:
            logger.error(f"Failed to create calendar event: {result}")
            return False

    def format_for_briefing(self, days_ahead: int = 2) -> str:
        """
        Return a formatted string of upcoming events suitable for
        inclusion in the evening briefing prompt.
        """
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

    def create_event(self, title: str, date_str: str, time_str: str = "",
                     duration_minutes: int = 60, calendar_name: str = "") -> bool:
        """
        Create a new event in Apple Calendar using AppleScript.

        date_str: natural language or formatted date e.g. "tomorrow", "May 9, 2026"
        time_str: e.g. "8:00 AM", "14:30" — leave empty for all-day event
        duration_minutes: length of event in minutes (default 60)
        calendar_name: specific calendar to add to, or empty for default
        """
        if not self.enabled:
            logger.warning("Apple Calendar not enabled — cannot create event")
            return False

        safe_title = title.replace('"', '\\"')
        safe_date = date_str.replace('"', '\\"')
        safe_time = time_str.replace('"', '\\"')

        if time_str:
            # Timed event
            date_time_str = f"{safe_date} at {safe_time}"
            script = f'''
set eventDate to date "{date_time_str}"
set eventEnd to eventDate + ({duration_minutes} * minutes)

tell application "Calendar"
'''
            if calendar_name:
                safe_cal = calendar_name.replace('"', '\\"')
                script += f'''
    set targetCal to first calendar whose name is "{safe_cal}"
'''
            else:
                script += '''
    set targetCal to first calendar whose writable is true
'''
            script += f'''
    tell targetCal
        make new event with properties {{summary:"{safe_title}", start date:eventDate, end date:eventEnd}}
    end tell
    reload calendars
end tell
return "success"
'''
        else:
            # All-day event
            script = f'''
set eventDate to date "{safe_date}"

tell application "Calendar"
'''
            if calendar_name:
                safe_cal = calendar_name.replace('"', '\\"')
                script += f'''
    set targetCal to first calendar whose name is "{safe_cal}"
'''
            else:
                script += '''
    set targetCal to first calendar whose writable is true
'''
            script += f'''
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
