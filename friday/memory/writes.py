"""
memory/writes.py
recent_writes: the ten-minute memory that stops a retry double-booking.

THE PROBLEM, EXACTLY. Friday asks the calendar to create an event. The request
goes out. Fifteen seconds later osascript has not answered, so the write
reports `unknown` (calendars/writes.py) — it may have landed, it may not have.
Something retries. If the first write did land, the calendar now has two
identical events and the user cleans them up by hand.

The obvious fix — ask the calendar whether the event is there — is exactly the
fix that does not work, because the thing that just failed to answer is the
calendar. So the check has to be local, and it has to be recorded at the
moment of attempting rather than at the moment of succeeding.

KEYED ON CONTENT, NOT ON AN IDENTIFIER. An `unknown` write has no identifier;
not having one is its whole content. So the key is a fingerprint of what was
asked for — title, day, start time, calendar (calendars/writes.py::
write_fingerprint) — which is available before the write is attempted and
identical across a retry.

TEN MINUTES. Long enough to cover a timeout, a user tapping a card twice, and
a model calling the same tool on two hops. Short enough that a genuine second
"add lunch on Thursday at noon" an hour later is a real second event and not
suppressed. This window is a guess at human intent and is deliberately small:
the cost of being wrong in one direction is a duplicate the user deletes, and
in the other it is a missing event the user thinks Friday created.

RECORDED BEFORE THE OUTCOME IS KNOWN. reserve() is called BEFORE the write.
A row written only on success would be missing in exactly the case it exists
for — the write that never reported back.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger("friday.memory.writes")

TTL_MINUTES = 10


@dataclass(frozen=True, slots=True)
class PriorWrite:
    """A write with this fingerprint that is still inside the TTL."""
    fingerprint: str
    status: str          # written | unknown
    identifier: str = ""
    created_at: str = ""
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == "written"


def find(conn, fingerprint: str) -> PriorWrite | None:
    """A live prior attempt at this exact write, or None.

    Expired rows answer None rather than being deleted here: a read path that
    writes is a read path that needs a transaction, and the nightly cleanup
    already exists for this.
    """
    if conn is None or not fingerprint:
        return None
    try:
        row = conn.execute(
            "SELECT status, identifier, created_at, detail, expires_at "
            "FROM recent_writes WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
    except sqlite3.Error as e:
        # A broken idempotency table must not block a write. Logged loudly:
        # this is the one instrumentation failure with a real cost, since it
        # means the next retry has nothing to check against.
        logger.error(f"recent_writes lookup failed (write NOT deduped): {e}")
        return None
    if row is None:
        return None
    status, identifier, created_at, detail, expires_at = row
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now():
                return None
        except ValueError:
            pass
    return PriorWrite(fingerprint=fingerprint, status=status,
                      identifier=identifier or "", created_at=created_at or "",
                      detail=detail or "")


def reserve(conn, fingerprint: str, detail: str = "") -> None:
    """Claim a fingerprint BEFORE attempting the write.

    The row starts as `unknown`, which is the truth at that instant: the write
    has been decided on and its outcome is not yet known. settle() upgrades it.
    A crash between the two leaves an `unknown` row, which is also the truth —
    and is what a later retry needs to see.
    """
    if conn is None or not fingerprint:
        return
    now = datetime.now()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO recent_writes "
            "(fingerprint, created_at, expires_at, status, identifier, detail) "
            "VALUES (?, ?, ?, 'unknown', NULL, ?)",
            (fingerprint, now.isoformat(),
             (now + timedelta(minutes=TTL_MINUTES)).isoformat(), detail),
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"recent_writes reserve failed for {fingerprint}: {e}")


def settle(conn, fingerprint: str, *, status: str, identifier: str = "",
           detail: str = "") -> None:
    """Record what the write turned out to be.

    A `refused` write RELEASES the fingerprint. Refused means the service told
    us it did not happen, so there is nothing to protect against and leaving a
    row would block a legitimate second attempt — the user fixing the calendar
    name and asking again is the ordinary case.
    """
    if conn is None or not fingerprint:
        return
    try:
        if status == "refused":
            conn.execute("DELETE FROM recent_writes WHERE fingerprint = ?",
                         (fingerprint,))
        else:
            conn.execute(
                "UPDATE recent_writes SET status = ?, identifier = ?, detail = ? "
                "WHERE fingerprint = ?",
                (status, identifier or None, detail, fingerprint),
            )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"recent_writes settle failed for {fingerprint}: {e}")


def prune(conn) -> int:
    """Drop expired rows. Called by the nightly activity cleanup."""
    if conn is None:
        return 0
    try:
        cur = conn.execute("DELETE FROM recent_writes WHERE expires_at < ?",
                           (datetime.now().isoformat(),))
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.debug(f"recent_writes prune failed: {e}")
        return 0
