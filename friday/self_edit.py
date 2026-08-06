"""
self_edit.py
The narrow slice of itself Friday is allowed to change at runtime.

Two stores, both plain YAML:

  friday_voice.yaml   (paths.data_dir())  — learned content: confirmation
                      quips, general voice phrases, and bundled quips the
                      user has asked Friday to stop using.
  friday_config.yaml  (paths.config_path()) — a WHITELIST of settings, and
                      nothing else. See _SETTINGS.

Everything here writes to paths.data_dir(). Nothing writes through
paths.resource_path() — that resolves into the read-only PyInstaller bundle on
Windows/frozen builds, which is why learned quips live in their own file
instead of being appended to the bundled quips.yaml.

Friday does NOT edit its own Python source. That is deliberate: the core runs
always-on under launchd (macOS) or tray.py (Windows), both of which relaunch it
on exit, so a syntax error would become a silent restart loop rather than a
visible failure.
"""

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

import paths

logger = logging.getLogger("friday.self_edit")

VOICE_FILENAME = "friday_voice.yaml"

# The three lists in friday_voice.yaml. Anything else in the file is preserved
# on write but ignored on read.
_LISTS = ("confirm_quips", "voice_phrases", "disabled_quips")

MAX_PHRASE_LEN = 200
MAX_PHRASES    = 60

# Markdown/emoji are banned in Friday's output by AGENTS.md, so a
# phrase containing them can never be used as written. Reject at the door
# rather than storing something that will read wrong later.
_EMOJI_RE    = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿]"
)
_MARKDOWN_RE = re.compile(r"(\*\*|__|^\s*[-*+]\s|^\s*#|`)", re.MULTILINE)


def voice_path() -> Path:
    """Resolved lazily — paths.data_dir() creates the directory on call, and
    that is not something an import should do."""
    return paths.data_dir() / VOICE_FILENAME


# ── Read ─────────────────────────────────────────────────────────────────────
# mtime-keyed cache. load() is called on every quip pick and (via core.py) on
# every persona composition, so the common case must not hit the disk twice.

_cache: dict | None = None
_cache_mtime: float = -1.0

# Bumped on every write from this process. Paired with the file mtimes below
# because mtime resolution is not guaranteed to be finer than a second on every
# filesystem — two edits inside one tick would otherwise look like no edit at
# all, and the persona would keep serving the stale voice.
_revision: int = 0


def mtime() -> float:
    """Modification time of the voice file, or 0.0 if it does not exist yet."""
    try:
        return voice_path().stat().st_mtime
    except OSError:
        return 0.0


def _config_mtime() -> float:
    try:
        return paths.config_path().stat().st_mtime
    except OSError:
        return 0.0


def version() -> tuple:
    """Opaque cache key covering everything Friday can edit about itself.
    Changes whenever the voice file or the config changes, in this process or
    any other (the dashboard writes the same config)."""
    return (mtime(), _config_mtime(), _revision)


def load() -> dict:
    """Return {confirm_quips, voice_phrases, disabled_quips}, always lists."""
    global _cache, _cache_mtime
    m = mtime()
    if _cache is not None and m == _cache_mtime:
        return _cache
    data = _read_raw()
    _cache = {k: [str(x) for x in (data.get(k) or []) if str(x).strip()]
              for k in _LISTS}
    _cache_mtime = m
    return _cache


def _read_raw() -> dict:
    """The full file, including any keys we don't own, so a write round-trips
    a hand-edited file without dropping anything."""
    try:
        with open(voice_path()) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Could not read {voice_path()}: {e} — treating as empty")
        return {}


def list_phrases() -> dict:
    """Everything Friday could say, split by where it came from. `bundled` is
    read-only (it ships inside the app); only `learned` entries can be removed
    outright — a bundled quip is suppressed via disabled_quips instead."""
    import phrases
    store = load()
    disabled = {p.casefold() for p in store["disabled_quips"]}
    bundled = phrases.bundled_quips()
    return {
        "confirmation": {
            "learned":  list(store["confirm_quips"]),
            "bundled":  [q for q in bundled if q.casefold() not in disabled],
            "disabled": list(store["disabled_quips"]),
        },
        "voice": {"learned": list(store["voice_phrases"])},
    }


# ── Write ────────────────────────────────────────────────────────────────────

_TARGETS = {
    "confirmation": ("confirm_quips",),
    "voice":        ("voice_phrases",),
    "both":         ("confirm_quips", "voice_phrases"),
}


def _clean(text: str) -> str:
    """Normalize a phrase or raise ValueError with a message meant for the user."""
    if not isinstance(text, str):
        raise ValueError("Phrase must be text.")
    t = " ".join(text.split())          # collapse newlines and runs of spaces
    # Users (and voice transcription) often wrap the phrase in quotes; the
    # stored form should not carry them, since quip_prompt quotes it again.
    while len(t) >= 2 and t[0] in "\"'“‘" and t[-1] in "\"'”’":
        t = t[1:-1].strip()
    if not t:
        raise ValueError("Phrase is empty.")
    if len(t) > MAX_PHRASE_LEN:
        raise ValueError(f"Phrase is too long ({len(t)} chars, max {MAX_PHRASE_LEN}).")
    if _EMOJI_RE.search(t):
        raise ValueError("Phrase contains emoji, which Friday never uses.")
    if _MARKDOWN_RE.search(t):
        raise ValueError("Phrase contains markdown, which Friday never uses.")
    return t


def _save(data: dict) -> None:
    """Atomic write — tmp file in the same directory, then move over the
    original, so a crash mid-write can never leave a truncated voice file."""
    global _cache, _cache_mtime, _revision
    path = voice_path()
    fd, tmp = tempfile.mkstemp(prefix=".friday_voice_", suffix=".yaml",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=True)
        shutil.move(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _revision += 1
    _cache = None
    _cache_mtime = -1.0


def add_phrase(text: str, target: str = "both") -> dict:
    """Store a phrase verbatim. Returns {"ok": bool, ...}, never raises."""
    keys = _TARGETS.get((target or "both").lower())
    if keys is None:
        return {"ok": False, "error":
                f"target must be one of {', '.join(_TARGETS)} — got '{target}'."}
    try:
        phrase = _clean(text)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    import phrases as _phrases
    bundled = {q.casefold() for q in _phrases.bundled_quips()}

    data = _read_raw()
    added, already = [], []
    for key in keys:
        existing = [str(x) for x in (data.get(key) or [])]
        if any(x.casefold() == phrase.casefold() for x in existing):
            already.append(key)
            continue
        # Re-adding a quip the user had retired should un-retire it (below),
        # not stash a second learned copy alongside the bundled original.
        if key == "confirm_quips" and phrase.casefold() in bundled:
            already.append(key)
            continue
        if len(existing) >= MAX_PHRASES:
            return {"ok": False, "error":
                    f"Already storing {MAX_PHRASES} phrases for {key}; "
                    f"remove one before adding another."}
        existing.append(phrase)
        data[key] = existing
        added.append(key)

    # Adding a phrase back un-disables it — otherwise the filter in phrases.py
    # would silently swallow the thing the user just asked for.
    disabled = [str(x) for x in (data.get("disabled_quips") or [])]
    kept = [x for x in disabled if x.casefold() != phrase.casefold()]
    if len(kept) != len(disabled):
        data["disabled_quips"] = kept
        added.append("disabled_quips (re-enabled)")

    if not added:
        return {"ok": True, "status": "already_present", "phrase": phrase,
                "targets": already}
    _save(data)
    logger.info(f"Learned phrase ({', '.join(added)}): {phrase!r}")
    return {"ok": True, "status": "added", "phrase": phrase, "targets": added}


def remove_phrase(text: str, target: str = "both") -> dict:
    """Remove a learned phrase, or suppress a bundled quip.

    Matching is case-insensitive substring, so the user can say "the touch
    grass one". Multiple matches return the candidates instead of guessing —
    deleting the wrong phrase is not something they can see happening.
    """
    keys = _TARGETS.get((target or "both").lower())
    if keys is None:
        return {"ok": False, "error":
                f"target must be one of {', '.join(_TARGETS)} — got '{target}'."}
    needle = " ".join((text or "").split()).strip("\"'“”‘’").casefold()
    if not needle:
        return {"ok": False, "error": "Nothing to match against."}

    import phrases
    data = _read_raw()
    store = load()

    # Candidates: learned phrases in the requested targets, plus bundled quips
    # when the confirmation pool is in scope.
    candidates: list[tuple[str, str]] = []   # (phrase, origin)
    for key in keys:
        for p in store[key]:
            if needle in p.casefold():
                candidates.append((p, key))
    if "confirm_quips" in keys:
        disabled = {p.casefold() for p in store["disabled_quips"]}
        for q in phrases.bundled_quips():
            if needle in q.casefold() and q.casefold() not in disabled:
                candidates.append((q, "bundled"))

    if not candidates:
        return {"ok": False, "status": "not_found", "query": text,
                "error": "No stored phrase matches that."}

    # An exact match is never ambiguous, even when it happens to be a substring
    # of some longer phrase. The dashboard always sends the full text, and the
    # LLM usually does too — neither should be told to disambiguate a phrase it
    # named in full.
    exact = [c for c in candidates if c[0].casefold() == needle]
    if exact:
        candidates = exact

    # Same phrase learned under both targets is one decision, not two. Keep the
    # original casing — these get read back to the user verbatim.
    unique: dict[str, str] = {}
    for p, _ in candidates:
        unique.setdefault(p.casefold(), p)
    if len(unique) > 1:
        return {"ok": False, "status": "ambiguous", "query": text,
                "matches": sorted(unique.values()),
                "error": "Several phrases match — ask the user which one."}

    phrase = candidates[0][0]
    origins = {origin for _, origin in candidates}
    removed = []
    for key in keys:
        existing = [str(x) for x in (data.get(key) or [])]
        kept = [x for x in existing if x.casefold() != phrase.casefold()]
        if len(kept) != len(existing):
            data[key] = kept
            removed.append(key)
    if "bundled" in origins:
        # quips.yaml ships inside the app bundle and is read-only there, so a
        # bundled quip is retired by name rather than deleted.
        disabled = [str(x) for x in (data.get("disabled_quips") or [])]
        if not any(x.casefold() == phrase.casefold() for x in disabled):
            disabled.append(phrase)
            data["disabled_quips"] = disabled
            removed.append("disabled")

    if not removed:
        return {"ok": False, "status": "not_found", "query": text,
                "error": "No stored phrase matches that."}
    _save(data)
    logger.info(f"Removed phrase ({', '.join(removed)}): {phrase!r}")
    return {"ok": True, "status": "removed", "phrase": phrase, "targets": removed}


# ── Settings ─────────────────────────────────────────────────────────────────
# A whitelist, not a denylist: a key Friday has never heard of is refused. Every
# entry here applies without a restart — persona.* through the live persona
# recomposition in agent/core.py, default_calendar because tools read it from
# the config dict per call, briefing times via job re-registration in tools.py.
#
# Never add: api keys, bot tokens, chat_id, provider, model names, any path,
# db_path, iCal URLs. Those stay in the dashboard and the setup wizard.

_PRESETS = ("professional", "butler", "friday")
_SNARK   = ("none", "medium", "maximum")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

MAX_CUSTOM_INSTRUCTIONS = 500


def _v_choice(options):
    def check(v: str) -> str:
        s = str(v).strip().lower()
        if s not in options:
            raise ValueError(f"must be one of: {', '.join(options)}")
        return s
    return check


def _v_time(v: str) -> str:
    s = str(v).strip()
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError("must be a 24-hour time like 07:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _v_text(limit: int):
    def check(v: str) -> str:
        s = " ".join(str(v).split())
        if len(s) > limit:
            raise ValueError(f"must be {limit} characters or fewer")
        return s
    return check


_SETTINGS: dict[str, tuple] = {
    # key: (validator, human description)
    "persona.snark_level":        (_v_choice(_SNARK),   "how sarcastic Friday is"),
    "persona.preset":             (_v_choice(_PRESETS), "Friday's overall mode"),
    "persona.custom_instructions": (_v_text(MAX_CUSTOM_INSTRUCTIONS),
                                    "free-form standing instructions"),
    "agent.default_calendar":     (_v_text(120), "calendar new events go to"),
    "agent.morning_briefing_time": (_v_time, "when the morning briefing fires"),
    "agent.briefing_time":        (_v_time, "when the evening briefing fires"),
}

SETTABLE_KEYS = tuple(_SETTINGS)


def sync_briefing_times(cfg: dict) -> None:
    """Keep agent.{morning_briefing_time,briefing_time} in sync with the
    notifications mirror. The agent block stays canonical for the job_queue.

    Both the dashboard and update_setting write briefing times, so this lives
    here rather than in either writer — a mirror only one of them maintains
    goes stale the moment the other is used.
    """
    notif = cfg.get("notifications") or {}
    agent_cfg = cfg.setdefault("agent", {})
    morning = (notif.get("morning_briefing") or {}).get("time")
    evening = (notif.get("evening_briefing") or {}).get("time")
    if morning:
        agent_cfg["morning_briefing_time"] = morning
    if evening:
        agent_cfg["briefing_time"] = evening


def _mirror_briefing_time(cfg: dict, key: str, value: str) -> None:
    """The reverse of sync_briefing_times: agent.* changed, push it out to the
    notifications block the dashboard reads."""
    which = "morning_briefing" if key.endswith("morning_briefing_time") else "evening_briefing"
    notif = cfg.setdefault("notifications", {})
    block = notif.setdefault(which, {})
    block["time"] = value


def _save_config(cfg: dict) -> None:
    path = paths.config_path()
    fd, tmp = tempfile.mkstemp(prefix=".friday_config_", suffix=".yaml",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=True)
        shutil.move(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    global _revision
    _revision += 1


def update_setting(key: str, value: str, live_config: dict | None = None) -> dict:
    """Validate and persist one whitelisted setting.

    `live_config` is the dict the running agent holds. It is mutated IN PLACE
    (not replaced) because agent._config and the make_tools closure are the
    same object — an in-place update is what makes the change visible to the
    current process without a restart.
    """
    k = (key or "").strip()
    entry = _SETTINGS.get(k)
    if entry is None:
        return {"ok": False, "status": "not_settable", "key": key,
                "settable_keys": list(SETTABLE_KEYS),
                "error": f"'{key}' is not a setting Friday may change. "
                         f"Everything else is edited in the dashboard."}
    validator, description = entry
    try:
        clean = validator(value)
    except ValueError as e:
        return {"ok": False, "status": "invalid", "key": k,
                "error": f"{k} {e} — got '{value}'."}

    section, leaf = k.split(".", 1)
    try:
        with open(paths.config_path()) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return {"ok": False, "error": f"Could not read config: {e}"}

    previous = (cfg.get(section) or {}).get(leaf)
    cfg.setdefault(section, {})[leaf] = clean
    if k.endswith("briefing_time"):
        _mirror_briefing_time(cfg, k, clean)
    try:
        _save_config(cfg)
    except Exception as e:
        return {"ok": False, "error": f"Could not write config: {e}"}

    if live_config is not None:
        live_config.setdefault(section, {})[leaf] = clean
        if k.endswith("briefing_time"):
            _mirror_briefing_time(live_config, k, clean)

    logger.info(f"Setting changed: {k} = {clean!r} (was {previous!r})")
    return {"ok": True, "status": "updated", "key": k, "value": clean,
            "previous": previous, "description": description}
