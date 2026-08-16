"""Wake-word detection wrapper over openWakeWord.

Friday's voice layer feeds 16 kHz mono int16 PCM frames into `WakeDetector.feed`
and gets back a `WakeHit` when any configured phrase fires above its threshold.

Currently every configured phrase aliases to the bundled `hey_jarvis` model —
the custom-trained Friday models aren't ready yet (see `voice/models/README.md`).
When a real `voice/models/<phrase-slug>.onnx` exists it is picked up automatically
with no code change.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from openwakeword.model import Model

_LOGGER = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parent / "models"

DEFAULT_THRESHOLD = 0.5
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz — openWakeWord's expected chunk size
SAMPLE_RATE = 16000

HEY_JARVIS_ALIASES = {"hey friday", "hey jarvis"}


def _slug(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def _bundled_hey_jarvis_path() -> Optional[Path]:
    import openwakeword
    p = Path(openwakeword.__file__).parent / "resources" / "models" / "hey_jarvis_v0.1.onnx"
    return p if p.is_file() else None


def _resolve_model_path(phrase: str) -> Optional[Path]:
    """Pick the .onnx for a phrase. Custom model in voice/models/ wins; otherwise
    a small allowlist of phrases falls back to the bundled hey_jarvis model."""
    slug = _slug(phrase)
    custom = MODELS_DIR / f"{slug}.onnx"
    if custom.is_file() and custom.stat().st_size > 0:
        return custom
    if phrase.lower() in HEY_JARVIS_ALIASES:
        # In dev we copy the bundled file to voice/models/hey_jarvis.onnx;
        # check there first, then fall back to the package resource.
        local = MODELS_DIR / "hey_jarvis.onnx"
        if local.is_file():
            return local
        return _bundled_hey_jarvis_path()
    return None


@dataclass(frozen=True)
class WakeHit:
    phrase: str
    score: float


class WakeDetector:
    """Streaming wake-word detector.

    Args:
        phrases: ordered list of wake phrases as they appear in friday_config.yaml
            (e.g. ["Hey Jarvis", "Jarvis you up", "Jarvis status", "Jarvis"]).
        solo_trigger_enabled: if False, single-token phrases (one word) are dropped
            from the active set. Matches the spec's `voice.solo_trigger_enabled` gate.
        threshold: score above which a phrase is considered fired. Per-phrase override
            via `thresholds[phrase] = ...` after construction.
    """

    def __init__(
        self,
        phrases: Sequence[str],
        solo_trigger_enabled: bool = False,
        threshold: float = DEFAULT_THRESHOLD,
        debounce_frames: int = 25,  # 25 * 80ms = 2s — suppress repeat fires
    ):
        self.threshold = threshold
        self.debounce_frames = debounce_frames
        self.thresholds: Dict[str, float] = {}
        self._cooldown: Dict[str, int] = {}

        active: List[str] = []
        for phrase in phrases:
            if not solo_trigger_enabled and len(phrase.split()) <= 1:
                _LOGGER.info("skipping single-token wake phrase %r (solo_trigger_enabled=False)", phrase)
                continue
            active.append(phrase)
        if not active:
            raise ValueError("no usable wake phrases — enable solo_trigger or pass multi-word phrases")

        # Resolve each phrase to a model file, deduplicating by path so identical
        # aliases load one model.
        path_for_phrase: Dict[str, Optional[Path]] = {p: _resolve_model_path(p) for p in active}
        missing = [p for p, pp in path_for_phrase.items() if pp is None]
        if missing:
            _LOGGER.warning(
                "no model available for phrases %s — they will never fire. "
                "Train custom models or alias them to hey_jarvis.",
                missing,
            )

        # openwakeword's Model accepts a list of paths; the internal label is the
        # filename stem. Build phrase→label map so we can read prediction scores.
        unique_paths: List[Path] = []
        label_for_phrase: Dict[str, str] = {}
        for phrase, p in path_for_phrase.items():
            if p is None:
                continue
            if p not in unique_paths:
                unique_paths.append(p)
            label_for_phrase[phrase] = p.stem.replace("_v0.1", "")  # owww strips _vX

        if not unique_paths:
            raise RuntimeError("no wake-word model files were resolved")

        self.phrase_for_label: Dict[str, List[str]] = {}
        for phrase, label in label_for_phrase.items():
            self.phrase_for_label.setdefault(label, []).append(phrase)

        _LOGGER.info("WakeDetector loading models: %s", [str(p) for p in unique_paths])
        self._model = Model(
            wakeword_models=[str(p) for p in unique_paths],
            inference_framework="onnx",
        )
        _LOGGER.info("WakeDetector ready. Phrase→label map: %s", label_for_phrase)

    def set_threshold(self, phrase: str, value: float) -> None:
        """Override the firing threshold for one phrase."""
        self.thresholds[phrase] = value

    def reset(self) -> None:
        """Clear internal buffer + per-phrase cooldowns. Call after a wake-cycle
        finishes so a long session of speech doesn't keep firing."""
        self._model.reset()
        self._cooldown.clear()

    def feed(self, pcm16: np.ndarray) -> Optional[WakeHit]:
        """Feed one chunk of audio. Returns the first phrase that fires this call,
        or None. Chunks must be int16 PCM at 16 kHz. Sizes other than FRAME_SAMPLES
        are accepted — openwakeword buffers internally."""
        if pcm16.dtype != np.int16:
            raise ValueError(f"expected int16 PCM, got {pcm16.dtype}")
        scores = self._model.predict(pcm16)

        # Decay cooldowns by one frame regardless of whether anything fired.
        for label in list(self._cooldown):
            self._cooldown[label] -= 1
            if self._cooldown[label] <= 0:
                del self._cooldown[label]

        for label, score in scores.items():
            if label in self._cooldown:
                continue
            phrases = self.phrase_for_label.get(label, [label])
            for phrase in phrases:
                threshold = self.thresholds.get(phrase, self.threshold)
                if score >= threshold:
                    self._cooldown[label] = self.debounce_frames
                    return WakeHit(phrase=phrase, score=float(score))
        return None


if __name__ == "__main__":
    # Smoke check — load detector with the default config and feed silence.
    logging.basicConfig(level=logging.INFO)
    det = WakeDetector(phrases=["Hey Jarvis", "Jarvis you up", "Jarvis status", "Jarvis"])
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    hit = det.feed(silence)
    print("silence hit:", hit)
    print("phrase→label map:", det.phrase_for_label)
