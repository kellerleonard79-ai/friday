# Project Friday — Claude Code Instructions

> **⚠️ TEARDOWN IN PROGRESS — branch `llm-layer-teardown`.**
>
> The entire LLM interaction layer and the tool-calling system have been
> removed, to be rebuilt from scratch. **Deleted:** `agent/tools.py`,
> `agent/dispatcher.py`, `agent/profiles.py`, all persona and
> system-instruction assembly, and every prompt string in the repo.
> `agent/core.py` is now a bare transport seam — `complete(prompt, *, images,
> json, triggered_by) -> str`, no system instruction, no tools, no history.
>
> Sections below that describe those layers are marked **[TORN DOWN]**. The
> architectural principles are NOT torn down and still bind: the semaphore at
> the entry point, calendar-as-event-store, SQLite-as-operational-state,
> no-iMessage, and PTB-JobQueue-only.
>
> Do not rebuild any of the removed layers piecemeal while working on
> something else. That is its own task.

## Codebase Navigation & Knowledge Graph
- **Search Strategy:** Before running broad `grep` or file searches across the repository, check `graphify-out/GRAPH_REPORT.md` to map out dependencies and locate relevant files.
- **⚠️ The Graphify snapshot is STALE as of the `llm-layer-teardown` branch.** It still maps `agent/tools.py`, `agent/dispatcher.py` and `agent/profiles.py`, which are deleted. Regeneration is a `/graphify` skill invocation, not a scripted repo step, so it was not run as part of the teardown. Re-run it before trusting the graph for anything in `agent/`.
- **Context Gathering:** Use the Graphify snapshot to identify component relationships first, then perform targeted reads on specific files.

## What is Friday?

Friday is a personal AI secretary running on an always-on Mac (and, since the Windows port, on a friend's PC). It ingests information from multiple sources (Canvas, GroupMe, Google Calendar subscriptions), manages the user's calendar, delivers proactive briefings and urgent alerts via Telegram, and asks for approval before writing anything it inferred rather than was told. The user interacts with Friday through Telegram, a local web dashboard, and a voice satellite.

Friday is **not** a simple chatbot. It is a structured, event-driven agent with a tool layer, a memory layer, a proactive alert system, and a cost-aware LLM call layer. *(The tool layer and the LLM call layer are currently torn down — see the banner above. The memory layer, alert plumbing and approval gate are intact.)*

---

## Core Architecture Principles

- **[TORN DOWN] The persona, prompts and tools are gone.** Chat replies are bare and persona-less; briefings render as plain labeled text. That is the current expected behavior, not a bug.
- **Telegram is the primary UI.** Briefings, alerts, approvals, and conversational queries all happen there. Two secondary surfaces exist and are read/control planes, not conversation: the local web dashboard (`dashboard/`, 127.0.0.1:5174) and the voice satellite (`voice/`), which bridges speech back in as an ordinary Telegram message.
- **PTB JobQueue is the only scheduler.** `python-telegram-bot` is fully async. Never introduce a second scheduling library (`apscheduler`, `schedule`, `while True: sleep()`). All scheduled jobs register directly on `application.job_queue`. The dashboard's web server runs inside that same loop — it is not a second process.
- **The semaphore lives at the entry point.** `asyncio.Semaphore(1)` at the very top of `channels/telegram.py::on_message()` — before SQLite queries, before context assembly, before anything.
- **The LLM is the decision maker for ingested data.** *(Suspended — ingestion reaches no model at all right now. The rule stands for the rewrite: do not answer the gap with deterministic tagging.)* Deterministic code fetches, parses, and writes rows. The LLM decides urgency, filters announcements, extracts events from natural language, and writes every user-facing sentence.
- **The calendar backend is the event store.** Due dates, shifts, appointments live in Apple Calendar (macOS) or Google Calendar (Windows) — never in SQLite. See `calendars/backend.py`.
- **SQLite is the operational backbone.** No `state.json`. No vector store. No RAG. No Redis. It holds runtime key-value state, conversation history, the raw events buffer, cursors, pending approvals, and activity/observability rows.
- **No iMessage anywhere.** Not polled, not read, not drafted to. Out of scope permanently.

---

## File Structure

The repo root (`/Users/keller/friday`) holds only packaging, the `FridayVoice.app`
bundle, LaunchAgent plists, and `graphify-out/`. The Python package is the
`friday/` directory inside it — `friday/friday.py` is the core entry point. Run
`ls friday/` for the current layout.

Non-obvious placements:
- `agent/` — **[TORN DOWN]** now only `core.py` (the transport seam: `complete()`) and `briefings.py` (deterministic context bundling + plain renderers). `tools.py`, `dispatcher.py` and `profiles.py` are deleted.
- `calendars/` — backend dispatcher plus the two implementations (`apple.py`, `google_cal.py`). Distinct from `actions/calendar.py`, which is the write API both backends sit behind, and from `connectors/apple_calendar.py`, which is the reader.
- `dashboard/` — a FastAPI package, not a Tkinter script.
- `memory/activity.py` — best-effort instrumentation writes. Never raises into the hot path.
- `self_edit.py` + `phrases.py` + `quips.yaml` + `friday_voice.yaml` — the narrow slice of itself Friday may rewrite at runtime. **[PARTLY TORN DOWN]** the persona composition is gone; `phrases.random_quip()` and the dashboard's `/api/quips` still read these. `self_edit.version()` and `update_setting()` have no caller and are kept for the rewrite.
- `paths.py` and `compat.py` — the cross-platform seams.
- Entry points differ per platform: `friday.py` (core), `mac_app.py` (packaged .app supervisor), `tray.py` (Windows), `menubar.py` (rumps, source checkout), `macos_setup.py` (renders LaunchAgent templates), `setup_wizard.py` (first run, both platforms).

---

## SQLite Schema

Defined in `friday/memory/db.py` — read it there rather than trusting a copy.
Two families of table:

- **Operational** — `system_state`, `conversation_history`, `events`, `last_seen`,
  `pending_actions`, `synced_events`.
- **Activity capture** — `llm_exchanges`, `tool_calls`, `dispatch_log`,
  `briefings_sent`, `urgent_alerts_sent`. These record what Friday actually *did*
  and power the dashboard's Today surface. Written through `memory/activity.py`,
  which swallows its own errors by design; a nightly job trims each to 30 days.

Calendar-type data never lives here — see the event-store rule above.

---

## Async Architecture

```
friday.py
└── builds PTB Application
    ├── MessageHandler → telegram.py::on_message()
    │     └── asyncio.Semaphore(1)  ← gate is HERE, before everything
    │           ├── query SQLite for context
    │           ├── dispatcher.dispatch() → tool shortlist
    │           ├── agent/core.py::_think()
    │           └── telegram.send()
    │
    ├── CallbackQueryHandler → approval-card buttons (confirm/cancel)
    │
    ├── dashboard web server (same loop, 127.0.0.1:5174)
    │
    └── job_queue
          ├── run_daily        → morning_briefing_job     (tz-window guarded)
          ├── run_daily        → briefing_job (evening)   (tz-window guarded)
          ├── run_daily        → activity cleanup
          ├── run_repeating    → poll_connectors_job      (15 min)
          ├── run_repeating    → check_urgent_alerts_job  (1 min)
          └── run_once         → briefing overrides + missed-briefing catch-up
```

Briefings are guarded twice: a timezone sanity window (a briefing firing well
outside its hour is almost always a clock/DST bug, not a real trigger) and a
catch-up path that redelivers a briefing missed to sleep or an outage, up to
`agent.briefing_catchup_max_minutes`. `run_daily` owns the on-time minute so the
catch-up poll can never double-send.

---

## The LLM Call Layer — **[TORN DOWN]**

*Both mechanisms below are deleted. `agent/profiles.py` and
`agent/dispatcher.py` no longer exist; `TOOL_MANIFEST`,
`assert_manifest_matches_tools`, `_MEMBERSHIP`, `_TOOL_COUPLED`, the CHAT /
COMPOSE / CLASSIFY / ROUTE profiles, and the hand-rolled `_gemini_exchange`
tool loop are all gone. Every call is a plain text call. The `dispatch_log`
table, its migration and `tools_verify_dispatch.py` are kept; nothing writes
to that table any more. Kept here as the record of what the rewrite is
replacing and why each rule existed.*

Two mechanisms sat between a message and the model. Both exist for the same
reason: the naive envelope — full persona plus all ten tool schemas — cost
thousands of tokens on every call, re-sent on every hop of the tool loop.

### Call profiles (`agent/profiles.py`)

Every LLM call declares a profile that selects which slice of the persona it
carries. Sections are addressed by their Markdown heading, so `_MEMBERSHIP` is
the single table deciding what each profile gets.

- **CHAT** — the user is talking to Friday. Voice is the product and tools are live; carries everything.
- **COMPOSE** — Friday writes prose the user reads (briefings, urgent alerts). Voice matters, tool-usage sections do not, because these calls run with tools off by construction.
- **CLASSIFY** — the model returns a label, an index, or JSON (urgency tagging, GroupMe/media extraction, quip selection). Carries `CLASSIFY_INSTRUCTION` and no persona at all. Butler voice on a call whose contract is to return exactly `URGENT` is not merely wasted spend — it is a parse failure and a mis-tagged event.
- **ROUTE** — the dispatcher's own profile. Deliberately mapped to no persona section anywhere.

Unrecognized headings **fail open** to CHAT+COMPOSE and log once. AGENTS.md is
user-editable prose and Friday is an always-on daemon: an unmapped heading must
never silently vanish from the persona, and must never be a startup failure.

Some sections are tool-coupled (`_TOOL_COUPLED`) — shipped only when the tool
they describe is actually attached.

### Tool dispatcher (`agent/dispatcher.py`)

A cheap call against a small model (`models/gemini-2.5-flash-lite` by default)
returns a shortlist of tool names; the real call attaches only those schemas.
`TOOL_MANIFEST` is a hand-maintained one-line-per-tool table — deliberately NOT
derived from the docstrings, because deriving it would reintroduce the exact
cost the dispatcher exists to avoid.

Rules that must not be relaxed:
- **Every failure falls back to the FULL tool list, never to `[]`.** Timeout, parse failure, hallucinated name, dead provider — all attach everything. An empty list from a garbled response is indistinguishable from a genuine "no tools needed", and guessing wrong silently drops the user's request.
- **The manifest and the registered tools must match exactly, both directions.** `assert_manifest_matches_tools()` raises at startup. A tool missing from the manifest is invisible to the dispatcher and silently stops working while everything still looks healthy.
- The dispatcher is biased toward over-selection on purpose: an extra tool costs ~200 tokens of schema, a missing one is a wrong answer.
- Every decision, including failures, is logged to `dispatch_log`. `tools_verify_dispatch.py` reads it back paired with the chat call it produced.

### The tool loop is ours, not the SDK's (`_gemini_exchange`)

`automatic_function_calling` is disabled on every tool-bearing call. The SDK
still infers the schemas from the callables; only its execute-and-re-ask loop is
off. `FridayAgent._gemini_exchange` runs the loop by hand because the SDK's has
exactly one exit — append the tool result, call the model again, take its text —
and that last hop re-sends the full envelope (persona, every attached schema,
history) to ask a question that is already answered.

The traffic controller:
- **Getter** (`get_schedule`, `get_weather`, …) — append the model's
  function-call turn plus a `{"result": …}` function response, call again for
  the prose. The `{"result": …}` wrapper matches what AFC used to send; don't
  change its shape.
- **Terminal** — any tool result carrying `"user_notified": True`. Stop. The
  tool already sent the user their reply (the calendar writers ship a
  confirmation with a quip), so `_think` returns `""` and the caller's
  empty-response branch handles it. This is success, not an error.

Rules:
- **`user_notified` is a per-branch fact, not a property of the tool.**
  `add_calendar_event` both writes-and-notifies AND returns validation errors
  ("uid required — call `get_schedule` first") the model must see to recover
  from. Terminating on the tool's *name* would answer those with silence. Only
  the branches that actually messaged the user set the flag.
- Failure branches inside the calendar writers deliberately do NOT set it —
  `auto_write`/`auto_update` send their own failure note, but the model still
  gets the error so it can explain itself.
- A tool that raises, or a name the model invented, comes back as
  `{"error": …}` and the loop continues. Never crash the turn.
- `_TOOL_MAX_HOPS` bounds a runaway loop. Usage is summed across hops — each
  hop is its own billed request.

This refactor is what removed the "silent residue" bug: asked to produce nothing
after a calendar write, the model would answer with an empty Markdown fence or a
stray CJK token, which shipped to Telegram as the reply. There is no longer a
hop in which it can. Do not reintroduce a downstream filter for it; fix the loop
instead.

### Time and location are injected, not tools

`get_now` and `get_location` no longer exist. `_system_instruction()` appends the
authoritative wall-clock stamp to every profile (including CLASSIFY — the urgency
rubric is entirely relative) and the cached machine location to CHAT/COMPOSE. A
tool the model has to remember to call is a tool it can skip, and skipping the
clock is how "this Friday" resolved to a date out of the training set. Appended,
not prepended, so the persona stays a stable cacheable prefix.

Provider reality: the tool layer is Gemini-only. On the `ollama` provider
`_tools` is `None` and there is nothing for the dispatcher to narrow.

---

## LLM Processing Flow — **[TORN DOWN]**

*The LLM step is a logged no-op. `process_untagged_events` and
`extract_groupme_events` return immediately without touching their rows —
`processed` and `event_extracted` stay 0 deliberately, so the new tagger can
backfill the whole teardown-window backlog. Consequence: nothing is tagged
URGENT, so urgent alerts are silent, and GroupMe produces no approval cards.
The connector half of the flow below is untouched and still accurate.*

```
Connector fetches raw data
        ↓
Write raw record to events table (unprocessed)
        ↓
LLM evaluates record (CLASSIFY profile):
  - Assigns urgency (URGENT / SOON / NORMAL)
  - For Canvas announcements: determines if actionable
  - For GroupMe: considers group priority tier, extracts any event
        ↓
If URGENT → immediate Telegram interrupt (COMPOSE), set notified = 1
If an inferred calendar event → gated_write approval card
If briefing entry → sits in events table until next briefing
        ↓
Calendar backend ← receives all confirmed writes
```

Deterministic code only handles: HTTP requests, iCal parsing, raw SQL writes, API auth.

---

## Calendar Writes: which path gates — **[PARTLY TORN DOWN]**

*Both paths survive verbatim in `actions/calendar.py`. What changed is who
calls them. `auto_write` now has exactly one live producer, Canvas due dates
(silent, no Telegram); the `add_calendar_event` tool that was its other one is
deleted, so Friday can no longer write to the calendar from chat.
`gated_write` has no live producer at all — GroupMe extraction and media
extraction, its only two, are torn down. `confirm_pending` still works and
still ships a quip, so any card already in `pending_actions` resolves
normally. **The gate itself is untouched and inviolable.**

`actions/calendar.py` has two public modes, and the split is about who asserted
the fact, not how important it is.

- **`auto_write` — no gate.** Canvas due dates (the user's school published them) and the `add_calendar_event` tool (the user just said it out loud in chat; a confirmation card for something they explicitly asked for is friction, not safety). Friday replies with a one-line confirmation plus a quip.
- **`gated_write` — approval card, staged in `pending_actions`.** Anything Friday *inferred*: events extracted from GroupMe messages, and events extracted from an image or PDF the user sent. `confirm_pending()` performs the write when ✅ is tapped; the dashboard can also resolve these.

---

## Implementation Status

Phases 1–4 and 6 are built and running in production. What follows is the
current state, not a plan.

**Done — Phase 1 (Foundation):** SQLite schema + migrations, `system_state`
helpers, semaphore at the entry point, JobQueue-only scheduling, catch-up on
restart, LaunchAgents generated from templates by `macos_setup.py`, rumps
menubar, FastAPI web dashboard, persona in `AGENTS.md`.

**Done — Phase 2 (Read-only ingest):** Canvas iCal, weather, GroupMe, Google
Calendar iCal mirroring, location.

**Done — Phase 3 (Briefings & alerts):** morning/evening briefings with
timezone-window guards and missed-briefing catch-up, on-demand "brief me"
(composed in-process from a deterministic bundle), urgent interrupts.

**Done — Phase 4 (Writing & GroupMe):** calendar writes through the backend
dispatcher, gated and auto paths, manual work-shift entry via chat, GroupMe
polling with priority tiers and a per-group enable switch.

**Done — Phase 6 (Voice):** standalone `voice/listen.py` — wake word, clap
detection, push-to-talk, local Whisper, TTS, Telegram bridge. Never imported by
the core.

**Not built:**
- **Phase 5 (Drafting & sending)** — no `actions/groupme_send.py`, no Gmail drafts. Gmail stays deprioritized: no accessible API path for locked-down school accounts. The config block exists only to keep the shape stable.
- **Proactive due-date reminders (5/3/1 days)** — `notifications.reminder_thresholds` is written and editable in the dashboard, but no job consumes it. This is the largest gap between the config surface and behavior.

**Ongoing — Phase 7 (Hardening):** connector error recovery and observability.

**Torn down — the whole LLM layer.** Branch `llm-layer-teardown` removed the
tool layer, the dispatcher, the call profiles, persona assembly and every
prompt. The cost work that Phase 7 previously described (profiles +
dispatcher) is deleted along with it. What Friday currently does NOT do:
tag urgency, extract events from GroupMe or from images/PDFs, fire urgent
alerts, write to the calendar from chat, speak in any persona, or select a
quip by context. Chat replies and briefings still ship. This is the state the
rewrite starts from.

---

## Key Constraints & Rules for Claude Code

1. **Never remove or move the semaphore.** Top of `on_message()` in `telegram.py`. No exceptions.
2. **Never use a second scheduling library.** No `schedule`, no raw `apscheduler`, no background threads for timing. PTB `JobQueue` only.
3. **Never poll iMessage.** Not via AppleScript, not via `chat.db`, not via any method.
4. **Never write an *inferred* event without an approval gate.** Explicitly requested writes and Canvas due dates use `auto_write`; everything Friday deduced uses `gated_write`. See the calendar-writes section.
5. **Never hardcode a Python path in a LaunchAgent.** `macos_setup.py` renders the plists against the interpreter resolved on the running machine. Never `/usr/bin/python3`.
6. **Canvas uses the iCal feed.** Never HTML scraping. Use `icalendar`.
7. **The LLM processes all ingested data.** Never bypass it for urgency, filtering, or calendar decisions — even for clean structured data.
8. **The calendar backend is the event store.** Briefings and reminders read from it, not from the SQLite events table.
9. **Briefings run with tools OFF.** If a briefing is thin, expand `bundle_briefing_context` — never re-enable tools on that path. `bundle_briefing_context` survived the teardown intact precisely because it is the layer worth keeping.
10. ~~**The dispatcher fails open.**~~ **[TORN DOWN]** — no dispatcher. Reinstate this rule with it.
11. ~~**`TOOL_MANIFEST` must stay in sync with the registered tools.**~~ **[TORN DOWN]** — no manifest, no tools.
12. **SQLite is the operational backbone only.** No state.json. No vector store. No Redis.
13. **Voice is a standalone satellite.** Never imports from Friday's core.
14. **Friday does not edit its own Python source.** `self_edit.py` writes YAML only — learned quips and a whitelist of settings. The core is relaunched on exit by launchd/tray, so a syntax error would be a silent restart loop rather than a visible failure.
15. **All secrets** live in `friday_config.yaml` or environment variables. Never hardcoded. That file is gitignored; `friday_config.yaml.example` is the documented template.
16. **`compat.strftime()` for any format string with `%-`.** `%-d`/`%-I` are glibc-only and crash on Windows.

---

## Config

`friday/friday_config.yaml.example` is the canonical, commented template — read
it rather than a copy here. Startup hard-fails only on `telegram.bot_token`,
`telegram.chat_id`, and a Gemini key when `provider: gemini`. Every other block
is optional; an unconfigured connector is skipped, not an error.

Blocks worth knowing about:
- `dispatcher` — `enabled: false` restores pre-dispatcher behavior exactly (all tools, no extra call).
- `calendar.backend` — `apple` | `google`. Defaults to google on win32, apple elsewhere.
- `notifications` — the dashboard-facing mirror. `groupme_polling: false` is a real kill switch read by `poll_connectors_job`; the `agent` block stays canonical for the JobQueue and wins if the two disagree.
- `groupme.groups[].priority` — `high` (can interrupt) | `normal` (briefings only) | `muted` (ingested, never surfaced). `low` is the legacy spelling of `muted`.
- `voice` — read only by `voice/listen.py`, which does not reload it. Restart the voice agent after changing it.

---

## Persona (`AGENTS.md`) — **[TORN DOWN]**

*Nothing loads `AGENTS.md`. It stays in the repo and in the PyInstaller spec
as source material for the rewrite. Its headings are no longer an API —
`profiles._MEMBERSHIP` is deleted — so renaming one is currently harmless.
`quips.yaml` and `friday_voice.yaml` ARE still read, by `phrases.py`; quip
selection is now `random.choice`, which means a quip can contradict its event
(the failure mode the LLM index pick existed to prevent).*

`AGENTS.md` is the single source: the butler voice from the old `Soul.md` was
merged into it, and `phrases.py` reads the shared quip palette. Friday is
concise and direct, addresses the user as "sir", states its sources, and never
invents an event or a deadline.

Two things make this file load-bearing beyond prose:
- **Its headings are an API.** `profiles._MEMBERSHIP` keys off them. Renaming a heading in `AGENTS.md` without updating the table silently reroutes that section (fail-open to CHAT+COMPOSE, logged once).
- **Learned voice is separate.** Bundled quips live in `quips.yaml` (read-only in a frozen build); anything Friday learns at runtime goes to `friday_voice.yaml` under `paths.data_dir()`. Never append to the bundled file.

---

## Google Calendar Sync

Friday polls Google Calendar iCal subscription URLs and mirrors new events into
same-named Apple Calendars. **One-directional: Google → Apple only.** The secret
iCal URLs need no OAuth and no API key — just the URL, which makes them
credentials.

Apple Calendars must be created manually by the user first. Friday never creates
Apple Calendars on the apple backend; it only writes into existing ones. (The
google backend does auto-create, and skips gcal_sync entirely — Google already
*is* the event store there.)

- **Connector:** `connectors/gcal_sync.py`; writes via `actions/calendar.py`.
- **Deduplication:** the `synced_events` table, keyed on the iCal `UID` Google always provides.
- **Poll frequency:** every 15 minutes, alongside `poll_connectors()`.
- **No approval gate:** these are events the user already created themselves.
- **Out of scope:** deletion sync and update sync. A changed or removed Google event is logged, not mirrored.
- A failing iCal URL is logged and skipped. Never crash the poll.

---

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

Push-to-talk needs Accessibility and Input Monitoring, and those grants follow the
*running* process identity — the launcher must fork, never `execv`, or the .app identity
is thrown away and hand-added Settings grants stop matching.

If a process appears running via `launchctl print` but the menubar reports voice offline,
the menubar polls `/api/voice/status` every few seconds and may have cached an earlier
failed boot. Wait ~10 seconds or restart via the menubar's "Restart Voice" item.

## Location

`connectors/location.py` answers "where am I". It is no longer a tool — the
poll job calls `location.warm()` in an executor every 15 minutes, and
`_system_instruction` injects the *cached* fix into CHAT/COMPOSE prompts. The
prompt path never fetches: a cold lookup blocks for seconds. Before the first
warm the block is simply absent, which is the honest state.

It reports where the **Mac** is, not where the user is — there is no passive way
to read a phone's position from here, so the answer is "home" whenever the
machine is home and it does not move when the user does. The injected block
carries that caveat explicitly; a bare "Current location: X" in a system prompt
reads as the user's whereabouts, which is the exact claim this module forbids.

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

Fixes are cached for 5 minutes; an always-on Mac does not move.

## Packaging

Both platforms ship a supervised GUI process that owns the core, plus a
Tkinter first-run wizard (`setup_wizard.py`).

- **macOS** — `packaging/macos/` (`friday.spec`, `build.sh`, `make_icon.py`, `BUILD_MACOS.md`) and `.github/workflows/build-macos.yml` produce a `.app`/`.dmg`. `mac_app.py` is the bundle entry point: it runs the wizard on first launch, then supervises `Friday.app --core` and shows the menu bar. A source checkout does not need it — `menubar.py` plus LaunchAgents installed by `macos_setup.py` is the developer path. The bundle version comes from `$VERSION`, never hardcoded.
- **Windows** — `packaging/windows/` (spec, `installer.iss`, `build.ps1`, `BUILD_WINDOWS.md`) and `.github/workflows/build-windows.yml` produce `FridaySetup.exe` (Inno Setup around a PyInstaller onedir build), with Velopack for updates. The Google OAuth desktop client JSON is created once by the maintainer and bundled at build time.

## Windows Port (friend's build)

The same codebase runs on Windows. Architecture differences are isolated behind
small seams:

- **Calendar backend dispatch** (`calendars/backend.py`): config `calendar.backend`
  selects `apple` (JXA, `calendars/apple.py`) or `google` (`calendars/google_cal.py`,
  Google Calendar API + OAuth installed-app flow). Default is google on win32, apple
  elsewhere. All reads/writes — tools, briefings, Canvas due dates, gated writes — go
  through this dispatcher. On the google backend, gcal_sync is skipped and missing
  calendars are auto-created.
- **Paths** (`paths.py`): mutable state (config, db, logs, Google token, learned voice)
  lives in `%APPDATA%\Friday` on Windows/frozen builds, in the package dir on a macOS
  source checkout. Bundled read-only resources (AGENTS.md, quips.yaml, dashboard static)
  resolve via `paths.resource_path()` (PyInstaller `_MEIPASS`-aware). Never write through
  `resource_path()`.
- **compat.py**: `compat.strftime()` translates glibc `%-d`/`%-I` to Windows `%#d`.
  Also `IS_WINDOWS` and the listening-flag temp path.
- **Process model**: `tray.py` (pystray) supervises the core (`Friday.exe --core`) and
  auto-restarts it on exit. The dashboard's `/api/friday/restart` on Windows just raises
  SIGINT in-process; the tray brings it back. Quit sets a flag so the exit is final.
  No launchd, no services.
- **run_polling on Windows must NOT receive stop_signals** — Proactor loops have no
  `add_signal_handler`; friday.py only passes them on non-Windows.
- **Out of scope on Windows**: voice, menubar.py/rumps, iMessage (still banned everywhere).
