"""Target configuration as a reviewed input — v0.7 §9.

> ConstraintFlow must not allow: *"make the check green by deleting the target."*

Everything else in the product measures a repository against a target. That makes
the target itself the cheapest thing to attack, and the attack does not even have
to be deliberate — a tidy-up commit that drops a `[[target]]` makes a failing
build pass, with a smaller diff than the fix would have been. So the set of
evaluated targets is recorded, and a later run compares against the record.

Two classifications block, per §9:

    TARGET_REMOVED              a previously evaluated target is gone
    TARGET_COVERAGE_REDUCED     it still exists but checks less than it did

and two are reported without blocking, because §9 permits them and only requires
that they stay visible:

    TARGET_PROFILE_CHANGED      a different loader surface is being evaluated
    TARGET_COVERAGE_INCREASED   a parameter was raised, a scenario was added

The baseline is `.constraintrelay/targets.baseline.json`, written by an explicit
command. It is deliberately not auto-created on first run: a baseline that writes
itself whenever it is missing can be reset by deleting it, which is the same hole
one indirection further along.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig
from .registry import REGISTRY
from .scenario import generate
from .version import IMPLEMENTATION

BASELINE_NAME = "targets.baseline.json"

TARGET_REMOVED = "TARGET_REMOVED"
TARGET_COVERAGE_REDUCED = "TARGET_COVERAGE_REDUCED"
TARGET_PROFILE_CHANGED = "TARGET_PROFILE_CHANGED"
TARGET_COVERAGE_INCREASED = "TARGET_COVERAGE_INCREASED"
TARGET_ADDED = "TARGET_ADDED"

BLOCKING = (TARGET_REMOVED, TARGET_COVERAGE_REDUCED)


class BaselineError(Exception):
    """An unreadable baseline. A configuration error, so exit 1."""


def stable_target_id(target) -> str:
    """Item 6's stable target ID: profile + mirror path + working directory.

    Deliberately not the name and not a random uuid. A name is renamed; a uuid has
    to be stored somewhere before it means anything. This is derived from what the
    target actually evaluates, so the same evaluation keeps its id across a rename
    and a genuinely different evaluation gets a different one.
    """
    import hashlib
    basis = (f"{REGISTRY[target.profile_key].profile_id}\x00"
             f"{target.delivery.get('mirror_path', '')}\x00{target.cwd}")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def baseline_path(project_root: Path) -> Path:
    from .store import STORE_DIR
    return project_root / STORE_DIR / BASELINE_NAME


@dataclass(frozen=True)
class TargetRecord:
    name: str
    profile_id: str
    cwd: str
    parameters: dict
    context: dict
    scenarios: tuple[str, ...]
    #: Item 6: the stable id, so a baseline survives a rename legibly.
    target_id: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "target_id": self.target_id,
                "profile_id": self.profile_id, "cwd": self.cwd,
                "parameters": self.parameters, "context": self.context,
                "scenarios": list(self.scenarios)}


@dataclass(frozen=True)
class TargetChange:
    classification: str
    name: str
    detail: str
    approved: bool = False

    @property
    def blocking(self) -> bool:
        return self.classification in BLOCKING and not self.approved


@dataclass
class Baseline:
    implementation: str = IMPLEMENTATION
    recorded_at: str = ""
    targets: dict[str, TargetRecord] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"implementation": self.implementation,
                "recorded_at": self.recorded_at,
                "targets": {k: v.as_dict() for k, v in sorted(self.targets.items())}}


def snapshot(cfg: ProjectConfig, project_root: Path, *, when: str = "") -> Baseline:
    """What this configuration currently evaluates."""
    out = Baseline(recorded_at=when)
    for target in cfg.targets:
        scenarios = tuple(s.label(project_root) for s in generate(target, project_root))
        out.targets[target.name] = TargetRecord(
            name=target.name,
            target_id=stable_target_id(target),
            profile_id=REGISTRY[target.profile_key].profile_id,
            cwd=target.cwd,
            parameters=dict(target.parameters),
            context=dict(target.context),
            scenarios=scenarios)
    return out


def load(path: Path) -> Baseline:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise BaselineError(f"{path}: cannot read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path}: not valid JSON: {exc}") from exc
    out = Baseline(implementation=raw.get("implementation", ""),
                   recorded_at=raw.get("recorded_at", ""))
    for name, rec in (raw.get("targets") or {}).items():
        out.targets[name] = TargetRecord(
            name=name,
            target_id=str(rec.get("target_id", "")),
            profile_id=rec.get("profile_id", ""),
            cwd=rec.get("cwd", "."),
            parameters=dict(rec.get("parameters", {})),
            context=dict(rec.get("context", {})),
            scenarios=tuple(rec.get("scenarios", ())))
    return out


APPROVALS_NAME = "target_approvals.json"


def approvals_path(project_root: Path) -> Path:
    from .store import STORE_DIR
    return project_root / STORE_DIR / APPROVALS_NAME


@dataclass(frozen=True)
class Approval:
    """A persisted, reasoned approval for a blocking target change.

    Pre-merge ruling item 6: *target removal/coverage reduction requires an explicit
    persisted approval record with reason*. A command-line `--approve staging` was
    not enough — it leaves nothing behind, so the next reviewer sees a green build
    and no record of what was waived or why. Persisted here, an approval is a file in
    the repository: it appears in a diff, it can be revoked, and it carries the
    sentence somebody had to write.
    """

    target: str
    reason: str
    approved_at: str
    baseline_recorded_at: str = ""
    #: Qualification ruling item 6: a STABLE target id beside the human-facing
    #: name. The name is the thing a project renames; an approval keyed only on it
    #: either follows the rename silently or is lost silently, and both are worse
    #: than being explicit about which identity was waived.
    target_id: str = ""

    def as_dict(self) -> dict:
        return {"target": self.target, "target_id": self.target_id,
                "reason": self.reason,
                "approved_at": self.approved_at,
                "baseline_recorded_at": self.baseline_recorded_at}


def load_approvals(path: Path) -> dict[str, Approval]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"{path}: cannot read approvals: {exc}") from exc
    out: dict[str, Approval] = {}
    for entry in raw.get("approvals", []):
        target = str(entry.get("target", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        if not target:
            raise BaselineError(f"{path}: an approval has no `target`")
        if not reason:
            # An approval with no reason is the thing this record exists to prevent.
            raise BaselineError(
                f"{path}: the approval for {target!r} has no `reason`. §9 makes a "
                f"target change a reviewed input, and an unexplained waiver is not "
                f"a review.")
        out[target] = Approval(target, reason,
                               str(entry.get("approved_at", "")),
                               str(entry.get("baseline_recorded_at", "")),
                               str(entry.get("target_id", "")))
    return out


def write_approvals(path: Path, approvals: dict[str, Approval]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": ("Persisted approvals for blocking target changes (§9, pre-merge "
                 "ruling item 6). Each entry waives one target's removal or "
                 "coverage reduction and must carry a reason. Deleting an entry "
                 "restores the block."),
        "implementation": IMPLEMENTATION,
        "approvals": [a.as_dict() for a in
                      sorted(approvals.values(), key=lambda a: a.target)],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def compare(before: Baseline, after: Baseline, *,
            approved: "set[str] | None" = None) -> list[TargetChange]:
    """§9's classification of a configuration change."""
    ok = approved or set()
    changes: list[TargetChange] = []

    for name, old in sorted(before.targets.items()):
        new = after.targets.get(name)
        if new is None:
            changes.append(TargetChange(
                TARGET_REMOVED, name,
                f"target {name!r} was evaluated in the baseline "
                f"({old.profile_id}, {len(old.scenarios)} scenario(s)) and is no "
                f"longer declared", name in ok))
            continue
        if new.profile_id != old.profile_id:
            changes.append(TargetChange(
                TARGET_PROFILE_CHANGED, name,
                f"{old.profile_id} -> {new.profile_id}", name in ok))

        lost = [s for s in old.scenarios if s not in new.scenarios]
        gained = [s for s in new.scenarios if s not in old.scenarios]
        # A profile change renames every scenario label, so scenario membership is
        # only a coverage signal when the profile is the same. Otherwise the
        # profile change itself is the reported fact and counting the renamed
        # labels as losses would double-report one edit as four.
        if lost and new.profile_id == old.profile_id:
            changes.append(TargetChange(
                TARGET_COVERAGE_REDUCED, name,
                f"{len(lost)} scenario(s) previously checked are no longer "
                f"generated: {', '.join(lost[:4])}"
                + (" …" if len(lost) > 4 else ""), name in ok))
        elif gained and new.profile_id == old.profile_id:
            changes.append(TargetChange(
                TARGET_COVERAGE_INCREASED, name,
                f"{len(gained)} new scenario(s): {', '.join(gained[:4])}"
                + (" …" if len(gained) > 4 else "")))

        for key in sorted(set(old.parameters) | set(new.parameters)):
            was, now = old.parameters.get(key), new.parameters.get(key)
            if was == now:
                continue
            if now is None:
                changes.append(TargetChange(
                    TARGET_COVERAGE_REDUCED, name,
                    f"parameter {key}={was!r} was declared in the baseline and is "
                    f"now undeclared, so the endpoint it pinned is no longer "
                    f"checked", name in ok))
            elif isinstance(was, int) and isinstance(now, int) \
                    and not isinstance(was, bool) and not isinstance(now, bool):
                # §9: raising a context window is allowed and must stay visible.
                # Lowering it narrows what was checked, which is a reduction.
                kind = TARGET_COVERAGE_INCREASED if now > was else TARGET_COVERAGE_REDUCED
                changes.append(TargetChange(
                    kind, name, f"parameter {key}: {was} -> {now}",
                    name in ok if kind == TARGET_COVERAGE_REDUCED else False))
            else:
                changes.append(TargetChange(
                    TARGET_COVERAGE_REDUCED, name,
                    f"parameter {key}: {was!r} -> {now!r}; a non-numeric change "
                    f"cannot be shown to widen coverage, so it is treated as a "
                    f"reduction until approved", name in ok))

        for key in sorted(set(old.context) | set(new.context)):
            was, now = old.context.get(key), new.context.get(key)
            if was != now:
                # A narrower declared context can only ever *exclude* constraints
                # (§2.4), so this is a coverage question and not a cosmetic one.
                changes.append(TargetChange(
                    TARGET_COVERAGE_REDUCED, name,
                    f"declared context {key}: {was!r} -> {now!r}; a changed context "
                    f"changes which constraints are applicable", name in ok))

    for name in sorted(set(after.targets) - set(before.targets)):
        changes.append(TargetChange(
            TARGET_ADDED, name,
            f"new target {name!r} ({after.targets[name].profile_id})"))
    return changes
