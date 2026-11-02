"""Phase 12 layer A — a **lossless** structural parse of a skill document.

Owner ruling 2026-10-08, opening the semantic discovery path: *"Preserve
frontmatter, prose, lists, tables and fenced material as first-class source
structures. Do not discard structures before semantic inspection."*

Why a second parser
-------------------
`blocks.segment` is **frozen** and stays frozen: the marker and verdict layers
resolve verdicts by its block indices, so a second segmentation that disagreed
by one block would attribute a verdict to the wrong constraint. It is also
*lossy* on purpose — it drops blank runs and has no notion of tables or fences —
which is exactly what the deterministic extractor wants and exactly what a
semantic pass must not inherit. Every structure the deterministic path throws
away (`fenced_code: 4,235`, `table_row: 2,188`, `heading: 1,826`,
`emphasis_label: 265`, `frontmatter: 97`, `too_short: 938` corpus-wide) is a
structure the semantic path is required to look at.

So this module is additive. It does not import `blocks`, nothing in
`candidates.py` imports it, and the deterministic extractor is byte-for-byte
unchanged.

The losslessness contract
-------------------------
`parse(source)` returns units that **tile the source exactly**:

* `units[0].start == 0`, `units[-1].end == len(source)`;
* `units[i].end == units[i+1].start` for every adjacent pair;
* `"".join(u.text for u in units) == source`.

`verify_lossless` checks all three and is called by the field-test harness
before a single unit is shown to a model. The guarantee is the point: a semantic
candidate is required to carry verbatim evidence resolvable to an exact span,
and that is only checkable if the units are the source rather than a rendering
of it.

Blank runs become `blank` units rather than being skipped, because "skip the
bits that do not matter" is how a tiling stops being a tiling.

Kinds
-----
`frontmatter` `heading` `prose` `list` `table` `fence` `blank`

Tables and lists and fences additionally expose *views* — `rows`, `items`,
`info`, `body` — over their own text. Views, not separate units: nesting rows
inside the tiling would mean two levels of truth about where a byte lives, and
the one thing this module sells is that there is exactly one.

Fences win every tie. A `|` inside a fenced block is not a table row and a `-`
inside one does not open a list, which is the whole reason the deterministic
path's fence handling needed a dedicated pass in Phase 10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FRONTMATTER = "frontmatter"
HEADING = "heading"
PROSE = "prose"
LIST = "list"
TABLE = "table"
FENCE = "fence"
BLANK = "blank"
KINDS = (FRONTMATTER, HEADING, PROSE, LIST, TABLE, FENCE, BLANK)

_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([A-Za-z0-9_.+-]*)")
_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")
_TABLE_LINE = re.compile(r"^[ \t]{0,3}\|")
_TABLE_DELIM = re.compile(r"^[ \t]{0,3}\|?[ \t:|-]+\|[ \t:|-]*$")
_LIST_OPEN = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d+[.)])[ \t]+\S")
#: A continuation line of a list item: indented, and not itself a new structure.
_LIST_CONT = re.compile(r"^[ \t]+\S")
_FM_FENCE = re.compile(r"^---[ \t]*$")


@dataclass(frozen=True)
class Unit:
    """One structural unit. `text` is `source[start:end]`, verbatim."""

    kind: str
    start: int
    end: int
    text: str
    #: Fence info string, lowercased. `""` for a bare fence; only set on FENCE.
    info: str = ""
    #: Line number of the unit's first line, 1-based.
    line: int = 1
    #: `#`-path of enclosing headings, e.g. `"Risk > Limits"`.
    heading_path: str = ""
    #: Verbatim data rows, TABLE only. Header and `|---|` rule excluded.
    rows: tuple[str, ...] = ()
    #: Verbatim header row, TABLE only.
    header: str = ""
    #: Verbatim item lines, LIST only (an item's continuation lines are joined
    #: into that item).
    items: tuple[str, ...] = ()

    @property
    def body(self) -> str:
        """A FENCE's contents without its delimiter lines; else `text`.

        Fences are the case where the delimiters are punctuation rather than
        content, and a model shown ```` ```python ```` as part of the rule
        reads the backticks as part of the rule.
        """
        if self.kind != FENCE:
            return self.text
        lines = self.text.splitlines(keepends=True)
        if len(lines) < 2:
            return ""
        inner = lines[1:]
        if inner and _FENCE_OPEN.match(inner[-1].rstrip("\r\n")):
            inner = inner[:-1]
        return "".join(inner)

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "start": self.start, "end": self.end,
               "line": self.line, "heading_path": self.heading_path,
               "text": self.text}
        if self.kind == FENCE:
            out["info"] = self.info
        if self.kind == TABLE:
            out["header"] = self.header
            out["rows"] = list(self.rows)
        if self.kind == LIST:
            out["items"] = list(self.items)
        return out


@dataclass
class Document:
    """A parsed document, plus what the parse itself observed."""

    source: str
    units: list[Unit] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Unit]:
        return [u for u in self.units if u.kind == kind]

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in KINDS}
        for u in self.units:
            out[u.kind] += 1
        return out

    @property
    def signal_units(self) -> list[Unit]:
        """Everything a semantic pass should see: all units except blanks.

        Headings are included. They carry scope — *"## Backtesting only"* is the
        condition on every rule beneath it — and the deterministic path excludes
        1,826 of them corpus-wide.
        """
        return [u for u in self.units if u.kind != BLANK]


class LosslessError(AssertionError):
    """The units do not tile the source. Always fatal."""


def verify_lossless(doc: Document) -> None:
    """Raise unless the units tile `doc.source` exactly.

    Three separate checks, because each catches a different way a tiling breaks
    and passing one says nothing about the others: a gap or overlap between
    adjacent units; a unit whose `text` is not its own span; and a
    reconstruction that differs from the source. The third alone would catch
    most of it, but not a unit whose `text` was correct while its `start`/`end`
    pointed somewhere else — and the spans are what the evidence grounding in
    `semantic.py` resolves against, so a wrong span with right text is the
    dangerous case rather than the harmless one.
    """
    units = doc.units
    if not units:
        if doc.source:
            raise LosslessError(
                f"no units for a {len(doc.source)}-character source")
        return

    if units[0].start != 0:
        raise LosslessError(f"first unit starts at {units[0].start}, not 0")
    if units[-1].end != len(doc.source):
        raise LosslessError(
            f"last unit ends at {units[-1].end}, source is "
            f"{len(doc.source)} characters")
    for a, b in zip(units, units[1:]):
        if a.end != b.start:
            raise LosslessError(
                f"{'gap' if a.end < b.start else 'overlap'} between "
                f"{a.kind}[{a.start}:{a.end}] and {b.kind}[{b.start}:{b.end}]")
    for u in units:
        if u.text != doc.source[u.start:u.end]:
            raise LosslessError(
                f"{u.kind}[{u.start}:{u.end}] text is not its own span")
    rebuilt = "".join(u.text for u in units)
    if rebuilt != doc.source:
        raise LosslessError(
            f"reconstruction differs from source: {len(rebuilt)} vs "
            f"{len(doc.source)} characters")


def _lines(source: str) -> list[tuple[str, int, int]]:
    """`(text_with_newline, start, end)` per line, covering the whole source."""
    out: list[tuple[str, int, int]] = []
    off = 0
    for ln in source.splitlines(keepends=True):
        out.append((ln, off, off + len(ln)))
        off += len(ln)
    return out


def _is_blank(line: str) -> bool:
    return not line.strip()


def _starts_structure(line: str) -> bool:
    """Does this line begin something other than prose?"""
    bare = line.rstrip("\r\n")
    return bool(_FENCE_OPEN.match(bare) or _HEADING.match(bare)
                or _TABLE_LINE.match(bare) or _LIST_OPEN.match(bare))


def _split_items(text: str) -> tuple[str, ...]:
    """A list block's items, each including its own continuation lines."""
    items: list[str] = []
    for ln in text.splitlines(keepends=True):
        if _LIST_OPEN.match(ln.rstrip("\r\n")) or not items:
            items.append(ln)
        else:
            items[-1] += ln
    return tuple(i.rstrip("\n") for i in items if i.strip())


def _split_table(text: str) -> tuple[str, tuple[str, ...]]:
    """`(header, data rows)` for a table block, verbatim and newline-stripped.

    Everything before the `|---|` rule is header. A table written without one
    yields no rows rather than importing its labels as data — the same rule
    `candidates._table_rows` uses, restated here rather than imported, because
    importing it would couple the frozen deterministic path to this one.
    """
    header = ""
    rows: list[str] = []
    past = False
    for raw in text.splitlines():
        if not _TABLE_LINE.match(raw):
            continue
        if _TABLE_DELIM.match(raw):
            past = True
        elif past:
            rows.append(raw)
        elif not header:
            header = raw
    return header, tuple(rows)


def parse(source: str) -> Document:
    """Tile *source* into structural units. Never raises; see `verify_lossless`."""
    doc = Document(source=source)
    lines = _lines(source)
    if not lines:
        return doc

    i = 0
    n = len(lines)
    heads: list[tuple[int, str]] = []        # (level, title) stack

    def path() -> str:
        return " > ".join(t for _, t in heads)

    def emit(kind: str, lo: int, hi: int, first_line: int, **kw) -> None:
        doc.units.append(Unit(kind=kind, start=lo, end=hi, text=source[lo:hi],
                              line=first_line, heading_path=path(), **kw))

    # --- frontmatter, only at offset 0 -------------------------------------
    if _FM_FENCE.match(lines[0][0].rstrip("\r\n")):
        close = None
        for j in range(1, n):
            if _FM_FENCE.match(lines[j][0].rstrip("\r\n")):
                close = j
                break
        if close is not None:
            emit(FRONTMATTER, lines[0][1], lines[close][2], 1)
            i = close + 1

    while i < n:
        line, lo, hi = lines[i]
        bare = line.rstrip("\r\n")

        # --- blank run ----------------------------------------------------
        if _is_blank(line):
            j = i
            while j < n and _is_blank(lines[j][0]):
                j += 1
            emit(BLANK, lo, lines[j - 1][2], i + 1)
            i = j
            continue

        # --- fence: wins every tie ----------------------------------------
        m = _FENCE_OPEN.match(bare)
        if m:
            token = m.group(1)[:3]
            info = (m.group(2) or "").lower()
            j = i + 1
            while j < n:
                mb = _FENCE_OPEN.match(lines[j][0].rstrip("\r\n"))
                if mb and mb.group(1)[:3] == token:
                    j += 1
                    break
                j += 1
            # An unclosed fence runs to end of file, which is how real markdown
            # readers treat it and keeps the tiling total.
            emit(FENCE, lo, lines[min(j, n) - 1][2], i + 1, info=info)
            i = j
            continue

        # --- heading ------------------------------------------------------
        mh = _HEADING.match(bare)
        if mh:
            level, title = len(mh.group(1)), mh.group(2)
            while heads and heads[-1][0] >= level:
                heads.pop()
            # The heading's own path excludes itself, so a rule *under* it reads
            # the heading as scope while the heading unit is not its own parent.
            emit(HEADING, lo, hi, i + 1)
            heads.append((level, title))
            i += 1
            continue

        # --- table: a pipe line whose successor is the `|---|` rule --------
        if (_TABLE_LINE.match(bare) and i + 1 < n
                and _TABLE_DELIM.match(lines[i + 1][0].rstrip("\r\n"))):
            j = i
            while j < n and _TABLE_LINE.match(lines[j][0].rstrip("\r\n")):
                j += 1
            text = source[lo:lines[j - 1][2]]
            header, rows = _split_table(text)
            emit(TABLE, lo, lines[j - 1][2], i + 1, header=header, rows=rows)
            i = j
            continue

        # --- list ---------------------------------------------------------
        if _LIST_OPEN.match(bare):
            j = i + 1
            while j < n:
                nb = lines[j][0].rstrip("\r\n")
                if _is_blank(lines[j][0]):
                    break
                if _LIST_OPEN.match(nb) or _LIST_CONT.match(nb):
                    j += 1
                    continue
                break
            text = source[lo:lines[j - 1][2]]
            emit(LIST, lo, lines[j - 1][2], i + 1, items=_split_items(text))
            i = j
            continue

        # --- prose: to the next blank line or structure --------------------
        j = i + 1
        while j < n:
            if _is_blank(lines[j][0]) or _starts_structure(lines[j][0]):
                break
            j += 1
        emit(PROSE, lo, lines[j - 1][2], i + 1)
        i = j

    return doc
