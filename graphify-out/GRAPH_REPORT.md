# Graph Report - .  (2026-07-15)

## Corpus Check
- 55 files · ~58,222 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 644 nodes · 1087 edges · 40 communities (36 shown, 4 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 39 edges (avg confidence: 0.67)
- Token cost: 21,897 input · 2,077 output

## Community Hubs (Navigation)
- Briefing Composition
- Dashboard Frontend
- Voice Telegram Bridge
- Voice Audio Capture
- Dashboard API Server
- Calendar Actions & Approvals
- macOS Menu Bar App
- Windows Tray Supervisor
- Menu Bar Icon Generation
- Paths, Phrases & Quips
- Setup Wizard UI
- Google Calendar Backend
- Wake Word Detection
- Apple Calendar Backend
- Project Docs & Architecture
- Voice Config Loader
- Telegram Message Handlers
- Agent Tools & Canvas
- Entry Point & Database
- Activity Logging
- GroupMe Connector
- Text-to-Speech Output
- Gemini Agent Core
- Telegram Channel & State
- Weather Connector
- Calendar Backend Dispatch
- Persona & PDF Handling
- Google Calendar Sync
- Windows Icon Generator
- Dashboard Static Assets
- Restart Script
- Run Script
- Windows Build CI

## God Nodes (most connected - your core abstractions)
1. `Wizard` - 21 edges
2. `FridayMenuBar` - 19 edges
3. `AudioStream` - 19 edges
4. `FridayTray` - 16 edges
5. `VoiceListener` - 15 edges
6. `get()` - 13 edges
7. `TelegramBridge` - 13 edges
8. `saveConfig()` - 12 edges
9. `TelegramHandler` - 11 edges
10. `_local()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `voice/listen.py` --conceptually_related_to--> `voice/models/README.md`  [INFERRED]
  voice/listen.py → friday/voice/models/README.md
- `agent/core.py` --calls--> `memory/db.py`  [INFERRED]
  agent/core.py → memory/db.py
- `channels/telegram.py` --calls--> `agent/core.py`  [INFERRED]
  channels/telegram.py → agent/core.py
- `main()` --calls--> `FridayAgent`  [INFERRED]
  friday/friday.py → friday/agent/core.py
- `_NoCacheStatic` --uses--> `TelegramHandler`  [INFERRED]
  friday/dashboard/server.py → friday/channels/telegram.py

## Import Cycles
- 1-file cycle: `friday/channels/telegram.py -> friday/channels/telegram.py`

## Hyperedges (group relationships)
- **Friday Core Architecture** — friday_py, agent_core_py, channels_telegram_py, memory_db_py [EXTRACTED 1.00]
- **Friday Persona & Voice** — friday_agents_md, friday_soul_md, friday_quips_yaml [EXTRACTED 0.90]
- **Windows Port Infrastructure** — github_workflows_build_windows_yml, packaging_windows_build_windows_md, friday_config_yaml [EXTRACTED 0.90]

## Communities (40 total, 4 thin omitted)

### Community 0 - "Briefing Composition"
Cohesion: 0.09
Nodes (47): _block_canvas(), _block_day_events(), _block_groupme(), _block_weather(), _block_week(), bundle_briefing_context(), _canvas_block(), compose_evening() (+39 more)

### Community 1 - "Dashboard Frontend"
Cohesion: 0.10
Nodes (43): api, bindInput(), boot(), drawPending(), escapeAttr(), escapeHtml(), fetchGroups(), FLASH (+35 more)

### Community 2 - "Voice Telegram Bridge"
Cohesion: 0.06
Nodes (22): Telegram bridge — talks to the Friday bot AS the user account via Telethon.  Why, Send `text` to the Friday bot AS the user. Wait up to `timeout`         seconds, After connect() succeeds, returns the StringSession blob to persist.         Ret, Poll the connection state every 500 ms while waiting for the bot         reply., Initialize the bridge.          `session_string` is an opaque base64-ish blob pr, Start the asyncio loop in a worker thread and run the Telethon         client lo, TelegramBridge, friday_is_running() (+14 more)

### Community 3 - "Voice Audio Capture"
Cohesion: 0.08
Nodes (20): Event, AudioStream, ClapDetector, Consumer, _pcm_to_wav_bytes(), ndarray, Audio I/O for the voice subsystem.  One PyAudio input stream feeds a shared thre, Suspend dispatch to consumers. The underlying stream keeps reading         so th (+12 more)

### Community 4 - "Dashboard API Server"
Cohesion: 0.09
Nodes (34): BaseModel, FastAPI, _build_activity_feed(), create_app(), _kind_for_tool(), _load_config(), _mask_secrets(), _migrate_config() (+26 more)

### Community 5 - "Calendar Actions & Approvals"
Cohesion: 0.08
Nodes (33): auto_write(), cancel_pending(), confirm_pending(), _confirmation_date(), format_confirmation(), _format_when(), _friendly_date(), _friendly_date_phrase() (+25 more)

### Community 6 - "macOS Menu Bar App"
Cohesion: 0.11
Nodes (13): _fmt_clock(), _fmt_int(), _friday_running_proc(), FridayMenuBar, menubar.py F.R.I.D.A.Y. menu bar app — standalone rumps app, never imported by f, Re-pick icons if the user dropped/updated a custom PNG., Replace the default Python rocket Dock icon with the user PNG,         center-cr, Compute which cached icon should currently be displayed. Listening         only (+5 more)

### Community 7 - "Windows Tray Supervisor"
Cohesion: 0.11
Nodes (13): _acquire_singleton(), _api(), FridayTray, main(), _make_icon_image(), tray.py Windows entry point — system tray app that supervises the Friday core., Velopack update check — shortly after boot, then every few hours.         Applyi, Ask the core to exit cleanly via its own API; fall back to a hard         termin (+5 more)

### Community 8 - "Menu Bar Icon Generation"
Cohesion: 0.14
Nodes (24): _attributed(), _build_from_text(), _build_from_user_icon(), _cache_is_stale(), circular_crop(), _dimmed(), ensure_favicon(), ensure_icons() (+16 more)

### Community 9 - "Paths, Phrases & Quips"
Cohesion: 0.12
Nodes (23): config_path(), data_dir(), db_path(), google_client_secret_path(), google_token_path(), log_dir(), Path, paths.py Single source of truth for where Friday's files live.  macOS (source ch (+15 more)

### Community 10 - "Setup Wizard UI"
Cohesion: 0.27
Nodes (4): Initial value for a re-shown step's entry: whatever the user         already typ, Wizard, Label, StringVar

### Community 11 - "Google Calendar Backend"
Cohesion: 0.16
Nodes (20): calendar_exists(), _calendar_map(), _ensure_calendar(), events_for_day(), events_in_window(), _get_service(), load_credentials(), date (+12 more)

### Community 12 - "Wake Word Detection"
Cohesion: 0.13
Nodes (13): _bundled_hey_jarvis_path(), ndarray, Path, Wake-word detection wrapper over openWakeWord.  Friday's voice layer feeds 16 kH, Override the firing threshold for one phrase., Clear internal buffer + per-phrase cooldowns. Call after a wake-cycle         fi, Feed one chunk of audio. Returns the first phrase that fires this call,, Pick the .onnx for a phrase. Custom model in voice/models/ wins; otherwise     a (+5 more)

### Community 13 - "Apple Calendar Backend"
Cohesion: 0.16
Nodes (16): calendar_exists(), datetime, calendars/apple.py Apple Calendar backend — reads and writes via JXA (osascript, Create an event in the named Apple Calendar. Returns Apple UID or None     on fa, _run_jxa(), write_event(), events_for_day(), events_in_window() (+8 more)

### Community 14 - "Project Docs & Architecture"
Cohesion: 0.13
Nodes (11): actions/calendar.py, actions/groupme_send.py, agent/core.py, channels/telegram.py, connectors/canvas.py, connectors/groupme.py, connectors/weather.py, memory/db.py (+3 more)

### Community 15 - "Voice Config Loader"
Cohesion: 0.22
Nodes (15): Any, _build(), _coerce_bool(), _coerce_float(), _coerce_int(), _find_repo_root(), load(), persist_telethon_session() (+7 more)

### Community 16 - "Telegram Message Handlers"
Cohesion: 0.17
Nodes (9): DEFAULT_TYPE, Connection, Entry point for all text messages. Semaphore serializes processing., Entry point for photos and PDF documents → calendar event extraction.         Sa, Handle inline button taps. Stale callbacks are silently discarded., Approval gate card — wired up in Phase 4., Pause gate — dashboard sets system_state.paused = "true". Silent         drop so, TelegramHandler (+1 more)

### Community 17 - "Agent Tools & Canvas"
Cohesion: 0.19
Nodes (13): make_tools(), agent/tools.py Synchronous tool functions Gemini can invoke. The google-genai SD, Return the list of tool callables bound to this Friday instance.      `agent` is, _due_at_local(), fetch(), Connection, datetime, connectors/canvas.py Read-only Canvas LMS connector. Fetches the iCal feed, dedu (+5 more)

### Community 18 - "Entry Point & Database"
Cohesion: 0.19
Nodes (9): check_environment(), load_config(), main(), friday.py Project Friday — entry point.  PTB Application owns the main event loo, Remove internal LLM-only tags from an event body for user-facing display., _strip_internal_tags(), Database, Connection (+1 more)

### Community 19 - "Activity Logging"
Cohesion: 0.23
Nodes (13): cleanup_old_activity(), _preview(), Connection, memory/activity.py Best-effort recorders for the activity-capture tables (see me, Delete activity rows older than `days`. Returns total rows removed.     Called n, One row per _think() call. Full prompt/response stored verbatim so     /api/llm/, One row per Gemini function-call invocation., One row per briefing actually sent. (+5 more)

### Community 20 - "GroupMe Connector"
Cohesion: 0.26
Nodes (11): fetch(), _get_messages(), _poll_one(), _populate_name_cache(), Connection, connectors/groupme.py Read-only GroupMe connector. Polls each configured group's, # TODO: events table can produce duplicate rows for the same GroupMe, Wrapper around the messages endpoint. Returns [] on any failure     (304, networ (+3 more)

### Community 21 - "Text-to-Speech Output"
Cohesion: 0.26
Nodes (11): _cap_length(), _device_names(), external_audio_present(), Text-to-speech via macOS `say`, with markdown stripping and length capping.  Out, Speak `text` via macOS `say -v <voice>`. Non-blocking — returns the     worker t, True if any active output device looks like AirPods / headphones / USB /     Blu, _say_worker(), speak() (+3 more)

### Community 22 - "Gemini Agent Core"
Cohesion: 0.25
Nodes (6): FridayAgent, Bump system_state counters. Best-effort — never raise from here., Call Gemini generate_content with backoff on transient 503/504/429         and o, Synchronous LLM call. Always run via run_in_executor inside async handlers., Extract ONE calendar event from a photo or PDF the user sent, then         route, LLM extraction JSON → gated_write-shaped event dict         (title/date/start_ti

### Community 23 - "Telegram Channel & State"
Cohesion: 0.29
Nodes (8): channels/telegram.py Telegram interface for Friday.  Inbound: async PTB handlers, delete(), get(), Connection, memory/state.py Helpers for the system_state table. Replaces state.json entirely, Write multiple keys in a single transaction., set(), set_many()

### Community 24 - "Weather Connector"
Cohesion: 0.33
Nodes (9): fetch(), _intent(), _parse_time(), datetime, connectors/weather.py Stateless weather fetch. No storage, no side effects. Call, Return (start_hour, end_hour, label, tomorrow) or None for no specific time., Return a natural-language answer to a weather query, or '' on failure., respond() (+1 more)

### Community 25 - "Calendar Backend Dispatch"
Cohesion: 0.39
Nodes (8): backend_name(), calendar_exists(), events_for_day(), events_in_window(), init(), _mod(), calendars/backend.py Calendar backend dispatch — the one place that decides whet, write_event()

### Community 26 - "Persona & PDF Handling"
Cohesion: 0.29
Nodes (6): _compose_persona(), _load_persona_base(), _pdf_to_png_pages(), agent/core.py LLM calls only. No routing, no state, no Telegram references., Rasterize a PDF (from bytes) into one PNG per page, capped at     _PDF_MAX_PAGES, AGENTS.md prose + a rendered block built from config['persona'].

### Community 27 - "Google Calendar Sync"
Cohesion: 0.36
Nodes (7): fetch(), Connection, connectors/gcal_sync.py Mirror Google Calendar iCal subscriptions into named App, iCal DTSTART may be timed (datetime) or all-day (date). Return     (start_dt, en, Sync all configured Google calendars. Returns total new events written., _resolve_times(), _sync_one()

### Community 28 - "Windows Icon Generator"
Cohesion: 0.50
Nodes (3): Image, badge(), Generate friday.ico (multi-size) for the Windows exe and installer. Same orange

## Knowledge Gaps
- **18 isolated node(s):** `FLASH`, `api`, `ROUTES`, `KIND_ORDER`, `JARVIS_DEFAULT` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Wizard` connect `Setup Wizard UI` to `Paths, Phrases & Quips`?**
  _High betweenness centrality (0.283) - this node is a cross-community bridge._
- **Why does `VoiceListener` connect `Voice Telegram Bridge` to `Voice Audio Capture`, `Wake Word Detection`?**
  _High betweenness centrality (0.157) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `AudioStream` (e.g. with `VoiceListener` and `.__init__()`) actually correct?**
  _`AudioStream` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `VoiceListener` (e.g. with `AudioStream` and `ClapDetector`) actually correct?**
  _`VoiceListener` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `FLASH`, `api`, `ROUTES` to the rest of the system?**
  _18 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Briefing Composition` be split into smaller, more focused modules?**
  _Cohesion score 0.08687943262411348 - nodes in this community are weakly interconnected._
- **Should `Dashboard Frontend` be split into smaller, more focused modules?**
  _Cohesion score 0.09898242368177614 - nodes in this community are weakly interconnected._