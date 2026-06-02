"""voice/clap.py — double-clap trigger for the main Friday process.

Runs an always-on 16 kHz mono float32 sounddevice InputStream in a daemon
thread. Each 512-sample block has its RMS compared to `agent.clap_threshold`
(re-read from config on every block so the user can tune live without a
restart). A clap is registered when RMS crosses the threshold AND at least
80 ms has passed since the last clap (debounce). A double-clap fires when
two claps land 200–700 ms apart; the listener then records `listen_seconds`
of audio, transcribes via voice.stt.transcribe, and hands the text to the
`on_trigger` callback (typically `agent.on_message`). 3-second cooldown
after every trigger so an immediate echo doesn't re-fire.

Note: the always-on input stream means macOS's orange "mic in use" indicator
stays lit whenever this listener is running. That is correct and expected
behavior — there is no way to detect claps without continuously sampling.

This module is independent of voice/listen.py — its own stream, its own
thread, its own STT call. Both can run simultaneously; doing so means two
processes share the mic and both will fire on a clap, which is rarely what
the user wants. Keep `voice.clap_enabled` (listen.py satellite) and
`agent.clap_enabled` (this module) mutually exclusive in practice.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

_LOGGER = logging.getLogger("friday.voice.clap")

_SAMPLE_RATE = 16000
_BLOCK_SAMPLES = 512
_MIN_GAP_S = 0.080        # debounce — single block of crossings collapses to one clap
_DOUBLE_MIN_S = 0.150     # paired claps must be at least this far apart
_DOUBLE_MAX_S = 1.100     # ...and no farther than this (covers natural human double-clap tempo)
_POST_TRIGGER_COOLDOWN_S = 3.0
_DEFAULT_THRESHOLD = 0.15
_DEFAULT_LISTEN_SECONDS = 4
_CONFIG_RELOAD_INTERVAL_S = 1.0


def _read_threshold(config_path: Path, default: float) -> float:
    """Re-read clap_threshold from disk. Returns `default` on any failure so
    a malformed yaml never silences the trigger."""
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        raw = (cfg.get("agent") or {}).get("clap_threshold")
        if raw is None:
            return default
        return float(raw)
    except (OSError, ValueError, TypeError):
        return default
    except Exception as e:  # yaml parse errors, etc.
        _LOGGER.debug("threshold reload failed: %s", e)
        return default


class ClapListener:
    def __init__(self, config: dict, on_trigger: Callable[[str], None]):
        agent_cfg = (config.get("agent") or {})
        self._enabled = bool(agent_cfg.get("clap_enabled", False))
        self._initial_threshold = float(agent_cfg.get("clap_threshold", _DEFAULT_THRESHOLD))
        self._listen_seconds = int(agent_cfg.get("listen_seconds", _DEFAULT_LISTEN_SECONDS))
        self._on_trigger = on_trigger

        # Cached threshold + last-reload-time so we re-read at most ~1 Hz
        # rather than on every 32 ms block.
        self._threshold = self._initial_threshold
        self._last_reload = 0.0
        self._config_path = Path(__file__).resolve().parent.parent / "friday_config.yaml"

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_clap_t = 0.0
        self._cooldown_until = 0.0
        self._stream = None
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            _LOGGER.warning("ClapListener.start: already running")
            return
        try:
            import sounddevice as sd  # noqa: F401  (probe import)
        except Exception as e:
            _LOGGER.warning(
                "sounddevice not available — clap detection disabled. "
                "Install with `pip install sounddevice`. (import error: %s)", e,
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="clap-listener", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                _LOGGER.debug("stream close failed: %s", e)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── audio thread ────────────────────────────────────────────────────────

    def _run(self) -> None:
        import sounddevice as sd
        try:
            self._stream = sd.InputStream(
                samplerate=_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=_BLOCK_SAMPLES,
            )
            self._stream.start()
        except Exception as e:
            _LOGGER.error("clap stream open failed: %s", e)
            self._stream = None
            return

        _LOGGER.info(
            "clap listener active — threshold=%.3f, listen_seconds=%d "
            "(macOS mic indicator stays lit while running)",
            self._threshold, self._listen_seconds,
        )

        # TCC denial under launchd returns zero-filled buffers, not errors.
        # Drain ~1 s and check we're getting real signal — if not, surface a
        # loud warning rather than spinning in silence forever.
        try:
            probe_chunks: list[np.ndarray] = []
            samples = 0
            while samples < _SAMPLE_RATE and not self._stop.is_set():
                block, _ = self._stream.read(_BLOCK_SAMPLES)
                probe_chunks.append(block.copy())
                samples += _BLOCK_SAMPLES
            probe = np.concatenate(probe_chunks).astype(np.float32) if probe_chunks else np.zeros(1, dtype=np.float32)
            peak = float(np.abs(probe).max())
            rms = float(np.sqrt(np.mean(np.square(probe))))
            if peak == 0.0:
                _LOGGER.error(
                    "clap probe: %d samples drained, peak=0.0 (TCC denial). "
                    "macOS Privacy is blocking microphone access for the "
                    "python binary that runs friday.py "
                    "(/Library/Frameworks/Python.framework/Versions/3.14/bin/python3). "
                    "Run friday.py once from Terminal so the permission "
                    "dialog can appear, grant access, then restart via "
                    "`launchctl kickstart -k gui/$(id -u)/com.friday.agent`. "
                    "Clap thread exiting.",
                    samples,
                )
                return
            _LOGGER.info("clap probe: peak=%.4f rms=%.4f (signal OK)", peak, rms)
        except Exception as e:
            _LOGGER.warning("clap probe failed: %s", e)

        # Rolling peak-RMS logger so the user can tune clap_threshold by
        # watching what real claps actually measure on their mic.
        diag_peak = 0.0
        diag_next = time.monotonic() + 5.0
        # Onset detection: a "clap" is the rising edge from below→above
        # threshold. Sustained loud audio (clap tail, speech, music) does NOT
        # register additional claps until RMS drops back below threshold for
        # at least _MIN_GAP_S, which collapses the whole envelope into one
        # event and lets the 200-700 ms double-clap window measure
        # onset-to-onset rather than block-to-block.
        above = False
        below_since = time.monotonic()

        while not self._stop.is_set():
            try:
                block, _overflow = self._stream.read(_BLOCK_SAMPLES)
            except Exception as e:
                _LOGGER.warning("clap stream read error: %s — retrying in 0.5s", e)
                time.sleep(0.5)
                continue

            now = time.monotonic()

            # Pull a fresh threshold periodically so dashboard edits land
            # without a restart. The yaml read is cheap but not free, so
            # rate-limit it.
            if now - self._last_reload >= _CONFIG_RELOAD_INTERVAL_S:
                self._threshold = _read_threshold(self._config_path, self._initial_threshold)
                self._last_reload = now

            rms = float(np.sqrt(np.mean(np.square(block.astype(np.float32)))))
            if rms > diag_peak:
                diag_peak = rms
            if now >= diag_next:
                _LOGGER.info(
                    "clap diag: 5s peak rms=%.4f (threshold=%.3f)",
                    diag_peak, self._threshold,
                )
                diag_peak = 0.0
                diag_next = now + 5.0

            if now < self._cooldown_until:
                # Reset envelope so post-cooldown noise is treated as a fresh
                # onset, not a continuation of pre-cooldown audio.
                above = False
                below_since = now
                continue

            if rms < self._threshold:
                if above:
                    below_since = now
                above = False
                continue

            # rms >= threshold. Treat as clap onset only on the rising edge,
            # and only if we've been quiet long enough to call it a new clap.
            if above or (now - below_since) < _MIN_GAP_S:
                above = True
                continue
            above = True

            with self._lock:
                gap = now - self._last_clap_t
                if _DOUBLE_MIN_S <= gap <= _DOUBLE_MAX_S:
                    self._last_clap_t = 0.0
                    self._cooldown_until = now + _POST_TRIGGER_COOLDOWN_S
                    fire = True
                else:
                    self._last_clap_t = now
                    fire = False

            if fire:
                _LOGGER.info("double-clap detected (rms=%.3f, gap=%.3fs)", rms, gap)
                threading.Thread(
                    target=self._handle_trigger,
                    name="clap-session",
                    daemon=True,
                ).start()
            else:
                _LOGGER.info("single clap (rms=%.3f) — waiting for partner", rms)

    # ── trigger handler ─────────────────────────────────────────────────────

    def _handle_trigger(self) -> None:
        wav_path = f"/tmp/friday_clap_{uuid.uuid4().hex}.wav"
        try:
            self._record_to_wav(wav_path, self._listen_seconds)
        except Exception as e:
            _LOGGER.exception("clap recording failed: %s", e)
            return

        try:
            from voice.stt import transcribe
            text = transcribe(wav_path)
        except Exception as e:
            _LOGGER.exception("clap transcribe failed: %s", e)
            text = ""

        try:
            os.unlink(wav_path)
        except OSError:
            pass

        if not text:
            _LOGGER.info("clap session — empty transcript, skipping")
            return
        _LOGGER.info("clap transcript: %r", text[:200])
        try:
            self._on_trigger(text)
        except Exception as e:
            _LOGGER.exception("on_trigger raised: %s", e)

    def _record_to_wav(self, path: str, seconds: int) -> None:
        """Capture `seconds` of audio from a fresh InputStream and write a
        16-bit PCM WAV to `path`. Uses its own stream rather than the
        detection stream so we get a clean buffer with no carry-over."""
        import sounddevice as sd
        n_samples = int(seconds * _SAMPLE_RATE)
        audio = sd.rec(
            n_samples,
            samplerate=_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocking=True,
        )
        pcm = np.clip(audio.flatten(), -1.0, 1.0)
        pcm_i16 = (pcm * 32767.0).astype(np.int16)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(pcm_i16.tobytes())
