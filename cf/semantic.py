"""Phase 12 layer B — semantic candidate discovery, grounded in the source.

Owner ruling 2026-10-08: the next extraction strategy is a **semantic
high-recall candidate-discovery path**, not more regex. *"The semantic path is a
candidate generator, not an authority."* *"Do not allow the model to invent or
paraphrase a constraint with no recoverable source evidence."*

This module is the part of that path with no model in it. It defines what a
semantic candidate must carry, **grounds** each one against the source, and
unions the survivors with the deterministic extractor's output. The model call
itself is a client the field-test harness injects, so `cf` keeps its zero
dependencies and no provider is wired into the package.

Nothing here imports `candidates`. The deterministic extractor is unchanged and
is one of two inputs to `union`.

Grounding is the whole safety property
--------------------------------------
A model asked to enumerate constraints will produce fluent, plausible,
well-structured rules that the document does not contain. The only defence that
survives contact is to require every candidate to quote the source and then
**check the quote** — not to ask the model to be careful.

So `ground` resolves `evidence` to an exact `(start, end)` span in the source
and **rejects** the candidate if it cannot. Three match tiers, each recorded by
name so a report can say how much of the yield leaned on the loose one:

* `exact` — the evidence is a verbatim substring;
* `whitespace` — it matches once whitespace runs are collapsed, which is the
  honest allowance: models reflow line breaks inside a quoted table row or list
  item and that is a rendering difference, not a different claim;
* `rejected` — not locatable. Counted, reported, never stored.

A rejected candidate is not an error to retry. It is the measurement: the
`ungrounded_rate` is how often this path invents, and it belongs in the report
beside precision and recall.

What a candidate must carry
---------------------------
`REQUIRED_FIELDS` is the Owner's list, enforced by `validate` rather than hoped
for: evidence, holder, action kind, action, condition, scope, ambiguity. Absent
*optional* content (`condition`, `scope`) is legitimately empty — the ruling
says "if present" and "if recoverable" — but the **keys** must be there, because
a missing key and an empty one are different failures and only one of them is
the model's.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

#: Action kinds. The Owner's three, plus the honest fourth: a model shown a
#: table row of thresholds will often produce something that is a *constraint on
#: data* rather than an action, and forcing it into "required" would be the
#: paraphrase this path is forbidden to make.
REQUIRED = "required"
PROHIBITED = "prohibited"
CONDITIONAL = "conditional"
DATA = "data"
ACTION_KINDS = (REQUIRED, PROHIBITED, CONDITIONAL, DATA)

#: Keys every returned object must have. Values may be empty for the ones the
#: ruling marks optional; the key may not be absent.
REQUIRED_FIELDS = ("evidence", "holder", "action_kind", "action", "condition",
                   "scope", "ambiguous")

MATCH_EXACT = "exact"
MATCH_WHITESPACE = "whitespace"
MATCH_REJECTED = "rejected"

#: Evidence shorter than this is not evidence. A model that quotes `must` has
#: not grounded anything, and a 4-character span matches dozens of places.
MIN_EVIDENCE_CHARS = 12

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class SemanticCandidate:
    """One model-proposed constraint, after grounding."""

    skill_id: str
    source_file: str
    #: Verbatim source text at the resolved span — the SOURCE's bytes, not the
    #: model's. A candidate carries what the document says, not what the model
    #: typed, so a whitespace-tier match still stores the real span text.
    source_text: str
    source_span: tuple[int, int]
    #: What the model quoted, kept for audit even when it differs in whitespace.
    quoted_evidence: str
    match_tier: str
    holder: str
    action_kind: str
    action: str
    condition: str = ""
    scope: str = ""
    ambiguous: bool = False
    confidence: str = ""
    #: The structural unit the model was shown this from, as it claimed.
    claimed_unit: str = ""
    #: True when the resolved span does not fall inside `claimed_unit`'s span.
    unit_mismatch: bool = False

    @property
    def candidate_id(self) -> str:
        """A deterministic id that distinguishes two rules sharing one span.

        Keyed on the span **and** the asserted rule, not the span alone. One
        source span legitimately carries two constraints — a list item stating
        an obligation and a bound, say — and `union` is documented to keep both.
        With the span alone they collided: two distinct findings got one id, and
        the item fold silently merged them, which `score_union.fold_to_items`
        caught as 293 items against the union's 294.

        Including the rule also makes a genuine duplicate — same span, same
        action — collapse to one id, which is the behaviour wanted there.
        """
        key = (f"{self.skill_id}\x00{self.source_file}\x00{self.source_span}"
               f"\x00{self.action_kind}\x00{self.action}")
        return "sem-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "origin": "semantic",
                "skill_id": self.skill_id, "source_file": self.source_file,
                "source_span": list(self.source_span),
                "source_text": self.source_text,
                "quoted_evidence": self.quoted_evidence,
                "match_tier": self.match_tier,
                "holder": self.holder, "action_kind": self.action_kind,
                "action": self.action, "condition": self.condition,
                "scope": self.scope, "ambiguous": self.ambiguous,
                "confidence": self.confidence,
                "claimed_unit": self.claimed_unit,
                "unit_mismatch": self.unit_mismatch}


@dataclass
class Grounding:
    """What grounding did to one skill's worth of model output."""

    accepted: list[SemanticCandidate] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        tiers = {MATCH_EXACT: 0, MATCH_WHITESPACE: 0}
        for c in self.accepted:
            tiers[c.match_tier] = tiers.get(c.match_tier, 0) + 1
        reasons: dict[str, int] = {}
        for r in self.rejected:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        total = len(self.accepted) + len(self.rejected)
        return {"proposed": total, "accepted": len(self.accepted),
                "rejected": len(self.rejected),
                "ungrounded_rate": (round(100.0 * len(self.rejected) / total, 1)
                                    if total else 0.0),
                "by_tier": tiers, "by_reason": reasons}


def validate(obj: object) -> str:
    """`""` if *obj* is a usable candidate object, else why not. Pure."""
    if not isinstance(obj, dict):
        return f"not an object: {type(obj).__name__}"
    missing = [f for f in REQUIRED_FIELDS if f not in obj]
    if missing:
        return f"missing required field(s): {missing}"
    ev = obj.get("evidence")
    if not isinstance(ev, str) or not ev.strip():
        return "evidence is empty or not a string"
    if len(ev.strip()) < MIN_EVIDENCE_CHARS:
        return (f"evidence is {len(ev.strip())} characters, under the "
                f"{MIN_EVIDENCE_CHARS}-character floor")
    kind = obj.get("action_kind")
    if kind not in ACTION_KINDS:
        return f"action_kind {kind!r} is not one of {list(ACTION_KINDS)}"
    if not isinstance(obj.get("action"), str) or not obj["action"].strip():
        return "action is empty"
    if not isinstance(obj.get("ambiguous"), bool):
        return f"ambiguous must be a boolean, got {obj.get('ambiguous')!r}"
    return ""


def _find_whitespace_insensitive(source: str, needle: str
                                 ) -> "tuple[int, int] | None":
    """Span in *source* matching *needle* up to whitespace runs, or None.

    Built by walking the source once and comparing collapsed forms, rather than
    by regex-escaping the needle — the needle is model output and can contain
    anything, and a `re` pattern built from it is both slower and a way to hand
    control of the matcher to the model.
    """
    target = _WS.sub(" ", needle).strip()
    if not target:
        return None
    # Map each non-space character of the collapsed source back to its offset.
    collapsed: list[str] = []
    offsets: list[int] = []
    prev_space = True
    for i, ch in enumerate(source):
        if ch.isspace():
            if not prev_space:
                collapsed.append(" ")
                offsets.append(i)
            prev_space = True
        else:
            collapsed.append(ch)
            offsets.append(i)
            prev_space = False
    hay = "".join(collapsed)
    at = hay.find(target)
    if at == -1:
        return None
    start = offsets[at]
    last = offsets[at + len(target) - 1]
    return start, last + 1


def ground(raw: object, *, source: str, skill_id: str, source_file: str,
           unit_spans: "dict[str, tuple[int, int]] | None" = None
           ) -> Grounding:
    """Validate and locate every proposed candidate in *raw*.

    *raw* is whatever the client returned — a list of objects, or a dict with a
    `constraints` list. Anything else is rejected wholesale rather than coerced,
    because coercing malformed model output is how a parse error becomes a
    silent zero-yield skill.
    """
    out = Grounding()
    items: list = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("constraints", "candidates", "items"):
            if isinstance(raw.get(key), list):
                items = raw[key]
                break
        else:
            out.rejected.append({"reason": "payload_shape",
                                 "detail": f"dict without a list under "
                                           f"constraints/candidates/items: "
                                           f"{sorted(raw)[:6]}"})
            return out
    else:
        out.rejected.append({"reason": "payload_shape",
                             "detail": f"not a list or object: "
                                       f"{type(raw).__name__}"})
        return out

    for obj in items:
        why = validate(obj)
        if why:
            out.rejected.append({"reason": "invalid_object", "detail": why,
                                 "object": _trim(obj)})
            continue
        ev = obj["evidence"]
        span: "tuple[int, int] | None" = None
        tier = MATCH_REJECTED
        at = source.find(ev)
        if at != -1:
            span, tier = (at, at + len(ev)), MATCH_EXACT
        else:
            loose = _find_whitespace_insensitive(source, ev)
            if loose:
                span, tier = loose, MATCH_WHITESPACE
        if span is None:
            out.rejected.append({"reason": "ungrounded_evidence",
                                 "detail": "evidence is not in the source, "
                                           "even ignoring whitespace",
                                 "object": _trim(obj)})
            continue

        claimed = str(obj.get("unit") or obj.get("unit_id") or "")
        mismatch = False
        if claimed and unit_spans and claimed in unit_spans:
            lo, hi = unit_spans[claimed]
            mismatch = not (lo <= span[0] and span[1] <= hi)

        out.accepted.append(SemanticCandidate(
            skill_id=skill_id, source_file=source_file,
            source_text=source[span[0]:span[1]], source_span=span,
            quoted_evidence=ev, match_tier=tier,
            holder=str(obj.get("holder") or ""),
            action_kind=obj["action_kind"], action=str(obj["action"]),
            condition=str(obj.get("condition") or ""),
            scope=str(obj.get("scope") or ""),
            ambiguous=bool(obj["ambiguous"]),
            confidence=str(obj.get("confidence") or ""),
            claimed_unit=claimed, unit_mismatch=mismatch))
    return out


def _trim(obj: object, limit: int = 300) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return repr(obj)[:limit]


# ---------------------------------------------------------------------------
# UNION
# ---------------------------------------------------------------------------

@dataclass
class UnionResult:
    """The merged candidate set, plus what the merge did."""

    #: One entry per review-queue item. `origins` names where it came from.
    items: list[dict] = field(default_factory=list)

    @property
    def deterministic_ids(self) -> set[str]:
        return {i["deterministic_id"] for i in self.items
                if i.get("deterministic_id")}

    def counts(self) -> dict:
        both = sum(1 for i in self.items if len(i["origins"]) == 2)
        det = sum(1 for i in self.items if i["origins"] == ["deterministic"])
        sem = sum(1 for i in self.items if i["origins"] == ["semantic"])
        return {"items": len(self.items), "deterministic_only": det,
                "semantic_only": sem, "both": both}


class SuppressionError(AssertionError):
    """The union lost a deterministic candidate. Always fatal."""


def union(deterministic: list, semantic: list[SemanticCandidate]
          ) -> UnionResult:
    """Merge the two paths. **Every deterministic candidate survives.**

    Owner ruling: *"The semantic path must not silently suppress an existing
    deterministic candidate."* So the merge is additive by construction — it
    starts from the full deterministic list and never removes from it — and
    `assert_no_suppression` checks the result anyway, because "by construction"
    is a claim about code I wrote and the check is a claim about the output.

    A semantic candidate whose span **overlaps** a deterministic one is attached
    to that item rather than added beside it, so the review queue shows one
    thing to confirm instead of two. Overlap, not equality: the two paths work
    at different granularities — the deterministic path emits a whole list item,
    the semantic path may quote the clause inside it — and treating those as two
    separate findings would inflate the queue with duplicates while claiming to
    have found more.

    Attachment is one-to-many on purpose: several semantic candidates can land
    inside one deterministic span when a list item carries two rules. Each is
    kept, under `semantic`, so the extra rules are not lost to the collapse.
    """
    items: list[dict] = []
    for d in deterministic:
        lo, hi = d.source_span
        items.append({
            "origins": ["deterministic"],
            "deterministic_id": d.candidate_id,
            "skill_id": d.skill_id, "source_file": d.source_file,
            "source_span": [lo, hi], "source_text": d.source_text,
            "tier": d.tier, "extraction_reason": d.extraction_reason,
            "semantic": [],
        })

    index: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        index.setdefault((it["skill_id"], it["source_file"]), []).append(it)

    for s in semantic:
        lo, hi = s.source_span
        host = None
        for it in index.get((s.skill_id, s.source_file), ()):
            dlo, dhi = it["source_span"]
            if lo < dhi and dlo < hi:
                host = it
                break
        if host is None:
            items.append({
                "origins": ["semantic"], "deterministic_id": "",
                "skill_id": s.skill_id, "source_file": s.source_file,
                "source_span": [lo, hi], "source_text": s.source_text,
                "tier": "", "extraction_reason": "",
                "semantic": [s.as_dict()],
            })
        else:
            if "semantic" not in host["origins"]:
                host["origins"] = ["deterministic", "semantic"]
            host["semantic"].append(s.as_dict())

    items.sort(key=lambda i: (i["skill_id"], i["source_file"],
                              i["source_span"]))
    return UnionResult(items=items)


def assert_no_suppression(result: UnionResult, deterministic: list) -> None:
    """Raise unless every deterministic candidate is still present, by id."""
    want = {d.candidate_id for d in deterministic}
    got = result.deterministic_ids
    lost = sorted(want - got)
    if lost:
        raise SuppressionError(
            f"the union lost {len(lost)} deterministic candidate(s): "
            f"{lost[:5]}. The semantic path is a generator and may not suppress "
            "a deterministic finding.")
    if len(got) != len(want):
        raise SuppressionError(
            f"deterministic ids collapsed: {len(want)} in, {len(got)} out")
