"""The Delivery Receipt — v0.7 §5, §6, §7, §8.

§5 defines what a v1 receipt means, and most of this module exists to keep it
from meaning more than that:

> Under qualified harness profile X, version/fingerprint Y, scenario Z, the
> generated capsule is predicted and replay-verified to be delivered on initial
> load according to the qualified loader model.

Not an observation of a running agent. Initial load only. Nothing about
compaction, retention or compliance. The four phrases §5 forbids are listed in
`FORBIDDEN` and a test asserts no renderer here can emit them — a wording rule
that is only documented is one `.format()` away from being broken.

§6 is why a receipt is two computations and not one
--------------------------------------------------
A capsule that delivers its own constraints by displacing somebody else's is not
a success. So:

* **Part A** — were all required applicable constraints delivered?
* **Part B** — did inserting the capsule take initial-load delivery away from
  instruction material that had it?

Part B is computed by taking the *before* blocks and testing them against the
*after* predicted delivery. That avoids re-segmenting the mirrored file after
insertion, where every block index downstream of the capsule would have shifted
and a naive index join would compare unrelated blocks. The question §6 actually
asks is about the before-material, so the before-material is what gets asked.

§10's version identity applies here too: both sides of the comparison come from
one `issue()` call in one process, and `IMPLEMENTATION` is recorded in the receipt
so that a stored receipt can be checked rather than assumed.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import qualification as qual_mod
from . import store as store_mod
from . import naming
from .applicability import CLAIM, Context
from .blocks import segment
from .capsule import Capsule, MirrorError, build as build_capsule, check_mirror, write_region
from .fingerprint import FingerprintCheck, check as fingerprint_check
from .markers import scan_project
from .profiles import InputFile, Prediction, block_needle
from .registry import measure_units
from .scenario import Scenario, load_set
from .verdict import Cause, Status, resolve_block, resolve_block_spans
from .version import IMPLEMENTATION

ESTABLISHED = "DELIVERY_ESTABLISHED"
FAILED = "DELIVERY_FAILED"
INDETERMINATE = "DELIVERY_INDETERMINATE"

#: §5's prohibited vocabulary. Checked by `tests/test_v07_contract.py` against
#: every line this module can print.
FORBIDDEN = ("runtime receipt", "guaranteed forever",
             "remains present after compaction", "compliance established")

#: The sentence §5 requires a receipt to carry about its own reach.
SCOPE_NOTE = ("This receipt covers initial load only. Context compaction, "
              "summarization, later context rewriting and long-session retention "
              "are outside v1.")

#: How far a status can fall before it counts as damage. §6 names the destination
#: states; the ordering is what lets a PARTIAL -> NOT_DELIVERED slide be reported
#: as well, without treating it as the §6 failure (which is specifically about
#: *losing* initial-load delivery).
_RANK = {
    Status.DELIVERED_ON_LOAD: 3,
    Status.PARTIAL: 2,
    Status.AVAILABLE_ONLY_ON_REQUEST: 1,
    Status.NOT_DELIVERED: 0,
    Status.CONDITIONAL: -1,
    Status.UNKNOWN: -1,
}


@dataclass(frozen=True)
class MemberDelivery:
    """Part A: one capsule member's predicted fate."""

    id: str
    constraint_version: int
    status: Status
    reason: str
    machine_enforcement_declared: bool = False
    #: Pre-merge ruling item 6: the receipt carries the constraint's hash, not
    #: only its id, so a stored receipt can be checked against a later store.
    canonical_sha256: str = ""
    #: Which mechanism decided a non-delivered status. Owner ruling 2026-10-02
    #: needs `TRANSFORMED_BY_DIGEST` to be visible in the receipt and not collapsed
    #: into a bare `PARTIAL`: "the only route that carried this rewrote it" and "the
    #: cut landed mid-constraint" call for different author actions.
    cause: Cause = Cause.NONE

    @property
    def delivered(self) -> bool:
        return self.status is Status.DELIVERED_ON_LOAD

    def as_dict(self) -> dict:
        return {"id": self.id, "constraint_version": self.constraint_version,
                "canonical_sha256": self.canonical_sha256,
                "status": self.status.value, "reason": self.reason,
                "cause": self.cause.value,
                "machine_enforcement_declared": self.machine_enforcement_declared}


@dataclass(frozen=True)
class Regression:
    """Part B: one piece of pre-existing material the capsule cost something."""

    file: str
    locator: str
    label: str
    before: Status
    after: Status
    protected: bool
    #: Why it is protected. Pre-merge ruling item 6 freezes protected content as
    #: *author-marked critical constraints **plus** explicitly configured protected
    #: blocks*, so the two reasons are distinguished rather than merged — an author
    #: who marked a clause critical did not also have to list its file.
    protection: str = ""

    @property
    def lost_delivery(self) -> bool:
        """§6's exact condition: it was delivered on initial load, and now is not."""
        return self.before is Status.DELIVERED_ON_LOAD and \
            self.after is not Status.DELIVERED_ON_LOAD

    def as_dict(self) -> dict:
        return {"file": self.file, "locator": self.locator, "label": self.label,
                "before": self.before.value, "after": self.after.value,
                "protected": self.protected, "protection": self.protection or None,
                "lost_delivery": self.lost_delivery}


@dataclass
class Receipt:
    """§5's record. Every field §5 lists is present and named."""

    # --- §5 required fields ---------------------------------------------------
    profile: str
    profile_evidence_level: str
    loader_fingerprint: FingerprintCheck
    scenario: str
    required_ids: list[str] = field(default_factory=list)
    delivered_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    partial_ids: list[str] = field(default_factory=list)
    indeterminate_ids: list[str] = field(default_factory=list)
    capsule_size: int = 0
    capsule_unit: str = ""
    qualification_evidence: str = ""
    # --- outcome and workings -------------------------------------------------
    outcome: str = INDETERMINATE
    members: list[MemberDelivery] = field(default_factory=list)
    regressions: list[Regression] = field(default_factory=list)
    suppressed_sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    missing_declarations: list[str] = field(default_factory=list)
    capsule: Capsule | None = None
    mirror_target: str = ""
    implementation: str = IMPLEMENTATION
    # --- pre-merge ruling ----------------------------------------------------
    #: Item 1: the adapter's §14 gate state for this invocation.
    qualification: "qual_mod.Qualification | None" = None
    #: Item 1: named missing qualification evidence, when a gate is open.
    missing_qualification: list[str] = field(default_factory=list)
    #: Item 1: what the delivery computation *would* have said if qualification
    #: were complete. Populated only when the caller asked for development
    #: evidence, and never used to set `outcome` — see `issue()`.
    development_evidence_only: dict = field(default_factory=dict)
    #: Item 6: a digest of every input this receipt was computed from.
    input_fingerprint: str = ""
    input_fingerprint_basis: list[str] = field(default_factory=list)
    # --- qualification ruling item 6: the rest of the required field set -----
    #: The ConstraintFlow commit, not only the analyzer version. A stored receipt
    #: naming a version but not a commit cannot be tied back to a tree.
    constraintflow_commit: str = ""
    #: Separate fingerprints, because "the inputs changed" is too coarse to act on:
    #: a store edit, a source edit and a config edit want different responses.
    store_fingerprint: str = ""
    source_fingerprint: str = ""
    config_fingerprint: str = ""
    #: A reference to the evidence artifact the qualification state was computed
    #: from, so a receipt can be re-checked rather than believed.
    evidence_reference: dict = field(default_factory=dict)
    #: Item 6 names this as its own receipt field rather than leaving it inside the
    #: regression list: a reader needs the verdict before the detail.
    protected_content_regression: str = ""
    #: The stable target id (item 6), distinct from the human-facing target name.
    target_id: str = ""

    @property
    def established(self) -> bool:
        return self.outcome == ESTABLISHED

    @property
    def qualification_complete(self) -> bool:
        return self.qualification is None or self.qualification.complete

    @property
    def protected_losses(self) -> list[Regression]:
        return [r for r in self.regressions if r.protected and r.lost_delivery]

    def as_dict(self) -> dict:
        return {
            "receipt": "constraintflow/v0.7 §5",
            "outcome": self.outcome,
            "claim": CLAIM,
            "basis": ("predicted and replay-verified against the qualified loader "
                      "model; not an observation from a live running agent"),
            "covers": SCOPE_NOTE,
            "implementation": self.implementation,
            "profile": self.profile,
            "profile_evidence_level": self.profile_evidence_level,
            "loader_fingerprint": self.loader_fingerprint.as_dict(),
            "qualification": (self.qualification.as_dict()
                              if self.qualification else None),
            "missing_qualification_evidence": self.missing_qualification,
            "development_evidence_only": self.development_evidence_only or None,
            "input_fingerprint": self.input_fingerprint,
            "input_fingerprint_basis": self.input_fingerprint_basis,
            "constraintflow_commit": self.constraintflow_commit,
            "store_fingerprint": self.store_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "evidence_reference": self.evidence_reference or None,
            "protected_content_regression": self.protected_content_regression,
            "target_id": self.target_id,
            "scenario": self.scenario,
            "mirror_target": self.mirror_target or None,
            "required_constraint_ids": self.required_ids,
            "delivered_constraint_ids": self.delivered_ids,
            "missing_constraint_ids": self.missing_ids,
            "partial_constraint_ids": self.partial_ids,
            "indeterminate_constraint_ids": self.indeterminate_ids,
            "capsule_size": {"value": self.capsule_size, "unit": self.capsule_unit},
            "qualification_evidence": self.qualification_evidence,
            "missing_declarations": self.missing_declarations,
            "environment_regression": [r.as_dict() for r in self.regressions],
            "suppressed_sources": self.suppressed_sources,
            "reasons": self.reasons,
            "capsule": self.capsule.as_dict() if self.capsule else None,
            "members": [m.as_dict() for m in self.members],
        }

    def render(self) -> str:
        fp = self.loader_fingerprint
        # `scenario` already carries the profile id, so the header does not repeat
        # it: §15's rule is that every figure is labelled with its profile, not
        # that the label appears twice.
        out = [f"{self.outcome}  {self.scenario}",
               f"  claim: {CLAIM}",
               f"  evidence level: {self.profile_evidence_level}",
               f"  loader surface: {fp.state} — {fp.message}",
               f"  qualification: "
               + (self.qualification.summary() if self.qualification
                  else "not evaluated"),
               f"  analyzer: {self.implementation}",
               f"  input fingerprint: {self.input_fingerprint[:16]} over "
               f"{len(self.input_fingerprint_basis)} input(s) "
               f"(store {self.store_fingerprint or '-'}, "
               f"source {self.source_fingerprint or '-'}, "
               f"config {self.config_fingerprint or '-'})",
               #: Labelled with the PRODUCT name; the field keeps its own
               #: spelling, which the Owner's ruling of 2026-10-25 deferred to
               #: a separate check. A label that reads as a command is worse
               #: than one that reads as a product.
               f"  {naming.PRODUCT} commit: "
               f"{self.constraintflow_commit[:12]}",
               f"  target id: {self.target_id}",
               f"  protected content: {self.protected_content_regression or '?'}",
               f"  evidence: {self.evidence_reference.get('artifact') or 'none'}"
               + (f" @ {self.evidence_reference.get('digest')}"
                  if self.evidence_reference.get('digest') else ""),
               f"  capsule: {len(self.required_ids)} constraint(s), "
               f"{self.capsule_size:,} {self.capsule_unit}"
               + (f", mirrored in {self.mirror_target}" if self.mirror_target else ""),
               f"  {SCOPE_NOTE}"]
        if self.required_ids:
            out.append(f"  required:      {', '.join(self.required_ids)}")
        else:
            out.append("  required:      none applicable under this declared context")
        if self.delivered_ids:
            out.append(f"  delivered:     {', '.join(self.delivered_ids)}")
        for label, ids in (("partial", self.partial_ids),
                           ("missing", self.missing_ids),
                           ("indeterminate", self.indeterminate_ids)):
            if ids:
                out.append(f"  {label + ':':<14} {', '.join(ids)}")
        for m in self.members:
            if not m.delivered:
                out.append(f"    {m.status.value:<26} {m.id}: {m.reason}")
            if m.machine_enforcement_declared:
                # §7: recorded, and explicitly not a reason to omit anything.
                out.append(f"    accounting: {m.id} declares machine enforcement; "
                           f"§7 keeps it in the model-visible capsule anyway")
        for g in (self.qualification.open_gates if self.qualification else ()):
            out.append(f"    OPEN GATE {g.state:<17} {g.id}: {g.evidence}")
        if self.development_evidence_only:
            out.append("    DEVELOPMENT EVIDENCE ONLY — not a receipt: "
                       f"{self.development_evidence_only.get('would_be', '?')}, "
                       f"{self.development_evidence_only.get('note', '')}")
        for r in self.regressions:
            tag = "PROTECTED REGRESSION" if (r.protected and r.lost_delivery) \
                else "regression"
            where = f" [{r.protection}]" if r.protected else ""
            out.append(f"    {tag:<26} {r.file}:{r.locator}{where} "
                       f"{r.before.value} -> {r.after.value}: {r.label}")
        for s in self.suppressed_sources:
            out.append(f"    SUPPRESSED SOURCE          {s}")
        for d in self.missing_declarations:
            out.append(f"  cannot establish delivery until: {d} is declared.")
        for reason in self.reasons:
            out.append(f"  {reason}")
        if self.qualification_evidence:
            out.append(f"  qualification evidence: {self.qualification_evidence}")
        return "\n".join(out)


# ---------------------------------------------------------------------------

def _locate_mirror(files: list[InputFile], mirror_path: str) -> InputFile | None:
    """The load-set entry the capsule is mirrored into, matched three ways.

    Context files are discovered under their bare filename, skills under their
    directory name, and a project may spell the mirror path with directories. All
    three spellings have to resolve to the same entry or a correct configuration
    would read as a placement failure.
    """
    wanted = mirror_path.replace("\\", "/")
    for f in files:
        if f.name == wanted:
            return f
    for f in files:
        p = f.path.replace("\\", "/")
        if p == wanted or p.endswith("/" + wanted):
            return f
    return None


def _resolve(profile, f: InputFile, pred: Prediction, text: str,
             start: int, end: int) -> tuple[Status, str, Cause]:
    """One block's fate under one prediction, by route where routes exist.

    Owner ruling 2026-10-02, item 3, applied **here** and not only in `scan`. The
    span layer added for CF-012 went into `scan.py`, while the receipt — the only
    surface that can issue `DELIVERY_ESTABLISHED` — kept deciding membership by
    `needle in delivered`. On OpenClaw that was not a cosmetic gap: a constraint
    reaching context *only* through the `AGENTS.md` policy digest has its text
    present in the delivered blob, so containment answered yes and the receipt
    established delivery on a route the ruling says cannot establish anything.

    Containment remains the fallback for profiles that recover no spans, and the
    `span_membership` gate reports those as open rather than letting them pass.
    """
    if not pred.loaded:
        return Status.NOT_DELIVERED, pred.load_set_reason, Cause.NOT_IN_LOAD_SET
    if pred.spans is not None:
        return resolve_block_spans(text, start, end, pred.spans,
                                   unmodeled=bool(pred.unmodeled))
    status, reason = resolve_block(block_needle(profile, text), pred.delivered,
                                  unmodeled=bool(pred.unmodeled))
    return status, reason, (Cause.NONE if status is Status.DELIVERED
                            else pred.cause)


def _best_member_route(profile, f: InputFile, pred: Prediction, canonical: str, *,
                       unmodeled: bool) -> tuple[Status, str, Cause]:
    """The best route any copy of *canonical* reached context by.

    A constraint can legitimately appear in the file more than once: a repository
    that authored the clause in place and then imported it into the store has the
    text both where the author wrote it and inside the managed region. The question
    §6 Part A asks is whether *the constraint* is delivered, so every occurrence is
    resolved and the strongest result wins — one copy delivered through a
    source-preserving route establishes the constraint however the other copies
    fared.

    That "strongest wins" is bounded in the direction the ruling cares about:
    `_RANK` puts `DELIVERED_ON_LOAD` above `PARTIAL`, and a digest-only copy can
    only ever produce `PARTIAL`, so no number of digest copies can out-rank or
    substitute for a source-preserving one.
    """
    if pred.spans is None:
        status, reason = resolve_block(block_needle(profile, canonical),
                                       pred.delivered, unmodeled=unmodeled)
        return status, reason, (Cause.NONE if status is Status.DELIVERED
                                else pred.cause)
    text = f.file_text
    occurrences: list[int] = []
    at = text.find(canonical)
    while at != -1:
        occurrences.append(at)
        at = text.find(canonical, at + 1)
    if not occurrences:
        # The capsule is written into this file verbatim, so a canonical text that
        # is not in the file at all means the placement, not the delivery, failed.
        return (Status.NOT_DELIVERED,
                "the constraint's canonical text is not present in the mirrored "
                "file, so no source range could have delivered it",
                Cause.NOT_IN_LOAD_SET)
    best: tuple[Status, str, Cause] | None = None
    for start in occurrences:
        got = resolve_block_spans(canonical, start, start + len(canonical),
                                  pred.spans, unmodeled=unmodeled)
        if best is None or _RANK[got[0]] > _RANK[best[0]]:
            best = got
    assert best is not None
    return best


def _statuses(profile, f: InputFile, pred: Prediction
              ) -> dict[int, tuple[Status, str, str]]:
    """`index -> (status, label, locator)` for one file under one prediction.

    The locator is carried here rather than recomputed later: segmenting once per
    file and once per *block* are not the same cost on a 50,000-character
    `AGENTS.md`, and the field test already has a runtime entry (CF-003) from
    re-deriving something per block.
    """
    out: dict[int, tuple[Status, str, str]] = {}
    for block in segment(f.file_text):
        locator = (f"{block.line}" if block.end_line <= block.line
                   else f"{block.line}-{block.end_line}")
        status, _, _ = _resolve(profile, f, pred, block.text,
                                block.start, block.end)
        out[block.index] = (status, block.label, locator)
    return out


def issue(scenario: Scenario, store: store_mod.Store, project_root: Path, *,
          protected: "tuple[str, ...]" = (),
          harness_root: Path | None = None,
          development_evidence: bool = False) -> Receipt:
    """The §5 receipt for one scenario, with pre-merge ruling item 1 enforced.

    The delivery computation is `_compute`, and this wrapper applies the
    qualification cap **once, on every path**. That structure is deliberate:
    `_compute` has six early returns, and an invariant written as six separate
    checks is an invariant with six chances to be missed by the seventh return
    somebody adds later.

    `development_evidence` does **not** bypass anything. Item 1 forbids a
    test-only path that emits a production-looking success, so the flag cannot
    change `outcome`; it only adds a labelled `development_evidence_only` record
    of what the delivery computation said. A bypass that *cannot* reach
    `DELIVERY_ESTABLISHED` is strictly safer than one that can, and it leaves no
    code path to audit for whether it was switched on in production.
    """
    receipt = _compute(scenario, store, project_root, protected=protected,
                       harness_root=harness_root)
    _apply_qualification_cap(receipt, development_evidence=development_evidence)
    return receipt


def _apply_qualification_cap(receipt: Receipt, *, development_evidence: bool) -> None:
    """Item 1: incomplete qualification caps the outcome at INDETERMINATE.

    `DELIVERY_FAILED` is left alone. A confirmed failure is not a success-like
    result, and downgrading it to indeterminate would hide a real finding behind
    an adapter's paperwork.
    """
    if receipt.qualification is None or receipt.qualification.complete:
        return
    if development_evidence:
        receipt.development_evidence_only = {
            "would_be": receipt.outcome,
            "delivered_constraint_ids": list(receipt.delivered_ids),
            "note": ("development evidence only — the adapter's qualification is "
                     "incomplete, so this is not a receipt and must not be read as "
                     "one"),
        }
    if receipt.outcome == ESTABLISHED:
        receipt.outcome = INDETERMINATE
        receipt.indeterminate_ids = sorted(
            set(receipt.indeterminate_ids) | set(receipt.delivered_ids))
        receipt.delivered_ids = []
        # Hermes-audit ruling item 4 names the provisional case separately, because
        # "a gate is open" and "the gate set does not cover this adapter" call for
        # different responses: the first is answerable by running something, the second
        # is not. CF-023 is what the second looks like when it is not said out loud.
        if receipt.qualification.provisional:
            receipt.reasons.insert(0, (
                f"adapter is PROVISIONAL, so no production receipt may exceed "
                f"{INDETERMINATE} (ruling item 4). "
                f"{len(receipt.qualification.provisional)} scope reason(s): "
                + "; ".join(receipt.qualification.provisional)))
        else:
            receipt.reasons.insert(0, (
                f"qualification incomplete, so no successful receipt is issued "
                f"(pre-merge ruling item 1): "
                f"{receipt.qualification.summary()}"))


def _compute(scenario: Scenario, store: store_mod.Store, project_root: Path, *,
             protected: "tuple[str, ...]" = (),
             harness_root: Path | None = None) -> Receipt:
    """The delivery computation. Never call this directly — see `issue`."""
    reg = scenario.registered
    fp = fingerprint_check(scenario.profile_key, harness_root)
    profile = (reg.build(active=scenario.state.get("active_skills"))
               if reg.needs_declaration else reg.build())
    context: Context = scenario.declared_context(project_root)
    qualification = qual_mod.evaluate(
        scenario.profile_key, harness_root=harness_root,
        declared_external=scenario.external)

    files = load_set(scenario)
    capsule = build_capsule(store, context)
    basis, fingerprint = _input_fingerprint(scenario, store, files, protected)
    receipt = Receipt(
        profile=reg.profile_id,
        profile_evidence_level=reg.evidence_level,
        loader_fingerprint=fp,
        scenario=scenario.label(project_root),
        required_ids=list(capsule.ids),
        capsule_unit=reg.native_unit,
        capsule_size=capsule.size(reg.native_unit),
        qualification_evidence=(
            f"{profile.verified_against}; evidence level "
            f"{reg.evidence_level}; loader surface {fp.state}; "
            f"qualification {qualification.summary()}"),
        capsule=capsule,
        qualification=qualification,
        missing_qualification=qualification.missing_evidence,
        input_fingerprint=fingerprint,
        input_fingerprint_basis=basis,
        constraintflow_commit=cf_commit(),
        store_fingerprint=parts_digest(basis, "store="),
        source_fingerprint=parts_digest(basis, "file:"),
        config_fingerprint=parts_digest(
            basis, "parameter:", "context:", "external:", "delivery:", "protected="),
        evidence_reference=evidence_reference(),
        target_id=target_id(scenario),
    )

    # §8: a profile that needs a declaration it has not been given cannot reach a
    # definite endpoint, and the output has to name what would resolve it.
    undeclared = [p for p in reg.required_parameters
                  if p not in scenario.parameters]
    if undeclared:
        receipt.missing_declarations = undeclared
        receipt.outcome = INDETERMINATE
        receipt.reasons.append(
            f"CONDITIONAL: {reg.profile_id} has parameter-dependent delivery and "
            f"{', '.join(undeclared)} is not declared. §8 issues no successful "
            f"receipt from an unresolved conditional profile.")
        receipt.indeterminate_ids = list(capsule.ids)
        return receipt

    # §3 delivery mode. Two of the three modes cannot establish delivery at all
    # from a repository, and saying so is more useful than a false negative.
    #
    # §13 is why the target may override it: one canonical stored copy has to be
    # placed through *two* harnesses' qualified mechanisms, and where a capsule
    # goes is a property of the harness, not of the constraint.
    delivery = (store_mod._parse_delivery(scenario.delivery, store.path)
                if scenario.delivery else store.delivery)
    if delivery.mode == store_mod.EXTERNAL_GENERATOR:
        receipt.outcome = INDETERMINATE
        receipt.indeterminate_ids = list(capsule.ids)
        receipt.reasons.append(
            f"delivery mode is external_generator ({delivery.describe()}). §3 "
            f"forbids patching a generated output file that another tool will "
            f"overwrite, so delivery must be established at the generator's "
            f"source layer, which ConstraintRelay does not drive here.")
        return receipt
    if delivery.mode == store_mod.NO_MIRROR:
        receipt.outcome = INDETERMINATE
        receipt.indeterminate_ids = list(capsule.ids)
        receipt.missing_declarations.append("[delivery] mode")
        receipt.reasons.append(
            "the store declares no delivery channel, so there is no path on which "
            "initial-load delivery could be established.")
        return receipt

    receipt.mirror_target = delivery.mirror_path
    target = _locate_mirror(files, delivery.mirror_path)
    if target is None:
        receipt.outcome = FAILED
        receipt.missing_ids = list(capsule.ids)
        receipt.reasons.append(
            f"the capsule is mirrored into {delivery.mirror_path!r}, which is not "
            f"in this profile's load set for this scenario — the loader never reads "
            f"it, so nothing placed there can be delivered "
            f"({Cause.NOT_IN_LOAD_SET.value}).")
        return receipt

    if not capsule.members:
        # Nothing is required here. §2.5's wording matters: this is a statement
        # about the declared scope, not about every rule that should matter.
        receipt.outcome = ESTABLISHED
        receipt.reasons.append(
            f"no stored constraint is {CLAIM} for this scenario; "
            f"{len(capsule.excluded)} excluded, each with a recorded reason.")
        return receipt

    # Before / after, in memory. Writing to the repository to measure delivery
    # would make a read-only check mutate the thing it is measuring.
    before_files = files
    after_files = [
        InputFile(f.name, f.path, write_region(capsule, f.file_text),
                  loader_view=(None if f.loader_view is None
                               else write_region(capsule, f.loader_view)))
        if f.path == target.path else f
        for f in before_files]

    try:
        before_pred = profile.predict(before_files)
        after_pred = profile.predict(after_files)
    except Exception as exc:                      # pragma: no cover - defensive
        receipt.outcome = INDETERMINATE
        receipt.reasons.append(f"the profile could not predict delivery: {exc}")
        receipt.indeterminate_ids = list(capsule.ids)
        return receipt

    after_target = next(f for f in after_files if f.path == target.path)
    after_delivered = after_pred[after_target.path]

    # --- Part A: capsule delivery ------------------------------------------
    unmodeled = bool(after_delivered.unmodeled) or not fp.definite_allowed
    for member in capsule.members:
        c = member.constraint
        if not after_delivered.loaded:
            status, reason = Status.NOT_DELIVERED, after_delivered.load_set_reason
            cause = Cause.NOT_IN_LOAD_SET
        else:
            status, reason, cause = _best_member_route(
                profile, after_target, after_delivered, c.canonical_text,
                unmodeled=unmodeled)
            if unmodeled and not fp.definite_allowed:
                reason = fp.message
        receipt.members.append(MemberDelivery(
            c.id, c.version, status, reason, c.machine_enforcement_declared,
            c.canonical_sha256, cause))

    for m in receipt.members:
        if m.status is Status.DELIVERED_ON_LOAD:
            receipt.delivered_ids.append(m.id)
        elif m.status is Status.PARTIAL:
            receipt.partial_ids.append(m.id)
        elif m.status in (Status.UNKNOWN, Status.CONDITIONAL):
            receipt.indeterminate_ids.append(m.id)
        else:
            receipt.missing_ids.append(m.id)

    # Owner ruling 2026-10-02: a digest-only constraint fails closed, and the
    # receipt has to say *why* in the ruling's own words rather than reporting a
    # bare PARTIAL that reads like an ordinary boundary cut.
    digest_only = [m.id for m in receipt.members
                   if m.cause is Cause.TRANSFORMED_BY_DIGEST]
    if digest_only:
        receipt.reasons.append(
            f"{Cause.TRANSFORMED_BY_DIGEST.value}: {', '.join(digest_only)} reached "
            f"context only through {target.name}'s policy digest, which re-selects "
            f"lines, collapses whitespace and cuts at 240 UTF-16 units. A "
            f"source-preserving route is required to establish delivery; v1 does "
            f"not rule on whether a reshaped line still carries the constraint, so "
            f"this fails closed.")

    if after_delivered.cause in (Cause.TRUNCATED, Cause.OMITTED_BY_BUDGET) and \
            (receipt.missing_ids or receipt.partial_ids):
        receipt.reasons.append(
            f"the capsule exceeds the verified delivery path capacity: "
            f"{after_delivered.cause.value} on {target.name} at "
            f"{receipt.capsule_size:,} {receipt.capsule_unit} of capsule.")

    # --- Part B: environment regression -------------------------------------
    protected_patterns = tuple(protected)
    # Item 6: protected content = author-marked critical constraints PLUS the
    # explicitly configured protected blocks. Computed over the *before* files,
    # because the question is what the capsule took away from material that was
    # already there.
    marked = _marked_blocks(before_files)
    # Qualification ruling item 6 restates protected content as "applicable stored
    # critical constraints + explicitly configured protected blocks". The stored
    # constraints are the capsule's own members, so a block whose text IS an
    # applicable stored constraint is protected wherever it already sits — which
    # matters for a repository that authored the clause in place before importing
    # it. Markers remain a third source for repositories that have not imported.
    stored_texts = {m.constraint.canonical_text for m in capsule.members}
    after_by_path = {a.path: a for a in after_files}
    for f in before_files:
        before_here = _statuses(profile, f, before_pred[f.path])
        after_file = after_by_path[f.path]
        # Recomputed for *every* file, including the ones the capsule did not
        # touch. Their text is unchanged but their delivery is not: OpenClaw and
        # Codex both spend a budget shared across files, so a larger `AGENTS.md`
        # can push a later file out entirely. That is §13's `shared-budget
        # exhaustion` disruption, and reusing the before-statuses for untouched
        # files would make it invisible — the one case this part exists to catch.
        after_here = _statuses(profile, after_file, after_pred[after_file.path])
        by_path = _is_protected(f, project_root, protected_patterns)
        for index, (before_status, label, locator) in before_here.items():
            after_status = after_here[index][0]
            if _RANK[after_status] >= _RANK[before_status]:
                continue
            mark = marked.get((f.name, index))
            block_text = f.file_text[
                next((b.start for b in segment(f.file_text) if b.index == index), 0):
                next((b.end for b in segment(f.file_text) if b.index == index), 0)
            ].strip()
            stored = block_text in stored_texts
            if by_path:
                why = "configured protected path"
            elif stored:
                why = "applicable stored critical constraint"
            elif mark:
                why = f"author-marked critical constraint {mark}"
            else:
                why = ""
            receipt.regressions.append(Regression(
                f.name, locator, label, before_status, after_status,
                bool(by_path or stored or mark), why))
        # A placement that activates a loader rule suppressing another source.
        if before_pred[f.path].loaded and not after_pred[f.path].loaded:
            receipt.suppressed_sources.append(
                f"{f.name} was loaded before the capsule and is not after: "
                f"{after_pred[f.path].load_set_reason or 'suppressed by the loader'}")
        elif (before_pred[f.path].cause is not Cause.REJECTED_BY_LOADER
                and after_pred[f.path].cause is Cause.REJECTED_BY_LOADER):
            receipt.suppressed_sources.append(
                f"{f.name} is rejected by the loader only after the capsule is "
                f"inserted ({Cause.REJECTED_BY_LOADER.value})")

    # Item 6: the protected-content regression RESULT as its own field.
    if receipt.protected_losses:
        receipt.protected_content_regression = (
            f"REGRESSED: {len(receipt.protected_losses)} protected block(s) lost "
            f"initial-load delivery")
    elif not protected and not marked and not stored_texts:
        receipt.protected_content_regression = (
            "NO_PROTECTED_CONTENT: no applicable stored critical constraint and no "
            "configured protected block, so there was nothing to regress")
    else:
        receipt.protected_content_regression = "NOT_REGRESSED"

    # --- outcome -------------------------------------------------------------
    if receipt.missing_ids or receipt.partial_ids or receipt.protected_losses \
            or receipt.suppressed_sources:
        receipt.outcome = FAILED
        if receipt.protected_losses:
            receipt.reasons.append(
                f"{len(receipt.protected_losses)} configured protected instruction "
                f"block(s) lose initial-load delivery when the capsule is inserted "
                f"(§6).")
    elif receipt.indeterminate_ids:
        receipt.outcome = INDETERMINATE
    else:
        receipt.outcome = ESTABLISHED

    if receipt.regressions and not receipt.protected_losses:
        receipt.reasons.append(
            f"{len(receipt.regressions)} non-protected block(s) lose ground to the "
            f"capsule; reported under §6 part B and not blocking, because §6 fails "
            f"a capsule for a *configured* protected block.")

    # §3: if the mirror is already on disk, it has to agree with the store.
    try:
        state = check_mirror(capsule, target.file_text)
    except MirrorError as exc:
        receipt.outcome = FAILED
        receipt.reasons.append(f"managed mirror in {target.name} is malformed: {exc}")
        return receipt
    if state.present and not state.matches:
        receipt.outcome = FAILED
        receipt.reasons.append(
            f"managed mirror drift in {target.name}: {state.detail} "
            f"(recorded {state.recorded_digest[:12]}, store "
            f"{state.expected_digest[:12]}). §3: manual drift is an error.")
    return receipt


def _is_protected(f: InputFile, project_root: Path,
                  patterns: "tuple[str, ...]") -> bool:
    if not patterns:
        return False
    try:
        rel = str(Path(f.path).resolve().relative_to(project_root)).replace("\\", "/")
    except ValueError:
        rel = f.name
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f.name, p)
               for p in patterns)


def _marked_blocks(files: "list[InputFile]") -> dict[tuple[str, int], str]:
    """`(file, block index) -> constraint label` for author-marked critical blocks.

    Pre-merge ruling item 6 freezes protected content as *author-marked critical
    constraints plus explicitly configured protected blocks*. Before this, only the
    configured paths were protected — so a clause the author had explicitly marked
    `critical` could lose initial-load delivery to a capsule and the receipt would
    report it as a non-blocking regression. An author who wrote the marker has
    already said the thing matters; making them also list the file would be asking
    twice.

    A malformed marker set is **not** an error here. `verify` is where marker
    configuration errors belong (§9 exit 1); inside a receipt the only question is
    which blocks are protected, and a file whose markers do not parse simply
    contributes none — it cannot make protection quietly disappear for the files
    that do parse, because `scan_project` reports issues per site and still returns
    the constraints it resolved.
    """
    scan = scan_project([(f.name, f.file_text) for f in files])
    out: dict[tuple[str, int], str] = {}
    for c in scan.constraints:
        for index in c.blocks:
            out[(c.file, index)] = c.label
    return out


def _input_fingerprint(scenario: Scenario, store: store_mod.Store,
                       files: "list[InputFile]",
                       protected: "tuple[str, ...]") -> tuple[list[str], str]:
    """Pre-merge ruling item 6: a digest of everything the receipt stands on.

    Returns the human-readable basis and the digest. The basis is listed, not just
    hashed, because a fingerprint nobody can reconstruct is a number rather than
    evidence — and the list is what shows an input was *considered*, which is the
    half that matters when an input turns out to have been missed.

    Repository-external inputs appear as their declaration, never as their
    contents: the whole point of item 6 is that we cannot see them, and hashing a
    path we did not read would manufacture confidence.
    """
    parts: list[str] = [f"analyzer={IMPLEMENTATION}"]
    parts.append(f"store={_digest(store.path.read_bytes())}"
                 if store.path.is_file() else "store=(not on disk)")
    for f in sorted(files, key=lambda f: f.name):
        parts.append(f"file:{f.name}={_digest(f.file_text.encode('utf-8'))}")
    parts.append(f"profile={scenario.profile_id}")
    parts.append(f"scenario_root={scenario.root.name}")
    for key in sorted(scenario.parameters):
        parts.append(f"parameter:{key}={scenario.parameters[key]!r}")
    for key in sorted(scenario.context):
        parts.append(f"context:{key}={scenario.context[key]!r}")
    for key in sorted(scenario.external):
        parts.append(f"external:{key}={scenario.external[key]!r}")
    for key in sorted(scenario.delivery):
        parts.append(f"delivery:{key}={scenario.delivery[key]!r}")
    for pattern in protected:
        parts.append(f"protected={pattern}")
    return parts, _digest("\n".join(parts).encode("utf-8"))


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def parts_digest(basis: "list[str]", *prefixes: str) -> str:
    """A digest over just the basis lines with these prefixes.

    Item 6 asks for store, source and config fingerprints separately. They are cut
    from the same basis list as the overall input fingerprint rather than recomputed,
    so the four can never disagree about what they covered.
    """
    chosen = [p for p in basis if any(p.startswith(x) for x in prefixes)]
    return _digest("\n".join(chosen).encode("utf-8")) if chosen else ""


def cf_commit() -> str:
    """This tree's commit, or `"(not a git checkout)"`.

    Recorded rather than derived at read time: a receipt that names only a version
    cannot be tied back to a tree, and the version moves far less often than the
    code does.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]),
             "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):      # pragma: no cover
        return "(git unavailable)"
    return out.stdout.strip() if out.returncode == 0 else "(not a git checkout)"


def evidence_reference() -> dict:
    """A pointer to the evidence artifact the qualification state came from."""
    from . import evidence as evidence_mod
    ev = evidence_mod.load()
    if ev is None:
        return {"artifact": None,
                "note": ("no evidence artifact has been generated; qualification "
                         "gates cannot be closed (run tools/gen_evidence.py)")}
    return {"artifact": "cf/data/evidence.json",
            "generated_at": ev.generated_at,
            "implementation": ev.implementation,
            "digest": _digest(ev.path.read_bytes())}


def target_id(scenario: Scenario) -> str:
    """A stable id for the target, independent of its display name.

    Item 6 asks approvals and receipts to carry a stable target ID. The name is the
    thing a project renames; the profile plus the declared working directory is the
    thing that identifies what is being evaluated. Renaming a target therefore does
    not silently create a new one — and does not silently inherit the old one's
    approvals either, because the name still appears beside this.
    """
    return _digest(f"{scenario.profile_id}\x00{scenario.delivery.get('mirror_path', '')}"
                   f"\x00{scenario.context.get('working_directory', '')}".encode())
