"""Block segmentation — ConstraintFlow's frozen block semantics (§3).

A *block* is the unit a verdict is reported about, and §3's single-block marker
form is defined in terms of it: "the marker applies to the **next syntactic
block** under ConstraintFlow's frozen block semantics."

Segmentation: headings, list items and blank-line-separated paragraphs, each
carrying its source range so that a verdict can cite `path:line`.

A ConstraintFlow marker line is **not** a block and never joins one. It is
recognised here rather than in `markers.py` so that there is exactly one
segmentation in the system: the marker layer resolves a constraint's extent to
block indices, and the verdict layer looks verdicts up by those same indices, so
two segmentations that disagree by one block would silently attribute a verdict
to the wrong constraint. Treating a marker line as a separator also stops it
being glued into the paragraph it precedes, which is §3's own example layout.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

_HEADING = re.compile(r"^#{1,6}\s+\S")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")

#: A whole-line ConstraintFlow marker. A marker sharing a line with prose is not
#: recognised, so it cannot silently mark the wrong extent.
MARKER_LINE = re.compile(
    r"^[ \t]*<!--[ \t]*constraintflow[ \t]*:[ \t]*"
    r"(?P<kind>critical|end)\b(?P<attrs>[^>]*?)-->[ \t]*$"
)


@dataclass(frozen=True)
class Block:
    """One reportable unit of authored text."""

    index: int
    kind: str            # heading | list_item | paragraph
    text: str            # verbatim source slice
    start: int           # source offset, inclusive
    end: int             # source offset, exclusive
    line: int            # 1-indexed first line

    LABEL_MAX = 160

    @property
    def end_line(self) -> int:
        return self.line + self.text.count("\n")

    @property
    def label(self) -> str:
        """Display label.

        Long enough to keep an operative clause visible: the anchor case in the
        ICAIF paper carries its scope restriction at column 84 of a version
        footer, so a short label cuts off exactly the text a verdict is about.
        """
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return first if len(first) <= self.LABEL_MAX else first[:self.LABEL_MAX - 1] + "\u2026"


def segment(source: str) -> list[Block]:
    """Split *source* into blocks, preserving exact source offsets."""
    blocks: list[Block] = []
    offset = 0
    line_no = 1
    pending: list[tuple[str, int, int]] = []   # (line_text, start, line_no)

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        start = pending[0][1]
        end = pending[-1][1] + len(pending[-1][0])
        text = source[start:end]
        if text.strip():
            blocks.append(Block(len(blocks), "paragraph", text, start, end, pending[0][2]))
        pending = []

    for raw in source.splitlines(keepends=True):
        stripped = raw.rstrip("\r\n")
        body = stripped.strip()
        if not body or MARKER_LINE.match(stripped):
            # A marker separates blocks exactly as a blank line does, and emits
            # nothing of its own.
            flush()
        elif _HEADING.match(stripped):
            flush()
            blocks.append(Block(len(blocks), "heading", stripped, offset,
                                offset + len(stripped), line_no))
        elif _LIST_ITEM.match(stripped):
            flush()
            blocks.append(Block(len(blocks), "list_item", stripped, offset,
                                offset + len(stripped), line_no))
        else:
            pending.append((stripped, offset, line_no))
        offset += len(raw)
        line_no += 1
    flush()
    return blocks
