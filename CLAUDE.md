# CLAUDE.md

This file provides strict guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Project Friday is a proactive, hyper-optimized AI scheduling assistant that runs as a background process on macOS. It monitors GroupMe groups and iMessage, filters messages for scheduling signals, reasons about them with Google Gemini, and proposes calendar actions to the user via iMessage — always waiting for explicit approval before acting.

Current status: **Phase 2** (two-way iMessage, Apple Calendar read/write, permission gate). The running system uses the Google Gemini API (`google-genai` SDK, `GEMINI_API_KEY`).

---

## The Hybrid Engine Philosophy & Architecture Constraints

**Absolute Token Efficiency:** The LLM is a scarce, metered resource. Maximize local compute. If an incoming message can be pre-filtered, sanitized, or chronologically parsed using local Python logic, it must be handled completely before hitting the Gemini API to prevent context-window ballooning.

### 1. Data Sterilization (Injection Prevention)
* **Strict SQL Parameterization:** Direct string formatting, concatenation, or f-strings must NEVER be used to insert dynamic text variables into SQLite queries targeting `~/Library/Messages/chat.db`.
* **The Vulnerability:** Conversational single quotes (e.g., *"I'm at the library"* or *"We're meeting now"*) will break raw SQL query syntax, causing database syntax exceptions that crash the background listener threads.
* **The Rule:** All database read/write operations inside `channels/imessage.py`, `agent/memory.py`, or debug modules must strictly enforce placeholder tuple formatting:
  ```python
  # CORRECT IMPLEMENTATION
  cursor.execute("SELECT * FROM message WHERE text LIKE ?", (f"%{text_var}%",))
  ```

### 2. Native Semantic Date Pre-Parsing (0-Token Logic)
* Before passing raw message string components down to the `agent/core.py` `_think()` loop, the incoming data stream must pass through a local python validation hook.
* **The Extraction Task:** Implement a localized extraction module using native regex or lightweight python time libraries to isolate conversational timeline signatures (e.g., *"tonight"*, *"tomorrow"*, *"May 5"*, or *"next Tuesday"*).
* **The Output Target:** Translate those relative terms into strict, structured ISO-8601 strings and append them alongside the message metadata. This ensures Gemini evaluates minimal, highly deterministic tracking data rather than using expansive, raw context prompts to reason through calendar dates.

---

## Running Friday

```bash
cd friday/
pip3 install -r requirements.txt
export GEMINI_API_KEY="your-key-here"
python3 friday.py
```

Run in background:
```bash
nohup python3 friday.py > logs/friday.log 2>&1 &
echo $! > logs/friday.pid
kill $(cat logs/friday.pid)   # to stop
```

Tail live logs:
```bash
tail -f friday/logs/friday.log
```

Debug the Messages database directly (Enforcing safe parameterized emulation):
```bash
sqlite3 ~/Library/Messages/chat.db \
  "SELECT m.rowid, m.text, m.is_from_me, m.date \
   FROM message m \
   JOIN chat_message_join cmj ON cmj.message_id = m.rowid \
   JOIN chat c ON c.rowid = cmj.chat_id \
   WHERE c.chat_identifier = 'YOUR_HANDLE' \
   ORDER BY m.date DESC LIMIT 5;"
```

---

## Configuration

`friday/friday_config.yaml` controls all behavior — no code changes needed for:
- Enabling/disabling channels and skills
- Adding approved contacts or GroupMe groups
- Setting the AI model (`gemini` section, key `model`, default `models/gemini-2.0-flash-lite`)
- Scheduling poll interval and briefing time

`ANTHROPIC_API_KEY` in the config/spec is outdated — the running system uses `GEMINI_API_KEY`.

---

## Architecture

```text
friday.py (main)
  ├── schedule loop (threading, not asyncio)
  │     └── run_poll_cycle()
  │           ├── agent.process_user_replies()     ← reads iMessage thread
  │           └── agent.process_groupme_messages() ← polls GroupMe API
  │
  ├── FridayAgent (agent/core.py)
  │     ├── _think()            ← Optimized Gemini API call (Prefixed system prompts for Cache Hits)
  │     ├── process_user_replies()
  │     ├── process_groupme_messages()
  │     ├── send_evening_briefing()
  │     └── _parse_action_response()  ← structured text → action_type + action_data
  │
  ├── PermissionGate (agent/permissions.py)
  │     ├── request()           ← sends draft via iMessage, polls for Yes/No/Edit
  │     ├── _parse_reply()      ← 5-min timeout, 8-sec polling interval
  │     └── _execute()          ← dispatches to calendar or iMessage on approval
  │
  ├── Memory (agent/memory.py)  ← SQLite: long_term, short_term, processed_messages
  │
  ├── iMessageChannel (channels/imessage.py)
  │     ├── send() / send_to_self()  ← AppleScript via osascript
  │     └── read_replies()           ← READ-ONLY strict parameterized access to chat.db
  │
  ├── GroupMeChannel (channels/groupme.py)
  │     └── poll()              ← REST API, tracks last_id per group in memory
  │
  └── AppleCalendarChannel (channels/apple_calendar.py)
        └── AppleScript for all read/create/edit/delete operations
```

### Data flow for an incoming GroupMe message (Optimized):
1. `groupme.poll()` fetches messages since last stored `after_id`.
2. `filter_groupme()` checks group allowlist + scheduling signal regex.
3. **Pre-Processing Block (0-Token):** Message text drops into local string sanitizers. Dynamic single quotes are checked/parameterized, and conversational timestamps ("tonight", "May 5") are translated to structured context via script processing.
4. If passed: `agent._think()` fires. System prompts and invariant context models are placed strictly at the beginning of the call structure to maximize Gemini API implicit context caching hits.
5. `_parse_action_response()` converts response text

### Sliding Window Context Gathering (Minimal-Token Logic)
* **The Problem:** Messages that trigger an LLM review often lack explicit structural details (e.g., *"Sounds good, let's do that time instead"* or *"Can you move it to Room 102?"*). Passing isolated messages forces LLM failures or massive context logging.
* **The Local Implementation:** When a message clears the initial scheduling filter, the local Python layer must instantly query the source database/API to extract a sliding window of the immediate conversational history (**the last 5 to 10 messages**) from that specific `chat_id` or `group_id`.
* **The Optimization Goal:** This short-term conversation thread is bundled entirely offline and injected as a compact metadata block into the execution payload. This gives the Gemini engine immediate situational awareness to resolve phrases like "that time" or "it" accurately, using only a tiny fraction of the tokens a full chat log would require.

### Decoupled Desktop Mission Control (The Hybrid Dashboard)
* **The Architecture Rule:** The Graphical UI and the Background Core Engine must be completely decoupled. The Background Daemon must NEVER depend on the UI thread to execute polling, database reading, or iMessage communications.
* **The State Boundary:** The UI dashboard acts purely as a visual lens and configuration editor. It reads runtime metrics from `state.json` for live system monitoring and writes API keys, system prompts, and contact allowlists directly back to `friday_config.yaml`.
* **The UI Framework:** Use a single-threaded Tkinter or lightweight PyQt6 interface. It should display:
  1. Live System Ticker (Last file-watch timestamp, processed queues, API token cost tracking).
  2. Configuration Manager (GUI text fields for allowlisted group IDs and persona files).
  3. LLM API Key Credential Block (Secure input masking to write hidden environment keys directly to local files).

  ### Dual-Engine Execution Layer (Ollama Local Sandbox)
* **The Token Protection Protocol:** To prevent unnecessary API billings during ongoing integration testing, the system must support an interchangeable LLM engine configuration backend.
* **The Local Sandbox Env:** When the provider configuration flag is toggled to `ollama`, the backend diverts reasoning requests away from Gemini API endpoints to a localized loop targeting `http://localhost:11434/api/generate`.
* **The Structural Rule:** Claude Code must ensure that all prompts request JSON-mode formatting from Ollama (`"format": "json"`) to verify that the target automation parsers function perfectly on structured schema types before production compilation.