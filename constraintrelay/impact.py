"""`constraintrelay impact` — Brief v0.6 §10, the v1 hero feature.

It compares critical constraints across repository revisions, target
configuration revisions, qualified harness/profile revisions, or any
combination:

    Before = repository revision + target scenario/profile
    After  = repository revision + target scenario/profile

so both of these are supported, and so is changing both at once:

    repository changed, harness target unchanged
    repository unchanged, harness/profile changed

Two classifications are kept strictly apart, per §10.1: what happened to the
*constraint* (`UNCHANGED` / `MODIFIED` / `ADDED` / `REMOVED`) and what happened
to its *delivery*. A rewritten constraint is not a CI failure in v1 — §10.2,
ConstraintFlow v1 is not a policy-approval system — and only the delivery side
can block.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .scenario import BLOCKING, ScenarioResult
from .verdict import Status

# §10.1 — constraint categories, independent of delivery status.
UNCHANGED, MODIFIED, ADDED, REMOVED = "UNCHANGED", "MODIFIED", "ADDED", "REMOVED"

# Delivery classifications.
CRITICAL_REMOVED = "CRITICAL_REMOVED"
NEW_CRITICAL_NOT_DELIVERED = "NEW_CRITICAL_NOT_DELIVERED"
NEW_CRITICAL_INDETERMINATE = "NEW_CRITICAL_INDETERMINATE"
DELIVERY_REGRESSION = "DELIVERY_REGRESSION"
INDETERMINATE_CHANGE = "INDETERMINATE_CHANGE"
PERSISTING_DELIVERY_DEBT = "PERSISTING_DELIVERY_DEBT"
RESOLVED = "RESOLVED"
NONE = ""

#: §10.6 — the confirmed regression transitions, exactly as listed. The list is
#: not extended here: `AVAILABLE_ONLY_ON_REQUEST -> NOT_DELIVERED` is a real
#: worsening but §10.5/§10.7 put an already-blocking baseline outside Impact's
#: regression set, so it is reported as debt that worsened rather than silently
#: promoted to a CI failure this brief did not authorise. `worsened` carries it
#: into the output so nothing is lost.
REGRESSIONS: set[tuple[Status, Status]] = {
    (Status.DELIVERED_ON_LOAD, Status.PARTIAL),
    (Status.DELIVERED_ON_LOAD, Status.NOT_DELIVERED),
    (Status.DELIVERED_ON_LOAD, Status.AVAILABLE_ONLY_ON_REQUEST),
    (Status.PARTIAL, Status.NOT_DELIVERED),
}

#: Severity order used only to say whether blocking debt got worse, never to
#: decide an exit code.
_SEVERITY = {
    Status.DELIVERED_ON_LOAD: 0,
    Status.AVAILABLE_ONLY_ON_REQUEST: 1,
    Status.PARTIAL: 2,
    Status.NOT_DELIVERED: 3,
}


@dataclass
class ConstraintImpact:
    """One tracked id's before/after record."""

    id: str
    category: str
    classification: str
    before_status: Status | None
    after_status: Status | None
    locator: str = ""
    reason: str = ""
    approved: bool = False
    worsened: bool = False

    @property
    def blocking(self) -> bool:
        """§10.7: a confirmed newly introduced blocking change."""
        if self.classification == CRITICAL_REMOVED:
            return not self.approved
        return self.classification in (NEW_CRITICAL_NOT_DELIVERED,
                                       DELIVERY_REGRESSION)

    @property
    def indeterminate(self) -> bool:
        return self.classification in (NEW_CRITICAL_INDETERMINATE,
                                       INDETERMINATE_CHANGE)

    def as_dict(self) -> dict:
        return {"id": self.id, "category": self.category,
                "classification": self.classification or None,
                "before": self.before_status.value if self.before_status else None,
                "after": self.after_status.value if self.after_status else None,
                "locator": self.locator, "reason": self.reason,
                "approved": self.approved, "worsened": self.worsened,
                "blocking": self.blocking, "indeterminate": self.indeterminate}


@dataclass
class ScenarioImpact:
    """Impact for one paired scenario. Never merged across profiles (§15)."""

    key: str                        # target name + relative location
    before_profile: str
    after_profile: str
    entries: list[ConstraintImpact] = field(default_factory=list)
    note: str = ""

    @property
    def profile_changed(self) -> bool:
        return self.before_profile != self.after_profile

    @property
    def blocking(self) -> list[ConstraintImpact]:
        return [e for e in self.entries if e.blocking]

    @property
    def indeterminate(self) -> list[ConstraintImpact]:
        return [e for e in self.entries if e.indeterminate]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.category] = out.get(e.category, 0) + 1
        return out


def classify(category: str, before: Status | None, after: Status | None,
             *, approved: bool = False) -> tuple[str, str, bool]:
    """Delivery classification, its reason, and whether blocking debt worsened."""
    if category == REMOVED:
        return (CRITICAL_REMOVED,
                "a tracked critical constraint is gone from the After revision; "
                "intentional removal requires explicit approval at the CI "
                "invocation (§10.3)", False)

    if category == ADDED:
        if after is Status.UNKNOWN:
            return (NEW_CRITICAL_INDETERMINATE,
                    "a new critical constraint's delivery state is "
                    "indeterminate (§10.4)", False)
        if after in BLOCKING:
            return (NEW_CRITICAL_NOT_DELIVERED,
                    f"a new critical constraint is {after.value} under an "
                    f"initial-load requirement (§10.4)", False)
        return (NONE, "", False)

    # UNCHANGED or MODIFIED: the delivery side is judged on the transition.
    if after is Status.UNKNOWN:
        if before is Status.UNKNOWN:
            return (NONE, "indeterminate in both revisions", False)
        return (INDETERMINATE_CHANGE,
                f"{before.value} -> UNKNOWN is not a confirmed regression "
                f"(§10.6)", False)
    if before is Status.UNKNOWN:
        return (NONE, f"UNKNOWN -> {after.value}", False)
    if (before, after) in REGRESSIONS:
        return (DELIVERY_REGRESSION, f"{before.value} -> {after.value} (§10.6)",
                False)
    if before in BLOCKING and after in BLOCKING:
        worse = _SEVERITY.get(after, 0) > _SEVERITY.get(before, 0)
        detail = (f"{before.value} -> {after.value}" if before is not after
                  else f"still {after.value}")
        return (PERSISTING_DELIVERY_DEBT,
                detail + ("; worse than the baseline but outside §10.6's "
                          "regression set, so reported rather than blocking"
                          if worse else "; existing loss that remains lost "
                                        "(§10.5)"),
                worse)
    if before in BLOCKING and after is Status.DELIVERED_ON_LOAD:
        return (RESOLVED, f"{before.value} -> {after.value}", False)
    return (NONE, "", False)


def _scenario_key(result: ScenarioResult, project_root: Path) -> str:
    """Pairing key: the declared target and the relative location.

    Deliberately *not* the profile id, because §10 has to support "repository
    unchanged, harness/profile changed" — under which the two sides have
    different profiles by construction and would never pair.

    An **implied** target does not contribute its name either, and that is the
    whole of CF-008. `--profile openclaw` synthesises a target called `openclaw`,
    so `impact --before-profile openclaw --after-profile hermes-agent` produced
    the keys `openclaw::.` and `hermes-agent::.`, which never paired. The two
    sides were then reported as one scenario that only exists Before and another
    that only exists After — i.e. `CRITICAL_REMOVED` **and**
    `NEW_CRITICAL_NOT_DELIVERED` for a constraint that had not moved, on the one
    comparison the flags exist to express. Two false definite classifications,
    which §11 calls the worst class of bug.
    """
    try:
        rel = result.scenario.root.relative_to(project_root)
    except ValueError:
        rel = result.scenario.root
    loc = "." if str(rel) in ("", ".") else str(rel)
    name = "" if result.scenario.implied else result.scenario.target
    return f"{name or '-'}::{loc}"


def compare(before: list[ScenarioResult], after: list[ScenarioResult],
            before_root: Path, after_root: Path,
            approved_removals: "set[str] | None" = None) -> list[ScenarioImpact]:
    """Pair scenarios and classify every tracked constraint in each pair."""
    approved = approved_removals or set()
    b_map = {_scenario_key(r, before_root): r for r in before}
    a_map = {_scenario_key(r, after_root): r for r in after}

    out: list[ScenarioImpact] = []
    for key in sorted(set(b_map) | set(a_map)):
        b, a = b_map.get(key), a_map.get(key)
        si = ScenarioImpact(
            key,
            b.profile_id if b else "(scenario absent)",
            a.profile_id if a else "(scenario absent)")
        if b is None:
            si.note = ("scenario exists only in the After revision; every "
                       "constraint in it is treated as ADDED")
        elif a is None:
            si.note = ("scenario exists only in the Before revision; every "
                       "constraint in it is treated as REMOVED")
        elif si.profile_changed:
            si.note = (f"harness/profile changed for this scenario: "
                       f"{si.before_profile} -> {si.after_profile}")

        b_tracked = {c.id: c for c in (b.constraints if b else []) if c.tracked}
        a_tracked = {c.id: c for c in (a.constraints if a else []) if c.tracked}

        for cid in sorted(set(b_tracked) | set(a_tracked)):
            bc, ac = b_tracked.get(cid), a_tracked.get(cid)
            if bc is None:
                category = ADDED
            elif ac is None:
                category = REMOVED
            elif bc.constraint.canonical_sha256 != ac.constraint.canonical_sha256:
                category = MODIFIED       # §10.2: text change alone never fails
            else:
                category = UNCHANGED
            cls, reason, worsened = classify(
                category,
                bc.status if bc else None,
                ac.status if ac else None,
                approved=cid in approved)
            out_entry = ConstraintImpact(
                id=cid, category=category, classification=cls,
                before_status=bc.status if bc else None,
                after_status=ac.status if ac else None,
                locator=(ac or bc).constraint.locator,
                reason=reason, approved=cid in approved, worsened=worsened)
            si.entries.append(out_entry)
        out.append(si)
    return out


def untracked_count(results: list[ScenarioResult]) -> int:
    """§3: legacy markers with no id take no part in Impact. Counted, not used."""
    return sum(1 for r in results for c in r.constraints if not c.tracked)


# ---------------------------------------------------------------------------
# Materializing a Before revision
# ---------------------------------------------------------------------------

class RevisionError(Exception):
    """Could not materialize a revision. §10.7 exit 1."""


def materialize(repo: Path, rev: str, into: Path) -> Path:
    """Export *rev* of *repo* into *into* with `git archive`.

    `git archive` is used rather than `checkout` or `worktree` because it cannot
    modify the caller's repository — Impact must never be able to disturb the
    tree it is reporting on.
    """
    archive = into / "rev.tar"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", "-o",
             str(archive), rev],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RevisionError(f"git archive {rev} failed: {exc}") from exc
    if proc.returncode != 0:
        raise RevisionError(
            f"git archive {rev} failed: {proc.stderr.strip() or 'unknown error'}")
    dest = into / "tree"
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        # `filter="data"` refuses absolute paths, `..` components, links and
        # device nodes — an exported revision is untrusted input like any other.
        tf.extractall(dest, filter="data")
    archive.unlink(missing_ok=True)
    return dest


def revision_workspace() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="constraintrelay-rev-")
