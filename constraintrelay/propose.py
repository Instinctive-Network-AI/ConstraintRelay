"""S4 — day-0 value: show what is there, and make marking it one reviewed act.

The cliff this removes
----------------------
A developer who installs ConstraintFlow, points `container build` at a real
repository and follows the README gets this:

    "entries": 0,
    "candidates_skipped_not_author_marked": 2

Zero declarations, exit code 0, and the only signal buried in a JSON field.
The library is empty because the container keeps **author-marked** candidates
only — which is correct and stays: an unmarked line is the extractor's guess,
and delivering a guess as the author's declaration is the one thing this
project may never do.

So the gap is not the rule. The gap is that nothing helped the developer
*become* the author.

What this does
--------------
`propose` runs **the same `extract` the build runs**, so what it offers is
exactly what a build would accept once marked — never a different set, which
would be the worst possible outcome: mark everything it suggests and still get
an empty library.

Then it either prints the edit it would make, or makes it under `--write`.
The marker it inserts is the one the author would have written by hand. The
developer's `git diff` is the review, and their commit is the act of
authorship — which is what `author_marker` has always meant.

What it refuses to do
---------------------
Promote anything on its own. Without `--write` nothing on disk changes. With
`--write` the files change and **that is all**: no library is built, nothing
is confirmed, no manifest is adopted. Those remain separate acts.

Recall, stated where the developer reads it
-------------------------------------------
Extraction is heuristic. On the development gold set the random stratum
measured **precision ≈ 80%, recall ≈ 31%** — about two real constraints in
three are never proposed at all, and thresholds written as table rows carrying
no modal verb are found roughly **one time in five**. Those are development-set
figures, read while choosing fixes, so an unseen corpus is no better.

A proposal list is therefore a starting point and **absence from it is not
evidence that a rule does not exist**. `report()` puts that sentence in the
command's own output rather than in a document the developer has not opened.
"""

from __future__ import annotations

import re
from pathlib import Path

from .blocks import MARKER_LINE
from .candidates import AUTHOR_MARKER, extract
from .inventory import discover_skills
from .markers import scan_project

#: The only marker kind that opens a constraint. `end` closes a range, and
#: nothing else is a marker at all — a typo'd keyword is read as prose, which
#: is why this is taken from the parser's own vocabulary rather than typed.
MARKER_KIND = "critical"

#: What the measured limits are, in the words the command prints. Kept here so
#: the CLI, the JSON output and any document quote one source.
RECALL_NOTE = (
    "Extraction is heuristic. On the development gold set's random stratum: "
    "precision ~80%, recall ~31% — about two real constraints in three are "
    "NEVER proposed, and thresholds written as table rows with no modal verb "
    "are found roughly one time in five. Those are development-set figures. "
    "ABSENCE FROM THIS LIST IS NOT EVIDENCE THAT A RULE DOES NOT EXIST.")


class ProposeError(Exception):
    """The proposal cannot be made as asked."""


def _slug(skill_id: str) -> str:
    base = skill_id.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").upper() or "SKILL"


def _project_markers(source_root: Path, skills_dir: str) -> list:
    """Every marker in the project, read from the markers themselves.

    Not from the extractor's candidate list. A marker placed on a block the
    extractor does not rate — a heading, a sentence it has no pattern for —
    produces no candidate at all, so deriving "already marked" from
    candidates reported ZERO on a project that visibly had one. The markers
    are the artifact that cannot be wrong about what is marked.
    """
    inv = discover_skills(Path(source_root), skills_dir=skills_dir)
    files = [(f.rel_path, f.text) for s in inv.skills for f in s.files]
    return list(scan_project(files).constraints)


def _existing_ids(source_root: Path, skills_dir: str) -> set:
    """Every declared id already marked anywhere in the project.

    Project-wide, not per file: an id is the identity of a constraint across
    the whole repository, so a proposal that reused one would silently merge
    two different rules under one identity.
    """
    # `c.id` is read directly, with no `getattr` fallback. The first draft
    # used `getattr(c, "constraint_id", "")` — a field that does not exist —
    # so this returned an EMPTY set on a project with markers in it, and the
    # collision guard below was dead code that could never fire. A fallback
    # that yields nothing turns a wrong name into a silent no-op; raising is
    # what makes the next rename visible.
    return {c.id for c in _project_markers(source_root, skills_dir) if c.id}


def _already_marked_above(lines: list, line_no: int) -> bool:
    """Is the line immediately above *line_no* (1-based) already a marker?

    A second guard beside the tier partition. The extractor tiers a marked
    block as `author_marker` and this function should therefore never fire —
    which is exactly why it is here: if the two ever disagree, the cost of
    being wrong is a duplicate marker written into someone's file.
    """
    idx = line_no - 2                       # the line above, 0-based
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    return idx >= 0 and bool(MARKER_LINE.match(lines[idx].rstrip("\r\n")))


def plan(source_root, skill_ids: list, *, skills_dir: str = "skills") -> dict:
    """What `--write` would insert, without touching anything.

    Returns the proposals, what is already marked, and the per-file edits in
    the order they must be applied.
    """
    source_root = Path(source_root).resolve()
    inv = discover_skills(source_root, skills_dir=skills_dir)
    known = {s.skill_id: s for s in inv.skills}
    missing = [s for s in skill_ids if s not in known]
    if missing:
        raise ProposeError(
            f"no such skill(s) in {source_root}: {missing}\n"
            f"available: {sorted(known)}")

    # The SAME extraction the build runs. Offering a different set would be
    # the worst outcome available here: mark everything suggested, build, and
    # still get an empty library.
    ex = extract(inv, skill_ids=skill_ids)

    # What is already marked comes from the MARKERS, not from the candidate
    # list, and is restricted to the files of the skills being asked about.
    sel_files = {f.rel_path for sid in skill_ids for f in known[sid].files}
    already = [{"declared_id": m.id,
                "source_file": m.file,
                "marker_line": m.marker_line,
                "line": m.line,
                "text": m.canonical_text.strip()[:160]}
               for m in _project_markers(source_root, skills_dir)
               if m.file in sel_files]
    taken = _existing_ids(source_root, skills_dir)
    counters: dict = {}
    proposals: list = []
    texts: dict = {}

    for c in ex.candidates:
        if c.tier == AUTHOR_MARKER:
            continue
        rel = c.source_file
        key = (c.skill_id, rel)
        if key not in texts:
            texts[key] = (source_root / rel).read_text(
                encoding="utf-8").splitlines(keepends=True)
        lines = texts[key]
        line_no = c.source_lines[0]
        if line_no < 1 or line_no > len(lines):
            continue
        if _already_marked_above(lines, line_no):
            continue
        slug = _slug(c.skill_id)
        n = counters.get(slug, 0) + 1
        cid = f"{slug}-C{n:03d}"
        while cid in taken:
            n += 1
            cid = f"{slug}-C{n:03d}"
        counters[slug] = n
        taken.add(cid)
        indent = re.match(r"[ \t]*", lines[line_no - 1]).group(0)
        proposals.append({
            "constraint_id": cid,
            "skill_id": c.skill_id,
            "source_file": rel,
            "line": line_no,
            "tier": c.tier,
            "extraction_reason": c.extraction_reason,
            "text": c.source_text.strip(),
            "marker": f"{indent}<!-- constraintrelay: {MARKER_KIND} "
                      f"id={cid} -->",
        })

    return {
        "source_root": str(source_root),
        "skills": sorted(skill_ids),
        "already_marked": already,
        "proposals": proposals,
        "_what_this_is": (
            "candidates the deterministic extractor found and the build would "
            "KEEP once they carry a marker. Marking is the author's act; this "
            "writes the marker the author would have written, and the diff is "
            "what they review."),
        "_recall": RECALL_NOTE,
    }


def diff(result: dict) -> str:
    """The edit, as text a developer can read before allowing it."""
    out = []
    by_file: dict = {}
    for p in result["proposals"]:
        by_file.setdefault(p["source_file"], []).append(p)
    for rel in sorted(by_file):
        out.append(f"--- {rel}")
        for p in sorted(by_file[rel], key=lambda x: x["line"]):
            out.append(f"@@ line {p['line']} @@")
            out.append(f"+{p['marker']}")
            out.append(f" {p['text'].splitlines()[0][:100]}")
    return "\n".join(out)


def apply(result: dict) -> dict:
    """Insert the proposed markers. The only function here that writes.

    Edits are applied **bottom-up within each file**, because every insertion
    shifts the lines beneath it: top-down would place the second marker one
    line late, the third two lines late, and so on — each still looking
    plausible in the diff.
    """
    root = Path(result["source_root"])
    by_file: dict = {}
    for p in result["proposals"]:
        by_file.setdefault(p["source_file"], []).append(p)

    written = []
    for rel, items in sorted(by_file.items()):
        path = root / rel
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for p in sorted(items, key=lambda x: x["line"], reverse=True):
            i = p["line"] - 1
            if i < 0 or i > len(lines):
                raise ProposeError(
                    f"{rel}: proposed line {p['line']} is outside the file")
            lines.insert(i, p["marker"] + "\n")
        path.write_text("".join(lines), encoding="utf-8")
        written.append({"file": rel, "markers_inserted": len(items)})

    return {
        "files_written": written,
        "markers_inserted": sum(w["markers_inserted"] for w in written),
        "_next": (
            "Review with `git diff` — that review is the act of authorship, "
            "and the marker means nothing more than that you accepted it. "
            "Then `container build`: the files have changed, so any library "
            "built BEFORE this will now fail `container check` against its "
            "own bindings, which is the check working."),
    }
