"""
calendars/writes.py
What a calendar write actually reports back.

THE FAILURE THIS FILE EXISTS TO PREVENT: `write_event` used to return
`str | None`, which collapsed four distinguishable conditions into one value.
A calendar that does not exist and a fifteen-second osascript timeout both
came back as `None`, and nothing downstream could tell them apart.

They are not the same thing, and treating them as the same is how a calendar
gets double-booked. One of them definitely did not happen. The other may have
landed on the service a moment after we stopped waiting for it — the push had
already been issued. A retry of the first is free; a retry of the second
creates a second event.

So a write reports one of three statuses:

    written   the service confirmed it back, and we hold its identifier
    refused   it definitely did not happen — a structured error from the
              service, a calendar that does not exist. Safe to retry, and
              there is nothing to record.
    unknown   we do not know. A timeout, a non-zero exit, a response we could
              not parse. It may have succeeded server-side. NOT safe to retry
              without checking first, and this is the case that has to leave a
              record behind carrying its fingerprint (tools/types.py) so that
              a later retry has something to check against — the service that
              just timed out is exactly the thing a retry cannot rely on
              reaching.

A SMALL RESULT TYPE, NOT A SENTINEL. A sentinel value gets compared with
`is None` at some call site downstream and the distinction evaporates silently
at the one place it mattered. A dataclass with a status field cannot be
accidentally truth-tested into the wrong branch — and `bool(outcome)` is not
defined here on purpose, so `if outcome:` is a type error to a reader rather
than a quiet always-true.

VERIFICATION IS SEPARATE FROM THE WRITE. `verified` says a read-back went and
found the event under the identifier the write handed us. Invariant 4 says a
write is confirmed to the user only after the service confirms it back, and a
UID the write path minted for itself is not the service confirming anything —
it is the write path agreeing with itself. The read-back is what turns that
into a fact. See calendars/backend.py::write_event, which is where it happens
so that both backends get it from one implementation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

WriteStatus = Literal["written", "refused", "unknown"]


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """The result of one attempted calendar write.

    `uid` is the service's identifier and is present only on `written`.
    `detail` carries the reason for a `refused` or `unknown`, for the log and
    for the tool_calls row — never for the user.
    `verified` means a read-back located an event under `uid`. False on a
    `written` outcome means the read-back did not confirm it: either it failed
    or it disagreed, and both are worth knowing before telling the user the
    event is on their calendar.
    """
    status: WriteStatus
    uid: str = ""
    detail: str = ""
    verified: bool = False

    @property
    def ok(self) -> bool:
        """The write landed. Says nothing about whether it was verified."""
        return self.status == "written"

    @property
    def may_have_landed(self) -> bool:
        """True when a retry could double-book. The question idempotency asks."""
        return self.status in ("written", "unknown")


def written(uid: str, verified: bool = False) -> WriteOutcome:
    return WriteOutcome(status="written", uid=uid, verified=verified)


def refused(detail: str) -> WriteOutcome:
    return WriteOutcome(status="refused", detail=detail)


def unknown(detail: str) -> WriteOutcome:
    return WriteOutcome(status="unknown", detail=detail)


def write_fingerprint(title: str, day: str, start_time: str,
                      calendar: str) -> str:
    """The idempotency key for a calendar write: title, day, start time,
    calendar. Two writes with the same fingerprint inside the TTL are the same
    write, and the second one must not happen.

    Deliberately NOT the whole event. Notes and location are excluded because
    the same event described twice — once by the user, once by a retry after a
    timeout — routinely differs in them, and a fingerprint that changes when
    the description does is a fingerprint that never catches anything. What
    identifies an event to a person is what it is called, when it is, and
    where it lives.

    Normalised so that "Lunch With Sam" and "lunch with sam" collide, because
    to a human they are the same lunch. Times are compared as given: a caller
    holding "09:00" and one holding "9:00" are a real hazard, so the caller
    normalises before calling — see actions/calendar.py::_parse_event, which
    every path already goes through.
    """
    raw = "\x1f".join((
        (title or "").strip().casefold(),
        (day or "").strip(),
        (start_time or "").strip(),
        (calendar or "").strip().casefold(),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
