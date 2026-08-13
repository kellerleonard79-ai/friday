"""
policy/suppression.py
One question: was this already said recently enough that saying it again is
noise?

    already_alerted(notified) -> bool
    recently_surfaced(last_surfaced_at, now, within_hours=...) -> bool

SUPPRESSION HIDES A REMINDER, NEVER AN ITEM. That distinction is the whole
module. An item Friday has already mentioned stays on the list, stays in the
briefing, stays answerable when the user asks — what is withheld is the
STANDALONE INTERRUPT, because the second unprompted message about the same
thing is the one that trains someone to ignore the first.

Compare policy/visibility.py, which answers a question that does not depend on
when you ask it. Kept separate on purpose: folded together they become one
predicate that is false for two unrelated reasons, and the first bug report is
"why did it stop telling me about X" with no way to tell which rule caught it.

Decides and performs no action, like the rest of policy/. No queries, no
writes, no clock read of its own — `now` is passed in, so a decision can be
tested without waiting for one.

TWO RULES, AND ONLY ONE OF THEM HAS A CONSUMER TODAY.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# How long a briefing mention suppresses the same item's standalone reminder.
#
# Sized against the briefing cadence, not picked round: briefings are morning
# and evening, so a window shorter than the gap between them would let an item
# mentioned at 07:00 interrupt again at 15:00 having already been said once
# that day, and a window longer than the gap would let the evening briefing
# suppress a reminder for something genuinely due overnight.
DEFAULT_BRIEFING_WINDOW_HOURS = 12


def already_alerted(notified) -> bool:
    """Whether this event has already been sent as an interrupt.

    TODAY'S RULE, AND IT IS A LATCH RATHER THAN A WINDOW. `events.notified` is
    set to 1 when an urgent alert goes out and is never cleared, so the
    suppression is permanent: an item interrupts the user exactly once, ever.

    That is the correct behavior for the thing it guards — the alert path
    fires every 60 seconds against the same table, and anything short of
    permanent is a loop — and it is NOT the windowed rule below. They are two
    different questions and this module deliberately does not reshape one into
    the other. Reshaping it would change live behavior to satisfy a symmetry
    nothing has asked for.

    Named here rather than left as a bare `notified=0` in three SQL strings so
    that the next surface asking "has the user heard about this" finds the
    question already answered. Callers keep their own WHERE clause; this is
    what they check an item they already hold against.
    """
    return bool(notified)


def recently_surfaced(last_surfaced_at: datetime | str | None,
                      now: datetime,
                      within_hours: int = DEFAULT_BRIEFING_WINDOW_HOURS) -> bool:
    """Whether this item was surfaced recently enough to suppress a reminder.

    THE WINDOWED RULE. If an item was carried in a briefing within the window,
    its standalone reminder is suppressed — and the item itself is untouched:
    it stays on the list, stays in the next briefing, and stays answerable.
    Only the unprompted second mention is withheld.

    NO CONSUMER YET, and saying so is more useful than pretending otherwise.
    The thing this exists for is the to-do reminder path in step 8. The
    proactive due-date reminders (notifications.reminder_thresholds, the
    largest gap between Friday's config surface and its behavior) are the
    other caller, and they will need exactly this or they will interrupt about
    a due date the morning briefing already listed.

    NEVER-SURFACED SUPPRESSES NOTHING. A None timestamp means the item has not
    been mentioned, which is the case a reminder exists for — so it answers
    False rather than failing closed. This is the opposite of a precondition,
    where absence of evidence must block; here, blocking on missing data would
    silence the first reminder for every item, which is the only one that
    matters.

    An unparseable timestamp answers False for the same reason, and loudly is
    not an option: this runs on a background job and there is nobody to tell.
    """
    if last_surfaced_at is None:
        return False
    if isinstance(last_surfaced_at, str):
        try:
            last_surfaced_at = datetime.fromisoformat(last_surfaced_at)
        except ValueError:
            return False
    if within_hours <= 0:
        # A zero or negative window means "do not suppress". Spelled out
        # because timedelta(hours=0) would otherwise make every item with a
        # timestamp at or before `now` suppressed, which is the inversion.
        return False

    # Naive and aware datetimes cannot be compared, and both spellings reach
    # this module: SQLite rows come back as whatever was written, and
    # clock.local_now() is aware. Rather than guess a timezone for the naive
    # one — which is how an off-by-five-hours suppression window is born — the
    # comparison is made in whichever form `now` is, and a mismatch answers
    # False. See calendars/eventtime.py for the same problem solved for
    # calendar reads, where guessing was not an option either.
    if (last_surfaced_at.tzinfo is None) != (now.tzinfo is None):
        return False

    return last_surfaced_at > now - timedelta(hours=within_hours)
