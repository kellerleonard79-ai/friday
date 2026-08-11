"""
tools/registry.py
What tools exist, what they accept, and which profiles may see them.

THE SCHEMA IS DERIVED FROM THE TYPED SIGNATURE. Parameter types come from
annotations, per-parameter descriptions from Annotated metadata, requiredness
from the absence of a default. Nothing about a tool's arguments is written
twice, so nothing about them can disagree with itself.

The docstring says WHEN to call the tool, and nothing else. This is structural,
not stylistic. In Phase II docstrings accumulated persona, argument semantics
and behavior rules until they were a meaningful fraction of every prompt — and
because they read like documentation, nobody counted them. Registration warns
above _MAX_DOCSTRING chars. If a tool needs more explanation than that, the
explanation belongs in AGENTS.md under TOOL_POLICY where it is visible as
persona and paid for once, not per tool.

Provider-neutral by construction: this module emits plain JSON-Schema dicts.
Turning one into types.FunctionDeclaration is llm/providers/gemini.py's job,
because gemini.py is the only file in the repository allowed to import an SDK
(CLAUDE.md rule 11). A registry that emitted SDK objects would quietly make
every tool Gemini-only.
"""

from __future__ import annotations

import inspect
import logging
import typing
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, get_args, get_origin, get_type_hints

from tools.types import ToolOutcome

logger = logging.getLogger("friday.tools.registry")

# Above this, the docstring is doing a job that belongs to the persona. A
# warning rather than an error: a long docstring is a cost regression, not a
# broken tool, and a daemon that refuses to boot over prose is worse than one
# that says so in the log.
_MAX_DOCSTRING = 200

# What a tool does to the world. The executor branches on it: a read declares
# its own ledger records and is believed, a write declares nothing and has its
# record synthesised from the service's confirmation. See tools/executor.py.
#
# The step-3 _ALLOWED_EFFECTS guard that rejected effect="write" at
# registration is gone, per its own instruction — writes have landed.
EffectClass = Literal["read", "write"]

_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    json_type: str
    description: str
    required: bool


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One registered tool. Everything the loop and the provider need."""
    name: str
    description: str
    when_to_call: str
    fn: Callable[..., ToolOutcome]
    parameters: tuple[Parameter, ...]
    scope: tuple[str, ...]
    effect: EffectClass
    preconditions: tuple[Any, ...]
    timeout_s: float

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters if p.required)

    def json_schema(self) -> dict[str, Any]:
        """The provider-neutral declaration. One dict, no SDK types."""
        return {
            "name": self.name,
            "description": (
                f"{self.description}\n{self.when_to_call}".strip()
                if self.when_to_call else self.description
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    p.name: {"type": p.json_type, "description": p.description}
                    for p in self.parameters
                },
                "required": list(self.required),
            },
        }


_registry: dict[str, ToolSpec] = {}


def _describe(annotation: Any) -> tuple[Any, str]:
    """(base type, description) — unwrapping Annotated[T, "..."].

    Annotated is where a parameter description belongs because it sits on the
    parameter itself. A description in the docstring drifts from its argument
    the first time an argument is renamed, and nothing catches it.
    """
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        base = args[0]
        desc = next((a for a in args[1:] if isinstance(a, str)), "")
        return base, desc
    return annotation, ""


def _json_type(base: Any, tool_name: str, param: str) -> str:
    origin = get_origin(base)
    if origin in (list, tuple):
        return "array"
    # Optional[T] / T | None — the declared type is T; optionality is carried
    # by the default, which is what decides `required`.
    if origin is typing.Union or str(origin) == "types.UnionType":
        inner = [a for a in get_args(base) if a is not type(None)]
        if len(inner) == 1:
            return _json_type(inner[0], tool_name, param)
    try:
        return _JSON_TYPES[base]
    except (KeyError, TypeError):
        raise TypeError(
            f"Tool {tool_name!r}: parameter {param!r} has type {base!r}, which "
            f"does not map to a JSON schema type. Supported: "
            f"{', '.join(sorted(set(_JSON_TYPES.values())))}, array."
        ) from None


def _build_parameters(fn: Callable, name: str) -> tuple[Parameter, ...]:
    sig = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)

    params: list[Parameter] = []
    for pname, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise TypeError(
                f"Tool {name!r}: *args/**kwargs cannot be described to a model. "
                f"Declare the parameters."
            )
        if pname not in hints:
            # A registration-time error, not a runtime one. An unannotated
            # parameter has no schema entry, so the model never learns it
            # exists and simply never passes it — a tool that quietly always
            # runs on defaults, which is the kind of bug that survives for
            # months because nothing fails.
            raise TypeError(
                f"Tool {name!r}: parameter {pname!r} has no type annotation. "
                f"The JSON schema is derived from the signature, so an "
                f"unannotated parameter is invisible to the model."
            )
        base, desc = _describe(hints[pname])
        params.append(Parameter(
            name=pname,
            json_type=_json_type(base, name, pname),
            description=desc,
            # Required is exactly "has no default". This is the definition the
            # missing-parameter gate checks against, and it is derived rather
            # than declared so the two cannot drift.
            required=p.default is inspect.Parameter.empty,
        ))
    return tuple(params)


def tool(*, name: str, description: str, scope: tuple[str, ...],
         effect: EffectClass = "read", preconditions: tuple[Any, ...] = (),
         timeout_s: float = 30.0):
    """Register a function as a tool.

    description — one sentence, what the tool returns. Goes to the model.
    scope       — the tags a profile's tool_scope must intersect to see it.
    effect      — "read" or "write". Decides the executor's ledger rule.
    preconditions — facts that must already be in the turn's ledger.
    timeout_s   — per-call ceiling, still bounded by the turn's shared deadline.
    """
    def decorate(fn: Callable[..., ToolOutcome]) -> Callable[..., ToolOutcome]:
        if not scope:
            raise ValueError(f"Tool {name!r} has an empty scope and no profile could see it.")
        if name in _registry:
            raise ValueError(f"Tool {name!r} is already registered.")

        when = inspect.cleandoc(fn.__doc__ or "")
        if len(when) > _MAX_DOCSTRING:
            logger.warning(
                f"Tool {name!r}: docstring is {len(when)} chars (limit "
                f"{_MAX_DOCSTRING}). The docstring says WHEN to call the tool; "
                f"argument semantics belong in Annotated metadata and behavior "
                f"rules belong in AGENTS.md TOOL_POLICY. This ships in every "
                f"prompt the tool is offered to."
            )

        _registry[name] = ToolSpec(
            name=name,
            description=description,
            when_to_call=when,
            fn=fn,
            parameters=_build_parameters(fn, name),
            scope=tuple(scope),
            effect=effect,
            preconditions=tuple(preconditions),
            timeout_s=timeout_s,
        )
        logger.debug(f"Registered tool {name!r} scope={scope} effect={effect}")
        return fn
    return decorate


def get(name: str) -> ToolSpec:
    """Look up a tool. Raises on an unknown name.

    The model can and does hallucinate a tool name. The turn loop catches that
    before it gets here and returns a ToolError, because a hallucinated name is
    something the model can recover from; reaching this raise means Friday's own
    code asked for a tool that does not exist, which is a bug.
    """
    try:
        return _registry[name]
    except KeyError:
        known = ", ".join(sorted(_registry)) or "<none registered>"
        raise KeyError(f"Unknown tool {name!r}. Registered: {known}") from None


def has(name: str) -> bool:
    return name in _registry


def for_scope(scope: tuple[str, ...] | None) -> tuple[ToolSpec, ...]:
    """Every tool visible to a profile with this tool_scope.

    None means no tools — not "no filter". That distinction is the whole
    guarantee: a profile that never opted in cannot be handed tools by a
    caller, a config edit, or a future refactor that forgets to pass a scope.
    """
    if not scope:
        return ()
    wanted = set(scope)
    return tuple(
        spec for name, spec in sorted(_registry.items())
        if wanted & set(spec.scope)
    )


def schemas_for_scope(scope: tuple[str, ...] | None) -> tuple[dict[str, Any], ...]:
    """The JSON declarations a provider should offer for this scope."""
    return tuple(spec.json_schema() for spec in for_scope(scope))


def names() -> tuple[str, ...]:
    return tuple(sorted(_registry))
