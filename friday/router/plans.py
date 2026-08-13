"""
router/plans.py
The plan vocabulary: what shape a turn is going to be, decided before it runs.

A PLAN IS DATA, NOT BEHAVIOR. It says which tools are in scope, whether a
write must be preceded by a read, and how many rounds of tool execution it may
spend. It does not run anything, does not call a model, and does not know what
a channel is. agent/turn.py executes it.

    ANSWER             no tools, straight to text
    READ_THEN_ANSWER   one read, then compose
    READ_THEN_WRITE    read -> policy check -> write or card
    WRITE_DIRECT       unambiguous create only
    CLARIFY            missing or ambiguous parameters

THE PLAN NARROWS; THE PROFILE IS THE CEILING.

A plan's tool_scope is intersected with the profile's in llm/assembly.py, and
the hop budget is min()'d with the profile's in agent/turn.py. Neither
direction is symmetric on purpose. `COMPOSE never gets tools` and `CHAT never
sees commit_calendar_event` are properties of the profile table — they are
architecture, and llm/profiles.py says in as many words that config may not
touch them. A plan chosen by a classifier, from a model's one-word answer, is
a much weaker thing than that, and if it could widen scope then the strongest
guarantee in the LLM layer would be one enum value away from being void.

So the plan can only ever subtract. A plan naming a scope the profile does not
carry gets nothing from it, silently and by construction, rather than smuggling
a tool into a prompt that was never supposed to see one.

READ_THEN_WRITE STRUCTURALLY CANNOT SKIP ITS READ.

`write_requires_read` is enforced in the turn loop by write_blocked() below,
before the tool is executed and regardless of what the model asked for. That
is the point of deciding the shape up front: the read is step one of the plan
rather than something the model elects to do.

This demotes tools/preconditions.py from THE mechanism to a backstop. Both
still run. They answer different questions and the difference is not
cosmetic — a precondition asks "has the specific day this call targets been
read?", which is a fact about coverage; the plan gate asks "has this turn read
anything at all before writing?", which is a fact about the shape of the turn.
A model that reads Tuesday and writes to Thursday passes the plan gate and is
caught by the precondition. A model that reads nothing and writes to a day no
precondition covers is caught here. Neither subsumes the other.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Plan:
    """One turn's shape.

    tool_scope          scope tags this plan may use, intersected with the
                        profile's. None means no tools — and unlike the
                        profile's None it is not a guarantee, because the
                        intersection is what enforces the ceiling either way.
    write_requires_read a write in this plan may not run until this turn has
                        recorded a read. Only meaningful when "write" is in
                        scope; harmless and False everywhere else.
    max_tool_hops       rounds of tool execution, min()'d with the profile's.
                        Same meaning as llm/profiles.py: a plan using every
                        hop makes max_tool_hops + 1 model calls.
    directive           one line appended to the prompt as a context block.
                        Declared here and empty on every plan but CLARIFY, so
                        that a plan needing to say something to the model is a
                        table entry rather than a new field and five call
                        sites. Same reason llm/types.py declared tool_calls
                        inert in step 1.
    """
    name: str
    tool_scope: tuple[str, ...] | None
    write_requires_read: bool = False
    max_tool_hops: int = 0
    directive: str = ""


# The table. Hop budgets are sized against what the shape actually needs, not
# rounded up to CHAT's 3: a plan that cannot use a hop should not be able to
# pay for one.
#
#   ANSWER            0 — one dispatch, no tools offered at all.
#   READ_THEN_ANSWER  2 — the read, plus one for a re-read on a bad date.
#   READ_THEN_WRITE   3 — read, write, and slack. CHAT's own budget, because
#                         this is the shape CHAT's 3 was measured against.
#   WRITE_DIRECT      2 — the write, plus one after an invalid_argument.
#   CLARIFY           0 — asking a question needs no tool by definition.
_PLANS: dict[str, Plan] = {
    "ANSWER": Plan(
        name="ANSWER",
        tool_scope=None,
        max_tool_hops=0,
    ),
    "READ_THEN_ANSWER": Plan(
        name="READ_THEN_ANSWER",
        tool_scope=("read",),
        max_tool_hops=2,
    ),
    "READ_THEN_WRITE": Plan(
        name="READ_THEN_WRITE",
        tool_scope=("read", "write"),
        write_requires_read=True,
        max_tool_hops=3,
    ),
    # NOT write_requires_read. That is the whole distinction between this plan
    # and READ_THEN_WRITE, and it is a claim about the REQUEST rather than
    # about safety: "add dentist on the 26th at 3pm" names its own target, so
    # there is nothing a prior read would establish. Anything referring to an
    # event that already exists — moved, cancelled, rescheduled — is
    # READ_THEN_WRITE, because there the identifier is the thing in doubt.
    #
    # The permission gate is unaffected either way. policy/gating.py and
    # add_calendar_event's GATED override are downstream of every plan here;
    # no plan can make a write skip its card.
    "WRITE_DIRECT": Plan(
        name="WRITE_DIRECT",
        tool_scope=("write",),
        max_tool_hops=2,
    ),
    "CLARIFY": Plan(
        name="CLARIFY",
        tool_scope=None,
        max_tool_hops=0,
    ),
}


def get(name: str) -> Plan:
    """Look up a plan by name. Raises on an unknown name.

    Callers that receive a name from a MODEL must not call this — see
    resolve(), which is the one that treats an unknown name as data rather
    than as a bug.
    """
    try:
        return _PLANS[name]
    except KeyError:
        known = ", ".join(sorted(_PLANS))
        raise KeyError(f"Unknown plan {name!r}. Known: {known}") from None


def resolve(name: str | None) -> Plan | None:
    """A plan name from an untrusted source, or None if it is not one.

    None is the fallback and it is not an error state: it means the turn runs
    exactly as it did before this package existed. A classifier that answers
    with prose, with a plan that was renamed, or with nothing at all lands
    here, and Friday is no less capable than it was yesterday.
    """
    if not name:
        return None
    return _PLANS.get(name.strip().upper())


def names() -> tuple[str, ...]:
    """Every plan name, for the classifier's constrained enum. Sorted so the
    enum handed to the model is stable across restarts — an enum whose order
    depends on dict insertion is a prompt that changes when this file is
    edited."""
    return tuple(sorted(_PLANS))


def write_blocked(plan: Plan | None, tool_scope: tuple[str, ...],
                  reads_recorded: int) -> str | None:
    """Whether this call must be refused because the plan's read has not
    happened. Returns the sentence to hand back to the model, or None to run.

    Pure, and takes the read COUNT rather than a Ledger, so the rule can be
    tested without building one and so this module needs no import from
    tools/. The count is what agent/turn.py reads off the ledger it already
    owns.

    Returns a sentence rather than a bool for the same reason
    tools/ledger.py's Precondition protocol does: the model is what will go
    and satisfy it, so the refusal has to say what is missing. A bare False
    reaches the model as a tool error with no instruction in it, and the model
    retries the identical call.
    """
    if plan is None or not plan.write_requires_read:
        return None
    if "write" not in set(tool_scope):
        return None
    if reads_recorded > 0:
        return None
    return (
        "This turn has not read the calendar yet, and this request needs it to "
        "before anything is written. Call get_schedule for the day in question "
        "first, then try this again."
    )


def effective_hops(plan: Plan | None, profile_hops: int) -> int:
    """The hop bound for a turn. The lower of the two, always.

    A plan cannot buy hops the profile did not budget for, for the same reason
    it cannot widen tool scope: the profile's number is architecture and the
    plan's came from a one-word model answer.
    """
    if plan is None:
        return profile_hops
    return min(profile_hops, plan.max_tool_hops)


def effective_scope(plan: Plan | None,
                    profile_scope: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """The tool scope for a turn: the plan's, intersected with the profile's.

    NARROWING ONLY, AND THE ASYMMETRY IS THE WHOLE POINT — see the module
    docstring. Three cases and none of them can widen:

      no plan            the profile's scope, untouched. Today's behavior.
      either is None     None. No tools, and the provider is handed no `tools`
                         argument at all rather than an empty list (see
                         llm/assembly.py::build_tools).
      both are tuples    the intersection, or None when it is empty. An empty
                         tuple would read like "some tools" while meaning
                         none, which llm/types.py::Profile rejects for exactly
                         that reason.

    The empty intersection is not an error. WRITE_DIRECT against a profile
    scoped ("read",) legitimately yields no tools, and the honest outcome is a
    turn that answers without writing — not a turn that writes because the
    plan asked nicely.
    """
    if plan is None:
        return profile_scope
    if plan.tool_scope is None or profile_scope is None:
        return None
    allowed = set(profile_scope)
    narrowed = tuple(s for s in plan.tool_scope if s in allowed)
    return narrowed or None
