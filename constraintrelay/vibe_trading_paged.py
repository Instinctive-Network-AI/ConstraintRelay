"""Vibe-Trading @ 9a27a6e — the paged skill loader, and the profile that can
prove `AVAILABLE ONLY ON REQUEST`.

At `dddf96e` an oversized skill was a silent prefix and everything past the cut
was simply gone. At `9a27a6e` the same skill comes back as an **outline** plus
its opening text, and the rest is reachable two ways: by naming a section from
that outline, or by sequential `offset` paging. So a block that misses the first
load is not necessarily lost — which is exactly the distinction
`AVAILABLE ONLY ON REQUEST` exists to make.

The status is only claimed when a retrieval path is **enumerated and shown to
contain the block**. "Some other read might reach it" is never enough: this
module simulates the actual calls the agent would make and reports the call that
returns the text, or reports nothing.

Ported from `agent/src/tools/load_skill_tool.py`, `agent/src/config/limits.py`
and `agent/src/agent/skills.py` at `9a27a6e`, and differentially tested against
that real loader in `tests/test_on_request.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .profiles import InputFile, Prediction

TOOL_RESULT_LIMIT = 10_000
_PAGE_CHARS = TOOL_RESULT_LIMIT - 600
_MIN_PAGE_CHARS = 1_000
_OUTLINE_CHARS = 4_500
_SUMMARY_CHARS = 110
PATH_SEPARATOR = ">"

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Section:
    level: int
    title: str
    start: int
    end: int
    summary: str = ""

    @property
    def chars(self) -> int:
        return self.end - self.start


def split_sections(document: str) -> list[Section]:
    raw: list[tuple[int, str, int, str]] = []
    offset = 0
    fenced = False
    pending: list[int] = []
    for line in document.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if _FENCE_RE.match(stripped):
            fenced = not fenced
        elif not fenced:
            heading = _HEADING_RE.match(stripped)
            if heading:
                raw.append((len(heading.group(1)), heading.group(2), offset, ""))
                pending.append(len(raw) - 1)
            elif pending and stripped.strip():
                index = pending[-1]
                pending.clear()
                summary = stripped.strip().lstrip("-*>| ").strip("*`").strip()
                if len(summary) > _SUMMARY_CHARS:
                    summary = summary[:_SUMMARY_CHARS - 1].rstrip() + "…"
                level, title, start, _ = raw[index]
                raw[index] = (level, title, start, summary)
        offset += len(line)

    total = len(document)
    sections: list[Section] = []
    for index, (level, title, start, summary) in enumerate(raw):
        end = total
        for deeper_level, _, later_start, _ in raw[index + 1:]:
            if deeper_level <= level:
                end = later_start
                break
        sections.append(Section(level, title, start, end, summary))
    return sections


def _ancestors(sections: list[Section], index: int) -> list[str]:
    out: list[str] = []
    level = sections[index].level
    for candidate in reversed(sections[:index]):
        if candidate.level < level:
            out.append(candidate.title)
            level = candidate.level
    out.reverse()
    return out


def qualified_path(sections: list[Section], index: int) -> str:
    parts = _ancestors(sections, index) + [sections[index].title]
    return f" {PATH_SEPARATOR} ".join(parts)


def _render_outline(name: str, sections: list[Section], total: int) -> str:
    header = (
        f"{len(sections)} sections, {total:,} characters total. Only the opening "
        f"text is in `content`.\nCall load_skill(name={name!r}, "
        f'section="<copy a label below verbatim>") to pull one section in full, or '
        f"load_skill(name={name!r}, offset=<next_offset>) to keep reading "
        f"straight through.\nSizes include nested subsections."
    )
    repeated = {
        title for title in (s.title for s in sections)
        if sum(1 for s in sections if s.title == title) > 1
    }
    labels = [qualified_path(sections, i) if s.title in repeated else s.title
              for i, s in enumerate(sections)]

    text = ""
    for with_summary, max_level in ((True, 6), (False, 6), (False, 2)):
        lines = [header]
        for section, label in zip(sections, labels):
            if section.level > max_level:
                continue
            line = f"{'  ' * (section.level - 1)}- {label} ({section.chars:,} chars)"
            if with_summary and section.summary:
                line += f" — {section.summary}"
            lines.append(line)
        text = "\n".join(lines)
        if len(text) <= _OUTLINE_CHARS:
            return text
    return text[:_OUTLINE_CHARS - 40].rstrip() + "\n… outline truncated."


def _paged(base: dict, body: str, offset: int, budget: int) -> tuple[str, bool, int | None]:
    size = max(budget, _MIN_PAGE_CHARS)
    while True:
        page = body[offset:offset + size]
        next_offset = offset + len(page)
        complete = next_offset >= len(body)
        envelope = json.dumps(
            {**base, "content": page, "offset": offset,
             "next_offset": None if complete else next_offset,
             "complete": complete},
            ensure_ascii=False)
        if len(envelope) <= TOOL_RESULT_LIMIT or size <= _MIN_PAGE_CHARS:
            return envelope, complete, (None if complete else next_offset)
        size = max(_MIN_PAGE_CHARS,
                   int(size * TOOL_RESULT_LIMIT / len(envelope)) - 64)


def wrap(name: str, body: str) -> str:
    return f'<skill name="{name}">\n{body}\n</skill>'


def first_load(name: str, content: str) -> str:
    """The envelope the agent receives for `load_skill(name=...)`."""
    total = len(content)
    document, complete, _ = _paged(
        {"status": "ok", "mode": "document", "total_chars": total},
        content, 0, _PAGE_CHARS)
    if complete:
        return document
    sections = split_sections(content)
    if not sections:
        return document
    outline = _render_outline(name, sections, total)
    mapped, _, _ = _paged(
        {"status": "ok", "mode": "outline", "total_chars": total,
         "section_count": len(sections), "outline": outline},
        content, 0, max(_PAGE_CHARS - len(outline), _MIN_PAGE_CHARS))
    return mapped


class VibeTradingPagedProfile:
    """Skill documents, paged. `dddf96e`'s successor with a retrieval path."""

    name = "vibe-trading@9a27a6e"
    verified_against = "HKUDS/Vibe-Trading 9a27a6e (2026-09-29)"
    UNMODELED: tuple[str, ...] = ()

    #: Cap on how many sequential pages a retrieval proof will walk. A path
    #: longer than this is still reported, with its page number, but the bound
    #: keeps a pathological document from making `scan` unbounded.
    MAX_PAGES = 64

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        out: dict[str, Prediction] = {}
        for f in files:
            out[f.path] = Prediction(True, first_load(f.name, wrap(f.name, f.view)),
                                     "", self.UNMODELED)
        return out

    def retrieval_paths(self, f: InputFile, needle: str) -> list[str]:
        """Deterministic calls that return *needle*, proven by simulation.

        Returns an empty list when no enumerated call reaches the text, which is
        what keeps `AVAILABLE ONLY ON REQUEST` from becoming a guess.
        """
        content = wrap(f.name, f.view)
        if needle not in content:
            return []
        paths: list[str] = []

        # 1. Named section, as advertised in the first-load outline.
        sections = split_sections(content)
        for index, section in enumerate(sections):
            body = content[section.start:section.end]
            if needle not in body:
                continue
            label = qualified_path(sections, index) if any(
                s.title == section.title and s is not section for s in sections
            ) else section.title
            envelope, complete, _ = _paged(
                {"status": "ok", "mode": "section", "section": section.title,
                 "section_start": section.start, "total_chars": len(body),
                 "document_chars": len(content)},
                body, 0, _PAGE_CHARS)
            if needle in envelope:
                paths.append(
                    f'load_skill(name={f.name!r}, section={label!r}) '
                    f'— {section.chars:,} chars'
                    f'{"" if complete else ", first page"}')
                break

        # 2. Sequential paging from the first load.
        offset: int | None = 0
        page = 0
        while offset is not None and page < self.MAX_PAGES:
            if page == 0:
                envelope = first_load(f.name, content)
                data = json.loads(envelope)
                offset = data.get("next_offset")
            else:
                envelope, _, offset = _paged(
                    {"status": "ok", "mode": "document",
                     "total_chars": len(content)},
                    content, offset, _PAGE_CHARS)
            page += 1
            if needle in envelope:
                if page > 1:
                    paths.append(f"load_skill(name={f.name!r}, offset=...) "
                                 f"— sequential page {page}")
                break
        return paths
