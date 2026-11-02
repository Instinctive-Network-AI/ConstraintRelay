"""v2 layer 3 — deterministic selection, and explicit coverage marking.

What this does: filters library entries by **skill**, **source version**,
**tool name** and **action tag**. Four deterministic keys, all either declared
by the author or derived at build time. No model call, no inference, no reading
of a rule's prose.

What this does NOT do, stated as the contract rather than as a caveat
---------------------------------------------------------------------
**It does not rule on whether a rule's condition applies.** A rule reading
*"when confidence is below 0.6 … except in backtest mode"* is selected and
handed over with `condition_adjudicated = False`. Whether confidence is below
0.6, and whether the backtest exception fires, is not decided here and is not
decided anywhere in v2. Calling the output "the rules that apply" would be the
overclaim; it is **the rules to deliver**.

Coverage is a report, never a decision
--------------------------------------
`NOT_IN_LIBRARY`, `VERSION_MISMATCH` and `COVERAGE_INSUFFICIENT` describe what
the library could and could not answer. They are **not** allow/deny verdicts and
carry no authorisation meaning. `Coverage.__bool__` raises for that reason: a
caller writing `if coverage:` has mistaken a coverage report for a decision, and
the shape of this project's recurring defect is a guard that reads as a verdict.

Not finding a rule is never "there is no rule"
----------------------------------------------
Every path returns a status. `Selection` has no state in which an empty rule
list is presented without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .library import DEVELOPER_REVIEWED, Library, file_digest, sha256_text

COVERED = "COVERED"
NOT_IN_LIBRARY = "NOT_IN_LIBRARY"
VERSION_MISMATCH = "VERSION_MISMATCH"
COVERAGE_INSUFFICIENT = "COVERAGE_INSUFFICIENT"
COVERAGE_STATES = (COVERED, NOT_IN_LIBRARY, VERSION_MISMATCH,
                   COVERAGE_INSUFFICIENT)


class SelectorError(Exception):
    """A misuse of the selector, including reading coverage as a decision."""


@dataclass
class SkillCoverage:
    """What the library could say about ONE skill."""

    skill_id: str
    status: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "status": self.status,
                "detail": dict(self.detail)}


@dataclass
class Coverage:
    """What the library could say about the task's skills, as a whole."""

    status: str
    per_skill: list = field(default_factory=list)   # list[SkillCoverage]

    def __bool__(self) -> bool:
        raise SelectorError(
            "Coverage is a report, not a decision. It states what the library "
            "could and could not answer; it carries no allow/deny meaning and "
            "may not be used as a truth value. Read `.status` and decide "
            "explicitly.")

    @property
    def covered(self) -> list:
        return [c.skill_id for c in self.per_skill if c.status == COVERED]

    @property
    def uncovered(self) -> list:
        return [c.skill_id for c in self.per_skill if c.status != COVERED]

    def as_dict(self) -> dict:
        return {"status": self.status,
                "_warning": ("a coverage status is not an authorisation "
                             "decision; it reports what the library could "
                             "answer"),
                "covered": self.covered, "uncovered": self.uncovered,
                "per_skill": [c.as_dict() for c in self.per_skill]}


@dataclass
class Selection:
    """Rules to deliver, plus the coverage report that must travel with them."""

    to_deliver: list = field(default_factory=list)   # list[LibraryEntry]
    coverage: "Coverage | None" = None
    #: Always False in v2. Present so no surface can imply otherwise by omission.
    condition_adjudicated: bool = False
    min_review_tier: str = DEVELOPER_REVIEWED

    def as_dict(self) -> dict:
        return {"condition_adjudicated": self.condition_adjudicated,
                "min_review_tier": self.min_review_tier,
                "_warning": ("conditions, exceptions and scope limits are "
                             "carried verbatim and NOT adjudicated; these are "
                             "the rules to deliver, not the rules that apply"),
                "coverage": (self.coverage.as_dict() if self.coverage
                             else None),
                "to_deliver": [e.as_dict() for e in self.to_deliver]}


def _skill_coverage(lib: Library, skill_id: str, source_root) -> SkillCoverage:
    if not lib.holds_skill(skill_id):
        return SkillCoverage(skill_id, NOT_IN_LIBRARY,
                             {"reason": "the library holds no binding for "
                                        "this skill; nothing here says the "
                                        "skill has no constraints"})
    binding = lib.skills[skill_id]
    drifted = []
    for f in binding.files:
        p = source_root / f.rel_path
        if not p.is_file():
            drifted.append({"rel_path": f.rel_path, "reason": "file missing",
                            "built_sha256": f.content_sha256,
                            "current_sha256": None})
            continue
        now = file_digest(p)
        if now != f.content_sha256:
            drifted.append({"rel_path": f.rel_path,
                            "reason": "content changed since build",
                            "built_sha256": f.content_sha256,
                            "current_sha256": now})
    if drifted:
        return SkillCoverage(skill_id, VERSION_MISMATCH,
                             {"source_version": binding.source_version,
                              "changed_files": drifted})
    return SkillCoverage(skill_id, COVERED,
                         {"source_version": binding.source_version,
                          "files": len(binding.files)})


def select(lib: Library, task, *, source_root,
           min_review_tier: str = DEVELOPER_REVIEWED) -> Selection:
    """Deterministic selection for *task*, with a coverage report attached.

    `task` is a `constraintrelay.taskscope.TaskRecord`. Filtering keys, in order:

    1. **skill** — the entry's `skill_id` is one the task names;
    2. **version** — that skill's bound file hashes still match the source.
       A version mismatch yields **no rules for that skill**, because a rule
       bound to bytes that changed cannot be quoted as current;
    3. **tool name** — an entry declaring `tool_names` is kept only when the
       task's tool is among them. An entry declaring none is skill-wide and
       always kept;
    4. **action tag** — same rule.

    Declaring nothing means skill-wide, deliberately: a rule with no declared
    narrowing is broader than one with, so the absence of a selector must not
    quietly exclude it.
    """
    from .taskscope import TaskRecord  # local: avoid an import cycle
    if not isinstance(task, TaskRecord):
        raise SelectorError(f"task must be a TaskRecord, got "
                            f"{type(task).__name__}")

    per = [_skill_coverage(lib, sid, source_root) for sid in task.skills]
    statuses = {c.status for c in per}
    if not per:
        overall = COVERAGE_INSUFFICIENT
    elif statuses == {COVERED}:
        overall = COVERED
    elif statuses == {NOT_IN_LIBRARY}:
        overall = NOT_IN_LIBRARY
    elif statuses == {VERSION_MISMATCH}:
        overall = VERSION_MISMATCH
    else:
        # A mix: some skills answered, some did not. That is precisely
        # "partially covered", and flattening it to either extreme would lose
        # the half that matters.
        overall = COVERAGE_INSUFFICIENT
    coverage = Coverage(status=overall, per_skill=per)

    usable = {c.skill_id for c in per if c.status == COVERED}
    out = []
    for e in lib.for_skill_many(sorted(usable), min_tier=min_review_tier):
        if e.tool_names and task.tool_names:
            if not set(e.tool_names) & set(task.tool_names):
                continue
        if e.action_tags and task.action_tags:
            if not set(e.action_tags) & set(task.action_tags):
                continue
        out.append(e)
    out.sort(key=lambda e: (e.skill_id, e.constraint_id))
    return Selection(to_deliver=out, coverage=coverage,
                     condition_adjudicated=False,
                     min_review_tier=min_review_tier)


def verify_traceability(entry, *, source_root) -> dict:
    """Re-verify one delivered entry against the bytes on disk.

    Two independent checks, because they fail for different reasons:
    the **span** hash catches a recorded text that does not match its own hash,
    and the **file** hash catches a source that moved under the library.
    """
    span_ok = entry.verify_source_hash()
    p = source_root / entry.source_file
    if p.is_file():
        now = file_digest(p)
        file_ok = (now == entry.file_content_sha256)
    else:
        now, file_ok = None, False
    return {"constraint_id": entry.constraint_id,
            "span_hash_ok": span_ok, "file_hash_ok": file_ok,
            "recorded_file_sha256": entry.file_content_sha256,
            "current_file_sha256": now,
            "recomputed_span_sha256": sha256_text(entry.source_text)}
