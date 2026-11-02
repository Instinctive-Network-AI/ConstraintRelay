"""The boundary between the v2 library and the Phase-1 skill store.

Owner ruling 2026-10-14: the two stores may stay separate for now, but which
one is **authoritative for the new flow** must be stated, and the old path must
not silently drop `exception` or a review status.

Authority, stated once — and it is NOT a pair
---------------------------------------------
The second ruling of 2026-10-14 is specifically that these two must stop being
described as two authorities: *"Do not present both as competing product
authorities."* So:

* **`cf/library.py` is THE canonical authority of the current v2 product.**
  Delivery (`cf/selector.py` → `cf/predelivery.py` → `cf/vibe_predelivery.py`)
  reads it and nothing else. It is the only store with a skill-level
  `source_version`, per-file `content_sha256`, a four-valued review tier, an
  `exception` field, record provenance, and a `library_content_id` separate
  from a per-attempt `build_run_id`.
* **`cf/skillstore.py` is the Phase-1 legacy/audit store.** It is not a product
  authority and nothing in the product consults it. It is retained for exactly
  two purposes: the historical provenance of records created under Phase 1, and
  the `cf/query.py` / `cf/cli_phase1.py` surfaces that already shipped against
  it. "Authoritative for its own flow" was the earlier wording here and it was
  too generous — a legacy surface reading its own store is not an authority, it
  is a reader of an archive.
* **Nothing is merged, and the import is one-way.**
  `import_phase1_store` moves records *into* v2 and leaves the legacy file
  byte-identical; there is no code path in v2 that writes the Phase-1 store.

The import changes location, not epistemic status
-------------------------------------------------
The same ruling adds a Phase-1 → v2 import path so the historical inventory is
not stranded — and forbids the obvious shortcut: *"do not classify all 473
Phase-1 imported candidates as `author_marked`. Those records originated from
deterministic candidate extraction."* `PHASE1.md` is explicit that all 473 were
promoted by one `--confirm-all` and that precision was never measured, so a
batch promotion is the one thing the import must not be able to perform.

Hence `from_phase1_record` preserves `tier`, `extraction_reason`, `status` and
the legacy record id verbatim, lands every record at `UNREVIEWED`, and
**refuses** to emit `author_marker` without per-record evidence named by the
caller. `assert_no_promotion` then re-derives that from the two artifacts
afterwards, because a function that promises not to promote is not a guard
against a caller that promotes after it returns.

Why a guard rather than a note
------------------------------
`ConstraintRecord` in `cf/skillstore.py` has **no `exception` field** and only a
two-state review gate (and that only in `stash@{0}`, not on the committed
branch). So converting a v2 entry into it is lossy by construction for exactly
two things:

1. `exception` — the field that has never been measured anywhere in this
   project, and whose whole point is that an exception silently dropped turns a
   conditional rule into an absolute one;
2. any review tier above `developer_reviewed` — `model_reviewed` and
   `human_reviewed` would both collapse to "confirmed", which is how a library
   gets described as human-reviewed because something downstream had one bit.

A comment saying "don't do that" is not a guard. `to_phase1_record` **raises**
instead, and the only way past it is an explicit, named acknowledgement that
records what is being discarded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .library import (DEVELOPER_REVIEWED, HUMAN_REVIEWED, MODEL_REVIEWED,
                      PROV_PHASE1_IMPORT, PROVENANCES, UNREVIEWED,
                      LibraryEntry, SkillBinding, tier_rank)

#: The one canonical authority of the current v2 product. Delivery reads this.
CANONICAL_PRODUCT_AUTHORITY = "constraintrelay/library.py"
#: The Phase-1 legacy/audit store. Not a product authority; retained for
#: historical provenance and the Phase-1 surfaces that already read it.
LEGACY_AUDIT_STORE = "constraintrelay/skillstore.py"

#: Kept as aliases so existing callers and tests do not break, but the names
#: are deliberately the new ones everywhere a report is produced: the old pair
#: read as two peers, which is what the ruling forbids.
AUTHORITATIVE_FOR_V2 = CANONICAL_PRODUCT_AUTHORITY
AUTHORITATIVE_FOR_PHASE1 = LEGACY_AUDIT_STORE

#: Fields a v2 entry can carry that the Phase-1 record cannot represent.
UNREPRESENTABLE_IN_PHASE1 = ("exception", "condition", "scope", "tool_names",
                             "action_tags", "produced_by_runs",
                             "file_content_sha256", "source_version")

#: Review tiers the Phase-1 two-state gate cannot distinguish.
TIERS_PHASE1_CANNOT_HOLD = (MODEL_REVIEWED, HUMAN_REVIEWED)


class LossyExportError(Exception):
    """Exporting to the Phase-1 store would discard something load-bearing."""


@dataclass(frozen=True)
class LossReport:
    """Exactly what an export would discard. Computed, never estimated."""

    constraint_id: str
    lost_fields: tuple
    tier_collapse: str = ""

    @property
    def lossy(self) -> bool:
        return bool(self.lost_fields) or bool(self.tier_collapse)

    def as_dict(self) -> dict:
        return {"constraint_id": self.constraint_id,
                "lost_fields": list(self.lost_fields),
                "tier_collapse": self.tier_collapse, "lossy": self.lossy}


def inspect_export(entry) -> LossReport:
    """What would be discarded if *entry* went into the Phase-1 store.

    Pure. Looks only at fields that are actually populated — an empty
    `exception` loses nothing, and reporting it as a loss would make the guard
    fire on every entry and therefore be switched off.
    """
    lost = []
    for f in UNREPRESENTABLE_IN_PHASE1:
        v = getattr(entry, f, None)
        if v not in (None, "", (), []):
            lost.append(f)
    collapse = ""
    if entry.review_tier in TIERS_PHASE1_CANNOT_HOLD:
        collapse = (f"{entry.review_tier} would be recorded as the Phase-1 "
                    "two-state 'confirmed', losing the distinction between a "
                    "rule a model checked and a rule a person checked")
    return LossReport(constraint_id=entry.constraint_id,
                      lost_fields=tuple(lost), tier_collapse=collapse)


def to_phase1_record(entry, *, acknowledge_loss: "tuple | None" = None) -> dict:
    """Project a v2 entry onto Phase-1's field set. Refuses silent loss.

    `acknowledge_loss` must name **every** field the projection would discard,
    exactly. Naming a superset is refused too, because an acknowledgement that
    over-claims is one nobody has read: the caller has to know what it is
    throwing away, not wave at it.

    A review tier above `developer_reviewed` can never be acknowledged away.
    That collapse is the one this project has an explicit ruling about, so it is
    a hard stop rather than a waiver.
    """
    report = inspect_export(entry)
    if report.tier_collapse:
        raise LossyExportError(
            f"{entry.constraint_id}: {report.tier_collapse}. This is not "
            "waivable. A model-reviewed or human-reviewed rule may not enter a "
            "store that cannot say which.")
    if report.lost_fields:
        named = tuple(acknowledge_loss or ())
        if set(named) != set(report.lost_fields):
            raise LossyExportError(
                f"{entry.constraint_id}: exporting to "
                f"{AUTHORITATIVE_FOR_PHASE1} would discard "
                f"{list(report.lost_fields)}, and acknowledge_loss names "
                f"{list(named)}. The two must match exactly. "
                f"{AUTHORITATIVE_FOR_V2} is authoritative for the v2 flow; "
                "export only with a recorded reason.")
    return {"constraint_id": entry.constraint_id, "skill_id": entry.skill_id,
            "canonical_text": entry.source_text,
            "source_text": entry.source_text,
            "source_file": entry.source_file,
            "source_span": list(entry.source_span),
            "source_hash": entry.source_hash,
            "constraint_version": entry.constraint_version,
            "extraction_reason": entry.extraction_reason,
            "tier": entry.tier,
            "_discarded": list(report.lost_fields),
            "_review_tier_in_v2": entry.review_tier}


# ─────────────────── Phase-1 → v2 import (ruling item 6) ────────────────────

#: Tiers the import may never invent. `author_marker` is the only extraction
#: tier in this project that is not a heuristic — it is the skill's author
#: having already decided — so handing it to a record that came out of
#: deterministic candidate extraction would launder a guess into a decision.
#:
#: This is a *refusal*, not a translation: there is no mapping from a Phase-1
#: tier onto `author_marker`, and a caller who really has per-record evidence
#: has to name that record and its evidence, one at a time.
TIERS_IMPORT_MAY_NOT_INVENT = ("author_marker",)

#: The id prefix every imported constraint gets. Namespaced so that a legacy
#: id can never silently collide with, or be mistaken for, a natively built
#: one — and so `grep p1-` answers "what is imported" without a schema read.
IMPORT_ID_PREFIX = "p1-"


class ImportProvenanceError(Exception):
    """An import that would change what is KNOWN about a record, not just where
    it is stored."""


@dataclass(frozen=True)
class ImportEvidence:
    """Per-record evidence that a legacy record really was author/human-confirmed.

    The ruling's exception: *"unless an individual record has separate evidence
    of author/human confirmation."* Singular and per-record on purpose — this
    type cannot express "all of them", which is the whole point. There is no
    constructor that takes a list of ids and one shared reason.
    """

    legacy_record_id: str
    #: The tier this record's own evidence supports.
    tier: str
    #: The v2 review tier its own evidence supports.
    review_tier: str
    #: Where the evidence IS. A path, a commit, a marker — not an argument.
    evidence_ref: str
    reviewed_by: str
    reviewed_at: str

    def __post_init__(self):
        for f in ("legacy_record_id", "tier", "review_tier", "evidence_ref",
                  "reviewed_by", "reviewed_at"):
            if not str(getattr(self, f)).strip():
                raise ImportProvenanceError(
                    f"ImportEvidence.{f} is empty. Evidence that does not say "
                    "where it is, or who checked it and when, is an assertion "
                    "about a record rather than evidence about it.")


def store_digest(path: Path) -> str:
    """sha256 of the legacy store's exact bytes. The import's anchor."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def from_phase1_record(record, *, skill_id: str, source_version: str,
                       build_run_id: str,
                       evidence: "ImportEvidence | None" = None
                       ) -> LibraryEntry:
    """One Phase-1 `ConstraintRecord` → one v2 `LibraryEntry`.

    Everything the legacy record knows is carried verbatim. Everything it does
    not know stays empty rather than being filled with a plausible value:

    * `file_content_sha256` is **""**. Phase-1 hashed the constraint's span,
      not the file, so there is no file hash to carry. Recomputing one from
      whatever is on disk today would bind the record to bytes it was never
      extracted from — and on this host the Phase-1 source tree is gone, so
      "whatever is on disk" would be a different repository entirely.
    * `review_tier` is `UNREVIEWED`. Phase-1's `status: active` is not a
      review; it is the result of `--confirm-all`.
    * `condition` / `exception` / `scope` are empty because the Phase-1 record
      has no such fields. An absent exception must read as "never extracted",
      never as "none".
    """
    tier = str(getattr(record, "tier", "") or "")
    review_tier = UNREVIEWED
    reviewed_by = reviewed_at = ""
    if evidence is not None:
        if evidence.legacy_record_id != record.constraint_id:
            raise ImportProvenanceError(
                f"evidence names {evidence.legacy_record_id!r} but is being "
                f"applied to {record.constraint_id!r}. Per-record evidence "
                "that is applied to another record is not per-record.")
        tier, review_tier = evidence.tier, evidence.review_tier
        reviewed_by = f"{evidence.reviewed_by} (evidence: {evidence.evidence_ref})"
        reviewed_at = evidence.reviewed_at
    elif tier in TIERS_IMPORT_MAY_NOT_INVENT:
        raise ImportProvenanceError(
            f"{record.constraint_id}: the legacy record's own tier is {tier!r}, "
            "which this import may not carry without per-record evidence. "
            "Phase-1 promoted all of its candidates in one --confirm-all and "
            "never measured precision, so a Phase-1 tier is not author "
            "confirmation. Supply ImportEvidence for this record or let it "
            "import at its deterministic tier.")
    return LibraryEntry(
        constraint_id=f"{IMPORT_ID_PREFIX}{record.constraint_id}",
        skill_id=skill_id,
        source_version=source_version,
        source_file=record.source_file,
        source_span=tuple(record.source_span),
        source_text=record.source_text,
        source_hash=record.source_hash,
        file_content_sha256="",
        tier=tier,
        extraction_reason=str(getattr(record, "extraction_reason", "") or ""),
        build_run_id=build_run_id,
        produced_by_runs=(build_run_id,),
        review_tier=review_tier,
        reviewed_at=reviewed_at,
        reviewed_by=reviewed_by,
        constraint_version=int(getattr(record, "constraint_version", 1) or 1),
        note=str(getattr(record, "note", "") or ""),
        provenance=PROV_PHASE1_IMPORT,
        legacy_record_id=record.constraint_id,
        legacy_status=str(getattr(record, "status", "") or ""))


def import_phase1_store(store, *, library, store_path: Path, build_run_id: str,
                        evidence: "dict | None" = None) -> dict:
    """Carry a whole Phase-1 store into the v2 library. Returns a report.

    `source_version` for every imported skill is derived from the **legacy
    store's bytes**, not from a tree on disk: `phase1-store:<sha256[:12]>`.
    That is the only identity an import can honestly claim — it binds the
    records to the archive they came from, and it keeps working after the
    source repository is gone, which on this host it already is.

    The legacy file is opened read-only and its digest is taken before and
    after, so "the legacy records stay intact for audit provenance" is a
    checked fact rather than a property of not having written any code to
    change them.
    """
    store_path = Path(store_path)
    before = store_digest(store_path)
    source_version = f"phase1-store:{before[:12]}"
    ev = dict(evidence or {})
    by_skill: dict = {}
    for s in store.skills:
        binding = SkillBinding(
            skill_id=s.skill_id, name=s.name, source_version=source_version,
            files=[], built_at="", build_run_id=build_run_id,
            provenance=PROV_PHASE1_IMPORT)
        library.add_skill(binding)
        by_skill[s.skill_id] = 0
    imported, skipped = [], []
    for rec in store.constraints:
        if rec.skill_id not in by_skill:
            skipped.append({"constraint_id": rec.constraint_id,
                            "why": f"no skill record for {rec.skill_id!r}"})
            continue
        entry = from_phase1_record(
            rec, skill_id=rec.skill_id, source_version=source_version,
            build_run_id=build_run_id, evidence=ev.pop(rec.constraint_id, None))
        library.add_entry(entry)
        by_skill[rec.skill_id] += 1
        imported.append(entry)
    if ev:
        raise ImportProvenanceError(
            f"evidence was supplied for records that are not in this store: "
            f"{sorted(ev)}. Unmatched evidence means the caller believes it "
            "promoted something it did not — a silent no-op in the one place "
            "an epistemic claim is being made.")
    after = store_digest(store_path)
    if after != before:
        raise ImportProvenanceError(
            f"{store_path} changed during the import ({before[:12]} -> "
            f"{after[:12]}). The legacy store is audit provenance and the "
            "import is one-way.")
    return {
        "legacy_store_path": str(store_path),
        "legacy_store_sha256": before,
        "legacy_store_unchanged": True,
        "source_version": source_version,
        "build_run_id": build_run_id,
        "skills_imported": len(by_skill),
        "records_imported": len(imported),
        "records_skipped": skipped,
        "records_per_skill": dict(sorted(by_skill.items())),
        "review_tiers_assigned": {
            t: sum(1 for e in imported if e.review_tier == t)
            for t in sorted({e.review_tier for e in imported})},
        "legacy_status_carried": {
            s: sum(1 for e in imported if e.legacy_status == s)
            for s in sorted({e.legacy_status for e in imported})},
        "tiers_carried": {
            t: sum(1 for e in imported if e.tier == t)
            for t in sorted({e.tier for e in imported})},
        "promoted_by_evidence": sorted(
            e.legacy_record_id for e in imported
            if e.review_tier != UNREVIEWED),
        "file_content_hashes_available": sum(
            1 for e in imported if e.file_content_sha256),
        "note": ("an import changes canonical storage location and nothing "
                 "else. file_content_sha256 is empty for every imported record "
                 "because Phase-1 never recorded one, so no imported record "
                 "can be re-verified against source bytes."),
    }


def assert_no_promotion(store, *, library, allow: "dict | None" = None) -> dict:
    """Re-derive, from the TWO artifacts, that nothing gained status.

    Deliberately not a check inside `import_phase1_store`: that function could
    be correct and the library still end up promoted, because `mark_reviewed`
    exists and a caller can run it in the next line. This compares the legacy
    store against the library as they both stand now, which is the only form
    of the check that a later edit cannot walk past.

    Per the lesson about deriving a figure's set from an artifact its author
    can edit: the expected tier for each record comes from the **legacy store**,
    which this code cannot write, not from the library entry's own fields.
    """
    allowed = dict(allow or {})
    legacy = {r.constraint_id: r for r in store.constraints}
    offenders = []

    # --- which entries are imported is NOT the entries' own word ------------
    #
    # The 2026-10-14 review broke the first version of this function three
    # ways, and all three were the same mistake: the loop asked each entry
    # `e.provenance != PROV_PHASE1_IMPORT` and skipped it if it said no. So
    #
    #   * clearing provenance AND the legacy fields together made an imported
    #     entry indistinguishable from a native one -- my earlier check caught
    #     only the half-edit that kept the legacy fields;
    #   * `provenance='phase1_import_v2'`, a string that is in no allowlist,
    #     also skipped the loop. A comparison against one known-bad value
    #     passes everything it has not heard of.
    #
    # The fix is the lesson this project already wrote down: derive the set
    # from an artifact the author cannot edit. The legacy store's `source_hash`
    # is that artifact. Any entry whose text hashes to a legacy record's hash
    # IS an imported record, whatever it calls itself, and it is checked.
    by_hash: dict = {}
    for r in store.constraints:
        by_hash.setdefault(r.source_hash, []).append(r)
    claimed = {e.constraint_id for e in library.entries
               if e.provenance == PROV_PHASE1_IMPORT}
    derived = {e.constraint_id for e in library.entries
               if e.source_hash in by_hash}
    disguised = sorted(derived - claimed)
    if disguised:
        offenders.append(
            f"{len(disguised)} entr(ies) carry the text of a legacy record but "
            f"do not declare provenance {PROV_PHASE1_IMPORT!r}: "
            f"{disguised[:6]}. The import set is derived from the legacy "
            "store's own hashes, not from what an entry says about itself.")

    # Provenance must be a KNOWN value. An unrecognised string is refused
    # rather than treated as 'not imported'.
    unknown = sorted({e.provenance for e in library.entries}
                     - set(PROVENANCES))
    if unknown:
        offenders.append(
            f"unknown provenance value(s) {unknown}; permitted: "
            f"{list(PROVENANCES)}. A provenance this code has never heard of "
            "must not be read as 'natively built'.")

    # One legacy record, one entry. `allow` is keyed by legacy record id, so
    # two entries sharing one id let a single piece of per-record evidence
    # promote both -- which is exactly the 'all of them' that `ImportEvidence`
    # is shaped to make impossible.
    seen_legacy: dict = {}
    for e in library.entries:
        if not e.legacy_record_id:
            continue
        seen_legacy.setdefault(e.legacy_record_id, []).append(e.constraint_id)
    shared = {k: v for k, v in seen_legacy.items() if len(v) > 1}
    if shared:
        offenders.append(
            f"legacy record id(s) claimed by more than one entry: "
            f"{ {k: v for k, v in list(shared.items())[:4]} }. One piece of "
            "per-record evidence would promote all of them.")

    for e in library.entries:
        imported = (e.provenance == PROV_PHASE1_IMPORT
                    or e.constraint_id in derived)
        if not imported:
            # A native entry may not carry legacy provenance fields either.
            if e.legacy_record_id or e.legacy_status:
                offenders.append(
                    f"{e.constraint_id}: provenance {e.provenance!r} but it "
                    f"carries legacy fields (id {e.legacy_record_id!r}, status "
                    f"{e.legacy_status!r}). An imported record cannot relabel "
                    "itself as natively built.")
            continue
        if not e.legacy_record_id:
            # Derived as imported, but it erased the pointer. Recover the
            # record by hash so the comparison below still has something to
            # compare against.
            cands = by_hash.get(e.source_hash) or []
            if len(cands) == 1:
                rec = cands[0]
                want_t = (allowed[rec.constraint_id].tier
                          if rec.constraint_id in allowed
                          else str(rec.tier or ""))
                if e.tier != want_t or e.review_tier != UNREVIEWED:
                    offenders.append(
                        f"{e.constraint_id}: erased its legacy_record_id and "
                        f"sits at tier {e.tier!r}/{e.review_tier!r}; the "
                        f"legacy record {rec.constraint_id!r} with the same "
                        f"text says {want_t!r}/{UNREVIEWED!r}")
                continue
        rec = legacy.get(e.legacy_record_id)
        if rec is None:
            offenders.append(
                f"{e.constraint_id}: claims provenance {PROV_PHASE1_IMPORT} "
                f"and legacy id {e.legacy_record_id!r}, which is not in "
                f"{store.path if hasattr(store, 'path') else 'the store'}")
            continue
        ok = allowed.get(e.legacy_record_id)
        want_tier = ok.tier if ok else str(rec.tier or "")
        want_review = ok.review_tier if ok else UNREVIEWED
        if e.tier != want_tier:
            offenders.append(
                f"{e.constraint_id}: tier {e.tier!r}, but the legacy record "
                f"says {want_tier!r}")
        if e.review_tier != want_review:
            offenders.append(
                f"{e.constraint_id}: review_tier {e.review_tier!r} without "
                f"per-record evidence; the import may only reach {want_review!r}")
        if str(rec.status or "") != e.legacy_status:
            offenders.append(
                f"{e.constraint_id}: legacy_status {e.legacy_status!r} does "
                f"not match the legacy record's {rec.status!r}")
        # Identity, not just status. Checking only tier/review_tier left a
        # coordinated edit open: re-point an entry's `legacy_record_id` at a
        # *different* legacy record that happens to share its tier and status,
        # and every check above passes while the entry's text now has no
        # relationship to the record it cites. Found by attacking this function
        # with a three-field edit rather than a one-field one.
        if e.source_hash != rec.source_hash or e.source_text != rec.source_text:
            offenders.append(
                f"{e.constraint_id}: cites legacy record "
                f"{e.legacy_record_id!r} but its text/hash is not that "
                "record's. The entry was re-pointed after the import.")
        if e.file_content_sha256:
            offenders.append(
                f"{e.constraint_id}: carries a file_content_sha256, which "
                "Phase-1 never recorded, so it was computed from bytes this "
                "record was not extracted from")
    if offenders:
        raise ImportProvenanceError(
            "the import changed epistemic status, not just storage location:\n"
            + "\n".join(f"  - {o}" for o in offenders))
    checked = [e for e in library.entries
               if e.provenance == PROV_PHASE1_IMPORT]
    return {"imported_entries_checked": len(checked),
            "legacy_records_available": len(legacy),
            "promotions_allowed_by_evidence": sorted(allowed),
            "derived_from": "the legacy store, which this module cannot write"}


def _imported_names(src: str) -> set:
    """Module names *imported* by `src`. Parsed, not grepped."""
    import ast
    out: set = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[-1])
            for a in node.names:
                out.add(a.name.split(".")[-1])
    return out


def assert_delivery_reads_v2_only(*modules) -> None:
    """Raise if any delivery module **imports** the Phase-1 store.

    Checked over the module's import graph via AST, not over its text. The
    first version grepped for the string and fired on `cf/library.py`, whose
    docstring *explains* its relationship to `cf/skillstore.py` — a prose
    mention satisfying a structural check, which is the same defect shape as a
    comment satisfying the B5 write-ordering check. A reference in a docstring
    is documentation; an entry in the import graph is a dependency.
    """
    import inspect
    offenders = []
    for m in modules:
        try:
            src = inspect.getsource(m)
        except OSError:                                  # pragma: no cover
            continue
        if "skillstore" in _imported_names(src):
            offenders.append(m.__name__)
    if offenders:
        raise LossyExportError(
            f"delivery module(s) {offenders} reference the Phase-1 store. "
            f"{AUTHORITATIVE_FOR_V2} is authoritative for the v2 flow and "
            "delivery must read it alone.")


def boundary_summary() -> dict:
    return {
        "canonical_product_authority": CANONICAL_PRODUCT_AUTHORITY,
        "legacy_audit_store": LEGACY_AUDIT_STORE,
        "competing_authorities": False,
        "legacy_store_is_a_product_authority": False,
        "merged": False,
        "v2_writes_phase1_store": False,
        "phase1_store_modified": False,
        "import_direction": "phase1 -> v2 only",
        "import_changes_epistemic_status": False,
        "tiers_import_may_not_invent": list(TIERS_IMPORT_MAY_NOT_INVENT),
        "unrepresentable_in_phase1": list(UNREPRESENTABLE_IN_PHASE1),
        "review_tiers_phase1_cannot_hold": list(TIERS_PHASE1_CANNOT_HOLD),
        "tier_ranks": {t: tier_rank(t) for t in
                       (UNREVIEWED, DEVELOPER_REVIEWED, MODEL_REVIEWED,
                        HUMAN_REVIEWED)},
        "note": ("export is guarded, not forbidden: `to_phase1_record` raises "
                 "unless every discarded field is named, and a review tier "
                 "above developer_reviewed cannot be exported at all"),
    }
