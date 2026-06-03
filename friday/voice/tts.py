"""Text-to-speech via macOS `say`, with markdown stripping and length capping.

Output device detection is here too (`external_audio_present`) — listen.py
calls it per-session so plugging headphones in mid-session enables TTS.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import List, Optional

import pyaudio

_LOGGER = logging.getLogger(__name__)

MAX_SPEAK_CHARS = 500
TRUNCATION_TAIL = "...I've sent the full response to Telegram, sir."

# Devices we *want* TTS on (case-insensitive substring match).
EXTERNAL_KEYWORDS = (
    "airpods",
    "headphones",
    "headphone",
    "external",
    "usb",
    "bluetooth",
    "scarlett",
    "aggregate",
)

# Devices we explicitly do NOT count as external even if their name might
# otherwise match (defensive — none currently, but easy to extend).
INTERNAL_DENYLIST = (
    "macbook pro speakers",
    "macbook air speakers",
    "built-in output",
    "internal speakers",
)


# ---------------------------------------------------------------------------
# Markdown stripping
# ---------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"```[\w-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD_ITALIC = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_BLOCKQUOTE = re.compile(r"^\s*>+\s*", re.MULTILINE)
_HRULE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_MULTI_BLANK = re.compile(r"\n{3,}")

# Emoji stripping for TTS: macOS `say` will otherwise verbalize codepoint names
# ("memo", "pushpin", "check mark"). Visual emoji in Telegram messages — the
# approval-card icons 📌 📅 🗂 📝, the confirm/cancel buttons ✅ ❌, etc. —
# stay in chat; we only filter them out of the speech path. Covers the BMP
# misc-symbols/dingbats blocks plus the Supplementary Multilingual Plane
# pictograph ranges, with an optional trailing variation selector.
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002600-\U000027BF"
    "\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF"
    "]"
    "[\U0000FE0E\U0000FE0F]?",
)
_EXTRA_SPACES = re.compile(r" {2,}")
_LEADING_SPACE_ON_LINE = re.compile(r"\n +")


def strip_markdown(text: str) -> str:
    text = _CODE_FENCE.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _BOLD_ITALIC.sub(r"\2", text)
    text = _HEADER.sub("", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _HRULE.sub("", text)
    text = _EMOJI.sub("", text)
    text = _EXTRA_SPACES.sub(" ", text)
    text = _LEADING_SPACE_ON_LINE.sub("\n", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def _cap_length(text: str, limit: int = MAX_SPEAK_CHARS) -> str:
    if len(text) <= limit:
        return text
    # Find the last sentence boundary before the limit.
    head = text[:limit]
    matches = list(_SENTENCE_END.finditer(head))
    if matches:
        cut = matches[-1].end()
        head = text[:cut].rstrip()
    return f"{head} {TRUNCATION_TAIL}"


# ---------------------------------------------------------------------------
# speak()
# ---------------------------------------------------------------------------

_say_lock = threading.Lock()


def _say_worker(voice: str, text: str) -> None:
    with _say_lock:
        try:
            subprocess.run(
                ["say", "-v", voice, text],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            _LOGGER.warning("`say` failed: %s", e)


def speak(text: str, voice: str = "Daniel") -> threading.Thread:
    """Speak `text` via macOS `say -v <voice>`. Non-blocking — returns the
    worker thread so callers can join() if they need to wait for completion.
    Strips markdown, truncates at 500 chars."""
    if not text:
        return threading.Thread(target=lambda: None, daemon=True)
    clean = _cap_length(strip_markdown(text))
    t = threading.Thread(target=_say_worker, args=(voice, clean), daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# external_audio_present()
# ---------------------------------------------------------------------------

def _device_names(pa: pyaudio.PyAudio) -> List[str]:
    names: List[str] = []
    for i in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(i)
        except Exception:
            continue
        if info.get("maxOutputChannels", 0) <= 0:
            continue
        names.append(str(info.get("name") or "").strip())
    return names


def external_audio_present() -> bool:
    """True if any active output device looks like AirPods / headphones / USB /
    Bluetooth / Scarlett / an aggregate device. Re-creates PyAudio every call
    so freshly-plugged devices are seen immediately."""
    try:
        pa = pyaudio.PyAudio()
    except Exception as e:
        _LOGGER.warning("PyAudio init failed in external_audio_present: %s", e)
        return False
    try:
        names = _device_names(pa)
    finally:
        try:
            pa.terminate()
        except Exception:
            pass

    for raw in names:
        n = raw.lower()
        if any(deny in n for deny in INTERNAL_DENYLIST):
            continue
        if any(kw in n for kw in EXTERNAL_KEYWORDS):
            _LOGGER.debug("external audio device detected: %s", raw)
            return True
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "At your service, sir."
    print("external_audio_present:", external_audio_present())
    print("speaking:", text)
    speak(text).join()
