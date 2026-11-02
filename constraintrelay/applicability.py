"""Applicability — v0.7 §2 and §15.

Author-declared and deterministic. Nothing here infers applicability from the
text of a constraint, and nothing here consults a runtime fact.

The four rules, implemented literally:

1. no scope metadata            -> globally applicable
2. a required dimension unknown -> applicable
3. only static dimensions exist in v1
4. excluded only when the declared predicate is **definitively false**

Which means exclusion needs three things at once: the constraint declares the
dimension, the build context supplies a value for it, and that value is not in
the declared set. Any gap in that chain and the constraint stays in. §2: *"The
safe default is over-inclusion, not under-inclusion."*

Why the rejected-dimension list is enforced rather than documented
-----------------------------------------------------------------
§15 names seven dimensions v1 supports and eight it does not. The eight all need
per-action runtime facts (§1), so a store that declares one of them would be
silently ignored by a build-time tool — and a scope that is silently ignored is
the under-inclusion §2 forbids, arriving as a configuration the author believes
is in force. So an unsupported dimension is a configuration error at load time,
named, with the version that may support it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: §15's supported static dimensions. The spelling is the store's key.
SUPPORTED = (
    "agent",                # agent identity
    "harness_target",       # the declared target profile / target name
    "working_directory",    # path relative to the project root
    "enabled_skill",        # a member of the enabled skill set
    "tool_available",       # declared tool availability
    "session_mode",         # session-level mode
    "project",              # project / subproject
)

#: §15's "do not include yet". Rejected by name so the message can say why.
DEFERRED = {
    "data_classification": "needs the current data's classification (§1 runtime)",
    "runtime_provenance": "needs the current data's provenance (§1 runtime)",
    "destination": "needs the current destination (§1 runtime)",
    "action_arguments": "needs the current generated argument (§1 runtime)",
    "user_intent": "needs the current user intent (§1 runtime)",
    "transaction_semantics": "needs the current transaction amount/semantics (§1 runtime)",
    "delegation_state": "needs dynamic delegation state (§1 runtime)",
}

#: The phrase §2.5 permits. Kept as a constant so the renderers cannot drift into
#: the stronger claim, and so a test can assert the stronger one never appears.
CLAIM = "applicable under declared scope"
FORBIDDEN_CLAIM = "all applicable constraints"


class ScopeError(Exception):
    """An invalid scope declaration. A configuration error, so exit 1."""


@dataclass(frozen=True)
class Scope:
    """One constraint's declared applicability.

    `dimensions` maps a supported dimension to the set of values under which the
    constraint applies. An empty mapping is §2.1's globally-applicable case.
    """

    dimensions: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def global_(self) -> bool:
        return not self.dimensions

    def describe(self) -> str:
        if self.global_:
            return "global (no scope declared)"
        return "; ".join(f"{k}={'|'.join(v)}"
                         for k, v in sorted(self.dimensions.items()))


@dataclass(frozen=True)
class Context:
    """The build/session context, §1's list of what v1 is allowed to know.

    A dimension absent from `declared` is **unknown**, which §2.2 resolves to
    applicable. That is the whole reason this is a mapping with absences rather
    than a record with defaults: a default would quietly answer a question
    nobody asked, and the answer would be used to remove a constraint.
    """

    declared: dict[str, str] = field(default_factory=dict)

    def value(self, dimension: str) -> str | None:
        v = self.declared.get(dimension)
        return v if v not in (None, "") else None

    def describe(self) -> str:
        if not self.declared:
            return "nothing declared — every scoped constraint stays applicable (§2.2)"
        return "; ".join(f"{k}={v}" for k, v in sorted(self.declared.items()))


@dataclass(frozen=True)
class Decision:
    """Why one constraint is in or out of a capsule. Always recorded."""

    applicable: bool
    reason: str
    #: Dimensions that were declared on the constraint but unknown in the
    #: context, so §2.2 kept the constraint in. Surfaced because an author who
    #: expected an exclusion needs to see that their context never said so.
    unknown: tuple[str, ...] = ()


def parse_scope(raw: object, where: str) -> Scope:
    """Validate a `[constraint.scope]` table.

    Values may be a string or a list of strings; both become a tuple, because
    `agent = "x"` and `agent = ["x"]` mean the same thing to an author and
    differing on that would be a trap rather than a rule.
    """
    if raw is None:
        return Scope({})
    if not isinstance(raw, dict):
        raise ScopeError(f"{where}: `scope` must be a table")
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key in DEFERRED:
            raise ScopeError(
                f"{where}: scope dimension {key!r} is not supported in v1 — "
                f"{DEFERRED[key]}. §15 defers it to the runtime applicability "
                f"layer. Remove it rather than leaving it unenforced.")
        if key not in SUPPORTED:
            raise ScopeError(
                f"{where}: unknown scope dimension {key!r}; v1 supports: "
                + ", ".join(SUPPORTED))
        if isinstance(value, str):
            values: tuple[str, ...] = (value,)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            values = tuple(value)
        else:
            raise ScopeError(
                f"{where}: scope `{key}` must be a string or a list of strings")
        if not values or any(not v.strip() for v in values):
            raise ScopeError(f"{where}: scope `{key}` has an empty value; an "
                             f"empty predicate is never definitively false, so "
                             f"it would exclude nothing while reading as a limit")
        out[key] = values
    return Scope(out)


def decide(scope: Scope, context: Context) -> Decision:
    """§2's applicability decision for one constraint under one context."""
    if scope.global_:
        return Decision(True, "no scope declared, so globally applicable (§2.1)")

    unknown: list[str] = []
    for dimension, allowed in sorted(scope.dimensions.items()):
        observed = context.value(dimension)
        if observed is None:
            unknown.append(dimension)
            continue
        if observed not in allowed:
            # The one and only exit: declared, observed, and not a member.
            return Decision(
                False,
                f"excluded: declared {dimension}={'|'.join(allowed)} and the "
                f"context declares {dimension}={observed}, so the predicate is "
                f"definitively false (§2.4)",
                tuple(unknown))
    if unknown:
        return Decision(
            True,
            f"applicable: {', '.join(unknown)} required for exclusion but not "
            f"declared in this context, so §2.2 keeps the constraint in",
            tuple(unknown))
    return Decision(True, "applicable: every declared dimension matches")
