# Project Friday — Claude Code Instructions

> **⚠️ REBUILD IN PROGRESS — Phase III, branch `phase3-dispatcher`.**
>
> The LLM layer was torn down in full (`llm-layer-teardown`) and is being
> rebuilt in eleven steps. **Steps 1–3 have landed:**
>
> - **Step 1** — `friday/llm/`: the dispatcher, the profile registry, the
>   provider adapters. Every LLM call goes through `llm/dispatch.py::dispatch()`
>   and `llm/providers/gemini.py` is the only file importing `google.genai`.
> - **Step 2** — persona and prompt assembly: `AGENTS.md` parsed into
>   addressable sections, history as real turns, injected clock and location,
>   the CLASSIFY and COMPOSE profiles.
> - **Step 3** — the tool layer: `friday/tools/`, the registry, two read-only
>   calendar tools, the fact ledger, and the bounded turn loop in
>   `agent/turn.py`. CHAT can now read the calendar.
>
> See **The LLM Layer** and **The Tool Layer** below.
>
> **Not yet rebuilt:** the effects layer, permission cards and the first gated
> write (step 4). Sections describing those are marked **[STEP 4]**. Friday
> still cannot write to the calendar from chat, nothing is tagged URGENT, and
> quips are chosen at random rather than by context. That is the current
> expected behavior, not a bug.
>
> Do not rebuild a later step piecemeal while working on something else. Each
> step is its own task, ends with a running system, and is independently
> revertable. The build order is in `phaseiii.MD` §12.
>
> **Two traps that cost real debugging time — do not rediscover them:**
>
> 1. **`ToolCall.signature` is load-bearing.** Gemini 3.x returns an opaque
>    `thought_signature` on every function-call part and rejects the *next*
>    request with `400 INVALID_ARGUMENT` unless it is replayed alongside the
>    call. A tool call cannot be reconstructed from name and arguments alone.
> 2. **`resp.text` is empty whenever the response holds a function-call part.**
>    The adapter reads `candidate.content.parts` directly; empty means empty
>    only when there were no function-call parts either. Reverting to
>    `resp.text` makes every tool call surface to the user as "the model
>    returned no text".

## Codebase Navigation & Knowledge Graph
- **Search Strategy:** Before running broad `grep` or file searches across the repository, check `graphify-out/GRAPH_REPORT.md` to map out dependencies and locate relevant files.
- **⚠️ The Graphify snapshot is STALE as of the `phase3-dispatcher` branch.** It still maps `agent/tools.py`, `agent/dispatcher.py` and `agent/profiles.py` (deleted) and knows nothing of `friday/llm/` (added). Regeneration is a `/graphify` skill invocation, not a scripted repo step, so it has not been run. Re-run it before trusting the graph for anything under `agent/` or `llm/`.
- **Context Gathering:** Use the Graphify snapshot to identify component relationships first, then perform targeted reads on specific files.

## What is Friday?

Friday is a personal AI secretary running on an always-on Mac (and, since the Windows port, on a friend's PC). It ingests information from multiple sources (Canvas, GroupMe, Google Calendar subscriptions), manages the user's calendar, delivers proactive briefings and urgent alerts via Telegram, and asks for approval before writing anything it inferred rather than was told. The user interacts with Friday through Telegram, a local web dashboard, and a voice satellite.

Friday is **not** a simple chatbot. It is a structured, event-driven agent with a tool layer, a memory layer, a proactive alert system, and a cost-aware LLM call layer. *(The tool layer and the LLM call layer are currently torn down — see the banner above. The memory layer, alert plumbing and approval gate are intact.)*

---

## Core Architecture Principles

- **Every LLM call goes through the dispatcher.** `llm/dispatch.py::dispatch()` is the chokepoint; a direct provider call anywhere else is a bug however convenient. Below it, `llm/providers/` owns every SDK detail — one file per provider, selected by the `provider` config key.
- **The tool layer is read-only.** `friday/tools/` holds a registry, two calendar read tools, an executor and a fact ledger. **[STEP 4]** no tool may write, and there is no effects layer — `Effect` is an empty base class on purpose.
- **Tools declare coverage; the executor writes the ledger.** Tool code has no path to the ledger at all. See **The Tool Layer**.
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
- `llm/` — the call layer. `dispatch.py` (the chokepoint), `assembly.py` (prompt assembly), `persona.py`, `context.py`, `profiles.py`, `types.py` (the vocabulary — no SDK import), `providers/` (`base.py`, `gemini.py`, `ollama.py`). Nothing above this package names a model or an SDK.
- `tools/` — the tool layer. `types.py` (the contract), `registry.py`, `calendar_read.py` (the only two tools), `executor.py`, `ledger.py`, `scratch.py`. **Not** the deleted `agent/tools.py`.
- `agent/` — `turn.py` (the bounded tool loop), `core.py` (media intake only — PDF rasterization and byte/mime plumbing; it reaches no model today) and `briefings.py` (deterministic context bundling + plain renderers). `tools.py`, `dispatcher.py` and `profiles.py` are deleted; `agent/profiles.py` is NOT the same file as `llm/profiles.py`.
- `calendars/` — backend dispatcher plus the two implementations (`apple.py`, `google_cal.py`), and `eventtime.py`, which is the ONE place event timestamps are parsed: the JXA reader emits UTC with a Z, EventKit emits naive local, and a second implementation is correct on one machine and five hours wrong on another. Distinct from `actions/calendar.py`, which is the write API both backends sit behind, and from `connectors/apple_calendar.py`, which is the reader.
- `tests/test_tools.py` — plain asserts, no test dependency. `python3 tests/test_tools.py` from the package dir.
- `dashboard/` — a FastAPI package, not a Tkinter script.
- `memory/activity.py` — best-effort instrumentation writes. Never raises into the hot path.
- `self_edit.py` + `phrases.py` + `quips.yaml` + `friday_voice.yaml` — the narrow slice of itself Friday may rewrite at runtime. **[STEP 4]** quip selection is still `random.choice`; `phrases.random_quip()` and the dashboard's `/api/quips` read these. `self_edit.version()` and `update_setting()` have no caller and are kept for the rewrite.
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
    │           ├── query SQLite for conversation history
    │           ├── build LLMRequest(profile=CHAT)
    │           ├── run_in_executor → agent/turn.py::run_turn()   ← whole turn, one call
    │           │       ├── llm/dispatch.py::dispatch()
    │           │       │       └── llm/providers/gemini.py::complete()
    │           │       └── _TOOL_POOL → tools/executor.py::run()
    │           └── reply, keyed on result.error_kind
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

## The LLM Layer

Everything under `friday/llm/`. Built in step 1 of Phase III; the layers above
it arrive in steps 2–4. The full design is `phaseiii.MD` — this is the working
summary and the rules that must not be relaxed.

### Layer stack

Each layer may call downward and return upward. **No layer reaches past its
neighbor.** If a tool needs to send a message it returns an effect; it does not
import the Telegram channel. That single rule is what the whole rebuild exists
to buy — Phase II failed because Gemini's function-calling shape leaked into
every layer and because tools messaged the user mid-turn.

```
Channels          Telegram · Dashboard · Voice              ← Telegram only today
                  Own concurrency, surface formatting, transport
        ↓
Turn runner       Bounded tool loop, one deadline, effects       ← BUILT (agent/turn.py)
        ↓
Effects           Intent → side effects, ordered, gate first      [step 4]
Context           Deterministic pre-fetch → labeled blocks        [step 6; agent/briefings.py is the surviving half]
Policy            Gating, suppression, visibility                 [step 6]
Tools             Registry, preconditions, structured returns   ← BUILT (friday/tools/)
Persona           Section-addressable AGENTS.md                 ← BUILT (llm/persona.py)
        ↓
Dispatcher        Profiles, retry, budget, logging — THE chokepoint     ← BUILT
        ↓
Providers         Gemini adapter — the only file importing google.genai ← BUILT
```

### What is in the package

- **`types.py`** — the vocabulary: `Profile`, `LLMRequest`, `LLMResponse`,
  `Usage`, `ToolCall`, `ContextBlock`, and the `Finish` / `ErrorKind` literals.
  Frozen dataclasses, no behavior, **no SDK import** — this is what lets the
  rest of the codebase discuss a model call without knowing who answers it.
  `tool_calls`, `persona_sections` and `tool_scope` are declared and inert so
  steps 2–3 add a table entry rather than a field to every call site.
- **`profiles.py`** — the profile registry. **CHAT only.** The registry is
  authoritative: `dispatch()` re-resolves every request's profile *by name* and
  calls with the registry's copy, so a caller cannot hand-roll a `Profile` named
  CHAT that quietly uses a different model. `get()` raises on an unknown name —
  a typo'd profile is a bug, and falling back to CHAT hides it behind a bigger
  bill. CLASSIFY / COMPOSE / EXTRACT arrive with the persona layer.
- **`providers/base.py`** — the `Provider` ABC (`complete(request, profile)`),
  plus `render_prompt()` and `remaining_seconds()`. The profile is passed in
  already resolved: a provider that could look up a name could also disagree
  with the dispatcher about what CHAT means. Holds the marked, deliberately
  empty **PERSONA ASSEMBLY POINT**.
- **`providers/gemini.py`** — the only file in the repository that imports
  `google.genai`. Grep-enforceable, and worth actually grepping.
- **`providers/ollama.py`** — ⚠️ untested since the rewrite. It keeps the
  `provider: gemini|ollama` key working and, more to the point, proves the
  interface has no Gemini-shaped assumptions in it. **No fallback chain**: a
  dead Ollama is a dead provider, not a reason to silently spend money on
  Gemini.
- **`dispatch.py`** — resolve the profile, select the provider, set the
  deadline, call, retry what is worth retrying, log, return. It does not build
  prompts, does not know what a persona is, does not know what Telegram is.
  **Synchronous by design** — callers in async contexts run it in an executor.
  An async dispatcher puts a blocking SDK call back on the event loop, which is
  the exact shape that wedged the pipeline in the July 9 outage.

### Error taxonomy

A provider **never raises** for an API-level failure. It returns
`finish="error"` with an `error_kind`, and the channel keys its reply off that
— no reaching into the agent for a `_last_error` attribute.

| kind | means | dispatcher | user sees |
|---|---|---|---|
| `none` | success | — | the reply |
| `rate_limit` | 429 / quota — the API answered and refused us | retry, backoff (2s, 5s) | "I'm being rate limited, sir." |
| `transient` | 500/502/503/504 — the API failed on its own side | retry, backoff (0.5s, 1.5s) | "The model service is having trouble on its end, sir." |
| `network` | never reached the API — DNS, refused, dead socket | **no retry** | "I can't reach the model from this network." |
| `fatal` | anything else — bad request, auth, malformed response | no retry | the error line |

Rules:
- **`transient` must not be folded into `rate_limit`.** "Google is having a bad
  day", "you are over quota" and "this network blocks the API" are three
  different problems with three different answers. The dashboard and the
  reachability probes both have to tell them apart.
- **`network` fails fast.** Telegram is blocked on school Wi-Fi for ~7h a day;
  a blocked network must not burn the whole budget rediscovering that it is
  blocked. This same classification covers Telegram and OAuth later.
- **Classification is on the parsed status code**, never on a bare `"500"` found
  in the message text.

### The deadline, not nested timeouts

`dispatch()` sets `deadline = now + profile.timeout_s` at entry and threads it
down on the request. The provider clamps its own HTTP timeout to
`min(60s ceiling, time remaining)`; no retry at any level fires once the
deadline has passed. One number, not three constants kept in sync — nested
per-attempt timeouts multiply, a deadline cannot.

**The SDK client's own timeout is load-bearing.** Without it, a request in
flight when the Mac sleeps blocks its executor thread forever while holding the
Telegram semaphore — the July 9 outage. `asyncio.wait_for` cannot fix this: it
does not kill an executor thread. Only `HttpOptions(timeout=…)` does. The 150s
`wait_for` in `channels/telegram.py` is a backstop, not the budget.

### The transport redial

Exactly one, no backoff, only if the deadline allows, and only for a fault on an
*established* connection — read/write errors, resets, mid-request socket
timeouts. A sleep-killed socket redials instantly and succeeds.
`ConnectError` / `ConnectTimeout` are deliberately excluded: DNS failure and
connection-refused are genuine "cannot reach this network" signals and must
surface as `network` immediately. A redial on those is the slow-fail that makes
a blocked network painful.

### Logging

One `llm_exchanges` row per dispatch, carrying `profile`, `finish` and
`error_kind` alongside the tokens and latency, plus the running `system_state`
counters the menubar and `/api/status` read. All of it wrapped: instrumentation
never fails a request. A lost row beats a lost reply.

### Phase III invariants

From `phaseiii.MD` §11. Anything violating one is a bug regardless of what it
enables. **1, 2, 6 and 9 bind today.** 3, 4 and 5 bind the moment step 4 lands
and are the reason it is its own step. 7, 8 and 10 belong to layers not yet
started.

1. **Every LLM call goes through the dispatcher.** No direct provider calls anywhere else.
2. **Tools never send messages.** They return effects. *Binding now: `Effect` exists as an empty base class and no tool produces one, so the only way a tool could message the user is by importing a channel — which none does.*
3. **Permission cards are emitted first**, and nothing may delay, editorialize on, or bury one.
4. **A write is confirmed to the user only after the service confirms it back.**
5. **Every write carries a fingerprint** checked before writing and before retrying, so a client-side timeout on a server-side success cannot double-book.
6. **Persona voice applies only to conversational replies** — never cards, briefings, errors, or TTS.
7. **Reconcile before prune.** Always.
8. **Presence is always a value plus a timestamp.** Stale means unknown, never assumed.
9. **The dashboard is a channel adapter**, not a second chat implementation.
10. **No webhooks.** Polling only — webhooks break sleep/wake queuing and complicate the Windows build.

### Not yet rebuilt, and what that costs today

- **The effects layer, permission cards and writes (step 4).** No tool may
  write. `actions/calendar.py::gated_write` still has no live producer, so
  Friday cannot add a calendar event from chat.
- **Urgency tagging (the connector work).** `process_untagged_events` and
  `extract_groupme_events` are still logged no-ops, so nothing is tagged URGENT
  and GroupMe produces no approval cards. The CLASSIFY profile exists and is
  wired; the callers are not.
- **Quip selection is `random.choice`**, so a quip can contradict its event.

---

## The Tool Layer

Everything under `friday/tools/`, plus the loop in `agent/turn.py`. Built in
step 3. **Read-only** — there is no write tool and no effects layer until step 4.

### The files

- **`types.py`** — the contract. `ToolResult`, `ToolError`, `Coverage`, and the
  deliberately empty `Effect` base class.
- **`registry.py`** — the `@tool` decorator and schema derivation.
- **`calendar_read.py`** — `get_schedule` and `find_free_blocks`, the only two
  tools that exist.
- **`executor.py`** — validate, check preconditions, execute, record coverage.
- **`ledger.py`** — the per-turn fact ledger and precondition types.
- **`scratch.py`** — per-turn tool storage, carried across the executor's thread hop.

### Rules that must not be relaxed

1. **A tool returns a `ToolResult` or a `ToolError`.** Never a string, never
   `None`, never a raised exception for an expected failure. A tool returning
   prose has already decided how the answer reads, which puts persona in the
   tool layer where nothing can see it. The executor rejects anything else.

2. **Tools declare coverage in their return value; the executor writes the
   ledger; tool code cannot write it.** There is no module-level accessor in
   `tools/ledger.py` — no thread-local, no singleton, nothing to reach. The
   `Ledger` is created per turn by `agent/turn.py` and passed explicitly. A
   tool recording its own coverage is a tool the precondition is trusting to
   tell the truth about the very thing being checked. **A `ToolError`
   contributes no coverage** — a read that failed is not a read.

3. **The JSON schema is derived from the typed signature.** Types from
   annotations, per-parameter descriptions from `Annotated` metadata, required
   from the absence of a default. An unannotated parameter is a
   registration-time error. The docstring says *when* to call the tool and
   nothing else; registration warns above 200 characters.

4. **Missing parameters are caught in code, not asked for in the prompt.**
   `tools/executor.py` validates before execution. Phase II instructed the
   model to ask for what was missing, which works most of the time — and
   failing silently is the problem.

5. **Schemas stay provider-neutral.** The registry emits plain JSON-Schema
   dicts; `gemini.py` turns them into SDK types. A registry emitting
   `FunctionDeclaration` would make every tool Gemini-only.

6. **Nothing ambient crosses into a tool.** Tools execute in `_TOOL_POOL`, not
   on the turn thread, so a thread-local installed by the turn is invisible
   from inside a tool. The ledger and the scratch are *passed*. This was a real
   bug: the read cache never hit and every precondition failed closed, both
   silently.

7. **`find_free_blocks` computes gaps in Python.** The model never receives a
   raw event list and is asked to find the holes. This is the determinism
   boundary. All-day events do not block time — they are returned separately.

### The turn loop

`agent/turn.py::run_turn()`. Synchronous, like `dispatch()`; the channel runs
the whole turn in one executor call so the per-turn state belongs to it.

- **`max_tool_hops` counts rounds of tool execution, not dispatches.** A turn
  using every hop makes `max_tool_hops + 1` model calls. CHAT is at **3**.
- **One deadline for the whole turn**, set once and threaded into every
  dispatch. Hop 3 does not get a fresh budget. `dispatch()` takes the min of
  its profile timeout and any deadline handed to it.
- **`_MAX_PRECONDITION_FAILURES = 2`** ends the turn. A model that cannot
  satisfy a precondition twice is spinning, and every spin is a paid call.
- **Bounded by a for-loop, never recursion.** A recursive loop hides its depth
  in the stack and the bound stops being readable off the profile table.
- **Manual dispatch only.** `automatic_function_calling` is disabled on every
  tool-bearing call. The SDK's automatic mode runs the loop itself and returns
  only final text, which hides hop count and token cost and makes the deadline,
  per-tool timeouts, the ledger and the `tool_calls` log unenforceable.
- One `tool_calls` row per call, carrying `hop` and `outcome`.

### `Profile.tool_scope`

`None` means **no tools**, and the provider is handed no `tools` argument at
all — not an empty list. `__post_init__` rejects an empty tuple (it means no
tools while reading like it means something) and rejects a scope with
`max_tool_hops=0` (schemas paid for on every request and never used). CHAT is
`("read",)`; COMPOSE and CLASSIFY are `None`.

## LLM Processing Flow — **[STEP 4 / connector work]**

*The call path and the CLASSIFY profile both exist; the callers do not. The LLM
step here is still a logged no-op: `process_untagged_events` and
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

## Calendar Writes: which path gates — **[STEP 4]**

*Both paths survive verbatim in `actions/calendar.py`. What changed is who
calls them. `auto_write` now has exactly one live producer, Canvas due dates
(silent, no Telegram); the `add_calendar_event` tool that was its other one is
deleted, so Friday can no longer write to the calendar from chat. Step 3's
tools are read-only and produce no writes at all.
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

**In progress — Phase III, the LLM layer rebuild (11 steps, `phaseiii.MD` §12).**
`llm-layer-teardown` removed the tool layer, the old dispatcher, the call
profiles, persona assembly and every prompt. **Steps 1–3 have landed** on
`phase3-dispatcher`: the dispatcher and provider adapters; persona assembly,
real multi-turn history and the CLASSIFY/COMPOSE profiles; and the tool layer
with two read-only calendar tools and a bounded turn loop.

Friday can now answer questions about the calendar in persona, using real
tool calls. It still does NOT: write to the calendar from chat, tag urgency,
extract events from GroupMe or images/PDFs, fire urgent alerts, or select a
quip by context.

**Next is step 4** — the effects layer, permission cards and the first gated
write. It is where invariants 3, 4 and 5 start to bind, which is why it is its
own step.

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
9. **Briefings run with tools OFF.** Enforced structurally: `COMPOSE.tool_scope` is `None`, so the provider is handed no `tools` argument at all. If a briefing is thin, expand `bundle_briefing_context` — never give COMPOSE a scope. `bundle_briefing_context` survived the teardown intact precisely because it is the layer worth keeping.
10. **Every LLM call goes through `llm/dispatch.py`.** No direct provider call anywhere else, no second door for images or JSON, no "just this once" convenience path. See the LLM Layer section.
11. **`llm/providers/gemini.py` is the only file that may import a provider SDK.** If a layer above it needs to know something SDK-shaped, the abstraction is wrong — fix the type, not the import.
12. **SQLite is the operational backbone only.** No state.json. No vector store. No Redis.
13. **Voice is a standalone satellite.** Never imports from Friday's core.
14. **Friday does not edit its own Python source.** `self_edit.py` writes YAML only — learned quips and a whitelist of settings. The core is relaunched on exit by launchd/tray, so a syntax error would be a silent restart loop rather than a visible failure.
15. **All secrets** live in `friday_config.yaml` or environment variables. Never hardcoded. That file is gitignored; `friday_config.yaml.example` is the documented template.
16. **`compat.strftime()` for any format string with `%-`.** `%-d`/`%-I` are glibc-only and crash on Windows.
17. **A tool returns a `ToolResult` or a `ToolError`** — never a string, never `None`, never a raised exception for an expected failure.
18. **Tools never write the ledger.** They declare coverage in their return value; `tools/executor.py` records it. There is no accessor in `tools/ledger.py` to reach, and it must stay that way.
19. **Never pass turn state to a tool through a thread-local.** Tools run in a worker pool and will not see it. Pass it explicitly — this failed silently once already.
20. **Parse event timestamps only through `calendars/eventtime.py`.** The two Apple readers disagree about timezone spelling and a second parser is wrong on one of them.

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

## Persona (`AGENTS.md`)

Parsed by `llm/persona.py` into level-2 sections, mtime-cached, and assembled
by `llm/assembly.py` in front of the context blocks. **Its headings are an
API**: profiles address sections by Markdown heading, so renaming one without
updating `persona.SECTIONS` and the profile table silently reroutes it.

Failure is asymmetric on purpose. A profile naming a section outside
`SECTIONS` raises **at import** — that is a typo in Python. A section
`AGENTS.md` does not currently provide only **warns**: the file is
user-editable prose and Friday is an always-on daemon, so a missing heading
must never be a startup failure.

Current sections and who takes them:

| Section | CHAT | COMPOSE | CLASSIFY |
|---|:--:|:--:|:--:|
| IDENTITY | ✅ | ✅ | — |
| TIME | ✅ | ✅ | ✅ |
| VOICE | ✅ | — | — |
| FORMATTING | ✅ | ✅ | — |
| TOOL_POLICY | ✅ | — | — |
| URGENCY | — | — | — |
| DEFERRED | — | — | — |

- **COMPOSE does not take VOICE** — invariant 6. The butler wit belongs to
  conversational replies, not to briefings.
- **CLASSIFY does not take IDENTITY** — its sourcing rule was bleeding cited
  prose into what has to be a bare label.
- **`## DEFERRED`** holds the Phase II `TOOL_POLICY` text, describing tools
  that no longer exist. It is named in `SECTIONS` so the parser does not warn
  at every boot, and requested by no profile so it can never ship in a prompt.
  Step 4 rewrites it against the tools it actually ships.
- **URGENCY** is for the connector tagging work, not for chat.

CHAT's assembled persona is ~1,060 tokens. `TOOL_POLICY` is 133 of them; the
Phase II version was 995, which is what a docstring-and-prose budget looks like
when nobody counts it.

**Learned voice is separate.** Bundled quips live in `quips.yaml` (read-only in
a frozen build); anything Friday learns at runtime goes to `friday_voice.yaml`
under `paths.data_dir()`. Never append to the bundled file. Quip *selection* is
still `random.choice` — **[STEP 4]** — so a quip can contradict its event.

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

**The daemon now HAS the grant** (`kTCCServiceCalendar` → allowed for the
interpreter in the LaunchAgent), and reads through EventKit. The log line to
check is `Apple Calendar reads using EventKit` versus `EventKit access not
granted (status 4)`. Status 4 is `writeOnly`, not "denied".

**⚠️ TCC attributes to the RESPONSIBLE process, not the binary you invoked.**
A probe run from a shell is attributed to the terminal app, so
`authorizationStatusForEntityType_` reports whatever *Terminal* was granted —
which read `writeOnly` and returned zero events while the daemon was
simultaneously on the EventKit fast path. **Never conclude anything about the
daemon's calendar access from a command-line probe.** Read the daemon's own log
line, or measure through a real turn's `tool_calls.duration_ms`. This cost real
time once already.

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
