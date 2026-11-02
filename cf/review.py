"""Phase 13 — review cards. The unit a human acts on.

Owner ruling 2026-10-08, ruling 3:

> Do not reduce candidate count by suppressing or ranking candidates yet. Build
> review-card grouping by shared source block / exact or strongly overlapping
> grounded evidence. Merge deterministic and semantic provenance into the same
> review card where appropriate. Support card-level accept/reject/edit while
> preserving all underlying candidate records.

**Nothing here suppresses or ranks.** Grouping changes how many things a
reviewer is shown, not how many candidates exist: every candidate is on exactly
one card, `assert_partition` proves it, and a card records each of its members
verbatim. The count a report quotes as "review burden" is the card count; the
count the store would hold is still the candidate count, and both are reported.

Grouping rule
-------------
Two candidates share a card when, in the same file, **either**

* **same source block** — both spans lie in one `cf.structure` unit; or
* **strongly overlapping grounded evidence** — their spans overlap by at least
  `STRONG_OVERLAP` of the shorter span.

The first clause is the ruling's "shared source block" and does the bulk of the
work: a list item that produced one deterministic candidate and three semantic
clauses becomes one card. The second catches the case the first misses — two
candidates in *different* units quoting substantially the same text, which
happens when a rule is restated across a heading boundary.

Grouping is **transitive**: it is a union-find over candidates, so A–B and B–C
put all three on one card even when A and C do not themselves overlap. That is
deliberate and is the honest reading of "one thing a human confirms" — but it is
also the clause that could in principle chain a whole document into one card, so
`largest_card` is reported and `MAX_CARD_MEMBERS` raises rather than silently
producing a card nobody can review.

Card actions
------------
`accept` / `reject` / `edit` are recorded on the card and **never mutate or
delete a member**. A decision is an annotation: `Card.decision` plus, for an
edit, the edited text alongside the originals. The ruling says "preserving all
underlying candidate records" and the way to not lose a record is to not have a
code path that removes one — so there is none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from statistics import median as _median

from .matching import UnitIndex, overlaps

ACCEPT = "accept"
REJECT = "reject"
EDIT = "edit"
PENDING = "pending"
DECISIONS = (PENDING, ACCEPT, REJECT, EDIT)

DETERMINISTIC = "deterministic"
SEMANTIC = "semantic"

#: Fraction of the SHORTER span that must be covered for two candidates in
#: different units to share a card. Of the shorter, not of either or of the
#: union: a 20-character clause inside a 400-character block overlaps 100% of
#: itself and 5% of the block, and the question "are these the same finding" is
#: answered by the smaller one being essentially inside the larger.
STRONG_OVERLAP = 0.80

#: A card with more members than this is a grouping failure, not a card. Raised
#: rather than returned: a 200-member card would be reported as "one review
#: item" and would quietly make the burden figure meaningless.
MAX_CARD_MEMBERS = 40


class GroupingError(AssertionError):
    """The grouping produced something unreviewable. Always fatal."""


@dataclass
class Member:
    """One candidate on a card, recorded verbatim."""

    candidate_id: str
    origin: str
    source_file: str
    source_span: tuple
    source_text: str
    #: Deterministic: the pattern names. Semantic: the structured fields.
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "origin": self.origin,
                "source_file": self.source_file,
                "source_span": list(self.source_span),
                "source_text": self.source_text, "detail": dict(self.detail)}


@dataclass
class Card:
    """One thing a reviewer confirms. Holds every candidate it covers."""

    card_id: str
    skill: str
    source_file: str
    members: list = field(default_factory=list)
    decision: str = PENDING
    #: Set only by `edit`; the originals stay in `members` untouched.
    edited_text: str = ""
    note: str = ""

    @property
    def origins(self) -> list:
        return sorted({m.origin for m in self.members})

    @property
    def span(self) -> tuple:
        """The hull of the members' spans — for display and for scoring.

        The hull, not a representative member: a card is shown to a reviewer as
        the region its findings came from, and picking one member's span would
        make the card's extent depend on which member happened to be first.
        """
        return (min(m.source_span[0] for m in self.members),
                max(m.source_span[1] for m in self.members))

    @property
    def is_merged(self) -> bool:
        """Both paths on one card — the ruling's 'merge provenance'."""
        return len(self.origins) > 1

    def accept(self, note: str = "") -> "Card":
        return self._decide(ACCEPT, note=note)

    def reject(self, note: str = "") -> "Card":
        return self._decide(REJECT, note=note)

    def edit(self, text: str, note: str = "") -> "Card":
        if not text.strip():
            raise ValueError("an edit needs replacement text; to drop a card, "
                             "reject it")
        return self._decide(EDIT, note=note, edited_text=text)

    def _decide(self, decision: str, *, note: str = "",
                edited_text: str = "") -> "Card":
        before = len(self.members)
        self.decision = decision
        self.note = note
        self.edited_text = edited_text
        if len(self.members) != before:
            raise GroupingError(
                "a decision changed the member list; decisions are annotations "
                "and must preserve every underlying candidate record")
        return self

    def as_dict(self) -> dict:
        return {"card_id": self.card_id, "skill": self.skill,
                "source_file": self.source_file,
                "span": list(self.span), "origins": self.origins,
                "is_merged": self.is_merged, "decision": self.decision,
                "edited_text": self.edited_text, "note": self.note,
                "members": [m.as_dict() for m in self.members]}


def _strong_overlap(a: tuple, b: tuple) -> bool:
    if not overlaps(a, b):
        return False
    inter = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return shorter > 0 and inter / shorter >= STRONG_OVERLAP


def build_cards(members: list, indexes: dict) -> list:
    """Group *members* into cards. `indexes` maps `(skill, file) -> UnitIndex`.

    Union-find over members, per file. A missing index is fatal: without it the
    "shared source block" clause silently stops applying and the grouping
    degrades to overlap-only, which would inflate the card count — the exact
    number the ruling asks to be reported.
    """
    by_file: dict[tuple, list] = {}
    for m in members:
        by_file.setdefault((m.skill, m.source_file), []).append(m)

    cards: list = []
    for (skill, rel), group in sorted(by_file.items()):
        index = indexes.get((skill, rel))
        if index is None:
            raise GroupingError(
                f"no UnitIndex for ({skill!r}, {rel!r}); the shared-source-block "
                "clause cannot be applied and the card count would be inflated")

        parent = list(range(len(group)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)

        units = [index.unit_containing(*m.source_span) for m in group]
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                same_block = (units[i] is not None and units[i] == units[j])
                if same_block or _strong_overlap(group[i].source_span,
                                                 group[j].source_span):
                    union(i, j)

        buckets: dict[int, list] = {}
        for i, m in enumerate(group):
            buckets.setdefault(find(i), []).append(m)

        for root in sorted(buckets):
            ms = sorted(buckets[root], key=lambda m: (m.source_span, m.origin))
            if len(ms) > MAX_CARD_MEMBERS:
                raise GroupingError(
                    f"card for {skill}/{rel} would hold {len(ms)} members, over "
                    f"the {MAX_CARD_MEMBERS} limit. Reported as one review item "
                    "it would make the burden figure meaningless; the grouping "
                    "rule needs narrowing before this corpus can be carded.")
            lo = min(m.source_span[0] for m in ms)
            cards.append(Card(card_id=f"card-{skill}-{rel.rsplit('/', 1)[-1]}"
                                      f"-{lo}",
                              skill=skill, source_file=rel, members=ms))
    cards.sort(key=lambda c: (c.skill, c.source_file, c.span))
    return cards


def assert_partition(cards: list, members: list) -> None:
    """Raise unless the cards are a partition of *members*, by id.

    Grouping must not lose a candidate and must not duplicate one onto two
    cards. Both would corrupt the burden figure, in opposite directions, and
    "every candidate is on exactly one card" is the property the ruling's
    "preserving all underlying candidate records" rests on.
    """
    want = [m.candidate_id for m in members]
    got = [m.candidate_id for c in cards for m in c.members]
    if len(got) != len(want):
        raise GroupingError(
            f"{len(want)} candidates went in and {len(got)} came out of "
            f"{len(cards)} cards — grouping is not a partition")
    if sorted(got) != sorted(want):
        lost = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        raise GroupingError(
            f"card membership does not match the input: lost {lost[:4]}, "
            f"unexpected {extra[:4]}")
    seen: set = set()
    for c in cards:
        for m in c.members:
            if m.candidate_id in seen:
                raise GroupingError(
                    f"candidate {m.candidate_id} is on more than one card")
            seen.add(m.candidate_id)


def burden(cards: list) -> dict:
    """The figures ruling 3 asks for: counts and the cards/skill distribution."""
    per_skill: dict[str, int] = {}
    for c in cards:
        per_skill[c.skill] = per_skill.get(c.skill, 0) + 1
    counts = sorted(per_skill.values())
    return {"cards": len(cards),
            "candidates": sum(len(c.members) for c in cards),
            "merged_cards": sum(1 for c in cards if c.is_merged),
            "skills": len(counts),
            "median_cards_per_skill": burden_stats_median(counts),
            "p90_cards_per_skill": _percentile(counts, 0.90),
            "p95_cards_per_skill": _percentile(counts, 0.95),
            # p90/p95 on a handful of skills are coarse: with n skills the
            # nearest-rank index is ceil(q*n)-1, so for n<=10 and q=0.95 the
            # answer IS the maximum and carries no information beyond it.
            # Reported so a reader does not take p95 for an independent
            # statistic.
            "quantile_note": (
                f"median = mean of the two middle values for even n; p90/p95 "
                f"are nearest-rank, index ceil(q*n)-1. With n={len(counts)} "
                f"skills, p95 is index {max(0, ceil(0.95 * len(counts)) - 1) if counts else 0} "
                f"of {len(counts)} and therefore equals the maximum."),
            "max_cards_per_skill": counts[-1] if counts else 0,
            "largest_card_members": max((len(c.members) for c in cards),
                                        default=0),
            "cards_per_skill": per_skill}


def burden_stats_median(values: list) -> float:
    """The median, with the even-`n` case handled as the median is defined.

    A named function so the test can drive it directly: the defect review found
    was a single-index rule standing in for the median, and a test that only
    read `burden()`'s output could not have distinguished a wrong convention
    from a wrong distribution.
    """
    return float(_median(values)) if values else 0.0


def _percentile(sorted_values: list, q: float) -> float:
    """Nearest-rank percentile: the `ceil(q*n)`-th smallest value.

    Nearest-rank rather than interpolated, because with 8 and 10 skills per
    stratum an interpolated p95 would be a weighted blend of two integers and
    would read as false precision on a sample this small.

    The first version computed `int(round(q * (n - 1)))`, which is wrong twice
    over and review caught both on the dev stratum's `[8, 8, 14, 15, 22, 22,
    22, 28]`:

    * the index should be over `q * n`, not `q * (n - 1)` — the latter is the
      interpolated-quantile *position*, and truncating it to an integer is
      neither convention. It read p90 as 22 where the nearest-rank answer is
      28, understating the tail;
    * `round` is banker's rounding, so `round(3.5)` is 4 and `round(2.5)` is 2.
      A percentile index that depends on the parity of the value it lands on is
      not a definition.

    The median is no longer computed here at all: for even `n` the median is the
    mean of the two middle values, which no single-index rule can produce, and
    `statistics.median` already means exactly that. Reporting one middle value
    and calling it the median read 22.0 where the median is 18.5.
    """
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    i = min(n - 1, max(0, ceil(q * n) - 1))
    return float(sorted_values[i])
