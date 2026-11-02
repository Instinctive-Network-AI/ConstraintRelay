"""The Phase-1 command surface.

Kept in its own module rather than appended to `cli.py` for one reason: Phase 1
is a change of product centre, not an extension of the v0.7 command set, and the
brief says to preserve the existing code. `cli.py` keeps its 1,262 lines and its
subcommands exactly as they were; `main()` registers these in addition.

    import       scan a repository for skills and candidate constraints
    confirm      promote candidates into the authoritative store
    skills       what skills does the store know about?
    constraints  what constraints belong to this skill?
    constraint   where did this constraint come from?
    diff         what changed since the last import?
    update       accept a changed source into the canonical record
    retire       deactivate a constraint (the only way one stops applying)
    deliver      the §4 end-to-end delivery demo
    report       the Phase-1 acceptance report

Exit codes extend `cli.py`'s single namespace rather than inventing a second
one:

    0  completed, nothing needs an author decision
    1  tool or configuration error — including a discovery failure
    2  a reported finding: `diff` saw a change, or `deliver` could not place a
       constraint whole

`1` never means "a finding was reported", which is the property the v0.6 brief
fixed this namespace to protect.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

from . import deliver as deliver_mod
from . import naming
from . import skillstore
from . import lifetime as lifetime_mod
from . import sync as sync_mod
from .candidates import ADVISORY, AUTHOR_MARKER, OPERATIVE, Extraction, extract
from .inventory import PROJECT_SKILL_ID, Inventory, discover_skills, resolve_skill
from .query import ConstraintFlow
from .skillstore import ConstraintRecord, SkillStore, StoreError

EXIT_OK, EXIT_ERROR, EXIT_FINDING = 0, 1, 2

CANDIDATES_NAME = "candidates.json"
CANDIDATES_SCHEMA = "constraintflow/candidates/1"

TIER_ALL = "all"
TIER_CHOICES = (TIER_ALL, AUTHOR_MARKER, OPERATIVE, ADVISORY)

#: Printed by `report`. Deliberately short: `LIMITATIONS.md` is the canonical
#: list and this one points at it rather than paraphrasing it, because two
#: prose copies of the same caveats drift and the one in the binary is the one
#: a user reads first.
LIMITATIONS = (
    "Extraction is pattern-based: a constraint written without a modal verb is "
    "not proposed, and constraint-shaped headings that produced nothing are "
    "counted above as unresolved cases.",
    "Tabular rules are excluded — a whole markdown table segments as one block, "
    "so importing it as one constraint would be worse than missing it.",
    "Prose support files only (.md, .markdown, .txt, .rst), max 3 levels deep; "
    "skipped sources are counted, never silently dropped.",
    "skill_id is the skill directory path, so renaming the directory produces a "
    "new skill_id and the old constraints are reported SOURCE_REMOVED rather "
    "than re-attributed.",
    "Precision is unmeasured: candidates can be confirmed in batch and no human "
    "has adjudicated them one by one. Use --tier operative to take the "
    "obligations and leave advisory text as candidates.",
    "Every heuristic here is calibrated on one corpus.",
    "See LIMITATIONS.md for the full list, what was deliberately not built and "
    "why, and the adapter technical debt (CF-ADAPTER-DEBT-1).",
)


def _today() -> str:
    return _dt.date.today().isoformat()


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _root(args) -> Path | None:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return None
    return root


def _scan(root: Path, args) -> tuple[Inventory, Extraction]:
    inv = discover_skills(root,
                          include_project=getattr(args, "include_project", False),
                          skills_dir=getattr(args, "skills_dir", "") or "")
    return inv, extract(inv)


def candidates_path(root: Path) -> Path:
    return root / skillstore.STORE_DIR / CANDIDATES_NAME


def _tier_filter(candidates, tier: str):
    if tier == TIER_ALL:
        return list(candidates)
    if tier == OPERATIVE:
        return [c for c in candidates if c.tier in (AUTHOR_MARKER, OPERATIVE)]
    return [c for c in candidates if c.tier == tier]


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def cmd_import(args) -> int:
    """Scan, record the candidate set, and — only when told to — confirm.

    Without `--confirm-all` this command writes no constraint: the brief's rule
    for layer B is that heuristic extraction must not silently promote text into
    authoritative constraints, and an import that confirmed by default would be
    exactly that. `--confirm-all` **is** the author confirmation: one explicit,
    auditable, batch act, which the brief permits in as many words.
    """
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    inv, ex = _scan(root, args)
    if not inv.skills:
        return _fail(f"no skills found under {root}"
                     + (f"/{args.skills_dir}" if args.skills_dir else "")
                     + ". A skill is a directory containing SKILL.md.")

    record = {
        "schema": CANDIDATES_SCHEMA,
        "generated_at": args.when or _today(),
        "root": str(root),
        "skills_dir": args.skills_dir or "",
        "include_project": bool(args.include_project),
        "blocks_seen": ex.blocks_seen,
        "excluded": ex.excluded,
        "skills": [s.as_dict() for s in inv.skills],
        "candidates": [c.as_dict() for c in ex.candidates],
        "unresolved": [u.as_dict() for u in ex.unresolved],
        "marker_issues": [str(i) for i in ex.marker_issues],
        "unreadable": [{"path": p, "reason": r} for p, r in inv.unreadable],
    }
    out = candidates_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    confirmed: list[ConstraintRecord] = []
    store_file = None
    if args.confirm_all:
        store = skillstore.load_or_empty(root)
        when = args.when or _today()
        # Every discovered skill gets a `[[skill]]` record, including the ones
        # that produced no candidate. The store is the inventory of *skills* as
        # well as of constraints, so `constraintrelay skills` has to be able to
        # answer "89 found, 8 of them with nothing extracted" rather than
        # reporting 81 and leaving the other 8 invisible. Recording them in
        # sorted order also fixes prefix allocation deterministically.
        for skill in sorted(inv.skills, key=lambda s: s.skill_id):
            store.upsert_skill(skill.skill_id, skill.name, skill.basename,
                               skill.rel_dir, when)
        confirmed = _promote(store, inv, _tier_filter(ex.candidates, args.tier),
                             when=when)
        store_file = skillstore.write(store)

    if args.json:
        print(json.dumps({
            "command": "import", "root": str(root),
            "candidate_set": str(out),
            "skills_found": len(inv.skills),
            "candidates": len(ex.candidates),
            "tier_counts": ex.tier_counts(),
            "unresolved": len(ex.unresolved),
            "confirmed_now": len(confirmed),
            "store": str(store_file) if store_file else None,
        }, indent=2, ensure_ascii=False))
        return EXIT_OK

    collections: dict[str, int] = {}
    for s in inv.skills:
        collections[s.collection or "(repository root)"] = \
            collections.get(s.collection or "(repository root)", 0) + 1
    print(f"{root}")
    print(f"skills found: {len(inv.skills)}")
    for name, n in sorted(collections.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4} in {name}")
    tiers = ex.tier_counts()
    print(f"blocks examined: {ex.blocks_seen}")
    print(f"candidate constraints: {len(ex.candidates)} "
          f"(operative {tiers[OPERATIVE]}, advisory {tiers[ADVISORY]}, "
          f"author-marked {tiers[AUTHOR_MARKER]})")
    print(f"unresolved extraction cases: {len(ex.unresolved)}")
    if ex.marker_issues:
        print(f"marker configuration issues: {len(ex.marker_issues)}")
        for issue in ex.marker_issues[:10]:
            print(f"  {issue}")
    print(f"candidate set written to {out}")
    if confirmed:
        print(f"\nconfirmed {len(confirmed)} candidate(s) into {store_file}")
        print("The store is now authoritative for those constraints. The skill "
              "files are no longer an independent authoritative copy of them.")
    else:
        print("\nNothing has been confirmed. Candidate extraction is heuristic, so "
              "it never promotes text into the store on its own.")
        print(f"  confirm everything:        {naming.cmd('import', root)} "
              f"--confirm-all")
        print(f"  confirm the obligations:   {naming.cmd('import', root)} "
              f"--confirm-all --tier operative")
        print(f"  confirm one skill:         {naming.cmd('confirm', root)} "
              f"--skill <skill> --all")
    return EXIT_OK


def _promote(store: SkillStore, inv: Inventory, candidates, *, when: str
             ) -> list[ConstraintRecord]:
    """Turn candidates into store records. The only writer of new constraints.

    Idempotent by `candidate_id`: re-confirming a candidate that is already in
    the store is a no-op rather than a duplicate, so `--confirm-all` can be run
    twice without inflating the inventory.
    """
    already = store.by_candidate()
    out: list[ConstraintRecord] = []
    for c in sorted(candidates, key=lambda c: (c.skill_id, c.source_file,
                                               c.source_span)):
        if c.candidate_id in already:
            continue
        skill = inv.get(c.skill_id)
        name = skill.name if skill else c.skill_id
        basename = skill.basename if skill else c.skill_id.rsplit("/", 1)[-1]
        rel_dir = skill.rel_dir if skill else ""
        store.upsert_skill(c.skill_id, name, basename, rel_dir, when)
        cid = c.declared_id or store.next_id(c.skill_id)
        if cid in store.by_id:
            # An author marker whose id is already taken. Not promoted silently
            # under a different id: that would detach the store record from the
            # id the author wrote in their own file.
            raise StoreError(
                f"{c.locator}: author marker id {cid!r} is already in the store "
                f"for {store.by_id[cid].skill_id!r}; ids must be unique across "
                f"the project")
        rec = store.add(ConstraintRecord(
            constraint_id=cid, skill_id=c.skill_id,
            canonical_text=c.source_text.strip(),
            source_text=c.source_text, source_file=c.source_file,
            source_span=tuple(c.source_span), source_hash=c.source_hash,
            source_lines=tuple(c.source_lines),
            created_at=when, updated_at=when,
            extraction_reason=c.extraction_reason, tier=c.tier,
            candidate_id=c.candidate_id))
        out.append(rec)
        already[c.candidate_id] = rec
    return out


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

def cmd_confirm(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    if not (args.all or args.skill or args.candidate):
        return _fail("nothing selected; pass --all, --skill <skill>, or "
                     "--candidate <candidate_id> (repeatable)")
    inv, ex = _scan(root, args)
    if not inv.skills:
        return _fail(f"no skills found under {root}")

    pool = ex.candidates
    if args.skill:
        skill, options = resolve_skill(inv, args.skill)
        if skill is None:
            return _fail(f"{args.skill!r} does not resolve to one skill"
                         + (f"; candidates: "
                            f"{', '.join(s.skill_id for s in options)}"
                            if options else ""))
        pool = [c for c in pool if c.skill_id == skill.skill_id]
    if args.candidate:
        wanted = set(args.candidate)
        pool = [c for c in pool if c.candidate_id in wanted]
        missing = wanted - {c.candidate_id for c in pool}
        if missing:
            return _fail(f"candidate(s) not present in a fresh scan: "
                         f"{', '.join(sorted(missing))}. A candidate_id is keyed "
                         f"on its source text, so a missing one means the text "
                         f"changed since the candidate set was written. Re-run "
                         f"`import` and pick the new id.")
    pool = _tier_filter(pool, args.tier)
    if not pool:
        return _fail("the selection matched no candidates")

    store = skillstore.load_or_empty(root)
    try:
        confirmed = _promote(store, inv, pool, when=args.when or _today())
    except StoreError as exc:
        return _fail(str(exc))
    path = skillstore.write(store)

    if args.json:
        print(json.dumps({"command": "confirm", "store": str(path),
                          "selected": len(pool), "confirmed": len(confirmed),
                          "already_present": len(pool) - len(confirmed),
                          "constraints": [c.as_dict() for c in confirmed]},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"selected {len(pool)} candidate(s); confirmed {len(confirmed)} new, "
          f"{len(pool) - len(confirmed)} already in the store")
    for c in confirmed[:40]:
        print(f"  {c.constraint_id:<22} {c.locator}")
    if len(confirmed) > 40:
        print(f"  … {len(confirmed) - 40} more")
    print(f"store: {path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# skills / constraints / constraint
# ---------------------------------------------------------------------------

def cmd_skills(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        cf = ConstraintFlow.open(root)
    except StoreError as exc:
        return _fail(str(exc))
    views = cf.list_skills(with_constraints_only=args.with_constraints)
    if args.json:
        print(json.dumps({"command": "skills", "count": len(views),
                          "skills": [v.as_dict() for v in views]},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"{len(views)} skill(s) in {cf.store.path}")
    for v in views:
        flag = "" if v.status == skillstore.ACTIVE else f"  [{v.status}]"
        scope = "" if v.hold_scope == skillstore.HOLD_TASK \
            else f"  [hold={v.hold_scope}]"
        print(f"  {v.constraint_count:>4}  {v.name:<34} "
              f"{v.skill_id}{scope}{flag}")
    total = sum(v.constraint_count for v in views)
    print(f"\n{total} active constraint(s) across "
          f"{sum(1 for v in views if v.constraint_count)} skill(s)")
    return EXIT_OK


def cmd_constraints(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        cf = ConstraintFlow.open(root)
    except StoreError as exc:
        return _fail(str(exc))
    skill_id, options = cf.resolve(args.skill)
    if not skill_id:
        return _fail(f"{args.skill!r} does not resolve to one skill in the store"
                     + (f"; candidates: {', '.join(options)}" if options else ""))
    records = cf.get_constraints(skill_id, include_retired=args.include_retired)
    if args.json:
        print(json.dumps({"command": "constraints", "skill_id": skill_id,
                          "count": len(records),
                          "constraints": [c.as_dict() for c in records]},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    rec = cf.skill_record(skill_id)
    print(f"{rec.name if rec else skill_id}  ({skill_id})")
    print(f"{len(records)} constraint(s), read from the Constraint Store\n")
    for c in records:
        tag = "" if c.active else f"  [{c.status}]"
        print(f"{c.constraint_id}  v{c.constraint_version}  {c.tier}{tag}")
        for line in c.canonical_text.splitlines():
            print(f"    {line}")
        print(f"    source: {c.locator}  chars[{c.source_span[0]},"
              f"{c.source_span[1]})  sha256 {c.source_hash[:12]}…")
        print()
    return EXIT_OK


def cmd_constraint(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        cf = ConstraintFlow.open(root)
    except StoreError as exc:
        return _fail(str(exc))
    rec = cf.get_constraint(args.constraint_id)
    if rec is None:
        return _fail(f"unknown constraint_id {args.constraint_id!r}")
    if args.json:
        print(json.dumps({"command": "constraint", **rec.as_dict()},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    skill = cf.skill_record(rec.skill_id)
    print(f"{rec.constraint_id}")
    print(f"  skill          {skill.name if skill else ''} ({rec.skill_id})")
    print(f"  status         {rec.status}  v{rec.constraint_version}")
    print(f"  tier           {rec.tier}")
    print(f"  extracted by   {rec.extraction_reason}")
    print(f"  source file    {rec.source_file}")
    print(f"  source span    chars[{rec.source_span[0]},{rec.source_span[1]})  "
          f"lines {rec.source_lines[0]}-{rec.source_lines[1]}")
    print(f"  source sha256  {rec.source_hash}")
    print(f"  created        {rec.created_at}")
    print(f"  updated        {rec.updated_at}")
    if rec.note:
        print(f"  note           {rec.note}")
    print("\n  canonical text")
    for line in rec.canonical_text.splitlines():
        print(f"    {line}")
    if rec.source_text.strip() != rec.canonical_text:
        print("\n  source text (as recorded at import; the canonical text above "
              "is the authority)")
        for line in rec.source_text.splitlines():
            print(f"    {line}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def cmd_diff(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        store = skillstore.load_or_empty(root)
    except StoreError as exc:
        return _fail(str(exc))
    inv, ex = _scan(root, args)
    skill_ids = None
    if args.skill:
        skill, options = resolve_skill(inv, args.skill)
        if skill is None:
            return _fail(f"{args.skill!r} does not resolve to one skill"
                         + (f"; candidates: "
                            f"{', '.join(s.skill_id for s in options)}"
                            if options else ""))
        skill_ids = [skill.skill_id]
    report = sync_mod.diff(store, inv, ex, include_retired=args.include_retired,
                           skill_ids=skill_ids)
    if args.json:
        print(json.dumps({"command": "diff", "root": str(root),
                          **report.as_dict()}, indent=2, ensure_ascii=False))
    else:
        print(sync_mod.render(report, verbose=args.verbose))
    if report.discovery_failed:
        return EXIT_ERROR
    return EXIT_OK if report.clean else EXIT_FINDING


# ---------------------------------------------------------------------------
# update / retire
# ---------------------------------------------------------------------------

def cmd_update(args) -> int:
    """Accept a reported source change into the canonical record.

    The brief's §D rule — *"Do not automatically overwrite canonical constraint
    text … require an explicit update to the canonical record"* — is this
    command and nothing else. It takes constraint ids, never a wildcard over
    `SOURCE_CHANGED`, so accepting twenty changes is twenty named decisions on
    one command line rather than one unexamined `--all`.
    """
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        store = skillstore.load_or_empty(root)
    except StoreError as exc:
        return _fail(str(exc))
    inv, ex = _scan(root, args)
    report = sync_mod.diff(store, inv, ex)
    if report.discovery_failed:
        print(sync_mod.render(report))
        return EXIT_ERROR

    rows = {r.constraint_id: r for r in report.rows if r.constraint_id}
    changed: list[str] = []
    refreshed: list[str] = []
    when = args.when or _today()
    for cid in args.constraint:
        row = rows.get(cid)
        if row is None:
            return _fail(f"{cid}: not in the store, or not reported by diff")
        if row.state == sync_mod.SOURCE_CHANGED:
            cand = ex.by_id.get(row.candidate_id)
            if cand is None:
                return _fail(f"{cid}: diff located a successor but it is no longer "
                             f"in the scan; re-run `diff`")
            store.update_canonical(
                cid, canonical_text=cand.source_text.strip(),
                source_text=cand.source_text, source_file=cand.source_file,
                source_span=tuple(cand.source_span),
                source_lines=tuple(cand.source_lines),
                source_hash=cand.source_hash, when=when)
            changed.append(cid)
        elif row.state == sync_mod.UNCHANGED and row.span_moved is not None:
            if not args.refresh_provenance:
                return _fail(f"{cid}: the text is unchanged and only its position "
                             f"moved. Pass --refresh-provenance to record the new "
                             f"span; that does not bump constraint_version, "
                             f"because the constraint did not change.")
            store.refresh_provenance(
                cid, source_file=row.source_file,
                source_span=tuple(row.span_moved),
                source_lines=tuple(row.new_lines or (0, 0)), when=when)
            refreshed.append(cid)
        else:
            return _fail(f"{cid}: diff reports {row.state}; `update` only accepts a "
                         f"SOURCE_CHANGED record, or an UNCHANGED one whose span "
                         f"moved. A SOURCE_REMOVED record is retired, not updated.")
    path = skillstore.write(store)
    if args.json:
        print(json.dumps({"command": "update", "store": str(path),
                          "canonical_updated": changed,
                          "provenance_refreshed": refreshed},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    for cid in changed:
        rec = store.by_id[cid]
        print(f"{cid}: canonical text updated to v{rec.constraint_version}")
    for cid in refreshed:
        print(f"{cid}: provenance refreshed; constraint_version unchanged")
    print(f"store: {path}")
    return EXIT_OK


def cmd_retire(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        store = skillstore.load_or_empty(root)
    except StoreError as exc:
        return _fail(str(exc))
    when = args.when or _today()
    try:
        done = [store.retire(cid, when, args.reason) for cid in args.constraint]
    except StoreError as exc:
        return _fail(str(exc))
    path = skillstore.write(store)
    if args.json:
        print(json.dumps({"command": "retire", "store": str(path),
                          "retired": [c.constraint_id for c in done],
                          "reason": args.reason}, indent=2, ensure_ascii=False))
        return EXIT_OK
    for rec in done:
        print(f"{rec.constraint_id}: retired ({args.reason or 'no reason given'})")
    print(f"store: {path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------

def cmd_deliver(args) -> int:
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    harness = Path(args.harness).resolve() if args.harness else None
    try:
        proof = deliver_mod.prove(root, args.skill, harness_root=harness,
                                  prefer_harness=not args.no_harness)
    except (StoreError, KeyError, FileNotFoundError) as exc:
        return _fail(str(exc).strip("'"))
    if args.json:
        payload = proof.as_dict()
        if args.show_input:
            payload["model_input_prefix"] = proof.model_input[:args.show_input]
        print(json.dumps({"command": "deliver", **payload}, indent=2,
                         ensure_ascii=False))
    else:
        print(deliver_mod.render(proof, show_input=args.show_input))
    return EXIT_OK if proof.established else EXIT_FINDING


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def cmd_report(args) -> int:
    """Acceptance criterion 11, computed rather than written down."""
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    inv, ex = _scan(root, args)
    try:
        store = skillstore.load_or_empty(root)
    except StoreError as exc:
        return _fail(str(exc))
    report = sync_mod.diff(store, inv, ex)
    tiers = ex.tier_counts()
    confirmed = len(store.active)
    skills_with = len({c.skill_id for c in store.active})
    payload = {
        "root": str(root),
        "skills_dir": args.skills_dir or "",
        "skills_found": len(inv.skills),
        "skills_by_collection": {},
        "source_files_scanned": sum(len(s.files) for s in inv.skills),
        "source_files_skipped": sum(len(s.skipped) for s in inv.skills),
        "blocks_examined": ex.blocks_seen,
        "blocks_excluded": ex.excluded,
        "candidate_constraints": len(ex.candidates),
        "candidates_by_tier": tiers,
        "confirmed_constraints": confirmed,
        "skills_with_confirmed_constraints": skills_with,
        "unresolved_extraction_cases": len(ex.unresolved),
        "marker_issues": len(ex.marker_issues),
        "sync": report.counts() if not report.discovery_failed else "DISCOVERY_FAILED",
        "limitations": list(LIMITATIONS),
    }
    for s in inv.skills:
        key = s.collection or "(repository root)"
        payload["skills_by_collection"][key] = \
            payload["skills_by_collection"].get(key, 0) + 1
    if args.json:
        print(json.dumps({"command": "report", **payload}, indent=2,
                         ensure_ascii=False))
        return EXIT_OK
    print(f"ConstraintRelay Phase 1 — {root}")
    print(f"  skills found                      {payload['skills_found']}")
    for key, n in sorted(payload["skills_by_collection"].items(),
                         key=lambda kv: -kv[1]):
        print(f"    {n:>4} in {key}")
    print(f"  source files scanned              {payload['source_files_scanned']}")
    print(f"  source files skipped              {payload['source_files_skipped']}")
    print(f"  blocks examined                   {payload['blocks_examined']}")
    print(f"  candidate constraints             {payload['candidate_constraints']}")
    print(f"    operative                       {tiers[OPERATIVE]}")
    print(f"    advisory                        {tiers[ADVISORY]}")
    print(f"    author-marked                   {tiers[AUTHOR_MARKER]}")
    print(f"  confirmed / imported              {confirmed}")
    print(f"  skills with constraints           {skills_with}")
    print(f"  unresolved extraction cases       "
          f"{payload['unresolved_extraction_cases']}")
    print(f"  marker configuration issues       {payload['marker_issues']}")
    print(f"  sync against the store            "
          f"{payload['sync'] if isinstance(payload['sync'], str) else ', '.join(f'{k} {v}' for k, v in payload['sync'].items())}")
    print("\n  known limitations")
    for item in LIMITATIONS:
        print(f"    - {item}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# active  (v0.1 product surface)
# ---------------------------------------------------------------------------

def cmd_active(args) -> int:
    """What would the model see if these skills were active?

    The v0.1 delivery artifact, shown without needing a model or an API key:
    the active-set capsule is a pure function of the store and the active set,
    so it can be previewed exactly. This is the block that
    `cf.lifetime.ConstraintAwareLLM` appends to every request — not the
    load-time prepend that `deliver` demonstrates, which Phase 3 superseded.
    """
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        cf = ConstraintFlow.open(root)
    except StoreError as exc:
        return _fail(str(exc))

    active = lifetime_mod.ActiveSkillSet()
    for token in args.skill:
        skill_id, options = cf.resolve(token)
        if not skill_id:
            return _fail(f"{token!r} does not resolve to one skill in the store"
                         + (f"; candidates: {', '.join(options)}"
                            if options else ""))
        active.activate(skill_id, source=lifetime_mod.SRC_HOST)
    for token in args.release:
        skill_id, _ = cf.resolve(token)
        if not skill_id or not active.release(skill_id, "preview",
                                              source=lifetime_mod.SRC_HOST):
            return _fail(f"cannot release {token!r}: it is not active here")

    injection = lifetime_mod.render_active(cf, active.ids, dedup=not args.no_dedup)
    chars = injection.chars if injection else 0
    tokens = chars // 4
    if args.json:
        print(json.dumps({
            "command": "active", "active": active.ids,
            "released": active.released,
            "capsule_chars": chars, "capsule_est_tokens": tokens,
            "injection": injection.as_dict() if injection else None,
            "transitions": active.audit_rows(),
            "capsule": injection.content if injection and args.show else None,
        }, indent=2, ensure_ascii=False))
        return EXIT_OK

    print(f"active set: {len(active)} skill(s)")
    for skill_id in active.ids:
        n = len(cf.get_constraints(skill_id))
        rec = cf.skill_record(skill_id)
        print(f"  {n:>3} constraint(s)  hold={cf.hold_scope(skill_id):<5} "
              f"{rec.name if rec else skill_id}")
    for skill_id in active.released:
        print(f"    released: {skill_id}")
    if injection is None:
        print("\nnothing would be injected: no active skill has a recorded "
              "constraint")
        return EXIT_OK
    print(f"\ncapsule: {chars} chars, ~{tokens} est. tokens, "
          f"{len(injection.constraint_ids)} constraint(s)")
    if injection.deduped_count:
        print(f"  exact dedup merged {injection.deduped_count} duplicate(s), "
              f"saving {injection.deduped_chars} chars")
    for skill_id, why in injection.skipped:
        print(f"  skipped {skill_id}: {why}")
    print("  This block is rebuilt from the store and appended to every model "
          "request,\n  so it does not depend on surviving compaction.")
    if args.show:
        print("\n--- the block, as the model receives it ---")
        print(injection.content)
    return EXIT_OK


# ---------------------------------------------------------------------------
# init / scope  (v0.1 product surface)
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Create `.constraintflow/` and show what is there to confirm.

    The first command a new user runs. It scans and reports; it writes no
    constraint, because `import --confirm-all` is the act of confirmation and
    `init` is not that act. Idempotent: running it on a project that already has
    a store reports the store instead of rescanning over it.
    """
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    existing = skillstore.find(root)
    if existing is not None and not args.force:
        store = skillstore.load(existing)
        print(f"already initialised: {existing}")
        print(f"  {len(store.skills)} skill(s), {len(store.active)} active "
              f"constraint(s)")
        print(f"\nnext: {naming.cmd('skills', root)}")
        return EXIT_OK

    inv, ex = _scan(root, args)
    if not inv.skills:
        return _fail(f"no skills found under {root}"
                     + (f"/{args.skills_dir}" if args.skills_dir else "")
                     + ". A skill is a directory containing SKILL.md. If your "
                       "skills live in a subdirectory, pass --skills-dir.")
    (root / skillstore.STORE_DIR).mkdir(parents=True, exist_ok=True)
    tiers = ex.tier_counts()
    if args.json:
        print(json.dumps({"command": "init", "root": str(root),
                          "store_dir": str(root / skillstore.STORE_DIR),
                          "skills_found": len(inv.skills),
                          "candidates": len(ex.candidates),
                          "tier_counts": tiers}, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"initialised {root / skillstore.STORE_DIR}")
    print(f"  {len(inv.skills)} skill(s) found")
    print(f"  {len(ex.candidates)} candidate constraint(s) "
          f"({tiers[OPERATIVE]} operative, {tiers[ADVISORY]} advisory)")
    print(f"  {len(ex.unresolved)} unresolved extraction case(s)")
    print("\nNothing is in the store yet — extraction is heuristic, so it never "
          "promotes text on its own.")
    print(f"  review candidates:  {naming.cmd('import', root)}")
    print(f"  confirm all:        {naming.cmd('import', root)} --confirm-all")
    print(f"  confirm obligations {naming.cmd('import', root)} --confirm-all "
          f"--tier operative")
    return EXIT_OK


def cmd_scope(args) -> int:
    """Declare a skill's `hold_scope`. The only thing that ever sets `phase`.

    v0.1 lifetime semantics: `task` is the default and nothing infers `phase`.
    Phase 5 measured why guessing would be wrong — the benefit of releasing
    tracks the task/phase split of constraint mass almost exactly, so a wrong
    `phase` is not a missed optimisation, it is a dropped constraint that still
    applied.
    """
    root = _root(args)
    if root is None:
        return EXIT_ERROR
    try:
        store = skillstore.load_or_empty(root)
    except StoreError as exc:
        return _fail(str(exc))
    if not store.skills:
        return _fail(f"no skills in the store at {root}; run "
                     f"`{naming.cmd('import', root)} --confirm-all` first")

    cf = ConstraintFlow(store)
    if not args.skill:
        views = cf.list_skills()
        if args.json:
            print(json.dumps({"command": "scope",
                              "skills": [v.as_dict() for v in views]},
                             indent=2, ensure_ascii=False))
            return EXIT_OK
        phase = [v for v in views if v.hold_scope == skillstore.HOLD_PHASE]
        print(f"{len(views)} skill(s); {len(phase)} declared `phase`, the rest "
              f"`task` (the conservative default)")
        for v in views:
            if v.hold_scope != skillstore.HOLD_TASK or args.all:
                print(f"  {v.hold_scope:<6} {v.constraint_count:>4}  "
                      f"{v.name:<34} {v.skill_id}")
        if not phase and not args.all:
            print("  (none declared `phase`; pass --all to list every skill)")
        return EXIT_OK

    skill_id, options = cf.resolve(args.skill)
    if not skill_id:
        return _fail(f"{args.skill!r} does not resolve to one skill in the store"
                     + (f"; candidates: {', '.join(options)}" if options else ""))
    rec = store.skills_by_id[skill_id]
    before = rec.hold_scope
    rec.hold_scope = args.hold
    rec.last_seen = args.when or _today()
    path = skillstore.write(store)
    if args.json:
        print(json.dumps({"command": "scope", "skill_id": skill_id,
                          "hold_scope_before": before,
                          "hold_scope": rec.hold_scope, "store": str(path)},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"{rec.name} ({skill_id}): hold_scope {before} -> {rec.hold_scope}")
    if rec.hold_scope == skillstore.HOLD_PHASE:
        print("  `phase` means this skill's constraints may be released when "
              "its phase ends. Nothing releases them automatically — a host "
              "calls release, or the model calls release_skill.")
    print(f"store: {path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _common(parser) -> None:
    parser.add_argument("path", help="repository to work on")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")


def _scan_opts(parser) -> None:
    parser.add_argument("--skills-dir", default="",
                        help="scope discovery to this repository-relative subtree")
    parser.add_argument("--include-project", action="store_true",
                        help=f"also import root-level instruction files, under the "
                             f"reserved skill_id {PROJECT_SKILL_ID!r}")
    parser.add_argument("--when", default="",
                        help="date recorded on the records this run writes "
                             "(default: today)")


def register(sub) -> None:
    """Add the product subcommands to an existing `add_subparsers` object."""
    p = sub.add_parser("init", help="create .constraintflow/ and report what "
                                    "was found")
    _common(p)
    _scan_opts(p)
    p.add_argument("--force", action="store_true",
                   help="re-scan a project that already has a store")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("active", help="preview the capsule the model receives "
                                      "for a set of active skills")
    _common(p)
    p.add_argument("--skill", action="append", default=[], required=True,
                   help="skill to activate; repeatable")
    p.add_argument("--release", action="append", default=[],
                   help="skill to release again, to see the difference")
    p.add_argument("--show", action="store_true",
                   help="print the block itself, not just its size")
    p.add_argument("--no-dedup", action="store_true",
                   help="disable exact deduplication, to see what it saves")
    p.set_defaults(func=cmd_active)

    p = sub.add_parser("scope", help="show or declare a skill's hold_scope "
                                     "(task | phase)")
    _common(p)
    p.add_argument("--skill", default="",
                   help="skill to change; omitted lists declared scopes")
    p.add_argument("--hold", choices=list(skillstore.HOLD_SCOPES),
                   default=skillstore.HOLD_PHASE,
                   help="the scope to declare (default: phase, since `task` is "
                        "already every skill's default)")
    p.add_argument("--all", action="store_true",
                   help="when listing, include skills left at the default")
    p.add_argument("--when", default="")
    p.set_defaults(func=cmd_scope)

    p = sub.add_parser("import", help="scan a repository for skills and "
                                      "candidate constraints")
    _common(p)
    _scan_opts(p)
    p.add_argument("--confirm-all", action="store_true",
                   help="batch-confirm every candidate into the store; this flag "
                        "is the author confirmation")
    p.add_argument("--tier", choices=TIER_CHOICES, default=TIER_ALL,
                   help="restrict --confirm-all to one tier "
                        "(operative also includes author-marked)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("confirm", help="promote candidates into the authoritative store")
    _common(p)
    _scan_opts(p)
    p.add_argument("--skill", default="", help="skill_id, name or directory name")
    p.add_argument("--candidate", action="append", default=[],
                   help="candidate_id; repeatable")
    p.add_argument("--all", action="store_true", help="every candidate in scope")
    p.add_argument("--tier", choices=TIER_CHOICES, default=TIER_ALL)
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("skills", help="skills the store knows about, and their hold_scope")
    _common(p)
    p.add_argument("--with-constraints", action="store_true",
                   help="only skills that have at least one constraint")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("constraints", help="constraints belonging to a skill")
    _common(p)
    p.add_argument("--skill", required=True)
    p.add_argument("--include-retired", action="store_true")
    p.set_defaults(func=cmd_constraints)

    p = sub.add_parser("constraint", help="one constraint and where it came from")
    _common(p)
    p.add_argument("constraint_id")
    p.set_defaults(func=cmd_constraint)

    p = sub.add_parser("diff", help="what changed since the last import")
    _common(p)
    _scan_opts(p)
    p.add_argument("--skill", default="")
    p.add_argument("--include-retired", action="store_true")
    p.add_argument("--verbose", action="store_true",
                   help="every row, not the first 20 of each state")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("update", help="accept a changed source into the canonical record")
    _common(p)
    _scan_opts(p)
    p.add_argument("--constraint", action="append", default=[], required=True,
                   help="constraint_id; repeatable")
    p.add_argument("--refresh-provenance", action="store_true",
                   help="allow recording a moved span for unchanged text")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("retire", help="deactivate a constraint (the only way one stops applying)")
    _common(p)
    p.add_argument("--constraint", action="append", default=[], required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--when", default="")
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser("deliver", help="end-to-end demo: skill to model-visible constraints")
    _common(p)
    p.add_argument("--skill", required=True)
    p.add_argument("--harness", default="",
                   help="Vibe-Trading checkout (default: the repository itself)")
    p.add_argument("--no-harness", action="store_true",
                   help="use the tested port instead of importing the upstream "
                        "loader")
    p.add_argument("--show-input", type=int, default=0, metavar="N",
                   help="print the first N characters of the model input")
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("report", help="inventory report: skills, candidates, confirmed, limitations")
    _common(p)
    _scan_opts(p)
    p.set_defaults(func=cmd_report)
