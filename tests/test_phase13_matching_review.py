#!/usr/bin/env python3
"""Phase 13 — the matching contract and the review cards, as assertions.

Hermetic. Two things are checked, and both by breaking them:

* **`matching.DISCOVERED`** is the Owner's rule and not one of the two that
  already existed. The tests that matter are the ones showing it **differs**
  from `strict` and from `lenient` in the cases the contract names — otherwise
  the new metric is the old metric with a new label, which is the Phase 12
  failure repeated.
* **`review.build_cards`** groups without losing or duplicating a candidate.

    python3.13 tests/test_phase13_matching_review.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from constraintrelay import matching as mt                                     # noqa: E402
from constraintrelay import review as rv                                       # noqa: E402

FAILS: list[str] = []
COUNTS = {"checks": 0, "mutations": 0}


def check_(name: str, ok: bool, detail: str = "") -> None:
    COUNTS["checks"] += 1
    if name.startswith("MUTATION"):
        COUNTS["mutations"] += 1
    print(f"{'OK ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# A document with two units far apart, so a cross-unit overlap is
# constructible. The TIGHT list (no blank line between bullets) is one unit; the
# loose bullet after the blank line is its own. That is `structure.parse`'s
# choice and the tests below assert it rather than assume it either way, because
# it is what decides whether two bullets share a card.
DOC = """# Limits

- Orders must never exceed the configured notional limit, and must be logged.
- Positions must be reconciled before the close.

- Research output is not for direct live-trading signals.

| Metric | Threshold |
|---|---|
| IC mean | > 0.03 |
"""
IDX = mt.UnitIndex.of(DOC)

LIST_UNIT = IDX.unit_containing(DOC.index("- Orders"), DOC.index("- Orders") + 5)
item1 = (DOC.index("- Orders"), DOC.index("- Orders") + 76)
clause = (DOC.index("must be logged"), DOC.index("must be logged") + 14)
tight2 = (DOC.index("- Positions"), DOC.index("- Positions") + 48)
loose_start = DOC.index("- Research")
loose = (loose_start, loose_start + 54)

# ---------------------------------------------------------------------------
print("\n-- the unit index ------------------------------------------------")
# ---------------------------------------------------------------------------
check_("a span inside a unit resolves to that unit",
       IDX.unit_containing(*clause) is not None, str(IDX.unit_containing(*clause)))
check_("a span straddling two units resolves to NOTHING, which is what "
       "clause 4 rests on",
       IDX.unit_containing(item1[0], loose[1]) is None)
check_("two spans in one unit are `same_unit`",
       IDX.same_unit(item1, clause))
heading = (0, DOC.index("\n"))
check_("…and two spans in different units are not",
       not IDX.same_unit(clause, heading),
       f"{IDX.unit_containing(*clause)} vs {IDX.unit_containing(*heading)}")

# ---------------------------------------------------------------------------
print("\n-- DISCOVERED differs from strict AND from lenient ---------------")
# ---------------------------------------------------------------------------
# The whole point. If these three agreed everywhere, the contract would have
# bought nothing and `lenient` should have been promoted instead.
gold = item1          # the gold anchor is the first bullet

check_("two bullets with NO blank line between them are ONE unit, so they "
       "share a card", IDX.same_unit(item1, tight2),
       str(IDX.unit_containing(*item1)))
check_("a bullet after a BLANK line is its own unit, so it does not — two "
       "separately-written rules stay two cards",
       not IDX.same_unit(item1, loose),
       f"{IDX.unit_containing(*item1)} vs {IDX.unit_containing(*loose)}")

check_("candidate CONTAINS gold -> discovered (the deterministic shape)",
       mt.matches(item1, gold, rule=mt.DISCOVERED, index=IDX))
check_("gold CONTAINS candidate -> discovered too (the semantic shape); this "
       "is the bidirectional clause, and `strict` says NO here",
       mt.matches(clause, gold, rule=mt.DISCOVERED, index=IDX)
       and not mt.matches(clause, gold, rule=mt.STRICT),
       "discovered=True strict=False")

# a partial overlap that crosses no unit but contains nothing either way
partial = (gold[0] + 20, gold[1] + 0)
half = (gold[0] - 10, gold[0] + 20)
check_("MUTATION partial overlap with NO containment -> lenient says yes, "
       "DISCOVERED says NO. This is the 'arbitrary overlap is insufficient' "
       "clause doing work",
       mt.matches(half, gold, rule=mt.LENIENT)
       and not mt.matches(half, gold, rule=mt.DISCOVERED, index=IDX),
       f"half={half} gold={gold}")

# containment in a DIFFERENT unit: construct a gold anchor on the heading and a
# candidate containing it that reaches into the list unit
wide = (0, item1[1])
check_("MUTATION containment that CROSSES a unit boundary -> refused by "
       "clause 4, while plain bidirectional containment would accept it",
       mt.containment_relation(wide, heading)
       and not mt.matches(wide, heading, rule=mt.DISCOVERED, index=IDX),
       f"wide={wide} spans {IDX.unit_containing(*wide)}")

check_("MUTATION a whitespace-tier semantic match is NOT discovered, per "
       "clause 2",
       mt.matches(clause, gold, rule=mt.DISCOVERED, index=IDX, tier="exact")
       and not mt.matches(clause, gold, rule=mt.DISCOVERED, index=IDX,
                          tier="whitespace"))
check_("a deterministic candidate carries no tier and qualifies by "
       "construction", mt.exact_evidence("") and mt.exact_evidence("exact")
       and not mt.exact_evidence("whitespace"))

try:
    mt.matches(clause, gold, rule=mt.DISCOVERED, index=None)
    caught, why = False, "ACCEPTED DISCOVERED with no UnitIndex"
except ValueError as exc:
    caught, why = True, str(exc)[:70]
check_("MUTATION DISCOVERED with no UnitIndex -> raises rather than silently "
       "degrading to bidirectional containment", caught, why)

try:
    mt.matches(clause, gold, rule="loose", index=IDX)
    caught, why = False, "ACCEPTED an unknown rule"
except ValueError as exc:
    caught, why = True, str(exc)[:60]
check_("MUTATION an unnamed rule -> raises; a report cannot compare figures "
       "computed under different rules by accident", caught, why)

# ---------------------------------------------------------------------------
print("\n-- evaluate() over the three rules -------------------------------")
# ---------------------------------------------------------------------------


class C:
    def __init__(self, cid, span, tier="", skill="demo"):
        self.candidate_id = cid
        self.source_file = "SKILL.md"
        self.source_span = span
        self.source_text = DOC[span[0]:span[1]]
        self.match_tier = tier
        self.skill_id = f"skills/{skill}"


GOLD = [{"gold_id": "g1", "cls": "C", "file": "SKILL.md",
         "char_start": gold[0], "char_end": gold[1]},
        {"gold_id": "g2", "cls": "C", "file": "SKILL.md",
         "char_start": loose[0], "char_end": loose[1]}]
IDXS = {("demo", "SKILL.md"): IDX}

res = mt.evaluate([C("a", clause, "exact")], GOLD, indexes=IDXS)
check_("a clause inside one anchor discovers exactly that one anchor",
       res[mt.DISCOVERED].found == {"g1"}
       and res[mt.DISCOVERED].recall == 50.0, str(res[mt.DISCOVERED].as_dict()))
check_("and `strict` finds nothing, so the two rules are measurably different "
       "on the same input",
       res[mt.STRICT].found == set(), str(res[mt.STRICT].as_dict()))

res2 = mt.evaluate([C("a", clause, "whitespace")], GOLD, indexes=IDXS)
check_("MUTATION the same candidate at whitespace tier discovers nothing",
       res2[mt.DISCOVERED].found == set())

try:
    mt.evaluate([C("a", clause, "exact", skill="other")], GOLD, indexes=IDXS)
    caught, why = False, "ACCEPTED a candidate with no index for its file"
except KeyError as exc:
    caught, why = True, str(exc)[:70]
check_("MUTATION a file with no UnitIndex -> raises rather than deflating the "
       "primary metric invisibly", caught, why)

# ---------------------------------------------------------------------------
print("\n-- review cards: grouping without losing anything ----------------")
# ---------------------------------------------------------------------------


def mem(cid, origin, span):
    m = rv.Member(candidate_id=cid, origin=origin, source_file="SKILL.md",
                  source_span=span, source_text=DOC[span[0]:span[1]])
    m.skill = "demo"
    return m


members = [
    mem("d1", rv.DETERMINISTIC, item1),       # whole list block, one unit
    mem("s1", rv.SEMANTIC, clause),           # clause inside it
    mem("s2", rv.SEMANTIC, tight2),           # second tight bullet, SAME unit
    mem("s3", rv.SEMANTIC, loose),            # loose bullet, OWN unit
    mem("d2", rv.DETERMINISTIC, heading),     # heading, different unit
]
cards = rv.build_cards(members, IDXS)
rv.assert_partition(cards, members)
check_("every candidate is on exactly one card", True,
       f"{len(members)} candidates -> {len(cards)} cards")
check_("the tight list block's three findings merge onto ONE card — this is "
       "the shared-source-block clause and the burden reduction",
       sorted(len(c.members) for c in cards) == [1, 1, 3],
       str(sorted(len(c.members) for c in cards)))
check_("…and that card is exactly the three from the tight block",
       any(sorted(m.candidate_id for m in c.members) == ["d1", "s1", "s2"]
           for c in cards),
       str([sorted(m.candidate_id for m in c.members) for c in cards]))
check_("the heading stays its own card, because it is a different unit",
       any(len(c.members) == 1 and c.members[0].candidate_id == "d2"
           for c in cards))
merged = [c for c in cards if c.is_merged]
check_("deterministic and semantic provenance are merged onto the same card, "
       "which ruling 3 asks for",
       bool(merged) and merged[0].origins == [rv.DETERMINISTIC, rv.SEMANTIC],
       str([c.origins for c in cards]))
big = [c for c in cards if len(c.members) == 3][0]
check_("a card's span is the HULL of its members, not one member's",
       big.span == (min(m.source_span[0] for m in big.members),
                    max(m.source_span[1] for m in big.members)),
       str(big.span))

# strong overlap across units
far = DOC.index("| IC mean")
t1 = (far, far + 22)
t2 = (far + 2, far + 22)
check_("two near-identical spans strongly overlap", rv._strong_overlap(t1, t2))
check_("a short clause inside a long block does NOT count as strong overlap "
       "by itself — the fraction is of the SHORTER span, so this is 100% and "
       "they would group; the guard is that unrelated long/short pairs do not",
       rv._strong_overlap(clause, clause)
       and not rv._strong_overlap(clause, heading))

# --- decisions preserve everything ----------------------------------------
c0 = cards[0]
n_before = len(c0.members)
ids_before = [m.candidate_id for m in c0.members]
c0.accept(note="checked")
check_("accept records a decision and preserves every member",
       c0.decision == rv.ACCEPT and len(c0.members) == n_before
       and [m.candidate_id for m in c0.members] == ids_before)
c0.reject()
check_("reject is also non-destructive",
       c0.decision == rv.REJECT and len(c0.members) == n_before)
c0.edit("Orders must never exceed the configured notional limit.")
check_("edit stores the replacement BESIDE the originals, never over them",
       c0.decision == rv.EDIT and c0.edited_text
       and len(c0.members) == n_before
       and all(m.source_text == DOC[m.source_span[0]:m.source_span[1]]
               for m in c0.members))
try:
    c0.edit("   ")
    caught = False
except ValueError:
    caught = True
check_("MUTATION an empty edit is refused — dropping a card is a reject, not "
       "an edit to nothing", caught)

# --- the partition guard has to be able to fail ----------------------------
try:
    rv.assert_partition(cards[:-1], members)
    caught, why = False, "ACCEPTED cards missing a candidate"
except rv.GroupingError as exc:
    caught, why = True, str(exc)[:70]
check_("MUTATION a card dropped -> partition check refuses", caught, why)

dup = list(cards) + [cards[0]]
try:
    rv.assert_partition(dup, members)
    caught, why = False, "ACCEPTED a candidate on two cards"
except rv.GroupingError as exc:
    caught, why = True, str(exc)[:70]
check_("MUTATION a candidate on two cards -> refused, because that would "
       "deflate the burden figure", caught, why)

try:
    rv.build_cards(members, {})
    caught, why = False, "ACCEPTED a missing UnitIndex"
except rv.GroupingError as exc:
    caught, why = True, str(exc)[:70]
check_("MUTATION no UnitIndex -> refused; without it the shared-block clause "
       "stops applying and the card count inflates", caught, why)

saved = rv.MAX_CARD_MEMBERS
try:
    rv.MAX_CARD_MEMBERS = 2
    try:
        rv.build_cards(members, IDXS)
        caught, why = False, "ACCEPTED an oversized card"
    except rv.GroupingError as exc:
        caught, why = True, str(exc)[:70]
finally:
    rv.MAX_CARD_MEMBERS = saved
check_("MUTATION a card over the member limit -> refused rather than reported "
       "as one review item", caught, why)

# --- burden figures --------------------------------------------------------
b = rv.burden(cards)
check_("burden reports raw candidates AND cards, so neither can be quoted as "
       "the other",
       b["candidates"] == len(members) and b["cards"] == len(cards),
       str({k: b[k] for k in ('cards', 'candidates', 'merged_cards')}))
check_("it reports median, p90 and p95 cards per skill, which ruling 3 names",
       all(k in b for k in ("median_cards_per_skill", "p90_cards_per_skill",
                            "p95_cards_per_skill")), str(b["cards_per_skill"]))
# --- the burden statistics, against a hand-computed distribution ----------
# Review found the median and p90 wrong on the dev stratum's real distribution,
# so these are checked against values computed by hand rather than against the
# implementation's own output.
EVEN = [8, 8, 14, 15, 22, 22, 22, 28]          # the real dev-A distribution
check_("median of an EVEN sample is the mean of the two middles, not one of "
       "them — this read 22.0 before, where the median is 18.5",
       rv.burden_stats_median(EVEN) == 18.5,
       str(rv.burden_stats_median(EVEN)))
check_("p90 is nearest-rank ceil(q*n)-1, so for n=8 it is index 7 = 28. It "
       "read 22 before, which understated the tail",
       rv._percentile(EVEN, 0.90) == 28.0, str(rv._percentile(EVEN, 0.90)))
check_("p95 for n=8 is also index 7, i.e. the maximum — reported, not hidden",
       rv._percentile(EVEN, 0.95) == 28.0)
ODD = [6, 7, 8, 9, 10, 10, 10, 13, 15, 22]     # the real retired-H-A one
check_("median of an ODD-count sample is the middle value",
       rv.burden_stats_median(ODD) == 10.0, str(rv.burden_stats_median(ODD)))
check_("p90 for n=10 is index 8 = 15, and p95 is index 9 = 22",
       rv._percentile(ODD, 0.90) == 15.0 and rv._percentile(ODD, 0.95) == 22.0)
check_("MUTATION the old formula int(round(q*(n-1))) disagrees on BOTH of "
       "those, which is why it was wrong",
       int(round(0.90 * (len(EVEN) - 1))) == 6
       and EVEN[6] == 22 and rv._percentile(EVEN, 0.90) == 28.0,
       "old index 6 -> 22, correct index 7 -> 28")
check_("an empty sample is 0.0 rather than an exception",
       rv._percentile([], 0.9) == 0.0 and rv.burden_stats_median([]) == 0.0)
check_("`burden` reports the quantile convention it used, so p95 is not read "
       "as an independent statistic",
       "nearest-rank" in b["quantile_note"]
       and "equals the maximum" in b["quantile_note"])

print()
print(f"Phase 13 matching/review totals: {COUNTS['checks']} checks "
      f"({COUNTS['mutations']} mutations), {len(FAILS)} failures")
if FAILS:
    print(f"FAILED {len(FAILS)}: " + ", ".join(FAILS))
    sys.exit(1)
print("Phase 13 matching/review: all checks pass")
