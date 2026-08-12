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
if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
