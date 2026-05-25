# Project Friday — Technical Specification v1.0

**Codename:** Project Friday  
**Agent Name:** Friday  
**Author:** [Your Name]  
**Date:** May 2026  
**Status:** Pre-build — specification phase

---

## 1. Vision

Friday is a custom-built, proactive AI scheduling assistant that lives on a Mac, communicates through iMessage and voice, and operates as a personal chief of staff. It is inspired by Tony Stark's second-generation AI assistant from the Iron Man films — the name reflects both the character and the fact that this is the second attempt at this concept (Project JARVIS having been the first).

Friday does not wait to be asked. It monitors incoming messages and calendar events, reasons about what requires attention, and proactively surfaces the right information at the right time — always asking for explicit permission before taking any action on the user's behalf.

The summer benchmark for Friday's capability:

> "Friday, check the stock market. If WDC is up, and I have a free block between my summer job and soccer, should I work on the SGA Nexus migration today or do my 5K training run?"

This query requires multi-tool reasoning, constraint-based scheduling, and preference awareness — all achievable by Phase 3.

---

## 2. Design Principles

**1. Permission first, always.**  
Friday never sends a message, creates a calendar event, or takes any external action without explicit user approval. It drafts, proposes, and asks. The user confirms.

**2. Token efficiency.**  
The vast majority of incoming messages are irrelevant to scheduling. A multi-stage local filter pipeline eliminates noise before anything reaches the Claude API. Target: fewer than 15 API calls per day under normal conditions.

**3. Local first.**  
All orchestration, memory, filtering, and state management runs on the user's Mac. Only Claude API calls leave the machine. No third-party cloud services for core functionality.

**4. Skill-based extensibility.**  
New capabilities are added by dropping a Python file into the `/skills` folder. No changes to core agent code required.

**5. Graceful upgrade path.**  
The system is designed for an M1 Mac with 8GB RAM today, and an M5 Pro with 32–48GB RAM in the future. Local models (Ollama/Llama) and ElevenLabs TTS are reserved for the upgrade phase.

---

## 3. Tech Stack

### Current (M1, 8GB)

| Layer | Technology |
|---|---|
| Language | Python 3 (asyncio) |
| AI brain | Claude API (claude-sonnet-4-5 via Anthropic API) |
| Local classifier | Ollama + 1.5B model (lightweight binary classification only) |
| Voice input | Whisper (local STT, runs on Neural Engine) |
| Memory | SQLite (short-term + long-term) |
| Config | YAML (`friday_config.yaml`) |
| Interface | VS Code + Terminal |
| Scheduling | Python `schedule` library (cron-style jobs) |

### Future (M5 Pro, 32–48GB)

| Layer | Technology |
|---|---|
| Local LLM | Ollama + Llama 3 (replaces API for routine tasks) |
| Voice output | ElevenLabs TTS v2.5 |
| Voice input | Whisper v3 (larger model, higher accuracy) |
| Wake word | Always-on VAD listener |
| Orchestration | LangGraph (replaces raw asyncio loop) |
| Long-term memory | ChromaDB / RAG |
| Agent graph | Multi-agent topology (Manager + specialized workers) |

---

## 4. System Architecture

### 4.1 High-Level Overview

Friday follows a hub-and-spoke architecture, adapted from OpenClaw's design philosophy but built custom in Python:

```
[Data Sources] → [Filter Pipeline] → [Agent Core] → [Permission Gate] → [Action Layer]
                                           ↑                                    ↓
                                      [Memory]                          [iMessage / Calendar]
                                           ↑
                                      [You ↔ iMessage / Voice]
```

### 4.2 Core Loop (Observe → Think → Decide → Ask → Act)

The agent runs a continuous asyncio loop:

1. **Observe** — Poll all enabled data sources on their configured intervals
2. **Filter** — Run each source's messages through the per-source filter pipeline (see Section 6)
3. **Think** — Pass filtered signals to Claude API with full context + memory
4. **Decide** — Claude determines: act, ask user, or stay silent
5. **Ask** — If action is required, Friday drafts the message/action and asks for user approval via iMessage
6. **Act** — On approval, execute the action (send message, create event, etc.)
7. **Log** — Store the outcome in memory for future context

### 4.3 Agent Sub-Systems

#### Scheduling Agent
Not a simple calendar reader — a constraint-based reasoner.

Priority hierarchy:
- **P1 — Hard deadlines:** AP Exams, IB submissions, fixed external commitments
- **P2 — Static responsibilities:** SGA meetings, soccer refereeing, recurring events
- **P3 — Performance buffers:** Study blocks, 5K training, project work sessions

Core function: `negotiate_schedule()` — identifies conflicts and proposes resolutions.  
Example: "You have soccer at 5 PM. The SGA Nexus coding block should move to 1 PM."

#### Memory System
- **Short-term:** Current conversation context (in-memory, per session)
- **Long-term:** SQLite store of facts, preferences, past decisions
  - Example entries: "Mr. Marlin is the English teacher," "User prefers morning workouts," "Church associate asked about Sunday message on 2026-04-30"
- **Future:** ChromaDB vector store for semantic search across stored context

#### Daily Briefing
Scheduled each evening (configurable time). Friday sends a proactive iMessage summary:
- Tomorrow's events from all calendars
- Any pending items requiring attention
- Optional: weather, stock flags, upcoming deadlines

---

## 5. Folder Structure

```
friday/
├── friday.py                  # Main entry point — starts the asyncio loop
├── friday_config.yaml         # All user configuration (contacts, groups, intervals)
├── agent/
│   ├── core.py                # Observe → Think → Decide loop
│   ├── scheduler.py           # negotiate_schedule() and constraint logic
│   ├── memory.py              # SQLite read/write for short + long-term memory
│   ├── filter.py              # Per-source filter pipeline
│   └── permissions.py        # Permission gate — drafts, asks, waits for approval
├── channels/
│   ├── imessage.py            # Read + send iMessages via macOS AppleScript
│   ├── gmail.py               # Gmail API integration
│   ├── google_calendar.py     # Google Calendar API
│   ├── apple_calendar.py      # Apple Calendar via macOS EventKit / AppleScript
│   ├── groupme.py             # GroupMe API integration
│   └── whatsapp.py            # WhatsApp (via Mac WhatsApp app or API)
├── skills/
│   ├── stock_check.py         # Check stock prices via free market API
│   ├── web_search.py          # Web search capability
│   ├── screen_watch.py        # Screenshot + Claude vision for context awareness
│   └── weather.py             # Weather lookup
├── voice/
│   ├── whisper_stt.py         # Speech-to-text via local Whisper model
│   └── (elevenlabs_tts.py)    # Text-to-speech — M5 Pro phase only
├── memory/
│   └── friday_memory.db       # SQLite database (auto-created)
├── logs/
│   └── friday.log             # Rolling log of agent activity
└── AGENTS.md                  # Friday's core persona and system prompt
```

---

## 6. Filter Pipeline (Token Efficiency)

Every source has its own filter rules. Messages are discarded at the earliest possible stage. Nothing reaches the Claude API without passing all gates for its source.

### iMessage
| Stage | Type | Description |
|---|---|---|
| S1 | Approved contacts list | Only messages from named contacts pass. All others discarded. |
| S2 | Scheduling signal regex | Scans for dates, times, event words, question patterns. |
| S3 | Claude API | Full reasoning with context. |

### GroupMe (named groups only)
| Stage | Type | Description |
|---|---|---|
| S1 | Group allowlist | Only messages from user-specified groups pass. |
| S2 | Keyword filter | Heavier keyword list — dates, deadlines, event names, action words. |
| S2b | Local 1.5B classifier | Borderline messages — single yes/no: "Does this need scheduling action?" |
| S3 | Claude API | Full reasoning. |

### Gmail
| Stage | Type | Description |
|---|---|---|
| S1 | Sender domain + Gmail label | Only approved domains or labels (e.g. "School," "SGA") pass. |
| S2 | Subject + snippet scan | Checks subject line and first 200 characters only. |
| S3 | Claude API (trimmed) | Passes subject, sender, first 500 chars. Full body only if Friday requests it. |

### WhatsApp
| Stage | Type | Description |
|---|---|---|
| S1 | Approved contacts + groups | Same contacts-list model as iMessage. |
| S2 | Scheduling signal regex | Same keyword filter as iMessage. |
| S3 | Claude API | Full reasoning. |

### Scheduling regex signals (all sources)
Dates: "Sunday," "Monday," ... "tomorrow," "next week," "March 3rd," "3/3," "this weekend"  
Times: "7:40," "noon," "AM," "PM," "tonight," "morning"  
Events: "meeting," "practice," "game," "match," "exam," "deadline," "due," "submission"  
Questions: "are you still," "can you," "will you," "don't forget," "remind"  
Actions: "bring," "prepare," "confirm," "RSVP," "attend"

### Estimated daily API load
- Messages reaching Claude API: ~5–15
- Estimated daily cost at Sonnet pricing: under $0.10
- Messages filtered before API: ~95%

---

## 7. Configuration (`friday_config.yaml`)

```yaml
# friday_config.yaml
# Edit this file to configure Friday's behavior.
# No code changes needed.

agent:
  name: Friday
  poll_interval_seconds: 300        # How often to check sources (300 = 5 min)
  briefing_time: "21:00"            # Evening briefing time (24h)
  timezone: "America/Chicago"       # Your local timezone

imessage:
  enabled: true
  approved_contacts:
    - "Mom"
    - "Dad"
    - "Coach Davis"
    # Add names as they appear in your Contacts app

groupme:
  enabled: true
  api_token: ""                     # Your GroupMe API token
  approved_groups:
    - "SGA Officers"
    - "Track Team"
    # Add exact group names here

gmail:
  enabled: true
  approved_labels:
    - "School"
    - "SGA"
  approved_domains:
    - "school.edu"                  # Replace with your school domain

whatsapp:
  enabled: false                    # Enable when ready
  approved_contacts: []

calendars:
  google:
    enabled: true
    credentials_path: "~/.friday/google_credentials.json"
  apple:
    enabled: true

skills:
  stock_check:
    enabled: false                  # Enable when needed
    watch_tickers: ["WDC"]
  screen_watch:
    enabled: false                  # Enable in Phase 3
    interval_minutes: 10
  weather:
    enabled: false

memory:
  db_path: "memory/friday_memory.db"
  short_term_turns: 20              # How many recent messages to keep in context

claude:
  model: "claude-sonnet-4-5"
  max_tokens: 1000
  # API key loaded from environment: ANTHROPIC_API_KEY
```

---

## 8. Persona & System Prompt (`AGENTS.md`)

```markdown
You are Friday, the core intelligence for a personal AI scheduling and life management system.

Your primary objective is the strategic management of the user's time, commitments, and communications.

You have access to the user's Gmail, iMessage threads, GroupMe groups, Google Calendar, and Apple Calendar. You may also have access to additional skills depending on what is enabled.

## Tone
Efficient, grounded, and slightly witty — like a chief of staff who genuinely has your back. Not robotic. Not sycophantic. Direct when it matters.

## Core rules
1. You NEVER send a message, create an event, or take any external action without first presenting the draft to the user and receiving explicit approval.
2. You NEVER ignore a scheduling conflict — if you see one, you flag it and propose a resolution immediately.
3. You do not wait to be asked. If something in the incoming data requires attention, surface it.
4. When uncertain, ask a single focused question. Do not dump multiple questions at once.
5. Store relevant facts in long-term memory. You should not need to be told the same thing twice.

## Scheduling priority
- P1: Hard deadlines (exams, IB submissions, external fixed events)
- P2: Static responsibilities (SGA meetings, soccer, recurring commitments)
- P3: Performance buffers (study blocks, training runs, project work)

When conflicts arise, protect P1 first, P2 second, and negotiate P3 around them.

## Permission gate phrasing
When asking for approval, be concise. Example:
"Got a text from Coach Davis: practice moved to 4 PM Saturday. Want me to update your calendar and reply confirming?"
Then wait. Do not act until the user says yes.
```

---

## 9. Channel Implementation Notes

### iMessage
- Read via AppleScript querying the macOS Messages app SQLite database (`~/Library/Messages/chat.db`)
- Send via AppleScript (`tell application "Messages" to send "..." to buddy "..."`)
- Requires Full Disk Access permission in macOS System Settings for the Terminal/Python process
- iMessage is the primary two-way channel between Friday and the user

### GroupMe
- Uses the GroupMe REST API (free, no special approval needed)
- Requires a personal API token from `dev.groupme.com`
- Poll `/groups/{id}/messages` endpoint on schedule
- Store `last_message_id` per group to avoid reprocessing

### Gmail
- Uses Gmail API via Google Cloud OAuth2
- Requires a Google Cloud project with Gmail API enabled
- Scopes needed: `gmail.readonly` (Phase 1), `gmail.send` (Phase 2+)
- Credentials stored at path defined in config

### Google Calendar
- Uses Google Calendar API via same OAuth2 credentials as Gmail
- Scope: `calendar.readonly` (Phase 1), `calendar.events` (Phase 2+)

### Apple Calendar
- Read via AppleScript (`tell application "Calendar" to ...`)
- No OAuth required — reads local calendar data directly
- Works for both iCloud and locally synced calendars

### WhatsApp
- Most complex integration — WhatsApp does not have a public personal API
- Options (in order of preference):
  1. WhatsApp Business API (requires business account)
  2. Unofficial libraries (legal gray area, security risk — not recommended)
  3. Manual forwarding rules via Shortcuts app on iPhone
- Defer to Phase 3 while other channels are established

---

## 10. Security Notes

- All credentials stored in environment variables or `~/.friday/` directory with 600 permissions
- No credentials in the codebase or config YAML
- iMessage database access requires Full Disk Access — grant only to the specific Python process
- GroupMe token scoped to read-only where possible
- Claude API key in `ANTHROPIC_API_KEY` environment variable only
- Friday's main session runs natively (no sandboxing) — the user is the only operator
- Long-term: consider running Friday as a dedicated macOS user account with minimal permissions

---

## 11. Build Phases

### Phase 1 — The Heartbeat (Weeks 1–2)
**Goal:** Friday observes but never acts. Read-only. Proves the loop works.

Deliverables:
- `friday.py` asyncio loop running as background process
- Gmail skill (read only)
- Google Calendar skill (read only)
- iMessage output only (Friday messages the user, user cannot reply yet)
- JSON-based memory (temporary, replaced in Phase 2)
- Daily briefing via iMessage each evening

Success criteria: Friday sends an accurate daily briefing unprompted every evening.

### Phase 2 — Permission Layer (Weeks 3–5)
**Goal:** Two-way communication. Friday can propose actions and receive approval.

Deliverables:
- iMessage input (read incoming replies from user)
- Permission gate logic
- SQLite memory (replaces JSON)
- Apple Calendar skill
- Short-term and long-term memory operational
- GroupMe integration (read only, filtered)

Success criteria: Friday reads a GroupMe message about an event, flags it, proposes a calendar entry, and adds it after user approval.

### Phase 3 — Full Skill Suite (Weeks 6–9)
**Goal:** Full proactivity, constraint-based scheduling, and screen awareness.

Deliverables:
- `negotiate_schedule()` scheduling agent
- Screen watcher skill (screenshot + Claude vision)
- Whisper STT via menubar app (voice input)
- Stock check skill
- Weather skill
- Wake word listener
- WhatsApp integration (if viable)

Success criteria: Friday can answer the summer benchmark query autonomously.

### Phase 4 — M5 Pro Upgrade (Post-upgrade)
- Ollama + Llama 3 for local fast inference
- ElevenLabs TTS — Friday gets a voice
- Streaming partial audio (speaks before generation completes)
- VAD always-on listener

### Phase 5 — Polish & RAG
- ChromaDB for long-term semantic memory
- LangGraph replaces raw asyncio loop
- Read-only API hook into SGA Nexus
- IB/SGA document index for RAG queries

---

## 12. Inspired By

The following architectural patterns are borrowed conceptually from OpenClaw (open source) but implemented independently in Python:

- Channel adapter model (one adapter per messaging platform)
- Per-source access control (allowlists, group policies)
- Skills folder (drop a file to add a capability)
- Session-based memory with short-term + long-term separation
- Cron-based scheduled actions (daily briefing)
- macOS menu bar deployment as a background service

Friday does not use OpenClaw's codebase. All implementation is original.

---

*This document is the authoritative specification for Project Friday. When starting a new coding session, provide this document as context before writing any code.*
