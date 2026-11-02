"""v2 layer 1 — the version-bound public constraint library.

The v2 architecture splits what v0.1 did in one step:

    BUILD TIME   skill@version -> extract -> review -> library (content-hash bound)
    RUN TIME     task -> match against library -> deliver -> record

This module is the build-time half's output format. It wraps, rather than
replaces, what `cf/skillstore.py` already records: that store binds a
*constraint* to its source span and `source_hash`, which detects per-rule drift.
What it cannot express is a **skill version identity** — so "the library was
built from skill S at version V" had nowhere to live, and "version mismatch"
could not be stated. That is the gap this module closes, and it is the whole of
its reason to exist.

Three additions over the Phase-1 record, and nothing else
---------------------------------------------------------
1. **`source_version` + per-file `content_sha256`**, at the *skill* level. A
   library entry is only meaningful against the bytes it was built from.
2. **A review tier, four-valued.** `cf/skillstore.py`'s stashed review gate had
   two states, `imported_unreviewed` and `reviewed_confirmed`. Two is not
   enough: a rule I checked, a rule a model checked, and a rule a person
   checked are three different claims, and collapsing them is how a library
   comes to be described as human-reviewed when nobody read it.
3. **A build record** — run id, call count, cost — so a delivered rule resolves
   to how it was produced.

What this module deliberately does NOT do
-----------------------------------------
It does not extract, does not call a provider, does not decide applicability,
and does not deliver. It also **does not claim that fixing reviewed rules into
a library bounds recall**: a rule nobody extracted is absent from this file and
nothing here can see that. `LibraryEntry` carries no completeness flag for that
reason.

Why JSON and not TOML
---------------------
`cf/store.py`'s TOML renderer is known not to round-trip a regex inside a
constraint. A library must round-trip arbitrary rule text verbatim or its
`source_hash` stops re-verifying, so the serialisation is JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

#: Bumped 2 -> 3 on 2026-10-14 by the Owner ruling that adds a
#: provenance-preserving Phase-1 -> v2 import path. Three fields join the entry
#: (`provenance`, `legacy_record_id`, `legacy_status`) and one joins the skill
#: binding, and all four are *epistemic* rather than audit history, so they are
#: inside `library_content_id`. A library built before the bump therefore has a
#: different content id after it, which is correct and is disclosed rather than
#: papered over: the library now says something it did not say before — where
#: each rule came from.
SCHEMA_VERSION = 3

# --- provenance of a record ------------------------------------------------
#: Extracted by this v2 build from source bytes it hashed itself.
PROV_NATIVE = "native"
#: Carried across from the Phase-1 legacy/audit store by `cf.storeboundary`.
#: **It is not a weaker version of `native` — it is a different claim.** A
#: native entry's `file_content_sha256` was computed from bytes this build read;
#: an imported entry may have none, because the Phase-1 store outlives the tree
#: it was built from.
PROV_PHASE1_IMPORT = "phase1_import"
PROVENANCES = (PROV_NATIVE, PROV_PHASE1_IMPORT)

# --- review tiers, ordered -------------------------------------------------
#: Ordered weakest to strongest. The ordering is the point: a delivery
#: threshold is expressed as "at least this tier", and a surface that reports
#: provenance can say which tier each rule actually reached.
UNREVIEWED = "unreviewed"
DEVELOPER_REVIEWED = "developer_reviewed"
MODEL_REVIEWED = "model_reviewed"
HUMAN_REVIEWED = "human_reviewed"
REVIEW_TIERS = (UNREVIEWED, DEVELOPER_REVIEWED, MODEL_REVIEWED, HUMAN_REVIEWED)
_TIER_RANK = {t: i for i, t in enumerate(REVIEW_TIERS)}


class LibraryError(Exception):
    """An unreadable, invalid or internally inconsistent library."""


def tier_rank(tier: str) -> int:
    if tier not in _TIER_RANK:
        raise LibraryError(f"unknown review tier {tier!r}; "
                           f"expected one of {list(REVIEW_TIERS)}")
    return _TIER_RANK[tier]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceFileBinding:
    """One source file, bound by content hash."""

    rel_path: str
    content_sha256: str
    chars: int

    def verify(self, text: str) -> bool:
        return sha256_text(text) == self.content_sha256

    def as_dict(self) -> dict:
        return {"rel_path": self.rel_path,
                "content_sha256": self.content_sha256, "chars": self.chars}


@dataclass(frozen=True)
class LibraryEntry:
    """One reviewed rule, bound to the exact bytes it came from.

    `review_tier` is *per entry*, never per library, because a spot-check marks
    the entries actually examined and nothing else. A library in which 10 of 60
    entries are `human_reviewed` is a library with 10 human-reviewed entries,
    not a human-reviewed library.
    """

    constraint_id: str
    skill_id: str
    source_version: str
    source_file: str
    source_span: tuple[int, int]
    source_text: str
    source_hash: str
    file_content_sha256: str
    tier: str
    extraction_reason: str
    build_run_id: str
    review_tier: str = UNREVIEWED
    reviewed_at: str = ""
    reviewed_by: str = ""
    constraint_version: int = 1
    #: EVERY build attempt that produced this entry, in order. A three-pass
    #: union or a retried pass appends here; `build_run_id` stays the first.
    #: Kept out of `library_content_id` on purpose — which attempts found a
    #: rule is audit history, not part of what the library *says*.
    produced_by_runs: tuple[str, ...] = ()
    #: Deterministic runtime selectors. Author-declared or build-time derived;
    #: NEVER inferred from the rule's prose at match time.
    tool_names: tuple[str, ...] = ()
    action_tags: tuple[str, ...] = ()
    #: Free text, carried for the reviewer. `condition` is NOT adjudicated by
    #: anything in v2 layer 3 — see `cf/selector.py`.
    condition: str = ""
    exception: str = ""
    scope: str = ""
    note: str = ""
    #: Where this record came from. See `PROVENANCES`.
    provenance: str = PROV_NATIVE
    #: The Phase-1 store's own `constraint_id` for an imported record, so the
    #: legacy row stays addressable for audit after the import. Empty for a
    #: native entry.
    legacy_record_id: str = ""
    #: The Phase-1 `status` (`active` / `retired`) as it stood at import.
    #: Carried rather than translated: v2 has no status field, and silently
    #: dropping it would make a retired legacy rule indistinguishable from a
    #: live one. Empty for a native entry.
    legacy_status: str = ""

    def verify_source_hash(self) -> bool:
        """Re-hash the recorded span text. Cheap, local, no file read."""
        return sha256_text(self.source_text) == self.source_hash

    def as_dict(self) -> dict:
        return {"constraint_id": self.constraint_id, "skill_id": self.skill_id,
                "source_version": self.source_version,
                "source_file": self.source_file,
                "source_span": list(self.source_span),
                "source_text": self.source_text,
                "source_hash": self.source_hash,
                "file_content_sha256": self.file_content_sha256,
                "tier": self.tier,
                "extraction_reason": self.extraction_reason,
                "build_run_id": self.build_run_id,
                "produced_by_runs": list(self.produced_by_runs),
                "review_tier": self.review_tier,
                "reviewed_at": self.reviewed_at,
                "reviewed_by": self.reviewed_by,
                "constraint_version": self.constraint_version,
                "tool_names": list(self.tool_names),
                "action_tags": list(self.action_tags),
                "condition": self.condition, "exception": self.exception,
                "scope": self.scope, "note": self.note,
                "provenance": self.provenance,
                "legacy_record_id": self.legacy_record_id,
                "legacy_status": self.legacy_status}

    @staticmethod
    def from_dict(d: dict) -> "LibraryEntry":
        return LibraryEntry(
            constraint_id=d["constraint_id"], skill_id=d["skill_id"],
            source_version=d["source_version"], source_file=d["source_file"],
            source_span=tuple(d["source_span"]), source_text=d["source_text"],
            source_hash=d["source_hash"],
            file_content_sha256=d["file_content_sha256"], tier=d["tier"],
            extraction_reason=d.get("extraction_reason", ""),
            build_run_id=d["build_run_id"],
            produced_by_runs=tuple(d.get("produced_by_runs",
                                         (d["build_run_id"],))),
            review_tier=d.get("review_tier", UNREVIEWED),
            reviewed_at=d.get("reviewed_at", ""),
            reviewed_by=d.get("reviewed_by", ""),
            constraint_version=int(d.get("constraint_version", 1)),
            tool_names=tuple(d.get("tool_names", ())),
            action_tags=tuple(d.get("action_tags", ())),
            condition=d.get("condition", ""),
            exception=d.get("exception", ""), scope=d.get("scope", ""),
            note=d.get("note", ""),
            provenance=d.get("provenance", PROV_NATIVE),
            legacy_record_id=d.get("legacy_record_id", ""),
            legacy_status=d.get("legacy_status", ""))


@dataclass
class SkillBinding:
    """A built skill: its version identity and every file hash behind it."""

    skill_id: str
    name: str
    source_version: str
    files: list = field(default_factory=list)   # list[SourceFileBinding]
    built_at: str = ""
    build_run_id: str = ""
    #: `native` when this build hashed the skill's files itself, so `files` is
    #: a real per-file binding. `phase1_import` when the binding was carried
    #: from the legacy store, where `files` may be **empty** — the Phase-1
    #: store records a `source_file` per constraint but no file content hash,
    #: and on this host the tree it was built from no longer exists. An empty
    #: `files` list under `native` is a defect; under `phase1_import` it is the
    #: honest statement that no file binding was available.
    provenance: str = PROV_NATIVE

    def file(self, rel_path: str) -> "SourceFileBinding | None":
        for f in self.files:
            if f.rel_path == rel_path:
                return f
        return None

    def verifiable(self) -> bool:
        """Can a later run re-check this skill against bytes on disk?"""
        return bool(self.files)

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "name": self.name,
                "source_version": self.source_version,
                "files": [f.as_dict() for f in self.files],
                "built_at": self.built_at, "build_run_id": self.build_run_id,
                "provenance": self.provenance}


@dataclass
class BuildRecord:
    """How one build run was produced. Cost and calls are first-class.

    An offline, author-marker-only build makes **zero** provider calls, and
    recording that zero is the point: a later build that made calls is
    distinguishable from one that did not, without reading a ledger.
    """

    build_run_id: str
    built_at: str
    extraction_path: str
    #: Which pass of a multi-pass build this attempt is, 1-based.
    pass_index: int = 1
    #: Which retry of that pass, 0 for the first attempt.
    attempt: int = 0
    #: `ok` | `failed` | `superseded`. A failed attempt KEEPS its row.
    outcome: str = "ok"
    #: The library content id this attempt produced, when it produced one.
    produced_content_id: str = ""
    provider_calls: int = 0
    retries: int = 0
    cost_usd: float = 0.0
    call_ledger_path: str = ""
    raw_response_dir: str = ""
    notes: str = ""

    def as_dict(self) -> dict:
        return {"build_run_id": self.build_run_id, "built_at": self.built_at,
                "extraction_path": self.extraction_path,
                "pass_index": self.pass_index, "attempt": self.attempt,
                "outcome": self.outcome,
                "produced_content_id": self.produced_content_id,
                "provider_calls": self.provider_calls, "retries": self.retries,
                "cost_usd": round(self.cost_usd, 6),
                "call_ledger_path": self.call_ledger_path,
                "raw_response_dir": self.raw_response_dir,
                "notes": self.notes}


#: Entry fields that are audit history, not content. Excluded from
#: `library_content_id` so that a second extraction pass, a retry, or a review
#: timestamp does not change the library's VERSION IDENTITY while a changed
#: rule, span, hash, selector or review tier does.
_AUDIT_ONLY_FIELDS = ("build_run_id", "produced_by_runs", "reviewed_at")

#: The same exclusion for a skill binding. `SkillBinding` carries `built_at`
#: and `build_run_id` for the audit chain, and leaving them in the content
#: digest was a real defect: two attempts over byte-identical sources produced
#: different VERSION identities, which is the exact confusion this split exists
#: to remove. Caught by running two attempts and comparing, not by reading.
_SKILL_AUDIT_ONLY_FIELDS = ("built_at", "build_run_id")


@dataclass
class Library:
    """The public constraint library. A file, versioned and content-bound.

    Two identities, deliberately separate — Owner ruling 2026-10-14:

    * **`library_content_id`** is the *version identity*. It digests the whole
      of what the library says: every skill binding and every entry's content,
      selectors and review tier. Rebuilding the same sources through the same
      path reproduces it exactly, which is what makes "version mismatch" a
      decidable question.
    * **`build_run_id`** identifies one *build attempt*. A three-pass union has
      three of them, a retried pass has another, and a failed attempt keeps its
      row. They are audit history.

    Conflating the two was the earlier defect in both directions: a clock-based
    id made the version move without the content moving, and folding the run
    into the content id would make a retry look like a new library version.
    """

    path: Path
    schema_version: int = SCHEMA_VERSION
    skills: dict = field(default_factory=dict)      # skill_id -> SkillBinding
    entries: list = field(default_factory=list)     # list[LibraryEntry]
    builds: dict = field(default_factory=dict)      # run_id -> BuildRecord

    # -- identity -----------------------------------------------------------
    def library_content_id(self) -> str:
        """Digest of everything the library SAYS. Excludes audit history.

        Reproducible by construction: the inputs are sorted and the
        audit-only fields are dropped, so two attempts over identical sources
        agree even though their `build_run_id`s differ.
        """
        skills = [{k: v for k, v in s.as_dict().items()
                   if k not in _SKILL_AUDIT_ONLY_FIELDS}
                  for _, s in sorted(self.skills.items())]
        entries = []
        for e in sorted(self.entries, key=lambda e: (e.skill_id,
                                                     e.constraint_id)):
            d = {k: v for k, v in e.as_dict().items()
                 if k not in _AUDIT_ONLY_FIELDS}
            entries.append(d)
        body = json.dumps({"schema_version": self.schema_version,
                           "skills": skills, "entries": entries},
                          sort_keys=True, ensure_ascii=False)
        return "lib-" + sha256_text(body)[:16]

    def audit_chain(self) -> dict:
        """Every build attempt, and which entries each one produced."""
        by_run: dict = {rid: [] for rid in self.builds}
        for e in self.entries:
            for rid in (e.produced_by_runs or (e.build_run_id,)):
                by_run.setdefault(rid, []).append(e.constraint_id)
        return {"library_content_id": self.library_content_id(),
                "attempts": [
                    {**self.builds[rid].as_dict(),
                     "entries_produced": sorted(by_run.get(rid, []))}
                    for rid in sorted(self.builds)],
                "total_attempts": len(self.builds),
                "total_provider_calls": sum(b.provider_calls
                                            for b in self.builds.values()),
                "total_cost_usd": round(sum(b.cost_usd
                                            for b in self.builds.values()), 6)}

    def record_attempt(self, rec: "BuildRecord") -> None:
        """Append a build attempt. Never replaces an existing row."""
        if rec.build_run_id in self.builds:
            raise LibraryError(
                f"build attempt {rec.build_run_id!r} is already recorded; an "
                "attempt row is append-only so a retry cannot erase the "
                "attempt it replaces")
        self.builds[rec.build_run_id] = rec

    # -- queries ------------------------------------------------------------
    def holds_skill(self, skill_id: str) -> bool:
        return skill_id in self.skills

    def for_skill(self, skill_id: str, *, min_tier: str = DEVELOPER_REVIEWED
                  ) -> list:
        want = tier_rank(min_tier)
        return [e for e in self.entries if e.skill_id == skill_id
                and tier_rank(e.review_tier) >= want]

    def for_skill_many(self, skill_ids, *, min_tier: str = DEVELOPER_REVIEWED
                       ) -> list:
        want = tier_rank(min_tier)
        keep = set(skill_ids)
        return [e for e in self.entries if e.skill_id in keep
                and tier_rank(e.review_tier) >= want]

    def review_tier_census(self) -> dict:
        """How many entries reached each tier. Never a single library-wide label."""
        out = {t: 0 for t in REVIEW_TIERS}
        for e in self.entries:
            out[e.review_tier] = out.get(e.review_tier, 0) + 1
        return out

    def human_reviewed_count(self) -> int:
        return sum(1 for e in self.entries if e.review_tier == HUMAN_REVIEWED)

    def provenance_census(self) -> dict:
        """How many entries came from a build, and how many from an import.

        Reported next to `review_tier_census` for the same reason that census
        exists: "the library holds 473 rules" invites the reader to assume all
        473 were extracted and checked by this flow. 473 carried across from a
        legacy store at `unreviewed` is a different sentence, and only a split
        count can say which one is true.
        """
        out = {p: 0 for p in PROVENANCES}
        for e in self.entries:
            out[e.provenance] = out.get(e.provenance, 0) + 1
        return out

    def unverifiable_skills(self) -> list:
        """Skills with no per-file hash binding, so nothing can re-verify them.

        Always empty for a native build. Non-empty after an import whose source
        tree is gone — which is a property of the import, not a bug, and has to
        be visible in the same place the counts are.
        """
        return sorted(sid for sid, s in self.skills.items()
                      if not s.verifiable())

    # -- mutation -----------------------------------------------------------
    def add_skill(self, binding: SkillBinding) -> None:
        self.skills[binding.skill_id] = binding

    def add_entry(self, entry: LibraryEntry) -> None:
        if entry.skill_id not in self.skills:
            raise LibraryError(
                f"entry {entry.constraint_id} names skill {entry.skill_id!r}, "
                "which has no binding. A rule cannot enter the library without "
                "the version identity of the source it came from.")
        if any(e.constraint_id == entry.constraint_id for e in self.entries):
            raise LibraryError(f"duplicate constraint_id {entry.constraint_id}")
        self.entries.append(entry)

    def mark_reviewed(self, constraint_id: str, *, tier: str, when: str,
                      by: str) -> None:
        """Mark ONE entry at ONE tier. Deliberately not a bulk operation.

        There is no `mark_all_reviewed`. A spot check examines the entries it
        examined, and the only honest way to record that is one call per entry
        actually read.
        """
        rank = tier_rank(tier)
        if rank == 0:
            raise LibraryError("mark_reviewed cannot set 'unreviewed'")
        if not by.strip():
            raise LibraryError("a review needs a reviewer; `by` is empty")
        for i, e in enumerate(self.entries):
            if e.constraint_id == constraint_id:
                self.entries[i] = LibraryEntry(
                    **{**e.as_dict(),
                       "source_span": tuple(e.source_span),
                       "tool_names": tuple(e.tool_names),
                       "action_tags": tuple(e.action_tags),
                       "produced_by_runs": tuple(e.produced_by_runs),
                       "review_tier": tier, "reviewed_at": when,
                       "reviewed_by": by})
                return
        raise LibraryError(f"no entry {constraint_id!r} to review")

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        payload = {
            "schema_version": self.schema_version,
            "library_content_id": self.library_content_id(),
            "_identity": ("library_content_id is the VERSION identity and is "
                          "reproducible from the sources; build_run_id "
                          "identifies one build ATTEMPT and differs per pass "
                          "and per retry. Do not use one for the other."),
            "_warning": ("review_tier is per ENTRY. A count of human_reviewed "
                         "entries is not a statement about the library. Rules "
                         "nobody extracted are absent from this file and "
                         "nothing in it can detect them."),
            "review_tier_census": self.review_tier_census(),
            "provenance_census": self.provenance_census(),
            "unverifiable_skills": self.unverifiable_skills(),
            "_provenance": ("entries at provenance 'phase1_import' were "
                            "carried from the Phase-1 legacy/audit store. The "
                            "import changed where they are STORED and nothing "
                            "about what is known of them: tier, review status "
                            "and extraction reason are the legacy values, and "
                            "no record was promoted."),
            "skills": {k: v.as_dict() for k, v in sorted(self.skills.items())},
            "builds": {k: v.as_dict() for k, v in sorted(self.builds.items())},
            "entries": [e.as_dict() for e in sorted(
                self.entries, key=lambda e: (e.skill_id, e.constraint_id))],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False)
                             + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "Library":
        if not Path(path).is_file():
            raise LibraryError(f"no library at {path}")
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise LibraryError(f"{path}: schema_version "
                               f"{raw.get('schema_version')!r} is not "
                               f"{SCHEMA_VERSION}")
        lib = Library(path=Path(path), schema_version=SCHEMA_VERSION)
        for sid, s in raw.get("skills", {}).items():
            lib.skills[sid] = SkillBinding(
                skill_id=s["skill_id"], name=s["name"],
                source_version=s["source_version"],
                files=[SourceFileBinding(f["rel_path"], f["content_sha256"],
                                         int(f["chars"]))
                       for f in s.get("files", [])],
                built_at=s.get("built_at", ""),
                build_run_id=s.get("build_run_id", ""),
                provenance=s.get("provenance", PROV_NATIVE))
        for rid, b in raw.get("builds", {}).items():
            lib.builds[rid] = BuildRecord(
                build_run_id=b["build_run_id"], built_at=b["built_at"],
                extraction_path=b["extraction_path"],
                pass_index=int(b.get("pass_index", 1)),
                attempt=int(b.get("attempt", 0)),
                outcome=b.get("outcome", "ok"),
                produced_content_id=b.get("produced_content_id", ""),
                provider_calls=int(b.get("provider_calls", 0)),
                retries=int(b.get("retries", 0)),
                cost_usd=float(b.get("cost_usd", 0.0)),
                call_ledger_path=b.get("call_ledger_path", ""),
                raw_response_dir=b.get("raw_response_dir", ""),
                notes=b.get("notes", ""))
        lib.entries = [LibraryEntry.from_dict(e) for e in raw.get("entries", [])]
        want = raw.get("library_content_id")
        if want and want != lib.library_content_id():
            raise LibraryError(
                f"{path}: recorded library_content_id {want!r} does not match "
                f"the content, which digests to {lib.library_content_id()!r}. "
                "The file was edited after it was written.")
        return lib


def file_digest(path: Path) -> str:
    """Hash a source file's exact bytes as text.

    Read with `newline=""` so CRLF survives — CF-020 was exactly this: reading
    with `Path.read_text()` rewrites CRLF to LF, and then every offset and hash
    describes text that is not on disk.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return sha256_text(fh.read())
