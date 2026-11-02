"""Scan: per-profile block verdicts for a load set.

A3 is enforced structurally: results are keyed by profile, counts are per
profile, and there is no aggregate across profiles in either the object model or
the JSON rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .blocks import segment
from .profiles import InputFile, Profile, block_needle
from .verdict import (BlockVerdict, Cause, FileVerdict, Status, resolve_block,
                      resolve_block_spans, routes_for)


def _lost_lines(block, delivered: str, needle_of) -> tuple[list[tuple[int, int]], str]:
    """Line ranges inside *block* whose text is absent from *delivered*.

    Purely diagnostic. Blank lines and lines shorter than 4 characters are
    skipped: they are near-certain to appear somewhere in the delivered text by
    coincidence, which would understate the loss.
    """
    ranges: list[tuple[int, int]] = []
    excerpt = ""
    for i, raw in enumerate(block.text.splitlines()):
        line = raw.strip()
        if len(line) < 4:
            continue
        if needle_of(line) in delivered:
            continue
        n = block.line + i
        if ranges and ranges[-1][1] == n - 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
        if not excerpt:
            excerpt = line if len(line) <= 160 else line[:159] + "\u2026"
    return ranges, excerpt


@dataclass
class ProfileResult:
    profile: str
    verified_against: str
    files: list[FileVerdict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in Status}
        for f in self.files:
            for b in f.blocks:
                c[b.status.value] += 1
        return c


def scan_under_profile(files: list[InputFile], profile: Profile,
                       critical_patterns: "list[re.Pattern[str]] | None" = None
                       ) -> ProfileResult:
    """Verdicts for one load set under one profile.

    `critical_patterns` are matched against each block's canonical full source
    text, here, where that text exists — not later against whatever the
    renderer decided to show.
    """
    patterns = critical_patterns or []
    predictions = profile.predict(files)
    result = ProfileResult(profile.name, profile.verified_against)

    for f in files:
        pred = predictions[f.path]
        fv = FileVerdict(
            path=f.path,
            name=f.name,
            loaded=pred.loaded,
            delivered_chars=len(pred.delivered),
            raw_chars=len(f.file_text),
            load_set_reason=pred.load_set_reason,
            unmodeled=pred.unmodeled,
        )
        for block in segment(f.file_text):
            if not pred.loaded:
                # A2: the reason is the file-level statement; blocks inherit it.
                fv.blocks.append(BlockVerdict(
                    block.index, block.kind, block.line, block.label,
                    Status.NOT_DELIVERED, pred.load_set_reason,
                    block.end_line, block.start, block.end,
                    cause=Cause.NOT_IN_LOAD_SET,
                    critical=any(p.search(block.text) for p in patterns)))
                continue
            needle = block_needle(profile, block.text)
            routes: list[str] = []
            if pred.spans is not None:
                # Pre-merge ruling item 3: where the profile recovers source
                # spans, membership is decided from them. Containment is the
                # fallback for profiles that do not yet, and the qualification
                # gate reports that as open rather than letting it pass quietly.
                status, reason, span_cause = resolve_block_spans(
                    block.text, block.start, block.end, pred.spans,
                    unmodeled=bool(pred.unmodeled))
                routes = routes_for(block.start, block.end, pred.spans)
            else:
                status, reason = resolve_block(needle, pred.delivered,
                                               unmodeled=bool(pred.unmodeled))
                span_cause = Cause.NONE
            if status is Status.DELIVERED:
                cause = Cause.NONE
            elif span_cause is Cause.TRANSFORMED_BY_DIGEST:
                # The route's own mechanism outranks the file-level one: the file
                # was indeed truncated, but what happened to *this block* is that
                # the only route carrying it rewrote it, and that calls for a
                # different fix from "the cut landed here".
                cause = span_cause
            else:
                cause = pred.cause
            retrieval: list[str] = []

            # A block missing from the on-load text is only AVAILABLE ONLY ON
            # REQUEST when the profile can enumerate a call that returns it.
            # An empty list means the block stays NOT_DELIVERED: "there might be
            # another way to read it" is not a retrieval path.
            if status is Status.NOT_DELIVERED and hasattr(profile, "retrieval_paths"):
                retrieval = profile.retrieval_paths(f, block.text.strip())
                if retrieval:
                    status = Status.AVAILABLE_ONLY_ON_REQUEST
                    cause = Cause.ON_REQUEST
                    reason = "not in the first load; reachable by " + retrieval[0]

            bv = BlockVerdict(
                block.index, block.kind, block.line, block.label, status, reason,
                block.end_line, block.start, block.end, cause=cause,
                retrieval=retrieval, routes=routes,
                critical=any(p.search(block.text) for p in patterns))
            if status in (Status.PARTIAL, Status.NOT_DELIVERED) and block.end_line > block.line:
                bv.lost_lines, bv.lost_excerpt = _lost_lines(
                    block, pred.delivered, lambda t: block_needle(profile, t))
            fv.blocks.append(bv)
        result.files.append(fv)
    return result


def render(results: list[ProfileResult]) -> str:
    """Text rendering. Per A3, states the framing once and never totals across
    profiles."""
    lines: list[str] = []
    if len(results) > 1:
        lines += ["These are evaluations under each profile listed below, not a "
                  "statement about which harness this repository uses. Counts are "
                  "per profile and are not summed across profiles.", ""]
    for r in results:
        lines += [f"## Profile {r.profile}", f"verified against {r.verified_against}", ""]
        for f in r.files:
            lines.append(f"### {f.name} — {f.status_line}")
            if f.unmodeled:
                for step in f.unmodeled:
                    lines.append(f"  unmodeled step: {step}")
            for b in f.blocks:
                if b.status is Status.DELIVERED:
                    continue
                suffix = f" — {b.reason}" if b.reason else ""
                tag = b.status.value if b.cause in (Cause.NONE, Cause.TRUNCATED) \
                    else f"{b.status.value}/{b.cause.value}"
                lines.append(f"  {tag:<30} {f.name}:{b.locator}: {b.label}{suffix}")
                for path in b.retrieval[:2]:
                    lines.append(f"      reachable by: {path}")
                if b.lost_excerpt and b.lost_excerpt not in b.label:
                    spans = ", ".join(f"{a}" if a == z else f"{a}-{z}"
                                      for a, z in b.lost_lines[:4])
                    lines.append(f"      lost lines {spans}: {b.lost_excerpt}")
            lines.append("")
        c = r.counts()
        lines += [f"blocks under {r.profile}: " +
                  ", ".join(f"{k} {v}" for k, v in c.items() if v), ""]
    return "\n".join(lines)


def to_json(results: list[ProfileResult]) -> dict:
    return {
        "note": ("Evaluations under each profile. Not a statement about which "
                 "harness this repository uses. No totals across profiles."),
        "profiles": [
            {
                "profile": r.profile,
                "verified_against": r.verified_against,
                "counts": r.counts(),
                "files": [
                    {
                        "path": f.path,
                        "name": f.name,
                        "loaded": f.loaded,
                        "load_set_reason": f.load_set_reason,
                        "raw_chars": f.raw_chars,
                        "delivered_chars": f.delivered_chars,
                        "unmodeled": list(f.unmodeled),
                        "blocks": [
                            {"index": b.index, "kind": b.kind,
                             "line": b.line, "end_line": b.end_line,
                             "char_start": b.start, "char_end": b.end,
                             "label": b.label, "status": b.status.value,
                             "reason": b.reason, "cause": b.cause.value,
                             "retrieval": b.retrieval,
                             "lost_lines": [list(r) for r in b.lost_lines],
                             "lost_excerpt": b.lost_excerpt,
                             "critical": b.critical}
                            for b in f.blocks
                        ],
                    }
                    for f in r.files
                ],
            }
            for r in results
        ],
    }
