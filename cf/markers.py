"""Critical-constraint identity — Brief v0.6 §3.

v1 has **no central constraint registry**. Critical constraints stay in the
author's own instruction files and are identified by an explicit marker:

    <!-- constraintflow: critical id=FIN-003 -->

Identity is the `id`, never the text. §3's consequences, which this module
implements literally:

* same id, different canonical text  -> the same constraint was **modified**
* id gone                            -> the constraint was **removed**
* new id                             -> a constraint was **added**

and therefore ConstraintFlow never infers identity from text similarity. A
legacy marker carrying no id can still be verified in one revision, but it is
`UNTRACKED` and takes no part in cross-revision Impact.

Forms
-----
*Single block.* With no end marker the start applies to the **next syntactic
block** under the frozen block semantics in `blocks.py`.

*Range.* `<!-- constraintflow: end id=FIN-003 -->` closes an explicit range
covering every block in between.

Distinguishing the two
----------------------
§3 lists both "without an explicit end marker the marker applies to the next
block" and "an unclosed explicit range is a configuration error". Those collide
unless the author's intent can be read off the file, and §3 defines no separate
range-opening syntax. A start is therefore read as opening a range when any of
these holds:

* a matching `end id=<same>` appears later in the same file;
* the next marker in the file is an `end`, whatever its id — so a mismatched
  pair is reported as `ID_MISMATCH` against §3's "start and end IDs must match"
  rather than silently becoming a single-block constraint plus a stray end;
* an `end` for that id exists elsewhere in the project — which is how an
  unclosed explicit range stays distinguishable from the single-block form. The
  stray `end` is also an unmatched-end error at its own site, so one mistake is
  reported from both ends.

Everything else is the single-block form, so a marker is only read that way when
nothing in the project suggests a range was intended. Recorded as a derived
decision under §20.1 (choose the conservative option), not as a brief statement.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .blocks import MARKER_LINE as _MARKER, Block, segment

_ID_ATTR = re.compile(r"\bid[ \t]*=[ \t]*(?:\"(?P<q>[^\"]*)\"|'(?P<s>[^']*)'|(?P<b>[^\s\"']+))")

#: Marker preferred by §3. Anything else is still a marker line but is reported,
#: because a typo'd marker that is silently treated as prose is the one failure
#: this layer must never have.
UNTRACKED = "UNTRACKED"


@dataclass(frozen=True)
class MarkerIssue:
    """A configuration error. §9 maps these to exit 1 — tool/config error —
    never to exit 2, because a malformed project is not a delivery finding."""

    code: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.code}: {self.message}"


@dataclass(frozen=True)
class CriticalConstraint:
    """One author-identified critical constraint, resolved to block extent."""

    id: str                     # "" for a legacy marker with no id
    file: str                   # path as discovered, used as the report locator
    marker_line: int
    form: str                   # single_block | range | legacy
    blocks: tuple[int, ...]     # block indices in `blocks.segment()` order
    line: int                   # first covered line
    end_line: int               # last covered line
    start: int                  # char offset, inclusive
    end: int                    # char offset, exclusive
    canonical_text: str

    @property
    def tracked(self) -> bool:
        """Only an id carries identity across revisions (§3)."""
        return bool(self.id)

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest()

    @property
    def locator(self) -> str:
        span = f"{self.line}" if self.end_line <= self.line else f"{self.line}-{self.end_line}"
        return f"{self.file}:{span}"

    @property
    def label(self) -> str:
        return self.id or f"{UNTRACKED}@{self.locator}"


@dataclass
class MarkerScan:
    """Markers found across a project."""

    constraints: list[CriticalConstraint] = field(default_factory=list)
    issues: list[MarkerIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def tracked(self) -> dict[str, CriticalConstraint]:
        return {c.id: c for c in self.constraints if c.tracked}

    @property
    def untracked(self) -> list[CriticalConstraint]:
        return [c for c in self.constraints if not c.tracked]


@dataclass(frozen=True)
class _Marker:
    kind: str          # critical | end
    cid: str           # "" when absent
    line: int          # 1-indexed
    start: int         # char offset of the line
    end: int           # char offset just past the line's newline-stripped text


def _parse_markers(source: str) -> list[_Marker]:
    out: list[_Marker] = []
    offset = 0
    for i, raw in enumerate(source.splitlines(keepends=True), start=1):
        stripped = raw.rstrip("\r\n")
        m = _MARKER.match(stripped)
        if m:
            attr = _ID_ATTR.search(m.group("attrs") or "")
            cid = ""
            if attr:
                cid = attr.group("q") or attr.group("s") or attr.group("b") or ""
            out.append(_Marker(m.group("kind"), cid.strip(), i,
                               offset, offset + len(stripped)))
        offset += len(raw)
    return out


def content_blocks(source: str) -> list[Block]:
    """The blocks of *source*.

    A thin alias for `blocks.segment`, kept because this layer's callers are
    asking a different question ("what can a marker cover?") and because it
    documents the invariant they depend on: **the marker layer and the verdict
    layer must segment identically**, so a constraint's block indices mean the
    same thing in both. `segment` excludes marker lines itself.
    """
    return segment(source)


def canonical_text(blocks: list[Block], indices: "tuple[int, ...] | list[int]") -> str:
    """Canonical form of a constraint's covered blocks.

    Each block stripped, joined by one blank line. Stable against reflowed blank
    lines between blocks and trailing whitespace, and exact about everything
    else. This is **not** a similarity measure: it only decides whether the text
    under an unchanged id is byte-identical or not (§10.2 MODIFIED).
    """
    chosen = [b for b in blocks if b.index in set(indices)]
    return "\n\n".join(b.text.strip() for b in chosen if b.text.strip())


def scan_file(path: str, source: str, *, project_end_ids: "set[str] | None" = None
              ) -> tuple[list[CriticalConstraint], list[MarkerIssue]]:
    """Resolve the markers in one file to constraints.

    `project_end_ids` is every id that carries an `end` marker anywhere in the
    project; it is what makes an unclosed explicit range distinguishable from
    the single-block form.
    """
    markers = _parse_markers(source)
    if not markers:
        return [], []

    blocks = content_blocks(source)
    constraints: list[CriticalConstraint] = []
    issues: list[MarkerIssue] = []
    elsewhere = project_end_ids or set()

    open_marker: _Marker | None = None

    def build(start: _Marker, form: str, lo: int, hi: int) -> None:
        """Constraint covering blocks with `lo <= block.start < hi`."""
        covered = [b for b in blocks if lo <= b.start < hi and b.text.strip()]
        if not covered:
            issues.append(MarkerIssue(
                "NO_COVERED_BLOCK", path, start.line,
                f"marker {start.cid or UNTRACKED} covers no block"))
            return
        constraints.append(CriticalConstraint(
            id=start.cid,
            file=path,
            marker_line=start.line,
            form=form,
            blocks=tuple(b.index for b in covered),
            line=covered[0].line,
            end_line=covered[-1].end_line,
            start=covered[0].start,
            end=covered[-1].end,
            canonical_text=canonical_text(blocks, [b.index for b in covered]),
        ))

    def opens_range(i: int) -> bool:
        m = markers[i]
        later = markers[i + 1:]
        if m.cid and any(x.kind == "end" and x.cid == m.cid for x in later):
            return True                      # matching end, same file
        if later and later[0].kind == "end":
            return True                      # next marker closes something
        return bool(m.cid) and m.cid in elsewhere   # end marker in another file

    for idx, m in enumerate(markers):
        if m.kind == "critical":
            if open_marker is not None:
                # §3: nested critical ranges are invalid in v1. Reported against
                # the inner marker, and the outer range is abandoned rather than
                # guessed at.
                issues.append(MarkerIssue(
                    "NESTED_RANGE", path, m.line,
                    f"critical id={m.cid or UNTRACKED} opens inside the still-open "
                    f"range id={open_marker.cid or UNTRACKED} (line "
                    f"{open_marker.line}); nested critical ranges are invalid in v1"))
                open_marker = None
                continue
            if opens_range(idx):
                open_marker = m            # expect a matching end in this file
                continue
            # Single-block form, or a legacy marker with no id.
            nxt = next((b.start for b in blocks if b.start > m.end and b.text.strip()), None)
            if nxt is None:
                issues.append(MarkerIssue(
                    "NO_COVERED_BLOCK", path, m.line,
                    f"marker {m.cid or UNTRACKED} has no following block"))
                continue
            form = "single_block" if m.cid else "legacy"
            build(m, form, nxt, nxt + 1)   # exactly the one block starting there
        else:  # end
            if open_marker is None:
                issues.append(MarkerIssue(
                    "UNMATCHED_END", path, m.line,
                    f"end id={m.cid or UNTRACKED} closes nothing"))
                continue
            if m.cid != open_marker.cid:
                issues.append(MarkerIssue(
                    "ID_MISMATCH", path, m.line,
                    f"end id={m.cid or UNTRACKED} does not match the open "
                    f"critical id={open_marker.cid or UNTRACKED} (line "
                    f"{open_marker.line}); start and end ids must match"))
                open_marker = None
                continue
            build(open_marker, "range", open_marker.end, m.start)
            open_marker = None

    if open_marker is not None:
        issues.append(MarkerIssue(
            "UNCLOSED_RANGE", path, open_marker.line,
            f"critical id={open_marker.cid or UNTRACKED} opens an explicit range "
            f"that is never closed in this file; an unclosed explicit range is a "
            f"configuration error"))

    return constraints, issues


def scan_project(files: "list[tuple[str, str]]") -> MarkerScan:
    """Resolve markers across the project. *files* is `[(path, source), ...]`.

    Two passes, because both the unclosed-range test and the duplicate-id test
    are project-wide facts that no single file can answer.
    """
    end_ids: set[str] = set()
    for _, source in files:
        for m in _parse_markers(source):
            if m.kind == "end" and m.cid:
                end_ids.add(m.cid)

    scan = MarkerScan()
    for path, source in files:
        found, issues = scan_file(path, source, project_end_ids=end_ids)
        scan.constraints += found
        scan.issues += issues

    # §3: duplicate ids in the same project are a configuration error. Reported
    # against every site but the first, so the output names what to change.
    first: dict[str, CriticalConstraint] = {}
    for c in scan.constraints:
        if not c.tracked:
            continue
        if c.id in first:
            scan.issues.append(MarkerIssue(
                "DUPLICATE_ID", c.file, c.marker_line,
                f"id={c.id} is already declared at {first[c.id].file}:"
                f"{first[c.id].marker_line}; duplicate ids in one project are a "
                f"configuration error"))
        else:
            first[c.id] = c
    return scan
