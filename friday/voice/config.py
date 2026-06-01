"""Voice subsystem config loader.

Reads `friday_config.yaml` from the repo root and exposes a typed view of the
voice-related fields plus a few shared keys (telegram credentials).

Cache strategy: mtime-keyed. We re-parse the YAML only when the file actually
changes, so the wake-word loop can call `load()` per frame without hammering
the disk.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_LOGGER = logging.getLogger(__name__)


def _find_repo_root() -> Path:
    """Walk up from this file looking for friday_config.yaml."""
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        if (parent / "friday_config.yaml").is_file():
            return parent
    raise FileNotFoundError(
        "friday_config.yaml not found walking up from voice/config.py"
    )


REPO_ROOT = _find_repo_root()
CONFIG_PATH = REPO_ROOT / "friday_config.yaml"
DB_PATH = REPO_ROOT / "memory" / "friday_memory.db"


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool
    mic_enabled: bool
    solo_trigger_enabled: bool
    always_speak: bool

    clap_sensitivity: float
    clap_window_ms: int

    silence_ms: int
    silence_rms_threshold: int
    max_recording_ms: int
    preroll_ms: int

    whisper_model: str
    push_to_talk_key: str
    tts_voice: str
    response_timeout_s: int

    wake_phrases: List[str]
    acknowledgment_phrases: List[str]

    telegram_bot_token: str
    telegram_chat_id: str
    telegram_api_id: int
    telegram_api_hash: str

    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "mic_enabled": True,
    "solo_trigger_enabled": False,
    "always_speak": False,
    "clap_sensitivity": 0.7,
    "clap_window_ms": 600,
    "silence_ms": 1500,
    "silence_rms_threshold": 500,
    "max_recording_ms": 30000,
    "preroll_ms": 500,
    "whisper_model": "base",
    "push_to_talk_key": "right_option",
    "tts_voice": "Daniel",
    "response_timeout_s": 30,
    "wake_phrases": ["Hey Friday", "Friday you up", "Friday status", "Friday"],
    "acknowledgment_phrases": [
        "At your service, sir.",
        "For you sir, always.",
        "As you wish, sir.",
        "Welcome home, sir.",
    ],
}


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return default


def _build(data: Dict[str, Any]) -> VoiceConfig:
    voice = (data.get("voice") or {})
    persona = (data.get("persona") or {})
    telegram = (data.get("telegram") or {})

    def vget(key: str) -> Any:
        return voice.get(key, _DEFAULTS[key])

    # Acknowledgment phrases: prefer persona.jarvis_phrases if it exists and
    # has anything enabled. That avoids duplicating the curated list the user
    # already maintains there.
    jarvis_phrases = persona.get("jarvis_phrases") or {}
    enabled_from_persona = [p for p, on in jarvis_phrases.items() if on]
    if enabled_from_persona:
        ack_phrases = enabled_from_persona
    else:
        ack_phrases = list(voice.get("acknowledgment_phrases") or _DEFAULTS["acknowledgment_phrases"])

    wake_phrases = list(voice.get("wake_phrases") or _DEFAULTS["wake_phrases"])

    return VoiceConfig(
        enabled=_coerce_bool(vget("enabled"), True),
        mic_enabled=_coerce_bool(vget("mic_enabled"), True),
        solo_trigger_enabled=_coerce_bool(vget("solo_trigger_enabled"), False),
        always_speak=_coerce_bool(vget("always_speak"), False),
        clap_sensitivity=_coerce_float(vget("clap_sensitivity"), 0.7),
        clap_window_ms=_coerce_int(vget("clap_window_ms"), 600),
        silence_ms=_coerce_int(vget("silence_ms"), 1500),
        silence_rms_threshold=_coerce_int(vget("silence_rms_threshold"), 500),
        max_recording_ms=_coerce_int(vget("max_recording_ms"), 30000),
        preroll_ms=_coerce_int(vget("preroll_ms"), 500),
        whisper_model=str(vget("whisper_model")),
        push_to_talk_key=str(vget("push_to_talk_key")),
        tts_voice=str(vget("tts_voice")),
        response_timeout_s=_coerce_int(vget("response_timeout_s"), 30),
        wake_phrases=wake_phrases,
        acknowledgment_phrases=ack_phrases,
        telegram_bot_token=str(telegram.get("bot_token") or ""),
        telegram_chat_id=str(telegram.get("chat_id") or ""),
        telegram_api_id=_coerce_int(telegram.get("api_id"), 0),
        telegram_api_hash=str(telegram.get("api_hash") or ""),
        raw=data,
    )


_cache_lock = threading.Lock()
_cache_mtime: Optional[float] = None
_cache_value: Optional[VoiceConfig] = None


def load() -> VoiceConfig:
    """Return the current VoiceConfig. Re-parses YAML only when the file mtime
    changes — call this freely from hot loops."""
    global _cache_mtime, _cache_value
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError as e:
        _LOGGER.error("config file unreadable: %s", e)
        if _cache_value is not None:
            return _cache_value
        raise

    with _cache_lock:
        if _cache_value is not None and _cache_mtime == mtime:
            return _cache_value

        try:
            data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        except yaml.YAMLError as e:
            _LOGGER.error("config YAML parse failure: %s — using last good", e)
            if _cache_value is not None:
                return _cache_value
            raise

        cfg = _build(data)
        _cache_mtime = mtime
        _cache_value = cfg
        return cfg


def telegram_credentials_present(cfg: Optional[VoiceConfig] = None) -> bool:
    cfg = cfg or load()
    return bool(
        cfg.telegram_bot_token
        and cfg.telegram_chat_id
        and cfg.telegram_api_id
        and cfg.telegram_api_hash
    )


if __name__ == "__main__":
    cfg = load()
    print("repo root:", REPO_ROOT)
    print("config path:", CONFIG_PATH)
    print("db path:", DB_PATH)
    print("voice config:")
    for k, v in cfg.__dict__.items():
        if k == "raw":
            continue
        print(f"  {k}: {v}")
    print("telegram creds present:", telegram_credentials_present(cfg))
