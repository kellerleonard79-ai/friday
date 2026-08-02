# Project Friday — Claude Code Instructions

## Codebase Navigation & Knowledge Graph
- **Search Strategy:** Before running broad `grep` or file searches across the repository, check `graphify-out/GRAPH_REPORT.md` to map out dependencies and locate relevant files.
- **Context Gathering:** Use the Graphify snapshot to identify component relationships first, then perform targeted reads on specific files.

## What is Friday?
 
Friday is a personal AI secretary running on an always-on Mac. It ingests information from multiple sources (Canvas, GroupMe, and eventually Gmail), manages Apple Calendar, delivers proactive briefings and urgent alerts via Telegram, and drafts replies for user review. The user interacts with Friday exclusively through Telegram.
 
Friday is **not** a simple chatbot. It is a structured, event-driven agent with a tool layer, memory layer, and a proactive alert system.
 
---
 
## Core Architecture Principles
 
- **Telegram is the sole UI.** All interaction — briefings, alerts, approvals, drafts, conversational queries — happens through Telegram.
- **PTB JobQueue is the only scheduler.** `python-telegram-bot` is fully async. Never introduce a second scheduling library (`apscheduler`, `schedule`, `while True: sleep()`). All scheduled jobs (briefings, reminders, connector polling) register directly on `application.job_queue`. This guarantees they share the same async event loop and Telegram connection without conflicts.
- **The semaphore lives at the entry point.** `asyncio.Semaphore(1)` is placed at the very top of the Telegram message handler — before SQLite queries, before context assembly, before anything. Messages wait in line from the first byte. It is never placed only around the LLM call.
- **The LLM is the single decision maker for all ingested data.** Deterministic code handles the mechanical parts — fetching, parsing API responses into raw records, writing to SQLite. The LLM processes everything after that: deciding urgency, filtering announcements, parsing natural language input, and formatting all output. It is never bypassed for "simple" structured data.
- **Apple Calendar is the event store.** Due dates, work shifts, appointments, and any other calendar-type data live in Apple Calendar — not SQLite. SQLite tracks operational state only.
- **SQLite is the operational backbone.** No `state.json`. No vector store. No RAG. No Redis. SQLite tracks: runtime key-value state, conversation history, raw ingested events buffer, and last_seen cursors. The `system_state` table replaces `state.json` entirely.
- **No iMessage in the automated pipeline.** iMessage is not polled, not read programmatically, and not drafted to. It is out of scope for all phases.
---
 
## File Structure

The Python package is at `friday/friday/` — one level below the repo root,
which holds only packaging, the `.app` bundle, and LaunchAgent plists. Run
`ls friday/friday` for the current layout.

Non-obvious placements: the calendar backend dispatcher and its two
implementations are in `calendars/`; the web dashboard is a package
(`dashboard/`), not a Tkinter script; `paths.py` and `compat.py` are the
cross-platform seams.
 
---
 
## SQLite Schema

Defined in `friday/memory/db.py` — read it there rather than trusting a copy.
SQLite holds operational state only: runtime key-value state, conversation
history, the raw ingested-events buffer, and per-source cursors. Calendar-type
data (due dates, shifts, appointments) never lives here — see the event-store
rule above.
 
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
          ├── run_daily        → send_morning_briefing()   (fixed time, from config)
          ├── run_daily        → send_evening_briefing()   (fixed time, from config)
          ├── run_repeating    → poll_connectors()         (every 15 min)
          └── run_repeating    → check_urgent_alerts()     (every 1 min)
```
 
All scheduled work runs through PTB's `JobQueue`. No threads. No secondary loops. No `schedule` library.
 
---
 
## LLM Processing Flow
 
```
Connector fetches raw data
        ↓
Write raw record to events table (unprocessed)
        ↓
LLM evaluates record:
  - Assigns urgency (URGENT / SOON / NORMAL)
  - Decides action (calendar write / alert / briefing entry / ignore)
  - For Canvas announcements: determines if actionable
  - For GroupMe: considers group priority tier
        ↓
If URGENT → immediate Telegram interrupt, set notified = 1
If calendar action → approval gate (except Canvas due dates = auto)
If briefing entry → sits in events table until next briefing
        ↓
Apple Calendar ← receives all confirmed calendar writes
```
 
Deterministic code only handles: HTTP requests, iCal parsing, raw SQL writes, API auth.
The LLM handles everything else.
 
---
 
## Phase Implementation Plan
 
### Phase 1 — Foundation
 
**Goal:** Friday runs reliably, survives restarts, and processes messages safely.
 
- [ ] **SQLite schema** (`memory/db.py`) — create all tables above, write migration helper
- [ ] **`system_state` helpers** (`memory/state.py`) — `get(key)`, `set(key, value)` backed by SQLite. Complete replacement for `state.json`.
- [ ] **Semaphore at entry point** (`channels/telegram.py`) — `asyncio.Semaphore(1)` is the first thing acquired inside `on_message()`. Messages queue here before any processing begins.
- [ ] **PTB JobQueue setup** (`friday.py`) — register all scheduled jobs on `application.job_queue`. Remove all uses of `schedule` library and background threads.
- [ ] **Catch-up logic** (`friday.py`) — on startup use PTB's `drop_pending_updates=False`. The semaphore handles the backlog sequentially. No custom Telegram update tracking needed.
- [ ] **LaunchAgent plist** — must point to the absolute path of the venv Python binary (e.g. `/Users/username/friday/.venv/bin/python`). Never `/usr/bin/python3`. Include `KeepAlive = true` and `RunAtLoad = true`.
- [ ] **Menu bar app** (`menubar.py`) — rumps-based. Shows Friday status in menu bar. Buttons: Brief Me Now, Pause Friday, Open Dashboard, Quit. Provider submenu: Gemini / Ollama toggle (writes to config, restarts Friday process).
- [ ] **Dashboard** (`dashboard.py`) — Tkinter. Full config editing with form fields. Launched from menu bar. Reads/writes `friday_config.yaml`. Replaces standalone dashboard. Includes GroupMe group priority management.
- [ ] **Persona** (`AGENTS.md`) — define Friday's voice, urgency policy, briefing format, announcement filtering policy, and decision-making rules.
---
 
### Phase 2 — Read-Only Integrations
 
**Goal:** Friday knows what is happening in the user's world.
 
- [ ] **Canvas** (`connectors/canvas.py`) — fetch Canvas iCal feed using API token. Parse with `icalendar` library. Write raw records to `events` table. LLM evaluates each record: tags urgency, decides if due date goes to calendar or announcement goes to briefing.
- [ ] **Weather** (`connectors/weather.py`) — stateless, no storage. Called on demand or injected into briefing payload.
- [ ] **Gmail** — deprioritized. School Gmail is inaccessible via API (locked down, no 3rd party sign-in, forwarding disabled). Do not implement until a clean access path exists.
- [ ] **iMessage** — **not implemented. Do not add under any circumstances.**
All connectors must:
- Read and update their cursor in the `last_seen` table
- Write raw records to `events` table before any LLM processing
- Never call the LLM directly — the agent processes `events` table records
- Be called from `poll_connectors()` registered on PTB's `job_queue`
---
 
### Phase 3 — Briefings & Proactive Alerts
 
**Goal:** Friday speaks first when it matters.
 
- [ ] **Morning briefing** — registered on `job_queue.run_daily()` at morning time from config. Format: "Good morning, sir. Here is your day:" followed by chronological calendar items for the day. Pulls from Apple Calendar, not SQLite events.
- [ ] **Evening briefing** — registered on `job_queue.run_daily()` at `briefing_time` from config. Summarizes tomorrow's calendar, pending Canvas due dates, unresolved alerts.
- [ ] **On-demand briefing** — user sends "brief me" → agent pulls Apple Calendar + unnotified events → single LLM call → Telegram response.
- [ ] **Proactive reminders** — `job_queue.run_repeating()` checks Apple Calendar for items due in 5 days, 3 days, and 1 day. Fires once per threshold per event. Example: "Sir, your History paper is due tomorrow at 11:59 PM."
- [ ] **Urgent interrupt** — LLM tags event as URGENT during processing → immediate Telegram message fires without waiting for briefing cycle → `notified = 1`.
---
 
### Phase 4 — Calendar Writing & GroupMe
 
**Goal:** Friday can modify Apple Calendar and read GroupMe.
 
- [ ] **Apple Calendar write** (`actions/calendar.py`) — writes to user's default Apple Calendar. Canvas due dates written automatically (no gate). All other calendar writes go through `send_permission_request`. Uses `caldav` library or AppleScript via subprocess.
- [ ] **Manual Nation entry** — user sends work schedule via Telegram text. LLM parses into structured event, proposes calendar entry, approval gate, then writes. Nation has no API and sends no emails. Always manual.
- [ ] **GroupMe reading** (`connectors/groupme.py`) — polls GroupMe API for new messages since `last_seen` cursor. Writes to `events` table. LLM evaluates with awareness of group priority tier. High priority groups: urgent items interrupt immediately. Low priority groups: briefing only.
---
 
### Phase 5 — Drafting & Sending
 
**Goal:** Friday composes replies with full context, user approves before anything is sent or saved.
 
- [ ] **Gmail drafts** — Friday composes reply via LLM. Shows draft in Telegram for approval. On confirm, saves to Gmail native Drafts folder via API. Never sends directly.
- [ ] **GroupMe replies** (`actions/groupme_send.py`) — Friday shows the original thread context alongside the proposed reply in Telegram. Format:
  ```
  📨 GroupMe — [Group Name]
  [Person]: "original message"
 
  ✏️ Suggested reply:
  "proposed reply text"
 
  ✅ Confirm  ✏️ Edit  ❌ Cancel
  ```
  On confirm, sends via GroupMe API immediately.
- [ ] **No iMessage drafting** — not implemented.
---
 
### Phase 6 — Voice Interface
 
**Goal:** User can speak to Friday as an alternative to typing in Telegram.
 
- [ ] **Separate script** (`voice/listen.py`) — fully standalone. Never imported by `friday.py`. Has no knowledge of Friday's internals.
- [ ] **Wake word** — "Hey Friday" detected using lightweight local model (e.g. `openwakeword`)
- [ ] **Transcription** — audio captured and transcribed locally using `whisper` (small or base model)
- [ ] **Telegram bridge** — transcription sent as Telegram message to the bot. Friday processes it identically to a typed message. Friday's core requires zero changes.
---
 
### Phase 7 — Hardening & Polish
 
**Goal:** Improve reliability, observability, and experience across all existing features.
 
- [ ] Improve LLM urgency and announcement filtering accuracy based on real usage
- [ ] Refine briefing format and timing based on user preference
- [ ] Robust error handling and recovery across all connectors
- [ ] Menu bar and dashboard improvements
- [ ] Logging and observability improvements
---
 
## Key Constraints & Rules for Claude Code
 
1. **Never remove or move the semaphore.** It lives at the top of `on_message()` in `telegram.py`. No exceptions.
2. **Never use a second scheduling library.** No `schedule`, no raw `apscheduler`, no background threads for timing. PTB `JobQueue` only.
3. **Never poll iMessage.** Not via AppleScript, not via `chat.db`, not via any method.
4. **Never write externally without an approval gate** — except Canvas due dates to Apple Calendar which are auto-written.
5. **Never use `/usr/bin/python3` in the LaunchAgent plist.** Always the absolute venv binary path.
6. **Canvas uses the iCal feed.** Never HTML scraping. Use `icalendar` library.
7. **The LLM processes all ingested data.** Never bypass the LLM for urgency, filtering, or calendar decisions — even for clean structured data.
8. **Apple Calendar is the event store.** Briefings and reminders pull from Apple Calendar, not the SQLite events table.
9. **GroupMe confirm = send immediately.** Show thread context with every draft. Use `actions/groupme_send.py`.
10. **Gmail confirm = save to Drafts only.** Never send Gmail directly.
11. **SQLite is the operational backbone only.** No state.json. No vector store. No Redis.
12. **Voice is a standalone satellite script.** Never imports from Friday's core.
13. **Nation has no API and sends no emails.** Work schedule entry is always manual via Telegram text.
14. **Gmail is deprioritized.** School Gmail has no accessible API path. Do not implement until a clean solution exists.
15. **All secrets** live in `friday_config.yaml` or environment variables. Never hardcoded.
---
 
## Config Structure (`friday_config.yaml`)
 
```yaml
agent:
  name: Friday
  morning_briefing_time: '08:00'
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
  model: gemma-4-31b-it
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
  groups:
    - id: ''
      name: ''
      priority: high   # high | low
    - id: ''
      name: ''
      priority: low
 
weather:
  api_key: ''
  location: ''
```
 
---
 
## Persona Notes (`AGENTS.md`)
 
Friday's persona must convey:
- Concise, direct, professional tone — a real secretary, not a chatbot
- Addresses the user as "sir" unless told otherwise
- Proactive about urgency, conservative about unnecessary interruptions
- Always states the source of information ("Your Canvas feed shows...", "From GroupMe...")
- Never hallucinates events or deadlines — if uncertain, says so and asks
- Morning briefing format: "Good morning, sir. Here is your day:" followed by chronological items
- Canvas announcements: err on the side of caution — surface anything potentially actionable
- GroupMe: high priority groups treated with same urgency as direct messages
 
 ## Google Calendar Sync

### Overview
Friday polls three Google Calendar iCal subscription URLs and mirrors new events into the
corresponding Apple Calendar. Sync is **one-directional: Google → Apple only**. There is no
reverse sync. The secret iCal URLs require no OAuth, no API key, and no school account
authentication — just the URL.

### Calendar Mapping
| Google Calendar         | Apple Calendar          |
|-------------------------|-------------------------|
| PHS SGA                 | PHS SGA                 |
| FBLA Officer Calendar   | FBLA Officer Calendar   |
| Keller Leonard          | Keller Leonard          |

Apple Calendars must be created manually by the user before Friday attempts to write to them.
Friday never creates Apple Calendars — it only writes events into existing ones.

### Implementation
- **Connector:** `connectors/gcal_sync.py`
- **Action:** `actions/calendar.py` (reuses existing Apple Calendar write logic)
- **Deduplication:** `synced_events` table in SQLite tracks every Google event ID that has
  already been written to Apple Calendar. Friday never writes the same event twice.
- **Poll frequency:** Every 15 minutes via PTB `job_queue`, alongside `poll_connectors()`
- **No approval gate:** Google Calendar sync writes to Apple Calendar automatically without
  user confirmation. These are events the user already created themselves on Google.

### SQLite Addition
```sql
-- Deduplication table for Google → Apple calendar sync
CREATE TABLE synced_events (
    google_event_id  TEXT PRIMARY KEY,
    calendar_name    TEXT,       -- 'PHS SGA', 'FBLA Officer Calendar', 'Keller Leonard'
    apple_event_id   TEXT,
    synced_at        TEXT
);
```

### Config Addition
```yaml
gcal_sync:
  calendars:
    - name: PHS SGA
      ical_url: ''
    - name: FBLA Officer Calendar
      ical_url: ''
    - name: Keller Leonard
      ical_url: ''
```

### Rules
- Secret iCal URLs are treated as secrets — stored in `friday_config.yaml`, never hardcoded.
- If an iCal URL returns an error, log it and skip that calendar silently. Never crash.
- Event deduplication is based on the iCal `UID` field, which Google Calendar always provides.
- Friday does not delete Apple Calendar events if they are removed from Google Calendar.
  Deletion sync is out of scope.
- Friday does not modify existing Apple Calendar events. If a Google event changes, Friday
  logs it but does not attempt to update the Apple Calendar entry. Update sync is out of scope
  for now.

## Apple Calendar: two readers, one writer

Reads and writes take different paths, and the split is deliberate.

- **Reads** — `connectors/apple_calendar.py`. EventKit (PyObjC) first, JXA as a fallback.
  These are not interchangeable on speed: every property read over the Apple Events
  bridge is its own IPC round trip, so JXA costs ~35 ms per event *in the calendar being
  scanned*, not per event returned. A 2,600-event shared calendar took 55 s to answer
  "what is on today"; a full briefing bundle took over six minutes and timed out on every
  read, so briefings reported an empty day. EventKit answers the same query in ~3 ms
  because it reads the local store instead of talking to Calendar.app. Bulk property
  fetches (`cal.events.startDate()`) are *not* a workaround — measured slower still.
- **Writes** — `calendars/apple.py`, JXA only. Writes are one event at a time, so the
  per-round-trip cost never accumulates. Do not port them to EventKit for symmetry.

EventKit needs both `NSCalendarsUsageDescription` and, on macOS 14+,
`NSCalendarsFullAccessUsageDescription` in the app's Info.plist. Without the second key
the request resolves to *write-only* access, which reports as granted and then returns
zero events — the reader treats that as unavailable and falls back. Authorization is
requested once per process and cached both ways.

The EventKit completion handler must return `None`. PyObjC checks the block signature
against the ObjC `void` return and raises inside the callback thread otherwise, which
surfaces as an uncaught NSException that aborts the process — not something the calling
code can catch.

## Microphone Access (TCC) and the Orange Indicator

macOS TCC microphone permission is granted per executable binary path, not per Python
script. Friday's two processes use different binaries:

- `friday.py` runs directly under whichever interpreter `macos_setup.resolve_python()` picked
  when the LaunchAgent was generated (recorded in the agent's `FRIDAY_PYTHON` env var)
- `voice/listen.py` runs under the `FridayVoice.app/Contents/MacOS/FridayVoice` wrapper

Each binary needs its own TCC grant. A grant given to the wrapper does NOT extend to the
raw python binary, and vice versa. Under launchd, TCC denial returns silent (zero-filled)
audio buffers rather than an error — so a denied process opens its stream "successfully"
and reads nothing forever. Code that opens an input stream from friday.py must validate
real signal at boot (probe peak > 0) and log a clear TCC warning on zero-only buffers.

The orange "mic in use" indicator lights whenever any process holds an active input
stream. `voice/listen.py` only starts the always-on stream at boot if `voice.wake_enabled`
or `voice.clap_enabled` is true; with both false, listen.py runs in PTT-only mode and
only opens the mic during an actual PTT session — dot off at idle. **Config changes do
not affect a running listen.py.** After flipping wake/clap flags, restart the voice
LaunchAgent (`launchctl kickstart -k gui/$(id -u)/com.friday.voice`) for it to take
effect. The boot-time `_probe_microphone` call briefly opens the mic to trigger the TCC
dialog — this is by design and does not mean the always-on stream is running.

If a process appears running via `launchctl print` but the menubar reports voice offline,
the menubar polls `/api/voice/status` every few seconds and may have cached an earlier
failed boot. Wait ~10 seconds or restart via the menubar's "Restart Voice" item.
## Location

`connectors/location.py` answers "where am I", exposed as the `get_location` tool.
It reports where the **Mac** is, not where the user is — there is no passive way to
read a phone's position from here, so the answer is "home" whenever the machine is
home and it does not move when the user does. The tool docstring instructs the LLM
never to claim knowledge of the user's personal whereabouts.

Two backends, tried in order, both lazy-imported so a failure degrades instead of
crashing:

- **CoreLocation** (PyObjC) — device positioning, accurate to tens of metres.
  Subject to the same per-binary TCC rule as the calendar and mic: under the
  LaunchAgent, friday.py is a bare interpreter with no Info.plist, so
  `NSLocationWhenInUseUsageDescription` is absent and authorization resolves to
  `kCLErrorDenied` (code 1) **without ever prompting**. Only the packaged .app
  gets the prompt. Delegate methods and the CLGeocoder completion handler must
  return `None` — same PyObjC NSException trap as the EventKit handler.
- **IP geolocation** — city-level, keyless, works on Windows too. Not optional:
  it is the only path that answers under the LaunchAgent. Two providers are tried
  in order because these services throttle by source IP without warning (ipapi.co
  returned 429 on the very first request and was dropped).

Fixes are cached for 5 minutes; an always-on Mac does not move, and a CoreLocation
round trip costs seconds.

## Windows Port (friend's build)

The same codebase runs on Windows, packaged as `FridaySetup.exe` (Inno Setup wrapping a
PyInstaller onedir build). Architecture differences are isolated behind small seams:

- **Calendar backend dispatch** (`calendars/backend.py`): config `calendar.backend`
  selects `apple` (JXA, the existing code, now in `calendars/apple.py`) or `google`
  (`calendars/google_cal.py`, Google Calendar API + OAuth installed-app flow). Default
  is google on win32, apple elsewhere. All reads/writes — tools, briefings, Canvas due
  dates, gated writes — go through this dispatcher. On the google backend, gcal_sync is
  skipped (Google already IS the event store) and missing calendars are auto-created.
- **Paths** (`paths.py`): mutable state (config, db, logs, Google token) lives in
  `%APPDATA%\Friday` on Windows/frozen builds, in the package dir on macOS. Bundled
  read-only resources (AGENTS.md, quips.yaml, dashboard static) resolve via
  `paths.resource_path()` (PyInstaller `_MEIPASS`-aware).
- **compat.py**: `compat.strftime()` translates glibc `%-d`/`%-I` to Windows `%#d`;
  always use it for format strings with `%-`. Also `IS_WINDOWS` and the listening-flag
  temp path.
- **Process model**: `tray.py` (pystray) is the Windows entry point — it supervises the
  core (`Friday.exe --core`) and auto-restarts it on exit. The dashboard's
  `/api/friday/restart` on Windows just raises SIGINT in-process; the tray brings it
  back. Quit sets a flag so the exit is final. No launchd, no services.
- **run_polling on Windows must NOT receive stop_signals** — Proactor loops have no
  `add_signal_handler`; friday.py only passes them on non-Windows.
- **First run**: `setup_wizard.py` (Tkinter) collects Telegram token (+ chat-id
  auto-detect), Gemini key, Google OAuth + calendar pickers, optional Canvas/weather.
- **Packaging**: `packaging/windows/` (spec, installer.iss, build.ps1, BUILD_WINDOWS.md)
  and `.github/workflows/build-windows.yml` (CI build, artifact `FridaySetup.exe`).
  The Google OAuth desktop client JSON is created once by the maintainer (friend added
  as test user) and bundled at build time; see BUILD_WINDOWS.md.
- **Out of scope on Windows**: voice, menubar.py/rumps, iMessage (still banned everywhere).
