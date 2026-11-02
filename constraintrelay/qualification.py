"""Adapter qualification as a gate on the receipt — pre-merge ruling item 1.

> A profile with incomplete qualification MUST NOT issue `DELIVERY_ESTABLISHED`.
> Its maximum success-like result is `DELIVERY_INDETERMINATE`, with the missing
> qualification evidence named.

Before this module the receipt's outcome was decided by delivery arithmetic alone,
so a profile whose §14 gates were open could still print
`DELIVERY_ESTABLISHED` — which is exactly the shape the ICAIF paper is about: a
confident statement standing on evidence nobody checked.

Two rules make the gate hard to launder.

**Machine-checkable gates are recomputed, and the code's answer wins.** A gate like
"the loader fingerprint matches" is answered by `fingerprint.check`, not by a line
in a data file. If `qualification.json` claims `MET` for one of those, the claim is
*ignored* — and a claim that contradicts the computed state is reported as
`CLAIM_CONTRADICTED`, which is worse than `UNMET`, because a wrong claim is
evidence about the project and not only about the adapter.

**Development evidence can never look like a receipt.** `receipt.issue` accepts
`development_evidence=True`, which still refuses `DELIVERY_ESTABLISHED`; it records
what the delivery computation *would* have said under
`development_evidence_only`, labelled, so a slice test can demonstrate the
mechanism without producing a production-looking success.

Gate inventory lives in `cf/data/qualification.json`. Human-recorded evidence
(a holdout run, a layer inventory derived from upstream execution) is data, because
it is a fact about work that happened. Everything a program can decide is decided by
a program.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence as evidence_mod
from .fingerprint import (DECLARED, QUALIFIED, FingerprintCheck,
                          check as fingerprint_check)
from .profiles import InputFile
from .registry import REGISTRY

from constraintrelay import datafiles as _df

DATA = _df.path("qualification.json")

MET = "MET"
UNMET = "UNMET"
NOT_APPLICABLE = "NOT_APPLICABLE"
CLAIM_CONTRADICTED = "CLAIM_CONTRADICTED"

#: Gates this module decides itself. A data-file claim for any of these is not
#: read as evidence; it is checked against the computed answer.
MACHINE_CHECKED = (
    "loader_fingerprint",
    "real_replay",
    "span_membership",
    "external_inputs_declared",
    "layer_fixture_coverage",
)


class QualificationError(Exception):
    """An unreadable or self-contradictory qualification record. Exit 1."""


@dataclass(frozen=True)
class Gate:
    id: str
    requirement: str
    state: str
    evidence: str

    @property
    def open(self) -> bool:
        return self.state in (UNMET, CLAIM_CONTRADICTED)

    def as_dict(self) -> dict:
        return {"gate": self.id, "requirement": self.requirement,
                "state": self.state, "evidence": self.evidence}


#: Hermes-audit ruling item 4's gate id. Not a §14 gate — a scope statement about the
#: gate list itself, which is why it cannot be closed by evidence.
ADAPTER_SCOPE = "adapter_scope"


@dataclass(frozen=True)
class Qualification:
    profile_id: str
    gates: tuple[Gate, ...] = ()
    #: Hermes-audit ruling item 4: reasons this adapter is provisional. Decisive — see
    #: `complete`. Kept separate from `gates` because a gate is answerable by evidence
    #: and this is not: it says the gate list does not cover the adapter.
    provisional: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """`COMPLETE` means *every gate closed **and** the gate set covers the adapter*.

        The second half is ruling item 4, and it exists because the first half was
        satisfied while being wrong: OpenClaw reported 12 of 12 gates closed against a
        gate set that stopped at the materialization path, and the receipt duly issued
        `DELIVERY_ESTABLISHED` for a constraint a subagent run never delivers (CF-023).

        A provisional reason therefore outranks the whole gate list and cannot be
        cleared by adding evidence to it — only by removing the reason.
        """
        return not self.provisional and not any(g.open for g in self.gates)

    @property
    def open_gates(self) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates if g.open)

    @property
    def missing_evidence(self) -> list[str]:
        """What the receipt must name, in the ruling's words."""
        return ([f"{ADAPTER_SCOPE}: {r}" for r in self.provisional]
                + [f"{g.id}: {g.evidence}" for g in self.open_gates])

    def summary(self) -> str:
        if self.provisional:
            closed = len(self.gates) - len(self.open_gates)
            return (f"PROVISIONAL ({len(self.provisional)} scope reason(s)); "
                    f"{closed} of {len(self.gates)} gate(s) closed")
        if self.complete:
            return f"{len(self.gates)} gate(s), all closed"
        return (f"{len(self.open_gates)} of {len(self.gates)} gate(s) open: "
                + ", ".join(g.id for g in self.open_gates))

    def as_dict(self) -> dict:
        return {"profile_id": self.profile_id,
                "complete": self.complete,
                "provisional": list(self.provisional),
                "summary": self.summary(),
                "gates": [g.as_dict() for g in self.gates]}


def load_data(path: Path | None = None) -> dict:
    """The qualification record.

    A missing file **raises** rather than returning `{}` — see
    `constraintrelay.datafiles.MissingDataFile`. An absent file and an empty record are
    different facts and may not share a representation.
    """
    p = path or DATA
    _df.require(p, what="the qualification record")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{p}: cannot read qualification record: {exc}") from exc


# ---------------------------------------------------------------------------
# the machine-checked gates
# ---------------------------------------------------------------------------

def _loader_fingerprint(profile_key: str, fp: FingerprintCheck) -> tuple[str, str]:
    if fp.state in (QUALIFIED, DECLARED):
        return MET, f"loader surface {fp.state}: {fp.message}"
    return UNMET, (f"loader surface is {fp.state}; a profile whose surface was "
                   f"never fingerprinted, or whose surface changed, cannot assert "
                   f"its qualification still holds (§8)")


def _real_replay(profile_key: str) -> tuple[str, str]:
    reg = REGISTRY[profile_key]
    if reg.replay_pending:
        return UNMET, ("no replay of the real loader exists; the profile is "
                       "source-verified only, and A1 does not accept a "
                       "source-derived reimplementation however closely it agrees")
    return MET, f"real loader executed; evidence level {reg.evidence_level}"


def _emits_spans(profile_key: str) -> tuple[str, str]:
    """Item 3, checked by *running* the profile rather than reading a constant.

    `evidence_level` is a registry constant, and a gate that trusts a constant is
    a gate that can be closed by editing one line. So the profile is built and
    asked to predict a probe; if the prediction carries no `spans`, this adapter
    still decides membership by containment whatever the registry says.
    """
    reg = REGISTRY[profile_key]
    try:
        profile = reg.build(active=["probe"]) if reg.needs_declaration else reg.build()
        probe = [InputFile("AGENTS.md", "/probe/AGENTS.md",
                           "# probe\n\nA line of instruction text.\n")]
        emits = any(p.spans is not None
                    for p in profile.predict(probe).values())
    except Exception as exc:                       # pragma: no cover - defensive
        return UNMET, f"the profile could not be probed for span support: {exc}"
    if emits:
        return MET, ("the profile recovers delivered source spans, verified by "
                     "probing `predict` rather than by reading `evidence_level`; "
                     "membership is decided from spans, not from containment")
    return UNMET, (f"the profile returns no delivered spans, so membership falls "
                   f"back to containment inside the file's own delivered text, "
                   f"which within-file repetition can fool. Registry evidence "
                   f"level is {reg.evidence_level}.")


def _external_inputs(profile_key: str, declared_external: "dict | None"
                     ) -> tuple[str, str]:
    """Ruling item 6: repository-external loader inputs must be declared or
    snapshotted, or the affected result is indeterminate."""
    reg = REGISTRY[profile_key]
    required = tuple(reg.external_inputs)
    if not required:
        return NOT_APPLICABLE, "this loader reads nothing outside the repository"
    supplied = set((declared_external or {}).keys())
    missing = [name for name in required if name not in supplied]
    if missing:
        return UNMET, (f"repository-external loader input(s) not declared or "
                       f"snapshotted: {', '.join(missing)}. Declare them under "
                       f"`[target.external]` or the affected result is "
                       f"indeterminate.")
    return MET, (f"external input(s) declared: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(
                     (declared_external or {}).items())))


def layer_inventory(record: dict) -> list[dict]:
    """The profile's loader-layer inventory.

    Read from the **generated** file named by `loader_layers_from`, never from a
    list typed into `qualification.json`. Ruling item 2 requires the inventory to
    originate from upstream execution, and a hand-copied list in a second file is
    a copy of an inventory rather than an inventory — it can drift from the run
    that produced it without anything noticing.
    """
    name = record.get("loader_layers_from")
    if not name:
        return []
    path = DATA.parent / name
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{path}: cannot read layer inventory: {exc}") from exc
    return [lay for lay in data.get("layers", []) if lay.get("state") == "OBSERVED"]


def _layer_fixture_coverage(profile_key: str, record: dict) -> tuple[str, str]:
    """Ruling item 5: *every documented loader layer in each candidate shipped
    adapter must have at least one fixture/mutation capable of making the receipt
    fail.*

    Checked structurally here — each observed layer must name at least one fixture
    — and executed in `tests/test_layer_coverage.py`, which runs each named fixture
    and requires the receipt to come back non-established. A name alone proves
    nothing, so both halves exist; this gate reports the structural half and the
    recorded result of the executed half.
    """
    layers = layer_inventory(record)
    if not layers:
        return UNMET, ("no loader-layer inventory recorded; §12 requires "
                       "loader-step coverage, and an adapter with no inventory "
                       "cannot show it has any")
    naked = [lay.get("layer", "?") for lay in layers if not lay.get("fixtures")]
    if naked:
        return UNMET, (f"loader layer(s) with no fixture able to fail the receipt: "
                       f"{', '.join(naked)}")
    executed = record.get("layer_coverage_executed")
    if executed is not True:
        return UNMET, (f"{len(layers)} layer(s) each name a fixture, but the "
                       f"executed coverage run is not recorded "
                       f"(`layer_coverage_executed` is {executed!r}); run "
                       f"tests/test_layer_coverage.py and record its result")
    return MET, (f"{len(layers)} documented loader layer(s), each with at least one "
                 f"fixture proven able to make the receipt fail")


# ---------------------------------------------------------------------------

def evaluate(profile_key: str, *, harness_root: Path | None = None,
             declared_external: "dict | None" = None,
             data: dict | None = None) -> Qualification:
    """The qualification state of one profile, for one invocation.

    `declared_external` comes from the target, so the same adapter can be
    qualified in a project that declares its external inputs and unqualified in
    one that does not — which is correct: the gate is about this evaluation.
    """
    reg = REGISTRY[profile_key]
    record = (data if data is not None else load_data()).get(reg.profile_id, {})
    claimed = {g.get("gate"): g for g in record.get("gates", [])}

    fp = fingerprint_check(profile_key, harness_root)
    computed = {
        "loader_fingerprint": _loader_fingerprint(profile_key, fp),
        "real_replay": _real_replay(profile_key),
        "span_membership": _emits_spans(profile_key),
        "external_inputs_declared": _external_inputs(profile_key, declared_external),
        "layer_fixture_coverage": _layer_fixture_coverage(profile_key, record),
    }

    gates: list[Gate] = []
    for gid in MACHINE_CHECKED:
        state, evidence = computed[gid]
        entry = claimed.get(gid)
        if entry is not None and entry.get("state") == MET and state == UNMET:
            # A data file asserting a gate this module can decide, against the
            # computed answer, is a worse signal than the open gate itself.
            gates.append(Gate(
                gid, _REQUIREMENTS[gid], CLAIM_CONTRADICTED,
                f"the qualification record claims MET; computed state is UNMET "
                f"because {evidence}"))
            continue
        gates.append(Gate(gid, _REQUIREMENTS[gid], state, evidence))

    # Qualification ruling item 1: these too are COMPUTED, from the generated
    # evidence artifact. `qualification.json` supplies the description and the
    # reference; it no longer supplies the verdict. A `"state"` key in the record is
    # read only to detect a claim that contradicts the computed answer — the same
    # treatment the machine-checked gates get, for the same reason.
    ev = evidence_mod.load()
    for gid, requirement in _RECORDED.items():
        entry = claimed.get(gid) or {}
        decider = _EVIDENCE_GATES.get(gid)
        if decider is None:                       # pragma: no cover - defensive
            gates.append(Gate(gid, requirement, UNMET,
                              "no evidence rule defined for this gate"))
            continue
        if ev is None:
            gates.append(Gate(
                gid, requirement, UNMET,
                "no evidence artifact has been generated; run "
                "tools/gen_evidence.py. A gate cannot be closed by a description."))
            continue
        state, evidence = decider(reg.profile_id, ev, entry)
        claim = entry.get("state")
        if claim == MET and state == UNMET:
            gates.append(Gate(
                gid, requirement, CLAIM_CONTRADICTED,
                f"the qualification record claims MET; the evidence artifact says "
                f"UNMET because {evidence}"))
            continue
        note = str(entry.get("note", "")).strip()
        gates.append(Gate(gid, requirement, state,
                          f"{evidence}" + (f" — {note}" if note else "")))

    # Ruling item 4, applied last and unconditionally: a provisional adapter is not
    # COMPLETE however its gates came out. Read straight from the registry rather than
    # from an evidence artifact, because the statement is "the gate set does not cover
    # this adapter" and no artifact the gate set produces can answer that.
    return Qualification(reg.profile_id, tuple(gates), tuple(reg.provisional))


# ---------------------------------------------------------------------------
# item 1: each recorded gate decided from the evidence artifact
# ---------------------------------------------------------------------------

def _g_commit_identity(profile_id, ev, entry):
    if profile_id.startswith("hermes-agent/"):
        commit = ev.get("hermes.commit")
        if commit and len(str(commit)) == 40:
            return MET, (f"loader-layer inventory was generated against commit "
                         f"{commit} (evidence: hermes.commit)")
        return UNMET, "no 40-character upstream commit recorded for the inventory"
    if profile_id.startswith("openclaw/"):
        # Owner ruling 2026-10-02 item 1 closes this on a **compatibility identity**
        # with five components, not on one artifact. Each is required here, because
        # the point of naming five is that no single one carries the claim: a commit
        # without a manifest is a string, and a manifest without the build's own
        # record of what it was built from does not connect to the code on this host.
        commit = ev.get("openclaw.source.commit")
        tag = ev.get("openclaw.source.tag")
        manifest = ev.get("openclaw.source.sha256_manifest")
        git_tree = ev.get("openclaw.source.git_tree_manifest")
        build_commit = ev.get("openclaw.source.build_info_commit")
        checks_pass = ev.get("openclaw.source.all_checks_pass")
        verified = ev.get("openclaw.source.files_verified")
        mismatched = ev.get("openclaw.source.files_mismatched")
        if not commit or len(str(commit)) != 40:
            # The pre-ruling fallback, kept so the gate degrades to the weaker
            # evidence rather than to nothing: the build's own commit alone.
            legacy = ev.get("openclaw.upstream_commit")
            if legacy and len(str(legacy)) == 40:
                return UNMET, (
                    f"the build reports commit {legacy}, but the ruling's "
                    f"compatibility identity is not recorded; run "
                    f"tools/verify_openclaw_source.py --write")
            return UNMET, ("no upstream commit recorded; the package version is a "
                           "release label, not a commit")
        missing = [name for name, value in (("tag", tag),
                                            ("whole-tree sha256 manifest", manifest),
                                            ("git-tree manifest", git_tree),
                                            ("installed build-info commit",
                                             build_commit)) if not value]
        if missing:
            return UNMET, (f"the compatibility identity is incomplete; missing: "
                           f"{', '.join(missing)}")
        if str(build_commit) != str(commit):
            return CLAIM_CONTRADICTED, (
                f"the installed build reports commit {build_commit} but the recorded "
                f"identity is {commit}; this host is not running the code the "
                f"identity describes")
        if checks_pass is not True:
            return UNMET, ("tools/verify_openclaw_source.py did not pass every check; "
                           "re-run it and read its output rather than this gate")
        if mismatched:
            return UNMET, (f"{mismatched} file(s) in the extracted tree disagree with "
                           f"the whole-tree manifest")
        return MET, (
            f"compatibility identity complete and verified on disk: tag {tag}, commit "
            f"{commit}, whole-tree sha256 manifest {str(manifest)[:12]}…, git-tree "
            f"manifest {str(git_tree)[:12]}…, and the installed build's own "
            f"dist/build-info.json reports the same commit — two independent sources. "
            f"{verified} source file(s) verified against the manifest, 0 mismatched "
            f"(evidence: openclaw.source.*)")
    return UNMET, f"no commit-identity rule for {profile_id}"


def _g_layer_inventory(profile_id, ev, entry):
    key = ("hermes.layers" if profile_id.startswith("hermes-agent/")
           else "openclaw.layers")
    n = ev.get(key)
    if isinstance(n, int) and n > 0:
        return MET, (f"{n} loader layer(s) observed by real execution "
                     f"(evidence: {key})")
    return UNMET, f"no layer inventory recorded (evidence: {key})"


def _g_scanner(profile_id, ev, entry):
    if not profile_id.startswith("hermes-agent/"):
        # Recorded NOT_APPLICABLE only where the record says so AND the artifact
        # carries no scanner metrics for it.
        if entry.get("state") == NOT_APPLICABLE:
            return NOT_APPLICABLE, str(entry.get("evidence", "")).strip() or                 "declared not applicable"
        return UNMET, f"no scanner evidence for {profile_id}"
    total = ev.get("hermes.scanner.rules_total")
    covered = ev.get("hermes.scanner.rules_with_positive_and_near_miss")
    crafted = ev.get("hermes.scanner.crafted_expected_vs_real") or {}
    corpus = ev.get("hermes.scanner.corpus_model_vs_real") or {}
    loader_ok = ev.get("hermes.scanner.loader_cases_all_ok")
    capsule_ok = ev.get("hermes.scanner.capsule_trips_scanner_ok")
    if not isinstance(total, int) or not isinstance(covered, int):
        return UNMET, "no scanner qualification artifact; run the scanner suite"
    if covered < total:
        return UNMET, (f"only {covered} of {total} rules have both a positive and a "
                       f"near-miss fixture")
    if crafted.get("fn") or crafted.get("fp"):
        return UNMET, (f"crafted expected-vs-real 2x2 is not clean: {crafted}")
    disagree = (corpus.get("model_rejects_real_allows", 0)
                + corpus.get("model_allows_real_rejects", 0))
    if disagree:
        return UNMET, f"corpus model-vs-real disagrees on {disagree} file(s)"
    if not loader_ok:
        return UNMET, "a loader-level scanner/fallback fixture did not pass"
    if not capsule_ok:
        return UNMET, ("the capsule-text-trips-scanner case did not produce a "
                       "failing receipt with a loader-rejection cause")
    return MET, (
        f"{covered}/{total} rules each with a positive and a near-miss, verified "
        f"against the real scanner; crafted expected-vs-real 2x2 "
        f"tp={crafted.get('tp')} fn={crafted.get('fn')} tn={crafted.get('tn')} "
        f"fp={crafted.get('fp')}; corpus model-vs-real over "
        f"{ev.get('hermes.scanner.corpus_files')} file(s) with 0 disagreements; "
        f"loader-level rejection and fallback fixtures pass; a capsule whose own "
        f"canonical text trips the scanner fails the receipt "
        f"(evidence: hermes.scanner.*)")


def _g_edge_cases(profile_id, ev, entry):
    # The edge cases are layers in the generated inventory, so their presence is
    # computed from it rather than asserted. Named explicitly: a layer list that
    # lost them would otherwise still satisfy a count check.
    required = {
        "hermes-agent/": ("outside_git_repo_walk_has_no_stop",
                          "git_as_file_stops_the_walk"),
        "openclaw/": ("git_state_is_irrelevant", "no_discovery_no_ancestor_search"),
    }
    names = next((v for k, v in required.items() if profile_id.startswith(k)), ())
    if not names:
        return UNMET, f"no edge-case rule for {profile_id}"
    record = (load_data().get(profile_id) or {})
    observed = {lay.get("layer") for lay in layer_inventory(record)}
    missing = [n for n in names if n not in observed]
    if missing:
        return UNMET, (f"the generated layer inventory does not contain: "
                       f"{', '.join(missing)}")
    return MET, (f"established by real execution and present in the generated "
                 f"inventory: {', '.join(names)}")


def _g_holdout(profile_id, ev, entry):
    """A fresh untouched holdout, per adapter, after the last correctness fix.

    Per adapter is the load-bearing word. The hermes tranche is never borrowed for
    openclaw: its ground truth is the real hermes loader, so it says nothing about a
    different loader's model, and sharing the evidence key would let it look as if it
    did. Each adapter therefore has its own namespace and its own tranche series.
    """
    if profile_id.startswith("openclaw/"):
        prefix = "openclaw.holdout"
        required_after = ("CF-016", "CF-018")
    elif profile_id.startswith("hermes-agent/"):
        prefix = "holdout"
        required_after = ("CF-011",)
    else:
        return UNMET, ("no holdout instrument exists for this adapter; there is no "
                       "field-test verifier whose ground truth is this loader, so "
                       "there is nothing to run a tranche through")

    tranche = ev.get(f"{prefix}.qualifying_tranche")
    frozen = ev.get(f"{prefix}.frozen_pairs")
    false_definite = ev.get(f"{prefix}.false_definite")
    run_after = ev.get(f"{prefix}.run_after") or []
    tranches = ev.get(f"{prefix}.tranches_run") or []
    missing_after = [fix for fix in required_after if fix not in run_after]
    if not tranche:
        return UNMET, (f"no tranche is marked qualifying; tranches run: "
                       f"{', '.join(tranches) or 'none'}")
    if not isinstance(frozen, int) or frozen < 30:
        return UNMET, (f"the qualifying tranche froze {frozen} pair(s); the ruling "
                       f"requires 30")
    if missing_after:
        return UNMET, (f"the qualifying tranche does not record running after "
                       f"{', '.join(missing_after)}; a holdout that predates the last "
                       f"correctness fix measures the defect, not the fix")
    if false_definite:
        return UNMET, (f"the qualifying tranche reported {false_definite} false "
                       f"definite status(es)")
    return MET, (
        f"tranche {tranche} of {len(tranches)} run: {frozen} frozen pairs, "
        f"{ev.get(f'{prefix}.evaluated_pairs')} evaluated, "
        f"{ev.get(f'{prefix}.definite_verdicts')} definite verdicts, 0 false "
        f"definite, run after {', '.join(run_after)} "
        f"(evidence: {prefix}.*)")


def _g_loader_step_coverage(profile_id, ev, entry):
    """Owner ruling 2026-10-02 item 2: every documented loader step gets a fixture
    **and** a mutation the fixture is required to catch.

    Distinct from `layer_fixture_coverage`, which asks whether each layer can make a
    *receipt* fail. This asks whether the port and the real loader agree on each
    step, and whether that agreement is being measured rather than assumed — a
    fixture both sides pass is not evidence until a mutation shows it would notice a
    difference. Both gates are needed: a layer can be able to fail a receipt while
    the port's arithmetic for it is wrong in a way no fixture would see.
    """
    if not profile_id.startswith("openclaw/"):
        if entry.get("state") == NOT_APPLICABLE:
            return NOT_APPLICABLE, (str(entry.get("evidence", "")).strip()
                                    or "declared not applicable")
        return UNMET, (f"the ruling's loader-step manifest is specific to openclaw; "
                       f"no step manifest exists for {profile_id}")
    total = ev.get("openclaw.loader_steps.total")
    with_fixture = ev.get("openclaw.loader_steps.with_fixture")
    caught = ev.get("openclaw.loader_steps.mutations_caught")
    cases = ev.get("openclaw.loader_steps.cases")
    spans = ev.get("openclaw.loader_steps.spans_traced")
    if not isinstance(total, int) or total <= 0:
        return UNMET, ("no loader-step manifest has been generated; run "
                       "tests/test_openclaw_loader_steps.py --write")
    if total < 10:
        return UNMET, (f"the ruling names 10 loader steps; the manifest records "
                       f"{total}")
    if with_fixture != total:
        return UNMET, (f"only {with_fixture} of {total} steps have a fixture that "
                       f"agrees with the real loader")
    if caught != total:
        return UNMET, (f"only {caught} of {total} steps have a mutation the fixture "
                       f"catches; a step whose mutation survives is untested however "
                       f"many fixtures pass")
    return MET, (
        f"{total}/{total} loader steps covered across {cases} fixture case(s), each "
        f"agreeing with the whole real loader, and {caught}/{total} mutations caught; "
        f"{spans} recovered span(s) traced back to the source line they name "
        f"(evidence: openclaw.loader_steps.*)")


def _g_artifact_provenance(profile_id, ev, entry):
    record = (load_data().get(profile_id) or {})
    basis = str(record.get("fingerprint_basis", "")).strip()
    if not basis:
        return UNMET, "the record does not state the fingerprint basis"
    if profile_id.startswith("openclaw/"):
        n = ev.get("openclaw.loader_surface_files")
        total = ev.get("openclaw.dist_file_count")
        if not isinstance(n, int) or n <= 1:
            return UNMET, (f"the fingerprint surface is {n} file(s); a one-file "
                           f"surface over a {total}-file artifact leaves almost "
                           f"every upstream change undetected (CF-013)")
        return MET, (f"basis stated as '{basis}'; surface is {n} module(s) of "
                     f"{total} in dist/, derived by execution "
                     f"(evidence: openclaw.loader_surface_files)")
    return MET, f"basis stated as '{basis}'"


_EVIDENCE_GATES = {
    "commit_identity": _g_commit_identity,
    "layer_inventory_provenance": _g_layer_inventory,
    "scanner_rejection_replay": _g_scanner,
    "edge_case_behaviour": _g_edge_cases,
    "loader_step_coverage": _g_loader_step_coverage,
    "holdout": _g_holdout,
    "artifact_provenance": _g_artifact_provenance,
}


_REQUIREMENTS = {
    "loader_fingerprint":
        "the loader-relevant implementation surface matches a recorded fingerprint "
        "(§8)",
    "real_replay":
        "the real loading code has been executed and compared against the "
        "prediction (A1, §12)",
    "span_membership":
        "block membership is decided from recovered source spans, not from "
        "containment inside the delivered text (pre-merge ruling item 3)",
    "external_inputs_declared":
        "repository-external loader inputs are declared or snapshotted "
        "(pre-merge ruling item 6)",
    "layer_fixture_coverage":
        "every documented loader layer has at least one fixture able to make the "
        "receipt fail (§12, pre-merge ruling item 5)",
}

_RECORDED = {
    "commit_identity":
        "the exact upstream commit and profile identity are reconciled and "
        "recorded (§14)",
    "layer_inventory_provenance":
        "the loader-layer inventory originates from upstream loader behaviour or "
        "real execution, not from the profile implementation being tested (§14)",
    "scanner_rejection_replay":
        "the scanner/filter/rejection behaviour required for delivery is "
        "replay-qualified (§14)",
    "edge_case_behaviour":
        "behaviour outside a git repository, and where `.git` is a file, is "
        "established by real execution (§14)",
    "loader_step_coverage":
        "every documented loader step has a fixture agreeing with the real loader "
        "and a mutation that fixture catches (owner ruling 2026-10-02 item 2)",
    "holdout":
        "a fresh untouched holdout was evaluated after the last correctness fix "
        "(§14)",
    "artifact_provenance":
        "the fingerprint basis is stated explicitly as upstream source or built "
        "artifact (pre-merge ruling item 3)",
}

ALL_GATES = tuple(MACHINE_CHECKED) + tuple(_RECORDED)
