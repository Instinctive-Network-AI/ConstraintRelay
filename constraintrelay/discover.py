"""Load-set discovery.

Split out of `cli.py` at v0.6 so that `scenario`, `verify`, `impact` and
`budget` can all enumerate a load set without importing the CLI. The names are
re-exported from `constraintrelay.cli`, so existing callers keep working.

§13 governs file identity here: filesystem identity is **evidence**, not
universal loader semantics.
"""

from __future__ import annotations

from pathlib import Path

from .profiles import InputFile

CONTEXT_FILENAMES = [
    "AGENTS.md", "agents.md", ".hermes.md", "HERMES.md", "CLAUDE.md",
    "claude.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md", ".cursorrules",
]

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build"}


def read_source(path: Path) -> str:
    """The file's text **exactly as it is on disk** — CF-020.

    `Path.read_text()` opens in universal-newline mode, which silently rewrites
    `\\r\\n` to `\\n`. Block offsets are reported in **file** coordinates so a verdict
    can be checked against the file a person edits, and that requires the bytes the
    file actually contains: on a CRLF-authored instruction file, universal-newline
    translation moves every offset after the first line, so a verdict citing
    `chars[40552,40674)` named the wrong range.

    Windows editors and PowerShell's `Set-Content` write CRLF by default (finding 17
    records the same stratum for the BOM), so this is a real population.

    **This is the raw source, not any loader's view of it** — run-path ruling item 6.
    Whether a harness *sees* CRLF is a property of that harness's own reader, and the
    two shipped adapters disagree: OpenClaw reads bytes through Node, hermes reads
    through Python's `read_text(encoding="utf-8")`, which translates. Applying either
    one here would impose it on the other, which is CF-022. The per-profile transform
    lives in `Registered.source_newlines` and is applied into `InputFile.loader_view`
    by `apply_loader_read`, leaving `file_text` raw for coordinates.
    """
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


#: Newline handling a harness's own reader applies before the loader sees the text.
#: Not cosmetic: it changes the character count a cap is compared against, so it moves
#: the truncation boundary on every CRLF-authored file.
NEWLINES_RAW = "raw"              # the loader sees the bytes (Node, OpenClaw)
NEWLINES_UNIVERSAL = "universal"  # \r\n and \r become \n (Python, hermes)


def apply_loader_read(text: str, newlines: str) -> str:
    """*text* as the harness's own reader would hand it to the loader.

    `universal` reproduces Python's universal-newline translation, which is what
    `Path.read_text` does and therefore what the real hermes loader receives. CF-022:
    the real hermes output for a CRLF file contains no `\\r\\n` at all, so a model
    predicting CRLF disagrees with it byte-for-byte — and, because the translation
    shortens the text by one character per line, disagrees about where a 20,000-char
    cap falls.
    """
    if newlines == NEWLINES_UNIVERSAL:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    if newlines == NEWLINES_RAW:
        return text
    raise KeyError(f"unknown newline policy {newlines!r}; expected "
                   f"{NEWLINES_RAW!r} or {NEWLINES_UNIVERSAL!r}")


def with_loader_view(files: "list[InputFile]", newlines: str) -> "list[InputFile]":
    """Attach each file's **loader view** for one profile's reader — ruling item 6.

    `file_text` is never touched: block offsets and reported coordinates stay in raw
    file coordinates, which is what makes a verdict checkable against the file a person
    edits. The transform goes into `loader_view`, so two profiles can disagree about
    newlines over the same discovered files without either rewriting the source.

    A no-op for a raw reader, and for a file whose text has no `\\r` at all — which is
    most of them, so the common path allocates nothing.
    """
    if newlines == NEWLINES_RAW:
        return files
    out: list[InputFile] = []
    for f in files:
        if "\r" not in f.file_text:
            out.append(f)
            continue
        out.append(InputFile(f.name, f.path, f.file_text,
                             loader_view=apply_loader_read(f.file_text, newlines),
                             missing=f.missing, unreadable=f.unreadable))
    return out


def view_offset_to_file(text: str, newlines: str) -> "list[int]":
    """Map each offset in the loader's view back to an offset in the raw file.

    Returned as a list of length `len(view) + 1`, so `m[i]` is the file offset of view
    offset `i` and `m[len(view)]` is the end of the file's mapped region.

    A span recovered from a loader that reads through universal-newline translation is
    in **view** coordinates, while a block is segmented over the raw file — so comparing
    them directly is off by the number of `\\r` characters dropped before that point.
    One CRLF per line over a 600-line `AGENTS.md` is a 600-character error, which is
    more than enough to move a verdict. Identity for a raw reader, so OpenClaw pays
    nothing for this.
    """
    if newlines == NEWLINES_RAW:
        return list(range(len(text) + 1))
    out: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        out.append(i)
        if text[i] == "\r":
            # `\r\n` and a bare `\r` both collapse to one `\n` in the view, so the view
            # advances by one while the file advances by two or one respectively.
            i += 2 if i + 1 < n and text[i + 1] == "\n" else 1
        else:
            i += 1
    out.append(n)
    return out


def collect_bootstrap_files(root: Path, names: "tuple[str, ...]",
                            absent_consumes_budget: "tuple[str, ...]"
                            ) -> list[InputFile]:
    """A **fixed-roster** load set, in the loader's own order — CF-016.

    `collect_context_files` is a union of every filename any surveyed harness might
    read, discovered in one shared order. For a loader whose roster is a literal
    list in its own source that union is wrong in three independent ways, and
    OpenClaw showed all three at once:

    * **it adds files the loader never opens.** `loadWorkspaceBootstrapFiles`
      (`src/agents/workspace.ts:1192`) iterates six names and no others, so
      `CLAUDE.md` is never read — yet a scan of a directory holding one reported its
      clauses `DELIVERED_ON_LOAD`. A false definite status, A4's release-blocking
      class.
    * **it omits files the loader does open.** `BOOTSTRAP.md` is in the roster and
      in no shared list, so a delivered constraint was invisible to every verdict
      and `_locate_mirror` would have called it out of the load set.
    * **it uses the wrong order.** The roster is consumed against one shared
      60,000-unit budget with a hard floor that skips every later file, so order
      decides delivery. `BOOTSTRAP.md` sits before `MEMORY.md`.

    `absent_consumes_budget` is the fourth way, and the subtlest. For four of the
    six names an absent file still occupies its slot: the loader pushes
    `{missing: true}`, and `buildBootstrapContextFiles` (`bootstrap.ts:403-414`)
    injects `[MISSING] Expected at: <path>` and charges its length to the shared
    budget. For `USER.md` and `MEMORY.md` an absent file is dropped before that,
    by an `exactWorkspaceEntryExists` check (`workspace.ts:1227-1231`). A model
    that treats all six alike mis-predicts the remaining budget in one direction
    or the other.

    Every clause above was established by running the real loader through
    `replays/openclaw/loadset.mjs`, not by reading the list — the roster is also
    exported as `WORKSPACE_BOOTSTRAP_FILENAMES`, and the observed order is checked
    against it in `tests/test_openclaw_loader_steps.py` step 1.
    """
    out: list[InputFile] = []
    for name in names:
        path = root / name
        if path.is_file():
            try:
                out.append(InputFile(name, str(path),
                                     read_source(path)))
                continue
            except (OSError, UnicodeDecodeError):
                # Unreadable is not absent. The real loader emits
                # `[UNREADABLE: ...]` for this case rather than a missing record,
                # and that text is not the authored text, so the slot is kept with
                # empty content and the file reports nothing delivered.
                out.append(InputFile(name, str(path), "", missing=False,
                                     unreadable=True))
                continue
        if name in absent_consumes_budget:
            out.append(InputFile(name, str(path), "", missing=True))
    return out


def _identity(path: Path) -> object:
    """A key that collapses two spellings of one file into one entry.

    §13: `(st_dev, st_ino)` is evidence of sameness, not a definition of the
    loader's semantics, and it is not always available. When `stat` fails the
    fallback is the path string, which can only ever *over*-count — it never
    merges two unrelated files, which is the direction §13 forbids.
    """
    try:
        st = path.stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        return str(path)


def collect_context_files(root: Path) -> list[InputFile]:
    """Context files at *root*, one entry per real file.

    CF-001: the candidate list carries both `AGENTS.md` and `agents.md`, and on
    a case-insensitive filesystem — macOS and Windows by default — those resolve
    to the *same* file. Enumerating it twice made the second copy lose the
    priority contest and be reported `NOT_DELIVERED`, a false definite status
    for content the loader had in fact delivered.

    Identity is `(st_dev, st_ino)`, not the resolved path: `Path.resolve()`
    preserves the spelling it was given, so on macOS it reports `AGENTS.md` and
    `agents.md` as two different paths for one inode. The first spelling in
    `CONTEXT_FILENAMES` order wins, which is also the order a harness checks.
    """
    context: list[InputFile] = []
    seen: set[object] = set()
    for name in CONTEXT_FILENAMES:
        path = root / name
        if not path.is_file():
            continue
        key = _identity(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            context.append(InputFile(name, str(path),
                                     read_source(path)))
        except (OSError, UnicodeDecodeError):
            continue

    # CF-004: hermes-agent's fourth tier concatenates `.cursorrules` with every
    # `.cursor/rules/*.mdc`, so the .mdc files are part of that load set and
    # have to be discovered or the predicted section is missing sections.
    rules_dir = root / ".cursor" / "rules"
    if rules_dir.is_dir():
        for mdc in sorted(rules_dir.glob("*.mdc")):
            key = _identity(mdc)
            if key in seen:
                continue
            seen.add(key)
            try:
                context.append(InputFile(f".cursor/rules/{mdc.name}", str(mdc),
                                         read_source(mdc)))
            except (OSError, UnicodeDecodeError):
                continue
    return context


def find_git_root(start: Path) -> Path | None:
    """Nearest ancestor containing `.git`, or None.

    Mirrors hermes-agent's `_find_git_root` (`agent/prompt_builder.py:63`), which
    tests `(parent / ".git").exists()` — so a `.git` *file*, as a worktree or
    submodule has, stops the walk just as a directory does.
    """
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def collect_ancestors(root: Path, names: "tuple[str, ...]") -> list[InputFile]:
    """Instruction files *above* `root` that a walking loader would reach.

    CF-011. hermes-agent's tier 1 is documented `.hermes.md / HERMES.md (walk to
    git root)` and implemented that way in `_find_hermes_md`
    (`agent/prompt_builder.py:79`): cwd first, then each parent, stopping after
    the git root. Our load set was the working directory only, so an ancestor
    `.hermes.md` was invisible — the profile then named `AGENTS.md` the winner and
    issued definite `DELIVERED` verdicts for a file the real loader never read.
    That is a false definite status, the class §11 calls the worst, and the same
    shape as CF-004.

    Returned nearest-first, which is the order the real walk returns them in, and
    named relative to *root* (`../.hermes.md`) so a report cannot be read as if
    the file were in the repository being scanned. The delivered section label is
    the bare filename, because `_load_hermes_md` falls back to
    `hermes_md_path.name` when `relative_to(cwd)` raises.
    """
    if not names:
        return []
    start = root.resolve()
    stop_at = find_git_root(start)
    if stop_at is not None and stop_at == start:
        # The real loop checks the working directory and *then* breaks, so when the
        # working directory is itself the git root no parent is ever examined.
        # `collect_context_files` already covers the working directory.
        return []
    out: list[InputFile] = []
    seen: set[object] = set()
    for directory in list(start.parents):
        for name in names:
            path = directory / name
            if not path.is_file():
                continue
            key = _identity(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = read_source(path)
            except (OSError, UnicodeDecodeError):
                continue
            try:
                rel = path.relative_to(start)
            except ValueError:
                rel = Path("..") / path.name
            out.append(InputFile(str(rel), str(path), text))
        # The real walk checks the git root and *then* stops, so an instruction
        # file sitting at the git root is in scope and its parents are not.
        if stop_at is not None and directory == stop_at:
            break
    return out


def collect_skills(root: Path) -> list[InputFile]:
    """Every `SKILL.md` beneath *root*."""
    skills: list[InputFile] = []
    seen: set[object] = set()
    for path in sorted(root.rglob("SKILL.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        key = _identity(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            text = read_source(path)
        except (OSError, UnicodeDecodeError):
            continue
        skills.append(InputFile(path.parent.name, str(path), text))
    return skills


def discover(root: Path) -> tuple[list[InputFile], list[InputFile]]:
    """Context files at the root, and every `SKILL.md` beneath it."""
    return collect_context_files(root), collect_skills(root)


def strip_frontmatter(text: str) -> str:
    """What a loader that strips YAML frontmatter would see."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4:].lstrip("\n") if end != -1 else text


def context_dirs(root: Path) -> list[Path]:
    """Directories under *root* whose location can change a context load set.

    §5's default scenario generator: the repository root, plus every directory
    that carries at least one recognised instruction file — those are the
    locations where a cwd-sensitive loader would see a different load set.
    Directories with no instruction file of their own are not scenarios: moving
    into one changes nothing a context loader reads.
    """
    out = [root]
    names = set(CONTEXT_FILENAMES)
    for path in sorted(root.rglob("*")):
        if not path.is_dir() or path == root:
            continue
        if any(part in SKIP_DIRS or part.startswith(".git") for part in path.parts):
            continue
        if (path / ".cursor" / "rules").is_dir() or any(
                (path / n).is_file() for n in names):
            out.append(path)
    return out
