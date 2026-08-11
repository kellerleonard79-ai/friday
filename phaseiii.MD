# Friday — Phase III Architecture

Status: design complete, pre-implementation
Current state: stripped to Telegram → Gemini → text. No persona, no tools, no dispatcher.

---

## 1. Why this rebuild exists

Phase II failed in four specific ways, and every decision below traces back to one of them.

| Failure | Cause | Fix |
|---|---|---|
| Token blowup, rate limits | One undifferentiated prompt envelope for every call | Call profiles + section-addressable persona |
| Provider lock-in | Gemini's function-calling shape leaked into every layer | Single dispatcher, provider adapters below it |
| Stray characters after tool calls | Tools messaged the user mid-turn, then the model had to be talked into silence | Effects layer; tools return intent, never send |
| Docstring bloat | Tool descriptions carried persona, argument semantics, and behavior rules all at once | Schemas derived from typed signatures; docstrings say *when*, not *what* |

Two constraints discovered later shape the rest:

- **Telegram is blocked on school Wi-Fi**, in both directions, for roughly seven hours a day. The dashboard is therefore the primary channel during school hours, not a convenience.
- **The Gemini API and Google OAuth are reachable at school.** Tested and confirmed. No home-side exit node is needed.

---

## 2. Layer stack

```
Channels          Telegram · Dashboard · Voice
                  Own concurrency, surface formatting, transport
        ↓
Turn runner       Plan selection, bounded tool loop, effect emission
        ↓
Effects           Intent → side effects, ordered, gate first
Context           Deterministic pre-fetch → labeled blocks
Policy            Gating, suppression, visibility
Tools             Registry, preconditions, structured returns
Persona           Section-addressable AGENTS.md
        ↓
Dispatcher        Profiles, retry, budget, logging — THE chokepoint
        ↓
Providers         Gemini adapter. The only file that imports google.genai
```

**The rule that keeps this from eroding:** each layer may call downward and return upward. No layer reaches past its neighbor. If a tool needs to send a message, it returns an effect — it does not import the Telegram channel.

---

## 3. Layer specifications

### 3.1 Providers

**Owns:** translating a canonical request into one provider's SDK call and the response back out.

**Handles:** message format, tool schema translation, tool-call parsing, token usage extraction, error classification (rate limit / transient network / fatal / blocked).

**Must not:** know about profiles, persona, tools by name, or any channel.

Adding a second provider later is one new file implementing the same interface. This is the only insurance against Gemini pricing or availability changing.

### 3.2 Dispatcher

**Owns:** every LLM call in the system. No exceptions — not briefings, not classification, not quips (which no longer make calls at all).

**Handles:**
- Profile resolution → model, persona sections, tool scope, temperature, token cap, timeout, hop limit
- Prompt envelope assembly (persona sections + context blocks + history + prompt)
- Provider selection and fallback
- Retry with backoff on rate limits; distinct handling for network-unreachable
- Timeout enforcement
- Logging every exchange with profile, token counts, latency, and what triggered it
- Per-profile daily token budget with a circuit breaker

**Must not:** know about Telegram, know persona *content*, know what any tool does.

**Profiles:**

| Profile | Model tier | Persona sections | Tools | Purpose |
|---|---|---|---|---|
| `CLASSIFY` | cheapest | identity, time | none | Intent and urgency, constrained enum output |
| `EXTRACT` | mid | identity, time | none | Photo/PDF → structured event, schema-constrained |
| `COMPOSE` | mid | identity, voice, formatting, time | none | Briefings from pre-fetched context |
| `CHAT` | full | all | scoped | Conversational turns with tools |

`tool_scope = None` is the type-level guarantee that replaces `use_tools=False`. A profile cannot accidentally be given tools.

### 3.3 Persona

**Owns:** AGENTS.md, parsed into named sections, cached with an mtime check so edits reload without a restart.

Sections: `IDENTITY`, `VOICE`, `FORMATTING`, `TOOL_POLICY`, `TIME`.

**Must not:** be imported by tools, channels, or the turn runner. Only the dispatcher assembles persona.

**The scoping rule:** voice applies to conversational replies only. Permission cards, briefings, error messages, and anything bound for TTS are literal. This is enforced by which sections each profile requests, not by asking the model nicely.

### 3.4 Tools

**Owns:** the registry, schema derivation, precondition checking, execution, and structured returns.

**Registration** carries: name, scope tags, effect class (read / auto-write / auto-update / gated-write), preconditions, timeout, and quip group.

**Schema derives from the typed signature.** The docstring is one sentence saying when to call the tool. Argument semantics come from types and annotations. This structurally prevents the docstring-stuffing that happened in Phase II.

**Returns** are always a structured result: data, a list of effects, and a commit flag. Never a string, never a side effect.

**Must not:** send messages, touch a channel, or call the LLM.

**Preconditions** record what was read, not that a read happened. A calendar read covering next week does not license a write to today. Failure returns a structured error *into the tool loop* — not raised, not shown to the user. Two precondition failures in one turn aborts the turn to a clarification.

### 3.5 Policy

**Owns:** every "should this happen" decision that isn't the model's to make.

- **Gating** — see §5
- **Suppression** — if an item was surfaced in a briefing within N hours, its standalone reminder is suppressed but the item stays on the list
- **Visibility** — a completed to-do is invisible to every surface except dashboard history
- **Channel selection** — see §7

Centralized because each of these would otherwise be reimplemented in three places and drift.

### 3.6 Context

**Owns:** deterministic pre-fetch and the labeled-block formatter.

One formatter used by briefings, chat, and classification alike. This is where "inject, don't let the model call tools for it" stops being a briefing-only habit and becomes a system property. Current time, day of week, location, and presence are always injected, never fetched by tool call.

### 3.7 Effects

**Owns:** turning declared intent into actual side effects, in a defined order.

Catalog:

| Effect | Fires |
|---|---|
| `SendMessage` | Text to a channel |
| `SendPermissionCard` | Gated action, buttons wired to a pending key |
| `SendNotification` | Native macOS notification (school-hours interrupt path) |
| `ScheduleItem` | Persisted scheduled work |
| `CancelScheduled` | Kills pending items for a completed to-do |
| `Speak` | TTS, only when presence permits |
| `Quip` | Appended to a confirmation, chosen in code |

**Ordering is fixed: permission cards first, always.** This is what converts the inviolable-gate rule from something a developer must remember into a property of one function.

### 3.8 Turn runner

**Owns:** the loop.

```
build request → dispatch → tool calls? → execute → feed results → repeat (bounded)
                                      ↓
                              collect effects → emit
```

Bounded hops, no recursion. Tool execution goes through the executor with per-tool timeouts. A tool failure returns a structured error into the loop rather than raising.

### 3.9 Channels

**Owns:** transport, concurrency, and surface-specific formatting.

Each implements the same interface: `send`, `send_card`, `notify`, `receive`. The dashboard must be a channel adapter, not a second chat implementation — same turn runner, same history, same pending actions. Confirming a card in the dashboard and confirming it in Telegram resolve the same action.

---

## 4. Routing

Three tiers, cheapest first:

1. **Pattern matching.** Regex and prefix for high-frequency commands. Zero LLM, zero latency, and — critically — **this tier is the offline mode.** Every command moved here survives an API outage.
2. **`CLASSIFY` profile.** Cheap model, enum-constrained output. Roughly 100 tokens in.
3. **`CHAT` profile.** Only when tiers 1–2 say conversational or multi-step.

The router selects a **plan shape**, not a tool:

| Plan | Shape |
|---|---|
| `ANSWER` | No tools, straight to text |
| `READ_THEN_ANSWER` | One read, then compose |
| `READ_THEN_WRITE` | Read → policy check → write or card |
| `WRITE_DIRECT` | Unambiguous create only |
| `CLARIFY` | Missing or ambiguous parameters |

`READ_THEN_WRITE` structurally cannot skip its read. Preconditions become a backstop against the model going off-script rather than the primary mechanism.

---

## 5. Gating

|  | Reversible | Irreversible |
|---|---|---|
| **User stated the fact** | AUTO | GATED |
| **Friday inferred it** (email, Canvas, photo, GroupMe) | GATED | GATED |

`AUTO_UPDATE` requires additionally that the target was proven by a read in the same turn. An update chosen from the model's memory is an inferred fact and gets a card.

Delete is always gated: irreversible plus fuzzy title matching is the worst pairing in the system.

**Canvas due dates remain the standing auto-write exception**, as in Phase II.

---

## 6. The determinism boundary

Python computes. The LLM phrases.

- **Gap-finding is interval arithmetic.** The model never scans a raw event list for holes. Same for "shorten this to fit" — Python proposes candidate compressions, the model picks and phrases.
- **Missing parameters are caught by the executor** before the call leaves. Required params have no defaults. The prompt instruction is a nicety, not the mechanism. Clarification rounds cap at two.
- **Labels come from a config enum**, selected via response schema.
- **Quips are per-tool groups keyed on tool + outcome**, chosen by `random.choice` with a short no-repeat memory, applied by the effects layer. Zero LLM cost. An auto-write turn is now one call, not two.
- **Confirmations are templates fired on commit.** The model is never woken to say "done."
- **Time and day of week are injected**, never inferred.

---

## 7. Channels and presence

### Channel selection

| Presence | Primary | Interrupt |
|---|---|---|
| Home, unlocked | TTS + Telegram | Telegram push |
| Home, locked | Telegram | Telegram push |
| **School** | **Dashboard** | **macOS notification** |
| Elsewhere, lid closed | Telegram, queued | Telegram push |

Selection reads from reachability, not from assumptions. A network that blocks Telegram behaves correctly even if it isn't school.

### Presence

Signals: Wi-Fi network name, idle/lock state, manual override with a six-hour expiry. No phone geofencing — the laptop travels with you.

Every reading carries a timestamp. Older than ~20 minutes is unknown, and consumers degrade rather than guess.

Arrival at home and lid-open are published as **events**, not conditions polled inside individual jobs. Multiple components subscribe.

### What presence buys

1. **Arrival-triggered deep work briefing** — fires when you actually sit down, not at a fixed 3:30 that's usually wrong
2. **Delivery mode** — speak aloud at home, stay silent at school
3. **Leave-by origin** — travel time needs a departure point
4. **Location-gated reminders** — held until home
5. **Focus coupling** — deep work block start toggles DND, end releases it

### Reachability

A module tracking Telegram, Gemini, and general network as timestamped state. Channel selection and dispatcher gating both read it, so failure handling is written once.

When Gemini is unreachable: say so in one line, no retry storm, queue `COMPOSE` work for later, and **tier-1 pattern-matched commands keep working.**

### Phone access

Tailscale. Phone and Mac on the same tailnet; the dashboard is reachable from any network with nothing exposed to the internet.

Two Mac-side changes that **must ship in the same commit**: the dashboard binds beyond loopback, and it gets a shared-token auth cookie. Right now "only this machine can reach it" *is* the security model; the moment that changes, auth has to already exist.

Telegram stays as fallback for now. Revisit after a month.

---

## 8. Scheduling

Every scheduled item is a persisted row, never an in-memory job. In-memory scheduling is an optimization layered on top, not the source of truth.

On wake, overdue items are replayed — but anything more than an hour late folds into the next briefing rather than firing. With the lid open most of the school day this is now an edge case rather than the norm, but it still has to be correct.

Pending items pointing at a completed to-do are **cancelled during reconciliation**, not skipped at fire time. Otherwise they sit in the queue and fire on wake before the next reconcile catches them.

---

## 9. To-dos

**The database holds the real data. Apple Reminders is the visible face.**

You see a normal Reminders list on Mac, phone, and watch. Behind it, Friday holds what Reminders can't: estimated effort, the reserved calendar block, source and source reference, and whether the item has already been mentioned in a briefing.

**iCloud does the syncing, not Friday.** A completion made on your phone mid-day is already there when the Mac catches up.

### Reconciliation

By comparison, not notification. Friday captures each reminder's identifier at creation and diffs against the list.

| Field | Source of truth |
|---|---|
| Completed, deleted | Reminders |
| Title, due date | Reminders, *if you changed them* |
| Everything else | The database |

Shadow copies of the last values Friday pushed are what distinguish "you edited this" from "nothing happened" — no timestamps or clocks required.

A vanished identifier means **dropped**, not done. Semantically different, and it shouldn't count toward completion.

**Reconcile on:** the normal poll cycle, on wake, before every briefing, and — most importantly — as a single-item lookup immediately before firing any reminder about a specific to-do. That last one is what stops Friday nagging about a paper submitted four minutes ago.

### Configuration

- Dedicated list named "Friday" — scopes queries, keeps personal lists untouched
- **Due dates carry no time.** A timed reminder fires its own iOS notification and competes with Friday's. Precise timing lives in the database.
- Items you add to that list manually get adopted automatically
- **Nightly prune at 3 AM, silent.** Reconcile first, then delete completed items older than 24 hours. The reverse order records finished work as abandoned.

A completed item is invisible everywhere except dashboard history. If early completion leaves a reserved calendar block empty, that block is removed without comment.

Behind a backend dispatcher — Apple on macOS, Google Tasks on Windows — same seam as the calendar backend.

---

## 10. Tool inventory

**Calendar:** create, update, delete, read, find free blocks, propose compressions.

**To-dos:** create, complete, defer, list, reconcile.

**Reminders:** schedule, cancel, snooze.

**Briefings:** morning, evening, deep work (arrival-triggered), on-demand.

**Email:** read and label continuously; draft on request; **never send.** Enforced by requesting a Google scope that excludes sending — the credential physically cannot send mail, which is stronger than any check in code. Labels from a fixed config list. Drafts land in Gmail for you to send. Requires contact resolution first.

**GroupMe:** on-demand search, no polling. Send on your behalf is a gated write.

**iMessage:** send only, always gated, never read. Reading needs Full Disk Access and breaks across OS updates. macOS-only; raises unsupported on Windows.

**Supporting:** contact resolution (ambiguity → clarify, never guess), leave-by alerts, Focus/DND toggle, note capture, Canvas grades.

---

## 11. Invariants

These are the rules that must survive every future change. Anything violating one is a bug regardless of what it enables.

1. **Every LLM call goes through the dispatcher.** No direct provider calls anywhere else.
2. **Tools never send messages.** They return effects.
3. **Permission cards are emitted first**, and nothing may delay, editorialize on, or bury one.
4. **A write is confirmed to the user only after the service confirms it back.**
5. **Every write carries a fingerprint** checked before writing and before retrying, so a client-side timeout on a server-side success cannot double-book.
6. **Persona voice applies only to conversational replies** — never cards, briefings, errors, or TTS.
7. **Reconcile before prune.** Always.
8. **Presence is always a value plus a timestamp.** Stale means unknown, never assumed.
9. **The dashboard is a channel adapter**, not a second chat implementation.
10. **No webhooks.** Polling only — webhooks break sleep/wake queuing and complicate the Windows build.

---

## 12. Build order

Each step ends with a running system. Steps 1–4 are independently revertable.

| # | Step | Verification |
|---|---|---|
| 1 | Dispatcher + Gemini provider, one `CHAT` profile, no tools, no persona | A Telegram message round-trips. Exchange log shows profile, tokens, latency. |
| 2 | Persona sections; wire `CLASSIFY` and `COMPOSE` | Prompt size for a classify call is visibly smaller in the log than a chat call. Edit AGENTS.md, observe tone change without restart. |
| 3 | Tool registry + turn loop, two read-only tools | A calendar question triggers a tool call and returns real data. Hop limit observably enforced. |
| 4 | Effects layer + permission card as effect, then first gated write | Card arrives before any other output. Confirming commits; cancelling doesn't. No stray characters after the tool call. |
| 5 | **Dashboard channel** | Chat works in the browser with Wi-Fi off. History is shared with Telegram. A card confirmed in the dashboard resolves the same pending action. |
| 6 | Context and policy extraction | Briefings and chat use the same formatter. Gating decisions come from one module. |
| 7 | Plan router + pattern-matched fast path | Frequent commands complete with zero LLM calls logged. Works with the API unreachable. |
| 8 | To-dos: schema, backend dispatcher, reconciliation | Check an item off on your phone; it reflects on the Mac. A dropped item records as dropped, not done. |
| 9 | Presence, reachability, arrival triggers | Deep work briefing fires on arrival home. Channel switches to dashboard on school Wi-Fi. |
| 10 | Leave-by, Focus toggle, contact resolution | — |
| 11 | Email, then iMessage | Scope check: the credential cannot send mail. |

**Verification is behavioral, not compilation.** Proof of a change is observed output — a tone shift, a card staying literal, a smaller prompt in the log. Not a successful build. Confirm the daemon was restarted before any output was observed.

---

## 13. Open

- Whether Telegram stays after a month of dashboard use
- Whether a one-way projection of to-dos to watch/Siri is worth building — only after the schema stops moving
- Windows to-do path (Google Tasks) — designed, not scheduled

---

## Appendix — implementation detail

Skip unless handing this to Claude Code.

**To-do storage.** A `todos` table: title, due date, estimated minutes, source and source reference, reserved calendar block UID, status (open/done/deferred/dropped), deferred-until, last-surfaced-in-briefing timestamp. Reconciliation columns: external identifier, external backend, shadow copies of last-pushed title and due date, last reconcile time.

This table is genuine content, not operational state — a deliberate exception to the existing SQLite rule. Document it as such in AGENTS.md or a future maintainer deletes it on principle.

**Idempotency.** A `recent_writes` table keyed on a hash of title, date, start time, and calendar, with a ten-minute expiry. Checked before every write and before every retry.

**Reminders integration.** Capture the identifier from the same `osascript` invocation that creates the reminder — creating then re-querying by title is racy and title collisions are real. Write the database row ID into the reminder's notes field as a recovery path. All scripting runs in the executor; calls can take seconds and must never block the loop. Prune completed items after roughly a week regardless of status, since Reminders scripting degrades on long lists.

**Dashboard transport.** Server-Sent Events over a single long-lived GET rather than WebSocket — a dozen lines, reconnects natively, and only server-to-client push is needed. Bind either to all interfaces with a firewall rule limiting to the tailnet, or to the Tailscale address specifically. The latter is tighter: nothing on school Wi-Fi can see the port at all. Auth is a single shared token in a cookie — one user, one device, no session management.

**Tailscale.** MagicDNS enabled so the dashboard is a hostname rather than an IP. WireGuard uses UDP/41641 with fallback to relays over TCP/443, which is what carries it through school filtering.

**Reachability probes.** Cached HEAD requests, each carrying state plus observation timestamp. The dispatcher classifies network-unreachable distinctly from rate limits and fatal errors.

**Executor discipline.** Every blocking call — `osascript`, HTTP, LLM — runs in the executor with a ceiling above the worst-case provider timeout, so a hung call fails loudly and releases the pipeline rather than wedging it.