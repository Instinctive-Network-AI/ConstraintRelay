"""Profile adapters.

A profile predicts the delivered text for a load set **without running the
harness and without a model call**. Each one here corresponds to a replayed
profile document under `constraintflow/profiles/`, and each declares the steps
it does not model so that `verdict.resolve_block` can fall back to `UNKNOWN`.

The interface is deliberately not frozen: A5 says no adapter or profile
interface is frozen until M0 closes, and Brief v0.5 has not been read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from .verdict import Cause, DeliveredSpan

from constraintrelay import datafiles as _df


@dataclass
class InputFile:
    """One authored file, plus what the loader actually sees of it.

    Verdicts are reported in **file** coordinates, so `file_text` is the text on
    disk and blocks are segmented over it. `loader_view` is the slice the loader
    receives — for Vibe-Trading that is the frontmatter-stripped body, so a
    clause authored in frontmatter is correctly reported as not delivered
    instead of shifting every line number by the frontmatter's length.
    """

    name: str
    path: str
    file_text: str
    loader_view: str | None = None
    #: CF-016: this file does not exist, and the loader still gives it a slot. Four
    #: of OpenClaw's six bootstrap names inject `[MISSING] Expected at: <path>` and
    #: charge its length to the shared budget (`bootstrap.ts:403-414`), so an absent
    #: file is part of the budget arithmetic rather than absent from it. A load set
    #: that simply omits it over-predicts the budget left for later files.
    missing: bool = False
    #: The file exists but could not be decoded. Distinguished from `missing`
    #: because the real loader substitutes `[UNREADABLE: <reason>]` content rather
    #: than a missing record — the slot is consumed, and no authored text is
    #: delivered from it.
    unreadable: bool = False

    @property
    def view(self) -> str:
        return self.file_text if self.loader_view is None else self.loader_view


@dataclass
class Prediction:
    """What a profile predicts for one input file."""

    loaded: bool
    delivered: str
    load_set_reason: str = ""
    unmodeled: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)
    #: Mechanism that removed text from this file, when one did. The scan uses
    #: it for blocks that did not survive; a block that did is always NONE.
    cause: Cause = Cause.TRUNCATED
    #: Pre-merge ruling item 3: the source ranges this file's delivered text is
    #: accounted for by, in `file_text` coordinates. `None` means the profile does
    #: not yet recover spans and membership falls back to containment — which the
    #: qualification gate reports as an open gate rather than letting it pass
    #: quietly.
    spans: "tuple | None" = None
    #: Text the loader emits **in this file's place** that is not authored content:
    #: OpenClaw's `[MISSING] Expected at: <path>` and `[UNREADABLE: <reason>]`
    #: notices. It reaches context and it spends the shared budget, so a differential
    #: test has to be able to see it; no block can ever be delivered from it, so the
    #: verdict layers must not. Kept out of `delivered` for exactly that reason —
    #: folding it in would let a notice satisfy a containment test.
    injected: str = ""


class Profile(Protocol):
    name: str
    verified_against: str

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        """Map path -> prediction for the whole load set at once, because budgets
        can be shared across files."""


# ---------------------------------------------------------------------------
# hermes-agent @ 2517917de  — see profiles/hermes-agent.md
# ---------------------------------------------------------------------------

# Four tiers, first found wins, matching `build_context_files_prompt`. Tier 4
# is a *group*: `.cursorrules` and every `.cursor/rules/*.mdc`, concatenated
# into one capped section rather than one file winning.
#
# CF-004: tier 4 was documented in profiles/hermes-agent.md but missing from
# this list, so a repository whose only context file is `.cursorrules` got
# "no project context type matched" and every block came back NOT_DELIVERED —
# 100% false definite statuses on two List B repositories.
#
# CF-011: tier 1 is the only tier that walks. `_find_hermes_md` searches the
# working directory and then every parent up to and including the git root, so
# tier-1 candidates arrive in the load set from outside the scanned directory and
# are matched on their *basename*. Tiers 2-4 are documented "cwd only" in the
# loader's own docstring and are matched on the exact load-set name, so an
# ancestor `AGENTS.md` can never win.
_HERMES_WALKED = (".hermes.md", "HERMES.md")

_HERMES_PRIORITY = [
    (["AGENTS.md", "agents.md"], "AGENTS.md"),
    (["CLAUDE.md", "claude.md"], "CLAUDE.md"),
]

_CURSOR_RULES_PREFIX = ".cursor/rules/"


def _strip_yaml_frontmatter(content: str) -> str:
    """Byte-exact port of hermes-agent's `_strip_yaml_frontmatter`
    (`agent/prompt_builder.py:100`), applied by tier 1 only.

    Deliberately *not* `discover.strip_frontmatter`, which differs on one case: a
    file that is nothing but frontmatter. The real loader returns the original
    content there (`return body if body else content`), ours returns the empty
    body. For a scope clause authored inside frontmatter that is the difference
    between NOT_DELIVERED and DELIVERED, so the two cannot share an implementation
    just because they share a name.
    """
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            body = content[end + 4:].lstrip("\n")
            return body if body else content
    return content


def _is_cursor_group(f: "InputFile") -> bool:
    return f.name == ".cursorrules" or f.name.startswith(_CURSOR_RULES_PREFIX)


def _section_to_source(kept: "tuple[tuple[int, int], ...]", *, section_start: int,
                       source_start: int, length: int
                       ) -> "tuple[DeliveredSpan, ...]":
    """Translate kept *section* ranges into `file_text` spans for one source file.

    `section_start` is where this file's content begins inside the delivered
    section (past any `## label` prefix, and past earlier files for the tier-4
    group); `source_start` is where that same content begins in the file on disk;
    `length` is the content length. Ranges are intersected rather than offset, so a
    file whose content was partly inside the head and partly dropped yields only
    the part that survived.
    """
    spans: list[DeliveredSpan] = []
    for a, b in kept:
        lo = max(a, section_start)
        hi = min(b, section_start + length)
        if hi <= lo:
            continue
        spans.append(DeliveredSpan(source_start + (lo - section_start),
                                   source_start + (hi - section_start),
                                   "head" if a == 0 else "tail"))
    return tuple(spans)


def _stripped_offset(raw: str, stripped: str) -> int:
    """Offset of `stripped` inside `raw`, for a `.strip()` or suffix-removal.

    Computed from lengths rather than by searching, because `str.index` would find
    an *earlier* identical occurrence — exactly the within-file repetition that
    ruling item 3 exists to stop being load-bearing.
    """
    return len(raw) - len(raw.lstrip()) if raw.strip() == stripped else \
        len(raw) - len(stripped)


class HermesContextScan:
    """Model of `_scan_context_content`, from extracted profile data.

    On any hit the loader replaces the entire file content with a
    `[BLOCKED: ...]` notice, so delivery for every block in the file is zero.
    Size plays no part. The data file records the commit it was derived from;
    `tests/test_hermes_scan.py` checks this model against the real scanner.
    """

    def __init__(self, data_path: Path | None = None) -> None:
        path = _df.require(data_path or _df.path("hermes_context_scan.json"),
                           what="the hermes context-scan patterns")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.source_commit: str = data["source_commit"]
        self.invisible: frozenset[str] = frozenset(data["invisible_chars_literal"])
        self.patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(p["regex"], re.IGNORECASE), p["id"]) for p in data["patterns"]
        ]

    def findings(self, content: str) -> list[str]:
        """Matched ids, in the real scanner's order: invisible characters first."""
        if not content:
            return []
        out = [f"invisible_unicode_U+{ord(ch):04X}"
               for ch in (set(content) & self.invisible)]
        out += [pid for rx, pid in self.patterns if rx.search(content)]
        return out

    def blocked_notice(self, file_name: str, findings: list[str]) -> str:
        return (f"[BLOCKED: {file_name} contained potential prompt injection "
                f"({', '.join(findings)}). Content not loaded.]")


def remap_spans_to_file(predictions: "dict[str, Prediction]",
                        files: "list[InputFile]") -> "dict[str, Prediction]":
    """Move every span from **loader-view** coordinates into **file** coordinates.

    Run-path ruling item 6, and CF-022's second half. A profile whose harness reads
    through universal-newline translation computes its spans over the translated text,
    while blocks are segmented over the raw file — so on a CRLF file every span is left
    of where it belongs by the number of `\r` characters dropped before it. One per line
    is enough to move a verdict on a 600-line `AGENTS.md`.

    A no-op when `loader_view` is None or identical to `file_text`, which is every file
    with no `\r` in it and every profile whose harness reads bytes — so OpenClaw and the
    ordinary LF case pay nothing.
    """
    from .discover import NEWLINES_UNIVERSAL, view_offset_to_file
    out: dict[str, Prediction] = {}
    for f in files:
        pred = predictions.get(f.path)
        if pred is None:
            continue
        if (pred.spans is None or not pred.spans or f.loader_view is None
                or f.loader_view == f.file_text):
            out[f.path] = pred
            continue
        m = view_offset_to_file(f.file_text, NEWLINES_UNIVERSAL)
        last = len(m) - 1

        def at(i: int) -> int:
            return m[min(max(i, 0), last)]
        pred.spans = tuple(
            DeliveredSpan(at(s.start), at(s.end), s.kind, s.verbatim,
                          s.delivered_text)
            for s in pred.spans)
        out[f.path] = pred
    for path, pred in predictions.items():
        out.setdefault(path, pred)
    return out


class HermesAgentProfile:
    name = "hermes-agent@2517917de"
    verified_against = "NousResearch/hermes-agent 2517917de (2026-05-25)"

    LIMIT = 20_000
    HEAD_RATIO = 0.7
    TAIL_RATIO = 0.2
    UNMODELED: tuple[str, ...] = ()

    def __init__(self, scan: HermesContextScan | None = None) -> None:
        self.scan = scan or HermesContextScan()

    def _cap(self, section: str, marker_name: str
             ) -> "tuple[str, tuple[tuple[int, int], ...]]":
        """Capped text, and which ranges of *section* survived.

        The ranges are what pre-merge ruling item 3 needs: hermes counts code
        points natively, so no unit conversion is involved, but the delivered blob
        still carries a `## label` prefix that is not part of any source file —
        which is finding 5, and why the caller maps section coordinates back to
        source rather than reusing these offsets directly.
        """
        if len(section) <= self.LIMIT:
            return section, ((0, len(section)),)
        head = int(self.LIMIT * self.HEAD_RATIO)
        tail = int(self.LIMIT * self.TAIL_RATIO)
        marker = (f"\n\n[...truncated {marker_name}: kept {head}+{tail} "
                  f"of {len(section)} chars. Use file tools to read the "
                  f"full file.]\n\n")
        return (section[:head] + marker + section[-tail:],
                ((0, head), (len(section) - tail, len(section))))

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        """Delivery for the hermes load set, in **file** coordinates.

        The arithmetic below runs over `f.view`, which for this harness is the
        universal-newline-translated text its own `read_text` produces. Spans therefore
        come out in view coordinates and are remapped once, here, rather than at each of
        the four places they are constructed.
        """
        return remap_spans_to_file(self._predict_in_view(files), files)

    def _predict_in_view(self, files: list[InputFile]) -> dict[str, Prediction]:
        by_name = {f.name: f for f in files}
        winner = None
        winner_label = ""
        strip_front = False

        # Tier 1 walks. The load set already arrives in the loader's search order
        # — working directory, then nearest ancestor — so the first tier-1
        # basename in that order is the file the real loader returns. Within one
        # directory `.hermes.md` precedes `HERMES.md`, which `collect_ancestors`
        # preserves by iterating the names in that order.
        for f in files:
            if PurePosixPath(f.name).name in _HERMES_WALKED:
                winner, winner_label = f, PurePosixPath(f.name).name
                # `_load_hermes_md` is the only tier that calls
                # `_strip_yaml_frontmatter`, before the scan and before the cap.
                strip_front = True
                break

        for names, label in [] if winner else _HERMES_PRIORITY:
            for n in names:
                if n in by_name:
                    winner, winner_label = by_name[n], label
                    break
            if winner:
                break

        out: dict[str, Prediction] = {}

        # Tier 4: the cursor group, reached only when tiers 1-3 found nothing.
        cursor_group = sorted((f for f in files if _is_cursor_group(f)),
                              key=lambda f: (f.name != ".cursorrules", f.name))
        if winner is None and cursor_group:
            blob = ""
            notes: list[str] = []
            # Where each file's content sits inside the shared blob, so a span can
            # be traced back to the file it came from. Finding 3: one shared cap
            # across several files means position *within the concatenation* is
            # what decides delivery, so per-file provenance is the only way to
            # answer "was this file's clause delivered".
            placed: dict[str, tuple[int, int, int]] = {}
            for f in cursor_group:
                raw = f.view
                content = raw.strip()
                if not content:
                    continue
                findings = self.scan.findings(content)
                if findings:
                    # The whole file is replaced by a notice, so no source range of
                    # it is delivered at all. Recorded by simply not placing it.
                    content = self.scan.blocked_notice(f.name, findings)
                    notes.append(f"content scan blocked {f.name}: "
                                 f"{', '.join(findings)}")
                    blob += f"## {f.name}\n\n{content}\n\n"
                    continue
                header = f"## {f.name}\n\n"
                placed[f.path] = (len(blob) + len(header),
                                  _stripped_offset(raw, content), len(content))
                blob += f"{header}{content}\n\n"
            delivered, kept = (self._cap(blob, ".cursorrules") if blob else ("", ()))
            for f in files:
                if _is_cursor_group(f):
                    if f.path in placed:
                        section_start, source_start, length = placed[f.path]
                        spans = _section_to_source(
                            kept, section_start=section_start,
                            source_start=source_start, length=length)
                    else:
                        spans = ()
                    out[f.path] = Prediction(
                        True, delivered, "", self.UNMODELED, list(notes),
                        cause=Cause.REJECTED_BY_LOADER if notes else Cause.TRUNCATED,
                        spans=spans)
                else:
                    out[f.path] = Prediction(
                        False, "", "not loaded: .cursorrules takes priority",
                        self.UNMODELED, cause=Cause.NOT_IN_LOAD_SET, spans=())
            return out

        for f in files:
            if winner is None or f is not winner:
                reason = (f"not loaded: {winner.name} takes priority"
                          if winner is not None else
                          "not loaded: no project context type matched")
                out[f.path] = Prediction(False, "", reason, self.UNMODELED,
                                         cause=Cause.NOT_IN_LOAD_SET, spans=())
                continue

            raw = f.view
            content = raw.strip()
            source_start = _stripped_offset(raw, content)
            if strip_front:
                body = _strip_yaml_frontmatter(content)
                # `body` is a suffix of `content` (or `content` itself, for the
                # all-frontmatter case), so the offset follows from the lengths.
                source_start += len(content) - len(body)
                content = body
            findings = self.scan.findings(content)
            if findings:
                notice = self.scan.blocked_notice(winner.name, findings)
                # Total loss, independent of size: no source range is delivered.
                out[f.path] = Prediction(
                    True, f"## {winner_label}\n\n{notice}", "", self.UNMODELED,
                    [f"content scan blocked the whole file: {', '.join(findings)}"],
                    cause=Cause.REJECTED_BY_LOADER, spans=())
                continue

            header = f"## {winner_label}\n\n"
            delivered, kept = self._cap(f"{header}{content}", winner_label)
            out[f.path] = Prediction(
                True, delivered, "", self.UNMODELED,
                spans=_section_to_source(kept, section_start=len(header),
                                         source_start=source_start,
                                         length=len(content)))
        return out


# ---------------------------------------------------------------------------
# Vibe-Trading @ dddf96e  — see profiles/vibe-trading.md
# ---------------------------------------------------------------------------

class VibeTradingProfile:
    """Skill files delivered as serialized tool results.

    The cap lands on the JSON envelope, so JSON escaping is inside the budget.
    """

    name = "vibe-trading@dddf96e"
    verified_against = "HKUDS/Vibe-Trading dddf96e (the ICAIF 2026 pin)"

    LIMIT = 10_000
    UNMODELED: tuple[str, ...] = ()

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        out: dict[str, Prediction] = {}
        for f in files:
            wrapped = f'<skill name="{f.name}">\n{f.view}\n</skill>'
            envelope = json.dumps({"status": "ok", "content": wrapped},
                                  ensure_ascii=False)
            out[f.path] = Prediction(True, envelope[:self.LIMIT], "", self.UNMODELED)
        return out


def block_needle(profile: Profile, text: str) -> str:
    """How a block's source text appears inside this profile's delivered text.

    Vibe-Trading escapes content into JSON, so a raw source slice is not a
    substring of the delivered envelope. Every other profile delivers source
    text verbatim.
    """
    if isinstance(profile, VibeTradingProfile):
        return json.dumps(text, ensure_ascii=False)[1:-1]
    return text
