"""
tests/test_channels.py
The channel contract. Plain asserts, no test framework.

    python3 tests/test_channels.py     (from the friday/ package directory)

The contract lived in a docstring until now, so nothing checked that the one
implementation matched it. These tests are the check: they assert conformance
structurally, assert that an incomplete channel fails at CONSTRUCTION rather
than at first use, and assert that the effects layer's wrapper does not lose
methods on the way through.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels.base import Channel                             # noqa: E402
from effects import entry                                     # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


class Complete(Channel):
    name = "complete"

    def __init__(self):
        self.calls = []

    def send(self, text: str) -> bool:
        self.calls.append(("send", text))
        return True

    def send_permission_request(self, proposal: str, pending_key: str) -> bool:
        self.calls.append(("card", proposal))
        return True

    def notify(self, title: str, text: str) -> bool:
        self.calls.append(("notify", f"{title}|{text}"))
        return True


print("\n-- the contract --")
c = Complete()
check("a complete channel constructs", isinstance(c, Complete))
check("a complete channel satisfies the protocol", isinstance(c, Channel))


class Incomplete(Channel):
    name = "incomplete"

    def send(self, text: str) -> bool:
        return True


try:
    Incomplete()
    check("a channel missing a method is refused at construction", False)
except TypeError:
    check("a channel missing a method is refused at construction", True)


print("\n-- duck typing still works --")


class Duck:
    """Not a subclass. effects/runner.py must keep accepting this — the tests
    use one, and so does anything wrapping a channel."""
    name = "duck"

    def send(self, text): return True
    def send_permission_request(self, proposal, pending_key): return True
    def notify(self, title, text): return True


check("a structurally identical non-subclass satisfies the protocol",
      isinstance(Duck(), Channel))


print("\n-- the history wrapper is transparent --")
wrapped = entry.as_history_channel(Complete(), None)
check("the wrapper keeps notify", callable(wrapped.notify))
check("the wrapper keeps the channel name", wrapped.name == "complete")
check("the wrapper still satisfies the protocol", isinstance(wrapped, Channel))


print("\n-- the real Telegram channel --")
try:
    from channels.telegram import TelegramHandler
    h = TelegramHandler({"telegram": {"bot_token": "x", "chat_id": "1"}},
                        None, None)
    check("TelegramHandler implements Channel explicitly",
          Channel in TelegramHandler.__mro__)
    check("TelegramHandler satisfies the protocol", isinstance(h, Channel))
    check("TelegramHandler names itself", h.name == "telegram")
except ImportError as e:
    # python-telegram-bot is a production dependency, not a test one.
    print(f"SKIP  TelegramHandler ({e})")


print()


# ── Failure phrasing belongs to the channel ──────────────────────────────────
#
# The taxonomy is shared; the words are the channel's. Before step 6 the words
# were a dict in channels/conversation.py, above the channels, so a surface
# that should phrase something differently had no say in it.

from channels.base import (DEFAULT_FAILURE_TEXT, default_failure_text,  # noqa: E402
                           failure_text_for)

print("\n-- failure phrasing --")

check("every shared kind has a default sentence",
      all(DEFAULT_FAILURE_TEXT.get(k) for k in
          ("rate_limit", "transient", "network", "timeout", "empty")))

# The distinction is the point of the taxonomy. A channel may change the
# words; it may not make two kinds read the same, because "you are over quota"
# and "this network cannot reach the API" are acted on differently — the
# second one means switch to the dashboard.
check("the sentences for the kinds are all distinct",
      len(set(DEFAULT_FAILURE_TEXT.values())) == len(DEFAULT_FAILURE_TEXT))

check("network is distinguishable from transient",
      DEFAULT_FAILURE_TEXT["network"] != DEFAULT_FAILURE_TEXT["transient"])

check("an unrecognised kind falls through to the raw detail",
      default_failure_text("fatal", "400 INVALID_ARGUMENT")
      == "LLM error, sir: 400 INVALID_ARGUMENT")

check("an unrecognised kind with no detail still says something",
      bool(default_failure_text("fatal")))

# Both real channels take the default today. That is the honest state: the
# seam exists, nothing has needed to use it yet, and a dashboard retry banner
# is a step-7 conversation.
# NOT a protocol member — see channels/base.py. A fourth member on a
# runtime_checkable Protocol un-Channels every duck-typed implementation,
# which is exactly what happened when this was tried the other way round.
check("failure_text is not part of the transport contract",
      not hasattr(Complete(), "failure_text"))

check("a channel with no opinion gets the default",
      failure_text_for(Complete(), "network")
      == DEFAULT_FAILURE_TEXT["network"])


class Loud(Complete):
    """A channel that phrases failures its own way."""
    def failure_text(self, kind, detail=""):
        return f"[{kind}]"


check("a channel that overrides failure_text is asked",
      failure_text_for(Loud(), "network") == "[network]")

# Duck-typed channels are supported on purpose (see above), and one that never
# heard of failure_text still has to get a sentence rather than an
# AttributeError on the path that is already reporting a failure.
check("a duck-typed channel still gets a sentence",
      failure_text_for(Duck(), "network") == DEFAULT_FAILURE_TEXT["network"])


class Broken(Complete):
    def failure_text(self, kind, detail=""):
        raise RuntimeError("boom")


# A channel's phrasing must not fail the reply it was phrasing. This is the
# same contract as send(): a channel that raises takes the turn down with it.
check("a channel whose phrasing raises falls back instead of propagating",
      failure_text_for(Broken(), "network") == DEFAULT_FAILURE_TEXT["network"])

# The wrapper delegates by __getattr__, so a wrapped channel keeps its own
# phrasing rather than silently reverting to the default.
check("a HistoryChannel-wrapped channel keeps the inner channel's phrasing",
      failure_text_for(entry.as_history_channel(Loud(), None), "network") == "[network]")


if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
