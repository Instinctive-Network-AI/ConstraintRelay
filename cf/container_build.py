"""0.2 — the container's build step, inside the package.

This was `v2/build_library.py`, which is a script in the repository and not in
the wheel. The 0.2 acceptance installs a wheel into a fresh venv **outside the
repository** and forbids depending on unpackaged `v2/` files, so the logic
lives here and the script became a thin re-export.

Author-marked rules only, so **zero provider calls**.

Why author markers
------------------
`<!-- constraintflow: critical id=... -->` is the one extraction tier that is
not a heuristic — it is the author having already decided. It is
deterministic, reproducible and needs no model. **It is also not
representative of extraction recall**, and nothing this module produces bears
on that question.

Review tier
-----------
Entries land at `developer_reviewed`: a developer read each marked rule against
its source and filled `condition` / `exception` / `scope` from the surrounding
text. That is a real review and it is **not** a human review of a public
library, nor an author confirmation. Author confirmation is a separate
artifact — see `cf/container_attest.py` and `cf/container.py`'s manifest.

Selector and review annotations come from an optional per-skill sidecar,
`.constraintflow-selectors.json`. Absent sidecar, or absent entry, means
**skill-wide**: a rule with no declared narrowing is broader than one with, so
silence must not exclude it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .candidates import extract
from .inventory import discover_skills
from .library import (BuildRecord, DEVELOPER_REVIEWED, Library, LibraryEntry,
                      SkillBinding, SourceFileBinding, file_digest)

AUTHOR_MARKER = "author_marker"
SELECTOR_SIDECAR = ".constraintflow-selectors.json"


class BuildError(Exception):
    """A build that cannot proceed as asked."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_selectors(skill_dir: Path) -> dict:
    p = Path(skill_dir) / SELECTOR_SIDECAR
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("rules", {})


def attempt_id(pass_index: int, attempt: int, stamp: str) -> str:
    """Identify one build ATTEMPT. Distinct per pass and per retry.

    Deliberately NOT content-derived: two passes over identical sources are two
    attempts and must stay distinguishable in the audit chain. The reproducible
    quantity is `Library.library_content_id()`, which excludes this.
    """
    return f"run-p{pass_index}.a{attempt}-{stamp}"


def build_library(*, source_root, skills_dir: str = "skills",
                  skill_ids: list, source_version: str, out,
                  reviewer: str = "developer:unnamed", built_at: str = "",
                  pass_index: int = 1, attempt: int = 0,
                  append_to=None) -> dict:
    """Build the version-bound library for *skill_ids*. Zero provider calls.

    Returns a summary dict. `provider_calls` and `cost_usd` are always 0 and
    are reported rather than assumed, so a later build that did spend is
    distinguishable without reading a ledger.
    """
    source_root = Path(source_root).resolve()
    out = Path(out).resolve()
    inv = discover_skills(source_root, skills_dir=skills_dir)
    known = {s.skill_id: s for s in inv.skills}
    missing = [s for s in skill_ids if s not in known]
    if missing:
        raise BuildError(f"no such skill(s) in {source_root}: {missing}\n"
                         f"available: {sorted(known)}")

    stamp = built_at or _now()
    path_label = "cf.candidates author_marker tier only"
    run_id = attempt_id(pass_index, attempt, stamp)

    if append_to is not None and Path(append_to).is_file():
        lib = Library.load(Path(append_to))
        lib.path = out
        prior = {e.constraint_id: e for e in lib.entries}
        lib.entries = []
    else:
        lib = Library(path=out)
        prior = {}
    lib.record_attempt(BuildRecord(
        build_run_id=run_id, built_at=stamp, extraction_path=path_label,
        pass_index=pass_index, attempt=attempt, outcome="ok",
        provider_calls=0, retries=0, cost_usd=0.0,
        call_ledger_path="", raw_response_dir="",
        notes="Offline deterministic build. No provider was contacted, so "
              "there is no raw response to preserve and no retry that could "
              "overwrite one. Author-marker recall is not extraction recall."))

    lib.skills = {}
    for sid in skill_ids:
        s = known[sid]
        files = []
        for f in s.files:
            p = source_root / f.rel_path
            files.append(SourceFileBinding(
                rel_path=f.rel_path, content_sha256=file_digest(p),
                chars=len(f.text)))
        lib.add_skill(SkillBinding(
            skill_id=sid, name=sid.rsplit("/", 1)[-1],
            source_version=source_version, files=files,
            built_at=stamp, build_run_id=run_id))

    ex = extract(inv, skill_ids=skill_ids)
    sidecars = {sid: load_selectors(source_root / known[sid].rel_dir)
                for sid in skill_ids}
    file_sha = {(sid, f.rel_path): fb.content_sha256
                for sid in skill_ids
                for fb, f in zip(lib.skills[sid].files, known[sid].files)}

    kept, skipped = 0, 0
    for c in ex.candidates:
        if c.tier != AUTHOR_MARKER:
            skipped += 1
            continue
        cid = c.declared_id or c.candidate_id
        ann = sidecars.get(c.skill_id, {}).get(cid, {})
        lib.add_entry(LibraryEntry(
            constraint_id=cid, skill_id=c.skill_id,
            source_version=source_version, source_file=c.source_file,
            source_span=tuple(c.source_span), source_text=c.source_text,
            source_hash=c.source_hash,
            file_content_sha256=file_sha[(c.skill_id, c.source_file)],
            tier=c.tier, extraction_reason=c.extraction_reason,
            build_run_id=(prior[cid].build_run_id if cid in prior else run_id),
            produced_by_runs=((prior[cid].produced_by_runs or
                               (prior[cid].build_run_id,)) + (run_id,)
                              if cid in prior else (run_id,)),
            review_tier=DEVELOPER_REVIEWED,
            reviewed_at=(prior[cid].reviewed_at if cid in prior else stamp),
            reviewed_by=reviewer,
            tool_names=tuple(ann.get("tool_names", ())),
            action_tags=tuple(ann.get("action_tags", ())),
            condition=ann.get("condition", ""),
            exception=ann.get("exception", ""), scope=ann.get("scope", ""),
            note=ann.get("note", "")))
        kept += 1

    content_id = lib.library_content_id()
    lib.builds[run_id] = BuildRecord(
        **{**lib.builds[run_id].as_dict(), "produced_content_id": content_id})
    lib.save()
    return {"build_run_id": run_id, "library_content_id": content_id,
            "pass_index": pass_index, "attempt": attempt,
            "attempts_recorded": len(lib.builds), "library": str(out),
            "skills_built": list(skill_ids), "entries": kept,
            "candidates_skipped_not_author_marked": skipped,
            "provider_calls": 0, "cost_usd": 0.0,
            "review_tier_census": lib.review_tier_census(),
            "human_reviewed_entries": lib.human_reviewed_count(),
            "_review_tier_is_not_confirmation": (
                "developer_reviewed means a developer read the rule against "
                "its source. Author confirmation is a separate artifact and "
                "no entry here carries it.")}
