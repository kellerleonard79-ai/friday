"""voice/stt.py — speech-to-text helper.

Thin wrapper around openai-whisper so callers can do
`from voice.stt import transcribe; text = transcribe(wav_path)`
without touching whisper internals or worrying about model lifecycle.

The model is loaded lazily on first call and cached for the lifetime of the
process — whisper.load_model takes seconds, and re-loading on every clap
would make the trigger feel sluggish. Model name comes from
`voice.whisper_model` in friday_config.yaml (default "base").
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

_LOGGER = logging.getLogger("friday.voice.stt")

_SAMPLE_RATE = 16000
_model = None
_model_lock = threading.Lock()
_loaded_name: Optional[str] = None


def _read_config_model_name() -> str:
    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "friday_config.yaml"
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return "base"
    return str((cfg.get("voice") or {}).get("whisper_model") or "base")


def _ensure_model(name: str):
    global _model, _loaded_name
    with _model_lock:
        if _model is not None and _loaded_name == name:
            return _model
        import whisper  # heavy import; defer until needed
        _LOGGER.info("loading whisper model %r...", name)
        t0 = time.time()
        _model = whisper.load_model(name)
        _loaded_name = name
        _LOGGER.info("whisper ready in %.1fs", time.time() - t0)
        return _model


def transcribe(wav_path: str, model_name: Optional[str] = None) -> str:
    """Transcribe a 16 kHz mono WAV file at `wav_path`. Returns the stripped
    transcript, or an empty string on any failure (model load error, empty
    audio, whisper exception)."""
    if not os.path.isfile(wav_path):
        _LOGGER.warning("transcribe: missing wav file %s", wav_path)
        return ""
    try:
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            pcm = np.frombuffer(wf.readframes(n), dtype=np.int16)
    except (wave.Error, OSError) as e:
        _LOGGER.warning("transcribe: failed reading %s: %s", wav_path, e)
        return ""
    if pcm.size == 0:
        _LOGGER.warning("transcribe: %s is empty", wav_path)
        return ""
    if sr != _SAMPLE_RATE:
        _LOGGER.warning("transcribe: unexpected sample rate %d (expected %d)", sr, _SAMPLE_RATE)

    audio = pcm.astype(np.float32) / 32768.0
    name = model_name or _read_config_model_name()
    try:
        model = _ensure_model(name)
        result = model.transcribe(audio, fp16=False, language="en")
    except Exception as e:
        _LOGGER.exception("transcribe: whisper failed: %s", e)
        return ""
    return (result.get("text") or "").strip()
