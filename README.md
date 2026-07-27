# Project Friday

A personal AI secretary that runs on an always-on machine. Friday ingests information from
Canvas, GroupMe, Google Calendar and the weather API, manages your calendar, delivers
proactive briefings and urgent alerts over Telegram, and drafts replies for your review.

**Telegram is the sole user interface.** Briefings, alerts, approvals, drafts and
conversational queries all happen in one Telegram chat. There is also a local web dashboard
and a menu bar / tray app, but those are for configuration and status — not conversation.

Friday is not a chatbot wrapper. It is an event-driven agent with a tool layer, a memory
layer, and a proactive alert system.

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Where files live](#where-files-live)
- [Configuration reference](#configuration-reference)
- [Connector setup](#connector-setup)
  - [Telegram (required)](#1-telegram-required)
  - [LLM provider (required)](#2-llm-provider-required)
  - [Calendar backend (required)](#3-calendar-backend-required)
  - [Canvas](#4-canvas)
  - [Weather](#5-weather)
  - [GroupMe](#6-groupme)
  - [Google Calendar → Apple Calendar sync](#7-google-calendar--apple-calendar-sync)
  - [Voice (macOS only)](#8-voice-macos-only)
- [Running Friday](#running-friday)
- [The dashboard](#the-dashboard)
- [Troubleshooting](#troubleshooting)

---

## How it works

### Architecture principles

These are load-bearing. Breaking one of them breaks Friday in ways that are hard to debug.

| Principle | Why |
|---|---|
| **PTB JobQueue is the only scheduler** | `python-telegram-bot` is fully async. Every scheduled job registers on `application.job_queue` so it shares one event loop and one Telegram connection. Never add `apscheduler`, `schedule`, or a `while True: sleep()` thread. |
| **The semaphore lives at the entry point** | `asyncio.Semaphore(1)` is acquired at the very top of the Telegram message handler — before SQLite queries, before context assembly. Messages queue from the first byte. It is never placed only around the LLM call. |
| **The LLM is the single decision maker** | Deterministic code fetches, parses, and writes raw records. The LLM decides urgency, filters announcements, parses natural language, and formats all output. It is never bypassed for "simple" structured data. |
| **The calendar is the event store** | Due dates, shifts and appointments live in Apple Calendar (macOS) or Google Calendar (Windows) — not SQLite. Briefings and reminders read from the calendar. |
| **SQLite is operational state only** | Runtime key-values, conversation history, a raw ingested-events buffer, and per-source cursors. No `state.json`, no vector store, no RAG, no Redis. |
| **No iMessage, ever** | Not polled, not read, not drafted to. Out of scope on every platform. |

### Runtime shape

```
friday.py
└── builds the PTB Application (owns the only event loop)
    ├── MessageHandler  → channels/telegram.py::on_message()
    │     └── asyncio.Semaphore(1)   ← the gate, before everything
    │           ├── read context from SQLite
    │           ├── agent/core.py  (LLM call + tool layer)
    │           └── send reply to Telegram
    ├── MessageHandler  → on_media()      (photos / PDFs → vision extraction)
    ├── CallbackQueryHandler → on_callback() (approval buttons)
    │
    ├── dashboard web server (same asyncio loop, 127.0.0.1:5174)
    │
    └── job_queue
          ├── run_daily     → morning_briefing_job    (default 07:00)
          ├── run_daily     → briefing_job (evening)  (default 21:45)
          ├── run_daily     → cleanup_activity_job    (03:00)
          ├── run_repeating → poll_connectors_job     (every 900 s)
          └── run_repeating → check_urgent_alerts_job (every 60 s)
```

### Ingestion flow

```
connector fetches raw data (HTTP / iCal)
        ↓
raw record written to the `events` table, unprocessed
        ↓
LLM evaluates each record:
   • assigns urgency (URGENT / SOON / NORMAL)
   • decides the action (calendar write / alert / briefing entry / ignore)
   • for Canvas announcements, decides whether it is actionable
   • for GroupMe, weighs the group's priority tier
        ↓
URGENT          → immediate Telegram interrupt, notified = 1
calendar action → approval gate  (exception: Canvas due dates auto-write)
briefing entry  → sits in `events` until the next briefing
```

Deterministic code only ever handles HTTP requests, iCal parsing, raw SQL writes and API
auth. Everything downstream is the LLM's call.

### Briefing safety windows

A briefing that fires far outside its expected hour almost always means a timezone
misconfiguration, so Friday refuses to send rather than pinging you at 2 AM:

- Morning briefing only fires between **06:00 and 10:00** local
- Evening briefing only fires between **19:00 and 24:00** local

There is also missed-briefing catch-up: on startup and on every poll cycle, Friday checks
whether a briefing was skipped (machine asleep, process down) and runs it late, bounded by
`agent.briefing_catchup_max_minutes` (default 120).

### SQLite schema

```sql
system_state         (key, value, updated_at)                  -- replaces state.json
events               (id, source, title, body, due_at, urgency,
                      processed, notified, calendar_synced, created_at)
last_seen            (source, cursor, updated_at)              -- per-source cursors
pending_actions      (id, action_type, payload, status, created_at)
conversation_history (id, role, content, created_at)
synced_events        (google_event_id, calendar_name, apple_event_id, synced_at)
```

---

## Requirements

- **Python 3.11+** (this checkout is developed against the python.org 3.14 framework build
  on macOS). `zoneinfo` and modern `X | None` type syntax are used throughout.
- **macOS** for the Apple Calendar backend, the rumps menu bar app, and voice.
- **Windows** is supported via a separate packaging path (see
  `packaging/windows/BUILD_WINDOWS.md`); it uses the Google Calendar backend and a
  pystray tray app instead. Voice and the menu bar are out of scope on Windows.

---

## Installation

### macOS (source checkout)

```bash
cd friday          # the inner package directory
python3 -m pip install -r requirements.txt
```

`requirements.txt` pulls in `python-telegram-bot[job-queue]`, `google-genai`, `icalendar`,
`rumps`, `fastapi`, `uvicorn`, `PyMuPDF` and `pyyaml`.

Then create your config:

```bash
cp friday_config.yaml.example friday_config.yaml
```

…and fill it in using the [connector setup](#connector-setup) section below. The config file
is gitignored — it holds live tokens.

The example file is annotated with where each credential comes from and which blocks are
optional.

### Windows

Windows users install from `FridaySetup.exe` — an Inno Setup installer wrapping a
PyInstaller onedir build. Dependencies come from `requirements-win.txt` (no `rumps`; adds
`pystray`, `Pillow`, the Google API client libraries, `tzdata` and `tzlocal`). See
`packaging/windows/BUILD_WINDOWS.md` for how the build and the bundled Google OAuth client
are produced.

### First-run wizard

`setup_wizard.py` is a Tkinter wizard that collects the essential config interactively:
Telegram token (with chat-ID auto-detect), Gemini key, Google OAuth and calendar pickers,
and optional Canvas and weather settings. It runs automatically on Windows first launch, and
can be invoked directly:

```bash
cd friday
python3 -c "import sys; sys.frozen=True; import setup_wizard; setup_wizard.run(first_run=True)"
```

The wizard validates the Telegram token live (`getMe`) at the point of entry, supports Back
navigation with value preservation, masks tokens in every message and log line, and has a
"Start over" reset. It only writes the config file at the final confirmation step, so an
abandoned run never leaves a dead token on disk.

---

## Where files live

`paths.py` is the single source of truth. Behaviour differs by platform:

| | macOS source checkout | Windows / any frozen build |
|---|---|---|
| Config | `friday/friday_config.yaml` | `%APPDATA%\Friday\friday_config.yaml` |
| Database | `friday/memory/friday_memory.db` | `%APPDATA%\Friday\memory\friday_memory.db` |
| Logs | `friday/logs/` | `%APPDATA%\Friday\logs\` |
| Google OAuth token | `friday/google_token.json` | `%APPDATA%\Friday\google_token.json` |
| Bundled resources (`AGENTS.md`, `quips.yaml`, dashboard static) | package directory | PyInstaller `_MEIPASS` bundle |

`memory.db_path` in the config may be relative (resolved against the data directory) or an
absolute path.

---

## Configuration reference

Everything lives in `friday_config.yaml`. Secrets are never hardcoded in source. Several
keys also accept an environment variable as a fallback.

```yaml
agent:
  name: Friday
  morning_briefing_time: '07:00'
  briefing_time: '21:45'           # evening briefing
  timezone: America/Chicago
  briefing_catchup_max_minutes: 120

  # Fallback calendar used when a write target doesn't exist (e.g. Canvas
  # auto-sync with no 'Canvas' calendar present). macOS exposes no scriptable
  # "default calendar", so this must be set explicitly.
  default_calendar: 'Keller Leonard'

  # Whitelist of calendar names to include in briefings. Leave empty to include
  # everything except 'Siri Suggestions' and 'US Holidays'.
  briefing_calendars:
    - Keller Leonard
    - Work
    - Family

telegram:
  bot_token: ''      # env fallback: TELEGRAM_BOT_TOKEN
  chat_id: ''        # env fallback: TELEGRAM_CHAT_ID

memory:
  db_path: memory/friday_memory.db
  short_term_turns: 20             # rolling conversation window

provider: gemini                   # gemini | ollama

gemini:
  model: gemma-4-31b-it
  max_tokens: 1000
  api_key: ''                      # env fallback: GEMINI_API_KEY

ollama:
  model: llama3.2:1b
  base_url: http://localhost:11434
  max_tokens: 1000

calendar:
  backend: apple                   # apple | google. Defaults to google on
                                   # win32, apple everywhere else.
  google:
    auto_create: true              # create missing calendars (google backend only)

canvas:
  ical_url: ''
  api_token: ''

weather:
  api_key: ''                      # env fallback: WEATHER_API_KEY
  location: ''                     # env fallback: WEATHER_LOCATION

groupme:
  api_token: ''
  groups:
    - name: 'Student Council'      # id OR name; name is resolved via the API
      id: ''
      priority: high               # see the GroupMe section on priority values
      enabled: true                # false stops polling this group entirely
    - name: 'Class of 2026'
      id: ''
      priority: normal
      enabled: true

gcal_sync:
  calendars:
    - name: PHS SGA
      ical_url: ''
    - name: FBLA Officer Calendar
      ical_url: ''
    - name: Keller Leonard
      ical_url: ''

notifications:                     # written lazily by the dashboard on first load
  morning_briefing: { enabled: true, time: '07:00' }
  evening_briefing: { enabled: true, time: '21:45' }
  proactive_reminders: true
  urgent_interrupts: true
  canvas_polling: true
  groupme_polling: true
  reminder_thresholds: [5, 3, 1]   # days before a due date

voice:                             # macOS only
  enabled: true
  mic_enabled: true
  wake_enabled: false
  clap_enabled: false
  always_speak: false
  whisper_model: base
  push_to_talk_key: right_option
  tts_voice: Daniel
  silence_ms: 1500
  max_recording_ms: 30000
  wake_phrases: ['Hey Friday', 'Friday you up', 'Friday']
```

> The `agent.morning_briefing_time` / `agent.briefing_time` keys are **canonical** — the
> JobQueue reads those. The `notifications` block is a dashboard-facing mirror. If the two
> disagree, `agent` wins at runtime.

Startup validation (`check_environment`) hard-exits if `telegram.bot_token` or
`telegram.chat_id` are missing, or if `provider: gemini` with no Gemini API key. Everything
else degrades gracefully: an unconfigured connector is simply skipped.

---

## Connector setup

### 1. Telegram (required)

Friday's entire UI. Without this it will not start.

**Get a bot token**

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, pick a display name and a username ending in `bot`.
3. BotFather replies with a token shaped like `123456789:AAH...` — roughly
   `^\d+:[A-Za-z0-9_-]{30,}$`.

**Get your chat ID**

1. Send any message to your new bot first — Telegram will not expose a chat that has never
   been messaged.
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
3. Read `result[0].message.chat.id`. That number is your `chat_id`.

The setup wizard automates both steps and distinguishes "you haven't messaged the bot yet"
from a genuine auth failure.

```yaml
telegram:
  bot_token: '123456789:AAH...'
  chat_id: '987654321'
```

**Do not regenerate your token as a troubleshooting step.** Regenerating invalidates the
old one and is the single most common way to make a working setup unrecoverable. If Friday
reports a token problem, first confirm it isn't a network error.

---

### 2. LLM provider (required)

Set `provider:` to either `gemini` or `ollama`. The menu bar / tray app can flip between
them, which rewrites the config and restarts the Friday process.

**Gemini (cloud, recommended)**

1. Get an API key from [Google AI Studio](https://aistudio.google.com/apikey).
2. Put it in `gemini.api_key`, or export `GEMINI_API_KEY`.

```yaml
provider: gemini
gemini:
  model: gemma-4-31b-it
  api_key: 'AIza...'
  max_tokens: 1000
```

**Ollama (local, no API key)**

1. Install [Ollama](https://ollama.com) and start it — it listens on `localhost:11434`.
2. Pull a model: `ollama pull llama3.2:1b`.

```yaml
provider: ollama
ollama:
  model: llama3.2:1b
  base_url: http://localhost:11434
```

Small local models are noticeably worse at urgency tagging and announcement filtering. If
briefings read as noisy, that is usually the model, not the prompt.

---

### 3. Calendar backend (required)

`calendars/backend.py` dispatches every read and write — briefings, Canvas due dates, gated
writes, gcal_sync — to one of two implementations. Selection is
`calendar.backend` in the config, defaulting to `google` on Windows and `apple` everywhere
else.

**Apple backend (macOS)** — `calendars/apple.py`, driven via JXA. No setup beyond
**creating the calendars yourself in Calendar.app first.** Friday never creates Apple
calendars; it only writes into existing ones. Create at minimum:

- Your `agent.default_calendar`
- A `Canvas` calendar, if you want Canvas due dates separated out
- One calendar per `gcal_sync` entry, named exactly as configured

**Google backend (Windows)** — `calendars/google_cal.py`, using the Google Calendar API
with an installed-app OAuth flow. Scope: `https://www.googleapis.com/auth/calendar`.

1. Create an OAuth **Desktop app** client in the Google Cloud Console and download the
   client JSON. (For the packaged Windows build, the maintainer creates this once and it is
   bundled at build time — see `BUILD_WINDOWS.md`.)
2. Place it at `%APPDATA%\Friday\google_client_secret.json`, or let the wizard copy it there.
3. On first run the browser opens for consent; the resulting token is cached at
   `google_token.json` in the data directory.

On the Google backend, `gcal_sync` is skipped entirely (Google already *is* the event
store) and missing calendars are auto-created when `calendar.google.auto_create` is true.

---

### 4. Canvas

Read-only. Fetches the Canvas iCal feed, deduplicates by iCal `UID`, and writes raw records
to `events`. Never HTML scraping.

**Get your iCal feed URL**

1. In Canvas, open **Calendar** from the left nav.
2. Click **Calendar Feed** in the lower right sidebar.
3. Copy the `https://…/feeds/calendars/user_….ics` URL.

**Get an API token** (optional — only needed if your institution's feed requires auth)

1. Canvas → **Account** → **Settings**.
2. Under *Approved Integrations*, click **+ New Access Token**.
3. Copy the token immediately; Canvas shows it once.

```yaml
canvas:
  ical_url: 'https://school.instructure.com/feeds/calendars/user_abc123.ics'
  api_token: ''
```

If set, the token is sent as `Authorization: Bearer <token>`. A 401/403 is logged as an auth
failure and the poll returns zero rather than crashing.

**Behaviour:** Canvas due dates with a future `due_at` are written to the `Canvas` calendar
**automatically, with no approval gate** — this is the one documented exception to the
approval rule. Writes are idempotent via the `events.calendar_synced` flag; past-due and
unparseable items are flagged synced so they aren't retried forever. If the write fails, the
flag stays at 0 and the next 15-minute poll retries.

---

### 5. Weather

Stateless — no storage, no cursor, no `events` rows. Called on demand when you ask, and
injected into the evening briefing.

1. Create a free account at [OpenWeatherMap](https://openweathermap.org/api).
2. Copy your API key from the *API keys* tab. **New keys take up to a couple of hours to
   activate** — a fresh key returning 401 is normal, not a misconfiguration.

```yaml
weather:
  api_key: 'abc123...'
  location: 'Austin,US'    # "City,CC" — the OpenWeatherMap query format
```

Uses the `/data/2.5/weather` and `/data/2.5/forecast` endpoints in imperial units. The
connector parses intent from your question (rain / temperature / general) and time
references ("tonight", "tomorrow afternoon", "at 3pm") to answer specifically. Any failure
returns an empty string, and Friday simply omits weather rather than erroring.

---

### 6. GroupMe

Read-only polling of each configured group, with priority tiers.

**Get an API token**

1. Go to [dev.groupme.com](https://dev.groupme.com) and sign in with your GroupMe account.
2. Click **Access Token** in the top right.
3. Copy the token.

**Identify your groups** — you can give either an `id` or a `name`. A name-only entry is
resolved against your account's group list via `GET /groups`, cached for the process
lifetime. To find IDs explicitly:

```
https://api.groupme.com/v3/groups?token=YOUR_TOKEN
```

```yaml
groupme:
  api_token: 'your-token'
  groups:
    - name: 'Student Council'
      priority: high
    - id: '12345678'
      name: 'Class of 2026'
      priority: normal
      enabled: false        # omit to mean true
```

**The `enabled` switch** (the toggle on each dashboard group card) stops a group being
polled at all — no API call, no rows stored. `muted` still ingests for history; `enabled:
false` does not. Turning a group back on resumes from the **newest** message rather than
replaying the gap, so a group left off for a month won't flood your briefing when you
re-enable it.

**Priority tiers.** Defined once in `connectors/groupme.py` and matched by every consumer:

| Tier | Can interrupt | In briefings | Stored |
|---|---|---|---|
| `high` | yes — treated like a direct message | yes | yes |
| `normal` | no | yes | yes |
| `muted` | no | no | yes, for history only |

`low` is the pre-dashboard spelling and is read as `muted` — that is what it always did in
practice. An unset priority defaults to `normal`; an unrecognized one logs a warning and
falls back to `normal`.

The connector prepends a `[priority=<tier>]` tag to each stored body as a signal for the
LLM's urgency pass and the briefing queries. That tag is stripped before anything reaches
your Telegram chat.

**No backfill on first poll.** The first time Friday sees a group, it reads the newest
message ID, stores it as the cursor, and writes nothing. You will not get blasted with
history. Subsequent polls use `since_id`.

**Sending** is approval-gated (`actions/groupme_send.py`). Friday shows the original thread
context alongside the proposed reply; confirming sends immediately via the API.

---

### 7. Google Calendar → Apple Calendar sync

Mirrors Google Calendar iCal subscriptions into same-named Apple Calendars. **One-way, Google
→ Apple only.** No OAuth, no API key, no school-account authentication — just the secret
iCal URL. This runs only on the `apple` backend; it is skipped on `google`.

**Get a secret iCal URL**

1. Open [Google Calendar](https://calendar.google.com) in a browser.
2. Hover the calendar in the left sidebar → **⋮** → **Settings and sharing**.
3. Scroll to *Integrate calendar* and copy **Secret address in iCal format**.

Treat these as secrets — anyone with the URL can read the calendar.

```yaml
gcal_sync:
  calendars:
    - name: PHS SGA
      ical_url: 'https://calendar.google.com/calendar/ical/.../basic.ics'
    - name: FBLA Officer Calendar
      ical_url: 'https://calendar.google.com/calendar/ical/.../basic.ics'
```

**Create the matching Apple Calendars manually first** — names must match `name:` exactly.
Friday never creates Apple calendars.

**Behaviour:** polls every 15 minutes alongside the other connectors. Deduplication is by
iCal `UID`, tracked in the `synced_events` table, so nothing is written twice. **No approval
gate** — these are events you already created yourself. A failing URL is logged and that
calendar is skipped; Friday never crashes on it.

Out of scope, by design: deletion sync (removing a Google event leaves the Apple copy) and
update sync (a changed Google event is logged but the Apple entry is not modified).

---

### 8. Voice (macOS only)

`voice/listen.py` is a **fully standalone satellite script**. It never imports from Friday's
core and Friday has no knowledge of it. Transcriptions are sent to the bot as ordinary
Telegram messages, so the core needs zero changes.

Wake word detection is local, transcription is local Whisper, and the bridge back to Friday
is Telegram.

```yaml
voice:
  enabled: true
  wake_enabled: false      # always-on wake word listening
  clap_enabled: false      # clap trigger
  push_to_talk_key: right_option
  whisper_model: base
  tts_voice: Daniel
```

**Microphone permission (TCC) is granted per executable binary, not per script.** Friday's
two processes use different binaries:

- `friday.py` runs under whichever interpreter the LaunchAgent points at (its `FRIDAY_PYTHON`
  environment variable records it)
- `voice/listen.py` runs under the `FridayVoice.app/Contents/MacOS/FridayVoice` wrapper

Each needs its own grant; granting one does **not** cover the other.

**The orange mic indicator** lights whenever any process holds an active input stream.
`listen.py` opens the always-on stream at boot only if `wake_enabled` or `clap_enabled` is
true. With both false it runs in push-to-talk-only mode and opens the mic only during an
actual PTT session — indicator off at idle. The brief boot-time `_probe_microphone` call
opens the mic to trigger the TCC dialog; that is by design and does not mean the always-on
stream is running.

**Config changes do not affect a running `listen.py`.** After flipping the wake/clap flags:

```bash
launchctl kickstart -k gui/$(id -u)/com.friday.voice
```

---

## Running Friday

### Directly

```bash
cd friday
python3 friday.py
```

Logs go to `logs/friday.log` (file only — nothing on stdout).

### With the watchdog

`restart.sh` is the normal way to (re)start Friday on macOS. It takes a `/tmp` lock so two
concurrent restarts can't race, kills any existing watchdog and `friday.py`, waits for a
clean exit, then relaunches under a supervising loop that restarts on crash — giving up
after 5 crashes in under 10 seconds each so a broken config doesn't spin forever.

```bash
cd friday
./restart.sh
```

### At login (macOS LaunchAgent)

`macos_setup.py` generates the LaunchAgents against paths resolved on the machine actually
running Friday — there is nothing to hand-edit:

```bash
cd friday
python3 -c "import macos_setup; macos_setup.install_agents(voice=True)"
```

That writes `com.friday.core`, `com.friday.menubar` and (with `voice=True`)
`com.friday.voice` into `~/Library/LaunchAgents/`, validates each generated plist by parsing
it, and `bootout`/`bootstrap`s them so the change takes effect immediately. The interpreter
it embeds is `friday/.venv/bin/python3` if that exists, otherwise the Python you ran the
command with — never `/usr/bin/python3`, which lacks the dependencies. `RunAtLoad` and
`KeepAlive` are always true.

`macos_setup.uninstall_agents()` unloads and deletes all three.

### Windows

`tray.py` (pystray) is the entry point. It supervises the core process (`Friday.exe --core`)
and auto-restarts it on exit. There is no launchd and no Windows service. The dashboard's
restart endpoint raises `SIGINT` in-process and the tray brings the core back; Quit sets a
flag so the exit is final.

---

## The dashboard

A FastAPI + uvicorn server that runs **inside Friday's own asyncio loop** — not a separate
process — bound to `127.0.0.1:5174`. It is never exposed to the network, so it has no auth.

Open it from the menu bar / tray **Open Dashboard** item, or browse to
<http://127.0.0.1:5174>.

It offers full config editing (briefing times, provider and model, API keys, GroupMe group
priorities, notification toggles, voice settings), live status, a Today activity feed, and a
restart control.

The menu bar app (`menubar.py`, rumps) shows status and offers Brief Me Now, Pause Friday,
Open Dashboard, a Gemini/Ollama provider switch, and wake-word mute. It polls
`/api/voice/status` every few seconds.

---

## Troubleshooting

### Friday won't start at all

**Check the log first — it is the only place errors go.**

```bash
tail -50 friday/logs/friday.log
```

| Log line | Cause | Fix |
|---|---|---|
| `Config not found: …` | No `friday_config.yaml` at the path `paths.config_path()` resolves to | Copy the example file into place; on Windows check `%APPDATA%\Friday\` |
| `Config error: telegram.bot_token not set.` | Missing token | Set it in the config or export `TELEGRAM_BOT_TOKEN` |
| `Config error: telegram.chat_id not set.` | Missing chat ID | See [Telegram setup](#1-telegram-required) |
| `Config error: GEMINI_API_KEY not set.` | `provider: gemini` with no key | Add `gemini.api_key` or export `GEMINI_API_KEY`, or switch to `provider: ollama` |
| `ModuleNotFoundError` | Wrong interpreter | You are running a Python that lacks the deps — see below |
| Crash loop, then `crashed 5 times in <10s each. Giving up.` | Startup error the watchdog can't survive | Run `python3 friday.py` in the foreground to see the real traceback |

**Wrong-interpreter problems** are the most common install failure. `run.sh` and
`restart.sh` resolve the interpreter at runtime, first hit wins:

1. `$FRIDAY_PYTHON`, if set and executable — the LaunchAgents export this
2. `friday/.venv/bin/python3`, a venv sitting next to `friday.py`
3. `python3` on `PATH`

If you installed dependencies into a different Python than the one being picked, either
create the venv at `friday/.venv` or export `FRIDAY_PYTHON=/path/to/your/python3`. To see
what is actually being chosen:

```bash
cd friday
FRIDAY_PYTHON= ./run.sh --version 2>&1 | head -1
```

Nothing in the tree hardcodes a Python version or a home directory any more. If you find one
that does, it is a bug.

### Nothing arrives in Telegram

1. **Confirm the chat ID is yours.** Visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and compare
   `result[*].message.chat.id` with your config.
2. **Message the bot first.** Telegram will not deliver to a chat that has never been
   opened from your side.
3. **Look for a startup message.** On boot Friday sends
   `Friday online — <provider> / <model>`. If that arrives, the token and chat ID are fine
   and the problem is downstream.
4. **Check for a second running instance.** Two processes polling the same bot token will
   fight over updates:
   ```bash
   pgrep -fl friday.py
   ```
   Kill the extras and use `restart.sh`, which enforces this for you.

### "Invalid token" that isn't

A connection or timeout failure is **not** an auth failure. Only an actual HTTP 401 or
`ok: false` from `getMe` means the token was rejected. Before touching BotFather, verify
network reachability:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getMe"
```

Regenerating a working token because of a network blip is the exact failure mode that
motivated the wizard's live validation. If you have already done it, the wizard's
**Start over / Reset setup** control clears the stale config so you can enter the new token
cleanly.

### Briefings don't fire

| Symptom | Likely cause |
|---|---|
| Nothing at the scheduled time, nothing in the log | Machine was asleep. Catch-up should fire on wake — check `briefing_catchup_max_minutes` (default 120); a longer outage is intentionally dropped. |
| Log shows the job ran but no message sent | The safety window rejected it. Morning only sends 06:00–10:00 local, evening 19:00–24:00. |
| Fires at the wrong hour | `agent.timezone` is wrong. It must be an IANA name like `America/Chicago`. On Windows, confirm `tzdata` is installed — Windows ships no system IANA database. |
| Dashboard time and actual time disagree | `agent.morning_briefing_time` / `agent.briefing_time` are canonical for the JobQueue; the `notifications` block is only a mirror. Edit the `agent` block. |
| Empty briefing despite calendar events | `agent.briefing_calendars` whitelist doesn't include the calendar. Leave it empty to include everything except `Siri Suggestions` and `US Holidays`. |

Briefing times are read **once at startup**. Editing them in the config requires a restart.

### Canvas events aren't showing up

- **Verify the feed by hand:** `curl -s "<ical_url>" | head -20` should return `BEGIN:VCALENDAR`.
- **401/403 in the log** means the feed needs the API token — add `canvas.api_token`.
- **Events fetched but no calendar entries:** only items with a **future** `due_at` are
  written. Past-due items are flagged `calendar_synced = 1` and skipped.
- **Write target missing:** Friday writes to a calendar named `Canvas`, falling back to
  `agent.default_calendar`. If neither exists in Calendar.app, every write fails silently
  and retries each poll. Create the calendar manually.
- **Duplicates after re-adding a feed:** dedup is by `canvas_<UID>` in the `events` table.
  A feed that reissues UIDs will produce new rows.

### GroupMe is quiet

- **First poll writes nothing by design** — the cursor is primed to the newest message with
  no backfill. Send a new message to the group to test.
- **`no group matching name '…' on this account`** means name resolution failed. Names are
  matched case-insensitively against `GET /groups`; if two groups share a name the first
  wins. Use an explicit `id` to be unambiguous.
- **`auth failed (401)`** — regenerate the token at dev.groupme.com.
- **Messages ingested but never surface** — check the `priority` tier. `muted` (and its
  legacy spelling `low`) stores messages without ever showing them. Use `normal` for
  briefing coverage, `high` to allow interrupts.
- **Nothing interrupts, even from an important group** — only `high` groups can produce
  URGENT/SOON. `normal` is briefing-only by design.
- **A group produces nothing at all, and no log lines mention it** — it is switched off
  (`enabled: false`). Check the toggle on its dashboard card.
- **Duplicate entries in the Today feed** — a known open issue: the `events` table can take
  duplicate rows for one GroupMe message. Tracked as a TODO in `connectors/groupme.py`.

### Weather never answers

- A brand-new OpenWeatherMap key takes **up to two hours** to activate. A 401 in that window
  is expected.
- Both `api_key` and `location` must be set, or `respond()` returns an empty string
  immediately without a network call.
- `location` must be in OpenWeatherMap's `City,CC` format — `Austin,US`, not `Austin, Texas`.
- All failures are swallowed and logged at WARNING. Grep the log for `Weather fetch failed`.

### Calendar writes fail

**Apple backend:**
- **The calendar must already exist.** Friday never creates Apple calendars. Check exact
  spelling against Calendar.app, including capitalization.
- Grant Automation/Calendar permission in **System Settings → Privacy & Security →
  Automation** for whichever binary is running Friday.
- Set `agent.default_calendar` explicitly. macOS exposes no scriptable "default calendar",
  so an unset value means writes have nowhere to fall back to.

**Google backend:**
- Delete `google_token.json` from the data directory to force a fresh OAuth consent flow.
- Confirm `google_client_secret.json` is present and is a **Desktop app** client.
- If the OAuth app is in testing mode, your account must be added as a test user.

### gcal_sync isn't mirroring

- **It does not run on the `google` backend** — Google already is the event store. Confirm
  `calendar.backend` is `apple`.
- Verify each secret iCal URL with `curl` — Google returns 404 for a revoked URL, and the
  connector logs and skips silently.
- The target Apple Calendar must exist with the exact `name:` from the config.
- **Nothing re-syncs after a manual delete.** Dedup is permanent via `synced_events`. To
  force a re-sync, delete the row:
  ```sql
  DELETE FROM synced_events WHERE google_event_id = '<uid>';
  ```
- Changed Google events are logged but never update the Apple copy — update sync is out of
  scope.

### The dashboard won't load

- It runs **inside** the Friday process. If Friday is down the dashboard is down. Check
  `logs/friday.log` first.
- Port 5174 may be taken: `lsof -i :5174`.
- It binds `127.0.0.1` only. It is not reachable from another machine, by design.

### Voice problems

| Symptom | Cause |
|---|---|
| Wake word never triggers, no error | TCC denial. Under launchd, a denied process gets silent zero-filled buffers rather than an error — it opens the stream "successfully" and reads nothing forever. |
| Orange dot on at idle | `wake_enabled` or `clap_enabled` is true, so the always-on stream is open. Set both false for PTT-only. |
| Config change did nothing | `listen.py` does not reload config. Run `launchctl kickstart -k gui/$(id -u)/com.friday.voice`. |
| Menu bar says voice offline but the process is running | The menubar polls `/api/voice/status` and may have cached an earlier failed boot. Wait ~10 s or use **Restart Voice**. |

Remember that TCC grants are per binary. The grant for `FridayVoice.app` does not extend to
the raw `python3` binary running `friday.py`, or vice versa.

### The LLM behaves badly

- **Noisy or wrong urgency tagging** is usually the model. `llama3.2:1b` is very small;
  switch to Gemini or a larger local model before rewriting prompts.
- **Truncated replies** — raise `max_tokens` for the active provider.
- **Ollama connection refused** — the daemon isn't running, or `base_url` is wrong. Test
  with `curl http://localhost:11434/api/tags`.
- Persona and decision rules live in `AGENTS.md` and `Soul.md`, loaded from the resource
  path. Editing those changes Friday's voice and urgency policy.

### Database issues

The database is at `memory/friday_memory.db` under the data directory. It is gitignored.

```bash
sqlite3 friday/memory/friday_memory.db '.tables'
sqlite3 friday/memory/friday_memory.db \
  'SELECT source, COUNT(*), MAX(created_at) FROM events GROUP BY source;'
sqlite3 friday/memory/friday_memory.db 'SELECT * FROM last_seen;'
```

To force a connector to re-poll from scratch, clear its cursor:

```sql
DELETE FROM last_seen WHERE source = 'canvas';
-- GroupMe cursors are keyed per group: 'groupme_<group_id>'
```

Deleting the database file entirely is safe — the schema is recreated on next start. You
lose conversation history, cursors (so GroupMe re-primes with no backfill) and the
`synced_events` dedup table, which means gcal_sync will re-write every event and create
duplicates in Apple Calendar. Prefer clearing individual tables.

### Getting more detail

Logging is INFO by default and file-only. For a live view:

```bash
tail -f friday/logs/friday.log
```

Per-subsystem loggers are named `friday`, `friday.canvas`, `friday.groupme`,
`friday.gcal_sync`, `friday.weather` and `friday.calbackend`, so you can filter:

```bash
grep 'friday.groupme' friday/logs/friday.log | tail -30
```

**Log files contain live tokens** — `logs/` is gitignored for that reason. Never paste raw
log output into an issue or a chat without redacting.

---

## Hard rules

These are non-negotiable constraints on the codebase:

1. Never remove or move the semaphore from the top of `on_message()`.
2. Never add a second scheduling library or a background timing thread. PTB `JobQueue` only.
3. Never poll iMessage — not via AppleScript, not via `chat.db`, not by any method.
4. Never write externally without an approval gate, except Canvas due dates and gcal_sync.
5. Never use `/usr/bin/python3` in a LaunchAgent plist. Always the absolute venv binary.
6. Canvas uses the iCal feed. Never HTML scraping.
7. The LLM processes all ingested data. Never bypass it for urgency or filtering decisions.
8. The calendar is the event store. Briefings read from it, not from the `events` table.
9. GroupMe confirm sends immediately. Gmail confirm saves to Drafts only, never sends.
10. All secrets live in `friday_config.yaml` or environment variables. Never hardcoded.
11. Voice is a standalone satellite. It never imports from Friday's core.
