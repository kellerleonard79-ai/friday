# Project Friday — Architectural Analysis

A file-by-file walkthrough of the repository: what each piece does, how it does
it, and why it exists in the shape it does.

Generated from a full read of the source at commit `6ecf5f9` (branch
`llm-call-profiles`).

---

## Table of Contents

1. [What Friday Is](#1-what-friday-is)
2. [Runtime Topology](#2-runtime-topology)
3. [Repository Map](#3-repository-map)
4. [The Core Package](#4-the-core-package)
   - [4.1 Entry Point & Platform Seams](#41-entry-point--platform-seams)
   - [4.2 The Agent Layer](#42-the-agent-layer)
   - [4.3 The Channel Layer](#43-the-channel-layer)
   - [4.4 The Memory Layer](#44-the-memory-layer)
   - [4.5 Connectors (Read Side)](#45-connectors-read-side)
   - [4.6 Calendars (Write Side + Backend Dispatch)](#46-calendars-write-side--backend-dispatch)
   - [4.7 Actions (The Approval Gate)](#47-actions-the-approval-gate)
   - [4.8 Self-Editing & Voice Files](#48-self-editing--voice-files)
   - [4.9 The Dashboard](#49-the-dashboard)
   - [4.10 Platform Front-Ends](#410-platform-front-ends)
   - [4.11 Operator Tooling](#411-operator-tooling)
5. [The Voice Satellite](#5-the-voice-satellite)
6. [Packaging & CI](#6-packaging--ci)
7. [Reference Tables](#7-reference-tables)
   - [7.1 The Complete Tool List](#71-the-complete-tool-list)
   - [7.2 Every LLM Call In The System](#72-every-llm-call-in-the-system)
   - [7.3 SQLite Schema](#73-sqlite-schema)
   - [7.4 Dashboard HTTP API](#74-dashboard-http-api)
   - [7.5 Scheduled Jobs](#75-scheduled-jobs)
   - [7.6 Configuration Surface](#76-configuration-surface)
8. [Cross-Cutting Invariants](#8-cross-cutting-invariants)
9. [Known Gaps & Rough Edges](#9-known-gaps--rough-edges)

---

## 1. What Friday Is

Friday is a personal AI secretary that runs continuously on a Mac (and, since
the Windows port, on a PC). It is not a chatbot wrapper. It is an event-driven
daemon with five distinct concerns layered on top of each other:

| Layer | Responsibility |
|---|---|
| **Ingest** | Poll Canvas LMS, GroupMe, Google Calendar iCal feeds, weather, machine location |
| **Judgment** | An LLM decides urgency, filters noise, extracts events from natural language |
| **Memory** | SQLite holds runtime state, conversation history, an events buffer, cursors, pending approvals, observability rows |
| **Action** | Calendar writes — gated behind an approval card when the fact was *inferred*, immediate when the user *said* it |
| **Surface** | Telegram (primary), a local web dashboard, a menu bar / tray app, and a voice satellite |

The organizing principle: **deterministic code fetches, parses, and writes
rows; the LLM decides and writes every user-facing sentence.** HTTP requests,
iCal parsing, SQL, and API auth never involve the model. Urgency tagging,
announcement filtering, event extraction, and prose composition never bypass
it.

The current branch (`llm-call-profiles`) adds a **cost-aware LLM call layer** —
two mechanisms that decide how much persona and how many tool schemas each call
actually carries. That work is described in detail in §4.2.

---

## 2. Runtime Topology

Friday is **two processes**, plus a supervisor:

```
┌─ Supervisor ──────────────────────────────────────────────────────┐
│  macOS source checkout:  launchd (com.friday.core + .menubar)     │
│  macOS packaged .app:    mac_app.py  → CoreSupervisor thread      │
│  Windows:                tray.py     → FridayTray._supervise      │
└───────────────────────────────────────────────────────────────────┘
             │ spawns / keeps alive
             ▼
┌─ Core process (friday.py) ────────────────────────────────────────┐
│  ONE asyncio event loop, owned by python-telegram-bot             │
│                                                                    │
│  MessageHandler (text)  → telegram.py::on_message                 │
│      └── asyncio.Semaphore(1)  ← the gate, first line             │
│            ├── read conversation_history from SQLite              │
│            ├── dispatcher.dispatch_detail()  → tool shortlist     │
│            ├── agent/core.py::_think()       → Gemini/Ollama      │
│            └── reply + write history                              │
│                                                                    │
│  MessageHandler (photo/PDF) → on_media → vision extract → gate    │
│  CallbackQueryHandler       → approval card ✅ / ❌               │
│  Dashboard (uvicorn) on 127.0.0.1:5174 — SAME loop, not a thread  │
│  JobQueue: briefings ×2, connector poll, urgent check, cleanup    │
└───────────────────────────────────────────────────────────────────┘

┌─ Voice satellite (voice/listen.py) ───────────────────────────────┐
│  Standalone. Never imports Friday's core.                         │
│  Wake word / clap / push-to-talk → Whisper → Telethon → the bot   │
│  Only coupling: a read-only SQLite query for system_state.status  │
└───────────────────────────────────────────────────────────────────┘
```

Two rules follow from this shape and are load-bearing throughout:

- **PTB's JobQueue is the only scheduler.** No `apscheduler`, no `schedule`, no
  `while True: sleep()`, no timing threads. The dashboard's uvicorn server runs
  as an `asyncio.create_task` on the same loop.
- **All blocking work goes through `run_in_executor`.** LLM calls, calendar
  reads/writes (JXA subprocesses, Google API), location fixes, HTTP fetches —
  every one of them is synchronous by design and wrapped at the call site.

---

## 3. Repository Map

```
/Users/keller/friday/                  ← repo root: packaging + the voice .app
├── README.md                          38 KB user/developer manual
├── wizard.md                          setup-wizard design notes
├── FridayVoice.app/                   compiled Mach-O launcher bundle
├── FridayVoice_launcher.c             its source (see §5)
├── com.friday.agent.plist             legacy hand-written LaunchAgents
├── com.friday.voice.plist               (macos_setup.py now generates these)
├── packaging/{macos,windows}/         PyInstaller specs, build scripts, Inno
├── .github/workflows/                 build-macos.yml, build-windows.yml
├── graphify-out/                      knowledge-graph snapshot of the repo
└── friday/                            ← THE PYTHON PACKAGE
    ├── friday.py                      core entry point (860 lines)
    ├── AGENTS.md                      the persona — a load-bearing API
    ├── quips.yaml                     bundled personality quips (read-only)
    ├── friday_voice.yaml              learned quips (runtime-written)
    ├── friday_config.yaml{,.example}  config (gitignored) + template
    ├── paths.py  compat.py            the two cross-platform seams
    ├── phrases.py  self_edit.py       the slice Friday may rewrite at runtime
    ├── agent/                         core.py profiles.py dispatcher.py
    │                                  tools.py briefings.py
    ├── channels/telegram.py           the semaphore lives here
    ├── memory/                        db.py state.py activity.py + the .db
    ├── connectors/                    canvas groupme weather location
    │                                  gcal_sync apple_calendar
    ├── calendars/                     backend.py apple.py google_cal.py
    ├── actions/calendar.py            auto_write / gated_write
    ├── dashboard/                     server.py + static/{index,app.js,style}
    ├── menubar.py menubar_icon.py     macOS rumps menu bar
    ├── mac_app.py tray.py             packaged supervisors (mac / win)
    ├── macos_setup.py setup_wizard.py LaunchAgent generation, first-run GUI
    ├── tools_verify_dispatch.py       dispatcher observability CLI
    ├── run.sh restart.sh              dev launch scripts
    └── voice/                         the standalone satellite (§5)
```

Roughly 19k lines of first-party Python, of which ~4.4k is the vendored
openWakeWord trainer (`voice/trainer_src/`, third-party, not Friday's code).

---

## 4. The Core Package

### 4.1 Entry Point & Platform Seams

---

#### `friday/friday.py` — 860 lines

**What.** The core entry point. Loads config, validates the environment, opens
SQLite, constructs the agent and the Telegram handler, starts the dashboard,
registers every scheduled job, and hands the process to
`Application.run_polling()`.

**How.** Everything is defined as closures inside `main()` so the job callbacks
capture `conn`, `config`, `agent`, `local_tz`, and the briefing times without
any globals. The structure:

- **`load_config()` / `check_environment()`** — hard-fails on exactly three
  things: `telegram.bot_token`, `telegram.chat_id`, and a Gemini key when
  `provider: gemini`. Every other block is optional; an unconfigured connector
  is skipped silently, not an error.
- **`post_init`** — writes `status`/`started_at`/`provider`/`model` into
  `system_state`, starts the dashboard server as an asyncio task, and sends the
  "Friday online" Telegram message. That send is wrapped in try/except
  deliberately: a bad `chat_id` used to abort `post_init`, killing the core
  seconds after boot and taking the dashboard down with it, so the menu bar saw
  connection-refused on :5174 while the supervisor restart-looped.
- **`morning_briefing_job` / `briefing_job`** — each is guarded twice. A
  **timezone sanity window** (morning must fire 06:00–10:00, evening 19:00–24:00
  local) refuses to send outside it, because a briefing firing at 02:00 is
  almost always a clock/DST bug rather than a real trigger. And an
  **idempotency lock** (`last_{morning,evening}_briefing_sent` = today's date)
  is claimed *before* the slow compose, not after the send — otherwise a compose
  taking >60 s lets the 60-second catch-up net queue a duplicate while the first
  is still running. On send failure or an empty compose the lock is released so
  a retry is still possible.
- **`_check_and_run_missed_briefings`** — the catch-up net. APScheduler drops
  cron jobs whose run time was missed beyond their grace period, and launchd
  `KeepAlive` keeps the *same* process alive across sleep/wake, so the old
  startup-only catch-up never re-ran on wake and briefings were silently lost.
  This helper runs on **every 60-second urgent-alert tick** (not the 15-minute
  poll — the post-wake latency target is ≤60 s and the poll itself gets
  misfire-skipped after long sleeps) and at startup. It fires a still-timely
  missed briefing late, or marks a stale slot as sent so it stops being
  re-evaluated. The docstring carries a four-scenario trace (on-time, 30-min
  late wake, next-morning wake, two polls in a row) proving it can never
  double-send.
- **`process_untagged_events`** — the urgency tagger. For every
  `events.processed = 0` row it builds a source-specific rubric (GroupMe rows
  are judged against their `[priority=...]` tier; Canvas rows against a
  time-to-due rubric) and asks the model for exactly one word. Anything not in
  `{URGENT, SOON, NORMAL}` degrades to `NORMAL`. Runs on the `CLASSIFY` profile.
- **`extract_groupme_events`** — for `[priority=high]` rows not yet examined,
  asks the model whether the message describes a concrete event with **both** a
  clear date and a clear start time. The prompt lists six explicit NONE
  conditions (vague phrasing, past events, banter, open scheduling polls,
  recurring routines the user already knows). Successful extractions go through
  `gated_write` — an approval card, never a silent write. Every row is marked
  `event_extracted = 1` regardless of outcome so the same message is never
  re-billed to the model.
- **`poll_connectors_job`** (900 s) — warms the location cache in an executor,
  then Canvas fetch → Canvas calendar sync → gcal_sync (only on the apple
  backend; mirroring Google→Apple is meaningless when Google *is* the store) →
  GroupMe → urgency tagging → GroupMe event extraction → missed-briefing check.
- **`check_urgent_alerts_job`** (60 s) — finds `urgency = 'URGENT' AND
  notified = 0`, has the LLM compose the interrupt (`COMPOSE` profile, one call
  per new urgent event, not per tick), falls back to deterministic text on model
  failure, sends, flags `notified = 1`, records to `urgent_alerts_sent`.
- **`send_on_demand_briefing`** — composed in-process behind an `asyncio.Lock`
  (the button is easy to double-click and a compose takes tens of seconds).
  Deliberately does **not** call `_record_briefing_sent`, because that would
  claim the slot lock and silently suppress the real scheduled briefing.
- **`_strip_internal_tags`** — removes the `[priority=<tier>]` line the GroupMe
  connector prepends. That tag is an LLM/SQL signal and must stay in
  `events.body`, but it is noise in anything the user reads.

**Why it looks like this.** Three production incidents are visible in the code:
`concurrent_updates(True)` on the Application builder (a hung handler used to
brick the entire bot because PTB awaited updates sequentially — July 9 outage);
the pre-compose lock claim (duplicate briefings); and the Windows branch at the
bottom (`run_polling` must not receive `stop_signals` on Windows, because
Proactor loops have no `add_signal_handler` and PTB would crash at startup).

---

#### `friday/paths.py` — 79 lines

**What.** The single source of truth for where every file lives.

**How.** `data_dir()` returns the package directory on a macOS source checkout,
`%APPDATA%\Friday` on Windows, and `~/.friday` on any other frozen build.
`resource_path()` is the *read-only* counterpart — PyInstaller `_MEIPASS`-aware
— used for `AGENTS.md`, `quips.yaml`, and the dashboard's static files.

**Why.** The Windows install directory (Program Files) is read-only, so mutable
state (config, DB, logs, OAuth token, learned voice) had to be separated from
bundled resources. Writing through `resource_path()` is the bug this module
exists to make impossible.

---

#### `friday/compat.py` — 28 lines

**What.** Two shims and two constants.

`compat.strftime(dt, fmt)` translates glibc's no-pad flag (`%-d`, `%-I`) into
Windows' `%#d`. **Any** format string containing `%-` must go through it — a
raw `dt.strftime("%-I:%M %p")` raises on Windows, and those strings appear
throughout `briefings.py`, `actions/calendar.py`, `tools.py`, and the dashboard.
`listening_flag_path()` resolves `/tmp/friday_listening` on macOS and the system
temp dir elsewhere.

---

### 4.2 The Agent Layer

This is where the current branch's work lives. The problem it solves: the naive
envelope — the full persona plus all ten tool schemas — cost thousands of
tokens on **every** call, and under the SDK's automatic function calling that
envelope is re-sent on **every hop** of the tool loop. One calendar add could
run five figures of input tokens.

Two mechanisms address it from different directions: **profiles** narrow the
persona, **the dispatcher** narrows the tools.

---

#### `friday/agent/core.py` — 713 lines

**What.** The LLM client. No routing, no state, no Telegram references. Owns
persona assembly, the dual-provider `_think()` entry point, and the media→event
vision path.

**How.**

- **Persona assembly** — `_load_persona_base()` reads `AGENTS.md` once (with a
  built-in fallback if it is missing *or blank*, since a blank system
  instruction is as bad as none). `_persona_blocks()` splits it into
  `(heading, text)` pairs and appends five config-composed blocks, each with its
  own `##` heading so one filter table governs both sources:
  `## Mode` (preset), `## Tone Calibration` (snark level),
  `## Approved Phrases` (enabled JARVIS phrases), `## Learned Phrases`
  (`self_edit`-stored, deliberately kept *separate* from Approved Phrases
  because that block closes the list with "do not invent new flourishes outside
  this list" — routing learned phrases through it would shrink Friday's voice to
  whatever the user happened to add), and `## Custom Instructions`.
- **Persona caching** — keyed on `self_edit.version()` (voice-file mtime, config
  mtime, and an in-process revision counter) crossed with the profile and the
  frozen tool selection. Friday can edit its own voice and settings at runtime;
  answering "noted, sir" and then keeping the old voice until restart would be a
  poor experience, and recomposition is a string join over a few KB.
- **`_system_instruction()`** — the persona slice **plus an appended
  wall-clock stamp**, plus a location block on CHAT/COMPOSE. Appended rather
  than prepended so the persona stays a stable, cacheable prefix; a volatile
  first line would defeat prefix caching. The time block ships on *every*
  profile including CLASSIFY, which looks like an exception to "CLASSIFY carries
  no persona" but is not — the urgency rubric is entirely relative ("URGENT =
  due within 24h"), and with no clock the tagger silently grades deadlines
  against a date out of the training set.
- **`_location_block()`** — reads `connectors.location.cached()` only, never
  fetches (a cold lookup blocks for seconds and would be paid on the hot path
  while a handler holds the semaphore). It carries the coordinates *and* a
  mandatory caveat that this is the **Mac's** position, not the user's — a bare
  "Current location: X" in a system prompt reads as the user's whereabouts,
  which is the exact claim `connectors/location.py` forbids.
- **`_gemini_generate_with_retry()`** — retries transient 500/503/504/429 with a
  1 s, 2 s backoff. Transport errors (`httpx.TransportError` — the
  sleep-killed-socket family) get at most **one** retry, because each attempt
  can burn the full 60 s client timeout and the worst case must stay under the
  handler's 150 s ceiling.
- **`_think()`** — the one synchronous LLM entry point, always called via
  `run_in_executor`. Parameters worth knowing:
  - `profile` defaults to `CHAT` when tools are on and `COMPOSE` when off.
    `CLASSIFY` must be asked for explicitly, because dropping the voice is
    visible in the output and should never happen by inference.
  - `tool_names=None` means attach everything (pre-dispatcher behavior). An
    **empty list is a real decision**, not a missing one: it attaches no tools
    and narrows the persona to match.
  - `images` attaches a multimodal turn (images before text, per Google's
    guidance); history is not supported on that path.
  - `response_json` asks for `application/json` and skips tools — JSON mode and
    function calling don't mix.
  - A backtick-only response is blanked to `""`. Gemma sometimes emits an empty
    Markdown fence as its post-tool reply instead of empty text, which renders
    as six literal backticks in Telegram.
- **`on_media()`** — photo or PDF → one calendar event. PDFs are rasterized to
  PNG at 150 dpi, **capped at 3 pages** (flyers front-load their content and
  each page adds latency and payload). The prompt hard-requires resolving all
  relative dates against an injected current date and never inventing one.
  `_parse_media_event()` normalizes the result into the `gated_write` event
  shape, treating a date-only start as an all-day event and rejecting end times
  that aren't same-day-and-after.

**Why.** The `_GEMINI_HTTP_TIMEOUT_MS = 60_000` constant is explicitly
load-bearing: `genai.Client` has no default HTTP timeout, so a request in flight
when the Mac sleeps blocked its executor thread forever while a handler held the
semaphore — the July 9 outage, where the process looked alive and every Telegram
message vanished.

---

#### `friday/agent/profiles.py` — 175 lines

**What.** The table that decides which slice of the persona each kind of call
carries. Sections are addressed by their Markdown heading, which is what makes
`AGENTS.md` an API and not just prose.

**The four profiles:**

| Profile | Used by | Carries |
|---|---|---|
| **CHAT** | The user talking to Friday | Everything. Voice is the product and tools are live. |
| **COMPOSE** | Briefings, urgent alerts | Voice sections, but not the four tool-usage sections — those calls run with tools off by construction. |
| **CLASSIFY** | Urgency tagging, GroupMe extraction, media extraction, quip selection | `CLASSIFY_INSTRUCTION` and **no persona at all**. |
| **ROUTE** | The dispatcher itself | Deliberately mapped to no section anywhere, so it cannot pick up persona prose — including learned phrases — even by accident. |

**Why CLASSIFY carries nothing.** On a call whose contract is to return exactly
`URGENT`, butler voice is not merely wasted spend — it is a parse failure and a
silently mis-tagged event. `CLASSIFY_INSTRUCTION` is deliberately non-empty
("You are a classification component inside a larger system…") because Gemma
with no system instruction at all drifts into conversational framing ("Sure! The
urgency here would be…") and every caller on that path parses the first token.

**`_TOOL_COUPLED`** narrows further: four sections ship only when the tool they
describe is actually attached. Note the comment on `"editing vs. adding"` —
despite the name it is entirely about `add_calendar_event` vs.
`update_calendar_event`, so it is coupled to the calendar tools, not the
self-edit ones. Coupling it wrongly would drop the duplicate-event guidance from
exactly the turns that need it.

**`_DEFAULT` fails OPEN.** An unrecognized heading lands in CHAT+COMPOSE and
logs once. `AGENTS.md` is user-editable prose and Friday is an always-on daemon:
an unmapped heading must never silently vanish from the persona, and must never
be a hard startup failure.

---

#### `friday/agent/dispatcher.py` — 357 lines

**What.** A cheap call against a small model (`models/gemini-2.5-flash-lite` by
default, 5-second budget) that returns a shortlist of tool names; the real call
then attaches only those schemas.

**How.**

- **`TOOL_MANIFEST`** is a hand-maintained one-line-per-tool table, under 12
  words each. This is the entire point: it is the *cheap* description, not the
  schema. The docstrings are ~2,700 tokens; the manifest is ~210. Deriving the
  manifest from the docstrings would reintroduce exactly the cost the dispatcher
  exists to avoid.
- **`assert_manifest_matches_tools()`** raises `ManifestMismatch` at startup on
  drift in **either** direction. A tool missing from the manifest is invisible
  to the dispatcher and silently stops working while everything still looks
  healthy. A manifest key with no tool behind it lands in the response schema's
  enum, so the model can name something that cannot be called.
- **Gemini path** constrains output with a `response_schema` whose `items.enum`
  is the exact tool-name list — the model is *structurally* unable to name a
  tool that doesn't exist. The Ollama path sends the same schema in `format` and
  validates defensively anyway, because even a schema-constrained local model is
  a weaker guarantee.
- **`_with_timeout`** enforces the budget client-side on a two-worker thread
  pool. The Gemini API refuses a client deadline under 10 s outright ("Manually
  set deadline 5s is too short"), so the transport deadline is floored at 10 s
  and the real budget is enforced here — which also makes `timeout_s` mean the
  same thing on both providers. A thread that outlives its timeout is abandoned,
  not killed.
- **`_is_timeout()`** matches on **exception type only**. Matching on message
  text looked tempting and was wrong: Gemini's 400 for a too-short deadline
  contains the word "deadline", so a plain config error was being logged and
  counted as a timeout.

**The failure contract — the most important thing in the file.** Every failure
mode (unparseable output, hallucinated name, timeout, dead provider, provider
error) falls back to the **FULL tool list, never to `[]`**. An empty list from a
garbled response is indistinguishable from a genuine "no tools needed", and
guessing wrong means Friday silently drops an action the user asked for.

`_parse()` encodes the one subtlety: a model returning a genuine empty array
*is* honored (nothing was dropped, so `[]` is a real decision), but a model
returning names of which **none** survive validation is **not** — that is a
garbled response wearing an empty list's clothes (`DROPPED_INVALID` → fail open).

Every decision, including failures, is written to `dispatch_log` with outcome,
latency, tokens, and the raw response.

---

#### `friday/agent/tools.py` — 555 lines

**What.** The ten synchronous functions Gemini can invoke. The google-genai SDK
owns the call loop when these are passed as `tools=[...]`.

**How.** `make_tools(conn, config, agent)` returns closures bound to this
Friday instance, each wrapped by `_instrument()` — a `functools.wraps` decorator
that writes a `tool_calls` row (name, bound args, result preview, duration,
`triggered_by`) on every invocation. `functools.wraps` matters: it preserves
`__name__`/`__doc__`/`__annotations__`/`__wrapped__`, so google-genai's
signature- and annotation-based schema inference is unaffected, and the
dispatcher's manifest check can read the real tool name off the wrapper.

Instrumentation is best-effort and never alters the return value.

**The full list is in [§7.1](#71-the-complete-tool-list).**

**Two deliberate absences,** both documented in the file as comments where the
tools used to be:

- **No `get_now`.** The current time is injected into every system instruction.
  A tool the model has to remember to call is a tool it can skip, and skipping
  the clock is how "this Friday" resolved to a date out of the training set. The
  injection costs ~25 tokens against ~2,900 for the schema entry.
- **No `get_location`.** Same reasoning, plus a worse failure: keeping the tool
  let the model fetch a live fix that *disagreed* with the cache-warmed line
  already in its prompt.

**The silent-confirmation contract.** `add_calendar_event` and
`update_calendar_event` send the user their confirmation *themselves* (via
`actions/calendar.py`), then return a dict containing
`"next_action": "Do not produce any further output for this turn."` and set
`agent._last_action_emitted = "calendar_added"` plus
`_last_calendar_confirmation` (the exact text that shipped). The Telegram layer
reads those flags to suppress a duplicate reply and to log the real text into
`conversation_history` — past history rows shape the model's future output, so
the row must match what the user actually saw.

**`_quip_for()`** runs a tiny `CLASSIFY` call that returns an *index* into the
numbered quip list, not a written line. The quips already carry the personality;
the model's job here is selection.

---

#### `friday/agent/briefings.py` — 653 lines

**What.** Prompt composers plus deterministic context bundling for the three
briefing shapes and the urgent alert.

**How.** The central idea: **a briefing needs a known-complete dataset.** Rather
than leave the model to decide whether to call tools,
`bundle_briefing_context()` pre-fetches everything (calendar, Canvas, weather,
briefing-visible GroupMe) by reusing the same underlying functions the tools
wrap, and injects it as a delimited `===== BRIEFING CONTEXT =====` block at the
top of the prompt. The call then runs with `use_tools=False` — a hard guarantee
of zero tool calls.

The consequence is diagnostic: **a weak briefing is always a bundle problem,
never a "did the model drill into a tool" rabbit hole.** If a briefing turns up
thin, expand the bundler; never re-enable tools on that path.

Windows and slots:

| Slot | Calendar | Weather |
|---|---|---|
| `morning` | today, plus the next 3 days excluding today | today |
| `evening` | tomorrow, plus 5 days from tomorrow | tomorrow |
| `on_demand` | **one 7-day read sliced three ways** — today / tomorrow / rest of week | today |

That single sliced read is deliberate: the user is waiting on the on-demand
briefing (it's the dashboard's Brief button), and a calendar read is the slowest
thing in the bundle by a wide margin — on the JXA fallback each one can take a
minute or more.

Every fetch is individually guarded and yields an `_UNAVAILABLE` sentinel on
failure, which the formatter renders verbatim. A single failed source produces a
visibly thin briefing, never an aborted one. `_log_bundle_summary()` writes one
INFO line per bundle (counts + weather state) so a future "weak briefing"
complaint can be diagnosed from the log alone.

**`_local_now()`** exists because `date.today()` reads the *host's* timezone,
which on the always-on Mac can disagree with the configured `America/Chicago`
and produce an off-by-one weekday label even while the date-keyed calendar
fetches stay correct.

**`_is_all_day()`** derives the flag neither backend reports: Google all-day
events carry a date-only value (no `T`); Apple all-day events arrive as a
midnight→midnight datetime spanning whole days.

**Urgent alerts** get the same treatment — the LLM writes them, because the raw
material is scaffolding meant for the model (a GroupMe body carries
`Group:`/`From:` lines the title already repeats) and reads as a machine dump if
forwarded verbatim. The prompt includes a worked example. `fallback_urgent_alert()`
is the deterministic version for when the model is unavailable — plainer, but
never an emoji-and-header dump, because an LLM outage must not change what an
alert *looks like* more than it has to.

---

### 4.3 The Channel Layer

---

#### `friday/channels/telegram.py` — 362 lines

**What.** Inbound async PTB handlers and outbound synchronous send helpers.

**The semaphore.** `asyncio.Semaphore(1)` at module level, acquired as the
**first statement** of `on_message` and `on_media` — before the text is read,
before any SQLite query, before context assembly. Messages queue in arrival
order. `concurrent_updates(True)` on the Application lets callback taps and new
updates *start* processing while a slow handler runs, but LLM work stays
serialized here, so it never produces concurrent Gemini calls from user
messages.

**`_EXECUTOR_TIMEOUT_S = 150`** ceilings any single executor call made while
holding the semaphore — slightly above the Gemini worst case (60 s client
timeout × capped retries ≈ 123 s). A hung blocking call fails loudly and
releases the pipeline instead of wedging it forever. `wait_for` cannot kill the
executor thread; the call may still finish in the background, but the pipeline
is free.

**The `on_message` flow:**

1. Acquire semaphore → pause gate (`_pause_active`, including timed auto-resume
   via `paused_until`) → record `last_message_at`/`preview`.
2. Read the rolling conversation window (`memory.short_term_turns × 2` rows).
3. Dispatcher call in an executor, result recorded to `dispatch_log`.
4. `_think()` in an executor with the selected tool names.
5. **Dispatcher-miss recovery** (see below).
6. Write the user turn, decide what to send, write the assistant turn.

**Dispatcher-miss recovery** is a narrow, deliberate retry. When the dispatcher
returned `[]`, the model answered with plain text, and no tool action was
emitted, the message is retried **once** with all tools attached — the risky
case is "I play tennis tonight" answered with small talk instead of a calendar
entry. A wrong *non-empty* set is explicitly not covered: the model had tools
and chose not to use them, which is a legitimate answer. The retry is flagged on
the `dispatch_log` row via `mark_dispatch_fallback`.

**`_is_silent_residue()`** handles the model failing to say nothing. A tool that
already messaged the user tells the model to stay quiet, but Gemma doesn't
reliably return empty text. Two observed shapes: an empty Markdown fence
(blanked in `core.py`) and a lone stray CJK token — `避`, sent to the user on
Aug 2 after a calendar add. Neither is English Friday would ever send, so
anything holding no ASCII letter or digit counts as silence.

**`on_media`** downloads photos (largest size) or PDF documents and hands them
to `agent.on_media` in an executor. The error message names the real cause:
Telegram bots can only download files ≤ 20 MB.

**`on_callback`** handles the approval-card taps. Only `confirm` and `cancel`
are wired; `edit` is reserved for Phase 5. Stale callbacks (unknown pending key)
are silently discarded.

---

### 4.4 The Memory Layer

---

#### `friday/memory/db.py` — 182 lines

**What.** The connection and the schema. Two families of table — operational,
and activity capture. Full schema in [§7.3](#73-sqlite-schema).

**How.** `executescript(_SCHEMA)` is all `CREATE TABLE IF NOT EXISTS`, followed
by `_migrate()` for columns added after the fact. The migrations are notable for
their **backfills**:

- `events.calendar_synced` backfills existing Canvas rows to `1`, so the new
  auto-sync doesn't retroactively dump historical assignments into the calendar.
- `events.event_extracted` backfills **all** rows to `1`, so the new extraction
  pass doesn't retroactively scan (and bill for) every historical GroupMe
  message.

`check_same_thread=False` because the connection is shared across executor
threads.

---

#### `friday/memory/state.py` — 38 lines

Four helpers over `system_state` (`get`/`set`/`delete`/`set_many`). This table
replaces `state.json` entirely — one of the project's hard rules.

---

#### `friday/memory/activity.py` — 182 lines

**What.** Best-effort recorders for the five observability tables, plus the
nightly trim.

**Why the design constraint matters.** Every function here is instrumentation
and **must never raise into the caller's hot path**. Each wraps its own
try/except, logs at DEBUG, and moves on. A dropped activity row is always
preferable to a broken briefing, tool call, or chat turn.

Two details:

- `record_dispatch()` returns the row id so the caller can flag it later if the
  miss-recovery retry fires.
- `tokens_in`/`tokens_out` pass through as `None`, never coerced to `0`, on a
  failed dispatcher call — a failed call genuinely has no usage metadata, and
  writing `0` would average in as a free call.

`cleanup_old_activity()` trims four tables to 30 days (`dispatch_log` is not in
that list — see §9).

---

### 4.5 Connectors (Read Side)

All connectors are **read-only, LLM-free, and failure-tolerant**. Every one
returns 0 / `[]` / `""` rather than raising into the poll job.

---

#### `friday/connectors/canvas.py` — 167 lines

**What.** Canvas LMS via the **iCal feed** — never HTML scraping.

`fetch()` pulls the feed (with an optional bearer token; the URL works without
one, but with it a rejected fetch reports 401/403 instead of looking like an
empty week), parses with `icalendar`, and writes new `VEVENT`s to `events`
deduplicated on `canvas_<UID>`. iCal feeds return the full calendar every poll,
so dedup is mandatory.

`sync_to_calendar()` writes unsynced Canvas due dates with a **future** due time
to a `Canvas` calendar via `auto_write` (silent — no Telegram confirmation).
This is an ungated write because the user's school published these facts; the
user didn't infer them. Idempotency is `events.calendar_synced`. An unparseable
`due_at` is marked synced anyway so it isn't retried forever; a failed write
leaves the flag at 0 so the next 15-minute poll retries.

---

#### `friday/connectors/groupme.py` — 311 lines

**What.** Per-group message polling with priority tiers.

**The tier vocabulary is defined here** and is the single source of truth for
the whole app:

| Tier | Behavior |
|---|---|
| `high` | LLM urgency pass + event extraction + briefings — **can interrupt** |
| `normal` | Briefings only, never interrupts |
| `muted` | Ingested for history, never surfaced |

`low` is the pre-dashboard spelling and normalizes to `muted`, because that is
what it always did in practice — nothing downstream ever queried a non-`high`
tag. `normalize_priority()` is imported by `dashboard/server.py` so the reader
and the writer cannot drift.

**Body layout** — every stored message gets a fixed shape that downstream
parsers depend on:

```
[priority=high]
Group: <name>
From: <sender>

<text…>
```

**No-backfill first poll.** A group with no cursor probes the single newest
message, saves its id as the cursor, and writes nothing. Otherwise enabling a
group would replay its entire history through the urgency tagger.

**Disabling a group clears its cursor** (`_forget_cursor`), so re-enabling
resumes from the newest message rather than replaying the whole gap. The
`rowcount` check means this logs and commits once per disable rather than every
15 minutes.

Name-only config entries are resolved lazily against `GET /groups`, at most one
call per poll cycle, with duplicate names logged and first-wins.

---

#### `friday/connectors/weather.py` — 153 lines

**What.** Stateless OpenWeatherMap fetch with crude intent parsing.

`respond(cfg, query)` classifies the query as `rain` / `temp` / `general` by
keyword, and for rain intent parses a time expression ("at 3pm", "tonight",
"this afternoon", "tomorrow") to pick the right 3-hour forecast slot. Returns a
natural-language sentence with a `(OpenWeatherMap)` source tag — Friday's
sourcing rule applies even here. Returns `""` on any failure, which the bundler
renders as `unavailable`.

---

#### `friday/connectors/location.py` — 354 lines

**What.** Where the *machine* is. Read the name carefully — this is not where
the user is, and the module goes out of its way to forbid that claim.

**Two backends, both lazy-imported so a failure degrades instead of crashing:**

1. **CoreLocation** (PyObjC) — Wi-Fi positioning, tens of metres. Subject to the
   per-binary TCC rule: under the LaunchAgent, `friday.py` is a bare interpreter
   with no Info.plist, so `NSLocationWhenInUseUsageDescription` is absent and
   authorization resolves to denied **without ever prompting**. Only the
   packaged .app gets the dialog.
2. **IP geolocation** — city-level, keyless, works on Windows. **Not optional**:
   it is the only path that answers under the LaunchAgent. Two providers are
   tried in order because these services throttle by source IP without warning
   (`ipapi.co` returned 429 on the very first request from this network and was
   dropped for it).

**Accuracy refinement.** At `kCLLocationAccuracyBest` CoreLocation delivers a
coarse fix immediately and refines it over the next few seconds. Taking the
first would report a ~1 km Wi-Fi estimate as final, so the delegate keeps the
tightest fix seen and stops early only once it is within 65 m — a floor chosen
because Mac Wi-Fi positioning tops out around 10–65 m anyway. `_reverse_geocode`
only claims a *street address* when the fix is that tight; a wrong street is
worse than a right city.

**The PyObjC trap, twice.** Both the delegate methods and the CLGeocoder
completion handler must return `None`. PyObjC type-checks the block signature
against the ObjC `void` return and raises inside the callback thread otherwise
— an uncaught NSException that aborts the whole process, not something the
calling code can catch. A bare `lambda g, e: done.setdefault(...)` returns the
dict value and does exactly that.

**The public API split** is the important part:

- `fetch()` — may block ~25 s worst case. Cached 5 minutes.
- `cached()` — read-only, no I/O. This is what `_system_instruction` calls.
- `warm()` — called from the poll job in an executor.

Before the first warm, `cached()` returns `None` and the prompt simply carries
no location block, which is the honest state.

---

#### `friday/connectors/gcal_sync.py` — 121 lines

**What.** Mirrors Google Calendar iCal subscriptions into same-named Apple
Calendars. **One-directional: Google → Apple only.**

Dedup is the `synced_events` table keyed on the iCal `UID` Google always
provides. No approval gate — these are events the user already created
themselves. A failing URL is logged and skipped, never crashing the poll.
Deletion sync and update sync are explicitly out of scope; a changed or removed
Google event is logged, not mirrored.

Apple Calendars must exist first — Friday never creates them on the apple
backend. The whole connector is skipped on the google backend, where Google
already *is* the event store.

The secret iCal URLs need no OAuth and no API key — just the URL, **which makes
the URL a credential**.

---

#### `friday/connectors/apple_calendar.py` — 357 lines

**What.** The Apple Calendar **reader**. Two backends, tried in order.

**This is the single most performance-critical file in the repo**, and the
docstring explains why with measurements:

Every event property read over the Apple Events bridge is its own IPC round
trip, so JXA costs roughly **35 ms per event *in the calendar being scanned*** —
not per event returned. A shared "Family" calendar with 2,600 events took **55
seconds** to answer "what is on today". A full briefing bundle took over **six
minutes** and hit every timeout, so briefings reported an empty day. EventKit
answers the same query in **~3 ms** because it reads the local store instead of
talking to Calendar.app. Bulk property fetches (`cal.events.startDate()`) are
*not* a workaround — measured slower still.

**EventKit path.** Two kinds of "no", handled differently: a missing framework
is permanent and latches (`_ek_missing`); a refused *permission* does not,
because the user can grant access in System Settings at any time and a
long-lived core process must pick that up without a restart. So the instant,
non-prompting status check runs on every call while the prompt itself is issued
once per process.

Authorization status `3` is both `Authorized` and, on macOS 14+, `FullAccess`.
Status `4` is **write-only** — it reports as granted and then returns zero
events, so anything other than `3` goes through a request first, preferring
`requestFullAccessToEventsWithCompletion_` where available. The completion
handler must return `None` (same PyObjC trap as location). The run loop is
pumped with a 30-second deadline so a prompt the user never answers can't wedge
a briefing.

`calendarItemExternalIdentifier` is used as the `uid`, not
`calendarItemIdentifier` — the former is the iCal UID the JXA write path can
look an event up by; the latter is an EventKit-store-local handle in a different
namespace, so returning it would silently break `update_calendar_event` the
moment full access is granted and reads switch to EventKit.

**JXA fallback.** One `osascript` invocation **per calendar**, not one for all.
This is not stylistic: `osascript` enforces its own 120-second Apple Event
timeout, independent of and shorter than the subprocess timeout, and a single
script covering several busy calendars blows through it and dies with
"AppleEvent timed out (-1712)", losing the work already done. One calendar per
invocation gives each its own budget, so a slow calendar can no longer poison
the ones after it. Duplicate calendar names are deduplicated with
`dict.fromkeys` because `whose({name: …})` already returns every calendar with
that name — visiting the name twice would report every event twice.

The subprocess timeout is a generous 150 s and applies to the fallback only:
a slow answer still beats an empty briefing.

---

### 4.6 Calendars (Write Side + Backend Dispatch)

---

#### `friday/calendars/backend.py` — 75 lines

**What.** The one place that decides whether the event store is Apple Calendar
or Google Calendar.

Selection is `config.calendar.backend` (`apple` | `google`), defaulting to
google on `win32` and apple everywhere else. `init(config)` is called once from
`friday.py` so the write path — reached from `actions/calendar.py` without a
config in hand — knows the config. Reads take `cfg` explicitly, matching the old
`apple_calendar` signatures.

Six functions cross the seam: `events_in_window`, `events_for_day`,
`write_event`, `update_event`, `calendar_exists`, `backend_name`.

---

#### `friday/calendars/apple.py` — 244 lines

**What.** Apple Calendar **writes**, JXA only, plus calendar enumeration for the
setup wizard. Reads are re-exported from `connectors/apple_calendar.py`.

**Why writes stay on JXA** while reads went to EventKit: writes are one event at
a time, so the per-round-trip cost never accumulates. Do not port them to
EventKit for symmetry.

**`update_event()`** is the expensive one. `whose()` can't use an index, so it
walks every event in the calendar — measured on the ~2.6k-event calendar,
matching on uid costs ~12.8 s against ~0.3 s for the property write itself.
Pre-filtering on a `startDate` window was tried and is *slower* (~14.1 s):
evaluating two date comparisons per event loses to one string equality. Hence
the longer 45 s timeout, and hence `calendar_name` as a **hint, not a filter** —
on a miss the script falls back to scanning every calendar rather than reporting
a spurious not-found, since the reader's `calendar` field can lag an event that
was moved.

One JXA ordering detail with a comment: `startDate` must be set **before**
`endDate`, because Calendar.app clamps an `endDate` that lands before the
event's current start, silently dropping the move.

`list_calendars_detailed()` returns `(name, writable)` pairs in one round trip
for the setup wizard. Writability matters: subscribed calendars (holidays, a
read-only shared feed) show up alongside real ones, and picking one as the
default calendar makes every later write fail.

---

#### `friday/calendars/google_cal.py` — 348 lines

**What.** The Google Calendar API backend, used on Windows.

Auth is the OAuth installed-app flow: the **setup wizard** performs the initial
browser consent and saves the token; this module only loads and refreshes it. If
the token is missing or unrefreshable, reads return `[]` and writes return
`None` — Friday never pops a browser from a background process.

The calendar-name → id map is cached for 10 minutes. `_ensure_calendar()`
auto-creates missing calendars by default on this backend (secondary Google
calendars are cheap and it keeps Canvas due dates organized without a manual
step) — the opposite of the apple backend's policy.

Reads filter to `agent.briefing_calendars` when set, otherwise every calendar
except holiday/contacts/weeknum feeds. The API's `events().list` window is
half-open on event *end* time, so a second filter narrows to events that
actually **start** inside the window, matching the Apple reader's contract.

`update_event()` uses `patch()`, so unspecified fields are left alone
server-side rather than cleared. Because a Google event id is only unique within
its own calendar's namespace, a missing `calendar_name` means trying every
readable calendar in turn; a 404 just means "not in this one, keep looking".

---

### 4.7 Actions (The Approval Gate)

---

#### `friday/actions/calendar.py` — 437 lines

**What.** The write API both backends sit behind, and the home of the gating
decision.

**The split is about who asserted the fact, not how important it is:**

| Path | Gate | Used by |
|---|---|---|
| **`auto_write`** | None. One-line confirmation + quip. | Canvas due dates (the user's school published them) and `add_calendar_event` (the user just said it out loud — a confirmation card for something they explicitly asked for is friction, not safety) |
| **`auto_update`** | None. One-line confirmation naming what changed. | `update_calendar_event` |
| **`gated_write`** | Approval card staged in `pending_actions`. | Anything Friday **inferred**: GroupMe extractions, and events pulled from a photo or PDF |

`confirm_pending()` performs the write when ✅ is tapped; `cancel_pending()`
marks it cancelled and tells the user. The dashboard can resolve the same rows
through the same functions.

**Supporting machinery:**

- **`_normalize_title()`** is the backstop for the LLM's Title Case rule. If a
  title is entirely lower- or entirely uppercase it is Title Cased with a
  stopword list; **mixed-case titles are left alone** so deliberate casing like
  `iPhone` or `FBLA` survives.
- **`_resolve_times()`** derives `(start, end, all_day)`. No `start_time` means
  all-day (midnight → midnight + 1 day). `start_time` alone defaults to a
  1-hour duration. Overnight ranges (`end <= start`) are rejected.
- **`_resolve_calendar()`** falls back to the configured default when the
  requested calendar doesn't exist. macOS exposes no scriptable "default
  calendar" concept, which is why this has to be explicit in config.
  `gated_write` **pins** the resolved calendar into the stored payload so
  `confirm_pending` writes to the same place the card promised.
- **`format_confirmation()` / `format_update_confirmation()`** are public
  precisely so callers can log the exact text the user saw into
  `conversation_history` without rebuilding it and risking drift.
  `_confirmation_date` renders "today" / "tomorrow" / "Thursday, June 18".

---

### 4.8 Self-Editing & Voice Files

---

#### `friday/self_edit.py` — 466 lines

**What.** The narrow slice of itself Friday is allowed to change at runtime.
Two stores, both plain YAML:

- **`friday_voice.yaml`** (in `paths.data_dir()`) — `confirm_quips`,
  `voice_phrases`, `disabled_quips`.
- **`friday_config.yaml`** — a **whitelist** of six settings, and nothing else.

**The hard boundary.** Friday does **not** edit its own Python source. That is
deliberate: the core runs always-on under launchd or `tray.py`, both of which
relaunch it on exit, so a syntax error would become a silent restart loop rather
than a visible failure.

Nothing here writes through `paths.resource_path()` — that resolves into the
read-only PyInstaller bundle on frozen builds, which is exactly why learned
quips live in their own file instead of being appended to the bundled
`quips.yaml`.

**Phrase handling:**

- `_clean()` collapses whitespace, strips surrounding quotes (users and voice
  transcription both add them; `quip_prompt` quotes it again), and **rejects
  emoji and markdown at the door** — both are banned in Friday's output, so a
  phrase containing them can never be used as written.
- `add_phrase()` re-enables a previously retired bundled quip rather than
  stashing a second learned copy. Caps at 60 phrases, 200 chars each.
- `remove_phrase()` matches case-insensitively on substrings, so the user can
  say "the touch grass one". **Multiple matches return candidates instead of
  guessing** — deleting the wrong phrase is not something the user can see
  happening. An exact match is never treated as ambiguous. A *bundled* quip is
  retired by name into `disabled_quips` rather than deleted, since `quips.yaml`
  is read-only in a frozen build.
- `_save()` is an atomic tmp-file-then-move, so a crash mid-write can never
  leave a truncated voice file. `_read_raw()` preserves keys the module doesn't
  own, so a hand-edited file round-trips without losing anything.

**`version()`** returns `(voice mtime, config mtime, in-process revision)`. The
revision counter is paired with the mtimes because mtime resolution isn't
guaranteed finer than a second on every filesystem — two edits inside one tick
would otherwise look like no edit at all, and the persona would keep serving the
stale voice.

**Settings whitelist** (`_SETTINGS`) — a whitelist, not a denylist: a key Friday
has never heard of is refused.

| Key | Validator |
|---|---|
| `persona.snark_level` | `none` / `medium` / `maximum` |
| `persona.preset` | `professional` / `butler` / `friday` |
| `persona.custom_instructions` | free text, ≤ 500 chars |
| `agent.default_calendar` | free text, ≤ 120 chars |
| `agent.morning_briefing_time` | 24-hour `HH:MM` |
| `agent.briefing_time` | 24-hour `HH:MM` |

Never to be added, per the module comment: API keys, bot tokens, chat_id,
provider, model names, any path, db_path, iCal URLs.

`update_setting()` mutates `live_config` **in place** rather than replacing it,
because `agent._config` and the `make_tools` closure hold the same object — an
in-place update is what makes the change visible without a restart.

**`sync_briefing_times()`** lives here rather than in either writer because both
the dashboard and `update_setting` write briefing times; a mirror maintained by
only one of them goes stale the moment the other is used.

---

#### `friday/phrases.py` — 75 lines

**What.** The quip palette. `bundled_quips()` reads the read-only
`quips.yaml`; `_load_quips()` unions it with `self_edit`'s learned phrases minus
the retired ones, **re-read from disk on every call** so a quip added over
Telegram is in play on the very next action with no restart.

`quip_prompt(context)` returns a numbered list plus a prompt that names the
failure mode explicitly: *"Avoid contradictions — a 'touch grass' quip is wrong
for an outdoor event, a 'sleep schedule' quip is wrong for a daytime event, an
'academic' quip is wrong for an errand."* `pick_quip()` resolves the model's
integer, falling back to random on a parse failure.

---

#### `friday/AGENTS.md` — the persona

**Two things make this file load-bearing beyond prose:**

1. **Its headings are an API.** `profiles._MEMBERSHIP` keys off them. Renaming a
   heading without updating the table silently reroutes that section (fail-open
   to CHAT+COMPOSE, logged once).
2. It declares its own precedence: *"If anything in the Voice section below ever
   conflicts with a rule here, the rule wins."*

Sections: `Operational Rules` (Tone and Address, Sourcing, Urgency Policy,
Scope), `Calendar Writes`, `Editing vs. Adding`, `Calendar Title Hygiene`,
`Self-Editing`, `Voice`, and `Where the voice does NOT apply` — the last of
which bans the butler wit from permission cards, briefings, error messages, and
anything TTS will read aloud.

`Soul.md` was the predecessor. It was merged into `AGENTS.md` and has since been
deleted from the tree.

---

### 4.9 The Dashboard

---

#### `friday/dashboard/server.py` — 1,018 lines

**What.** A FastAPI app on `127.0.0.1:5174`, hosted **inside** friday.py's
asyncio loop. Never exposed to the network, so no auth.

All endpoints touch the **same** SQLite connection and the **same** config file
the running agent uses. Full endpoint list in [§7.4](#74-dashboard-http-api).

**Notable mechanics:**

- **Secret masking.** `_SECRET_PATHS` are dot-paths into the config. `GET
  /api/config` masks them to `********<last4>` unless `?reveal=1`. `POST
  /api/config` **splices the real values back in** from disk when the incoming
  payload still carries a mask, so a save from a masked view can never overwrite
  a real secret with the mask string.
- **Atomic config writes** — tmp file in the same directory, then `shutil.move`.
- **`_migrate_config`** lazily adds the `persona` and `notifications` blocks and
  normalizes GroupMe group entries, importing `normalize_priority` from the
  connector so the vocabulary can't drift.
- **`_NoCacheStatic`** disables conditional/304 caching. Localhost dashboard, no
  bandwidth concern, and it prevents stale-asset blank pages (a cached
  `index.html` paired with new routing, or vice versa).
- **`/api/today`** is one bundle powering the whole Today tab, polled every 5
  seconds: status, next briefing, pending count, a unified chronological
  activity feed (briefings + tool calls + urgent alerts + conversation turns +
  freshly ingested events, newest-first, capped at 100), a forward-looking
  "what's next", and per-day LLM stats with a free-tier quota estimate from a
  hardcoded `_GEMINI_TIERS` table (the models endpoint doesn't return quotas).
- **`/api/llm/last`** returns the most recent exchange in full plus the tool
  calls that ran inside it, correlated by timestamp window (tool rows written
  after the previous exchange and before this one). Fetched on demand when the
  developer panel expands — not part of the 5-second poll.
- **`/api/friday/restart`** has real subtlety. On Windows it raises SIGINT
  in-process on a 0.3 s timer and lets the tray bring the core back. On macOS it
  **probes** whether the `com.friday.agent` LaunchAgent is actually installed
  before using `launchctl kickstart` — a kickstart against a label launchd has
  never heard of fails silently, and `Popen` only reports that launchctl
  *started*, so the endpoint used to answer `ok: true` while nothing restarted.
  When there's no LaunchAgent (the packaged .app case), it falls through to the
  same self-restart path. `start_new_session=True` so kickstart tearing down the
  job doesn't also kill the launchctl doing the tearing.
- **`/api/friday/brief`** carries a long comment about the bug it fixed. This
  used to POST the text "brief me" to `sendMessage` on the assumption it would
  come back around as a user message. **It does not** — Telegram never delivers
  a bot's own messages to that bot through `getUpdates`, so `on_message` never
  fired, no briefing was ever composed, and the button reported `ok: true` while
  doing nothing. It now awaits the real coroutine on the shared loop.
- **`/api/pending-approvals/{id}/{verb}`** runs confirm/cancel through the
  **same** `actions/calendar.py` pipeline Telegram uses, constructing a
  lightweight send-only `TelegramHandler` so the user still sees the
  confirmation they'd have gotten from tapping the inline button. `edit` is
  implemented here (unlike in Telegram) as a payload rewrite.

#### `friday/dashboard/static/` — `index.html` (450) · `app.js` (1,190) · `style.css` (1,175)

A dependency-free vanilla-JS SPA with `<template>`-based pages and a hash
router. Seven pages: **Today**, **AI Model**, **Persona**, **Integrations**,
**Calendar**, **Notifications**, **Voice**, plus **About**. `bindInput` binds
form controls to dot-paths in the config object; `saveConfig` POSTs the whole
thing back. The Today page polls `/api/today` every 5 s.

---

### 4.10 Platform Front-Ends

---

#### `friday/menubar.py` — 419 lines

**What.** The macOS rumps menu bar app. Standalone — never imported by
`friday.py`, and it talks to the core only over the dashboard's HTTP API.

**Two timers:** a 10-second tick that pulls `/api/status` and `/api/voice/status`
and rewrites the menu, and a **1-second tick that does nothing but `stat()` the
listening flag** and maybe swap the icon — cheap, and it gives near-instant
visual feedback when wake fires.

**Menu:** a non-clickable status header (`Online · last msg 14:32 · 41 calls ·
88,102/9,431 tok`), Brief Me Now, Pause / Resume, a **Pause For…** submenu (15
minutes / 1 hour / Until 8 AM), a **Voice** submenu (status, Mute/Unmute Wake
Word, Restart Voice, Open Voice Logs), Open Dashboard, Open Logs, Run Setup
Wizard, Quit.

**Threading discipline** is the recurring theme. `rumps.alert` must be called on
the main thread, so `brief_me` runs its 180-second HTTP call on a worker and
*parks* the error message for the existing tick to raise. `run_setup` spawns the
wizard as a **separate process** (Tkinter and rumps each insist on owning their
process's main thread) and waits on a worker so the menu bar doesn't wedge.

Timed pauses are enforced from both ends: the dashboard stores `paused_until`,
`telegram.py`'s pause guard auto-clears it on the next message, and this tick
auto-resumes proactively when the deadline lapses.

#### `friday/menubar_icon.py` — 346 lines

Generates the menu bar icon set with AppKit (no PIL — PyObjC is already a rumps
dependency). Prefers a user PNG at
`~/Library/Application Support/friday/menubar.png`, auto-scaled, with paused at
~55% opacity and offline at ~25%. Falls back to rendering "F.R.I.D.A.Y." in
orange. Cached under `~/Library/Caches/friday/menubar/`, invalidated by source
mtime. Also supplies `ensure_favicon()` for the dashboard and `circular_crop()`
for the Dock icon.

---

#### `friday/mac_app.py` — 225 lines

**What.** The entry point for the packaged `Friday.app`. Mirrors `tray.py`'s
role on Windows.

`Friday.app` → this file (wizard on first launch, then supervise + menu bar).
`Friday.app --core` → `friday.main()`. `Friday.app --setup` → the wizard.

**Why supervision lives here** rather than in a `KeepAlive` LaunchAgent: the
.app must survive being launched from Finder by a user who has never opened a
terminal. "Start at Login" adds the LaunchAgent on top for the always-on case.

**`_prime_calendar_access()`** is subtle and important. TCC grants are per
executable binary, and the menu bar process and the core are the **same
binary** — so a grant obtained here is one the core inherits. It has to happen
in *this* process because the core has no foreground UI: its request produces a
prompt nobody is looking at, times out as "not determined", and demotes every
calendar read for the rest of that process's life to the JXA fallback (minutes
per briefing).

**`_become_accessory()`** drops the Dock tile at runtime rather than declaring
`LSUIElement` in the bundle — an accessory process never gets Tk windows on
screen, which would make the setup wizard invisible.

**`_run_wizard()`** always spawns a child process: Tkinter creates and
configures the process's `NSApplication`, and rumps would then inherit that
instead of building its own, losing the status item.

A singleton is held by binding port 51740; a second launch just opens the
dashboard.

---

#### `friday/tray.py` — 276 lines

**What.** The Windows pystray tray app. Same supervisor policy as `mac_app.py`
— a clean exit is a restart, rapid exits are a crash loop and get backed off (3 s
after a healthy run, 30 s after a quick death) so a bad config doesn't spin the
CPU. Quit sets a flag so the exit is final.

Adds a **Velopack update loop**: check shortly after boot, then every 6 hours;
applying goes through the normal quit path so the core isn't left orphaned
holding port 5174 across the restart. `velopack.App().run()` must be the very
first thing in `main()` — during install/update, `Update.exe` invokes this exe
with `--veloapp-*` args and expects it to handle them and exit.

The tray icon is drawn at runtime with PIL, so no asset file ships.

---

#### `friday/macos_setup.py` — 243 lines

**What.** Generates Friday's LaunchAgents and the voice launcher config.

**Why it exists,** verbatim from the module docstring: the repo used to ship
plists and a C launcher with absolute paths baked in for one developer's home
directory and Python version. Nothing in that set could launch on any other
machine. The plists are now templates with `@PLACEHOLDER@` variables rendered at
setup time against paths resolved on the machine actually running Friday.

The templates are string constants **in this file** rather than files under
`packaging/` so that a frozen .app and a source checkout resolve them the same
way — there is no bundle path to plumb through and nothing to drift.

**`resolve_python()`** — venv next to `friday.py` → running interpreter →
`shutil.which("python3")`. Never a hardcoded framework version. Under a frozen
.app, `sys.executable` is the app itself, which is correct: the agent should
relaunch the app, not a loose python. The resolved path is recorded in the
agent's `FRIDAY_PYTHON` env var.

`_write_agent()` **parses the plist it just generated** before installing it —
a malformed plist is rejected silently by launchd, which is miserable to debug.

The voice agent launches the `.app` wrapper, not python directly, because TCC
grants the microphone to the bundle. `write_voice_launcher_conf()` writes to
`~/.friday` and **not** `paths.data_dir()`, with a comment explaining the bug:
the C launcher reads exactly two locations, and on macOS `data_dir()` is the
package directory, which is neither — writing it there meant the launcher never
found a conf, fell through to bundle-relative defaults that don't exist in a
source checkout, and exited 127 on every spawn, which `KeepAlive` turned into a
crash loop.

---

#### `friday/setup_wizard.py` — 1,335 lines

**What.** The Tkinter first-run wizard, shared by both platforms.

Eight steps: **Welcome → Telegram → Gemini → Calendar (branching apple/google) →
Canvas → Weather → Schedule → Finish**, with a state machine
(`_show_step`/`_collect_step`/`_advance`/`_back`) and a busy/status indicator.

Each credential step **validates live** before letting the user continue:
`_telegram_getme` + `_telegram_getupdates` (which is how the chat_id is
discovered rather than typed), `_canvas_whoami` against the iCal feed,
`_token_format_ok`, `_chat_id_ok`. `_scrub()` strips the secret out of any error
text before it's displayed. `_guess_timezone()` seeds the schedule step.

The calendar step branches on `_calendar_backend()`: the Apple path lists real
calendars via `calendars.apple.list_calendars_detailed` (writable ones only for
the default-calendar picker), and the Google path runs the OAuth installed-app
consent flow and saves the token to `paths.google_token_path()`.
`_macos_foreground()` forces the Tk window to the front, which a
launched-from-Finder process otherwise won't get.

---

### 4.11 Operator Tooling

#### `friday/tools_verify_dispatch.py` — 66 lines

A read-only CLI (`python3 tools_verify_dispatch.py [N]`) that reads
`dispatch_log` back and **pairs each dispatcher decision with the chat call it
produced** — matching the nearest `user_message` exchange at or after it, which
is safe because the semaphore serializes the whole path. Prints per-row
selection, dispatcher latency, chat-call input tokens, and flags (`TIMEOUT`,
`PARSE_FAIL`, `FALLBACK-RETRY`), plus an all-time summary: total dispatches,
fallback-retry rate, and how many messages were repeated (the answer to "is a
dispatch cache worth building").

Renders a 10-tool selection as `ALL (fail-open)`.

#### `friday/run.sh` / `friday/restart.sh`

Developer launch scripts. Both resolve the interpreter as `$FRIDAY_PYTHON` →
`./.venv/bin/python3` → `python3` on PATH, and both resolve their own directory
so the files are identical on every machine.

`restart.sh` additionally runs a bash watchdog loop with a `/tmp` mutex, a PID
file at `logs/watchdog.pid`, and a give-up-after-5-crashes-in-10s rule. **On the
current setup this conflicts with the LaunchAgent** — prefer
`launchctl kickstart -k gui/$(id -u)/com.friday.agent`.

---

## 5. The Voice Satellite

`friday/voice/` is a **standalone program**. Rule 13 of the project: it never
imports from Friday's core. Its only coupling is a read-only SQLite query
against `system_state.status` to learn whether Friday is online.

It reaches Friday the same way the user does — by sending a Telegram message.

---

#### `voice/listen.py` — 768 lines · the orchestrator

Boots audio + wake + clap + PTT + Whisper + the Telegram bridge, then runs a
13-step session state machine.

**Three independently toggleable trigger paths:** wake word (openWakeWord),
double-clap, and push-to-talk.

**`_probe_microphone()`** is the boot-time TCC validator and it exists because
of a specific failure mode: **under launchd, TCC denial returns silent
(zero-filled) audio buffers rather than an error.** A denied process opens its
stream "successfully" and reads nothing forever. So the probe drains ~1 second,
computes the peak, and **aborts the process** (exit 3) on `peak == 0` so launchd
throttles instead of respawning into endless silent sessions.

**Two more permission gates,** each a separate TCC service and each checked with
its own prompt:

- **Accessibility** (`AXIsProcessTrusted`) — required for global key monitoring.
  It follows the *running binary*, not the responsible bundle, so a grant added
  by hand in System Settings (stored by bundle identifier) never matches a
  binary launchd exec'd directly (identified by path). Letting macOS raise the
  prompt is what registers the identity it will actually check. The
  no-options call is mandatory when not prompting: handing
  `AXIsProcessTrustedWithOptions` an empty dict **segfaults** inside
  `CFGetTypeID`, killing the process mid-boot with nothing in the log.
- **Input Monitoring** (`CGPreflightListenEventAccess`) — a *second* gate.
  pynput warns about neither: with Accessibility granted and this one denied,
  `keyboard.Listener` starts, logs nothing, and delivers zero events forever.

Both prompts are rate-limited to once an hour via `/tmp` marker files, because
granting Accessibility makes TCC **kill** the process, `KeepAlive` respawns it,
it prompts again, TCC kills it again — 35 respawns in four minutes, dialog each
time.

**The orange indicator.** The always-on stream only starts at boot if
`wake_enabled` or `clap_enabled`. With both false, listen.py runs PTT-only and
opens the mic per session, so the indicator is dark at idle. Within a PTT
session the stream is closed **right after capture**, not in the `finally` —
everything below it (Whisper, the bridge round trip, TTS) runs for seconds with
no further use for the mic, and macOS keeps the dot lit as long as the stream is
open. Closing early makes the dot go out on key release, which is what the user
reads as "it stopped listening."

**Failure cues** (`_FAILURE_CUES`) distinguish outcomes that matter to the user.
A **timeout means the message did reach Friday** and the answer is already on
its way to Telegram; the old blanket "I couldn't reach Friday" was simply false
— it said that while a briefing was landing in chat.

Also: `PTT_TAIL_MS` / device-open latency handling (below), a "Working on it,
sir" cue after 20 s of waiting, and TTS only when external audio is present
unless `always_speak`.

---

#### `voice/audio.py` — 488 lines

One PyAudio input stream feeds a shared thread-safe **ring buffer** (64 frames ≈
5.1 s); multiple consumers (wake, clap, recorder) subscribe with independent
cursors so a slow reader can't starve a fast one — frames it misses are dropped
with a warning.

`record_until_silence()` prepends ~500 ms of pre-roll from the ring so the first
syllable after a wake word isn't clipped, then stops after 1.5 s of
below-threshold RMS (but only once voice has been heard).

`record_while_held()` carries the measurement that motivates its two guards:
opening the input device on macOS takes ~470 ms, with the first real frame
landing ~575 ms after key-down. A plain "break when released" returned nothing
but pre-roll — every failed session in the log read exactly `0.48s captured`
with rms 0, and Whisper transcribed it as `""`. So: `PTT_TAIL_MS = 400` keeps
capturing briefly past release (speech runs past the key coming up), and
`MIN_PTT_CAPTURE_MS = 1500` never hands Whisper less than that.

`ClapDetector` is an onset detector: a clap spikes RMS far above the local
moving average and is followed by a brief quiet gap. Speech rarely produces that
gap structure; music averages out across the moving window. Sensitivity maps
0.0→6× and 1.0→3× the moving average, with an absolute floor.

---

#### `voice/bridge.py` — 470 lines

Talks to the Friday bot **as the user account** via Telethon. A bot cannot
impersonate a user, so the transcription must originate from the user's account
to trigger Friday's `on_message`.

Owns its own asyncio loop in a background thread; `send_and_wait` is the
synchronous integration point. It returns a `BridgeResult` with an `Outcome`
enum rather than a bare reply string, and the distinction that matters is
`reached_friday` — `OK`/`TIMEOUT`/`DISCONNECTED` mean the message *was*
delivered; `SEND_FAILED`/`NOT_CONNECTED`/`EMPTY_TEXT` mean nothing was.
Collapsing those into a bare `None` is what made voice announce "I couldn't
reach Friday" about a briefing that arrived four seconds later.

`StringSession` rather than a file session: the SQLite-backed session was prone
to `database is locked` when Telethon's update loop, keepalive, and reply
handler raced. The blob is persisted back into `friday_config.yaml` by
`config.persist_telethon_session`.

`_ensure_connected()` runs before **every** send. Telethon's auto-reconnect
gives up for good after `connection_retries` attempts — which a sleeping Mac or
a Wi-Fi drop reaches easily — and once it does, every send raises
`ConnectionError` for the rest of the process's life.

Under launchd there is no TTY, so headless mode replaces Telethon's phone/code
prompts with a callback that raises a **clear, actionable error** instead of
hitting `EOFError` and letting `KeepAlive` spin.

`_wait_for_reply_or_disconnect()` races the reply against connection loss on a
500 ms tick, so a mid-wait disconnect doesn't burn the full timeout on a future
nothing will resolve.

---

#### `voice/config.py` — 338 lines

An mtime-keyed typed view of the voice slice of `friday_config.yaml`, cheap
enough to call per frame from the wake loop. Falls back to the last good value
on a YAML parse error rather than crashing.

`_coerce_str()` treats a blank as the default — the dashboard writes these as
free-text inputs, so clearing one leaves `""` in the YAML rather than removing
the key, and an empty `push_to_talk_key` used to raise out of `PTTListener` and
take the whole listener down.

`response_timeout_s` defaults to **120**, with a comment explaining why: a plain
conversational turn answers in 5–12 s, but anything that makes the agent run
tools takes far longer — "brief me now" measured 34 s, which the old 30 s
ceiling cut off four seconds early, so Telegram got the briefing while voice
announced it couldn't reach Friday.

`persist_telethon_session()` does a **targeted line-level edit** rather than a
YAML round-trip, preserving comments and key order.

---

#### `voice/wakeword.py` · `voice/ptt.py` · `voice/tts.py`

- **`wakeword.py`** wraps openWakeWord. Each configured phrase resolves to an
  `.onnx` — a custom model in `voice/models/` wins, otherwise a small allowlist
  aliases to the bundled `hey_jarvis`. Single-token phrases are dropped unless
  `solo_trigger_enabled`. 2-second debounce per label.
- **`ptt.py`** wraps `pynput.keyboard.Listener` (`suppress=False` — we only
  observe). Its one-shot "key monitoring live — first event observed" log line
  exists because without it, "the event tap is dead" and "the configured key
  never matches" look identical.
- **`tts.py`** shells out to macOS `say`. Strips markdown **and emoji** —
  otherwise `say` verbalizes codepoint names ("memo", "pushpin", "check mark").
  Caps at 500 chars, cutting at the last sentence boundary and appending
  "…I've sent the full response to Telegram, sir." `external_audio_present()`
  recreates PyAudio every call so freshly-plugged headphones are seen
  immediately.

#### `voice/trainer_src/`, `voice/training/`, `voice/models/`

Vendored openWakeWord (third-party, ~4.4k lines) plus a Piper TTS shim for
generating synthetic training samples, and the two shipped models
(`hey_friday.onnx`, `hey_jarvis.onnx`). Not part of Friday's runtime path.

#### `FridayVoice_launcher.c` + `FridayVoice.app/`

A minimal Mach-O launcher. **Why a compiled launcher at all:** macOS TCC tracks
microphone permission against the "responsible code" of the requesting process.
A `#!/bin/bash` launcher fails to attach the bundle as responsible code, so TCC
matches on the bare python codesign identity and silently inherits whatever
launchd-context decision it has on file — typically denied, no dialog. Executing
a real Mach-O inside `Contents/MacOS/` makes macOS see the .app as the
originator, and the grant then persists against `CFBundleIdentifier`
(`com.friday.voice`).

Paths are resolved at runtime from a two-line conf (interpreter, script) in
either `Contents/Resources/` or `$HOME/.friday/`. The launcher must **fork, not
`execv`** — Accessibility and Input Monitoring grants follow the *running*
process identity, and `execv` threw away the .app identity so hand-added
Settings grants stopped matching.

---

## 6. Packaging & CI

**macOS** — `packaging/macos/` (`friday.spec`, `build.sh`, `make_icon.py`,
`BUILD_MACOS.md`) plus `.github/workflows/build-macos.yml` produce a
`.app`/`.dmg`. Version comes from `$VERSION`, exported so the spec reads it out
of the environment and `CFBundleVersion` matches the .dmg filename — never
hardcoded. Signing is ad-hoc by default; `SIGN_ID` switches to Developer ID for
notarization.

The `Info.plist` declares four usage descriptions, and the third is the one
people get wrong: `NSMicrophoneUsageDescription`,
`NSCalendarsUsageDescription`, **`NSCalendarsFullAccessUsageDescription`**
(without it, macOS 14+ resolves the request to *write-only* access, which
reports as granted and then returns zero events), and
`NSLocationWhenInUseUsageDescription`.

**Windows** — `packaging/windows/` (spec, `installer.iss`, `build.ps1`,
`BUILD_WINDOWS.md`) plus `build-windows.yml` produce `FridaySetup.exe`: Inno
Setup around a PyInstaller **onedir**, windowed (no console) build, with
Velopack for updates. Installs to `{autopf}\Friday` with an optional
`{userstartup}` shortcut; uninstall runs `taskkill` first. The Google OAuth
desktop client JSON is created once by the maintainer and bundled at build time.

---

## 7. Reference Tables

### 7.1 The Complete Tool List

Ten tools, registered in `agent/tools.py::make_tools()` and mirrored line-for-line
in `dispatcher.TOOL_MANIFEST`. Gemini-only — on the `ollama` provider `_tools` is
`None` and there is nothing to dispatch.

---

**1. `get_schedule(start_date, end_date) → dict`**
> *Manifest: "Look up existing calendar events in a date range."*

Events from the user's calendar with start times in `[start_date, end_date)`.
Both ISO `YYYY-MM-DD`; **end is exclusive** (a single day means `end = start +
1`). Only whitelisted calendars. Times are already converted to the user's local
timezone — the docstring tells the model to present them as-is and apply no
further offsets. Each event carries a `uid`, which is the handle for
`update_calendar_event`; the model is told to call this first whenever the user
wants to change an existing event, and never to show a uid to the user.

Returns `{timezone, count, events: [{title, start, end, location, calendar, uid}]}`.

---

**2. `get_weather(query="") → dict`**
> *Manifest: "Weather, forecast, rain, or temperature."*

The user's **exact phrasing** is passed through as `query` so the connector can
pick up intent (rain vs. temperature vs. general) and time-of-day phrases
("tonight", "at 3pm"). Returns `{weather: "<sentence>"}`.

---

**3. `get_pending_canvas() → dict`**
> *Manifest: "Unalerted Canvas assignments and due dates."*

Canvas items tagged `URGENT` or `SOON` with `notified = 0`, ordered by due date.
Reads the SQLite events buffer, not the calendar. Returns
`{count, items: [{title, due_at, urgency}]}`.

---

**4. `reschedule_briefing(briefing_type, time) → dict`**
> *Manifest: "Move the next morning or evening briefing once."*

**One-time** override of the next occurrence, then the config default is
restored automatically. `briefing_type` is `"morning"` or `"evening"`; `time` is
24-hour `HH:MM` — the model is told to convert any phrasing ("nine in the
morning", "half past eight") itself. A time already past today is interpreted as
tomorrow. Writes `{kind}_briefing_override` to `system_state` (so it survives a
restart) **and** re-registers the one-shot job on the live JobQueue. Returns the
resolved local fire time for the model to relay.

For a *permanent* change, the model is directed to `update_setting` instead.

---

**5. `add_calendar_event(title, date, start_time="", end_time="", calendar="", location="", notes="") → dict`**
> *Manifest: "Create a new calendar event."*

**Writes immediately, no approval gate.** The tool sends the user a one-line
confirmation with a quip appended, and returns `next_action: "Do not produce any
further output for this turn."`

Only for events that do not exist yet — the docstring explicitly routes
amendments to `update_calendar_event`, "calling this tool would leave them with
two copies of the same event."

`title` carries two mandatory pre-call rules: **sanity-check the words** (voice
transcription artifacts — "git apples" → "Get Apples", "by milk" → "Buy Milk",
"dock tor" → "Doctor"; ask rather than guess when genuinely ambiguous) and
**Title Case the result**, preserving intentional internal casing (iPhone,
FBLA). `actions/calendar.py::_normalize_title` is the backstop.

Omitting `start_time` makes it all-day; omitting `end_time` defaults to one
hour. `location` is the calendar's Location field — "do not put places in notes."

---

**6. `update_calendar_event(uid, title="", date="", start_time="", end_time="", calendar="", location="", notes="") → dict`**
> *Manifest: "Change an event already on the calendar."*

**Edits in place, immediately, no approval gate**, with its own one-line
confirmation naming what changed.

The docstring teaches the two-step protocol: call `get_schedule` for the day,
find the event, pass its `uid` **and** `calendar` plus only the changed fields.
*"Never invent a uid — if get_schedule doesn't return the event, tell the user
you can't find it rather than guessing or adding a new one."*

`date` is **required** whenever `start_time` or `end_time` changes (there is no
way to recompute one end of a range without knowing the day it sits on) — the
tool returns an error rather than guessing. Omitted fields are untouched; a
**single space** is the escape hatch for deliberately clearing one, since the
SDK cannot send `None` and `""` means "not supplied".

---

**7. `add_quip(text, target="both") → dict`**
> *Manifest: "Teach Friday a new phrase to say."*

Stores the user's wording **verbatim** — the docstring forbids paraphrasing,
grammar fixes, adding or removing "sir", or softening the joke, and says to read
back a suspected voice mis-transcription rather than guessing. `target` is
`confirmation` (only after calendar writes), `voice` (ordinary conversation), or
`both` (default). Live on the next message; the model is told never to say a
restart is needed.

---

**8. `list_quips(target="both") → dict`**
> *Manifest: "List the phrases Friday can say."*

Returns `learned` (removable), `bundled` (ships with the app), and `disabled`
(bundled phrases the user retired). The model is told to read back the phrases
themselves — the user doesn't need the bundled/learned distinction unless
they're removing one.

---

**9. `remove_quip(text, target="both") → dict`**
> *Manifest: "Stop Friday using a phrase."*

Case-insensitive partial match, so "the touch grass one" works. On status
`ambiguous` **nothing was removed** — the model must read the `matches` back and
ask. On `not_found`, say so plainly rather than guessing at a near miss.

---

**10. `update_setting(key, value) → dict`**
> *Manifest: "Change Friday's tone, preset, default calendar, or briefing times."*

Exactly six settable keys (see §4.8). Any other key is **refused** — API keys,
tokens, models, and file paths are dashboard-only, and the docstring instructs:
*"When refused, tell the user that plainly and point them at the dashboard; do
not look for another way to do it."*

`persona.custom_instructions` **replaces** rather than appends, so the model is
told to confirm the full new text when the user is adding to existing
instructions.

Changing a briefing time additionally **re-registers the daily job on the live
JobQueue** (remove by name, `run_daily` again), or the setting wouldn't take
effect until the next restart.

---

### 7.2 Every LLM Call In The System

| Call site | Profile | Tools | Purpose |
|---|---|---|---|
| `telegram.py::on_message` → `_think` | CHAT | dispatcher-selected | The conversation |
| `telegram.py` miss-recovery retry | CHAT | **all** | One retry when the dispatcher returned `[]` and no action fired |
| `dispatcher.dispatch_detail` | ROUTE | — | Pick the tool shortlist (separate small model, own client, own timeout) |
| `briefings.compose_morning` | COMPOSE | off | Morning briefing prose |
| `briefings.compose_evening` | COMPOSE | off | Evening briefing prose |
| `briefings.compose_on_demand` | COMPOSE | off | Dashboard/menubar "Brief Me Now" |
| `briefings.compose_urgent_alert` | COMPOSE | off | One urgent interrupt |
| `friday.py::process_untagged_events` | CLASSIFY | off | `URGENT`/`SOON`/`NORMAL` — one word |
| `friday.py::extract_groupme_events` | CLASSIFY | off | `NONE` or single-line event JSON |
| `core.py::on_media` | CLASSIFY | off, `response_json` | Photo/PDF → event JSON |
| `tools.py::_quip_for` | CLASSIFY | off | An integer index into the quip list |

Note that `on_message` is the **only** path where tools are ever attached.

---

### 7.3 SQLite Schema

Defined in `memory/db.py`. **Calendar-type data never lives here** — the
calendar backend is the event store.

**Operational tables**

| Table | Key columns | Purpose |
|---|---|---|
| `system_state` | `key`, `value`, `updated_at` | Runtime KV. Replaces `state.json`. Holds `status`, `paused`, `paused_until`, `provider`, `model`, `started_at`, `think_calls`, `tokens_in/out`, `last_message_at/preview`, `last_{morning,evening}_briefing_sent`, `{morning,evening}_briefing_override` |
| `events` | `id`, `source`, `title`, `body`, `due_at`, `urgency`, `processed`, `notified`, `calendar_synced`, `event_extracted`, `created_at` | The raw ingest buffer. Ids are `canvas_<UID>` / `groupme_<msgid>` |
| `last_seen` | `source`, `cursor`, `updated_at` | Per-connector cursors (`canvas`, `groupme_<gid>`) |
| `pending_actions` | `id`, `action_type`, `payload`, `status`, `created_at`, `resolved_at` | The approval gate. `status` ∈ pending/confirmed/cancelled/failed |
| `conversation_history` | `id`, `role`, `content`, `created_at` | Rolling chat log |
| `synced_events` | `google_event_id`, `calendar_name`, `apple_event_id`, `synced_at` | gcal_sync dedup, keyed on the iCal UID |

**Activity capture** (powers the dashboard's Today surface; all writes
best-effort via `memory/activity.py`)

| Table | Purpose |
|---|---|
| `llm_exchanges` | One row per `_think()` call — model, previews, tokens, duration, `triggered_by`, plus verbatim `full_prompt`/`full_response` |
| `tool_calls` | One row per function-call invocation — name, bound args, result preview, duration, `triggered_by` |
| `dispatch_log` | One row per dispatcher decision **including failures**. Carries raw + normalized message, sha256 hash (indexed), selected tools, counts, provider/model, latency, tokens, `outcome`, `fallback_triggered`. Exists to answer two questions: how often the same message is dispatched twice, and how often the dispatcher missed |
| `briefings_sent` | Every briefing that shipped — slot, full body, `on_time`/`catchup`/`override`, minutes late |
| `urgent_alerts_sent` | Every interrupt fired — source, the `events.id` that triggered it, body preview |

A nightly job at 03:00 trims `llm_exchanges`, `tool_calls`, `briefings_sent`,
and `urgent_alerts_sent` to 30 days.

---

### 7.4 Dashboard HTTP API

Bound to `127.0.0.1:5174`. No auth — never network-exposed.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The SPA |
| GET | `/api/status` | Live state, uptime, token/call counters, next briefing times |
| GET | `/api/config` | Full config, secrets masked (`?reveal=1` to unmask) |
| POST | `/api/config` | Atomic write, with mask-splicing protection |
| GET | `/api/today` | The Today bundle — status, feed, what's-next, stats, pending count |
| GET | `/api/llm/last` | Most recent exchange in full + its tool calls |
| GET | `/api/quips` | Learned / bundled / disabled phrases |
| POST | `/api/quips` | Add a phrase |
| DELETE | `/api/quips` | Remove or retire a phrase |
| GET | `/api/pending-approvals` | Pending cards with decoded drafts |
| POST | `/api/pending-approvals/{id}/{confirm\|edit\|cancel}` | Resolve through the same pipeline Telegram uses |
| GET | `/api/calendar/sync-status` | Last Google→Apple sync per calendar |
| GET | `/api/groupme/groups` | The account's groups, for the picker |
| GET | `/api/gemini/models` | Available models + free-tier quota hints |
| POST | `/api/friday/restart` | launchctl kickstart / self-SIGINT (see §4.9) |
| POST | `/api/friday/pause` | Pause / resume, with optional `until` |
| POST | `/api/friday/brief` | Compose and send an on-demand briefing |
| GET | `/api/voice/status` | LaunchAgent state, listening flag, wake enabled |
| POST | `/api/voice/wake` | Flip `voice.wake_enabled` + kick the agent |
| POST | `/api/voice/restart` | Kick the voice LaunchAgent |
| GET | `/api/voice/logs` | Tail `voice.err` |
| GET | `/api/logs` | Tail `friday.log` |
| POST | `/api/test/telegram` | Connectivity self-test |
| POST | `/api/test/canvas` | Connectivity self-test |

---

### 7.5 Scheduled Jobs

All on PTB's `JobQueue`. No second scheduler exists anywhere in the codebase.

| Registration | Name | Cadence | Job |
|---|---|---|---|
| `run_daily` | `morning_briefing_daily` | `agent.morning_briefing_time` | Morning briefing (window-guarded) |
| `run_daily` | `evening_briefing_daily` | `agent.briefing_time` | Evening briefing (window-guarded) |
| `run_daily` | — | 03:00 | Activity-table cleanup (30-day retention) |
| `run_repeating` | — | 900 s, first at 60 s | Poll connectors → tag urgency → extract events → catch-up check |
| `run_repeating` | — | 60 s, first at 10 s | Fire urgent interrupts → catch-up check |
| `run_once` | `{kind}_briefing_override_job` | on demand | Rescheduled briefing (restored at boot from `system_state`) |
| `run_once` | `{kind}_briefing_catchup_job` | on demand | A missed briefing, delivered late |

The two `run_daily` jobs are **named** so `update_setting` can find and replace
them in place when the user moves a briefing time permanently — the same
remove-then-re-register dance `reschedule_briefing` does for one-off overrides.

---

### 7.6 Configuration Surface

`friday/friday_config.yaml.example` is the canonical commented template. Blocks:

| Block | Notes |
|---|---|
| `agent` | Briefing times, IANA timezone, `briefing_catchup_max_minutes`, `default_calendar` (mandatory — macOS has no scriptable default), `briefing_calendars` whitelist |
| `telegram` | `bot_token`, `chat_id` (a **number**, not a @username — a username is accepted at startup and then 403s on the first send), plus `api_id`/`api_hash`/`telethon_session` for voice |
| `memory` | `db_path` (relative resolves against the data dir), `short_term_turns` |
| `provider` | `ollama` \| `gemini` |
| `gemini` / `ollama` | Model, max tokens, key / base URL |
| `dispatcher` | `enabled: false` restores pre-dispatcher behavior **exactly**; provider, model, `timeout_s`, `ollama_model` |
| `calendar` | `backend: apple\|google`, `google.auto_create` |
| `canvas` | `ical_url` (required), optional `api_token` |
| `weather` | OpenWeatherMap key + `"City,CC"` |
| `groupme` | `api_token` + per-group `{name, id, priority, enabled}` |
| `gcal_sync` | `[{name, ical_url}]` — the secret URLs are credentials |
| `notifications` | The dashboard-facing mirror. `groupme_polling: false` is a **real kill switch** read by the poll job; the `agent` block stays canonical and wins on disagreement |
| `voice` | Read only by `voice/listen.py`, which does **not** reload it — restart the voice agent after changing it |
| `gmail` | Present so the shape stays stable; not implemented |

Env fallbacks exist for `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`GEMINI_API_KEY`, `WEATHER_API_KEY`, `WEATHER_LOCATION`.

---

## 8. Cross-Cutting Invariants

These are the rules that hold the architecture together. Most were paid for once
already.

1. **The semaphore stays at the top of `on_message`.** Before SQLite, before
   context assembly, before anything.
2. **PTB's JobQueue is the only scheduler.** No second timing library, no
   background timing threads.
3. **No iMessage anywhere.** Not polled, not read, not drafted to. Permanently
   out of scope.
4. **Never write an inferred event without an approval gate.** Explicit requests
   and Canvas due dates use `auto_write`; anything Friday deduced uses
   `gated_write`.
5. **Never hardcode a Python path in a LaunchAgent.** `macos_setup.py` renders
   plists against the interpreter resolved on the running machine.
6. **Canvas uses the iCal feed.** Never HTML scraping.
7. **The LLM processes all ingested data** — urgency, filtering, extraction —
   even for clean structured input.
8. **The calendar backend is the event store.** Briefings and reminders read
   from it, not from the SQLite events table.
9. **Briefings run with tools OFF.** A thin briefing means expanding
   `bundle_briefing_context`, never re-enabling tools.
10. **The dispatcher fails open.** Any change that lets a failure path return
    `[]` instead of the full tool list is a bug, however clean it looks.
11. **`TOOL_MANIFEST` stays in sync with the registered tools.** Adding a tool
    means adding a manifest line in the same commit — `assert_manifest_matches_tools`
    raises at startup otherwise.
12. **SQLite is the operational backbone only.** No `state.json`, no vector
    store, no RAG, no Redis.
13. **Voice is a standalone satellite.** It never imports Friday's core.
14. **Friday does not edit its own Python source.** `self_edit.py` writes YAML
    only.
15. **All secrets live in config or environment variables.** Never hardcoded.
16. **`compat.strftime()` for any format string containing `%-`.**
17. **Instrumentation never raises into the hot path.** `memory/activity.py`
    swallows its own errors by design.
18. **Every PyObjC completion handler and delegate method returns `None`.**
    Returning a value raises an uncaught NSException inside the callback thread
    and aborts the process. This applies in `apple_calendar.py` and
    `location.py`.
19. **TCC grants are per executable binary.** The menu bar and the core share
    one; the voice `.app` wrapper and the bare interpreter do not. A grant for
    one never extends to the other.

---

## 9. Known Gaps & Rough Edges

Observed while reading, stated as fact rather than criticism.

**Not built (by design, per the implementation status):**

- **Phase 5 — Drafting & sending.** No `actions/groupme_send.py`, no Gmail
  drafts. The `edit` verb on approval cards is wired in the dashboard but is a
  no-op in Telegram (`on_callback` explicitly ignores it). Gmail stays
  deprioritized: there is no accessible API path for locked-down school
  accounts; the config block exists only to keep the shape stable.
- **Proactive due-date reminders (5/3/1 days).** `notifications.reminder_thresholds`
  is written, editable in the dashboard, and consumed by **nothing**. This is
  the largest gap between the config surface and actual behavior.

**Smaller things:**

- **GroupMe duplicate rows.** There is an explicit `TODO` in
  `connectors/groupme.py::_poll_one` — the events table can produce duplicate
  rows for the same message, visible as repeated entries in the dashboard's
  Today feed. The fix noted in the comment is to dedup on message id before the
  INSERT.
- **`dispatch_log` is not trimmed.** `cleanup_old_activity()` covers
  `llm_exchanges`, `tool_calls`, `briefings_sent`, and `urgent_alerts_sent` but
  not `dispatch_log`, which grows unbounded. Currently harmless (one small row
  per message) and arguably intentional while the dispatcher is being evaluated,
  since `tools_verify_dispatch.py` reports all-time statistics.
- **"Brief me" over Telegram is not the on-demand composer.**
  `compose_on_demand`'s docstring mentions it, but `send_on_demand_briefing` is
  wired **only** to the dashboard's `/api/friday/brief` (which the menu bar and
  tray call). Typing "brief me" in Telegram goes through the ordinary chat path
  with dispatcher-selected tools, which produces a similar answer by a different
  route and with different token economics.
- **Briefings ignore the pause flag.** `_pause_active` guards `on_message` and
  `on_media` only; scheduled briefings and urgent alerts still send while
  paused. Defensible (pause reads as "stop replying to me"), but worth knowing.
- **`restart.sh` conflicts with the LaunchAgent.** Its watchdog and launchd will
  both try to own the process. Use `launchctl kickstart -k gui/$(id -u)/com.friday.agent`.
- **`POST /api/config` accepts an arbitrary payload.** It is written to disk
  after mask-splicing and briefing-time syncing, with no schema validation.
  Localhost-only and driven by the SPA, so the exposure is a hand-crafted
  request or a buggy front-end, not an attacker.
