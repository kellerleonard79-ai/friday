"""
router/clarify.py
Asking for what is missing, and knowing when to stop asking.

    guard(conn, plan) -> (plan to run, extra context blocks)

TWO ROUNDS, THEN ANSWER WITH WHAT IS KNOWN. Specified since step 3 and only
now reachable, because CLARIFY had nowhere to be chosen from until the router
existed.

WHY A CAP AT ALL. A clarification loop is the one failure mode that costs the
user rather than the budget: two model calls that both end in a question read
as Friday being obtuse, and the third reads as broken. There is also a
specific way it goes wrong here — the classifier sees ONE MESSAGE AND NO
HISTORY (see router/classify.py), so the user's ANSWER to a clarifying
question is itself a bare fragment, "Its work" or "tomorrow at 3", which is
exactly the shape that routes to CLARIFY again. The cap is what stops Friday
asking a question, receiving its answer, and asking the same question about
the answer.

WHY THE THIRD ROUND IS NOT SIMPLY REFUSED. "Answer with what is known" means
the model gets its full capability back — plan=None, CHAT's own tool scope —
plus an instruction to stop asking. Dropping to a plan with no tools would
mean the third round is the one that CANNOT act on anything it worked out,
which is the opposite of the intent.

THE STREAK IS EXPLICIT STATE, not derived. It could have been read off
llm_exchanges.plan or off conversation_history, and both are worse: the first
makes an instrumentation table load-bearing for behavior, and the second has
no column that records a plan at all. One key in system_state, incremented on
a clarify and deleted on anything else, is the whole mechanism.
"""

from __future__ import annotations

import logging

import memory.state as state
from llm.types import ContextBlock
from router.plans import Plan

logger = logging.getLogger("friday.router.clarify")

# Rounds of asking before Friday answers with what it has. Two.
MAX_ROUNDS = 2

_STREAK_KEY = "clarify_streak"

# What the third round is told instead. Not a plan directive: this runs with NO
# plan, so it has nowhere to live on the table, and that is the honest shape —
# it is an instruction about this specific turn rather than a property of a
# turn shape.
_STOP_ASKING = (
    "You have already asked this user for clarification twice and they are "
    "still here. Do not ask a third question. Act on the most reasonable "
    "reading of what they have said, state the assumption you made in one "
    "clause, and carry on. If you genuinely cannot act, say plainly what you "
    "would need — as a statement, not a question."
)


def guard(conn, plan: Plan | None) -> tuple[Plan | None, tuple[ContextBlock, ...]]:
    """Apply the round cap, and return the plan to actually run.

    Also the place the streak is CLEARED, which is why it is called on every
    turn and not only on a CLARIFY one. A counter that is only ever
    incremented by the branch it guards never resets, and the third message of
    an unrelated conversation inherits a cap it did nothing to earn.
    """
    if conn is None:
        return plan, ()

    if plan is None or plan.name != "CLARIFY":
        # Any turn that is not a clarification ends the streak, including a
        # fallback. A message that reached CHAT with full tools was answered,
        # whatever shape it had.
        try:
            state.delete(conn, _STREAK_KEY)
        except Exception as e:
            logger.debug(f"clarify streak reset failed: {e}")
        return plan, _blocks(plan)

    try:
        streak = int(state.get(conn, _STREAK_KEY) or 0)
    except (TypeError, ValueError):
        streak = 0

    if streak >= MAX_ROUNDS:
        logger.info(
            f"CLARIFY suppressed after {streak} rounds — answering with what is known."
        )
        try:
            state.delete(conn, _STREAK_KEY)
        except Exception as e:
            logger.debug(f"clarify streak reset failed: {e}")
        # Full capability back, plus the instruction to stop asking.
        return None, (ContextBlock(label="Instruction", content=_STOP_ASKING),)

    try:
        state.set(conn, _STREAK_KEY, streak + 1)
    except Exception as e:
        logger.debug(f"clarify streak write failed: {e}")
    logger.info(f"CLARIFY round {streak + 1} of {MAX_ROUNDS}.")
    return plan, _blocks(plan)


def _blocks(plan: Plan | None) -> tuple[ContextBlock, ...]:
    """A plan's directive as a context block, if it has one.

    A block rather than a prefix on the prompt, so it renders through
    llm/context.py::format_context_block like every other injected fact and
    the model reads it in the shape it reads the clock in. Four renderings of
    the same idea was the thing step 6 removed; this is not going to be the
    fifth.
    """
    if plan is None or not plan.directive:
        return ()
    return (ContextBlock(label="Instruction", content=plan.directive),)
