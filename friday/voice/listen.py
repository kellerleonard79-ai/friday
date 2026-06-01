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
from bridge import TelegramBridge  # noqa: E402
from ptt import PTTListener  # noqa: E402
from wakeword import WakeDetector  # noqa: E402

_LOGGER = logging.getLogger(__name__)

LISTENING_FLAG = Path("/tmp/friday_listening")


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


def _transcribe_wav_bytes(model, wav_bytes: bytes) -> str:
    """Whisper accepts a numpy array of float32 at 16 kHz. Decode the WAV
    in-memory and hand it over."""
    import wave
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16)
    if sr != SAMPLE_RATE:
        _LOGGER.warning("transcribe: unexpected sr=%d", sr)
    audio = pcm.astype(np.float32) / 32768.0
    result = model.transcribe(audio, fp16=False, language="en")
    return (result.get("text") or "").strip()


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

        self.stream.start()
        self.whisper = _load_whisper(self.cfg.whisper_model)

        self.clap = ClapDetector(
            self.stream,
            sensitivity=self.cfg.clap_sensitivity,
            window_ms=self.cfg.clap_window_ms,
            on_clap=lambda: self._trigger(source="clap"),
        )
        self.clap.start()

        self.bridge = TelegramBridge(
            api_id=self.cfg.telegram_api_id,
            api_hash=self.cfg.telegram_api_hash,
            bot_token=self.cfg.telegram_bot_token,
        )
        self.bridge.connect()

        self.ptt = PTTListener(
            key_name=self.cfg.push_to_talk_key,
            on_press_cb=self._on_ptt_press,
            on_release_cb=self._on_ptt_release,
        )
        self.ptt.start()

        self._wake_thread = threading.Thread(
            target=self._wake_loop, name="wake-loop", daemon=True
        )
        self._wake_thread.start()

        _LOGGER.info("F.R.I.D.A.Y. Voice — online. Say 'Hey Jarvis' to activate.")

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
        try:
            cfg = voice_config.load()
            self.stream.pause()
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
            reply = self.bridge.send_and_wait(transcript, timeout=cfg.response_timeout_s)
            _LOGGER.info("reply: %r", (reply or "")[:200])

            # Step 10: TTS decision
            if reply:
                if cfg.always_speak or tts.external_audio_present():
                    tts.speak(reply, voice=cfg.tts_voice).join()
                else:
                    _LOGGER.info("reply delivered to Telegram only (no external audio, always_speak=False)")

        except Exception as e:
            _LOGGER.exception("session crashed: %s", e)
        finally:
            # Step 11: flag off
            try:
                if LISTENING_FLAG.exists():
                    LISTENING_FLAG.unlink()
            except OSError:
                pass
            # Step 12: resume wake
            self._ptt_active.clear()
            self._ptt_release_event.clear()
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
