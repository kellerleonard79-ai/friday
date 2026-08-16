"""
connectors/canvas.py
Read-only Canvas LMS connector. Never calls the LLM. NEVER WRITES TO CANVAS.

TWO LAYERS, AND THE SPLIT IS ABOUT WHAT CAN EXPIRE.

    iCal   The feed URL carries its own embedded secret, so it keeps working
           after the API token dies — measured: a 401 from REST and a 200 from
           the feed, the same minute, on the same account. It yields course
           ids, course display names, titles and DUE DAYS.
    REST   Needs a live Bearer token. Adds the real course_code, the due
           TIME, points, and submission status. Enrichment only: every field
           it fills is one the card can render without.

The token WILL expire again (this one lasted a semester), so REST is wired as
a layer that can fail without taking anything with it. A 401 leaves the iCal
rows exactly where they were and flips one status flag the dashboard reads.

THREE ENTRY POINTS, AND ONLY ONE OF THEM TOUCHES THE NETWORK.

    refresh()      poll cycle only. Network, slow, never raises.
    courses()      pure SQLite read. Safe on the request path.
    assignments()  pure SQLite read. Safe on the request path.

Canvas took 1.4s for the feed alone on a good connection. The dashboard reads
the cache tables and nothing else — see memory/db.py for why fetched_at is
stored rather than assumed fresh.

fetch() and sync_to_calendar() below are the ORIGINAL deterministic due-date
path and are deliberately untouched: they write Canvas due dates into the
calendar through actions/calendar.py::auto_write, which is a different job
from populating a card.
"""

import logging
import re
import sqlite3
from datetime import date as date_cls, datetime, time, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

import memory.state as state
from actions import calendar as apple_writer

logger = logging.getLogger("friday.canvas")

# Canvas puts the course on the end of every summary as "[NAME-TEACHER]" and
# the numeric course id in the event URL's include_contexts param. Both are
# 100% present on the live feed (65/65 events, 5 courses) — but a miss is a
# skipped row, never an exception: this is scraped text, not an API contract.
_COURSE_BRACKET = re.compile(r"\s*\[([^\]]+)\]\s*$")
_COURSE_ID_IN_URL = re.compile(r"course_(\d+)")

# UID shapes: 'event-assignment-3186449' and 'event-calendar-event-134510'.
_UID_ASSIGNMENT = re.compile(r"assignment-(\d+)")
_UID_CAL_EVENT = re.compile(r"calendar-event-(\d+)")

_REST_TIMEOUT = 30
_REST_MAX_PAGES = 10   # per collection; 100/page is already generous


def fetch(cfg: dict, conn: sqlite3.Connection) -> int:
    """
    Fetch new Canvas events from the iCal feed and write them to the events table.
    Returns the count of new events written.
    """
    ical_url  = cfg.get("ical_url", "").strip()
    api_token = cfg.get("api_token", "").strip()

    if not ical_url:
        return 0

    # Fetch iCal feed
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
    try:
        r = requests.get(ical_url, headers=headers, timeout=30)
        if r.status_code == 401 or r.status_code == 403:
            logger.error(f"Canvas auth failed ({r.status_code}). Check api_token in config.")
            return 0
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Canvas fetch failed: {e}")
        return 0

    # Parse iCal
    try:
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        logger.error(f"Canvas iCal parse error: {e}")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    count   = 0

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        try:
            uid     = str(component.get("UID", "")).strip()
            title   = str(component.get("SUMMARY", "")).strip()
            body    = str(component.get("DESCRIPTION", "")).strip()[:2000]
            dtstart = component.get("DTSTART")

            if not uid or not title:
                continue

            due_at = None
            if dtstart:
                dt = dtstart.dt
                if hasattr(dt, "isoformat"):
                    due_at = dt.isoformat()

            event_id = f"canvas_{uid}"

            # Deduplicate — iCal feeds return the full calendar every poll
            exists = conn.execute(
                "SELECT 1 FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if exists:
                continue

            conn.execute(
                """INSERT INTO events
                   (id, source, title, body, due_at, urgency, processed, notified, created_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 0, 0, ?)""",
                (event_id, "canvas", title, body, due_at, now_iso),
            )
            count += 1

        except Exception as e:
            logger.error(f"Canvas: error processing event {component.get('UID', '?')}: {e}")
            continue

    if count:
        conn.commit()

    # Update last_seen cursor
    conn.execute(
        "INSERT OR REPLACE INTO last_seen (source, cursor, updated_at) VALUES (?, ?, ?)",
        ("canvas", now_iso, now_iso),
    )
    conn.commit()

    return count


def sync_to_calendar(config: dict, conn: sqlite3.Connection) -> int:
    """Write unsynced Canvas events with a future due_at to the 'Canvas'
    calendar (auto_write handles the default-calendar fallback).
    Idempotent via events.calendar_synced. Returns count of new writes."""
    agent_cfg = config.get("agent") or {}
    tz_name   = agent_cfg.get("timezone", "America/Chicago")
    default_cal = agent_cfg.get("default_calendar")
    local_tz  = ZoneInfo(tz_name)
    now_local = datetime.now(local_tz)

    rows = conn.execute(
        "SELECT id, title, body, due_at FROM events "
        "WHERE source='canvas' AND due_at IS NOT NULL AND calendar_synced = 0"
    ).fetchall()

    written = 0
    for event_id, title, body, due_at in rows:
        try:
            date_str, time_str, due_dt_local = _due_at_local(due_at, local_tz)
        except Exception as e:
            logger.error(f"canvas sync — bad due_at {due_at!r}: {e}")
            # Unparseable — mark synced so we don't retry forever.
            conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE id = ?", (event_id,),
            )
            continue
        if due_dt_local < now_local:
            conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE id = ?", (event_id,),
            )
            continue
        event = {
            "title":    title,
            "date":     date_str,
            "calendar": "Canvas",
            "notes":    (body or "")[:1000],
        }
        if time_str:
            event["start_time"] = time_str
        uid = apple_writer.auto_write(event, default_calendar=default_cal)  # silent
        if uid:
            conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE id = ?", (event_id,),
            )
            written += 1
        # else: leave flag at 0; next 15-min poll retries.
    if rows:
        conn.commit()
    return written


def _due_at_local(due_at_iso: str, tz: ZoneInfo) -> tuple[str, str | None, datetime]:
    """(YYYY-MM-DD, HH:MM or None, local-aware datetime). All-day inputs
    (no 'T') stay all-day; their comparison datetime is midnight local."""
    if "T" not in due_at_iso:
        d = date_cls.fromisoformat(due_at_iso)
        return due_at_iso[:10], None, datetime.combine(d, time.min, tzinfo=tz)
    dt = datetime.fromisoformat(due_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    dt_local = dt.astimezone(tz)
    return dt_local.strftime("%Y-%m-%d"), dt_local.strftime("%H:%M"), dt_local


# ── The cache: courses and assignments for the period card ───────────────────
#
# Everything below fills or reads canvas_courses / canvas_assignments. None of
# it touches the events table, writes to the calendar, or calls the LLM.


def base_url(cfg: dict) -> str:
    """The Canvas host to talk REST to.

    Explicit `canvas.base_url` wins; otherwise it is derived from the iCal
    URL's origin, which is the same host in every deployment and saves asking
    the user for something they have already given us. Returns '' when neither
    is available, which is how the REST layer stays off rather than guessing.
    """
    explicit = (cfg.get("base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    ical = (cfg.get("ical_url") or "").strip()
    if not ical:
        return ""
    parts = urlparse(ical)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _ical_item(component) -> dict | None:
    """One VEVENT → a cache row, or None if it cannot be attributed to a course.

    The course id comes from the URL and the display name from the summary's
    trailing bracket. A row missing either is skipped: an assignment that
    cannot be attributed to a course cannot be shown on a period card, and
    inventing a placeholder course would put it on the wrong one.
    """
    uid = str(component.get("UID", "")).strip()
    summary = str(component.get("SUMMARY", "")).strip()
    url = str(component.get("URL", "")).strip()
    if not uid or not summary:
        return None

    course_match = _COURSE_ID_IN_URL.search(url)
    bracket = _COURSE_BRACKET.search(summary)
    if not course_match or not bracket:
        return None

    if _UID_ASSIGNMENT.search(uid):
        kind, num = "assignment", _UID_ASSIGNMENT.search(uid).group(1)
    elif _UID_CAL_EVENT.search(uid):
        kind, num = "event", _UID_CAL_EVENT.search(uid).group(1)
    else:
        return None

    due_at = None
    dtstart = component.get("DTSTART")
    if dtstart is not None and hasattr(dtstart.dt, "isoformat"):
        due_at = dtstart.dt.isoformat()

    # The feed publishes due dates as all-day VALUE=DATE. A bare date has no
    # 'T'; anything else came through with a real time on it.
    has_time = bool(due_at and "T" in due_at)

    return {
        "id": f"{kind}-{num}",
        "course_id": course_match.group(1),
        "course_name": bracket.group(1).strip(),
        # The bracket is the course label, and it is a separate column now —
        # leaving it on the title would repeat the course on every row of a
        # card that is already grouped by course.
        "title": _COURSE_BRACKET.sub("", summary).strip(),
        "due_at": due_at,
        "has_due_time": 1 if has_time else 0,
        "html_url": url,
        "kind": kind,
    }


def _store_ical(conn: sqlite3.Connection, items: list[dict], now_iso: str) -> None:
    """Upsert the iCal pass, WITHOUT clobbering anything REST enriched.

    due_at is left alone when has_due_time is already 1: the feed only knows
    the day, and overwriting a real REST timestamp with midnight would silently
    undo the enrichment on every poll. Same reasoning for source.
    """
    seen_courses: dict[str, str] = {}
    for it in items:
        seen_courses[it["course_id"]] = it["course_name"]

    for cid, name in seen_courses.items():
        conn.execute(
            """INSERT INTO canvas_courses (id, name, course_code, source, fetched_at)
               VALUES (?, ?, NULL, 'ical', ?)
               ON CONFLICT(id) DO UPDATE SET
                 name       = excluded.name,
                 fetched_at = excluded.fetched_at""",
            (cid, name, now_iso),
        )

    for it in items:
        conn.execute(
            """INSERT INTO canvas_assignments
                 (id, course_id, title, due_at, has_due_time, points,
                  submitted, html_url, kind, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'ical', ?)
               ON CONFLICT(id) DO UPDATE SET
                 course_id    = excluded.course_id,
                 title        = excluded.title,
                 due_at       = CASE WHEN canvas_assignments.has_due_time = 1
                                     THEN canvas_assignments.due_at
                                     ELSE excluded.due_at END,
                 html_url     = excluded.html_url,
                 kind         = excluded.kind,
                 fetched_at   = excluded.fetched_at""",
            (it["id"], it["course_id"], it["title"], it["due_at"],
             it["has_due_time"], it["html_url"], it["kind"], now_iso),
        )

    # Prune what the feed no longer carries — a deleted assignment must leave
    # the card. Scoped to iCal-sourced rows so a REST-only item (one with no
    # due date, which the feed omits entirely) is not collected as collateral.
    ids = [it["id"] for it in items]
    placeholders = ",".join("?" * len(ids)) if ids else "''"
    conn.execute(
        f"DELETE FROM canvas_assignments "
        f"WHERE source = 'ical' AND id NOT IN ({placeholders})",
        ids,
    )
    conn.commit()


def _to_local_iso(raw: str | None) -> str | None:
    """A Canvas UTC timestamp as a LOCAL ISO string, or None.

    Everything downstream — the day read off the front, the relative
    "tomorrow", the time rendered on the card — reads this one string, so it
    has to already be in the timezone the user lives in. Parsing goes through
    calendars/eventtime.py because a second implementation of this is how the
    same code becomes correct on one machine and five hours wrong on another.
    """
    if not raw:
        return None
    from calendars.eventtime import to_local
    dt = to_local(raw)
    if dt is None:
        logger.debug(f"canvas: unparseable due_at {raw!r}; storing nothing.")
        return None
    return dt.isoformat(timespec="seconds")


def _rest_get(url: str, token: str, params: dict) -> list:
    """A paginated Canvas REST collection. Raises on HTTP error — the caller
    turns that into a status flag, because a dead token must not be an
    exception that reaches the poll job."""
    out: list = []
    headers = {"Authorization": f"Bearer {token}"}
    next_url, next_params = url, dict(params)
    for _ in range(_REST_MAX_PAGES):
        r = requests.get(next_url, headers=headers, params=next_params,
                         timeout=_REST_TIMEOUT)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            raise ValueError(f"expected a list from {next_url}, got {type(page).__name__}")
        out.extend(page)
        # Canvas paginates through RFC5988 Link headers, not a cursor field.
        link = r.headers.get("Link", "")
        nxt = None
        for part in link.split(","):
            if 'rel="next"' in part:
                nxt = part.split(";")[0].strip().strip("<>")
                break
        if not nxt:
            break
        next_url, next_params = nxt, {}   # the next URL is already complete
    return out


def _store_rest(conn: sqlite3.Connection, courses: list[dict],
                per_course: dict[str, list[dict]], now_iso: str) -> None:
    """Overlay the REST pass on top of the iCal rows."""
    for c in courses:
        conn.execute(
            """INSERT INTO canvas_courses (id, name, course_code, source, fetched_at)
               VALUES (?, ?, ?, 'rest', ?)
               ON CONFLICT(id) DO UPDATE SET
                 name        = excluded.name,
                 course_code = excluded.course_code,
                 source      = 'rest',
                 fetched_at  = excluded.fetched_at""",
            (str(c.get("id")), c.get("name") or "", c.get("course_code") or "",
             now_iso),
        )

    for course_id, assignments_ in per_course.items():
        for a in assignments_:
            # CANVAS RETURNS UTC WITH A Z AND IT IS STORED AS LOCAL.
            #
            # A due date of "2026-08-20T04:59:59Z" is 11:59:59 PM on the 19th
            # in Central — an 11:59pm deadline, which is what essentially
            # every Canvas due date is. Stored verbatim, the DAY read off the
            # front of that string is tomorrow while the TIME rendered from it
            # is tonight: one label, two different days, wrong in the
            # direction that makes you miss the deadline.
            #
            # to_local is the one parser (rule 30, calendars/eventtime.py) and
            # it exists for exactly this class of bug on the calendar side.
            due = _to_local_iso(a.get("due_at"))
            submission = a.get("submission") or {}
            submitted = submission.get("submitted_at")
            conn.execute(
                """INSERT INTO canvas_assignments
                     (id, course_id, title, due_at, has_due_time, points,
                      submitted, html_url, kind, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'assignment', 'rest', ?)
                   ON CONFLICT(id) DO UPDATE SET
                     course_id    = excluded.course_id,
                     title        = excluded.title,
                     due_at       = excluded.due_at,
                     has_due_time = excluded.has_due_time,
                     points       = excluded.points,
                     submitted    = excluded.submitted,
                     html_url     = excluded.html_url,
                     source       = 'rest',
                     fetched_at   = excluded.fetched_at""",
                (f"assignment-{a.get('id')}", str(course_id),
                 a.get("name") or "", due, 1 if due else 0,
                 a.get("points_possible"),
                 1 if submitted else (0 if submission else None),
                 a.get("html_url") or "", now_iso),
            )

        # Prune this course's REST rows that the response no longer carries —
        # a deleted assignment has to leave the card, the same rule the iCal
        # pass follows.
        #
        # SCOPED PER COURSE, AND ONLY TO COURSES THAT ANSWERED. per_course
        # holds only successful fetches (a failed one is logged and skipped
        # above), so a course Canvas refused this cycle keeps every row it
        # had. Pruning on a failed read is how a timeout empties the card.
        seen = [f"assignment-{a.get('id')}" for a in assignments_]
        marks = ",".join("?" * len(seen)) if seen else "''"
        conn.execute(
            f"DELETE FROM canvas_assignments "
            f"WHERE source = 'rest' AND course_id = ? AND id NOT IN ({marks})",
            [str(course_id), *seen],
        )
    conn.commit()


def refresh(config: dict, conn: sqlite3.Connection) -> dict:
    """Repopulate the Canvas cache. POLL CYCLE ONLY — never the request path.

    Never raises. Returns a status dict; a failure leaves the previous rows in
    place and is reported through canvas_last_error, which is what lets the
    card render stale-with-a-timestamp instead of empty.
    """
    cfg = config.get("canvas") or {}
    now_iso = datetime.now().isoformat(timespec="seconds")
    result = {"ical_ok": False, "rest_ok": False, "courses": 0,
              "assignments": 0, "error": "", "refreshed_at": now_iso,
              "rest_auth_expired": False}

    # ── Layer 1: iCal. Works with a dead token. ───────────────────────────────
    ical_url = (cfg.get("ical_url") or "").strip()
    if ical_url:
        try:
            r = requests.get(ical_url, timeout=_REST_TIMEOUT)
            r.raise_for_status()
            cal = Calendar.from_ical(r.content)
            items = [x for x in (_ical_item(c) for c in cal.walk()
                                 if c.name == "VEVENT") if x]
            _store_ical(conn, items, now_iso)
            result["ical_ok"] = True
            result["assignments"] = len(items)
            result["courses"] = len({i["course_id"] for i in items})
        except Exception as e:
            result["error"] = f"ical: {e}"
            logger.warning(f"Canvas cache — iCal refresh failed: {e}")

    # ── Layer 2: REST. Enrichment only; a 401 costs nothing above. ────────────
    token = (cfg.get("api_token") or "").strip()
    host = base_url(cfg)
    if token and host:
        try:
            courses_json = _rest_get(
                f"{host}/api/v1/courses", token,
                {"per_page": 100, "enrollment_state": "active"})
            per_course: dict[str, list[dict]] = {}
            for c in courses_json:
                cid = c.get("id")
                if cid is None:
                    continue
                try:
                    per_course[str(cid)] = _rest_get(
                        f"{host}/api/v1/courses/{cid}/assignments", token,
                        {"per_page": 100, "include[]": "submission"})
                except Exception as e:
                    # One unreadable course must not lose the other four.
                    logger.debug(f"Canvas REST assignments for {cid} failed: {e}")
            _store_rest(conn, courses_json, per_course, now_iso)
            result["rest_ok"] = True
        except requests.exceptions.HTTPError as e:
            # A 401 on the FIRST call (/courses) means the token itself is
            # dead, not that one course refused us — distinct from the
            # per-course try/except above, which tolerates a single bad
            # course without flagging the whole token.
            status = e.response.status_code if e.response is not None else None
            if status == 401:
                result["rest_auth_expired"] = True
            result["error"] = (result["error"] + "; " if result["error"] else "") + f"rest: {e}"
            logger.info(f"Canvas cache — REST enrichment unavailable ({e}). "
                        f"iCal data retained.")
        except Exception as e:
            result["error"] = (result["error"] + "; " if result["error"] else "") + f"rest: {e}"
            logger.info(f"Canvas cache — REST enrichment unavailable ({e}). "
                        f"iCal data retained.")

    # Status is state, not instrumentation: the card reads it to decide whether
    # to show a staleness warning, so it is written even on total failure.
    try:
        if result["ical_ok"] or result["rest_ok"]:
            state.set(conn, "canvas_refresh_at", now_iso)
        state.set(conn, "canvas_rest_ok", "true" if result["rest_ok"] else "false")
        state.set(conn, "canvas_last_error", result["error"])
        state.set(conn, "canvas_rest_auth_expired",
                  "true" if result["rest_auth_expired"] else "false")
    except Exception as e:
        logger.debug(f"Canvas cache status write failed: {e}")
    return result


def courses(conn: sqlite3.Connection) -> list[dict]:
    """The cached course list, for the picker. Pure read."""
    try:
        rows = conn.execute(
            "SELECT id, name, course_code, source, fetched_at "
            "FROM canvas_courses ORDER BY name"
        ).fetchall()
    except Exception as e:
        logger.debug(f"canvas courses read failed: {e}")
        return []
    return [{"id": r[0], "name": r[1] or "", "course_code": r[2] or "",
             "source": r[3] or "", "fetched_at": r[4] or ""} for r in rows]


def assignments(conn: sqlite3.Connection, course_ids: list[str] | None = None,
                due_after: str | None = None, due_before: str | None = None,
                limit: int = 200, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Cached assignments, optionally scoped to courses, a due window, and kind.

    Pure read — safe on the request path, which is the entire reason the cache
    tables exist. Undated items sort last rather than being dropped: "no due
    date" is a real state a card should be able to show.

    `kinds` defaults to None (both 'assignment' and 'event'), because this is
    a general cache reader and a future consumer may genuinely want calendar
    events. A caller building a work/to-do list — the only two today — passes
    `kinds=("assignment",)`: an iCal course EVENT like "OPEN LAB" is a
    schedule entry, not something to turn in, and the UID pattern that sets
    `kind` (see `_ical_item`) already separates the two at ingest, so this is
    a filter, not a classification.
    """
    sql = ("SELECT id, course_id, title, due_at, has_due_time, points, "
           "submitted, html_url, kind, source, fetched_at "
           "FROM canvas_assignments WHERE 1=1")
    args: list = []
    if course_ids:
        sql += f" AND course_id IN ({','.join('?' * len(course_ids))})"
        args.extend(course_ids)
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args.extend(kinds)
    if due_after:
        sql += " AND due_at >= ?"
        args.append(due_after)
    if due_before:
        sql += " AND due_at <= ?"
        args.append(due_before)
    sql += " ORDER BY due_at IS NULL, due_at LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    try:
        rows = conn.execute(sql, args).fetchall()
    except Exception as e:
        logger.debug(f"canvas assignments read failed: {e}")
        return []
    return [{"id": r[0], "course_id": r[1], "title": r[2] or "",
             "due_at": r[3], "has_due_time": bool(r[4]), "points": r[5],
             "submitted": None if r[6] is None else bool(r[6]),
             "html_url": r[7] or "", "kind": r[8] or "", "source": r[9] or "",
             "fetched_at": r[10] or ""} for r in rows]


def cache_status(conn: sqlite3.Connection) -> dict:
    """What the card needs to decide whether to trust what it is showing."""
    try:
        return {
            "refreshed_at": state.get(conn, "canvas_refresh_at") or "",
            "rest_ok": (state.get(conn, "canvas_rest_ok") or "") == "true",
            "last_error": state.get(conn, "canvas_last_error") or "",
            "rest_auth_expired":
                (state.get(conn, "canvas_rest_auth_expired") or "") == "true",
        }
    except Exception as e:
        logger.debug(f"canvas cache status read failed: {e}")
        return {"refreshed_at": "", "rest_ok": False, "last_error": "",
                "rest_auth_expired": False}
