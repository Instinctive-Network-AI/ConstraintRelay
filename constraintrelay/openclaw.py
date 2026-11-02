"""OpenClaw bootstrap profile — Python port of the real loader's algorithm.

The engine predicts delivery without running the harness, so the profile cannot
shell out to Node at scan time. This is a faithful port of
`buildBootstrapContextFiles` and its trimmers from the installed bundle
(`dist/bootstrap-D7cACmY4.js` at `2026.9.1`), and
`tests/test_openclaw_profile.py` differentially checks it against the real Node
loader via `replays/openclaw/deliver.mjs`.

Two things make this port non-obvious, and both are why it is tested rather than
trusted:

* **The unit is UTF-16 code units**, not Python code points. Every length and
  every slice here goes through `u16len` / `u16slice`.
* **JS `trimEnd` and Python `str.rstrip` disagree.** JS strips `U+FEFF`; Python
  does not consider it whitespace. The explicit `_JS_WHITESPACE` set below
  follows ECMAScript, because the delivered text is what the harness produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .profiles import InputFile, Prediction
from .verdict import Cause, DeliveredSpan

# ---------------------------------------------------------------------------
# UTF-16 measurement
# ---------------------------------------------------------------------------

def _units(s: str) -> bytes:
    return s.encode("utf-16-le", errors="surrogatepass")


def u16len(s: str) -> int:
    return len(_units(s)) // 2


def _unit_at(b: bytes, i: int) -> int:
    return int.from_bytes(b[2 * i:2 * i + 2], "little")


def u16slice(s: str, start: int, end: int | None = None) -> str:
    """Port of `sliceUtf16Safe`: never split a surrogate pair; move inward."""
    b = _units(s)
    n = len(b) // 2
    frm = max(n + start, 0) if start < 0 else min(start, n)
    to = n if end is None else (max(n + end, 0) if end < 0 else min(end, n))
    if to <= frm:
        return ""
    if 0 < frm < n and 0xDC00 <= _unit_at(b, frm) <= 0xDFFF \
            and 0xD800 <= _unit_at(b, frm - 1) <= 0xDBFF:
        frm += 1
    if 0 < to < n and 0xD800 <= _unit_at(b, to - 1) <= 0xDBFF \
            and 0xDC00 <= _unit_at(b, to) <= 0xDFFF:
        to -= 1
    return b[2 * frm:2 * to].decode("utf-16-le", errors="surrogatepass")


def u16truncate(s: str, max_len: int) -> str:
    limit = max(0, int(max_len))
    return s if u16len(s) <= limit else u16slice(s, 0, limit)


# ECMAScript WhiteSpace + LineTerminator, which is what `trimEnd` removes.
_JS_WHITESPACE = (
    "\t\n\v\f\r          "
    "        　﻿"
)


def u16_to_cp(s: str, index: int) -> int:
    """Code-point offset in *s* corresponding to UTF-16 code-unit offset *index*.

    Needed because this loader slices in UTF-16 code units while a block's offsets
    are Python code points (finding 9). An index that lands *inside* a surrogate
    pair is rounded **down** to the start of that character: delivered spans must
    never claim half an astral character, and claiming less is the safe direction.
    """
    if index <= 0:
        return 0
    used = 0
    for i, ch in enumerate(s):
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > index:
            return i
        used += width
        if used == index:
            return i + 1
    return len(s)


def js_trim_end(s: str) -> str:
    return s.rstrip(_JS_WHITESPACE)


def js_trim(s: str) -> str:
    return s.strip(_JS_WHITESPACE)


# ---------------------------------------------------------------------------
# Constants, from dist/bootstrap-D7cACmY4.js
# ---------------------------------------------------------------------------

DEFAULT_BOOTSTRAP_MAX_CHARS = 20_000
DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS = 60_000
USER_BOOTSTRAP_MAX_CHARS = 4_000
MIN_BOOTSTRAP_FILE_BUDGET_CHARS = 64
MIN_BOOTSTRAP_TRIMMED_CONTENT_CHARS = 16
BOOTSTRAP_HEAD_RATIO = 0.75
BOOTSTRAP_TAIL_RATIO = 0.25
AGENTS_POLICY_DIGEST_RATIO = 0.35
AGENTS_POLICY_HEAD_RATIO = 0.45
AGENTS_POLICY_TAIL_RATIO = 0.15
AGENTS_POLICY_DIGEST_MAX_LINE_CHARS = 240

_STRUCTURAL = re.compile(r"^(?:#{1,6}|\s*[-*+]|\s*\d+[.)])\s+\S")
_CANDIDATE_KEYWORDS = re.compile(
    r"\b(?:AGENTS\.md|scoped|required|must|never|do not|before subtree|"
    r"read scoped|owner|security|secret|credential|test|validation|command|"
    r"commit|push|github|pr)\b", re.IGNORECASE)
_HIGH_PRIORITY = re.compile(
    r"\b(?:AGENTS\.md|scoped|required|must|never|do not|before subtree|"
    r"read scoped|security|secret|credential)\b", re.IGNORECASE)


def clamp_to_budget(content: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if u16len(content) <= budget:
        return content
    if budget <= 3:
        return u16truncate(content, budget)
    return u16truncate(content, budget - 1) + "…"


@dataclass
class _Candidate:
    line: str
    high_priority: bool
    #: Source offsets of the line this candidate was normalised from, in `trimmed`
    #: coordinates. Pre-merge ruling item 3 needs the provenance, not just the text.
    src_start: int = 0
    src_end: int = 0


def _normalize_digest_line(line: str) -> str:
    normalized = re.sub(r"\s+", " ", js_trim(line), flags=re.UNICODE)
    if u16len(normalized) <= AGENTS_POLICY_DIGEST_MAX_LINE_CHARS:
        return normalized
    return u16truncate(normalized, 239) + "…"


def _is_candidate(line: str) -> bool:
    return bool(_STRUCTURAL.match(line)) or bool(_CANDIDATE_KEYWORDS.search(line))


def _build_digest(candidates: list[_Candidate], budget: int
                  ) -> tuple[str, int, list[int]]:
    """Delivered digest text, the omitted count, and **which candidates** made it.

    The third element is the provenance: without it a digest line cannot be traced
    to a source line, and membership falls back to searching the blob.
    """
    if budget <= 0:
        return "", 0, []
    selected: list[int] = []
    used = 0
    chosen: set[int] = set()

    def try_select(i: int) -> None:
        nonlocal used
        sep = 1 if chosen else 0
        length = u16len(candidates[i].line)
        if used + sep + length > budget:
            return
        chosen.add(i)
        used += sep + length

    for i, c in enumerate(candidates):
        if c.high_priority:
            try_select(i)
    for i in range(len(candidates)):
        if i not in chosen:
            try_select(i)
    selected = [i for i in range(len(candidates)) if i in chosen]
    text = "\n".join(candidates[i].line for i in selected)
    return text, max(0, len(candidates) - len(selected)), selected


def _trim_agents(trimmed: str, max_chars: int) -> str:
    head_chars = int(max_chars * AGENTS_POLICY_HEAD_RATIO)
    tail_chars = int(max_chars * AGENTS_POLICY_TAIL_RATIO)
    digest_budget = int(max_chars * AGENTS_POLICY_DIGEST_RATIO)

    # Source offsets are carried alongside each candidate so a digest line can be
    # traced back to the line that produced it (pre-merge ruling item 3). Splitting
    # on newlines would throw exactly that away.
    candidates: list[_Candidate] = []
    if digest_budget > 0:
        # `re.split` discards the separator, and the separator is one or two
        # characters: the real loader splits on `/\r?\n/u`. Advancing the offset by
        # a constant `len(src) + 1` therefore drifts by one code point per CRLF
        # line, so on a Windows-authored `AGENTS.md` every digest span after the
        # first line named the wrong source range. `finditer` keeps the real spans
        # instead of reconstructing them.
        offset = 0
        for m in re.finditer(r"(?P<line>[^\r\n]*)(?:\r?\n|$)", trimmed):
            src = m.group("line")
            start, end = m.start("line"), m.end("line")
            line = _normalize_digest_line(src)
            if line and _is_candidate(line):
                candidates.append(_Candidate(line, bool(_HIGH_PRIORITY.search(line)),
                                             start, end))
            offset = m.end()
            if offset >= len(trimmed):
                break

    digest_text, omitted, chosen = _build_digest(candidates, digest_budget)

    def render() -> str:
        parts = [
            u16slice(trimmed, 0, head_chars),
            "[...truncated, read AGENTS.md for full content...]",
            "[Policy digest from AGENTS.md]" if digest_text else "",
            digest_text,
            f"[...{omitted} more policy lines omitted...]" if omitted > 0 else "",
            (f"…(truncated AGENTS.md: kept {head_chars}+policy "
             f"{u16len(digest_text)}+{tail_chars} chars of {u16len(trimmed)})…"),
            u16slice(trimmed, -tail_chars) if tail_chars > 0 else "",
        ]
        return "\n".join(p for p in parts if p)

    rendered = render()
    while u16len(rendered) > max_chars and (tail_chars > 0 or head_chars > 1
                                            or digest_budget > 0):  # noqa: E501
        overflow = u16len(rendered) - max_chars
        if tail_chars > 0:
            tail_chars = max(0, tail_chars - overflow)
        elif head_chars > 1:
            head_chars = max(1, head_chars - overflow)
        else:
            digest_budget = max(0, digest_budget - overflow)
            digest_text, omitted, chosen = _build_digest(candidates, digest_budget)
        rendered = render()
    # Spans are built from the FINAL head/tail/digest values, after the shrink
    # loop above. Computing them earlier would describe a rendering that was then
    # thrown away — the kind of near-miss that makes a provenance claim worse than
    # none.
    spans: list[DeliveredSpan] = []
    if head_chars > 0:
        spans.append(DeliveredSpan(0, u16_to_cp(trimmed, head_chars), "head"))
    for i in chosen:
        c = candidates[i]
        # Not verbatim: `_normalize_digest_line` collapses whitespace and may
        # truncate to 240 units, so the delivered rendering is carried with the
        # span and membership is decided inside that one line.
        spans.append(DeliveredSpan(c.src_start, c.src_end, "digest",
                                   verbatim=False, delivered_text=c.line))
    if tail_chars > 0:
        start = u16_to_cp(trimmed, max(0, u16len(trimmed) - tail_chars))
        spans.append(DeliveredSpan(start, len(trimmed), "tail"))

    if u16len(rendered) > max_chars:
        return u16truncate(rendered, max_chars), _clip_tail(
            spans, u16len(rendered) - max_chars)
    return rendered, tuple(spans)


def _clip_tail(spans: "list[DeliveredSpan]", dropped_u16: int
               ) -> "tuple[DeliveredSpan, ...]":
    """Shrink the last span by *dropped_u16* **code points**.

    A final `u16truncate` cuts the end of the rendered output, which is the tail.
    Shrinking by that many code points removes at least as much source as the cut
    removed delivered text, because a code point is never wider than a UTF-16 code
    unit — so the error is always in the direction of claiming less delivery.
    """
    if dropped_u16 <= 0 or not spans:
        return tuple(spans)
    last = spans[-1]
    end = max(last.start, last.end - dropped_u16)
    if end <= last.start:
        return tuple(spans[:-1])
    return tuple(spans[:-1]) + (DeliveredSpan(last.start, end, last.kind,
                                              last.verbatim, last.delivered_text),)


def _trim_bootstrap(content: str, file_name: str, max_chars: int
                    ) -> "tuple[str, tuple[DeliveredSpan, ...]]":
    trimmed = js_trim_end(content or "")
    if u16len(trimmed) <= max_chars:
        return trimmed, (DeliveredSpan(0, len(trimmed), "whole"),)
    if (file_name or "").lower() == "agents.md":
        return _trim_agents(trimmed, max_chars)

    total = u16len(trimmed)

    def marker_template(h: int, t: int) -> str:
        return "\n".join([
            "",
            f"[...truncated, read {file_name} for full content...]",
            f"…(truncated {file_name}: kept {h}+{t} chars of {total})…",
            "",
        ])

    def compact_template(h: int, t: int) -> str:
        return f"[…truncated {h}+{t}/{total}]"

    def sep_for(h: int, t: int, m: str) -> int:
        return (1 if h > 0 else 0) + (1 if t > 0 else 0) if "\n" in m else 0

    full = marker_template(0, 0)
    tpl = (marker_template
           if max_chars - u16len(full) - sep_for(1, 1, full) >= MIN_BOOTSTRAP_TRIMMED_CONTENT_CHARS
           else compact_template)

    h = t = 0
    m = tpl(h, t)
    for _ in range(3):
        budget = max(0, max_chars - u16len(m) - sep_for(h, t, m))
        nh = int(budget * BOOTSTRAP_HEAD_RATIO)
        nt = int(budget * BOOTSTRAP_TAIL_RATIO)
        nm = tpl(nh, nt)
        if nh == h and nt == t and u16len(nm) == u16len(m):
            break
        h, t, m = nh, nt, nm

    length = h + t + u16len(m) + sep_for(h, t, m)
    while length > max_chars and (t > 0 or h > 0):
        overflow = length - max_chars
        if t > 0:
            t = max(0, t - overflow)
        else:
            h = max(0, h - overflow)
        m = tpl(h, t)
        length = h + t + u16len(m) + sep_for(h, t, m)

    if h == 0 and t == 0 and u16len(trimmed) > 0:
        single = tpl(1, 0)
        if 1 + u16len(single) + sep_for(1, 0, single) <= max_chars:
            h, m = 1, single

    head = u16slice(trimmed, 0, h)
    tail = u16slice(trimmed, -t) if t > 0 else ""
    joiner = "\n" if "\n" in m else ""
    out = joiner.join(p for p in (head, m, tail) if p)
    spans: list[DeliveredSpan] = []
    if h > 0:
        spans.append(DeliveredSpan(0, u16_to_cp(trimmed, h), "head"))
    if t > 0:
        spans.append(DeliveredSpan(
            u16_to_cp(trimmed, max(0, u16len(trimmed) - t)), len(trimmed), "tail"))
    if u16len(out) > max_chars:
        return u16truncate(out, max_chars), _clip_tail(spans, u16len(out) - max_chars)
    return out, tuple(spans)


class OpenClawProfile:
    """Workspace bootstrap files, including the absent-but-budgeted slots.

    CF-016: an absent `AGENTS.md`, `SOUL.md`, `IDENTITY.md` or `BOOTSTRAP.md` is
    still handed to `buildBootstrapContextFiles` as `{missing: true}`, which
    injects `[MISSING] Expected at: <path>` and subtracts its length from the
    shared budget (`bootstrap.ts:403-414`). Modelling those slots is not
    bookkeeping: with the budget nearly spent, a `[MISSING]` record is what pushes
    the next real file under the 64-unit floor and skips it, so leaving them out
    over-predicts delivery for every later file.
    """

    name = "openclaw@2026.9.1"
    verified_against = "OpenClaw 2026.9.1 (installed bundle)"
    UNMODELED: tuple[str, ...] = ()

    def __init__(self, max_chars: int = DEFAULT_BOOTSTRAP_MAX_CHARS,
                 total_max_chars: int | None = None) -> None:
        self.max_chars = max_chars
        # CF-019: the real default is `max(maxChars, 60_000)`, not `60_000`
        # (`bootstrap.ts:386-389`). The port had a plain 60,000 default, so a
        # per-file cap above the total default was silently clipped by it: with
        # `maxChars=65_000` the loader gives the first file a 65,000-unit budget and
        # the port gave it 60,000, diverging by 3,000 units of delivered head and
        # tail. Not reachable from a default build today — the registry constructs
        # the profile with no arguments — but `budget` and any future
        # `[target.parameters]` cap can reach it, and a latent off-by-5,000 in a
        # delivery model is the kind of thing that is only latent until it is not.
        self.total_max_chars = (max(max_chars, DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS)
                                if total_max_chars is None else total_max_chars)

    #: An unreadable file's substituted text is `[UNREADABLE: <reason>]`, where the
    #: reason is an OS error message truncated to 300 units (`workspace.ts:1250-1256`).
    #: Its length is therefore not derivable from the repository, and it is charged to
    #: the shared budget — so neither this file nor any later one can be given a
    #: definite status.
    UNREADABLE_STEP = ("a bootstrap file could not be decoded; the loader substitutes "
                       "[UNREADABLE: <os error>] whose length is not derivable from "
                       "the repository and charges it to the shared budget")

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        remaining = max(1, int(self.total_max_chars))
        out: dict[str, Prediction] = {}
        exhausted = False
        #: Set once an unreadable file has consumed an unknown amount of budget.
        #: Everything after it is indeterminate, because the budget it sees is.
        budget_unknown: tuple[str, ...] = ()

        for f in files:
            if f.unreadable:
                out[f.path] = Prediction(
                    False, "", "the file exists but could not be decoded; the loader "
                    "delivers an [UNREADABLE: ...] notice in its place, so none of "
                    "the authored text reaches context",
                    (self.UNREADABLE_STEP,), cause=Cause.REJECTED_BY_LOADER)
                budget_unknown = (self.UNREADABLE_STEP,)
                continue
            if exhausted or remaining <= 0:
                out[f.path] = Prediction(
                    False, "", "not loaded: the shared bootstrap budget was "
                    "already spent by earlier files", self.UNMODELED,
                    cause=Cause.OMITTED_BY_BUDGET)
                exhausted = True
                continue
            # The missing-file branch is checked BEFORE the 64-unit floor, exactly
            # as the real loader does (`bootstrap.ts:403` precedes `:416`). A
            # `[MISSING]` record is therefore emitted even when the remaining budget
            # is below the floor that would skip a real file — and it still charges
            # the budget, so the ordering decides what the next file sees.
            if f.missing:
                missing_text = f"[MISSING] Expected at: {f.path}"
                capped = clamp_to_budget(missing_text, remaining)
                if not capped:
                    out[f.path] = Prediction(
                        False, "", "not loaded: the shared bootstrap budget was "
                        "already spent, so not even the missing-file record fits",
                        self.UNMODELED, cause=Cause.OMITTED_BY_BUDGET)
                    exhausted = True
                    continue
                remaining = max(0, remaining - u16len(capped))
                # No authored text exists, so nothing can be delivered from this
                # slot; `spans=()` says *span provenance applies and covers nothing*,
                # which is different from `None` (profile recovers no spans).
                out[f.path] = Prediction(
                    False, "", f"the file does not exist; the loader injects "
                    f"{missing_text!r} in its place and charges "
                    f"{u16len(capped)} units of the shared budget to it",
                    self.UNMODELED, cause=Cause.NOT_IN_LOAD_SET, spans=(),
                    injected=capped)
                continue

            if remaining < MIN_BOOTSTRAP_FILE_BUDGET_CHARS:
                out[f.path] = Prediction(
                    False, "", f"not loaded: only {remaining} characters of shared "
                    f"budget remain, below the {MIN_BOOTSTRAP_FILE_BUDGET_CHARS}-"
                    "character floor, so this and every later file is skipped",
                    self.UNMODELED, cause=Cause.OMITTED_BY_BUDGET)
                exhausted = True
                continue

            file_budget = (min(self.max_chars, USER_BOOTSTRAP_MAX_CHARS)
                           if (f.name or "").lower() == "user.md" else self.max_chars)
            file_max = max(1, min(file_budget, remaining))
            trimmed, spans = _trim_bootstrap(f.view, f.name, file_max)
            within = clamp_to_budget(trimmed, remaining)
            if not within:
                out[f.path] = Prediction(False, "", "not loaded: no budget remained",
                                         self.UNMODELED, cause=Cause.OMITTED_BY_BUDGET)
                continue
            if u16len(within) < u16len(trimmed):
                # The shared budget clipped this file's own rendering; the cut lands
                # at the end of the delivered text, so the last span shrinks.
                spans = _clip_tail(list(spans), u16len(trimmed) - u16len(within))
            remaining = max(0, remaining - u16len(within))
            # `budget_unknown` is non-empty only after an unreadable file charged the
            # shared budget by an amount this profile cannot derive. A1 then forbids a
            # definite status here, however exact this file's own arithmetic looks:
            # the budget it was computed against is a guess.
            out[f.path] = Prediction(True, within, "",
                                     self.UNMODELED + budget_unknown, spans=spans)
        return out


def bundle_version(openclaw_path: str | Path) -> str:
    import json
    return json.loads((Path(openclaw_path) / "package.json").read_text())["version"]
