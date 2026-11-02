"""Phase 1 layer C — the canonical Constraint Store, keyed by skill.

    .constraintflow/skills.toml

Why a second store file
-----------------------
`cf/store.py` already implements an authoritative store and it is not being
replaced. It holds the v0.7 record — id, text, version, status, provenance,
scope — and `capsule.py`, `receipt.py` and the frozen qualification evidence all
read it. The Phase-1 record is a different shape: it is **owned by a skill** and
it splits the one `provenance` string into the four fields the brief names
separately (`source_file`, `source_span`, `source_hash`, and the canonical text
kept apart from the source text). Widening `store.toml` in place would have
meant touching every file that reads it, including the paused qualification
evidence the brief says to preserve.

So this is a new file next to the old one, and nothing reads both. That is the
reversible choice: Phase 1 can be deleted by deleting one file and one module,
and the v0.7 surface still works.

What "authoritative after confirmation" means here
--------------------------------------------------
`canonical_text` is the constraint. `source_text` and `source_file` /
`source_span` / `source_hash` are a **record of where it came from**, not a
second copy with equal standing. Three consequences are implemented rather than
documented:

* `sync` never writes `canonical_text`. A changed source produces a reported
  delta and an explicit `update` is the only thing that moves the canonical
  record (brief §D).
* a constraint whose skill directory has vanished stays in the store with
  `status = "active"`. Discovery failing is not an author retiring a constraint,
  and `retire` is the only thing that changes status (brief criterion 8).
* `constraint_version` increments only on an explicit canonical-text update, so
  the version is a count of author decisions and not of re-scans.

Numbering
---------
`constraint_id` is `<prefix>-C<nnn>`, where `<prefix>` is bound to a `skill_id`
once, in the `[[skill]]` record, and never recomputed. Numbers come from
`max + 1` over everything the store has ever held for that skill, including
retired records, so an id is never reused and a removed constraint does not
cause the next import to renumber.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STORE_DIR = ".constraintflow"
STORE_NAME = "skills.toml"
SUPPORTED_VERSION = 1

ACTIVE = "active"
RETIRED = "retired"
STATUSES = (ACTIVE, RETIRED)

#: How long a skill's constraints stay in the active set once it is loaded —
#: the v0.1 lifetime declaration.
#:
#: `task` is the conservative default and the only value an import ever writes:
#: the constraints apply until the task ends, so correctness never depends on
#: anything choosing to release the skill. `phase` says the skill belongs to one
#: phase of the work and may be released when that phase is over.
#:
#: **`phase` is never inferred.** Phase 5 measured that the benefit of releasing
#: tracks the task/phase split of constraint mass almost exactly (23.5%
#: releasable -> 0% saved; 70% -> 43.9%), which makes the split worth declaring —
#: and makes guessing it exactly the wrong move, because a wrong `phase` drops a
#: constraint that still applied. Only `constraintrelay scope --phase` sets it.
HOLD_TASK = "task"
HOLD_PHASE = "phase"
HOLD_SCOPES = (HOLD_TASK, HOLD_PHASE)

_SLUG = re.compile(r"[^a-z0-9]+")
_ID_NUMBER = re.compile(r"-C(\d+)\Z")

_SKILL_KEYS = {"skill_id", "name", "prefix", "rel_dir", "first_imported",
               "last_seen", "status", "hold_scope"}
_CONSTRAINT_KEYS = {"constraint_id", "skill_id", "canonical_text", "source_text",
                    "source_file", "source_span", "source_lines", "source_hash",
                    "constraint_version", "status", "created_at", "updated_at",
                    "extraction_reason", "tier", "candidate_id", "note"}


class StoreError(Exception):
    """An unreadable or invalid store. Always a configuration error (exit 1)."""


def today() -> str:
    return _dt.date.today().isoformat()


def slug(value: str) -> str:
    out = _SLUG.sub("-", value.strip().lower()).strip("-")
    return out or "skill"


def _short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:4]


# ---------------------------------------------------------------------------
# TOML rendering
# ---------------------------------------------------------------------------

_BASIC_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
                  "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(value: str) -> str:
    """*value* as a TOML string, chosen so the common case stays readable.

    This function exists because the obvious rendering is wrong on real input.
    `store.py` writes `text = \"\"\"\\n{text}\\n\"\"\"`, a *basic* multi-line
    string, in which TOML processes backslash escapes — so a constraint
    containing `\\d` (a regex quoted in prose, and the Vibe-Trading corpus has
    several) is an invalid escape sequence and the file will not parse back. A
    text ending in `"` closes the delimiter early.

    Three forms, in order of preference:

    1. a single-line **literal** string `'...'` when the text has no newline,
       no `'` and no control character — no escaping, nothing to get wrong;
    2. a multi-line literal `'''...'''` when the text has no `'''` and does not
       end in `'`;
    3. a basic string with every escape applied, which always works and is only
       reached by text that defeated both literal forms.

    Every write is parsed back before it reaches disk, so a defect here fails
    the write instead of producing a store that cannot be read.
    """
    # Tab and newline are the only control characters a TOML literal string may
    # contain. A carriage return is **not** one of them — `tomllib` rejects a
    # bare `\r` inside `'''…'''` — so any text carrying one goes to the basic
    # form, where it becomes `\r`. CRLF-authored instruction files are a real
    # population (see `discover.read_source`), so this path is reached.
    has_control = any(ord(ch) < 0x20 and ch not in "\n\t" for ch in value)
    if "\n" not in value and "'" not in value and not has_control:
        return f"'{value}'"
    if "'''" not in value and not value.endswith("'") and not has_control:
        # A leading newline directly after the opening delimiter is trimmed by
        # TOML, so it is added deliberately to keep the text starting in
        # column 1 of its own line.
        return f"'''\n{value}'''"
    out = []
    for ch in value:
        if ch in _BASIC_ESCAPES:
            out.append(_BASIC_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _ints(values) -> str:
    return "[" + ", ".join(str(int(v)) for v in values) + "]"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    """What the store knows about a skill, independent of the filesystem."""

    skill_id: str
    name: str
    prefix: str
    rel_dir: str = ""
    first_imported: str = ""
    last_seen: str = ""
    status: str = ACTIVE
    #: `task` (default) or `phase`. See `HOLD_SCOPES`.
    hold_scope: str = HOLD_TASK

    @property
    def phase_scoped(self) -> bool:
        return self.hold_scope == HOLD_PHASE

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "name": self.name,
                "prefix": self.prefix, "rel_dir": self.rel_dir,
                "first_imported": self.first_imported,
                "last_seen": self.last_seen, "status": self.status,
                "hold_scope": self.hold_scope}


@dataclass
class ConstraintRecord:
    """One confirmed constraint. `canonical_text` is the authority."""

    constraint_id: str
    skill_id: str
    canonical_text: str
    source_text: str
    source_file: str
    source_span: tuple[int, int]
    source_hash: str
    constraint_version: int = 1
    status: str = ACTIVE
    created_at: str = ""
    updated_at: str = ""
    source_lines: tuple[int, int] = (0, 0)
    extraction_reason: str = ""
    tier: str = ""
    candidate_id: str = ""
    note: str = ""

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest()

    @property
    def number(self) -> int:
        m = _ID_NUMBER.search(self.constraint_id)
        return int(m.group(1)) if m else 0

    @property
    def locator(self) -> str:
        lo, hi = self.source_lines
        span = f"{lo}" if hi <= lo else f"{lo}-{hi}"
        return f"{self.source_file}:{span}" if lo else self.source_file

    def as_dict(self) -> dict:
        return {"constraint_id": self.constraint_id, "skill_id": self.skill_id,
                "canonical_text": self.canonical_text,
                "canonical_sha256": self.canonical_sha256,
                "source_text": self.source_text,
                "source_file": self.source_file,
                "source_span": list(self.source_span),
                "source_lines": list(self.source_lines),
                "source_hash": self.source_hash,
                "constraint_version": self.constraint_version,
                "status": self.status, "created_at": self.created_at,
                "updated_at": self.updated_at,
                "extraction_reason": self.extraction_reason,
                "tier": self.tier, "candidate_id": self.candidate_id,
                "note": self.note}


@dataclass
class SkillStore:
    path: Path
    version: int = SUPPORTED_VERSION
    skills: list[SkillRecord] = field(default_factory=list)
    constraints: list[ConstraintRecord] = field(default_factory=list)

    # -- lookups ----------------------------------------------------------
    @property
    def skills_by_id(self) -> dict[str, SkillRecord]:
        return {s.skill_id: s for s in self.skills}

    @property
    def by_id(self) -> dict[str, ConstraintRecord]:
        return {c.constraint_id: c for c in self.constraints}

    def for_skill(self, skill_id: str, *, include_retired: bool = False
                  ) -> list[ConstraintRecord]:
        return [c for c in self.constraints
                if c.skill_id == skill_id and (include_retired or c.active)]

    @property
    def active(self) -> list[ConstraintRecord]:
        return [c for c in self.constraints if c.active]

    def by_candidate(self) -> dict[str, ConstraintRecord]:
        return {c.candidate_id: c for c in self.constraints if c.candidate_id}

    # -- mutation ---------------------------------------------------------
    def _prefix_for(self, skill_id: str, basename: str) -> str:
        taken = {s.prefix: s.skill_id for s in self.skills}
        base = slug(basename)
        if taken.get(base) in (None, skill_id):
            return base
        return f"{base}.{_short(skill_id)}"

    def upsert_skill(self, skill_id: str, name: str, basename: str,
                     rel_dir: str, when: str) -> SkillRecord:
        """Record or refresh a skill. The `prefix` is bound once and kept."""
        existing = self.skills_by_id.get(skill_id)
        if existing is not None:
            existing.name = name or existing.name
            existing.rel_dir = rel_dir or existing.rel_dir
            existing.last_seen = when
            return existing
        rec = SkillRecord(skill_id=skill_id, name=name,
                          prefix=self._prefix_for(skill_id, basename),
                          rel_dir=rel_dir, first_imported=when, last_seen=when)
        self.skills.append(rec)
        return rec

    def next_id(self, skill_id: str) -> str:
        rec = self.skills_by_id.get(skill_id)
        if rec is None:
            raise StoreError(f"no skill record for {skill_id!r}; call "
                             f"upsert_skill before allocating a constraint id")
        used = max((c.number for c in self.constraints
                    if c.skill_id == skill_id), default=0)
        return f"{rec.prefix}-C{used + 1:03d}"

    def add(self, rec: ConstraintRecord) -> ConstraintRecord:
        if rec.constraint_id in self.by_id:
            raise StoreError(f"duplicate constraint_id {rec.constraint_id!r}")
        self.constraints.append(rec)
        return rec

    def retire(self, constraint_id: str, when: str, note: str = "") -> ConstraintRecord:
        rec = self.by_id.get(constraint_id)
        if rec is None:
            raise StoreError(f"unknown constraint_id {constraint_id!r}")
        rec.status = RETIRED
        rec.updated_at = when
        if note:
            rec.note = note
        return rec

    def update_canonical(self, constraint_id: str, *, canonical_text: str,
                         source_text: str, source_file: str,
                         source_span: tuple[int, int],
                         source_lines: tuple[int, int], source_hash: str,
                         when: str) -> ConstraintRecord:
        """Accept a changed source into the canonical record — brief §D.

        The only path by which `canonical_text` ever changes, and it bumps
        `constraint_version`. Called from `cmd_update`, never from `sync`.
        """
        rec = self.by_id.get(constraint_id)
        if rec is None:
            raise StoreError(f"unknown constraint_id {constraint_id!r}")
        if rec.canonical_text != canonical_text:
            rec.constraint_version += 1
        rec.canonical_text = canonical_text
        rec.source_text = source_text
        rec.source_file = source_file
        rec.source_span = tuple(source_span)
        rec.source_lines = tuple(source_lines)
        rec.source_hash = source_hash
        rec.updated_at = when
        return rec

    def refresh_provenance(self, constraint_id: str, *, source_file: str,
                           source_span: tuple[int, int],
                           source_lines: tuple[int, int], when: str
                           ) -> ConstraintRecord:
        """Record that unchanged text moved within its file.

        Separate from `update_canonical` and deliberately does **not** touch
        `constraint_version`: the constraint did not change, its line numbers
        did, and conflating the two would make the version meaningless.
        """
        rec = self.by_id.get(constraint_id)
        if rec is None:
            raise StoreError(f"unknown constraint_id {constraint_id!r}")
        rec.source_file = source_file
        rec.source_span = tuple(source_span)
        rec.source_lines = tuple(source_lines)
        rec.updated_at = when
        return rec


# ---------------------------------------------------------------------------
# Load / parse
# ---------------------------------------------------------------------------

def store_path(project_root: Path) -> Path:
    return project_root / STORE_DIR / STORE_NAME


def find(project_root: Path) -> Path | None:
    p = store_path(project_root)
    return p if p.is_file() else None


def load(path: Path) -> SkillStore:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"{path}: cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise StoreError(f"{path}: not valid TOML: {exc}") from exc
    return parse(raw, path)


def load_or_empty(project_root: Path) -> SkillStore:
    found = find(project_root)
    return load(found) if found else SkillStore(path=store_path(project_root))


def _require_str(entry: dict, key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{where}: `{key}` must be a non-empty string")
    return value


def _pair(entry: dict, key: str, where: str, *, required: bool) -> tuple[int, int]:
    value = entry.get(key)
    if value is None:
        if required:
            raise StoreError(f"{where}: missing `{key}`")
        return (0, 0)
    if not isinstance(value, list) or len(value) != 2 \
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        raise StoreError(f"{where}: `{key}` must be two integers, e.g. [12, 48]")
    return (value[0], value[1])


def parse(raw: dict, path: Path) -> SkillStore:
    if "version" not in raw:
        raise StoreError(f"{path}: missing `version`")
    if raw["version"] != SUPPORTED_VERSION:
        raise StoreError(f"{path}: skill-store version {raw['version']!r} is not "
                         f"supported by this build (expected {SUPPORTED_VERSION})")
    store = SkillStore(path=path, version=raw["version"])

    for i, entry in enumerate(raw.get("skill", []) or []):
        where = f"{path}: skill #{i + 1}"
        if not isinstance(entry, dict):
            raise StoreError(f"{where}: must be a table")
        unknown = set(entry) - _SKILL_KEYS
        if unknown:
            raise StoreError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        sid = _require_str(entry, "skill_id", where)
        if sid in store.skills_by_id:
            raise StoreError(f"{where}: duplicate skill_id {sid!r}")
        status = str(entry.get("status", ACTIVE))
        if status not in STATUSES:
            raise StoreError(f"{where}: `status` must be one of "
                             f"{', '.join(STATUSES)}, got {status!r}")
        hold = str(entry.get("hold_scope", HOLD_TASK))
        if hold not in HOLD_SCOPES:
            raise StoreError(f"{where}: `hold_scope` must be one of "
                             f"{', '.join(HOLD_SCOPES)}, got {hold!r}")
        store.skills.append(SkillRecord(
            skill_id=sid, name=str(entry.get("name", "")),
            prefix=_require_str(entry, "prefix", where),
            rel_dir=str(entry.get("rel_dir", "")),
            first_imported=str(entry.get("first_imported", "")),
            last_seen=str(entry.get("last_seen", "")), status=status,
            hold_scope=hold))

    known_skills = set(store.skills_by_id)
    seen_prefix: dict[str, str] = {}
    for rec in store.skills:
        if rec.prefix in seen_prefix:
            raise StoreError(f"{path}: prefix {rec.prefix!r} is bound to both "
                             f"{seen_prefix[rec.prefix]!r} and {rec.skill_id!r}; "
                             f"a prefix is what makes a constraint_id unique")
        seen_prefix[rec.prefix] = rec.skill_id

    for i, entry in enumerate(raw.get("constraint", []) or []):
        where = f"{path}: constraint #{i + 1}"
        if not isinstance(entry, dict):
            raise StoreError(f"{where}: must be a table")
        unknown = set(entry) - _CONSTRAINT_KEYS
        if unknown:
            raise StoreError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                             f"accepted: {', '.join(sorted(_CONSTRAINT_KEYS))}")
        cid = _require_str(entry, "constraint_id", where)
        if cid in store.by_id:
            raise StoreError(f"{where}: duplicate constraint_id {cid!r}")
        sid = _require_str(entry, "skill_id", where)
        if sid not in known_skills:
            raise StoreError(
                f"{where}: skill_id {sid!r} has no `[[skill]]` record. Every "
                f"constraint is owned by a skill, and the owning record is what "
                f"keeps the constraint addressable when the source directory is "
                f"gone.")
        version = entry.get("constraint_version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise StoreError(f"{where}: `constraint_version` must be a positive "
                             f"integer")
        status = str(entry.get("status", ACTIVE))
        if status not in STATUSES:
            raise StoreError(f"{where}: `status` must be one of "
                             f"{', '.join(STATUSES)}, got {status!r}")
        store.constraints.append(ConstraintRecord(
            constraint_id=cid, skill_id=sid,
            canonical_text=_require_str(entry, "canonical_text", where),
            source_text=str(entry.get("source_text", "")),
            source_file=_require_str(entry, "source_file", where),
            source_span=_pair(entry, "source_span", where, required=True),
            source_lines=_pair(entry, "source_lines", where, required=False),
            source_hash=_require_str(entry, "source_hash", where),
            constraint_version=version, status=status,
            created_at=str(entry.get("created_at", "")),
            updated_at=str(entry.get("updated_at", "")),
            extraction_reason=str(entry.get("extraction_reason", "")),
            tier=str(entry.get("tier", "")),
            candidate_id=str(entry.get("candidate_id", "")),
            note=str(entry.get("note", ""))))
    return store


# ---------------------------------------------------------------------------
# Render / write
# ---------------------------------------------------------------------------

_HEADER = """# ConstraintRelay — canonical Constraint Store (Phase 1, skill-keyed).
#
# `canonical_text` is the constraint. The `source_*` fields record where it came
# from and are not a second authority: a change there is reported by
# `constraintrelay diff` and only `constraintrelay update` moves this file.
#
# Generated and rewritten by ConstraintRelay. Hand edits survive a rewrite only
# if they parse; every write is round-tripped before it lands.
"""


def render(store: SkillStore) -> str:
    lines = [_HEADER, f"version = {SUPPORTED_VERSION}", ""]
    for s in sorted(store.skills, key=lambda s: s.skill_id):
        lines.append("[[skill]]")
        lines.append(f"skill_id = {toml_string(s.skill_id)}")
        lines.append(f"name = {toml_string(s.name)}")
        lines.append(f"prefix = {toml_string(s.prefix)}")
        if s.rel_dir:
            lines.append(f"rel_dir = {toml_string(s.rel_dir)}")
        lines.append(f"first_imported = {toml_string(s.first_imported)}")
        lines.append(f"last_seen = {toml_string(s.last_seen)}")
        lines.append(f"status = {toml_string(s.status)}")
        lines.append(f"hold_scope = {toml_string(s.hold_scope)}")
        lines.append("")
    for c in sorted(store.constraints, key=lambda c: (c.skill_id, c.number)):
        lines.append("[[constraint]]")
        lines.append(f"constraint_id = {toml_string(c.constraint_id)}")
        lines.append(f"skill_id = {toml_string(c.skill_id)}")
        lines.append(f"canonical_text = {toml_string(c.canonical_text)}")
        lines.append(f"source_text = {toml_string(c.source_text)}")
        lines.append(f"source_file = {toml_string(c.source_file)}")
        lines.append(f"source_span = {_ints(c.source_span)}")
        lines.append(f"source_lines = {_ints(c.source_lines)}")
        lines.append(f"source_hash = {toml_string(c.source_hash)}")
        lines.append(f"constraint_version = {c.constraint_version}")
        lines.append(f"status = {toml_string(c.status)}")
        lines.append(f"created_at = {toml_string(c.created_at)}")
        lines.append(f"updated_at = {toml_string(c.updated_at)}")
        if c.tier:
            lines.append(f"tier = {toml_string(c.tier)}")
        if c.extraction_reason:
            lines.append(f"extraction_reason = {toml_string(c.extraction_reason)}")
        if c.candidate_id:
            lines.append(f"candidate_id = {toml_string(c.candidate_id)}")
        if c.note:
            lines.append(f"note = {toml_string(c.note)}")
        lines.append("")
    return "\n".join(lines)


def write(store: SkillStore, path: Path | None = None) -> Path:
    """Serialise, parse the result back, and only then touch the disk.

    The round-trip is the whole point: a store that cannot be read is worse than
    no store, because the next run would treat an unreadable authority as an
    absent one. A rendering defect therefore raises here, with the old file
    intact.
    """
    out = path or store.path
    text = render(store)
    try:
        reparsed = parse(tomllib.loads(text), out)
    except (StoreError, tomllib.TOMLDecodeError) as exc:
        raise StoreError(f"refusing to write {out}: the rendered store does not "
                         f"parse back: {exc}") from exc
    if len(reparsed.constraints) != len(store.constraints) or \
            any(a.canonical_text != b.canonical_text
                for a, b in zip(sorted(reparsed.constraints,
                                       key=lambda c: c.constraint_id),
                                sorted(store.constraints,
                                       key=lambda c: c.constraint_id))):
        raise StoreError(f"refusing to write {out}: the rendered store does not "
                         f"round-trip to the same canonical text")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out
