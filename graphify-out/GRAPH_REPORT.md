# Graph Report - /Users/keller/friday  (2026-08-02)

## Corpus Check
- 63 files · ~77,025 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 767 nodes · 1291 edges · 52 communities (46 shown, 6 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 42 edges (avg confidence: 0.69)
- Token cost: 34,369 input · 3,108 output

## Community Hubs (Navigation)
- Briefing Composition
- Dashboard Frontend
- Voice Audio Stream
- Dashboard API Server
- macOS Menu Bar App
- Setup Wizard Validation
- Calendar Write Approval Gate
- Apple Calendar Write + GCal Sync
- Windows Tray Supervisor
- Apple Calendar Reads (EventKit/JXA)
- Menu Bar Icon Rendering
- GroupMe Connector + DB
- Google Calendar Backend
- macOS App Supervisor
- Paths and Quip Phrases
- Voice Listener Entry Point
- LaunchAgent Installation
- Wizard Step Screens
- Wizard State Machine
- Voice Config Loading
- Telegram Handler + Semaphore
- Telethon Voice Bridge
- Activity Logging
- Wake Word Detection
- Text-to-Speech Output
- Agent Think Loop
- Agent Tools + Windows Compat
- Canvas Connector
- Wizard Form Widgets
- Push-to-Talk Listener
- SQLite System State
- Weather Connector
- Persona Composition
- Application Entry Point
- Wake Model Resolution
- macOS Icon Generation
- Voice App Launcher
- Windows Icon Generation
- macOS Build Script
- Dashboard Static Assets
- Restart Script
- Run Script
- Windows Build Pipeline
- Voice Models README

## God Nodes (most connected - your core abstractions)
1. `Wizard` - 30 edges
2. `FridayMenuBar` - 21 edges
3. `AudioStream` - 19 edges
4. `FridayTray` - 16 edges
5. `calendars/google_cal.py` - 15 edges
6. `voice/listen.py` - 15 edges
7. `VoiceListener` - 15 edges
8. `get()` - 13 edges
9. `TelegramBridge` - 13 edges
10. `bundle_briefing_context()` - 12 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `FridayAgent`  [INFERRED]
  friday/friday.py → friday/agent/core.py
- `_NoCacheStatic` --uses--> `TelegramHandler`  [INFERRED]
  friday/dashboard/server.py → friday/channels/telegram.py
- `PauseRequest` --uses--> `TelegramHandler`  [INFERRED]
  friday/dashboard/server.py → friday/channels/telegram.py
- `main()` --calls--> `TelegramHandler`  [INFERRED]
  friday/friday.py → friday/channels/telegram.py
- `_migrate_config()` --calls--> `normalize_priority()`  [INFERRED]
  friday/dashboard/server.py → friday/connectors/groupme.py

## Import Cycles
- 1-file cycle: `friday/channels/telegram.py -> friday/channels/telegram.py`

## Hyperedges (group relationships)
- **Friday Persona & Voice** — friday_agents_md, friday_soul_md, friday_quips_yaml [EXTRACTED 0.90]
- **Windows Port Infrastructure** — github_workflows_build_windows_yml, packaging_windows_build_windows_md, friday_config_yaml [EXTRACTED 0.90]
- **Data Ingestion Flow** — friday_connectors_canvas, friday_connectors_groupme, friday_memory_db, friday_agent_core [EXTRACTED 1.00]
- **Calendar Backend Abstraction** — friday_calendars_backend, friday_calendars_apple, friday_calendars_google_cal [EXTRACTED 1.00]
- **User Interface Layer** — friday_channels_telegram, friday_menubar_py, friday_tray_py, friday_dashboard [INFERRED 0.90]
- **Wizard UX and Validation Overhaul** — friday_setup_wizard, wizard_back_navigation, wizard_live_validation [EXTRACTED 1.00]

## Communities (52 total, 6 thin omitted)

### Community 0 - "Briefing Composition"
Cohesion: 0.08
Nodes (52): _block_canvas(), _block_day_events(), _block_groupme(), _block_weather(), _block_week(), bundle_briefing_context(), _canvas_block(), compose_evening() (+44 more)

### Community 1 - "Dashboard Frontend"
Cohesion: 0.10
Nodes (43): api, bindInput(), boot(), drawPending(), escapeAttr(), escapeHtml(), fetchGroups(), FLASH (+35 more)

### Community 2 - "Voice Audio Stream"
Cohesion: 0.08
Nodes (20): Event, AudioStream, ClapDetector, Consumer, _pcm_to_wav_bytes(), ndarray, Audio I/O for the voice subsystem.  One PyAudio input stream feeds a shared thre, Suspend dispatch to consumers. The underlying stream keeps reading         so th (+12 more)

### Community 3 - "Dashboard API Server"
Cohesion: 0.09
Nodes (34): BaseModel, FastAPI, _build_activity_feed(), create_app(), _kind_for_tool(), _load_config(), _mask_secrets(), _migrate_config() (+26 more)

### Community 4 - "macOS Menu Bar App"
Cohesion: 0.09
Nodes (15): _fmt_clock(), _fmt_int(), _friday_running_proc(), FridayMenuBar, menubar.py F.R.I.D.A.Y. menu bar app — standalone rumps app, never imported by f, Re-pick icons if the user dropped/updated a custom PNG., Replace the default Python rocket Dock icon with the user PNG,         center-cr, Compute which cached icon should currently be displayed. Listening         only (+7 more)

### Community 5 - "Setup Wizard Validation"
Cohesion: 0.07
Nodes (29): Friday Config File, _calendar_backend(), _canvas_whoami(), _chat_id_ok(), _guess_timezone(), _macos_foreground(), _mask_token(), setup_wizard.py First-run setup wizard for the packaged Windows and macOS builds (+21 more)

### Community 6 - "Calendar Write Approval Gate"
Cohesion: 0.11
Nodes (27): auto_write(), cancel_pending(), confirm_pending(), _confirmation_date(), format_confirmation(), _format_when(), _friendly_date(), _friendly_date_phrase() (+19 more)

### Community 7 - "Apple Calendar Write + GCal Sync"
Cohesion: 0.11
Nodes (28): calendars/apple.py, calendar_exists(), list_calendars(), list_calendars_detailed(), datetime, calendars/apple.py Apple Calendar backend — reads and writes via JXA (osascript, Calendar names only. See list_calendars_detailed for the caveats., Create an event in the named Apple Calendar. Returns Apple UID or None     on fa (+20 more)

### Community 8 - "Windows Tray Supervisor"
Cohesion: 0.11
Nodes (13): _acquire_singleton(), _api(), FridayTray, main(), _make_icon_image(), Popen, socket, tray.py Windows entry point — system tray app that supervises the Friday core. (+5 more)

### Community 9 - "Apple Calendar Reads (EventKit/JXA)"
Cohesion: 0.12
Nodes (26): ensure_calendar_access(), _eventkit_events(), _eventkit_store(), events_for_day(), events_in_window(), _jxa_events(), _jxa_names_script(), _jxa_script() (+18 more)

### Community 10 - "Menu Bar Icon Rendering"
Cohesion: 0.14
Nodes (24): _attributed(), _build_from_text(), _build_from_user_icon(), _cache_is_stale(), circular_crop(), _dimmed(), ensure_favicon(), ensure_icons() (+16 more)

### Community 11 - "GroupMe Connector + DB"
Cohesion: 0.13
Nodes (20): connectors/groupme.py, fetch(), _forget_cursor(), _get_messages(), normalize_priority(), _poll_one(), _populate_name_cache(), Connection (+12 more)

### Community 12 - "Google Calendar Backend"
Cohesion: 0.16
Nodes (21): calendars/google_cal.py, calendar_exists(), _calendar_map(), _ensure_calendar(), events_for_day(), events_in_window(), _get_service(), load_credentials() (+13 more)

### Community 13 - "macOS App Supervisor"
Cohesion: 0.15
Nodes (16): _acquire_singleton(), _api(), _become_accessory(), CoreSupervisor, main(), _prime_calendar_access(), Popen, socket (+8 more)

### Community 14 - "Paths and Quip Phrases"
Cohesion: 0.17
Nodes (19): config_path(), data_dir(), db_path(), google_client_secret_path(), google_token_path(), log_dir(), Path, paths.py Single source of truth for where Friday's files live.  macOS (source ch (+11 more)

### Community 15 - "Voice Listener Entry Point"
Cohesion: 0.14
Nodes (13): Telegram bridge — talks to the Friday bot AS the user account via Telethon.  Why, voice/listen.py, friday_is_running(), _load_whisper(), main(), _probe_microphone(), Friday voice listener — standalone entry point.  Boots audio + wake + clap + PTT, Whisper accepts a numpy array of float32 at 16 kHz. Decode the WAV     in-memory (+5 more)

### Community 16 - "LaunchAgent Installation"
Cohesion: 0.19
Nodes (18): install_agents(), is_macos(), Path, macos_setup.py Generates Friday's macOS LaunchAgents and the voice launcher conf, Write (and reload) Friday's LaunchAgents. Returns the paths written., Locate FridayVoice.app in a source checkout or inside a frozen bundle., Write the two-line conf the C launcher reads (interpreter, then script).      Th, bootout + bootstrap so an edited plist actually takes effect. (+10 more)

### Community 17 - "Wizard Step Screens"
Cohesion: 0.24
Nodes (5): Render numbered instructions for creating a credential.          Every credentia, Big obvious 'take me there' button. Paired with _walkthrough so the         user, A gotcha the user will otherwise hit and misdiagnose., Dispatch on the active backend. Apple needs no accounts or keys —         just m, Label

### Community 18 - "Wizard State Machine"
Cohesion: 0.18
Nodes (6): Populate the in-memory model from the on-disk config. Used at         startup an, Default collector: snapshot every registered StringVar into the         model. S, Snapshot the current step into the model and move forward. Steps         whose v, Toggle the 'Checking…' state: disable nav while a network check runs         so, Start over: clear the in-memory model and any stale config on disk so         a, Wizard

### Community 19 - "Voice Config Loading"
Cohesion: 0.22
Nodes (15): Any, _build(), _coerce_bool(), _coerce_float(), _coerce_int(), _find_repo_root(), load(), persist_telethon_session() (+7 more)

### Community 20 - "Telegram Handler + Semaphore"
Cohesion: 0.17
Nodes (9): DEFAULT_TYPE, Connection, Entry point for all text messages. Semaphore serializes processing., Entry point for photos and PDF documents → calendar event extraction.         Sa, Handle inline button taps. Stale callbacks are silently discarded., Approval gate card — wired up in Phase 4., Pause gate — dashboard sets system_state.paused = "true". Silent         drop so, TelegramHandler (+1 more)

### Community 21 - "Telethon Voice Bridge"
Cohesion: 0.16
Nodes (6): Send `text` to the Friday bot AS the user. Wait up to `timeout`         seconds, After connect() succeeds, returns the StringSession blob to persist.         Ret, Poll the connection state every 500 ms while waiting for the bot         reply., Initialize the bridge.          `session_string` is an opaque base64-ish blob pr, Start the asyncio loop in a worker thread and run the Telethon         client lo, TelegramBridge

### Community 22 - "Activity Logging"
Cohesion: 0.23
Nodes (13): cleanup_old_activity(), _preview(), Connection, memory/activity.py Best-effort recorders for the activity-capture tables (see me, Delete activity rows older than `days`. Returns total rows removed.     Called n, One row per _think() call. Full prompt/response stored verbatim so     /api/llm/, One row per Gemini function-call invocation., One row per briefing actually sent. (+5 more)

### Community 23 - "Wake Word Detection"
Cohesion: 0.17
Nodes (7): ndarray, Override the firing threshold for one phrase., Clear internal buffer + per-phrase cooldowns. Call after a wake-cycle         fi, Feed one chunk of audio. Returns the first phrase that fires this call,, Streaming wake-word detector.      Args:         phrases: ordered list of wake p, WakeDetector, WakeHit

### Community 24 - "Text-to-Speech Output"
Cohesion: 0.26
Nodes (11): _cap_length(), _device_names(), external_audio_present(), Text-to-speech via macOS `say`, with markdown stripping and length capping.  Out, Speak `text` via macOS `say -v <voice>`. Non-blocking — returns the     worker t, True if any active output device looks like AirPods / headphones / USB /     Blu, _say_worker(), speak() (+3 more)

### Community 25 - "Agent Think Loop"
Cohesion: 0.25
Nodes (6): FridayAgent, Bump system_state counters. Best-effort — never raise from here., Call Gemini generate_content with backoff on transient 503/504/429         and o, Synchronous LLM call. Always run via run_in_executor inside async handlers., Extract ONE calendar event from a photo or PDF the user sent, then         route, LLM extraction JSON → gated_write-shaped event dict         (title/date/start_ti

### Community 26 - "Agent Tools + Windows Compat"
Cohesion: 0.18
Nodes (9): make_tools(), agent/tools.py Synchronous tool functions Gemini can invoke. The google-genai SD, Return the list of tool callables bound to this Friday instance.      `agent` is, listening_flag_path(), Path, compat.py Small cross-platform shims so the same codebase runs on macOS and Wind, Portable strftime. glibc's no-pad flag ('%-d', '%-I') raises on     Windows, whe, Transient flag file voice/listen.py touches during a PTT/wake session.     /tmp (+1 more)

### Community 27 - "Canvas Connector"
Cohesion: 0.27
Nodes (11): connectors/canvas.py, _due_at_local(), fetch(), Connection, datetime, connectors/canvas.py Read-only Canvas LMS connector. Fetches the iCal feed, dedu, Write unsynced Canvas events with a future due_at to the 'Canvas'     calendar (, (YYYY-MM-DD, HH:MM or None, local-aware datetime). All-day inputs     (no 'T') s (+3 more)

### Community 28 - "Wizard Form Widgets"
Cohesion: 0.24
Nodes (7): _combobox(), _init_palette(), Pick colours from the theme's own background luminance.      Reading it out of T, A ttk.Combobox whose open drop-down tracks the pointer.      Tk's built-in <Moti, Default-calendar combo + briefing-calendar multiselect. Populated by         whi, Misc, StringVar

### Community 29 - "Push-to-Talk Listener"
Cohesion: 0.24
Nodes (4): PTTListener, Push-to-talk key handler.  Wraps a `pynput.keyboard.Listener` so the PTT key (de, Global key listener. Fires `on_press_cb` once when the PTT key goes     down (ed, _resolve_key()

### Community 30 - "SQLite System State"
Cohesion: 0.29
Nodes (9): channels/telegram.py, channels/telegram.py Telegram interface for Friday.  Inbound: async PTB handlers, delete(), get(), Connection, memory/state.py Helpers for the system_state table. Replaces state.json entirely, Write multiple keys in a single transaction., set() (+1 more)

### Community 31 - "Weather Connector"
Cohesion: 0.33
Nodes (10): connectors/weather.py, fetch(), _intent(), _parse_time(), datetime, connectors/weather.py Stateless weather fetch. No storage, no side effects. Call, Return (start_hour, end_hour, label, tomorrow) or None for no specific time., Return a natural-language answer to a weather query, or '' on failure. (+2 more)

### Community 32 - "Persona Composition"
Cohesion: 0.29
Nodes (7): agent/core.py, _compose_persona(), _load_persona_base(), _pdf_to_png_pages(), agent/core.py LLM calls only. No routing, no state, no Telegram references., Rasterize a PDF (from bytes) into one PNG per page, capped at     _PDF_MAX_PAGES, AGENTS.md prose + a rendered block built from config['persona'].

### Community 33 - "Application Entry Point"
Cohesion: 0.32
Nodes (7): dashboard/, check_environment(), load_config(), main(), friday.py Project Friday — entry point.  PTB Application owns the main event loo, Remove internal LLM-only tags from an event body for user-facing display., _strip_internal_tags()

### Community 34 - "Wake Model Resolution"
Cohesion: 0.43
Nodes (6): _bundled_hey_jarvis_path(), Path, Wake-word detection wrapper over openWakeWord.  Friday's voice layer feeds 16 kH, Pick the .onnx for a phrase. Custom model in voice/models/ wins; otherwise     a, _resolve_model_path(), _slug()

### Community 35 - "macOS Icon Generation"
Cohesion: 0.47
Nodes (5): badge(), _font(), main(), Image, Generate friday.icns for Friday.app — the same orange 'F' badge the menu bar dra

### Community 36 - "Voice App Launcher"
Cohesion: 0.83
Nodes (3): bundle_root(), main(), read_conf()

### Community 37 - "Windows Icon Generation"
Cohesion: 0.50
Nodes (3): badge(), Image, Generate friday.ico (multi-size) for the Windows exe and installer. Same orange

## Knowledge Gaps
- **16 isolated node(s):** `FLASH`, `api`, `ROUTES`, `KIND_ORDER`, `JARVIS_DEFAULT` (+11 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `voice/listen.py` connect `Voice Listener Entry Point` to `Voice Audio Stream`, `Wake Model Resolution`, `Voice Config Loading`, `Text-to-Speech Output`, `Push-to-Talk Listener`, `SQLite System State`?**
  _High betweenness centrality (0.240) - this node is a cross-community bridge._
- **Why does `channels/telegram.py` connect `SQLite System State` to `Persona Composition`, `Application Entry Point`, `Telegram Handler + Semaphore`, `Voice Listener Entry Point`?**
  _High betweenness centrality (0.229) - this node is a cross-community bridge._
- **Why does `calendars/google_cal.py` connect `Google Calendar Backend` to `Canvas Connector`, `Paths and Quip Phrases`, `Apple Calendar Write + GCal Sync`?**
  _High betweenness centrality (0.162) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `AudioStream` (e.g. with `VoiceListener` and `.__init__()`) actually correct?**
  _`AudioStream` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `FLASH`, `api`, `ROUTES` to the rest of the system?**
  _16 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Briefing Composition` be split into smaller, more focused modules?**
  _Cohesion score 0.08055152394775036 - nodes in this community are weakly interconnected._
- **Should `Dashboard Frontend` be split into smaller, more focused modules?**
  _Cohesion score 0.09898242368177614 - nodes in this community are weakly interconnected._