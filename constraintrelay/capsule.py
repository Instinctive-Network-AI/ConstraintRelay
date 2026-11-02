"""The v1 capsule — v0.7 §4, and the managed mirror of §3.

§4 is a short section and the whole of it is a prohibition:

> The v1 capsule contains **all constraints applicable under the declared
> build/session context**. There is no semantic minimization in v1. No
> optimization objective is used to choose among applicable constraints.

So this module has no scoring, no ranking, no top-k, no dedup-by-similarity and
no budget-aware selection. The pipeline is `registered → declared applicable →
capsule`, and the only decision in it belongs to `applicability.decide`.

That prohibition is load-bearing rather than tidy. A capsule that drops a
constraint because it would not fit is the §6 failure — *exceeds the verified
delivery path capacity* — reported as a success. The capsule is built at full
size and the receipt refuses; the capsule never shrinks to make the receipt pass.

Why list items
--------------
Each constraint renders as a markdown list item. This is not cosmetic: STATUS.md
finding 10, replayed against the real OpenClaw loader, is that an over-cap
`AGENTS.md` is rebuilt as head + policy digest + tail, and the digest admits
headings, list items and lines matching a keyword set — bare prose in the dropped
middle is lost. Identical sentence, identical position: `- ` delivered, prose not.
So the rendering that survives the most harnesses is the one that is already a
list item, and the canonical text stays a contiguous substring of the line, which
is what the verdict layer tests containment against.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .applicability import CLAIM, Context, Decision, decide
from .store import StoredConstraint, Store

#: Managed-region delimiters. The digest is in the begin marker so that drift is
#: detectable from the file alone, without re-reading the store — which is what
#: makes a mirror check cheap enough to run in a pre-commit hook.
BEGIN = "<!-- constraintflow:capsule begin v=1 digest={digest} -->"
END = "<!-- constraintflow:capsule end -->"

_BEGIN_RE = re.compile(
    r"^<!--[ \t]*constraintflow:capsule[ \t]+begin[ \t]+v=1[ \t]+"
    r"digest=(?P<digest>[0-9a-f]{64})[ \t]*-->[ \t]*$", re.MULTILINE)
_END_RE = re.compile(r"^<!--[ \t]*constraintflow:capsule[ \t]+end[ \t]*-->[ \t]*$",
                     re.MULTILINE)

HEADING = "## Constraints (ConstraintRelay capsule)"


class MirrorError(Exception):
    """Drift, or a malformed managed region. §3: manual drift is an error."""


@dataclass(frozen=True)
class Member:
    constraint: StoredConstraint
    decision: Decision


@dataclass
class Capsule:
    """All constraints applicable under one declared context. No minimization."""

    members: list[Member] = field(default_factory=list)
    excluded: list[Member] = field(default_factory=list)
    context: Context = field(default_factory=Context)

    @property
    def ids(self) -> list[str]:
        return [m.constraint.id for m in self.members]

    @property
    def body(self) -> str:
        """The capsule's instruction text, without the managed-region markers."""
        if not self.members:
            return ""
        lines = [HEADING, ""]
        for m in self.members:
            lines.append(f"- [{m.constraint.id}] {m.constraint.canonical_text}")
        return "\n".join(lines) + "\n"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def region(self) -> str:
        """The managed region as it appears in a mirrored file."""
        if not self.members:
            return ""
        return (BEGIN.format(digest=self.digest) + "\n" + self.body + END + "\n")

    def size(self, unit: str) -> int:
        from .registry import measure_units
        return measure_units(self.body, unit)

    def as_dict(self) -> dict:
        return {
            "claim": CLAIM,
            "minimization": "none — v0.7 §4 forbids it in v1",
            "context": self.context.describe(),
            "digest_sha256": self.digest,
            "members": [{"id": m.constraint.id,
                         "constraint_version": m.constraint.version,
                         "scope": m.constraint.scope.describe(),
                         "why": m.decision.reason,
                         "machine_enforcement_declared":
                             m.constraint.machine_enforcement_declared}
                        for m in self.members],
            "excluded": [{"id": m.constraint.id, "why": m.decision.reason}
                         for m in self.excluded],
        }


def build(store: Store, context: Context) -> Capsule:
    """§4's pipeline. Order is by id, so the digest is reproducible.

    Retired constraints are not members. That is not minimization: a retired
    constraint is not applicable to anything, because §3's `status` is part of the
    record and `retired` is the author saying so.
    """
    capsule = Capsule(context=context)
    for c in sorted(store.constraints, key=lambda c: c.id):
        if not c.active:
            capsule.excluded.append(Member(c, Decision(
                False, f"status={c.status}: withdrawn by the author in the store")))
            continue
        d = decide(c.scope, context)
        (capsule.members if d.applicable else capsule.excluded).append(Member(c, d))
    return capsule


# ---------------------------------------------------------------------------
# §3 managed mirror
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MirrorState:
    """What a mirrored file currently holds, relative to the store."""

    present: bool
    matches: bool
    recorded_digest: str = ""
    expected_digest: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Region:
    """Offsets of a managed region in a file, and the body it currently holds."""

    start: int          # offset of the begin marker
    end: int            # just past the end marker's newline — safe to splice
    body: str           # exactly the text the digest covers
    recorded_digest: str


def find_region(source: str) -> Region | None:
    """Locate the managed region, or None. Raises `MirrorError` if malformed."""
    begin = _BEGIN_RE.search(source)
    if begin is None:
        if _END_RE.search(source) is not None:
            raise MirrorError(
                "the file has a `constraintflow:capsule end` marker with no begin "
                "marker; a half-open managed region cannot be verified or replaced")
        return None
    end = _END_RE.search(source, begin.end())
    if end is None:
        raise MirrorError(
            "the managed capsule region opens but never closes; refusing to guess "
            "where it ends, because guessing would overwrite authored text")
    second = _BEGIN_RE.search(source, begin.end())
    if second is not None and second.start() < end.start():
        raise MirrorError("nested managed capsule regions are invalid")
    # The begin marker is followed by exactly one newline, then the body, then the
    # end marker on its own line. Taking the body from the match offsets rather
    # than by splitting on markers means a body that happens to contain a similar
    # comment line cannot shift the boundary.
    body_start = begin.end() + 1 if source[begin.end():begin.end() + 1] == "\n" \
        else begin.end()
    stop = end.end()
    if stop < len(source) and source[stop] == "\n":
        stop += 1
    return Region(begin.start(), stop, source[body_start:end.start()],
                  begin.group("digest"))


def check_mirror(capsule: Capsule, source: str) -> MirrorState:
    """§3: does the mirror match the canonical store text?

    The comparison is on the rendered body, not only on the recorded digest. A
    hand-edited body with the old digest left in place and a hand-edited digest
    over an untouched body are both drift, and checking one of the two would miss
    half of it.
    """
    found = find_region(source)
    if found is None:
        return MirrorState(False, False, expected_digest=capsule.digest,
                           detail="no managed capsule region in this file")
    recorded = found.recorded_digest
    actual = hashlib.sha256(found.body.encode("utf-8")).hexdigest()
    if actual != recorded:
        return MirrorState(
            True, False, recorded, capsule.digest,
            detail=("the managed region's text does not match the digest recorded "
                    "in its own begin marker — the region was edited by hand"))
    if recorded != capsule.digest:
        return MirrorState(
            True, False, recorded, capsule.digest,
            detail=("the managed region is internally consistent but does not "
                    "match the store; regenerate it rather than editing it"))
    return MirrorState(True, True, recorded, capsule.digest,
                       detail="mirror matches the canonical store text")


def write_region(capsule: Capsule, source: str) -> str:
    """Return *source* with the managed region replaced or appended."""
    found = find_region(source)
    region = capsule.region()
    if found is None:
        sep = "" if source.endswith("\n\n") else ("\n" if source.endswith("\n") else "\n\n")
        return source + sep + region
    return source[:found.start] + region + source[found.end:]
