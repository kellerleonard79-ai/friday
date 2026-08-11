"""
tools/scratch.py
Per-turn working storage for tools, carried across the executor's thread hop.

Tools run in a worker pool, not on the thread that started the turn, so
anything a tool wants to keep for the duration of one turn cannot simply live
in a thread-local the turn set up — it would be invisible from inside the tool.
That is not hypothetical: it is exactly how the calendar read cache came to
never hit and the fact ledger came to be permanently absent.

So the turn owns a plain dict, and tools/executor.py installs it on whichever
worker thread is about to run a tool, for exactly the length of that call.

DELIBERATELY OPAQUE. This holds whatever a tool module wants to keep, under a
key it chooses, and neither the executor nor the turn loop looks inside. It is
NOT where the ledger lives — the ledger is passed explicitly and tool code has
no path to it. Scratch is for a tool's own convenience; the ledger is evidence,
and the two must not share a container that tools can reach.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

_local = threading.local()


@contextmanager
def installed(store: dict[str, Any] | None):
    """Make `store` this thread's scratch for the duration of the block.

    Restores whatever was there before rather than clearing, so a nested call
    cannot silently discard its caller's storage.
    """
    previous = getattr(_local, "store", None)
    _local.store = store
    try:
        yield
    finally:
        _local.store = previous


def space(key: str) -> dict[str, Any] | None:
    """This turn's storage for `key`, or None outside a turn.

    None rather than a fresh dict: a tool handed a private dict nobody keeps
    would cache into the void and appear to work, which is the failure this
    module exists because of.
    """
    store = getattr(_local, "store", None)
    if store is None:
        return None
    return store.setdefault(key, {})
