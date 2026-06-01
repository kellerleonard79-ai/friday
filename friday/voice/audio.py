"""Audio I/O for the voice subsystem.

One PyAudio input stream feeds a shared thread-safe ring buffer; multiple
consumers (wake detector, clap detector, recorder) subscribe and pull frames
at their own pace.

Public surface:
- AudioStream            — owns the mic, runs the ring buffer
- AudioStream.subscribe  — returns a Consumer with next_frame()
- record_until_silence   — returns WAV bytes after a silence trigger
- record_while_held      — returns WAV bytes when a key_released event fires
- ClapDetector           — onset detector tuned to two-clap patterns
"""
from __future__ import annotations

import io
import logging
import struct
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

import numpy as np
import pyaudio

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280       # 80 ms at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # int16 mono
RING_FRAMES = 64           # ~5.1 s of context


# ---------------------------------------------------------------------------
# AudioStream + Consumer
# ---------------------------------------------------------------------------

@dataclass
class _SubscriberState:
    name: str
    cursor: int = 0
    event: threading.Event = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.event is None:
            self.event = threading.Event()


class Consumer:
    """One reader of the ring buffer. Each consumer has its own cursor so a
    slow reader doesn't starve a fast one — frames it misses get dropped."""

    def __init__(self, stream: "AudioStream", state: _SubscriberState) -> None:
        self._stream = stream
        self._state = state

    @property
    def name(self) -> str:
        return self._state.name

    def next_frame(self, timeout: Optional[float] = 1.0) -> Optional[np.ndarray]:
        """Block until a new frame arrives. Returns int16 ndarray of FRAME_SAMPLES
        elements. Returns None on timeout or if dispatch is paused (see pause())."""
        if not self._state.event.wait(timeout=timeout):
            return None
        self._state.event.clear()
        return self._stream._take_frame(self._state)

    def latest_frames(self, n_frames: int) -> np.ndarray:
        """Return up to n_frames most recent frames as a single int16 array.
        Doesn't advance the cursor — useful for pulling the pre-roll at the start
        of a recording. Returns an empty array if buffer not yet full."""
        return self._stream._snapshot(n_frames)

    def unsubscribe(self) -> None:
        self._stream._unsubscribe(self._state)


class AudioStream:
    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_samples: int = FRAME_SAMPLES,
        ring_frames: int = RING_FRAMES,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.ring_frames = ring_frames

        self._lock = threading.Lock()
        self._ring: Deque[np.ndarray] = deque(maxlen=ring_frames)
        # Monotonic frame counter — subscribers track this so we can detect
        # when they fall behind.
        self._write_idx = 0

        self._subs: List[_SubscriberState] = []
        self._subs_lock = threading.Lock()

        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None
        self._running = False
        self._paused = False
        self._reopen_after: float = 0.0  # backoff timer when stream errors

    # ----- public lifecycle -----

    def start(self) -> None:
        self._running = True
        self._open()

    def stop(self) -> None:
        self._running = False
        self._close()

    def pause(self) -> None:
        """Suspend dispatch to consumers. The underlying stream keeps reading
        so the ring stays warm (preserving pre-roll for the next session)."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # ----- subscriptions -----

    def subscribe(self, name: str) -> Consumer:
        state = _SubscriberState(name=name, cursor=self._write_idx)
        with self._subs_lock:
            self._subs.append(state)
        _LOGGER.info("audio subscriber registered: %s", name)
        return Consumer(self, state)

    def _unsubscribe(self, state: _SubscriberState) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(state)
            except ValueError:
                pass

    # ----- internals -----

    def _open(self) -> None:
        try:
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frame_samples,
                stream_callback=self._callback,
            )
            self._stream.start_stream()
            _LOGGER.info("audio stream opened at %d Hz", self.sample_rate)
        except Exception as e:
            _LOGGER.error("audio stream open failed: %s — will retry", e)
            self._reopen_after = time.time() + 2.0

    def _close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception as e:
            _LOGGER.warning("error closing stream: %s", e)
        finally:
            self._stream = None
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass
        finally:
            self._pa = None

    def _callback(self, in_data, frame_count, time_info, status):
        if status:
            _LOGGER.debug("pyaudio status flag: %s", status)
        # Slice incoming bytes into our fixed frame size in case the host
        # delivers a different chunk shape.
        if len(in_data) != FRAME_BYTES:
            # PyAudio sometimes hands smaller chunks at startup; concatenate
            # via the ring buffer below by zero-padding to one frame.
            if len(in_data) < FRAME_BYTES:
                in_data = in_data + b"\x00" * (FRAME_BYTES - len(in_data))
            else:
                in_data = in_data[:FRAME_BYTES]
        frame = np.frombuffer(in_data, dtype=np.int16).copy()

        with self._lock:
            self._ring.append(frame)
            self._write_idx += 1

        # Wake subscribers (even when paused — they decide whether to pull).
        if not self._paused:
            with self._subs_lock:
                for s in self._subs:
                    s.event.set()

        return (None, pyaudio.paContinue)

    def _take_frame(self, state: _SubscriberState) -> Optional[np.ndarray]:
        with self._lock:
            if self._write_idx == state.cursor:
                return None
            # If subscriber fell behind by more than the buffer, skip ahead and
            # log the drop.
            lag = self._write_idx - state.cursor
            if lag > self.ring_frames:
                dropped = lag - self.ring_frames
                _LOGGER.warning("%s dropped %d frames (lagged)", state.name, dropped)
                state.cursor = self._write_idx - self.ring_frames
            # Index into deque: oldest item is at write_idx - len(ring)
            offset = state.cursor - (self._write_idx - len(self._ring))
            if offset < 0 or offset >= len(self._ring):
                return None
            frame = self._ring[offset]
            state.cursor += 1
            # If there's more to read, leave the event set so caller can drain.
            if state.cursor < self._write_idx:
                state.event.set()
            return frame

    def _snapshot(self, n_frames: int) -> np.ndarray:
        """Return the last n_frames as a single int16 array (concatenated)."""
        with self._lock:
            if not self._ring:
                return np.zeros(0, dtype=np.int16)
            take = min(n_frames, len(self._ring))
            frames = list(self._ring)[-take:]
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)

    def healthcheck(self) -> None:
        """Call periodically — re-opens the stream after an error backoff."""
        if not self._running:
            return
        if self._stream is None and time.time() >= self._reopen_after:
            self._open()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    # Compute in float to avoid int16 overflow on .square()
    f = samples.astype(np.float32)
    return float(np.sqrt(np.mean(f * f)))


def _pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    if pcm.dtype != np.int16:
        pcm = pcm.astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def record_until_silence(
    stream: AudioStream,
    silence_ms: int = 1500,
    max_ms: int = 30000,
    silence_rms_threshold: int = 500,
    preroll_ms: int = 500,
    consumer_name: str = "record_silence",
) -> bytes:
    """Capture audio until `silence_ms` of below-threshold RMS, or `max_ms`
    total. Returns WAV bytes. Prepends ~`preroll_ms` from the ring buffer so the
    first syllable after a wake word isn't clipped."""
    frame_ms = (FRAME_SAMPLES * 1000) // SAMPLE_RATE  # 80
    consumer = stream.subscribe(consumer_name)
    try:
        preroll_frames = max(1, preroll_ms // frame_ms)
        preroll = consumer.latest_frames(preroll_frames)
        parts: List[np.ndarray] = [preroll] if preroll.size else []
        elapsed_ms = preroll.size * 1000 // SAMPLE_RATE
        silent_ms = 0
        had_voice = False

        while elapsed_ms < max_ms:
            frame = consumer.next_frame(timeout=1.0)
            if frame is None:
                # No frame this tick (maybe paused); keep trying.
                continue
            parts.append(frame)
            elapsed_ms += frame_ms

            level = _rms(frame)
            if level < silence_rms_threshold:
                silent_ms += frame_ms
                if had_voice and silent_ms >= silence_ms:
                    break
            else:
                silent_ms = 0
                had_voice = True
        pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        _LOGGER.info(
            "record_until_silence: %.2fs captured, voice=%s",
            pcm.size / SAMPLE_RATE, had_voice,
        )
        return _pcm_to_wav_bytes(pcm)
    finally:
        consumer.unsubscribe()


def record_while_held(
    stream: AudioStream,
    key_released_event: threading.Event,
    max_ms: int = 30000,
    preroll_ms: int = 500,
    consumer_name: str = "record_ptt",
) -> bytes:
    """Capture audio until `key_released_event` fires. Same WAV contract as
    record_until_silence."""
    frame_ms = (FRAME_SAMPLES * 1000) // SAMPLE_RATE
    consumer = stream.subscribe(consumer_name)
    try:
        preroll_frames = max(1, preroll_ms // frame_ms)
        preroll = consumer.latest_frames(preroll_frames)
        parts: List[np.ndarray] = [preroll] if preroll.size else []
        elapsed_ms = preroll.size * 1000 // SAMPLE_RATE

        while elapsed_ms < max_ms:
            if key_released_event.is_set():
                break
            frame = consumer.next_frame(timeout=0.1)
            if frame is None:
                continue
            parts.append(frame)
            elapsed_ms += frame_ms
        pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        _LOGGER.info("record_while_held: %.2fs captured", pcm.size / SAMPLE_RATE)
        return _pcm_to_wav_bytes(pcm)
    finally:
        consumer.unsubscribe()


# ---------------------------------------------------------------------------
# Clap detector
# ---------------------------------------------------------------------------

class ClapDetector:
    """Two-clap onset detector. Designed to ignore speech and music.

    A clap is a sharp transient: RMS spikes far above the local moving average
    and is followed by a brief quiet gap. Speech rarely produces this gap
    structure; music averages out across the moving window so its energy ratio
    stays low.
    """

    def __init__(
        self,
        stream: AudioStream,
        sensitivity: float = 0.7,
        window_ms: int = 600,
        on_clap: Optional[Callable[[], None]] = None,
        frame_ms: int = 80,
        moving_avg_frames: int = 50,
        absolute_floor: float = 1500.0,
        min_gap_ms: int = 80,
        max_gap_ms: int = 600,
    ) -> None:
        self._stream = stream
        self._consumer = stream.subscribe("clap")
        self._on_clap = on_clap
        self.frame_ms = frame_ms
        self.window_ms = window_ms
        self.min_gap_ms = min_gap_ms
        self.max_gap_ms = max_gap_ms
        self.absolute_floor = absolute_floor

        # Sensitivity: 0.0 → require very strong burst (6× moving avg),
        #              1.0 → looser (3× moving avg).
        self.ratio = 6.0 - (3.0 * max(0.0, min(1.0, sensitivity)))

        self._moving = deque(maxlen=moving_avg_frames)
        self._last_peak_ms: Optional[float] = None
        self._post_peak_quiet_ms: float = 0.0
        self._t_ms: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="clap-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._consumer.unsubscribe()

    def _run(self) -> None:
        while not self._stop.is_set():
            frame = self._consumer.next_frame(timeout=0.5)
            if frame is None:
                continue
            self._t_ms += self.frame_ms
            level = _rms(frame)
            avg = float(np.mean(self._moving)) if self._moving else level
            self._moving.append(level)

            ratio = level / max(avg, 1.0)
            is_peak = (level >= self.absolute_floor) and (ratio >= self.ratio)

            if is_peak:
                if self._last_peak_ms is None:
                    self._last_peak_ms = self._t_ms
                    self._post_peak_quiet_ms = 0.0
                else:
                    gap = self._t_ms - self._last_peak_ms
                    if (
                        self.min_gap_ms <= gap <= self.max_gap_ms
                        and self._post_peak_quiet_ms >= self.min_gap_ms
                    ):
                        _LOGGER.info("clap detected (gap=%.0fms)", gap)
                        self._last_peak_ms = None
                        self._post_peak_quiet_ms = 0.0
                        if self._on_clap is not None:
                            try:
                                self._on_clap()
                            except Exception as e:
                                _LOGGER.exception("clap handler raised: %s", e)
                    elif gap > self.max_gap_ms:
                        # Too late — reset, treat this as the new first peak.
                        self._last_peak_ms = self._t_ms
                        self._post_peak_quiet_ms = 0.0
            else:
                if self._last_peak_ms is not None:
                    self._post_peak_quiet_ms += self.frame_ms
                    if self._t_ms - self._last_peak_ms > self.max_gap_ms:
                        self._last_peak_ms = None
                        self._post_peak_quiet_ms = 0.0


if __name__ == "__main__":
    # Quick smoke: open the stream, print frame stats for 3 seconds.
    logging.basicConfig(level=logging.INFO)
    stream = AudioStream()
    stream.start()
    try:
        c = stream.subscribe("smoke")
        for _ in range(int(3 * SAMPLE_RATE / FRAME_SAMPLES)):
            f = c.next_frame(timeout=1.0)
            if f is not None:
                print(f"rms={_rms(f):7.1f} peak={int(np.abs(f).max())}")
    finally:
        stream.stop()
