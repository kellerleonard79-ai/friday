# Voice satellite — permissions, launch chain, and failure modes

Everything below was established by measurement on 2026-08-02 while fixing
push-to-talk. Numbers are from this machine (macOS 26 / Darwin 25.5, python.org
framework Python 3.12); the mechanisms are general.

The recurring theme: **the voice listener fails silently.** It boots clean, logs
"Voice — online", and does nothing. Every section here exists because some
failure produced a healthy-looking log.

---

## 1. macOS permissions: three services, two different rules

PTT needs three TCC grants, and they do **not** resolve the same way.

| Service | TCC service name | Resolves against |
|---|---|---|
| Microphone | `kTCCServiceMicrophone` | the **responsible bundle** |
| Accessibility | `kTCCServiceAccessibility` | the **running process** |
| Input Monitoring | `kTCCServiceListenEvent` | the **running process** |

This split is the single most important fact in this document. `FridayVoice.app`
exists to give the interpreter a responsible bundle so the *microphone* prompt
names "FridayVoice" and the grant persists against `com.friday.voice`. That
mechanism does nothing for the other two.

pynput needs **both** Accessibility and Input Monitoring. Granting one and not
the other yields a listener that starts, logs nothing unusual, and receives zero
key events forever. pynput warns about Accessibility only, and only at listener
start:

```
WARNING pynput.keyboard.Listener: This process is not trusted! ...
```

There is no equivalent warning for Input Monitoring. Silence is not success.

### Why the launcher forks instead of exec'ing

`FridayVoice_launcher.c` originally called `execv`. That replaces the process
image, discarding the `.app` identity the bundle exists to provide — so
Accessibility and Input Monitoring were evaluated against a bare framework
python that TCC cannot prompt for and that no Settings entry matches.

Running the same script from Terminal always worked, because Terminal **forks**
a child and the child inherits Terminal's grants. The launcher now does the
same: it forks, the child `execv`s the interpreter, and the parent waits. With
`FridayVoice` alive as the parent, both permissions resolve to
`com.friday.voice`.

The parent also forwards `SIGTERM`/`SIGINT` to the child. Without that,
`launchctl bootout` kills the parent and leaves an orphaned listener that races
the next start over the microphone and the Telethon session. An orphan is easy
to mistake for success — it answers key presses while you believe you are
testing the LaunchAgent.

Confirm the shape after any change:

```sh
pgrep -fl "FridayVoice|listen.py"
# want TWO lines: .../MacOS/FridayVoice (parent) and .../Python listen.py (child)
```

### The framework-python detail

`/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12` is a real
152 KB Mach-O stub that re-execs
`.../Resources/Python.app/Contents/MacOS/Python`. So the binary TCC sees is
never the interpreter path in `voice_launcher.conf`. Granting the stub path
accomplishes nothing.

---

## 2. Reading and reasoning about the TCC database

Accessibility and Input Monitoring live in the **system** database; microphone
lives in the **user** one:

```sh
sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
  "SELECT service, client, client_type, auth_value FROM access
   WHERE service IN ('kTCCServiceAccessibility','kTCCServiceListenEvent');"

sqlite3 ~/"Library/Application Support/com.apple.TCC/TCC.db" \
  "SELECT client, auth_value FROM access WHERE service='kTCCServiceMicrophone';"
```

Four non-obvious facts, each of which cost a debugging round:

- **`client_type` matters.** `0` = bundle identifier, `1` = executable path. A
  grant added by hand through System Settings is stored as a bundle id, and a
  bundle-id row can never match a binary that launchd `exec`s directly. This is
  why adding python in Settings never worked.
- **Removing an entry does not delete the row.** The `−` button sets
  `auth_value = 0`, an explicit *deny*. "Remove and re-add" therefore leaves a
  deny behind that can block the re-add.
- **`auth_value = 2` does not mean it works.** Each row carries a `csreq` code
  requirement. Re-signing a bundle changes its cdhash, and every existing row
  keeps displaying as enabled in Settings while silently failing the
  requirement. Symptom: Settings shows the toggle on, the process reports
  denied.
- **The repair for a stale row is a reset, not a toggle:**
  ```sh
  sudo tccutil reset All com.friday.voice   # then restart voice; the prompt re-issues
  ```

`listen.py` raises both prompts itself at boot (`_accessibility_trusted`,
`_input_monitoring_trusted`) and names whichever is missing. Prompts are
throttled to one per hour via marker files in `/tmp`, because granting a
permission makes TCC kill the process and KeepAlive would otherwise loop the
dialog.

`CGRequestListenEventAccess()` is documented as the way to request Input
Monitoring, but for a launchd process with no app bundle it is a no-op — no
dialog, no row created. It only became unnecessary once the fork change made
`com.friday.voice` the responsible process.

### PyObjC trap

`AXIsProcessTrustedWithOptions({})` **segfaults** — `EXC_BAD_ACCESS` in
`CFGetTypeID`. It kills the process mid-boot with nothing in the log, and
launchd respawns straight back into it, which reads as an unexplained restart
storm. Use `AXIsProcessTrusted()` when not prompting, and pass a populated dict
only when you actually want the dialog. Same family of trap as the EventKit
completion handler documented in `CLAUDE.md`.

Crash reports land in `~/Library/Logs/DiagnosticReports/Python-*.ips` and are
JSON after the first line — worth checking whenever a boot dies without logging.

---

## 3. Diagnosing "I press the key and nothing happens"

`ptt.py` logs a one-shot line on the first key event of **any** kind:

```
INFO ptt: key monitoring live — first event observed: Key.alt_r (watching for Key.alt_r)
```

That line is the fork in the road:

- **Line absent** → the event tap is dead. A permission problem, not a key
  problem. Check Accessibility *and* Input Monitoring per §1–2.
- **Line present, names a different key** → the tap is alive and
  `voice.push_to_talk_key` doesn't match what your keyboard sends.
- **Line present with the right key, no session** → the fault is downstream, in
  `_on_ptt_press` or the session state machine.

To learn what identity macOS actually evaluates, run a probe through the real
launch chain — attribution differs between launchd, Terminal, and an agent's
shell, so testing from the wrong parent gives a misleading answer:

```python
import ctypes, ctypes.util, os
lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
buf = ctypes.create_string_buffer(4096); size = ctypes.c_uint32(4096)
ctypes.CDLL(None)._NSGetExecutablePath(buf, ctypes.byref(size))
print(bool(lib.AXIsProcessTrusted()), buf.value.decode(), os.getppid())
```

Point `~/.friday/voice_launcher.conf` at it temporarily and `kickstart` the
agent. `ppid=1` confirms the launcher `exec`'d rather than forked.

---

## 4. Push-to-talk capture timing

In PTT-only mode (`wake_enabled` and `clap_enabled` both false) the stream is
opened on key-down so the orange mic indicator stays dark at idle. Measured cost
of that open:

```
stream.start()            467 ms
first frame delivered     574 ms after key-down   (and it is the device-open
                                                   transient: rms 973, settling
                                                   to a ~15 noise floor)
```

So the mic is deaf for roughly the first 0.6 s of every press. `_run_session`
therefore opens the device *before* loading config and reading `system_state`,
so that work happens underneath the warm-up.

The close is symmetric and matters just as much to the user, because the orange
indicator tracks the stream, not the key. Closing in the `finally` block held
the device through transcription, the bridge round trip, and TTS playback — the
dot stayed lit for seconds after release, which reads as "it is still
listening". `_run_session` now calls `stream.stop()` immediately after
`record_while_held` returns; the `finally` still stops it, idempotently, for the
early returns and crashes that never reach that line.

`record_while_held` must not stop the instant the key lifts. It previously did,
and every failed session in the log read exactly:

```
record_while_held: 0.48s captured        # == preroll_ms(500) // 80 ms frame
audio diag: 0.48s, 7680 samples, rms=0.0, peak=0
transcript: ''                            # → "I didn't catch that, sir."
```

Two guards fix it, both ceilinged by `max_ms`:

- `PTT_TAIL_MS` (400 ms) — keep capturing past release; speech routinely runs on.
- `MIN_PTT_CAPTURE_MS` (1500 ms) — never hand Whisper less than this.

Measured before → after, same physical press:

| press | before | after |
|---|---|---|
| instant release | 0.48 s, rms 0 | 1.52 s, rms 219 |
| 300 ms hold | 0.48 s, rms 0 | 1.52 s |
| 2 s hold | 2.00 s | 2.48 s |

A healthy real session for reference:

```
record_while_held: 3.20s captured (key released)
audio diag: 3.20s, 51200 samples, rms=3290.9, peak=28125
transcript: 'Hello.'
```

**`rms=0.0, peak=0` over a full capture means TCC is denying the microphone.**
PyAudio does not raise on a mic denial — it delivers zero-filled buffers, so a
denied process opens its stream "successfully" and reads silence forever. The
boot probe validates real signal for exactly this reason and aborts rather than
running deaf. An occasional `peak=0` immediately after a restart is different
and benign: the outgoing process still held the device. KeepAlive recovers on
the next spawn.

---

## 5. The Telegram bridge dies permanently on a network blip

Telethon's default `connection_retries=5` is exhausted by an ordinary sleep or
Wi-Fi drop, after which it gives up **for good**:

```
WARNING telethon...: Attempt 6 at connecting failed: OSError: [Errno 51] Network is unreachable
ERROR   telethon...: Automatic reconnection failed 5 time(s)
```

`connect()` only ever ran at boot, and `send_and_wait` checked `self._client is
not None` rather than `is_connected()`. So one blip bricked voice for the life
of the process — every press transcribed fine and then died at the send with
`ConnectionError: Cannot send requests while disconnected`, reported to the user
as "I couldn't reach Friday, sir."

`_ensure_connected()` now runs before every send: it reconnects when needed,
re-resolves the bot entity only if missing, and is bounded by
`_RECONNECT_TIMEOUT_S` (10 s) so a dead network fails fast instead of leaving
the user in silence. The outer future timeout accounts for both legs.

Do **not** open a second Telethon client against the live `StringSession` to
test this — concurrent use of one session can get the auth key revoked. Use a
stub client.

---

## 6. Config and launch-path traps

- **Blank strings are not absent.** `voice.get(key, default)` returns a
  present-but-empty value, so clearing `push_to_talk_key` in the dashboard put
  `''` in the YAML, `_resolve_key('')` raised, `boot()` failed, and KeepAlive
  crash-looped the entire listener — wake and clap included — over one empty
  field. `_coerce_str` now falls back to the default for
  `push_to_talk_key`, `whisper_model` and `tts_voice`; `boot()` additionally
  catches an unresolvable key name and disables only PTT.
- **`voice_launcher.conf` must go in `~/.friday/`.** The C launcher reads only
  `<bundle>/Contents/Resources/` and `$HOME/.friday/`. `paths.data_dir()` is the
  package directory on macOS, which is neither — so `write_voice_launcher_conf`
  wrote a file nothing ever read, and any restart hit `FridayVoice: no
  interpreter found` and respawned forever.
- **Config changes never affect a running `listen.py`.** Always
  `launchctl kickstart -k gui/$(id -u)/com.friday.voice` after editing voice
  config. `AXIsProcessTrusted` in particular is read once per process, so a
  permission granted while voice is running has no effect until it restarts.

---

## 7. Quick reference

```sh
# restart voice
launchctl kickstart -k gui/$(id -u)/com.friday.voice

# is it actually up, and is the process tree right?
launchctl print gui/$(id -u)/com.friday.voice | grep -E "state = |pid = "
pgrep -fl "FridayVoice|listen.py"

# logs (logging goes to stderr; voice.log is nearly always empty)
tail -f /Users/keller/friday/friday/logs/voice.err

# a healthy boot
mic probe: ... (signal OK) → whisper ready → bot entity resolved
→ PTT listener started on key=Key.alt_r → Voice — online
# with NO "not granted", NO "not trusted", and a "key monitoring live"
# line on the first key press
```

After rebuilding `FridayVoice.app`, expect its TCC grants to go stale; see §2.
