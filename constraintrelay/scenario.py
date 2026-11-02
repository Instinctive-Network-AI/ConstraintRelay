"""Scenario — Brief v0.6 §5, and the §9 single-revision evaluation.

A **Scenario** is the unit of evaluation:

    qualified profile
    + working directory / entry point
    + declared parameters
    + declared external state where required

§15's output discipline is structural here, as A3 was before it: a result
belongs to one profile and one scenario, and nothing in this module can produce
a figure that spans two profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Target
from .discover import (collect_ancestors, collect_bootstrap_files,
                       collect_context_files, collect_skills, context_dirs,
                       strip_frontmatter, with_loader_view)
from .fingerprint import FingerprintCheck, check as fingerprint_check
from .markers import CriticalConstraint, MarkerScan, scan_project
from .profiles import InputFile
from .registry import REGISTRY
from .scan import ProfileResult, scan_under_profile
from .verdict import Cause, Status


@dataclass(frozen=True)
class Scenario:
    """One evaluable (profile, location, parameters, state)."""

    profile_key: str
    root: Path
    target: str = ""                       # declaring target name, "" if implied
    parameters: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    #: v0.7 §1: the declared build/session context, carried so §2 applicability
    #: can be decided per scenario rather than once per project.
    context: dict = field(default_factory=dict)
    #: See `config.Target.implied`. Carried so Impact pairing can tell a declared
    #: target name from a profile key wearing one.
    implied: bool = False
    #: v0.7 §13 per-target capsule placement; empty means the store's default.
    delivery: dict = field(default_factory=dict)
    #: Pre-merge ruling item 6: declared/snapshotted repository-external inputs.
    external: dict = field(default_factory=dict)

    @property
    def registered(self):
        return REGISTRY[self.profile_key]

    def declared_context(self, project_root: Path):
        """§2's context for this scenario.

        Two of §15's dimensions are derived rather than declared, because the
        scenario already knows them for certain: `working_directory` *is* the
        scenario, and `harness_target` is the declaring target's name. Both are
        set with `setdefault`, so an explicit `[target.context]` entry still wins.

        The working directory is project-relative. An absolute path would make
        every `working_directory` scope in a store unmatchable on a different
        machine, and §2.2 would then silently keep those constraints in — an
        over-inclusion that hides a scope the author believed was in force.
        """
        from .applicability import Context
        declared = {k: str(v) for k, v in self.context.items()}
        if self.target:
            declared.setdefault("harness_target", self.target)
        try:
            rel = self.root.relative_to(project_root)
        except ValueError:
            rel = self.root
        declared.setdefault("working_directory",
                            "." if str(rel) in ("", ".") else str(rel))
        return Context(declared)

    @property
    def profile_id(self) -> str:
        return self.registered.profile_id

    def label(self, project_root: Path) -> str:
        try:
            rel = self.root.relative_to(project_root)
        except ValueError:
            rel = self.root
        loc = "." if str(rel) in ("", ".") else str(rel)
        return f"{self.profile_id} @ {loc}"


def generate(target: Target, project_root: Path) -> list[Scenario]:
    """§5's default scenario generator for one declared target.

    For a repository-context harness: the declared working directory, plus every
    directory beneath it whose location can change the instruction load set. For
    a skills harness the load set is the activated skills, not the cwd, so the
    target yields exactly one scenario.
    """
    reg = REGISTRY[target.profile_key]
    root = target.root(project_root)
    if not reg.cwd_sensitive:
        return [Scenario(target.profile_key, root, target.name,
                         target.parameters, target.state, target.context,
                         target.implied, target.delivery, target.external)]
    return [Scenario(target.profile_key, d, target.name,
                     target.parameters, target.state, target.context,
                     target.implied, target.delivery, target.external)
            for d in context_dirs(root)]


def load_set(scenario: Scenario) -> list[InputFile]:
    """The files this scenario's profile actually consumes."""
    reg = scenario.registered
    if reg.consumes == "context":
        if reg.bootstrap_roster:
            # CF-016: a loader whose candidate list is a literal in its own source
            # gets that list, in that order, including the absent-but-budgeted
            # slots. The shared union is for loaders that *search*.
            files = collect_bootstrap_files(
                scenario.root, reg.bootstrap_roster, reg.absent_consumes_budget)
        else:
            # Working directory first, then any ancestor file this loader walks for.
            # Order is the loader's own search order, which the profile relies on to
            # pick the nearest match (CF-011).
            files = (collect_context_files(scenario.root)
                     + collect_ancestors(scenario.root, reg.ancestor_walk))
        return with_loader_view(files, reg.source_newlines)
    # Skill loaders receive the frontmatter-stripped body; blocks stay in file
    # coordinates so verdicts cite the file a person edits.
    return [InputFile(f.name, f.path, f.file_text,
                      loader_view=strip_frontmatter(f.file_text))
            for f in collect_skills(scenario.root)]


# ---------------------------------------------------------------------------
# Constraint-level status
# ---------------------------------------------------------------------------

#: §10.4's blocking after-states under an initial-load requirement.
BLOCKING = (Status.NOT_DELIVERED, Status.PARTIAL, Status.AVAILABLE_ONLY_ON_REQUEST)


def aggregate(statuses: "list[Status]") -> Status:
    """One status for a constraint spanning several blocks.

    Order matters and is deliberately pessimistic — `DELIVERED_ON_LOAD` is only
    reported when *every* covered block is delivered on load:

    1. any `UNKNOWN`                         -> `UNKNOWN`   (A1 / §8)
    2. all delivered                         -> `DELIVERED_ON_LOAD`
    3. nothing delivered, all reachable      -> `AVAILABLE_ONLY_ON_REQUEST`
    4. nothing delivered at all              -> `NOT_DELIVERED`
    5. otherwise                             -> `PARTIAL`

    Rule 1 outranks everything because a single unmodeled step on the load path
    forbids a definite statement about the constraint as a whole.
    """
    if not statuses:
        return Status.UNKNOWN
    if any(s is Status.UNKNOWN for s in statuses):
        return Status.UNKNOWN
    if all(s is Status.DELIVERED_ON_LOAD for s in statuses):
        return Status.DELIVERED_ON_LOAD
    if not any(s is Status.DELIVERED_ON_LOAD or s is Status.PARTIAL for s in statuses):
        if all(s is Status.AVAILABLE_ONLY_ON_REQUEST for s in statuses):
            return Status.AVAILABLE_ONLY_ON_REQUEST
        return Status.NOT_DELIVERED
    return Status.PARTIAL


@dataclass
class ConstraintResult:
    """A critical constraint's delivery state in one scenario."""

    constraint: CriticalConstraint
    status: Status
    cause: Cause
    reason: str = ""
    retrieval: list[str] = field(default_factory=list)
    lost_excerpt: str = ""

    @property
    def id(self) -> str:
        return self.constraint.id

    @property
    def tracked(self) -> bool:
        return self.constraint.tracked

    @property
    def blocking(self) -> bool:
        return self.status in BLOCKING

    def as_dict(self) -> dict:
        return {"id": self.constraint.id or None,
                "tracked": self.constraint.tracked,
                "locator": self.constraint.locator,
                "form": self.constraint.form,
                "canonical_sha256": self.constraint.canonical_sha256,
                "status": self.status.value, "cause": self.cause.value,
                "reason": self.reason, "retrieval": self.retrieval,
                "lost_excerpt": self.lost_excerpt}


@dataclass
class ScenarioResult:
    """§9's evaluation of one scenario. Never merged with another profile's."""

    scenario: Scenario
    profile_result: ProfileResult | None
    fingerprint: FingerprintCheck
    markers: MarkerScan
    constraints: list[ConstraintResult] = field(default_factory=list)
    skipped: str = ""

    @property
    def profile_id(self) -> str:
        return self.scenario.profile_id

    def by_status(self, *states: Status) -> list[ConstraintResult]:
        return [c for c in self.constraints if c.status in states]

    @property
    def has_critical_failure(self) -> bool:
        return any(c.blocking for c in self.constraints)

    @property
    def has_indeterminate(self) -> bool:
        return any(c.status is Status.UNKNOWN for c in self.constraints)

    @property
    def signature(self) -> tuple:
        """What §5 compares when deciding whether a neighbour differs."""
        return tuple(sorted((c.constraint.label, c.status.value)
                            for c in self.constraints))


def evaluate_scenario(scenario: Scenario, *, allow_on_request: bool = False,
                      harness_root: Path | None = None) -> ScenarioResult:
    """Resolve every §3 critical constraint in one scenario.

    `harness_root` is the upstream harness checkout when one is available. §8:
    absent, results hold *under the declared qualified target profile* — which
    is the normal CI state, not a defect.
    """
    reg = scenario.registered
    fp = fingerprint_check(scenario.profile_key, harness_root)
    files = load_set(scenario)
    markers = scan_project([(f.name, f.file_text) for f in files])

    if markers.issues:
        # A malformed project is a configuration error, not a finding. No
        # verdicts are produced from it at all.
        return ScenarioResult(scenario, None, fp, markers,
                              skipped="configuration error in critical markers")
    if not files:
        return ScenarioResult(scenario, None, fp, markers,
                              skipped="no instruction files under this profile")

    profile = (reg.build(active=scenario.state.get("active_skills"))
               if reg.needs_declaration else reg.build())
    result = scan_under_profile(files, profile)

    # Block verdicts, keyed by (file name, block index), so a constraint's
    # extent can be resolved to the statuses of exactly its own blocks.
    verdicts = {(f.name, b.index): b for f in result.files for b in f.blocks}

    out: list[ConstraintResult] = []
    for c in markers.constraints:
        picked = [verdicts[(c.file, i)] for i in c.blocks if (c.file, i) in verdicts]
        status = aggregate([b.status for b in picked])
        if status is Status.DELIVERED_ON_LOAD:
            cause, reason, retrieval, lost = Cause.NONE, "", [], ""
        else:
            worst = next((b for b in picked if b.status in BLOCKING
                          or b.status is Status.UNKNOWN), picked[0] if picked else None)
            cause = worst.cause if worst else Cause.NOT_IN_LOAD_SET
            reason = worst.reason if worst else "no block resolved for this marker"
            retrieval = list(worst.retrieval) if worst else []
            lost = worst.lost_excerpt if worst else ""
        if (status is Status.AVAILABLE_ONLY_ON_REQUEST and allow_on_request):
            reason = (reason or "") + " (allowed by --allow-on-request)"
        # §8: a changed or never-recorded loader surface withholds definite
        # statuses, exactly as an unmodeled step does.
        if not fp.definite_allowed and status is not Status.UNKNOWN:
            status, cause = Status.UNKNOWN, Cause.NONE
            reason = fp.message
        out.append(ConstraintResult(c, status, cause, reason, retrieval, lost))

    return ScenarioResult(scenario, result, fp, markers, out)


# ---------------------------------------------------------------------------
# §5 output folding
# ---------------------------------------------------------------------------

def expand_set(results: "list[ScenarioResult]",
               regressed: "set[str] | None" = None) -> set[int]:
    """Indices of the scenarios §5 says to expand in human-readable output.

    Expanded: a critical failure, an indeterminate critical result, a result
    that differs from an equivalent neighbouring scenario, or involvement in an
    Impact regression. Everything else is summarised **within the same profile**.

    CF-009: the **first** scenario of each profile is always expanded, because it
    is the one every later scenario is folded *against*. Without that, a healthy
    repository with a single scenario folded its only result and `verify` printed
    no per-constraint line at all — while the folded-count line claimed "no
    difference from a neighbouring scenario", of which there was none. A report
    that says nothing about what it checked is not a summary.
    """
    regressed = regressed or set()
    expand: set[int] = set()
    # Neighbours are scenarios of the same profile, in generation order.
    by_profile: dict[str, list[int]] = {}
    for i, r in enumerate(results):
        by_profile.setdefault(r.profile_id, []).append(i)
        if r.has_critical_failure or r.has_indeterminate or r.skipped:
            expand.add(i)
        if r.scenario.target in regressed or r.profile_id in regressed:
            expand.add(i)
    for idxs in by_profile.values():
        expand.add(idxs[0])
        for a, b in zip(idxs, idxs[1:]):
            if results[a].signature != results[b].signature:
                expand.add(a)
                expand.add(b)
    return expand
