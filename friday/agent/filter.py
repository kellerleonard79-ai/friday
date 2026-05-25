"""
agent/filter.py
Friday's pre-filter pipeline — eliminates noise before anything hits the Claude API.
Each source has its own rules. Messages are discarded at the earliest possible stage.
"""

import re

# ── Scheduling signal patterns ─────────────────────────────────────────────────

DAYS = r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|tonight|this weekend|next week|next month)\b"
DATES = r"\b(\d{1,2}[\/\-]\d{1,2}([\/\-]\d{2,4})?|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b"
TIMES = r"\b(\d{1,2}(:\d{2})?\s?(am|pm|AM|PM)|noon|midnight|morning|afternoon|evening)\b"
EVENTS = r"\b(meeting|practice|game|match|rehearsal|exam|test|deadline|due|submission|appointment|event|session|class|study|tryout|tournament|race|run|workout)\b"
QUESTIONS = r"\b(are you still|can you|will you|don't forget|do you|did you|have you|reminder|remind|confirm|rsvp|attending|coming|bringing|bring)\b"
ACTIONS = r"\b(cancelled|canceled|postponed|moved|rescheduled|changed|new time|new date|location)\b"

SCHEDULING_PATTERNS = [DAYS, DATES, TIMES, EVENTS, QUESTIONS, ACTIONS]

# Heavier keyword list for GroupMe (noisier environment)
GROUPME_EXTRA = r"\b(everyone|all|heads up|fyi|note|important|announcement|update|change|meet|gather|join|show up|be there|don't miss)\b"
GROUPME_PATTERNS = SCHEDULING_PATTERNS + [GROUPME_EXTRA]


def _matches_any(text: str, patterns: list) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in patterns)


# ── Per-source filters ─────────────────────────────────────────────────────────

def filter_groupme(message: dict, config: dict) -> tuple[bool, str]:
    """
    Filter a GroupMe message.
    Returns (should_process: bool, reason: str)
    
    message dict expected keys:
      - group_name: str
      - sender_name: str  
      - text: str
    """
    approved_groups = [g.lower() for g in config.get("approved_groups", [])]
    group = message.get("group_name", "").lower()

    # Stage 1 — Group allowlist
    if group not in approved_groups:
        return False, f"Group '{group}' not in allowlist"

    text = message.get("text", "")

    # Ignore empty or very short messages
    if len(text.strip()) < 5:
        return False, "Message too short to be relevant"

    # Stage 2 — Keyword filter
    if _matches_any(text, GROUPME_PATTERNS):
        return True, "Scheduling signal detected in GroupMe message"

    return False, "No scheduling signal found"


def filter_imessage(message: dict, config: dict) -> tuple[bool, str]:
    """
    Filter an iMessage.
    Returns (should_process: bool, reason: str)
    
    message dict expected keys:
      - sender: str (contact name or phone number)
      - text: str
    """
    approved = [c.lower() for c in config.get("approved_contacts", [])]

    # If approved_contacts is empty in Phase 1, skip contact filter
    if approved:
        sender = message.get("sender", "").lower()
        if not any(a in sender for a in approved):
            return False, f"Sender '{sender}' not in approved contacts"

    text = message.get("text", "")
    if len(text.strip()) < 5:
        return False, "Message too short"

    # Stage 2 — Scheduling signal
    if _matches_any(text, SCHEDULING_PATTERNS):
        return True, "Scheduling signal detected in iMessage"

    return False, "No scheduling signal found"


def filter_gmail(message: dict, config: dict) -> tuple[bool, str]:
    """
    Filter a Gmail message using subject line and snippet only.
    Returns (should_process: bool, reason: str)
    
    message dict expected keys:
      - sender_email: str
      - subject: str
      - snippet: str (first ~200 chars)
      - labels: list[str]
    """
    approved_labels = [l.lower() for l in config.get("approved_labels", [])]
    approved_domains = [d.lower() for d in config.get("approved_domains", [])]

    labels = [l.lower() for l in message.get("labels", [])]
    sender = message.get("sender_email", "").lower()
    domain = sender.split("@")[-1] if "@" in sender else ""

    # Stage 1 — Label or domain gate
    label_match = any(al in labels for al in approved_labels)
    domain_match = domain in approved_domains

    if not label_match and not domain_match:
        return False, f"Sender '{sender}' not in approved labels or domains"

    # Stage 2 — Subject + snippet scan
    combined = f"{message.get('subject', '')} {message.get('snippet', '')}"
    if _matches_any(combined, SCHEDULING_PATTERNS):
        return True, "Scheduling signal in email subject/snippet"

    return False, "No scheduling signal in email subject/snippet"
