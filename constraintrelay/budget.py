"""`constraintrelay budget` — Brief v0.6 §11.

Margin is defined **per profile and scenario**, in the harness's native unit.
There is no cross-profile total, and this module cannot compute one.

How margin is measured
----------------------
Not by arithmetic on a cap. Filler is inserted into the real load set and the
profile is asked to predict again, so the answer comes from the same delivery
model every verdict comes from. Three §11 requirements fall out of that:

* **Shared budget.** Earlier material in the same load set is already in the
  prediction, so a margin measured this way includes it without special cases.
* **Head/tail.** Prefix and suffix margins are measured separately and reported
  separately, because §11 says a head-delivered and a tail-delivered block do
  not share one scalar definition.
* **Content-aware deterministic selection.** If delivery is not monotone in the
  amount of inserted filler — OpenClaw's policy digest re-selects lines from the
  whole file, so adding text can *restore* a clause — there is no scalar margin
  and §11's exact wording is emitted instead of a number.

Filler is ASCII, so one filler character is exactly one unit in all three units
§11 allows (UTF-8 bytes, Unicode code points, UTF-16 code units). That is what
makes a single search correct for every profile in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .markers import CriticalConstraint, content_blocks
from .profiles import InputFile, block_needle
from .scan import scan_under_profile
from .scenario import Scenario, aggregate, load_set
from .verdict import Cause, Status, resolve_block_spans

#: §11's exact phrasing for the no-scalar-margin case.
NOT_DEFINED = "numeric margin: not defined for this delivery rule"

#: Upper bound on the search. Large enough to exceed every modeled cap
#: (OpenClaw's shared total is 60,000 units, Codex's budget 32,768 bytes), and
#: bounded so a pathological profile cannot make the search unbounded.
SEARCH_CEILING = 262_144

#: Filler is one character per line so that inserted text cannot accidentally
#: satisfy a content-aware selection rule — a run of `x` is neither a heading,
#: nor a list item, nor a policy keyword.
_FILLER_LINE = "x"


def _filler(units: int) -> str:
    """*units* native units of inert text."""
    if units <= 0:
        return ""
    # "x\n" is 2 units, so emit pairs and pad.
    whole, rest = divmod(units, 2)
    return "x\n" * whole + ("x" if rest else "")


@dataclass
class Margin:
    """One direction's margin for one constraint in one scenario."""

    direction: str                 # "prefix" | "suffix"
    units: int | None              # None when no scalar margin exists
    unit_name: str = ""
    note: str = ""

    def render(self) -> str:
        if self.units is None:
            return f"{self.direction}: {NOT_DEFINED}" + (
                f" ({self.note})" if self.note else "")
        return (f"{self.direction}: {self.units:,} {self.unit_name} "
                f"of margin" + (f" ({self.note})" if self.note else ""))


@dataclass
class BudgetReport:
    profile_id: str
    scenario_location: str
    unit_name: str
    constraint_id: str
    locator: str
    status: Status
    prefix: Margin | None = None
    suffix: Margin | None = None
    parameters: dict | None = None

    def as_dict(self) -> dict:
        def m(x: Margin | None) -> dict | None:
            return None if x is None else {
                "direction": x.direction, "units": x.units,
                "unit": x.unit_name, "note": x.note,
                "defined": x.units is not None}
        return {"profile_id": self.profile_id,
                "scenario": self.scenario_location,
                "unit": self.unit_name,
                "constraint_id": self.constraint_id or None,
                "locator": self.locator,
                "status": self.status.value,
                "declared_parameters": self.parameters or {},
                "prefix_margin": m(self.prefix),
                "suffix_margin": m(self.suffix)}


def _delivered_with(files: list[InputFile], profile, target: InputFile,
                    offset: int, filler: int, wanted: list[str]) -> bool:
    """Is every text in *wanted* still delivered on load with filler inserted?

    Membership uses the profile's **routes** where it has them, so a margin cannot
    disagree with the verdict it is a margin for. That matters more than it sounds:
    the old test was `block_needle(...) in pred.delivered`, and a one-line clause
    with no internal whitespace runs appears in the policy digest verbatim — so
    containment answered yes on a route the owner ruling says cannot establish
    delivery, and the search then reported a scalar margin for a delivery that was
    never established. Block *indices* are not used: inserted filler shifts them,
    and the authored text is what has to survive.
    """
    mutated: list[InputFile] = []
    for f in files:
        if f.path == target.path:
            new_text = f.file_text[:offset] + _filler(filler) + f.file_text[offset:]
            mutated.append(InputFile(
                f.name, f.path, new_text,
                loader_view=None if f.loader_view is None else new_text,
                missing=f.missing, unreadable=f.unreadable))
        else:
            mutated.append(f)

    mutated_target = next(f for f in mutated if f.path == target.path)
    pred = profile.predict(mutated)[target.path]
    if not pred.loaded or pred.unmodeled:
        return False
    if pred.spans is None:
        return all(block_needle(profile, text) in pred.delivered for text in wanted)
    for text in wanted:
        if not _established_by_route(mutated_target, pred, text):
            return False
    return True


def _non_monotone_route(reg, picked) -> str:
    """The declared non-monotone region this constraint's delivery depends on, if any.

    Keyed off the *observed route* rather than off the profile alone: an OpenClaw
    constraint delivered in the head is in a perfectly monotone region of a profile
    that also has a non-monotone one, and refusing a margin for the whole adapter
    would withhold a correct number from most constraints to protect a few.

    A `TRANSFORMED_BY_DIGEST` cause is the signal, because that is exactly the cause
    `resolve_block_spans` emits when the digest is the only route — so the region is
    identified by the mechanism that decided the verdict, not by guessing from the
    file name.
    """
    if not reg.non_monotone_regions:
        return ""
    if any(b.cause is Cause.TRANSFORMED_BY_DIGEST for b in picked):
        return reg.non_monotone_regions[0]
    return ""


def _established_by_route(f: InputFile, pred, text: str) -> bool:
    """Is *text* delivered on a **source-preserving** route in this prediction?

    Every occurrence is tried, because the filler insertion can move a clause and a
    repository may hold more than one copy; one source-preserving hit is enough.
    """
    haystack = f.file_text
    at = haystack.find(text)
    while at != -1:
        status, _, _ = resolve_block_spans(text, at, at + len(text), pred.spans,
                                          unmodeled=bool(pred.unmodeled))
        if status is Status.DELIVERED:
            return True
        at = haystack.find(text, at + 1)
    return False


def _search(probe, ceiling: int = SEARCH_CEILING) -> tuple[int | None, str]:
    """Largest `n >= 0` with `probe(n)` true, or `None` when not monotone.

    The monotonicity ladder is checked *before* the bisection, because §6's rule
    applies here too: equal endpoints are not evidence about the interior unless
    monotonicity has been established. A profile whose delivery rule re-selects
    content can be true, false, then true again, and bisecting that produces a
    confident wrong number.
    """
    if not probe(0):
        return 0, "not delivered on load even with no added text"

    ladder = [1, 2, 4, 8, 16, 64, 256, 1024, 4096, 16384, 65536, ceiling]
    seen: list[tuple[int, bool]] = [(0, True)]
    for n in ladder:
        if n > ceiling:
            break
        seen.append((n, probe(n)))

    # Monotone non-increasing means: once false, never true again.
    false_at = next((n for n, ok in seen if not ok), None)
    if false_at is None:
        return ceiling, f"still delivered at the {ceiling:,}-unit search ceiling"
    if any(ok for n, ok in seen if n > false_at):
        return None, ("delivery is not monotone in inserted upstream text, so "
                      "no scalar margin exists for this delivery rule")

    lo = max((n for n, ok in seen if ok), default=0)
    hi = false_at
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if probe(mid):
            lo = mid
        else:
            hi = mid
    return lo, ""


def measure(scenario: Scenario, constraint: CriticalConstraint,
            project_root: Path) -> BudgetReport:
    """Prefix and suffix margin for one constraint in one scenario."""
    reg = scenario.registered
    files = load_set(scenario)
    profile = (reg.build(active=scenario.state.get("active_skills"))
               if reg.needs_declaration else reg.build())
    try:
        rel = scenario.root.relative_to(project_root)
        loc = "." if str(rel) in ("", ".") else str(rel)
    except ValueError:
        loc = str(scenario.root)

    report = BudgetReport(
        profile_id=reg.profile_id, scenario_location=loc,
        unit_name=reg.native_unit, constraint_id=constraint.id,
        locator=constraint.locator, status=Status.UNKNOWN,
        parameters=dict(scenario.parameters))

    target = next((f for f in files if f.name == constraint.file), None)
    if target is None:
        report.status = Status.NOT_DELIVERED
        report.prefix = Margin("prefix", 0, reg.native_unit,
                               "file is not in this profile's load set")
        report.suffix = Margin("suffix", 0, reg.native_unit,
                               "file is not in this profile's load set")
        return report

    base = scan_under_profile(files, profile)
    fv = next((f for f in base.files if f.name == constraint.file), None)
    picked = ([b for b in fv.blocks if b.index in set(constraint.blocks)]
              if fv else [])
    report.status = aggregate([b.status for b in picked])

    if report.status is Status.UNKNOWN:
        note = "an unmodeled step forbids a definite margin"
        report.prefix = Margin("prefix", None, reg.native_unit, note)
        report.suffix = Margin("suffix", None, reg.native_unit, note)
        return report

    wanted = [b.text.strip() for b in content_blocks(target.file_text)
              if b.index in set(constraint.blocks) and b.text.strip()]
    if not wanted:
        note = "marker resolves to no authored text"
        report.prefix = Margin("prefix", 0, reg.native_unit, note)
        report.suffix = Margin("suffix", 0, reg.native_unit, note)
        return report

    # Owner ruling 2026-10-02, item 4: **no monotonicity inference inside a declared
    # non-monotone region.** `_search`'s ladder is an empirical monotonicity check,
    # and an empirical check can be fooled — the ladder samples 12 points out of a
    # 262,144-wide interval, and OpenClaw's greedy digest selection can flip at any
    # point between two samples. Where the profile *declares* the region, that is a
    # stronger statement than any sampling, so the search is refused outright rather
    # than run and hoped over.
    #
    # The ruling's exception is an exactly declared parameter set, which `measure`
    # does not have: it is asked for a margin *over* a parameter range. §8 already
    # fails closed on an undeclared required parameter before reaching here.
    region = _non_monotone_route(reg, picked)
    if region:
        note = (f"delivery of this constraint is decided in the {region}, where a "
                f"larger budget can deliver less: selection is greedy over the whole "
                f"file, so raising the budget can admit a high-priority line that "
                f"displaces a line a smaller budget kept. Endpoint outcomes are not "
                f"evidence about the interior, so no scalar margin is reported")
        report.prefix = Margin("prefix", None, reg.native_unit, note)
        report.suffix = Margin("suffix", None, reg.native_unit, note)
        return report

    def probe_at(offset: int):
        return lambda n: _delivered_with(files, profile, target, offset, n, wanted)

    pre, pre_note = _search(probe_at(constraint.start))
    report.prefix = Margin("prefix", pre, reg.native_unit, pre_note)
    suf, suf_note = _search(probe_at(min(constraint.end, len(target.file_text))))
    report.suffix = Margin("suffix", suf, reg.native_unit, suf_note)
    return report
