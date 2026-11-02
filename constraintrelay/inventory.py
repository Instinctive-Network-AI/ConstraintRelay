"""Phase 1 layer A — source discovery, organised around the **skill**.

The Phase-1 brief moves the primary organising unit from the harness file to the
skill: *"Do not organize the primary product around harness files such as
`AGENTS.md`."* So this module answers one question — *what skills are in this
repository, and which files carry their text?* — and answers it without
consulting any harness profile.

Why not reuse `discover.collect_skills`
---------------------------------------
`discover.collect_skills` returns one `InputFile` per `SKILL.md`, named by its
parent directory. That is the right load-set primitive and it stays, but it
cannot be the Phase-1 unit: a skill is a *directory* that may carry support
files beside `SKILL.md` (15 of the 88 Vibe-Trading skills ship an
`example_signal_engine.py`, several ship Chinese reference pages), and a
constraint can live in any of them. The inventory therefore keeps the directory,
not just the one file.

skill_id
--------
`skill_id` is the **repository-relative POSIX path of the skill directory**:

    agent/src/skills/tushare

Three candidate schemes were considered and this is the conservative one:

* the directory basename (`tushare`) is short and is what the demo in the brief
  shows, but it is not unique in general — two roots can both hold `research/` —
  and a collision would silently merge two skills' constraints;
* the frontmatter `name` is what the harness uses to address the skill, but the
  author can edit it, and an id that changes when a line of YAML changes is not
  a stable key for canonical records;
* the directory path is unique by construction, independent of every other
  skill in the repository (so adding a 89th skill can never renumber the first
  88), and reads as its own provenance.

The cost is a long id, and it is paid back in `resolve_skill`: the CLI accepts
the full id, the frontmatter name, or the directory basename whenever that is
unambiguous, so nobody has to type the path.

A directory rename *does* change the id. That is a known Phase-1 limitation
rather than a solved problem: the alternative — matching renamed skills by
content similarity — is the kind of inferred identity the project has refused
everywhere else.

The project pseudo-skill
-----------------------
Project-level instruction files (`AGENTS.md` and friends, via
`discover.CONTEXT_FILENAMES`) *may* be imported, and the brief says so, but they
are not the Phase-1 object. They are off by default and arrive under the single
reserved id `_project` when `include_project=True`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .discover import SKIP_DIRS, collect_context_files, read_source

SKILL_FILE = "SKILL.md"

#: Reserved id for root-level instruction files. Not a directory, so it can
#: never collide with a real skill_id.
PROJECT_SKILL_ID = "_project"

#: Support files that can carry constraint text. Deliberately a small,
#: prose-only set: the point of Phase 1 is an inventory of authored
#: instructions, and a 400 KB CSV fixture is not a constraint source.
#:
#: `.py` is **not** in it, and that is a decision rather than an oversight. The
#: Vibe-Trading corpus ships 15 `example_signal_engine.py` files and a handful
#: of `scripts/*.py`; their docstrings do occasionally carry a scope
#: restriction, but every one is a restatement of a sentence already in the
#: `SKILL.md`, and the surrounding code fires every obligation pattern in
#: `candidates.py` (`assert`, `raise ValueError("must ...")`). Scanning them
#: traded real precision for duplicate recall. Each one is recorded as a
#: skipped source instead, so the omission is counted in the report rather than
#: implied — and lifting it is a one-line change to this tuple.
SUPPORT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")

#: Hard ceiling on a single support file. A file past this is recorded as a
#: skipped source in the report rather than scanned, so "we did not look at it"
#: is visible instead of implied.
MAX_SUPPORT_BYTES = 512 * 1024

#: How far below a skill directory support files are collected, and how many.
#:
#: Both bounds exist because of one real case. The Vibe-Trading repository has a
#: `SKILL.md` at `agent/` — `name: vibe-trading`, the toolkit's own skill
#: document — whose directory is the entire agent source tree. An unbounded
#: `rglob` attributed 2,780 of 3,295 candidates to that single skill, almost all
#: of them from Python modules and test fixtures several levels down. A skill's
#: own `references/` and `scripts/` directories are two levels deep at most in
#: this corpus, so the depth bound keeps them and drops the source tree.
MAX_SUPPORT_DEPTH = 3
MAX_SUPPORT_FILES = 64

#: A directory holding one of these is somebody else's unit — a package or a
#: project — and a skill does not own it even when it sits underneath.
PACKAGE_MARKERS = ("__init__.py", "pyproject.toml", "setup.py", "setup.cfg",
                   "package.json", "go.mod", "Cargo.toml")

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n")


@dataclass(frozen=True)
class SourceFile:
    """One file belonging to a skill, with its text exactly as on disk."""

    #: Repository-relative POSIX path. This is what goes into a constraint's
    #: `source_file`, so it has to be stable and checkable by a person.
    rel_path: str
    abs_path: str
    text: str
    role: str                 # skill_doc | support | project_context

    @property
    def frontmatter_end(self) -> int:
        """Offset just past the YAML frontmatter, or 0 when there is none.

        Used to keep the frontmatter out of candidate extraction: `name:` and
        `description:` are harness metadata, and a description reading
        *"... backtest only ..."* is a summary of a constraint, not the
        constraint.
        """
        if self.role == "support" or not _FRONTMATTER.match(self.text):
            return 0
        end = self.text.find("\n---", 3)
        if end == -1:
            return 0
        nl = self.text.find("\n", end + 1)
        return len(self.text) if nl == -1 else nl + 1


@dataclass(frozen=True)
class Skill:
    """One discovered skill."""

    skill_id: str
    name: str                 # frontmatter `name`, else the directory basename
    rel_dir: str              # "" for the project pseudo-skill
    abs_dir: str
    files: tuple[SourceFile, ...]
    category: str = ""
    description: str = ""
    skipped: tuple[tuple[str, str], ...] = ()   # (rel_path, reason)

    @property
    def basename(self) -> str:
        return self.rel_dir.rsplit("/", 1)[-1] if self.rel_dir else self.skill_id

    @property
    def collection(self) -> str:
        """The directory the skill sits in — `agent/src/skills` for 88 of the
        Vibe-Trading skills. Not an identifier; a grouping for reports."""
        return self.rel_dir.rsplit("/", 1)[0] if "/" in self.rel_dir else ""

    @property
    def doc(self) -> SourceFile | None:
        for f in self.files:
            if f.role == "skill_doc":
                return f
        return None

    def file(self, rel_path: str) -> SourceFile | None:
        for f in self.files:
            if f.rel_path == rel_path:
                return f
        return None

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "name": self.name,
                "rel_dir": self.rel_dir, "category": self.category,
                "description": self.description,
                "files": [{"path": f.rel_path, "role": f.role,
                           "chars": len(f.text)} for f in self.files],
                "skipped": [{"path": p, "reason": r} for p, r in self.skipped]}


@dataclass
class Inventory:
    """Everything discovery found, plus what it could not read."""

    root: Path
    skills: list[Skill] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def by_id(self) -> dict[str, Skill]:
        return {s.skill_id: s for s in self.skills}

    def get(self, skill_id: str) -> Skill | None:
        return self.by_id.get(skill_id)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """The scalar keys of a YAML frontmatter block.

    Hand-rolled on purpose: PyYAML is not a dependency of this project and the
    only keys Phase 1 reads are `name`, `description` and `category`, all
    scalars. Anything structured is ignored rather than half-parsed.
    """
    if not _FRONTMATTER.match(text):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[text.find("\n") + 1:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t", "-"):
            continue                      # nested or list value; not a scalar key
        key, sep, value = line.partition(":")
        if not sep:
            continue
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[key.strip()] = v
    return out


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_package_dir(path: Path) -> bool:
    return any((path / marker).is_file() for marker in PACKAGE_MARKERS)


def _support_files(root: Path, skill_dir: Path
                   ) -> tuple[list[SourceFile], list[tuple[str, str]]]:
    """Prose files belonging to *skill_dir*, bounded and with the bounds reported.

    An explicit walk rather than `rglob`, because the pruning rules are about
    *directories* — a directory with its own `SKILL.md` belongs to that skill,
    and a directory with an `__init__.py` belongs to a Python package — and
    `rglob` has already descended by the time a filter can see the files.
    """
    files: list[SourceFile] = []
    skipped: list[tuple[str, str]] = []
    queue: list[tuple[Path, int]] = [(skill_dir, 0)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            skipped.append((_rel(root, directory),
                            f"unreadable directory: {exc.__class__.__name__}"))
            continue
        for path in entries:
            rel = _rel(root, path)
            if path.is_dir():
                if path.name in SKIP_DIRS or path.name.startswith("."):
                    continue
                if (path / SKILL_FILE).is_file():
                    continue          # another skill's territory
                if _is_package_dir(path):
                    skipped.append((rel, "package directory, not a skill resource"))
                    continue
                if depth + 1 > MAX_SUPPORT_DEPTH:
                    skipped.append((rel, f"deeper than {MAX_SUPPORT_DEPTH} levels "
                                         f"below the skill directory"))
                    continue
                queue.append((path, depth + 1))
                continue
            if not path.is_file() or path.name == SKILL_FILE:
                continue
            if path.suffix.lower() not in SUPPORT_SUFFIXES:
                skipped.append((rel, f"suffix {path.suffix or '(none)'} not scanned "
                                     f"in Phase 1"))
                continue
            if len(files) >= MAX_SUPPORT_FILES:
                skipped.append((rel, f"beyond the {MAX_SUPPORT_FILES}-file support "
                                     f"budget for one skill"))
                continue
            try:
                if path.stat().st_size > MAX_SUPPORT_BYTES:
                    skipped.append((rel, f"larger than {MAX_SUPPORT_BYTES} bytes"))
                    continue
                files.append(SourceFile(rel, str(path), read_source(path), "support"))
            except (OSError, UnicodeDecodeError) as exc:
                skipped.append((rel, f"unreadable: {exc.__class__.__name__}"))
    files.sort(key=lambda f: f.rel_path)
    skipped.sort()
    return files, skipped


def discover_skills(root: Path, *, include_project: bool = False,
                    skills_dir: str = "") -> Inventory:
    """Every skill beneath *root*.

    A skill is a directory containing `SKILL.md`. Nested skills are each
    returned on their own terms: the inner skill's files are never also
    attributed to the outer one, so no constraint is inventoried twice.

    *skills_dir* scopes discovery to one repository-relative subtree. Left
    empty, the whole repository is enumerated — which is the honest default, and
    it does find skill documents a harness's own loader would not read. The
    Vibe-Trading corpus is the example: `agent/src/skills/` holds the 88 skills
    the loader enumerates, and `agent/SKILL.md` is a 89th skill document for the
    toolkit itself. Both are real `SKILL.md` files, so both are reported, and
    `Skill.collection` is what lets a reader see the 88 as a corpus.
    """
    inv = Inventory(root=root)
    scope = root
    if skills_dir:
        scope = (root / skills_dir).resolve()
        # A `--skills-dir ../elsewhere` would discover skills whose paths cannot
        # be made relative to the root, and therefore whose skill_ids would not
        # be the thing this module says they are.
        if scope != root and root not in scope.parents:
            raise ValueError(f"--skills-dir {skills_dir!r} resolves to {scope}, "
                             f"which is outside the repository root {root}")
    docs: list[Path] = []
    for path in sorted(scope.rglob(SKILL_FILE)):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        docs.append(path)

    for doc_path in docs:
        skill_dir = doc_path.parent
        try:
            text = read_source(doc_path)
        except (OSError, UnicodeDecodeError) as exc:
            inv.unreadable.append((_rel(root, doc_path),
                                   f"{exc.__class__.__name__}: {exc}"))
            continue
        meta = _parse_frontmatter(text)
        rel_dir = _rel(root, skill_dir)
        files = [SourceFile(_rel(root, doc_path), str(doc_path), text, "skill_doc")]
        support, skipped = _support_files(root, skill_dir)
        files += support
        inv.skills.append(Skill(
            skill_id=rel_dir or doc_path.parent.name,
            name=meta.get("name") or skill_dir.name,
            rel_dir=rel_dir,
            abs_dir=str(skill_dir),
            files=tuple(files),
            category=meta.get("category", ""),
            description=meta.get("description", ""),
            skipped=tuple(skipped),
        ))

    if include_project:
        context = collect_context_files(root)
        if context:
            inv.skills.append(Skill(
                skill_id=PROJECT_SKILL_ID,
                name=PROJECT_SKILL_ID,
                rel_dir="",
                abs_dir=str(root),
                files=tuple(SourceFile(f.name, f.path, f.file_text,
                                       "project_context") for f in context),
                description="root-level project instruction files",
            ))
    return inv


def resolve_skill(inv: Inventory, token: str) -> tuple[Skill | None, list[Skill]]:
    """Resolve a user-supplied skill reference.

    Returns `(skill, candidates)`. `skill` is set only on an unambiguous match;
    otherwise `candidates` holds everything the token could have meant, so the
    caller can print them instead of guessing. Matched in order: exact
    `skill_id`, exact frontmatter `name`, exact directory basename, then a
    unique path suffix.
    """
    exact = inv.get(token)
    if exact is not None:
        return exact, [exact]
    for attr in ("name", "basename"):
        hits = [s for s in inv.skills if getattr(s, attr) == token]
        if len(hits) == 1:
            return hits[0], hits
        if len(hits) > 1:
            return None, hits
    suffix = [s for s in inv.skills
              if s.skill_id.endswith("/" + token.strip("/"))]
    if len(suffix) == 1:
        return suffix[0], suffix
    return None, suffix
