# Project Friday — Claude Code Instructions

> **Naming: JARVIS is the preferred name going forward.** The persona was
> renamed from Friday to JARVIS across every user-visible surface — the
> model's own identity, dashboard UI, notifications, menu bar/tray/Dock
> chrome and logos, TTS, and default wake phrases. New user-facing text
> (replies, UI strings, docs prose) should say JARVIS.
>
> **In code, `Friday` and `JARVIS` are interchangeable and neither needs to
> change.** The package directory (`friday/`), `friday_config.yaml` and every
> config key, `friday_memory.db`, launchd labels (`com.friday.*`), Python
> identifiers, log file names and logger names, and this project's own name
> ("Project Friday") are untouched by the rename and are not being migrated —
> see the rename commit on `phase3-dispatcher` for the exact split. Do not
> rename an internal to "finish the job"; that was a deliberate, load-bearing
> decision, not an oversight.

## Working with the user

**Do not verify a change with a headless browser or by booting a dev/preview
server, unless explicitly asked.** The user checks visually themselves and
will report back if something looks wrong. Default to static verification —
syntax checks, unit tests, reading the diff — and stop there. This holds for
every prompt, not just the one where it was said.

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
> - **Step 4** — the effects layer: `friday/effects/` (the runner and the card
>   lifecycle), `friday/policy/gating.py`, `add_calendar_event`, `recent_writes`,
>   and quips grouped by tool and outcome. Friday can write to the calendar from
>   chat again, behind a permission card.
>
> - **Step 5** — the second channel: `effects/entry.py` (the one door effects
>   go out of), `channels/base.py` (the contract), `channels/conversation.py`
>   (one user message, any channel), `channels/dashboard.py`, SSE, cards in
>   the browser, dashboard auth, a PWA manifest, and the interrupt path.
>   **The dashboard is now a full conversational channel and works with the
>   Wi-Fi off.**
>
> See **The LLM Layer**, **The Tool Layer**, **The Effects Layer** and
> **The Channel Layer** below.
>
> **Step 5 is complete.** Both surfaces run the identical turn path, share one
> transcript, and resolve the same permission cards.
>
> - **Step 6** — cleanup and consolidation, with no user-visible payoff and no
>   new capability. `TurnResult.text` is now `model_text` (it was never the
>   same thing as `Reply.text`); the failure-sentence table moved down to
>   `channels/base.py` where a channel can own its own phrasing; injected
>   context has one producer and one formatter (`llm/context.py`), used by
>   chat and briefings alike; `policy/visibility.py` and
>   `policy/suppression.py` sit beside the gate; and `tools/preconditions.py`
>   holds the definitions update and delete will be registered with.
>
> **Step 6 is complete.** See **Context and Policy** below.
>
> - **Step 7** — the router: `friday/router/`. Three tiers between a message
>   and a model. Tier 1 is pattern matching with no model and no network and
>   catches **31.1% of all real traffic**; tier 2 is one constrained CLASSIFY
>   call returning a plan shape; tier 3 is no plan at all, which is the old
>   path byte for byte. A plan narrows the profile's tool scope and hop budget
>   and can never widen either, and `READ_THEN_WRITE` structurally cannot reach
>   its write before its read.
>
> **Step 7 is complete.** See **The Router** below.
>
> **Next is step 8** — the to-do layer, which is what
> `policy/suppression.py`'s windowed briefing-echo and completed-item rules
> were built for.
>
> **One deferred item carries forward from step 5.** Resolving a card in the
> dashboard does not edit the Telegram message it was sent as — that needs a
> `message_id` column and an `editMessage` call, and the value is cosmetic
> because the Telegram tap already fails closed with "That one's already done,
> sir". Deliberately deferred, not forgotten.
>
> **Tailscale is not installed on this machine, so the dashboard still binds
> to 127.0.0.1.** Auth landed anyway and covers every route. See
> **The Channel Layer → Binding beyond loopback** for the exact edit that
> remains.
>
> **Not yet rebuilt:** urgency tagging and the connectors that produce inferred
> facts (step 6 onward). Nothing is tagged URGENT and GroupMe produces no
> approval cards. `Channel.notify` is implemented on both channels and has no
> producer yet — the alert that will call it arrives with the tagger. That is
> the current expected behavior, not a bug.
>
> **There are no update or delete tools.** `add_calendar_event` is the only
> write. The precondition machinery and `policy/gating.py`'s AUTO cells exist
> and are tested, but have no live producer until updates land.
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
> 3. **Nothing may be written to `conversation_history` as the assistant's turn
>    after a permission card.** Three encodings were tried and the model said
>    all three back to the user verbatim: a `[permission card sent]` marker, the
>    card's own text (which also caused it to re-propose old events), and a
>    past-tense "I put a confirmation card in front of you for X". History rows
>    are examples, and there is no phrasing of a non-reply that is a good
>    example of a reply. The assistant's turn for a gated write is its
>    **outcome**, written by `effects/pending.py` when the user taps.
>    Step 5 hardened this: prose after a card is now suppressed even when the
>    model *does* produce some, because the branch enforcing the rule only ran
>    when the model happened to say nothing. The first card the dashboard ever
>    produced was followed by the model narrating its own tool call, in
>    Chinese.
> 4. **A calendar write is verified through the door it went out of.**
>    `calendars/backend.py` calls the backend's `event_exists()`, not the
>    ordinary reader. The reader applies `agent.briefing_calendars` (a different
>    question), and on macOS it reads through EventKit while writes go out
>    through JXA — measured, a JXA read-back sees a write in 0.57s and an
>    EventKit read still cannot see it minutes later.

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
- **Tools never send messages; they return effects.** `effects/runner.py` is the only code that calls a channel on a tool's behalf, and it runs `SendPermissionCard` first, unconditionally. Invariant 3 is a stable sort inside one function rather than a rule anyone has to remember. `grep -rE "^\s*(from|import) channels" friday/tools/ friday/effects/` must stay empty.
- **Reads declare their own ledger records; writes do not.** A read states what it covered and the executor believes it. A write's record is synthesised by the executor from the service's confirmation, and `committed` comes from the same object — so the two cannot disagree. A failed *write* still records an attempt, because it may have landed server-side. See **The Tool Layer**.
- **There are two conversational channels, and one path through them.** Telegram and the local web dashboard (`dashboard/`, 127.0.0.1:5174) both run `channels/conversation.py::handle()` — same gate, same history, same effects, same cards. Telegram is still the primary UI, but it is blocked on school Wi-Fi for ~7h a day, which is what the dashboard exists for. The voice satellite (`voice/`) is not a third channel: it bridges speech back in as an ordinary Telegram message.
- **PTB JobQueue is the only scheduler.** `python-telegram-bot` is fully async. Never introduce a second scheduling library (`apscheduler`, `schedule`, `while True: sleep()`). All scheduled jobs register directly on `application.job_queue`. The dashboard's web server runs inside that same loop — it is not a second process.
- **One gate, acquired first.** `channels/conversation.py::TURN_GATE` is the process's single `asyncio.Semaphore(1)`, taken at the top of `handle()` before any SQLite query, any context assembly, any model call. It moved out of `telegram.py` in step 5 because a gate owned by one channel serializes that channel against itself while letting a dashboard turn and a Telegram turn interleave against the same `conversation_history`. **Never add a second one.**
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
- `channels/` — `base.py` (the contract: send, send_permission_request, notify), `conversation.py` (**one user message on any surface — the gate lives here**), `telegram.py` (transport only), `dashboard.py` (the dashboard channel). A new channel adds a file here and calls `conversation.handle()`.
- `tests/` — plain asserts, no test dependency. `python3 tests/test_tools.py` from the package dir; likewise `test_entry`, `test_channels`, `test_conversation`, `test_effects`, `test_pending`, `test_gating`.
- `dashboard/` — a FastAPI package, not a Tkinter script. `server.py` (routes), `stream.py` (SSE fan-out), `auth.py` (the shared token), `static/` (plain files, no build step).
- `memory/activity.py` — best-effort instrumentation writes. Never raises into the hot path.
- `self_edit.py` + `phrases.py` + `quips.yaml` + `friday_voice.yaml` — the narrow slice of itself Friday may rewrite at runtime. `quips.yaml` is grouped **tool → outcome → quips**; `phrases.quip_for(tool, outcome)` selects, and an empty group means silence rather than a borrowed line. The effects runner appends the quip, never the tool. `self_edit.version()` and `update_setting()` have no caller and are kept for the rewrite.
- `effects/` — `entry.py` (**the only caller of the runner**; welds history logging to the runner call), `runner.py` (ordering: cards first, then messages, then the rest) and `pending.py` (the card lifecycle: stage, confirm with the STORED arguments, cancel, expire, re-propose when stale).
- `router/` — what to do with a message before spending a model call on it.
  `plans.py` (the five plan shapes, and the narrowing rules), `fastpath.py`
  (tier 1: patterns, no model, works offline), `classify.py` (tier 2: one
  CLASSIFY call, constrained enum), `clarify.py` (the two-round cap). Nothing
  here sends anything; `channels/conversation.py` calls it and
  `agent/turn.py` executes the plan.
- `policy/` — three modules, three questions, none of which act. `gating.py` ("does this need a card?"), `visibility.py` ("may this be shown here at all?"), `suppression.py` ("was this already said recently enough?"). Visibility does not depend on when you ask; suppression does — that is why they are not one predicate.
- `tools/preconditions.py` — the precondition tuples update and delete will be registered with, written a step early because the machinery has never had a live consumer. **Neither tool exists.**
- `memory/writes.py` — `recent_writes`, the ten-minute fingerprint ledger that stops a timed-out write being retried into a duplicate.
- `paths.py` and `compat.py` — the cross-platform seams.
- Entry points differ per platform: `friday.py` (core), `mac_app.py` (packaged .app supervisor), `tray.py` (Windows), `menubar.py` (rumps, source checkout), `macos_setup.py` (renders LaunchAgent templates), `setup_wizard.py` (first run, both platforms).

---

## SQLite Schema

Defined in `friday/memory/db.py` — read it there rather than trusting a copy.
Two families of table:

- **Operational** — `system_state`, `conversation_history`, `events`, `last_seen`,
  `pending_actions`, `synced_events`, `recent_writes`.
  - `conversation_history.channel` records which surface a line came from
    ("telegram", "dashboard", empty for rows predating the second channel).
    **A record, never a filter** — see the Channel Layer.
  - `pending_actions` carries `tool_name`, `arguments_json`, `proposal`,
    `expires_at`, `turn_id`, `resolved_at`. Statuses: `pending`, `confirmed`,
    `cancelled`, `expired`, `failed`, `superseded`.
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
    ├── MessageHandler  → telegram.py::on_message()   ─┐  transport only
    ├── POST /api/chat  → DashboardChannel            ─┤  (3 lines each)
    │                                                  │
    │   both call ─────────────────────────────────────┘
    │     └── channels/conversation.py::handle()
    │           └── async with TURN_GATE   ← THE gate, before everything
    │                 ├── pause check
    │                 ├── router/fastpath.py::answer()   ← TIER 1: no model at all
    │                 │       └── hit? send, log, return. 31% of traffic ends here.
    │                 ├── run_in_executor → router/classify.py::classify()  ← TIER 2
    │                 │       └── one CLASSIFY call → a Plan, or None (= TIER 3, as before)
    │                 ├── router/clarify.py::guard()   ← two rounds, then answer
    │                 ├── query SQLite for conversation history (unfiltered)
    │                 ├── build LLMRequest(profile=CHAT)
    │                 ├── run_in_executor → agent/turn.py::run_turn(request, conn, plan)
    │                 │       ├── llm/dispatch.py::dispatch()
    │                 │       │       └── llm/providers/gemini.py::complete()
    │                 │       └── _TOOL_POOL → tools/executor.py::run()
    │                 ├── run_in_executor → effects/entry.py::deliver()  ← cards first
    │                 │       └── effects/runner.py::run()
    │                 └── channel.send(reply), keyed on result.error_kind
    │
    ├── CallbackQueryHandler   → approval-card taps ─┐
    ├── POST /api/pending-...  → approval-card taps ─┤  both → effects/pending.py
    │                                                 │      → effects/entry.py
    │   (the channel that handled the tap answers it) ┘
    │
    ├── dashboard web server (same loop, 127.0.0.1:5174)
    │     └── GET /api/stream → dashboard/stream.py (SSE)
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
it landed in steps 2–4. The full design is `phaseiii.MD` — this is the working
summary and the rules that must not be relaxed.

### Layer stack

Each layer may call downward and return upward. **No layer reaches past its
neighbor.** If a tool needs to send a message it returns an effect; it does not
import the Telegram channel. That single rule is what the whole rebuild exists
to buy — Phase II failed because Gemini's function-calling shape leaked into
every layer and because tools messaged the user mid-turn.

```
Channels          Telegram · Dashboard                    ← BUILT (channels/)
                  Transport ONLY. base.py is the contract.
        ↓
Conversation      One user message, any channel. THE gate. ← BUILT (channels/conversation.py)
        ↓
Turn runner       Bounded tool loop, one deadline, effects       ← BUILT (agent/turn.py)
        ↓
Effects           Intent → side effects, ordered, card first    ← BUILT (effects/)
                  entry.py is the ONLY door in.
Context           Deterministic pre-fetch → labeled blocks        [step 6; agent/briefings.py is the surviving half]
Policy            Gating, suppression, visibility               ← BUILT (policy/gating.py)
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
`("read", "write")`; COMPOSE and CLASSIFY are `None`.

**This said `("read",)` until step 7 and the code was right.** CHAT gained
`"write"` when `add_calendar_event` landed in step 4 and this line was not
updated — a documentation error rather than a behavior one, and worth
recording because it is the exact field the router now narrows. `"internal"`
is still absent, deliberately: `commit_calendar_event` carries that scope and
must never appear in a prompt.

**A router plan narrows this; it can never widen it.** `LLMRequest.tool_scope`
is INTERSECTED with the profile's in `llm/assembly.py::build_tools`. See
**The Router** below for which one wins and why.

## LLM Processing Flow — **[NOT YET REBUILT — connector work]**

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

## The Effects Layer

Everything under `friday/effects/`, plus `friday/policy/gating.py`. Built in
step 4. This is the layer the whole rebuild was designed around.

**A tool declares what should happen; the runner makes it happen.** In Phase II
tools sent Telegram messages themselves, mid-turn, then set flags on the agent
object so the channel would know — and the model had to be talked into staying
quiet afterward, in prose, which it did not reliably do. Every bug in that class
dies with this change.

### Ordering is the point

`effects/runner.py` executes a turn's effects in a fixed order:

1. `SendPermissionCard` — always first, unconditionally
2. `SendMessage`
3. everything else

Invariant 3 ("cards are emitted first, and nothing may delay, editorialize on,
or bury one") is a stable sort inside one function rather than a rule at every
call site. `tests/test_effects.py` proves a card emitted LAST in a batch runs
FIRST. `channels/telegram.py` runs the whole batch before the model's own reply,
which puts the batch ahead of the prose.

An unknown effect sorts last rather than raising, and a failing effect does not
stop the ones behind it — a batch is not a transaction, because a message
already sent cannot be recalled.

### The card is a stored tool call

`add_calendar_event` proposes; `commit_calendar_event` writes. Two registrations
rather than one tool with a bypass parameter, because the registry derives every
schema from the signature — a bypass parameter is one **the model can see and
set**. `commit_calendar_event` has scope `("internal",)`, which no profile's
`tool_scope` intersects, so it never appears in a prompt.

`pending_actions` carries the tool, its arguments, the card text, the proposing
turn and an expiry. **Confirm runs the stored arguments** — no model is
consulted on that path. Re-extracting them means the user approves one event and
receives another.

### Two thresholds, because they answer different questions

| | key | default | on tap |
|---|---|---|---|
| **TTL** | `agent.pending_action_ttl_minutes` | 1440 (24h) | refused out loud, nothing runs |
| **Stale** | `agent.pending_action_stale_minutes` | 30 | **re-proposed**, nothing runs |

The TTL is long deliberately: a card sent at 11pm has to survive until morning,
and shortening it would trade that real case for a rarer one. But a card that
has sat for hours is the one that gets tapped reflexively, and the model
occasionally proposes a card nobody asked for — so past the stale threshold one
tap is no longer enough.

**A stale card is re-proposed, not refused.** The old row is resolved
`superseded`, a fresh row with **the same stored arguments** is staged, and a
new card goes out saying how old the original was. The second confirmation is
therefore a tap on a *different button*, which is the entire point: a flag on
the row honoured by the next tap would be satisfied by exactly the reflex it
exists to catch.

**A resolved card is refused out loud.** It could never execute twice — the
status check has always been there — but the silence was the bug. A card
confirmed in the dashboard leaves a live-looking button in Telegram, and a tap
that does nothing visible reads as broken and earns another tap.
`pending.refusal_message(status)` is the one wording, used by both channels;
the dashboard's 409 carries it as `detail`.

The confirm path arrives from a callback, has no turn, and builds a fresh empty
ledger — honest, because the reads that justified the proposal belonged to a
turn that ended. It goes out through `effects/entry.py` like everything else.

### One door: `effects/entry.py`

**`deliver(effects, channel, conn)` is the only caller of `effects/runner.py`.**
Grep-enforceable and worth grepping.

It exists because of what has to happen *alongside* the runner call and is not
part of it: every sentence that reaches the user has to reach
`conversation_history` too, or the model's next request is built without it.
That obligation used to be discharged twice, differently — `telegram.py` wrote
history inline several branches later and never logged a tool's own
`SendMessage`; `pending.py` wrapped its channel. Neither was reusable, and the
dashboard was the third caller.

It takes effects and a channel and nothing else. **No request, no deadline, no
ledger** — the confirm path has none of the three, and requiring them would
mean fabricating a turn around a settled decision.

`entry.log_history()` is the only `conversation_history` write under
`effects/` and the only one any channel makes. Cards are never logged as prose.

### Gating

`policy/gating.py`:

| | Reversible (create, update) | Irreversible (delete) |
|---|---|---|
| **User stated the fact** | AUTO | GATED |
| **Friday inferred it** | GATED | GATED |

`AUTO_UPDATE` additionally requires the target to have been proven by a read
**in the same turn**; a target from the model's memory is an inference about
which event was meant.

**`add_calendar_event` overrides this to GATED, deliberately.** Everything
reaching it today is user-stated, so the table says AUTO. The override is at the
wiring site with its reason: the card carries the invariant this step exists to
make structural. AUTO gets exercised when update and delete land.

### Idempotency

`memory/writes.py` / `recent_writes`: a fingerprint of title, day, start time and
calendar, with a ten-minute TTL. Checked before every write, and therefore
before every retry, since a retry reaches the same line. **The check is local**
because the case it exists for is a write whose service call timed out — asking
the service again is asking the thing that just failed to answer. `reserve()` is
called *before* the write and starts the row as `unknown`; a `refused` outcome
deletes it, since refused means it did not happen.

### What the model must not be shown

See trap 3 in the banner. Nothing goes in the assistant slot after a card.


## The Channel Layer

Everything under `friday/channels/`, plus `dashboard/` as its transport host.
Built in step 5. **Two channels, one path.**

### The files

- **`base.py`** — the contract: `send`, `send_permission_request`, `notify`,
  plus a `name`. A `runtime_checkable` Protocol with abstract methods, so duck
  typing still works (the tests and `entry.HistoryChannel` rely on it) while a
  real channel that forgets a method fails at *construction*. It lived in
  `effects/runner.py`'s docstring until now, which is where a contract goes to
  be almost true. It also holds `DEFAULT_FAILURE_TEXT` and `failure_text_for()`
  — **the taxonomy is shared, the words are the channel's**. `failure_text` is
  deliberately **not** a Protocol member: a fourth member breaks `isinstance`
  for every duck-typed channel, which is exactly what happened when it was
  tried the other way round. Neither channel overrides it yet.
- **`conversation.py`** — `handle(text, channel, conn, config)`. The gate, the
  pause check, the history window, the request, the executor hop, the effects,
  the reply selection, the history writes. **This is the whole of a user
  message on any surface.**
- **`telegram.py`** — transport. `on_message` is three lines.
- **`dashboard.py`** — the dashboard channel. Buffers each send as an event and
  hands it to a sink.
- **`dashboard/stream.py`** — SSE fan-out.
- **`dashboard/auth.py`** — the shared token.

### Rules that must not be relaxed

1. **A new channel implements `channels/base.py` and calls
   `conversation.handle()`.** It does not read history, build a request, call
   the model, or decide what to say. If a channel duplicates anything from
   another beyond transport, the pipeline has forked — invariant 9.

2. **One gate.** `conversation.TURN_GATE`. Never a second `Semaphore(1)`
   anywhere.

3. **`conversation_history.channel` is a record, never a filter.** Every turn
   reads the window unfiltered. A message typed in the dashboard has to be in
   scope when the user asks about it over Telegram an hour later — they are one
   conversation with one person, and a window split by surface would make
   Friday forget things for a reason the user cannot see.

4. **The channel that handled a tap is the channel that answers it.** The
   dashboard used to build a send-only `TelegramHandler` to resolve a card, so
   a card confirmed in the dashboard sent its confirmation to Telegram — which
   on school Wi-Fi, the whole reason the dashboard exists, went nowhere.

5. **`notify` is literal.** Invariant 6. Never persona, never a quip. It is an
   interrupt, not a reply.

6. **Cards carry the same `pending_actions` key on every surface.** That is
   what makes a card confirmed in one resolve in the other. Both taps go
   through `effects/pending.py`; the second one, wherever it comes from, is
   refused out loud.

### The dashboard, specifically

`GET /api/chat/history`, `POST /api/chat`, `GET /api/stream`, and the existing
`/api/pending-approvals` endpoints. The chat route constructs a
`DashboardChannel` and calls `conversation.handle()` — there is nothing else in
it, which is the enforcement of rule 1.

**SSE, not WebSocket** (`dashboard/stream.py`). Only one direction is needed;
the browser already has POST. It reconnects natively when the Mac sleeps.
`publish()` is called from the executor threads effects run in, so it crosses
back via `call_soon_threadsafe` — touching an `asyncio.Queue` from a worker
thread is a race with no error message. Subscriber queues are bounded and a
slow subscriber is **dropped, never awaited**: a suspended background tab must
not become backpressure on a turn.

**No replay buffer, no `Last-Event-ID`.** Every event is also in
`conversation_history` or `pending_actions`, and every route that emits one
also returns it. A browser that missed the stream recovers by reading. A replay
log would be a second, worse copy of the database.

**Tool proposals are not editable in the dashboard.** The server has refused
that since step 4 (an edit that drops a field changes what was approved); the
Edit button was still rendered and 400'd every time. It is gone for
`tool_call` rows and intact for the dead `calendar_add` ones.

### Auth

`dashboard/auth.py`. One user, one token, one cookie — no user table, no
sessions, no login form. Three presentations, one door:

| | used by |
|---|---|
| cookie `friday_auth` | the browser, after the first visit |
| header `X-Friday-Token` | menubar, tray, `mac_app.py` |
| query `?token=…` | the first visit — sets the cookie, then **redirects** |

**Every route requires it**, including the SPA, `app.js`, the stream and the
card endpoints. `/api/config` alone returns the Telegram bot token and the
Gemini key in plaintext. Only the favicon, the manifest and the PWA icons are
open — an install prompt fetches icons from the OS installer's context, which
carries no cookie, and a 401 there means no home-screen icon and no error
anywhere.

The token is **generated on first boot**, written back to
`friday_config.yaml` under `dashboard.auth_token`, and logged once as a
ready-made URL. There is no blank default and **no disable switch**: a
dashboard with auth off is a dashboard that is open the moment the bind
changes.

Comparison is `secrets.compare_digest`. The bind is loopback today; the code is
not written for today.

### Binding beyond loopback — **[NOT DONE: Tailscale is not installed]**

Auth landed first on purpose. The bind change is a separate, small edit and
this is exactly what it needs:

1. Install Tailscale and enable MagicDNS (**the user's job, not Claude's**).
2. `dashboard/server.py::start_server()` — resolve the tailnet address at
   startup and pass it as `host`. Bind to **that address specifically, never
   `0.0.0.0`**: nothing on school Wi-Fi should be able to see the port at all,
   which is tighter than a firewall rule.
3. Bind loopback **as well**, or point `menubar.py` / `mac_app.py` /
   `tray.py`'s `127.0.0.1:5174` at the tailnet name — a socket bound only to
   the tailnet address is unreachable from `127.0.0.1`, and those three
   supervisors poll it.
4. Nothing else. Auth, the cookie flags and the open-path list are already
   correct for a non-loopback origin. `secure=False` on the cookie is
   deliberate: a tailnet HTTP origin has no TLS, and a `Secure` cookie would
   simply never be sent. Confidentiality is Tailscale's job.

### The interrupt path

`notify` has **two doors on the dashboard, because neither is enough alone**:

- **macOS `osascript display notification`** — reaches the user when the app is
  not running, which is the case the dashboard exists for. It does **not**
  click through: the notification is attributed to osascript, so clicking
  activates that. Accepted rather than worked around.
- **A Web Notification raised by the page** from the stream event — belongs to
  the dashboard window, so `onclick` focuses it. Needs the app open.

The osascript body is **escaped, not sanitised**: it is Friday's own text, but
text the model may have influenced, and an unescaped quote ends the AppleScript
literal. Permission is requested on a user gesture (sending a message), because
browsers refuse the request outside one and the refusal is permanent.

`notify` has its first producer: `friday.py`'s Canvas-health check calls it
directly (constructing a `DashboardChannel` from the dashboard's broadcaster)
when the Canvas REST token dies, latched in `system_state` so it fires once
per outage and clears on recovery. **The tagger's urgent-alert path is still
unbuilt** — that producer arrives with step 8, separately.


## Context and Policy

Built in step 6. No new capability — this is the consolidation steps 7 through
11 all depend on, and every one of them would otherwise have grown its own
copy.

### One producer, one formatter

`llm/context.py` owns both.

- **`format_context_block(label, content)`** and **`render_blocks(blocks)`** —
  the ONE rendering of an injected context block. Used by `llm/assembly.py`
  for chat and by `agent/briefings.py` for the briefing bundle and all three
  composers.
- **`time_block(config)`** / **`time_block_at(now, tz)`** — the ONE clock.
  `time_block_at` exists so a briefing renders the instant its bundle was
  captured at rather than a second read at format time; on the JXA path those
  can be eighty seconds apart.

There were **four** renderings of the same idea before this: assembly.py's
inline f-string, briefings' `format_briefing_context`, its `_render_sections`,
and its `_header`. All agreed by coincidence and were editable independently.
The router would have been the fifth.

The per-slot section list collapsed the same way — `_bundle_sections()` is now
the single list, shared by the injected context and all three composers, which
differ only in their title.

**Briefings gained the location block** (weather is a location-dependent claim
and the composer could not see where the machine thought it was) and **lost
the "use this date verbatim" instruction line** — which is not lost: it is the
persona's TIME section, which COMPOSE takes. It was duplicated in prose there
because at the time briefings assembled their own prompt and there was no
persona layer to hold it.

**What stays in `agent/briefings.py`: the bundle.** Deciding what a briefing
needs to pre-fetch, and guarding each source so one dead connector cannot sink
the whole thing, does not belong in a module every chat turn imports.

**INJECT, NEVER FETCH.** The injection site is `llm/dispatch.py`, commented at
length because that is the line a future change would undo. These are memory
reads on a path holding the one turn gate — `location.fetch()` can block ~25s —
and they must never become tool calls, because a model that has to *ask* what
day it is can decline to, and then writes an event into the wrong week. This
applied to the tool layer in step 3 and applies unchanged to the router.

### Policy: three questions, three modules

| module | question | depends on when you ask? |
|---|---|:--:|
| `gating.py` | does this need a card? | no |
| `visibility.py` | may this be shown here at all? | no |
| `suppression.py` | was this already said recently enough? | **yes** |

Kept apart deliberately. Folded together they become one predicate that is
false for two unrelated reasons, and the first bug report is "why did it stop
telling me about X" with no way to tell which rule caught it. **All three
decide and act on nothing** — no queries, no writes, no clock read of their
own.

**What was moved (real, wired):**

- Which GroupMe tiers a briefing surfaces. This was two literal `LIKE
  '%[priority=high]%'` patterns in a SQL string with **no link at all** to
  `connectors/groupme.py::PRIORITIES` — renaming a tier would have turned the
  filter into a pass-through and started surfacing muted groups. `policy/` may
  not import `connectors/`, so the tier names are restated and
  `tests/test_policy.py` asserts they are a subset of the connector's list.
- Which Canvas urgencies a briefing carries. Byte-identical copies in
  `agent/briefings.py` and `dashboard/server.py`.
- The already-alerted latch, named in `friday.py`'s alert job.

**What was built with no consumer** (to-dos, step 8 — and this is stated
rather than hidden, because inventing rules now means step 8 inherits
decisions it never made):

- The **windowed** briefing-echo rule: an item surfaced in a briefing within N
  hours has its **standalone reminder** suppressed and **stays on the list**.
  Suppression hides a reminder, never an item.
- The **completed-item** rule: invisible on every surface except dashboard
  history, which is the record rather than a surface competing for attention.

**The already-alerted latch is NOT reshaped into the window.** It is permanent
because `check_urgent_alerts_job` runs every 60 seconds against the same
table, and anything short of permanent is a loop. Two different questions;
reshaping one to satisfy a symmetry nothing asked for would change live
behavior.

**No channel selection.** That needs presence, which is step 9.

### Preconditions for update and delete

`tools/preconditions.py`. **Neither tool exists** — these are the tuples they
will be registered with, written and tested one step early because the
precondition machinery has been built since step 3 and has never had a live
consumer (`add_calendar_event` correctly requires no prior read).

Both require a read covering the target's **day**, in the same turn.
Day-level rather than event-level because **the identifier is the thing in
doubt**: checking an invented id against a ledger that never saw it fails
closed and tells the model nothing useful, while the day is a claim the model
cannot fake and the read that satisfies it returns the real identifiers anyway.

**THE CONSTRAINT ON THE FUTURE SIGNATURES — read this before writing
`update_calendar_event`.** `CalendarReadFor` resolves the target day out of
the call's **own arguments**, so an update or delete tool MUST take the day as
a parameter even when it also takes an event id. A
`delete_calendar_event(event_id)` has no date field to check, fails closed
forever, and gives no obvious reason why.

`target_proven()` feeds the same ledger answer into `policy/gating.py` as a
bool. Two functions rather than one: a precondition returning `None` means
"run" and a gate input of `False` means "ask first", and conflating inverted
senses behind one return value is a real hazard.

The precondition and the gate are different questions and a delete needs both:
*has Friday looked?* (refuses to run) and *should the user decide?* (asks
first). Having looked is not permission, and permission granted against an
event nobody verified is permission to delete the wrong thing.


## The Router

Everything under `friday/router/`, plus the plan argument on
`agent/turn.py::run_turn`. Built in step 7. **Three tiers, and the third one
is the absence of a decision.**

```
Tier 1   router/fastpath.py   pattern match, no model, no network    31.1% of traffic
Tier 2   router/classify.py   one CLASSIFY call -> a plan shape      97.6% accurate
Tier 3   no plan at all       CHAT exactly as before the router      the fallback
```

### Why this bought three separate things

**Cost.** A greeting answered by CHAT is ~2,600 input tokens and 6–13s,
measured off `llm_exchanges`. Answered by tier 1 it is a dict lookup.

**Offline.** Tier 1 works with the Wi-Fi off, and Telegram is blocked on
school Wi-Fi ~7h a day. Before this a dead network meant Friday did nothing;
now it means Friday does less.

**Failure surface.** Gemma re-proposes a card for an earlier turn's event
about one turn in eight. A request that never reaches CHAT cannot produce
one. This does not fix that behavior — it shrinks how often it can happen.

### The plan table

| Plan | tool_scope | write needs a read | hops |
|---|---|:--:|:--:|
| `ANSWER` | `None` | — | 0 |
| `READ_THEN_ANSWER` | `("read",)` | — | 2 |
| `READ_THEN_WRITE` | `("read","write")` | **yes** | 3 |
| `WRITE_DIRECT` | `("write",)` | no | 2 |
| `CLARIFY` | `None` | — | 0 |

**`WRITE_DIRECT` does not require a read and that is the distinction from
`READ_THEN_WRITE`, not a safety hole.** "Add dentist on the 26th at 3pm" names
its own target; a prior read establishes nothing. Anything referring to an
event that already exists — moved, cancelled, relocated — is
`READ_THEN_WRITE`, because there the identifier is what is in doubt. The
permission gate is downstream of every plan and unaffected: **no plan can make
a write skip its card.**

### Rules that must not be relaxed

1. **The plan narrows; the profile is the ceiling.** `LLMRequest.tool_scope`
   is INTERSECTED with `profile.tool_scope` in `llm/assembly.py::build_tools`,
   and the hop budget is `min()`'d in `agent/turn.py`. **Neither direction is
   symmetric, and the profile wins every disagreement.** "COMPOSE never gets
   tools" and "CHAT never sees `commit_calendar_event`" are properties of the
   profile table, which `llm/profiles.py` says config may not touch. A scope
   arriving on a request came, ultimately, from a classifier's one-word
   answer. If it could widen, the strongest guarantee in the LLM layer would
   be one enum value away from void. An empty intersection is `None`, never
   `()`.

2. **`PROFILE_SCOPE` is a sentinel and is not `None`.** `None` already means
   *no tools at all*. A plan that narrows to nothing and a caller with no
   opinion must not be the same value, or every pre-router caller silently
   loses its tools.

3. **`READ_THEN_WRITE` cannot reach its write without a read**, and it is
   enforced in the loop against the tool's REGISTERED scope and the ledger's
   read count — not described in the prompt and hoped for.
   `tests/test_router_turn.py` stubs a model that skips straight to the write
   and repeats after being refused.

4. **The plan gate does not replace `tools/preconditions.py`.** Both run and
   neither subsumes the other. The precondition asks *was the day this call
   targets read?* (coverage, the strong check); the gate asks *did this turn
   read anything at all before writing?* A model that reads Tuesday and writes
   Thursday passes the gate and fails the precondition. `Ledger.read_count()`
   is coverage-blind and says so.

5. **A tier-1 match must be unambiguous.** Every pattern is a `fullmatch` over
   the whole normalized message, and normalisation is shallow on purpose:
   case, whitespace, curly apostrophes, trailing punctuation, nothing else. A
   near-miss falls through. `fullmatch` rather than `search` specifically so
   "add lunch tomorrow, and what's the weather" cannot answer the weather half
   and drop the write.

6. **Tier-1 responses are templates.** The greeting draws from `quips.yaml`'s
   `greetings:` group — authored voice, not model output — plus the injected
   clock. Nothing here is generated.

7. **Falling through is never a failure.** `respond()` returns `None` whenever
   it cannot answer well, and the caller cannot distinguish that from "no
   pattern matched". Both mean the model handles it.

8. **The fallback is the absence of a plan, not a plan.** `plans.resolve()`
   answers `None` for anything that is not a plan name, and
   `run_turn(plan=None)` is the pre-router path byte for byte. **The router
   may narrow what a turn is allowed to do; it may never be the reason Friday
   can do less than it could yesterday**, and the cheapest guarantee of that
   is for the failure mode to be the old code rather than a plan approximating
   it.

9. **No slash commands, deliberately.** `friday.py` registers
   `MessageHandler(filters.TEXT & ~filters.COMMAND)`, so PTB drops `/brief`
   before any Friday code runs, while the dashboard has no such filter and
   would accept it. A pattern that works on one surface and silently does
   nothing on the other is worse than no pattern.

10. **There is no `resume` pattern and there must not be one.** A paused
    Friday drops the message in `conversation.handle()`'s pause check, which
    runs ABOVE the router and must — hoisting the router over it would let
    every tier-1 answer speak while paused, which is exactly what pausing is
    for. So `pause`'s confirmation says where the other end of the switch is.

### What tier 1 actually catches

Measured against the 183-message historical corpus:

| pattern | share | why it is there |
|---|---:|---|
| weather | 13.7% | see below — this is the only path to an answer |
| greeting | 13.1% | pure cost |
| calendar (today/tomorrow only) | 2.7% | **offline, not cost** |
| brief | 1.6% | already deterministic, was reaching CHAT for no reason |
| pause | 0% | a candidate from the brief; reported honestly |
| **total** | **31.1%** | |

**The weather responder is not a cheaper path to an existing answer — it is
the only path to one.** The weather tool went with the Phase II teardown, no
weather block is injected into any prompt, and
`connectors/weather.py`'s docstring still claims it is "called on demand from
`on_message`", which stopped being true at the teardown. Every weather
question in the corpus was answered by a model with no weather data in front
of it.

It is also **the one thing in `router/` that fetches**, which is stated rather
than hidden: two 10s requests at worst, while `TURN_GATE` is held, cached 10
minutes and **never served past 30** — a stale forecast delivered confidently
is worse than falling through. This is not a violation of **INJECT, NEVER
FETCH**: that rule governs what goes into a *prompt* and exists because a
model that has to ask can decline to. Nothing here reaches a model. What it
shares with the rule is the gate, which is why the bound matters.

**The calendar pattern is restricted to an explicit `today`/`tomorrow`.** A
bare "what's on my calendar" falls through: it has no date, and guessing which
day someone meant is the confident-wrong-answer this layer refuses to produce.

### The classifier

`CLASSIFY` (`gemini-3.5-flash-lite`, temperature 0.0), constrained to one
member of `plans.names()` via `response_schema`. ~255 input tokens, 3–8
output, **577ms median**.

**Constrained output was verified live before the prompt was written.** An
unconstrained classifier puts a regex over model prose on the path that
decides whether a turn may write to the calendar. The answer arrives as JSON,
so it is a **quoted** string — `'"ANSWER"'`, not `'ANSWER'` — and `json.loads`
is how it is read. The bare-token fallback accepts one token and refuses a
sentence; a hand-rolled quote strip would quietly become a substring search.

**Measured accuracy: 97.6% (81/83) against a hand-labeled corpus.**

| plan | n | accuracy |
|---|---:|---:|
| ANSWER | 27 | 100% |
| READ_THEN_ANSWER | 12 | 100% |
| READ_THEN_WRITE | 6 | 100% |
| WRITE_DIRECT | 33 | **97.0%** |
| CLARIFY | 5 | 80% |

**The prompt was written against observed failures, not from first
principles.** A throwaway one-line prompt scored 2/5 in the Phase 0 probe, and
one failure — `"I have work on Wednesday from 6-9 pm"` → `ANSWER` — sits in
the largest category of real traffic. So the first thing the definitions say
about `WRITE_DIRECT` is that **a statement of fact about the user's own
schedule is a write**, with five corpus examples of that shape. It reads like
an odd thing to have to spell out and it is the most valuable line in the
file.

**The definitions live in code, not `AGENTS.md`.** Every other prompt fragment
in Friday is persona and belongs to the user in a file they can edit while the
daemon runs. These name the plan shapes in `router/plans.py`; a user edit
renaming one would silently reroute every message.

**No history, deliberately.** One message plus the injected standing context.
If picking between five shapes needed twenty turns of transcript, the
vocabulary would be wrong. The real casualty is the bare follow-up — "double
it", "why not?" — which has no shape of its own and lands on `ANSWER`,
correctly often enough and wrongly at negligible cost.

**`CLASSIFY`'s free tier is rate-limited per minute, not per day.** An 83-call
evaluation run back-to-back hit `429 RESOURCE_EXHAUSTED` repeatedly and had to
be paced to 7s between calls. Irrelevant to chat, where calls are seconds
apart; it matters the moment anything batches CLASSIFY — which the urgency
tagger will.

### CLARIFY, and the two-round cap

`router/clarify.py`. Structurally `CLARIFY` and `ANSWER` are identical — no
tools, no hops, one dispatch — so **the directive is the whole of the plan**.
Without it, `CLARIFY` is `ANSWER` with a different name on the log line and
the model is as likely to invent a date as to ask for one. The directive says
**ONE question**, which is load-bearing: asked for everything missing at once
the model produces a form, which reads as an interrogation for something the
user thought was one sentence.

**Two rounds, then answer with what is known.** The cap exists for a failure
this design creates: the classifier sees one message and no history, so the
user's *answer* to a clarifying question is itself a bare fragment — "Its
work", "tomorrow at 3" — which is the exact shape that routes to `CLARIFY`
again. Uncapped, Friday asks a question, receives its answer, and asks the
same question about the answer.

**The third round is not a refusal and not a toolless plan.** It drops to
`plan=None` — CHAT's full scope — plus an instruction to act on the most
reasonable reading and state the assumption in one clause. A third round that
cannot act on what it worked out is the opposite of "answer with what is
known".

`guard()` runs on **every** turn, because that is where the streak is cleared.
A counter incremented only by the branch it guards never resets, and the next
unrelated message inherits a cap it did nothing to earn. The streak is one
`system_state` key, not derived: `llm_exchanges.plan` would make an
instrumentation table load-bearing for behavior, and `conversation_history`
has no column for a plan at all.

### Offline

`llm/dispatch.py::last_error_kind()` — one string, set on every dispatch
before the retry decision so a path added later cannot forget it.
`router/classify.py` skips its own call when the last thing to touch the API
could not reach it, so a blocked network costs **one** failed request per turn
instead of two.

**This is not a reachability probe** — that is step 9, and it is a module with
state, timestamps and staleness rules. This is an observation of a call that
already happened. It carries no timestamp because it has no staleness
question, and it heals with no timer: the CHAT turn the classifier declined to
route will run and overwrite it either way.

**`network` only.** A 429 means the API answered and refused us, and the
classifier is a different model on a different quota. A 500 means the far side
had a bad minute, per request. Only "we reached nothing" generalises from one
call to the next — which is why `transient` was never folded into
`rate_limit`.

Exposed as *how did the last call end*, never *is the model up*. The second is
a bigger claim than the data supports, and a caller that believed it would
eventually stop trying.

---

## Calendar Writes: which path gates

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

**Done — Phase III step 4 (effects, cards, the first gated write).** Verified
end to end on the live daemon: a card, a tap, a service-confirmed write, a
read-back that says `verified: true`, and one confirmation with a quip.
`add_calendar_event` is the only write tool; there is no update and no delete.

**In progress — Phase III, the LLM layer rebuild (11 steps, `phaseiii.MD` §12).**
`llm-layer-teardown` removed the tool layer, the old dispatcher, the call
profiles, persona assembly and every prompt. **Steps 1–3 have landed** on
`phase3-dispatcher`: the dispatcher and provider adapters; persona assembly,
real multi-turn history and the CLASSIFY/COMPOSE profiles; and the tool layer
with two read-only calendar tools and a bounded turn loop.

**Done — Phase III step 5 (the second channel).** `effects/entry.py` is the
only door effects go out of; `channels/base.py` is the contract;
`channels/conversation.py` is one user message on any surface, and holds the
one gate. The dashboard is a full conversational channel: chat, SSE, permission
cards, shared transcript, shared card keys, token auth on every route, a PWA
manifest, and a native interrupt path. **It works with the Wi-Fi off**, which
is the seven hours a day Telegram is blocked.

Friday can answer questions about the calendar in persona, write to it behind a
permission card, and do both from either surface. It still does NOT: tag
urgency, extract events from GroupMe or images/PDFs, fire urgent alerts, or
select a quip by context.

**Done — Phase III step 6 (cleanup, context and policy).** No new capability,
and that is the point: `llm/context.py` is the one producer and the one
formatter for injected context, `policy/` answers three separated questions,
`tools/preconditions.py` holds what update and delete will need, the failure
sentences sit where a channel can own them, and `TurnResult.model_text` no
longer shares a name with `Reply.text`.

**Done — Phase III step 7 (the router).** `friday/router/`: three tiers between
a message and a model. Tier 1 catches 31.1% of real traffic with no model and
no network, tier 2 picks a plan shape at 97.6% measured accuracy for ~577ms
and 255 tokens, and tier 3 is the old path unchanged. A plan narrows the
profile's tool scope and hop budget and can never widen either;
`READ_THEN_WRITE` structurally cannot reach its write before its read; and
`CLARIFY` asks at most twice.

Friday now answers greetings, weather, briefings and today/tomorrow's calendar
without a model at all — **and the weather answers are better than before,
because CHAT had no weather data to answer with.**

**Next is step 8** — the to-do layer, which `policy/suppression.py`'s windowed
briefing-echo and completed-item rules were built for.

`Channel.notify` has one producer now — a deterministic Canvas-token-expiry
check in `friday.py`, unrelated to the tagger. The tagger's own urgent-alert
producer is still unbuilt and arrives with step 8.

---

## Key Constraints & Rules for Claude Code

1. **One gate, and it is acquired first.** `channels/conversation.py::TURN_GATE` — an `asyncio.Semaphore(1)` taken at the top of `handle()` before any SQLite query, any context assembly, any model call. It was at the top of `telegram.py::on_message()` until step 5 and moved for one reason: a gate owned by one channel serializes that channel against itself while letting a dashboard turn and a Telegram turn interleave against the same `conversation_history`. **Never add a second semaphore, and never do work above the `async with`.**
2. **Never use a second scheduling library.** No `schedule`, no raw `apscheduler`, no background threads for timing. PTB `JobQueue` only.
3. **Never poll iMessage.** Not via AppleScript, not via `chat.db`, not via any method.
4. **Never write an *inferred* event without an approval gate.** Explicitly requested writes and Canvas due dates use `auto_write`; everything Friday deduced uses `gated_write`. See the calendar-writes section.
5. **Never hardcode a Python path in a LaunchAgent.** `macos_setup.py` renders the plists against the interpreter resolved on the running machine. Never `/usr/bin/python3`.
6. **Canvas uses the iCal feed.** Never HTML scraping. Use `icalendar`.
7. **The LLM processes all ingested data.** Never bypass it for urgency, filtering, or calendar decisions — even for clean structured data.
8. **The calendar backend is the event store.** Briefings and reminders read from it, not from the SQLite events table.
9. **Briefings run with tools OFF.** Enforced structurally: `COMPOSE.tool_scope` is `None`, so the provider is handed no `tools` argument at all. If a briefing is thin, expand `bundle_briefing_context` — never give COMPOSE a scope. `bundle_briefing_context` survived the teardown intact precisely because it is the layer worth keeping. **True but currently moot: no briefing reaches a model.** `agent/briefings.py::_compose` is a deterministic renderer — header plus the bundle's sections — and `COMPOSE` has no callers at all. The rule binds the moment one is composed by a model again, which is why it stays written down; it is not describing live enforcement today.
10. **Every LLM call goes through `llm/dispatch.py`.** No direct provider call anywhere else, no second door for images or JSON, no "just this once" convenience path. See the LLM Layer section.
11. **`llm/providers/gemini.py` is the only file that may import a provider SDK.** If a layer above it needs to know something SDK-shaped, the abstraction is wrong — fix the type, not the import.
12. **SQLite is the operational backbone only.** No state.json. No vector store. No Redis.
13. **Voice is a standalone satellite.** Never imports from Friday's core.
14. **Friday does not edit its own Python source.** `self_edit.py` writes YAML only — learned quips and a whitelist of settings. The core is relaunched on exit by launchd/tray, so a syntax error would be a silent restart loop rather than a visible failure.
15. **All secrets** live in `friday_config.yaml` or environment variables. Never hardcoded. That file is gitignored; `friday_config.yaml.example` is the documented template.
16. **`compat.strftime()` for any format string with `%-`.** `%-d`/`%-I` are glibc-only and crash on Windows.
17. **The assistant slot after a permission card must be empty.** Whatever text sits there, the model will repeat or act on. All three encodings were tried and all three failed: a `[permission card sent]` marker leaked to the user as prose two turns later; the card's own text caused the model to re-propose old events (one message, three cards); a past-tense "I put a confirmation card in front of you for X" was emitted verbatim as a reply. Nothing is written to that slot. **The outcome is the assistant's turn**, written by `effects/pending.py` when the user taps. History rows are examples, and there is no phrasing of a non-reply that is a good example of a reply.
18. **EventKit cannot see JXA writes — verify a write through the door it went out of.** Measured: a JXA read-back sees its own write in 0.57 s; an EventKit read still could not see it minutes later. Read-back verification is therefore per-backend — `calendars/backend.py` calls the backend's `event_exists()`, never the ordinary reader (which also applies `agent.briefing_calendars`, a different question entirely). Two diagnoses were chased here and both were wrong: `refreshSourcesIfNecessary()` (refreshes remote sources only, does nothing for local caches) and `EKEventStore.reset()`. Both are removed and neither was load-bearing. **Do not try them again.**
19. **A write outcome is a result type, never `None`.** `calendars/writes.py::WriteOutcome` has three statuses and the difference between two of them is the whole point. **`refused`** — calendar not found, structured service error — definitely did not happen, and records nothing. **`unknown`** — timeout, non-zero returncode, unparseable JSON — may have succeeded server-side, and records a `WriteAttempt` carrying its fingerprint so a retry has something local to check. A sentinel gets compared with `is None` downstream and the distinction evaporates at the one place it mattered.
20. **Nothing follows a permission card — not even the model's own text.** `conversation.handle()` suppresses `result.text` outright when a card went out, and suppresses the error line too. This is stronger than rule 17, which is about the history slot; this is about what reaches the user. The branch used to run only when the model happened to say nothing, and the first card the dashboard ever produced was followed by the model narrating its own tool call in Chinese. Suppressed rather than sent-and-not-logged: a sentence the user reads belongs in history, so the only correct handling of prose that must not be read is to not send it.
21. **A new channel implements `channels/base.py` and calls `conversation.handle()`.** It does not read history, build a request, call the model, or decide what to say. If it duplicates anything from another channel beyond transport, the pipeline has forked — invariant 9. See the Channel Layer.
22. **`conversation_history.channel` is a record, never a filter.** Every turn reads the window unfiltered. A message typed in the dashboard has to be in scope when the user asks about it over Telegram.
23. **The channel that handled a tap is the channel that answers it.** Never construct another channel to send a confirmation. The dashboard did exactly that and every card confirmed there answered into Telegram — which on school Wi-Fi went nowhere.
24. **Every dashboard route requires the auth token.** Including the SPA, `app.js`, the stream and the card endpoints. Only the favicon, manifest and PWA icons are open, and only because an OS install prompt fetches icons without a cookie. There is no disable switch and there must not be one.
25. **A permission card structurally cannot carry a quip.** `SendPermissionCard` has no `quip_key` field; only `SendMessage` does, and `effects/runner.py` is the only place a quip is ever appended. This makes invariant 6 on cards **unviolatable rather than merely enforced** — there is nowhere to put one. Do not add the field "for symmetry".
26. **A staged proposal is not editable.** An edit that drops a field changes what the user approved, which is the one thing the gate exists to protect. Refuse rather than half-implement — see `dashboard/server.py`, which returns 400 for `edit` on a `tool_call` row and tells the user to cancel and ask again.
27. **A tool returns a `ToolResult` or a `ToolError`** — never a string, never `None`, never a raised exception for an expected failure.
28. **Tools never write the ledger.** They declare coverage in their return value; `tools/executor.py` records it. There is no accessor in `tools/ledger.py` to reach, and it must stay that way.
29. **Never pass turn state to a tool through a thread-local.** Tools run in a worker pool and will not see it. Pass it explicitly — this failed silently once already.
30. **Parse event timestamps only through `calendars/eventtime.py`.** The two Apple readers disagree about timezone spelling and a second parser is wrong on one of them.
31. **`TurnResult.model_text` is not `Reply.text`.** The first is what the model produced; the second is what the user was told, and card suppression is the gap between them. They were both `.text` until step 6, on two objects that travel together through `handle()`.
32. **Injected context has one producer and one formatter** — `llm/context.py`. Chat, briefings and the router render the same shape. Never build a fifth.
33. **Deterministic context is injected, never fetched, and never a tool.** A model that has to ask what day it is can decline to. See the injection site in `llm/dispatch.py`.
34. **Policy decides and acts on nothing.** `gating.py`, `visibility.py` and `suppression.py` perform no queries, no writes, and no clock read of their own — `now` is passed in so a decision can be tested without waiting for one.
35. **Suppression hides a reminder, never an item.** A suppressed item stays on the list, stays in the briefing, and stays answerable. What is withheld is the standalone interrupt.
36. **An update or delete tool must take the target day as a parameter**, even when it also takes an event id — otherwise its precondition has nothing to check and fails closed forever. See `tools/preconditions.py`.
37. **A router plan narrows the profile; it can never widen it, and the profile wins.** Tool scope is INTERSECTED (`llm/assembly.py::build_tools`) and the hop budget is `min()`'d (`agent/turn.py`). A plan came from a classifier's one-word answer; the profile table is architecture. An empty intersection is `None`, never `()`.
38. **The router's fallback is the absence of a plan, not a plan.** `run_turn(plan=None)` is the pre-router path byte for byte. The router may narrow what a turn can do; it may never be the reason Friday can do less than it could yesterday.
39. **A tier-1 match must be a `fullmatch` over the whole message, and near-misses fall through.** A wrong fast-path answer is worse than a slow right one: it is confident, fast, and has no model in the loop to hedge. `search()` would let "add lunch tomorrow, and what's the weather" answer the weather and drop the write.
40. **`conversation_history` IS NOT THE TRANSCRIPT OF RECORD.** It holds 48 rows starting at `id=266` — it has been reset or pruned at some point with no record of when or why, and nothing in the code does that today. **The only surviving history is `logs/friday.log`**, where every turn appears as `Message (channel): <first 80 chars>`. All 183 messages the router was built against came from there. Anyone reasoning about usage, traffic shape or hit rates from the database will be wrong by a factor of four, and will not be able to tell.
41. **No slash commands in the router.** `friday.py` filters `~COMMAND` before any Friday code runs; the dashboard does not. A pattern that works on one surface and silently does nothing on the other is worse than no pattern. Bare words only.
42. **There is no chat `resume` and there must not be one.** The pause check runs above the router and must — hoisting the router over it would let every tier-1 answer speak while paused.

---

## Config

`friday/friday_config.yaml.example` is the canonical, commented template — read
it rather than a copy here. Startup hard-fails only on `telegram.bot_token`,
`telegram.chat_id`, and a Gemini key when `provider: gemini`. Every other block
is optional; an unconfigured connector is skipped, not an error.

### Which model CHAT runs, and why

`profiles.CHAT.model` is **`gemma-4-31b-it`**. CHAT is the profile that extracts
tool arguments, and extraction quality at call #1 is final — there is no
corrective pass over a tool's arguments before it runs — so this choice matters
more than any other in the table. All three candidates were measured on the
same five naturally-phrased add requests.

| model | function calling | argument extraction | turn discipline | median latency | quota |
|---|---|---|---|---|---|
| `gemma-4-31b-it` | **yes** — verified live, see below | 5/5 correct | 7/8 turns clean; 1 turn re-proposed an old event **and** emitted a stray token (`elderly`) | 6,986 ms | no daily ceiling |
| `gemini-3.5-flash-lite` | yes | 5/5 correct | 3/5 turns claimed the event was added while only a card had been sent; re-proposed old events on an ordinary transcript | 589 ms | no daily ceiling |
| `gemini-3.6-flash` | yes | not measured — quota exhausted | clean on the 2 turns observed | 1,975 ms | **20 requests/day** on the free tier |

`gemma-4-31b-it` function calling was verified live rather than assumed, using
the real `get_schedule` schema from the registry with
`automatic_function_calling` disabled:

- the API **accepts** `tools=[types.Tool(function_declarations=[…])]`
- a `function_call` part **does** come back in `content.parts`
- arguments are **well-formed** (`date_from`/`date_to` both `2026-08-12` for "tomorrow")
- a replayed function call round-trips **with or without** `thought_signature`.
  **Gemma has no signature requirement** — this is the one place it is laxer
  than Gemini 3.x, which returns `400 INVALID_ARGUMENT` unless the signature is
  replayed. `ToolCall.signature` is still load-bearing for Gemini and must not
  be dropped.

Gemma emits **thought parts** on nearly every call. `llm/providers/gemini.py`
filters `part.thought` out of the user-facing text; without that the model's
reasoning is prepended to every reply, and on a tool-calling turn the reasoning
is all the text there is.

**Costs, stated plainly.** Gemma is ~12× slower than flash-lite; a tool-calling
turn is two dispatches, so a card takes ~14 s and worst-case ~45 s. That fits
CHAT's 120 s deadline but is visibly sluggish. And it is better at turn
discipline than flash-lite, not immune to it — under a long history it still
re-proposed a previous event and emitted a stray token.

**The `避` bug was two bugs, and they were fixed in two different steps.** The
distinction matters because the first fix was widely assumed to be the whole
of it, and it was not.

*The half step 4 killed structurally.* In Phase II tools sent Telegram
messages themselves mid-turn, set flags on the agent object, and the model
then had to be *talked into* staying quiet afterward. That is genuinely dead:
tools return effects, `effects/runner.py` is the only code that sends, and
ordering is a sort rather than a rule. A tool cannot message the user because
it has no channel to import.

*The half that survived until step 5.* Suppressing the model's own prose after
a card was a branch that **only ran when `result.text` was empty**. So the
invariant "nothing may editorialize on a permission card" was, in the live
system, being upheld by the model's willingness to stay quiet — exactly the
arrangement step 4 was supposed to have retired. The code looked correct
because the only model in use usually did stay quiet. Step 5 made the
suppression unconditional: prose after a card is now dropped whether or not
the model produced any (see rule 20).

**How it was found is the part worth keeping.** Nothing in Telegram exposed
it. The first permission card the *dashboard* ever produced came back with the
model narrating its own tool call underneath it, in Chinese — visible because
the dashboard renders a turn's events as a list, where a second bubble under a
card is obvious, while Telegram's chat flow had been absorbing the same thing
as just another message. **A second surface rendering the same data
differently is a test the first surface cannot run**, and this one found a
live invariant violation that had been shipping for a step and a half. That is
an argument for building the second surface *earlier* than it feels justified,
not after the first one is finished.

**What this means for model choice.** Stray model output is still not a reason
to avoid a particular model — it is cosmetic noise in a reply, not a message
sent out of order, not a write performed without a card, not a turn whose
bookkeeping is wrong. Choose on extraction accuracy, latency and quota. But
the reason that is true is that the suppression is unconditional now; it was
not true when this section first claimed it.

Blocks worth knowing about:
- `dispatcher` — `enabled: false` restores pre-dispatcher behavior exactly (all tools, no extra call).
- `calendar.backend` — `apple` | `google`. Defaults to google on win32, apple elsewhere.
- `notifications` — the dashboard-facing mirror. `groupme_polling: false` is a real kill switch read by `poll_connectors_job`; the `agent` block stays canonical for the JobQueue and wins if the two disagree.
- `groupme.groups[].priority` — `high` (can interrupt) | `normal` (briefings only) | `muted` (ingested, never surfaced). `low` is the legacy spelling of `muted`.
- `voice` — read only by `voice/listen.py`, which does not reload it. Restart the voice agent after changing it.
- `dashboard.auth_token` — the shared token every dashboard route requires. Generated on first boot, written back, and logged once as a ready-made `?token=…` URL. Blank means "generate one", never "no auth". `menubar.py`, `mac_app.py` and `tray.py` read it from this same file and send it as `X-Friday-Token`.
- `agent.pending_action_ttl_minutes` (1440) and `agent.pending_action_stale_minutes` (30) — see The Effects Layer. Both are read at startup by `effects/pending.configure()`, so a change needs a restart.

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
grouped by tool and outcome, so a quip cannot contradict its event.

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
