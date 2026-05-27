# Project Friday — Claude Code Instructions

## What is Friday?

Friday is a personal AI secretary running on an always-on Mac. It ingests information from multiple sources (Canvas, Gmail, GroupMe), manages Apple Calendar, delivers proactive briefings and urgent alerts via Telegram, and drafts replies for user review. The user interacts with Friday exclusively through Telegram.

Friday is **not** a simple chatbot. It is a structured, event-driven agent with a tool layer, memory layer, approval-gated actions, and a proactive alert system.

---

## Core Architecture Principles

- **Telegram is the sole UI.** All interaction — briefings, alerts, approvals, drafts, conversational queries — happens through Telegram.
- **PTB JobQueue is the only scheduler.** `python-telegram-bot` is fully async. Never introduce a second scheduling library (`apscheduler`, `schedule`, `while True: sleep()`). All scheduled jobs (briefings, reminders, connector polling) register directly on `application.job_queue`. This guarantees they share the same async event loop and Telegram connection without conflicts.
- **The semaphore lives at the entry point.** `asyncio.Semaphore(1)` is placed at the very top of the Telegram message handler — before SQLite queries, before context assembly, before anything. Messages wait in line from the first byte. It is never placed only around the LLM call.
- **Urgency is tagged at ingestion.** Every piece of incoming data is evaluated for urgency immediately when ingested inside the connector. Urgent items bypass the briefing queue and trigger an immediate Telegram prompt. This is not a separate phase — it is part of every connector's ingestion routine.
- **Approval gates on all writes.** Every action that modifies the outside world (adding a calendar event, creating a Gmail draft) must go through `send_permission_request` and wait for explicit user confirmation before executing.
- **No iMessage in the automated pipeline.** iMessage is not polled, not read programmatically, and not drafted to. It is out of scope for all phases.
- **SQLite is the only data store.** No `state.json`. No vector store. No RAG. No Redis. All state — runtime key-value pairs, conversation history, stored events, urgency tags, pending approvals, last_seen timestamps — lives in `friday_memory.db`. The `system_state` table replaces `state.json` entirely.

---

## File Structure

```
friday/
├── friday.py                  # Entry point — builds PTB Application, registers handlers and jobs
├── friday_config.yaml         # Config (tokens, models, paths)
├── AGENTS.md                  # Persona/system prompt for the LLM
├── dashboard.py               # Tkinter status dashboard — reads from SQLite, not state.json
├── requirements.txt
│
├── agent/
│   └── core.py                # FridayAgent — LLM calls only. Semaphore is NOT here.
│
├── channels/
│   └── telegram.py            # TelegramChannel — semaphore entry point, message routing,
│                              # permission gates, JobQueue job registration
│
├── connectors/                # Read-only data ingestion
│   ├── canvas.py              # Canvas iCal feed reader
│   ├── gmail.py               # Gmail reader (OAuth)
│   ├── groupme.py             # GroupMe reader
│   └── weather.py             # Weather API (stateless, on-demand)
│
├── actions/                   # Approval-gated write operations
│   ├── calendar.py            # Apple Calendar write (caldav or AppleScript)
│   └── gmail_draft.py         # Gmail draft creation
│
├── memory/
│   ├── db.py                  # SQLite connection, schema, migrations
│   ├── state.py               # system_state table helpers (replaces state.json)
│   └── friday_memory.db       # SQLite database (gitignored)
│
├── voice/
│   └── listen.py              # Standalone voice script — never imported by friday.py
│
└── logs/
    └── friday.log
```

---

## SQLite Schema

```sql
-- Runtime key-value state (replaces state.json entirely)
CREATE TABLE system_state (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

-- Ingested items from all connectors
CREATE TABLE events (
    id          TEXT PRIMARY KEY,
    source      TEXT,       -- 'canvas', 'gmail', 'groupme'
    title       TEXT,
    body        TEXT,
    due_at      TEXT,
    urgency     TEXT,       -- 'URGENT', 'SOON', 'NORMAL'
    notified    INTEGER DEFAULT 0,
    created_at  TEXT
);

-- Per-source ingestion cursors
CREATE TABLE last_seen (
    source      TEXT PRIMARY KEY,
    cursor      TEXT,       -- timestamp or ID depending on source
    updated_at  TEXT
);

-- Pending approval actions
CREATE TABLE pending_actions (
    id          TEXT PRIMARY KEY,
    action_type TEXT,       -- 'calendar_add', 'gmail_draft'
    payload     TEXT,       -- JSON
    status      TEXT,       -- 'pending', 'confirmed', 'cancelled'
    created_at  TEXT
);

-- Conversation history (rolling window)
CREATE TABLE conversation_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT,       -- 'user', 'assistant'
    content     TEXT,
    created_at  TEXT
);
```

---

## Async Architecture

```
friday.py
└── builds PTB Application
    ├── MessageHandler → telegram.py::on_message()
    │     └── asyncio.Semaphore(1)  ← gate is HERE, before everything
    │           ├── query SQLite for context
    │           ├── agent/core.py::_think()
    │           └── telegram.send()
    │
    └── job_queue
          ├── run_daily → send_evening_briefing()
          ├── run_repeating (15min) → poll_connectors()
          └── run_repeating (1min) → check_urgent_alerts()
```

All scheduled work runs through PTB's `JobQueue`. No threads. No secondary loops. No `schedule` library.

---

## Phase Implementation Plan

### Phase 1 — Foundation

**Goal:** Friday runs reliably, survives restarts, and processes messages safely.

- [ ] **SQLite schema** (`memory/db.py`) — create all tables above, write migration helper
- [ ] **`system_state` helpers** (`memory/state.py`) — `get(key)`, `set(key, value)` backed by SQLite. This is the complete replacement for `state.json`.
- [ ] **Semaphore at entry point** (`channels/telegram.py`) — `asyncio.Semaphore(1)` is the first thing acquired inside `on_message()`. Messages queue here before any processing begins.
- [ ] **PTB JobQueue setup** (`friday.py`) — register all scheduled jobs on `application.job_queue`. Remove all uses of `schedule` library and background threads.
- [ ] **Catch-up logic** (`friday.py`) — on startup use PTB's `drop_pending_updates=False`. The semaphore handles the backlog sequentially. No custom Telegram update tracking needed.
- [ ] **LaunchAgent plist** — must point to the absolute path of the venv Python binary (e.g. `/Users/username/friday/.venv/bin/python`). Never `/usr/bin/python3`. Include `KeepAlive = true` and `RunAtLoad = true`.
- [ ] **Dashboard** (`dashboard.py`) — update to read all state from SQLite `system_state` table. Remove all references to `state.json`.
- [ ] **Persona** (`AGENTS.md`) — define Friday's voice, urgency policy, briefing format, and decision-making rules.

---

### Phase 2 — Read-Only Integrations

**Goal:** Friday knows what is happening in the user's world.

- [ ] **Canvas** (`connectors/canvas.py`) — fetch the Canvas iCal feed URL using the user's API token. Parse with `icalendar` library. Store assignments in `events` table. Tag urgency at ingestion: due within 24h = `URGENT`, due within 72h = `SOON`, otherwise `NORMAL`.
- [ ] **Gmail** (`connectors/gmail.py`) — OAuth2 via Google API. Fetch unread/recent threads since `last_seen` cursor. Store in `events` table. Tag urgency at ingestion based on sender, subject keywords, and date relevance.
- [ ] **Weather** (`connectors/weather.py`) — stateless, no storage. Called on demand or injected into the evening briefing payload.
- [ ] **iMessage** — **not implemented. Do not add under any circumstances.**

All connectors must:
- Read and update their cursor in the `last_seen` table
- Tag urgency before writing to `events`
- Never call the LLM directly — feed data into SQLite only
- Be called from `poll_connectors()` registered on PTB's `job_queue`

---

### Phase 3 — Briefings & Proactive Alerts

**Goal:** Friday speaks first when it matters.

- [ ] **On-demand briefing** — user sends "brief me" → agent queries `events` table for pending/unnotified items → assembles context → single LLM call → Telegram response → marks items as notified
- [ ] **Evening briefing** — registered on `job_queue.run_daily()` at `briefing_time` from config. Queries pending events, due dates, unresolved alerts, and assembles a summary.
- [ ] **Proactive reminders** — `job_queue.run_repeating()` checks for events due within 24h where `notified = 0`. Sends unprompted Telegram message. Example: "Sir, your History paper is due tomorrow at 11:59 PM."
- [ ] **Urgent interrupt** — fired inside the connector at ingestion time when urgency = `URGENT`. Does not wait for a job cycle. Sends immediate Telegram message and sets `notified = 1`.

---

### Phase 4 — Calendar Writing (Approval Gated)

**Goal:** Friday can modify Apple Calendar, but only with explicit user approval.

- [ ] **Apple Calendar write** (`actions/calendar.py`) — use `caldav` library or AppleScript via subprocess. Every write goes through `send_permission_request` first. Only executes after `confirm` callback. Stores pending action in `pending_actions` table with status `pending` until resolved.
- [ ] **Manual Nation entry** — user sends work schedule via Telegram text. Friday parses with LLM, proposes a calendar event, sends for approval, then writes. Nation has no API and sends no confirmation emails. Entry is always manual.
- [ ] **GroupMe reading** (`connectors/groupme.py`) — poll GroupMe API for new messages since `last_seen` cursor. Store in `events` table. Tag urgency. Feed into briefing system.

---

### Phase 5 — Drafting (Approval Gated, No Sending)

**Goal:** Friday composes replies, user decides what to do with them.

- [ ] **Gmail drafts** (`actions/gmail_draft.py`) — Friday composes a reply via LLM, sends draft text to user in Telegram for approval. On confirm, creates draft in Gmail's native Drafts folder via API. Does not send. Ever.
- [ ] **GroupMe reply suggestions** — Friday outputs proposed reply as plain text inside Telegram. No internal drafts queue. No write API calls to GroupMe. User copies and pastes manually.
- [ ] **No iMessage drafting** — not implemented.

---

### Phase 6 — Voice Interface

**Goal:** User can speak to Friday as an alternative to typing in Telegram.

- [ ] **Separate script** (`voice/listen.py`) — fully standalone. Never imported by `friday.py`. Has no knowledge of Friday's internals.
- [ ] **Wake word** — detect "Hey Friday" using a lightweight local model (e.g. `openwakeword`)
- [ ] **Transcription** — capture audio, transcribe locally using `whisper` (small or base model)
- [ ] **Telegram bridge** — send transcription as a Telegram message to the bot. Friday receives and processes it identically to a typed message.
- [ ] Friday's core requires zero changes for voice to work.

---

### Phase 7 — Hardening & Polish

**Goal:** Improve reliability, observability, and experience across all existing features.

- [ ] Improve urgency detection accuracy based on real usage patterns
- [ ] Refine briefing format and scheduling based on user preference
- [ ] Add robust error handling and recovery across all connectors
- [ ] Dashboard improvements
- [ ] Logging and observability improvements

---

## Key Constraints & Rules for Claude Code

1. **Never remove or move the semaphore.** It lives at the top of `on_message()` in `telegram.py`. No exceptions.
2. **Never use a second scheduling library.** No `schedule`, no raw `apscheduler`, no background threads for timing. PTB `JobQueue` only.
3. **Never poll iMessage.** Not via AppleScript, not via `chat.db`, not via any method.
4. **Never write externally without an approval gate.** Every write action calls `send_permission_request` and waits for `confirm`.
5. **Never use `/usr/bin/python3` in the LaunchAgent plist.** Always the venv binary absolute path.
6. **Canvas uses the iCal feed.** Never HTML scraping. Use `icalendar` library.
7. **Urgency is tagged inside the connector at ingestion time.** Never later in the agent.
8. **GroupMe drafts are plain text in Telegram only.** No write API calls. No internal drafts queue.
9. **SQLite is the only data store.** No `state.json`. No vector store. No Redis.
10. **Voice is a standalone satellite script.** It never imports from Friday's core.
11. **Nation has no API and sends no emails.** Work schedule entry is always manual via Telegram.
12. **All secrets** live in `friday_config.yaml` or environment variables. Never hardcoded.

---

## Config Structure (`friday_config.yaml`)

```yaml
agent:
  name: Friday
  briefing_time: '21:45'
  timezone: America/Chicago

telegram:
  bot_token: ''
  chat_id: ''

memory:
  db_path: memory/friday_memory.db
  short_term_turns: 20

provider: ollama  # ollama | gemini

ollama:
  model: llama3.2:1b
  base_url: http://localhost:11434
  max_tokens: 1000

gemini:
  model: models/gemini-2.5-flash-lite
  max_tokens: 1000
  api_key: ''

canvas:
  ical_url: ''
  api_token: ''

gmail:
  credentials_path: 'gmail_credentials.json'
  token_path: 'gmail_token.json'

groupme:
  api_token: ''
  group_id: ''

weather:
  api_key: ''
  location: ''
```

---

## Persona Notes (`AGENTS.md`)

Friday's persona should convey:
- Concise, direct, professional tone — a real secretary, not a chatbot
- Addresses the user formally unless told otherwise
- Proactive about urgency, conservative about interruptions
- Always states the source of information ("Your Canvas feed shows...", "From your Gmail...")
- Never hallucinates events or deadlines — if uncertain, says so
