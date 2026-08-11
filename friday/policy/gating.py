"""
policy/gating.py
One question: does this action need the user's permission first?

    decide(action) -> AUTO | GATED

THE TABLE.

                          | Reversible (create, update) | Irreversible (delete)
    ----------------------+-----------------------------+----------------------
    User stated the fact  | AUTO                        | GATED
    Friday inferred it    | GATED                       | GATED

The axis that matters is WHO ASSERTED THE FACT, not how important the action
is. "Add tennis practice Thursday at four" is the user telling Friday
something; a confirmation card for a thing they just asked for out loud is
friction, not safety, and friction on the common path is what trains people to
tap Confirm without reading. An event Friday extracted from a GroupMe message
is Friday's guess, and a guess that writes to a calendar unasked is the
behavior that makes an assistant untrustworthy.

The second axis is whether the action can be taken back. A wrong create is a
line the user deletes. A wrong delete is data that is gone, and Friday cannot
know what was in the description of the event it just removed. So deletes are
gated regardless of who asked — the one cell where certainty about intent does
not buy anything, because the cost of being wrong is unbounded.

AUTO_UPDATE HAS AN EXTRA CONDITION. An update names a target, and the model
can produce a plausible identifier from its own memory of the conversation as
easily as from a read. An update whose target was not proven by a read IN THE
SAME TURN is not a user-stated fact about that event — it is an inference
about which event the user meant — so it is gated. The ledger is what answers
this, and it is per-turn for exactly this reason: a read from an earlier turn
is not a fact about now, because the user may well have changed the calendar
because of what Friday just said.

NOTHING HERE PRODUCES INFERRED FACTS TODAY. The only input channel is the user
typing, so every action reaching this module is user-stated and the inferred
row has no live producer. It is built and tested anyway: the connectors that
will produce inferred facts arrive in a later step, and the moment a connector
is being written is the wrong moment to also be deciding what the gate means.

This module makes a DECISION and performs no action. It does not write, does
not send, does not touch the database. The caller acts on the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("friday.policy.gating")

# Who asserted the fact behind this action.
#
#   user_stated — the user said it, in words, in this conversation. Typing
#                 "add lunch Thursday" is user-stated. So is answering a
#                 question Friday asked.
#   inferred    — Friday worked it out. An event extracted from a GroupMe
#                 message, a due date read out of an image, a time the model
#                 guessed because the user did not give one.
Provenance = Literal["user_stated", "inferred"]

# What the action does to the world. Reversibility is about the DATA, not the
# API: an update is reversible because the previous values are recoverable
# from a read, and a delete is not because they are gone.
Operation = Literal["create", "update", "delete"]

Decision = Literal["AUTO", "GATED"]

_REVERSIBLE: tuple[Operation, ...] = ("create", "update")


@dataclass(frozen=True, slots=True)
class Action:
    """What is about to happen, in the terms the gate cares about.

    `target_day` and `target_proven` exist for the update case. `target_proven`
    is the caller's report of whether the ledger covers the day this action
    touches — the gate does not read the ledger itself, so that a decision can
    be tested and reasoned about without constructing a turn.
    """
    operation: Operation
    provenance: Provenance
    tool: str = ""
    target_proven: bool = False


def decide(action: Action) -> Decision:
    """AUTO or GATED. See the table in the module docstring."""
    if action.operation not in _REVERSIBLE:
        # Irreversible. Gated whoever asked — the one cell where certainty
        # about intent buys nothing, because the cost of being wrong is data
        # that cannot be recovered.
        return "GATED"

    if action.provenance != "user_stated":
        return "GATED"

    if action.operation == "update" and not action.target_proven:
        # The user stated the change; they did not state WHICH EVENT. A target
        # the model produced from memory rather than from a read this turn is
        # an inference, and inferences are gated.
        logger.info(
            f"{action.tool or 'update'}: gating an update whose target was not "
            f"proven by a read this turn."
        )
        return "GATED"

    return "AUTO"


def needs_card(action: Action) -> bool:
    return decide(action) == "GATED"
