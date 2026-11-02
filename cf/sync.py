"""Phase 1 layer D — source synchronization.

Re-scan the sources and compare them with what the store recorded. The brief's
four states, and nothing invented beyond them:

    UNCHANGED       the source still says what the store recorded
    SOURCE_CHANGED  the source for a stored constraint now reads differently
    SOURCE_REMOVED  the text a stored constraint came from is gone
    NEW_CANDIDATE   the source has a candidate the store does not hold

Two rules shape the implementation more than the states do.

*Do not automatically overwrite canonical constraint text.* Nothing in this
module writes. It returns a report; `cmd_update` is the only caller that may act
on one, and only for constraints named on its command line.

*Existing canonical records are never silently deleted because source discovery
fails.* A `SOURCE_REMOVED` row is a **report**, not a deletion — `status` stays
`active` until someone runs `retire`. And one failure mode is singled out
because it is the one that would turn a bad path into mass data loss: if the
store holds constraints and discovery finds no skills at all,
`DiffReport.discovery_failed` is set, the per-constraint rows are suppressed
entirely, and the caller is expected to exit as a configuration error. Pointing
ConstraintFlow at the wrong directory must not read as "the author deleted 473
constraints".

Localizing a changed constraint
-------------------------------
`source_hash` answers *did this text change?* but not *what did it become*. Three
localizations are tried, in descending order of confidence, and the one used is
named in the row so a reviewer can discount a weak match:

1. `exact_hash` — a fresh candidate with the same hash. The constraint is
   unchanged; if its offsets moved, `span_moved` carries the new ones.
2. `verbatim` — the stored `source_text` still appears in the file, but no
   longer as an extracted candidate. Also unchanged: the text is what matters,
   and extraction no longer flagging it is a fact about the heuristics.
3. `overlapping_span`, then `same_first_line` — a candidate at the old
   position, or one whose opening line is identical. This is the only place
   anything resembling similarity matching happens, and it never decides
   identity: it decides *which* changed text to show as the delta for a
   constraint whose identity is already fixed by its `constraint_id`.

If none of the three finds anything, the row is `SOURCE_REMOVED`. Guessing
further would mean attaching a stored id to text nobody connected to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import naming
from .candidates import Candidate, Extraction, source_hash
from .inventory import Inventory
from .skillstore import ConstraintRecord, SkillStore

UNCHANGED = "UNCHANGED"
SOURCE_CHANGED = "SOURCE_CHANGED"
SOURCE_REMOVED = "SOURCE_REMOVED"
NEW_CANDIDATE = "NEW_CANDIDATE"
STATES = (UNCHANGED, SOURCE_CHANGED, SOURCE_REMOVED, NEW_CANDIDATE)


@dataclass(frozen=True)
class Row:
    """One synchronization finding."""

    state: str
    skill_id: str
    constraint_id: str = ""
    candidate_id: str = ""
    source_file: str = ""
    detail: str = ""
    located_by: str = ""
    new_text: str = ""
    span_moved: tuple[int, int] | None = None
    new_lines: tuple[int, int] | None = None

    def as_dict(self) -> dict:
        out = {"state": self.state, "skill_id": self.skill_id,
               "constraint_id": self.constraint_id,
               "candidate_id": self.candidate_id,
               "source_file": self.source_file, "detail": self.detail,
               "located_by": self.located_by}
        if self.new_text:
            out["new_text"] = self.new_text
        if self.span_moved is not None:
            out["span_moved"] = list(self.span_moved)
        if self.new_lines is not None:
            out["new_lines"] = list(self.new_lines)
        return out


@dataclass
class DiffReport:
    rows: list[Row] = field(default_factory=list)
    discovery_failed: bool = False
    discovery_message: str = ""

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATES}
        for r in self.rows:
            out[r.state] += 1
        return out

    def of(self, state: str) -> list[Row]:
        return [r for r in self.rows if r.state == state]

    @property
    def clean(self) -> bool:
        """No change needs an author decision. `NEW_CANDIDATE` counts as one."""
        return not self.discovery_failed and not any(
            r.state in (SOURCE_CHANGED, SOURCE_REMOVED, NEW_CANDIDATE)
            for r in self.rows)

    def as_dict(self) -> dict:
        return {"discovery_failed": self.discovery_failed,
                "discovery_message": self.discovery_message,
                "counts": self.counts(),
                "rows": [r.as_dict() for r in self.rows]}


def _locate(rec: ConstraintRecord, file_text: str,
            in_file: list[Candidate]) -> tuple[str, Candidate | None, int]:
    """`(located_by, candidate, verbatim_offset)` for *rec*. See module docstring."""
    for c in in_file:
        if c.source_hash == rec.source_hash:
            return "exact_hash", c, -1
    if rec.source_text:
        at = file_text.find(rec.source_text)
        if at != -1:
            return "verbatim", None, at
    lo, hi = rec.source_span
    for c in in_file:
        if c.source_span[0] < hi and lo < c.source_span[1]:
            return "overlapping_span", c, -1
    first = rec.source_text.strip().splitlines()[0].strip() \
        if rec.source_text.strip() else ""
    if first:
        for c in in_file:
            head = c.source_text.strip().splitlines()[0].strip() \
                if c.source_text.strip() else ""
            if head == first:
                return "same_first_line", c, -1
    return "", None, -1


def diff(store: SkillStore, inv: Inventory, extraction: Extraction, *,
         include_retired: bool = False,
         skill_ids: "list[str] | None" = None) -> DiffReport:
    """Compare *store* against a fresh scan. Writes nothing."""
    report = DiffReport()
    wanted = set(skill_ids) if skill_ids else None

    tracked = [c for c in store.constraints
               if (include_retired or c.active)
               and (wanted is None or c.skill_id in wanted)]

    if tracked and not inv.skills:
        report.discovery_failed = True
        report.discovery_message = (
            f"the store holds {len(tracked)} constraint(s) but source discovery "
            f"found no skills under {inv.root}. Nothing is reported as removed: a "
            f"discovery failure is not an author deleting constraints. Check the "
            f"path, or pass --skills-dir.")
        return report

    by_file: dict[tuple[str, str], list[Candidate]] = {}
    for c in extraction.candidates:
        by_file.setdefault((c.skill_id, c.source_file), []).append(c)

    matched_candidates: set[str] = set()
    inv_by_id = inv.by_id

    for rec in sorted(tracked, key=lambda c: (c.skill_id, c.number)):
        skill = inv_by_id.get(rec.skill_id)
        if skill is None:
            report.rows.append(Row(
                SOURCE_REMOVED, rec.skill_id, rec.constraint_id,
                source_file=rec.source_file,
                detail="skill not found by discovery; the canonical record is "
                       "kept and stays active until it is retired explicitly"))
            continue
        source = skill.file(rec.source_file)
        if source is None:
            report.rows.append(Row(
                SOURCE_REMOVED, rec.skill_id, rec.constraint_id,
                source_file=rec.source_file,
                detail="source file not found in the skill; the canonical record "
                       "is kept"))
            continue

        in_file = by_file.get((rec.skill_id, rec.source_file), [])
        located_by, cand, verbatim_at = _locate(rec, source.text, in_file)

        if located_by == "exact_hash" and cand is not None:
            matched_candidates.add(cand.candidate_id)
            moved = tuple(cand.source_span) != tuple(rec.source_span)
            report.rows.append(Row(
                UNCHANGED, rec.skill_id, rec.constraint_id, cand.candidate_id,
                rec.source_file,
                detail="source text unchanged"
                       + (f"; moved to chars[{cand.source_span[0]},"
                          f"{cand.source_span[1]}) lines "
                          f"{cand.source_lines[0]}-{cand.source_lines[1]} — run "
                          f"`update --refresh-provenance` to record the new "
                          f"position" if moved else ""),
                located_by=located_by,
                span_moved=tuple(cand.source_span) if moved else None,
                new_lines=tuple(cand.source_lines) if moved else None))
            continue

        if located_by == "verbatim":
            end = verbatim_at + len(rec.source_text)
            moved = (verbatim_at, end) != tuple(rec.source_span)
            line = source.text.count("\n", 0, verbatim_at) + 1
            report.rows.append(Row(
                UNCHANGED, rec.skill_id, rec.constraint_id, "", rec.source_file,
                detail="source text unchanged, but extraction no longer produces a "
                       "candidate for it" + ("; it also moved within the file"
                                             if moved else ""),
                located_by=located_by,
                span_moved=(verbatim_at, end) if moved else None,
                new_lines=(line, line + rec.source_text.count("\n")) if moved else None))
            continue

        if cand is not None:
            matched_candidates.add(cand.candidate_id)
            report.rows.append(Row(
                SOURCE_CHANGED, rec.skill_id, rec.constraint_id, cand.candidate_id,
                rec.source_file,
                detail=f"source text differs from the recorded "
                       f"source_hash {rec.source_hash[:12]}…; canonical text is "
                       f"unchanged until `update` is run",
                located_by=located_by, new_text=cand.source_text,
                span_moved=tuple(cand.source_span),
                new_lines=tuple(cand.source_lines)))
            continue

        report.rows.append(Row(
            SOURCE_REMOVED, rec.skill_id, rec.constraint_id, "", rec.source_file,
            detail="the recorded source text is no longer in the file and no "
                   "successor could be located; the canonical record is kept"))

    confirmed = store.by_candidate()
    for c in extraction.candidates:
        if wanted is not None and c.skill_id not in wanted:
            continue
        if c.candidate_id in matched_candidates or c.candidate_id in confirmed:
            continue
        report.rows.append(Row(
            NEW_CANDIDATE, c.skill_id, "", c.candidate_id, c.source_file,
            detail=f"{c.tier}: {c.extraction_reason} at {c.locator}",
            new_text=c.source_text, span_moved=tuple(c.source_span),
            new_lines=tuple(c.source_lines)))
    return report


def render(report: DiffReport, *, verbose: bool = False) -> str:
    if report.discovery_failed:
        return "DISCOVERY FAILED\n" + report.discovery_message
    lines: list[str] = []
    counts = report.counts()
    for state in STATES:
        rows = report.of(state)
        if not rows:
            continue
        lines.append(f"## {state} ({len(rows)})")
        shown = rows if verbose else rows[:20]
        for r in shown:
            head = r.constraint_id or r.candidate_id
            lines.append(f"  {head:<22} {r.skill_id}")
            lines.append(f"    {r.source_file}: {r.detail}")
            if r.new_text and state != UNCHANGED:
                first = r.new_text.strip().splitlines()[0] if r.new_text.strip() else ""
                lines.append(f"    now: {first[:150]}")
        if len(rows) > len(shown):
            lines.append(f"  … {len(rows) - len(shown)} more (use --verbose)")
        lines.append("")
    lines.append("  ".join(f"{k} {v}" for k, v in counts.items()))
    if not report.clean:
        lines.append("")
        lines.append(f"Nothing above has been applied. `{naming.cmd('confirm')}` "
                     f"imports new candidates; `{naming.cmd('update')}` accepts "
                     f"a changed source into the canonical record; "
                     f"`{naming.cmd('retire')}` is the only thing that "
                     f"deactivates one.")
    return "\n".join(lines)


def verbatim_hash(text: str) -> str:
    """Re-exported so callers do not have to import `candidates` for one hash."""
    return source_hash(text)
