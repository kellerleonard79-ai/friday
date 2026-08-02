"""Friday voice listener — standalone entry point.

Boots audio + wake + clap + PTT + Whisper + Telegram bridge, runs the session
state machine specified in the project plan.

This module never imports from Friday's core. The only allowed external touch
is a read-only SQLite query against `system_state` to learn whether Friday is
online.
"""
from __future__ import annotations

import io
import logging
import os
import random
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

# Make sibling modules importable when running via LaunchAgent.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import config as voice_config  # noqa: E402
import tts  # noqa: E402
from audio import (  # noqa: E402
    SAMPLE_RATE,
    FRAME_SAMPLES,
    AudioStream,
    ClapDetector,
    record_until_silence,
    record_while_held,
)
from bridge import BridgeResult, Outcome, TelegramBridge  # noqa: E402
from ptt import PTTListener  # noqa: E402
from wakeword import WakeDetector  # noqa: E402

_LOGGER = logging.getLogger(__name__)

# Voice satellite feature map (standalone — never imports Friday's core):
#   • Three trigger paths, each independently toggleable in config:
#       wake word ("Hey Friday" via openWakeWord) | double-clap | push-to-talk
#   • Boot-time mic TCC probe that validates real signal (zeros == denied)
#   • Per-session state machine: offline check → ack phrase → record
#     (silence-terminated or held) → Whisper transcribe → Telegram bridge →
#     speak reply (only if external audio / always_speak)
#   • /tmp/friday_listening flag toggled per session (menubar/dashboard read it)
LISTENING_FLAG = Path("/tmp/friday_listening")

# Throttles for the two TCC prompts — see the call sites in boot(). Separate
# markers so approving one doesn't suppress the other's prompt.
_AX_PROMPT_MARKER = Path("/tmp/friday_ax_prompt")
_IM_PROMPT_MARKER = Path("/tmp/friday_input_monitoring_prompt")
_AX_PROMPT_INTERVAL_S = 3600


def _prompt_due(marker: Path, interval_s: int) -> bool:
    """True if `marker` is older than `interval_s` (or missing), touching it.
    Survives the respawn because it lives outside the process."""
    try:
        if marker.exists() and (time.time() - marker.stat().st_mtime) < interval_s:
            return False
        marker.touch()
    except OSError as e:
        _LOGGER.debug("prompt marker %s: %s", marker, e)
    return True


# ---------------------------------------------------------------------------
# Whisper wrapper
# ---------------------------------------------------------------------------

def _load_whisper(model_name: str):
    import whisper  # heavy import, do here
    _LOGGER.info("loading whisper model %r...", model_name)
    t0 = time.time()
    model = whisper.load_model(model_name)
    _LOGGER.info("whisper ready in %.1fs", time.time() - t0)
    return model


def _probe_microphone() -> bool:
    """Open a brief PyAudio input stream at boot to trigger the macOS TCC
    microphone permission dialog.

    A launchd-spawned binary will never appear in System Settings → Privacy &
    Security → Microphone until it actually attempts to capture audio — and
    when it does, TCC silently denies if the binary has no UI context to
    attach the prompt to. So the recommended flow is: run this interactively
    from a Terminal once so the prompt surfaces, grant access, then relaunch
    via LaunchAgent (which inherits the grant).

    Returns True if a stream opened and read cleanly, False otherwise. Always
    safe to call; never raises."""
    import pyaudio  # local: keep boot order explicit

    pa = None
    stream = None
    try:
        pa = pyaudio.PyAudio()
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=FRAME_SAMPLES,
        )
        # Drain ~1 second so the TCC prompt has time to appear and the user
        # has a chance to click Allow before we move on. Capture the actual
        # samples so we can validate signal — PyAudio doesn't raise when TCC
        # denies the mic, it just delivers zero-filled buffers.
        captured = 0
        target = SAMPLE_RATE  # ≈ 1.0s at 16 kHz
        chunks: list[bytes] = []
        while captured < target:
            chunks.append(stream.read(FRAME_SAMPLES, exception_on_overflow=False))
            captured += FRAME_SAMPLES
        pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
        peak = int(np.abs(pcm).max()) if pcm.size else 0
        if peak == 0:
            _LOGGER.error(
                "mic probe: opened OK but captured pure silence (peak=0). "
                "macOS TCC is denying microphone access to this binary "
                "(%s). Open System Settings → Privacy & Security → "
                "Microphone and enable it for the venv's python, OR run "
                "`%s %s` once in Terminal so the permission dialog can "
                "surface. Aborting so launchd throttles instead of "
                "respawning into endless silent sessions.",
                sys.executable, sys.executable, __file__,
            )
            return False
        _LOGGER.info("mic probe: %d samples drained, peak=%d (signal OK)", captured, peak)
        return True
    except (PermissionError, OSError) as e:
        _LOGGER.error(
            "Microphone access denied — grant access in System Settings → "
            "Privacy & Security → Microphone, then restart the voice agent. "
            "(probe error: %s: %s)",
            type(e).__name__, e,
        )
        return False
    except Exception as e:
        _LOGGER.exception("mic probe: unexpected error %s: %s", type(e).__name__, e)
        return False
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if pa is not None:
                pa.terminate()
        except Exception:
            pass


def _accessibility_trusted(prompt: bool = False) -> bool:
    """AXIsProcessTrusted for this process, optionally raising the system dialog
    that registers it with TCC.

    Accessibility is not the microphone. Mic TCC follows the *responsible
    bundle*, which is the whole reason FridayVoice.app wraps the interpreter —
    but Accessibility follows the *running binary*, and the launcher execv's
    python, so by the time pynput asks there is no .app left to credit (the
    listener runs with ppid=1). Worse, a python.org framework build re-execs
    into Python.framework/.../Resources/Python.app/Contents/MacOS/Python, so
    the binary that gets checked is not even the interpreter path in the conf.

    A grant added by hand through System Settings is stored by bundle
    identifier (client_type=0) and never matches a binary launchd exec'd
    directly, which TCC identifies by path. Letting macOS raise the prompt is
    what registers the identity it will actually check. Without this, voice
    boots perfectly clean — mic OK, bridge connected, "PTT listener started" —
    and the key silently does nothing.
    """
    try:
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except ImportError as e:
        # Missing pyobjc shouldn't block boot; PTT will warn on its own.
        _LOGGER.warning("accessibility check unavailable: %s", e)
        return True
    # Must be the no-options call when we aren't prompting: handing
    # AXIsProcessTrustedWithOptions an empty dict segfaults inside CFGetTypeID
    # (EXC_BAD_ACCESS at 0x8), which kills the process mid-boot with nothing in
    # the log — launchd then respawns straight back into it.
    if not prompt:
        return bool(AXIsProcessTrusted())
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))


def _input_monitoring_trusted(prompt: bool = False) -> bool:
    """Whether this process may listen to key events, optionally prompting.

    Input Monitoring is a *second* gate, separate from Accessibility, and
    pynput warns about neither once its listener is up: with Accessibility
    granted and this one denied, `keyboard.Listener` starts, logs nothing, and
    delivers zero events forever. Same identity rule as Accessibility — the
    grant has to name the binary that actually runs, so it must come from this
    prompt rather than from System Settings by hand.
    """
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGPreflightListenEventAccess,
            CGRequestListenEventAccess,
        )
    except ImportError as e:
        _LOGGER.warning("input-monitoring check unavailable: %s", e)
        return True
    if CGPreflightListenEventAccess():
        return True
    if prompt:
        CGRequestListenEventAccess()
    return False


def _transcribe_wav_bytes(model, wav_bytes: bytes) -> str:
    """Whisper accepts a numpy array of float32 at 16 kHz. Decode the WAV
    in-memory and hand it over."""
    import wave
    # Persist the last capture for offline debugging — listen with
    # `afplay /tmp/friday_last_ptt.wav` to confirm what the mic actually heard.
    try:
        with open("/tmp/friday_last_ptt.wav", "wb") as dbg:
            dbg.write(wav_bytes)
    except OSError as e:
        _LOGGER.debug("could not snapshot wav: %s", e)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16)
    if sr != SAMPLE_RATE:
        _LOGGER.warning("transcribe: unexpected sr=%d", sr)
    # Diagnostic: empty-string transcripts almost always trace back to silence
    # in the captured audio. Log RMS + peak so we can tell at a glance whether
    # the mic captured real signal or just noise floor.
    if pcm.size > 0:
        f = pcm.astype(np.float32)
        rms = float(np.sqrt(np.mean(f * f)))
        peak = int(np.abs(pcm).max())
        _LOGGER.info(
            "audio diag: %.2fs, %d samples, rms=%.1f, peak=%d",
            pcm.size / SAMPLE_RATE, pcm.size, rms, peak,
        )
    else:
        _LOGGER.warning("audio diag: empty pcm buffer")
    audio = pcm.astype(np.float32) / 32768.0
    result = model.transcribe(audio, fp16=False, language="en")
    return (result.get("text") or "").strip()


# ---------------------------------------------------------------------------
# Spoken failure cues
# ---------------------------------------------------------------------------

# What to say when there is no reply to read out. Keyed by why. The split that
# matters is whether the message actually got to Friday: on a timeout it did,
# the answer is already on its way to Telegram, and the old blanket "I couldn't
# reach Friday" was simply false — it sent a briefing while voice denied it.
_FAILURE_CUES = {
    Outcome.TIMEOUT: (
        "Friday is taking a while, sir. The reply will be in Telegram."
    ),
    Outcome.DISCONNECTED: (
        "I lost my connection mid-request, sir. Check Telegram."
    ),
    Outcome.SEND_FAILED: "I couldn't reach Friday, sir.",
    Outcome.NOT_CONNECTED: "I couldn't reach Friday, sir.",
    Outcome.EMPTY_TEXT: "I didn't catch that, sir.",
    Outcome.INTERNAL_ERROR: "Something went wrong on my end, sir.",
}

_FALLBACK_CUE = "I couldn't reach Friday, sir."


def _failure_cue(result: BridgeResult) -> str:
    """The sentence to speak when `result` carries no reply. Unknown outcomes
    fall back to the conservative wording rather than saying nothing."""
    cue = _FAILURE_CUES.get(result.outcome)
    if cue:
        return cue
    # OK-but-empty lands here: Friday answered with a blank message.
    if result.reached_friday:
        return "Friday answered, but said nothing, sir."
    return _FALLBACK_CUE


# ---------------------------------------------------------------------------
# SQLite system_state read
# ---------------------------------------------------------------------------

def friday_is_running() -> bool:
    """Read system_state.status from Friday's SQLite DB. Treat any error or
    missing row as 'offline' so we surface a clear message to the user rather
    than silently soldiering on."""
    db = voice_config.DB_PATH
    if not db.is_file():
        _LOGGER.warning("system_state: db missing at %s", db)
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            cur = conn.execute("SELECT value FROM system_state WHERE key = ?", ("status",))
            row = cur.fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _LOGGER.warning("system_state read failed: %s", e)
        return False
    if row is None:
        return False
    return str(row[0]).strip().lower() == "running"


# ---------------------------------------------------------------------------
# Listener orchestrator
# ---------------------------------------------------------------------------

class VoiceListener:
    def __init__(self) -> None:
        self.cfg = voice_config.load()
        self.session_lock = threading.Lock()
        self.shutdown = threading.Event()

        self.stream = AudioStream()
        # Wake/clap are gated by config flags so the detectors can be fully
        # disabled (no model load, no subscriber, no trigger surface) when the
        # user only wants push-to-talk active.
        self.wake: Optional[WakeDetector] = None
        if self.cfg.wake_enabled:
            self.wake = WakeDetector(
                phrases=self.cfg.wake_phrases,
                solo_trigger_enabled=self.cfg.solo_trigger_enabled,
            )
        self.clap: Optional[ClapDetector] = None
        self.ptt: Optional[PTTListener] = None
        self.bridge: Optional[TelegramBridge] = None
        self.whisper = None

        self._wake_thread: Optional[threading.Thread] = None
        self._ptt_release_event = threading.Event()
        # True iff a PTT session is currently active — used so the PTT release
        # callback can hand off control to record_while_held.
        self._ptt_active = threading.Event()

    # ----- boot / shutdown -----

    def boot(self) -> None:
        if not self.cfg.enabled:
            _LOGGER.info("voice.enabled = false → exiting")
            sys.exit(0)
        if not voice_config.telegram_credentials_present(self.cfg):
            print(
                "\n!!! Voice cannot start: Telegram credentials missing.\n"
                "    Register an app at https://my.telegram.org → API development tools\n"
                "    Then add telegram.api_id (int) and telegram.api_hash (str) to\n"
                "    friday_config.yaml. See voice/bridge.py for details.\n",
                file=sys.stderr,
            )
            sys.exit(2)

        # Force a microphone permission request before any other capture
        # happens. This is what makes the LaunchAgent's python binary appear
        # in System Settings → Privacy & Security → Microphone. Run this
        # script interactively from a Terminal at least once so the dialog
        # can surface on screen and be approved. The probe validates real
        # signal (not just bytes), so a TCC denial fails here rather than
        # silently feeding zeros to every PTT session.
        if not _probe_microphone():
            sys.exit(3)

        # The stream is only started here when a detector needs it always-on.
        # PTT-only mode opens/closes the stream per session so macOS's orange
        # "mic in use" menu-bar indicator is only lit during actual capture.
        if self.cfg.wake_enabled or self.cfg.clap_enabled:
            self.stream.start()
        self.whisper = _load_whisper(self.cfg.whisper_model)

        if self.cfg.clap_enabled:
            self.clap = ClapDetector(
                self.stream,
                sensitivity=self.cfg.clap_sensitivity,
                window_ms=self.cfg.clap_window_ms,
                on_clap=lambda: self._trigger(source="clap"),
            )
            self.clap.start()
        else:
            _LOGGER.info("clap detector disabled (voice.clap_enabled=false)")

        self.bridge = TelegramBridge(
            api_id=self.cfg.telegram_api_id,
            api_hash=self.cfg.telegram_api_hash,
            bot_token=self.cfg.telegram_bot_token,
            session_string=self.cfg.telegram_telethon_session or None,
        )
        self.bridge.connect()
        # If this was a first-time auth (or Telethon rotated the session
        # blob), persist it back to friday_config.yaml so the next launchd
        # restart skips the phone/code prompt entirely.
        new_session = self.bridge.get_session_string()
        if new_session and new_session != self.cfg.telegram_telethon_session:
            voice_config.persist_telethon_session(new_session)

        # Global key monitoring needs Accessibility. Ask before pynput does, so
        # the failure names itself instead of arriving as one buried warning.
        if not _accessibility_trusted():
            _LOGGER.error(
                "Accessibility not granted to %s — push-to-talk will receive no "
                "key events. Approve the system prompt, then restart voice "
                "(`launchctl kickstart -k gui/$(id -u)/com.friday.voice`). "
                "Adding python by hand in System Settings does not work: the "
                "grant must be created by this prompt.",
                sys.executable,
            )
            # Rate-limited because granting Accessibility makes TCC kill the
            # process. KeepAlive respawns it, we prompt again, TCC kills it
            # again — 35 respawns in four minutes, dialog each time. Once an
            # hour is enough to be actionable without the storm.
            if _prompt_due(_AX_PROMPT_MARKER, _AX_PROMPT_INTERVAL_S):
                _accessibility_trusted(prompt=True)
            else:
                _LOGGER.info("accessibility prompt suppressed (shown recently)")

        if not _input_monitoring_trusted():
            _LOGGER.error(
                "Input Monitoring not granted to %s — push-to-talk will receive "
                "no key events. This is a different permission from "
                "Accessibility and both are required; granting one alone leaves "
                "the listener running and deaf. Approve the prompt, then restart "
                "voice (`launchctl kickstart -k gui/$(id -u)/com.friday.voice`).",
                sys.executable,
            )
            if _prompt_due(_IM_PROMPT_MARKER, _AX_PROMPT_INTERVAL_S):
                _input_monitoring_trusted(prompt=True)
            else:
                _LOGGER.info("input-monitoring prompt suppressed (shown recently)")

        # A bad key name must not take the whole listener down. boot() failing
        # returns 1 from main(), and the LaunchAgent's KeepAlive then respawns
        # straight into the same failure — a crash loop that kills wake, clap
        # and PTT alike over one unusable config string.
        try:
            self.ptt = PTTListener(
                key_name=self.cfg.push_to_talk_key,
                on_press_cb=self._on_ptt_press,
                on_release_cb=self._on_ptt_release,
            )
            self.ptt.start()
        except ValueError as e:
            self.ptt = None
            _LOGGER.error(
                "push-to-talk disabled: %s. Set voice.push_to_talk_key to a "
                "recognized name (e.g. right_option) and restart.", e,
            )

        if self.cfg.wake_enabled and self.wake is not None:
            self._wake_thread = threading.Thread(
                target=self._wake_loop, name="wake-loop", daemon=True
            )
            self._wake_thread.start()
        else:
            _LOGGER.info("wake detector disabled (voice.wake_enabled=false)")

        # Banner reflects what's actually live so a glance at the log tells
        # the user which trigger paths exist.
        active = []
        if self.cfg.wake_enabled and self.wake is not None:
            active.append("wake")
        if self.cfg.clap_enabled and self.clap is not None:
            active.append("clap")
        if self.ptt is not None:
            active.append(f"PTT={self.cfg.push_to_talk_key}")
        _LOGGER.info(
            "F.R.I.D.A.Y. Voice — online. Triggers: %s",
            ", ".join(active) if active else "none (voice is deaf)",
        )

    def shutdown_all(self) -> None:
        self.shutdown.set()
        try:
            if LISTENING_FLAG.exists():
                LISTENING_FLAG.unlink()
        except OSError:
            pass
        for component in (self.ptt, self.clap, self.bridge, self.stream):
            try:
                if component is not None:
                    component.stop() if hasattr(component, "stop") else component.disconnect()
            except Exception as e:
                _LOGGER.warning("shutdown of %s: %s", type(component).__name__, e)
        # Give the wake loop a moment to exit
        if self._wake_thread is not None:
            self._wake_thread.join(timeout=2.0)

    # ----- wake loop -----

    def _wake_loop(self) -> None:
        consumer = self.stream.subscribe("wake")
        try:
            while not self.shutdown.is_set():
                # Re-read config each iteration (cheap thanks to mtime cache)
                # so menubar toggles take effect immediately.
                cfg = voice_config.load()
                if not cfg.mic_enabled:
                    time.sleep(0.25)
                    continue
                frame = consumer.next_frame(timeout=0.25)
                if frame is None:
                    continue
                try:
                    hit = self.wake.feed(frame)
                except Exception as e:
                    _LOGGER.exception("wake.feed raised: %s", e)
                    continue
                if hit is not None:
                    _LOGGER.info("WAKE: %s (%.2f)", hit.phrase, hit.score)
                    self._trigger(source="wake", phrase=hit.phrase)
        finally:
            consumer.unsubscribe()

    # ----- triggers -----

    def _trigger(self, source: str, phrase: Optional[str] = None) -> None:
        if not self.session_lock.acquire(blocking=False):
            _LOGGER.info("trigger dropped (session active): source=%s", source)
            return
        # Run the session in its own thread so the wake loop can return.
        t = threading.Thread(
            target=self._run_session,
            args=(source, phrase),
            name="session",
            daemon=True,
        )
        t.start()

    def _on_ptt_press(self) -> None:
        if not self.session_lock.acquire(blocking=False):
            _LOGGER.info("PTT press dropped (session active)")
            return
        self._ptt_active.set()
        self._ptt_release_event.clear()
        t = threading.Thread(
            target=self._run_session,
            args=("ptt", None),
            name="session-ptt",
            daemon=True,
        )
        t.start()

    def _on_ptt_release(self) -> None:
        # The session thread is waiting on this event inside record_while_held.
        self._ptt_release_event.set()

    # ----- the session state machine -----

    def _run_session(self, source: str, phrase: Optional[str]) -> None:
        # In PTT-only mode the stream is not running at idle (so macOS's
        # orange mic indicator stays dark). Open it now and tear it down in
        # the finally block. With wake/clap enabled the stream is always-on
        # at boot, so we only pause/resume around the session.
        ptt_owns_stream = (
            source == "ptt"
            and not self.cfg.wake_enabled
            and not self.cfg.clap_enabled
        )
        try:
            # Open the device before anything else. It is the long pole by an
            # order of magnitude (~470 ms, plus ~105 ms to the first frame), and
            # every millisecond of it is audio the user is already speaking into
            # a dead mic. Loading config and reading system_state underneath the
            # warm-up costs nothing and buys back their share of the head.
            if ptt_owns_stream:
                self.stream.start()
            cfg = voice_config.load()
            if not ptt_owns_stream:
                self.stream.pause()
            if self.wake is not None:
                self.wake.reset()

            # Step 3: offline check
            if not friday_is_running():
                _LOGGER.info("Friday offline — speaking offline message")
                tts.speak(
                    "F.R.I.D.A.Y. is currently offline, sir.",
                    voice=cfg.tts_voice,
                ).join()
                return

            # Step 4: acknowledgment phrase (skip for PTT)
            if source != "ptt" and cfg.acknowledgment_phrases:
                phrase_to_speak = random.choice(cfg.acknowledgment_phrases)
                _LOGGER.info("ack: %s", phrase_to_speak)
                tts.speak(phrase_to_speak, voice=cfg.tts_voice).join()

            # Step 5: flag
            try:
                LISTENING_FLAG.touch()
            except OSError as e:
                _LOGGER.warning("could not touch %s: %s", LISTENING_FLAG, e)

            # Resume the audio stream before recording. We paused at Step 2 so
            # the ack TTS wouldn't loop into the mic; with the stream still
            # paused, AudioStream._callback never notifies subscribers and
            # record_until_silence loops forever waiting on next_frame.
            # (In PTT-owns-stream mode the stream was just .start()-ed above
            # and isn't paused, so resume() is a no-op — safe to call.)
            self.stream.resume()

            # Step 6: record
            if source == "ptt":
                wav_bytes = record_while_held(
                    self.stream,
                    key_released_event=self._ptt_release_event,
                    max_ms=cfg.max_recording_ms,
                    preroll_ms=cfg.preroll_ms,
                )
            else:
                wav_bytes = record_until_silence(
                    self.stream,
                    silence_ms=cfg.silence_ms,
                    max_ms=cfg.max_recording_ms,
                    silence_rms_threshold=cfg.silence_rms_threshold,
                    preroll_ms=cfg.preroll_ms,
                )

            # Capture is over — hand the device back now, not in the finally.
            # Everything below (Whisper, the bridge round trip to Friday, TTS
            # playback) runs for seconds with no further use for the mic, and
            # macOS keeps the orange indicator lit for as long as the stream is
            # open. Closing here makes the dot go out on key release, which is
            # what the user reads as "it stopped listening". stop() is
            # idempotent, so the finally block still covers the paths that
            # never get here.
            if ptt_owns_stream:
                self.stream.stop()

            # Step 7: transcribe
            try:
                transcript = _transcribe_wav_bytes(self.whisper, wav_bytes)
            except Exception as e:
                _LOGGER.exception("whisper failed: %s", e)
                transcript = ""

            _LOGGER.info("transcript: %r", transcript[:200])

            # Step 8: empty handling
            if not transcript or len(transcript.strip()) < 2:
                tts.speak("I didn't catch that, sir.", voice=cfg.tts_voice).join()
                return

            # Step 9: bridge → bot → reply
            assert self.bridge is not None
            _LOGGER.info("bridge dispatch: %r", transcript[:200])

            # Tool-using requests ("brief me now") routinely run 30-60 s. The
            # cue keeps that wait from reading as a hang; `say` serializes on
            # its own lock, so it can't talk over the reply if one lands mid-cue.
            def _speak_slow_cue() -> None:
                tts.speak("Working on it, sir.", voice=cfg.tts_voice)

            result = self.bridge.send_and_wait(
                transcript,
                timeout=cfg.response_timeout_s,
                on_slow=_speak_slow_cue,
                slow_after_s=cfg.slow_reply_cue_s,
            )
            _LOGGER.info(
                "result: outcome=%s reply=%r",
                result.outcome.value, (result.reply or "")[:200],
            )

            # Step 10: TTS decision
            if result.ok and result.reply:
                if cfg.always_speak or tts.external_audio_present():
                    tts.speak(result.reply, voice=cfg.tts_voice).join()
                else:
                    _LOGGER.info("reply delivered to Telegram only (no external audio, always_speak=False)")
            else:
                # No reply to read out — say which kind of nothing it was. The
                # message may well have reached Friday and simply outrun our
                # listening window, in which case the answer is in Telegram and
                # claiming we couldn't reach him would be a lie.
                _LOGGER.warning(
                    "no reply to speak (outcome=%s): %s",
                    result.outcome.value, result.detail,
                )
                tts.speak(_failure_cue(result), voice=cfg.tts_voice).join()

        except Exception as e:
            _LOGGER.exception("session crashed: %s", e)
        finally:
            # Step 11: flag off
            try:
                if LISTENING_FLAG.exists():
                    LISTENING_FLAG.unlink()
            except OSError:
                pass
            # Step 12: resume wake / close stream
            self._ptt_active.clear()
            self._ptt_release_event.clear()
            if ptt_owns_stream:
                # Normally already closed right after capture; this covers the
                # early returns (Friday offline, empty transcript) and crashes.
                self.stream.stop()
            else:
                self.stream.resume()
            # Step 13: release lock
            try:
                self.session_lock.release()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# Entry point + signals
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    _setup_logging()
    listener = VoiceListener()

    def _handle_signal(signum, _frame):
        _LOGGER.info("signal %d received, shutting down", signum)
        listener.shutdown_all()
        # Allow LaunchAgent's KeepAlive to respawn cleanly.
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        listener.boot()
    except Exception as e:
        _LOGGER.exception("boot failed: %s", e)
        listener.shutdown_all()
        return 1

    # Block forever; everything runs in threads.
    try:
        while not listener.shutdown.is_set():
            time.sleep(1.0)
    finally:
        listener.shutdown_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
