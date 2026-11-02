"""Phase 13 — `DISCOVERED`, the primary recall rule, per the frozen contract.

`fieldtest/phase13/MATCHING_CONTRACT.md` is the authority; this module is its
implementation and nothing here may be read as amending it.

Owner ruling 2026-10-08: *"A gold constraint counts as discovered only when a
candidate has an exact grounded evidence span in a containment relationship with
the gold anchor within the same structural unit. Arbitrary overlap is
insufficient."*

Why this is a new module rather than an edit
--------------------------------------------
`fieldtest/phase7/score.py` holds the matching rules for Phases 7–12 and is
frozen evidence: every figure in five phase reports must stay reproducible from
it. Its `lenient` is **arbitrary overlap** — one character of intersection —
which is precisely what the ruling rejects, and its `strict` is one-directional
containment with no unit condition. So neither could be promoted, and editing
either would have moved the recorded history.

The four clauses
----------------
1. same file;
2. **exact** grounded evidence — a whitespace-tier semantic match does not
   qualify; a deterministic span qualifies by construction, because it is a
   slice of the source taken by the segmenter rather than a quote that could
   differ from it (contract §3 marks this as an interpretation);
3. **containment in either direction** — `c ⊇ g` or `g ⊇ c`;
4. both spans inside **one** `cf.structure` unit.

Clause 3 is bidirectional because the two paths sit on opposite sides of the
anchor: the deterministic extractor emits a block that contains it, the semantic
path quotes the clause inside it. Clause 4 is what keeps the rule from
collapsing into `lenient`: it forbids a match that straddles a unit boundary.

Clause 4 needs the document, not just the spans, so `UnitIndex` is built once
per file and reused. Built from `structure.parse`, whose losslessness is
verified by the caller before use — a parse that dropped bytes would move every
unit boundary after the drop and silently change which matches qualify.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from .structure import Document, parse, verify_lossless

#: The three rules, reported together. `DISCOVERED` decides; the other two are
#: diagnostics the contract retains for named reasons.
DISCOVERED = "discovered"
STRICT = "strict"
LENIENT = "lenient"
RULES = (DISCOVERED, STRICT, LENIENT)

#: Semantic match tiers that satisfy clause 2. Mirrors
#: `semantic.MATCH_EXACT`, restated rather than imported so this module does not
#: depend on the model path at all — `matching` is used by the deterministic
#: path too, and a one-way dependency is easier to reason about than a cycle.
EXACT_TIERS = frozenset({"exact", ""})


class UnitIndex:
    """Structural units of one file, queryable by span.

    `unit_containing(lo, hi)` is the only question asked of it. A sorted
    start-offset list plus a binary search, because the two retired strata are
    18 skills but the corpus is 212 files and a linear scan per candidate per
    gold row is the kind of quadratic that turns a measurement into a coffee
    break.
    """

    __slots__ = ("starts", "spans")

    def __init__(self, doc: Document) -> None:
        # Blanks are excluded: they carry no candidate and no anchor, and
        # including them would let a match "share a unit" that is whitespace.
        units = [(u.start, u.end) for u in doc.signal_units]
        units.sort()
        self.starts = [s for s, _ in units]
        self.spans = units

    @classmethod
    def of(cls, source: str) -> "UnitIndex":
        doc = parse(source)
        verify_lossless(doc)
        return cls(doc)

    def unit_containing(self, lo: int, hi: int) -> "tuple[int, int] | None":
        """The unit fully containing `[lo, hi)`, or None.

        Units tile the source and do not nest, so at most one can contain a
        span: the candidate is the last unit starting at or before `lo`.
        """
        i = bisect_right(self.starts, lo) - 1
        if i < 0:
            return None
        s, e = self.spans[i]
        return (s, e) if s <= lo and hi <= e else None

    def same_unit(self, a: tuple, b: tuple) -> bool:
        ua = self.unit_containing(a[0], a[1])
        return ua is not None and ua == self.unit_containing(b[0], b[1])


def contains(outer: tuple, inner: tuple) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def containment_relation(a: tuple, b: tuple) -> bool:
    """Either direction. Contract §2 clause 3."""
    return contains(a, b) or contains(b, a)


def overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def exact_evidence(tier: str) -> bool:
    """Contract §2 clause 2. A deterministic candidate carries no tier."""
    return (tier or "") in EXACT_TIERS


def matches(candidate_span: tuple, gold_span: tuple, *, rule: str,
            index: "UnitIndex | None" = None, tier: str = "") -> bool:
    """Does *candidate_span* match *gold_span* under *rule*?

    One function for all three rules so a report cannot accidentally compare a
    figure computed one way against a figure computed another — the Phase 12
    failure mode. `rule` is required and unnamed rules raise.
    """
    if rule == LENIENT:
        return overlaps(candidate_span, gold_span)
    if rule == STRICT:
        return contains(candidate_span, gold_span)
    if rule == DISCOVERED:
        if not exact_evidence(tier):
            return False
        if not containment_relation(candidate_span, gold_span):
            return False
        if index is None:
            raise ValueError(
                "DISCOVERED needs a UnitIndex: clause 4 of the contract is "
                "about structural units and cannot be evaluated from spans "
                "alone. Passing None would silently degrade the primary metric "
                "to bidirectional containment.")
        return index.same_unit(candidate_span, gold_span)
    raise ValueError(f"unknown rule {rule!r}; expected one of {list(RULES)}")


@dataclass
class RuleResult:
    """Per-rule outcome for one scored set."""

    rule: str
    gold_total: int = 0
    found: set = field(default_factory=set)
    #: `candidate_id -> best gold class it matched`, "" when it matched nothing.
    candidate_class: dict = field(default_factory=dict)
    #: `candidate_id -> [gold_id, …]`
    candidate_gold: dict = field(default_factory=dict)

    @property
    def recall(self) -> float:
        return (round(100.0 * len(self.found) / self.gold_total, 1)
                if self.gold_total else 0.0)

    def as_dict(self) -> dict:
        return {"rule": self.rule, "gold_C": self.gold_total,
                "found": len(self.found), "recall": self.recall}


#: Gold class preference when a candidate touches several anchors. Same order
#: `score.py` uses, restated so the two remain comparable.
CLASS_RANK = {"C": 0, "AMB": 1, "NONAGENT": 2, "DUP": 3, "R": 4, "P": 5}


def evaluate(candidates: list, gold_rows: list, *,
             indexes: dict, rules: "tuple[str, ...]" = RULES,
             tier_of=None, file_of=None, span_of=None, id_of=None
             ) -> dict[str, RuleResult]:
    """Score *candidates* against *gold_rows* under each rule.

    The accessors are injectable because a deterministic candidate, a semantic
    candidate and a review card are three different objects and this module
    declines to know about any of them. Defaults read the attribute names the
    `cf` candidate types already use.

    *indexes* maps `(skill, file) -> UnitIndex`. A missing index is fatal for
    `DISCOVERED` rather than skipped: a silently unindexed file would deflate
    the primary metric for a reason nobody would see.
    """
    tier_of = tier_of or (lambda c: getattr(c, "match_tier", ""))
    file_of = file_of or (lambda c: c.source_file)
    span_of = span_of or (lambda c: tuple(c.source_span))
    id_of = id_of or (lambda c: c.candidate_id)

    by_file: dict[str, list[dict]] = {}
    for g in gold_rows:
        by_file.setdefault(g["file"], []).append(g)
    gold_c = {g["gold_id"] for g in gold_rows if g["cls"] == "C"}

    out = {r: RuleResult(rule=r, gold_total=len(gold_c)) for r in rules}

    for c in candidates:
        rel = file_of(c)
        span = span_of(c)
        cid = id_of(c)
        tier = tier_of(c)
        skill = getattr(c, "skill", None) or _skill_of(c)
        index = indexes.get((skill, rel))
        for rule in rules:
            if rule == DISCOVERED and index is None:
                raise KeyError(
                    f"no UnitIndex for ({skill!r}, {rel!r}); DISCOVERED cannot "
                    "be evaluated and must not be approximated")
            hits = [g for g in by_file.get(rel, ())
                    if matches(span, (g["char_start"], g["char_end"]),
                               rule=rule, index=index, tier=tier)]
            hits.sort(key=lambda g: CLASS_RANK.get(g["cls"], 9))
            res = out[rule]
            res.candidate_class[cid] = hits[0]["cls"] if hits else ""
            res.candidate_gold[cid] = [g["gold_id"] for g in hits]
            for g in hits:
                if g["cls"] == "C":
                    res.found.add(g["gold_id"])
    return out


def _skill_of(c) -> str:
    sid = getattr(c, "skill_id", "") or ""
    return sid.rsplit("/", 1)[-1]
