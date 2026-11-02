"""Verdict resolution.

A1 is the governing rule here: *a profile may not print a definite status past a
step it does not model.* So a profile declares its unmodeled steps, and any file
those steps could touch yields `UNKNOWN` blocks regardless of what the size
arithmetic says.

A2 governs the load set: a file absent from the load set reports
`NOT_DELIVERED_UNDER_PROFILE` with a stated reason, per file, before any
block-level detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Delivery states.

    Brief v0.6 names the success state `DELIVERED_ON_LOAD` (§10.6, §15) rather
    than `DELIVERED`, because §15 forbids language that reads as a compliance
    claim: the statement is *delivered on initial load under profile X, scenario
    Y*, never *guaranteed to reach the model*. `DELIVERED` is kept as an Enum
    alias — the same member, so `Status.DELIVERED is Status.DELIVERED_ON_LOAD` —
    so the v0.5-era call sites keep working while the wire value is the v0.6 one.

    `CONDITIONAL` is §6: a parametric profile whose endpoint outcomes differ over
    the verified admissible parameter range. It is **not** a definite delivery
    state, and a conditional critical result is reported with its transition
    threshold rather than collapsed to either side.
    """

    DELIVERED_ON_LOAD = "DELIVERED_ON_LOAD"
    DELIVERED = "DELIVERED_ON_LOAD"          # v0.5 alias, same member
    PARTIAL = "PARTIAL"
    NOT_DELIVERED = "NOT_DELIVERED"
    AVAILABLE_ONLY_ON_REQUEST = "AVAILABLE_ONLY_ON_REQUEST"
    CONDITIONAL = "CONDITIONAL"
    UNKNOWN = "UNKNOWN"

    @property
    def definite(self) -> bool:
        """A definite status is one §16 counts when measuring false statuses.

        `CONDITIONAL` is excluded: §6 only permits it where a definite result
        could not be established, so counting it as definite would let a
        profile launder an undetermined outcome into the zero-false-definite
        figure.
        """
        return self not in (Status.UNKNOWN, Status.CONDITIONAL)


class Cause(str, Enum):
    """*Why* a block was not delivered on load.

    Status says what happened to the block; cause says which mechanism did it.
    Keeping them apart is what stops the status set from growing a new member
    every time a harness invents another way to drop text — a BOM rejection is
    `NOT_DELIVERED` + `REJECTED_BY_LOADER`, not a new status.
    """

    NONE = "NONE"
    TRUNCATED = "TRUNCATED"                    # size cut on this unit
    OMITTED_BY_BUDGET = "OMITTED_BY_BUDGET"    # a shared budget ran out
    FILTERED = "FILTERED"                      # selected against, e.g. a digest
    REJECTED_BY_LOADER = "REJECTED_BY_LOADER"  # the loader refused the source
    NOT_IN_LOAD_SET = "NOT_IN_LOAD_SET"        # never a candidate under this profile
    ON_REQUEST = "ON_REQUEST"                  # reachable by a proven retrieval path
    #: The only route that carried this text reshaped it on the way out, so no
    #: source-preserving route delivered it. Owner ruling 2026-10-02 (OpenClaw
    #: source pointers), item 3: v1 does not judge whether a rewritten line still
    #: means the same thing, so a digest-only block is reported `PARTIAL` with this
    #: cause and fails closed rather than being called delivered.
    TRANSFORMED_BY_DIGEST = "TRANSFORMED_BY_DIGEST"


@dataclass
class BlockVerdict:
    """A verdict about one authored block, in **file** coordinates.

    `line`/`end_line` and `start`/`end` are offsets into the file on disk, not
    into whatever slice the loader happened to receive, so a verdict can be
    checked against the file a person edits.
    """

    index: int
    kind: str
    line: int
    label: str
    status: Status
    reason: str = ""
    end_line: int = 0
    start: int = 0
    end: int = 0
    # Additive diagnostics: which lines inside this block are absent from the
    # delivered text. Never changes `status` — a block's status is decided for
    # the block as a whole, and the block unit is Brief v0.5's to define. This
    # exists because a clause in the middle of an unseparated paragraph is
    # otherwise reported under the paragraph's first line and never named.
    lost_lines: list[tuple[int, int]] = field(default_factory=list)
    lost_excerpt: str = ""
    cause: "Cause" = None  # type: ignore[assignment]
    #: Set when a caller-supplied `--critical` pattern matched the **canonical
    #: full source-block text**. Never the rendered label, never `lost_excerpt`,
    #: never a truncated preview: letting a display rule decide CI would mean
    #: the UI silently changes build outcomes.
    critical: bool = False
    # Deterministic retrieval paths, each one proven by enumeration rather than
    # inferred. Empty unless `status` is AVAILABLE_ONLY_ON_REQUEST.
    retrieval: list[str] = field(default_factory=list)
    #: Every delivered route covering this block, named, most authoritative first.
    #: Diagnostic: the status is decided by `resolve_block_spans`, but a block
    #: delivered in the head *and* repeated in the policy digest has two routes and
    #: hiding the second one would make the report disagree with the loader's
    #: output. Empty for profiles that do not recover spans.
    routes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.cause is None:
            self.cause = (Cause.NONE if self.status is Status.DELIVERED
                          else Cause.TRUNCATED)

    @property
    def locator(self) -> str:
        return f"{self.line}" if self.end_line <= self.line else f"{self.line}-{self.end_line}"


@dataclass
class FileVerdict:
    """Per-file result. `load_set_reason` is set only when the file is not loaded."""

    path: str
    name: str
    loaded: bool
    delivered_chars: int
    raw_chars: int
    blocks: list[BlockVerdict] = field(default_factory=list)
    load_set_reason: str = ""
    unmodeled: tuple[str, ...] = ()

    @property
    def status_line(self) -> str:
        if not self.loaded:
            return f"NOT DELIVERED UNDER PROFILE — {self.load_set_reason}"
        return f"{self.delivered_chars} of {self.raw_chars} delivered"


@dataclass(frozen=True)
class DeliveredSpan:
    """A range of the **source file** that the loader's own output accounts for.

    Pre-merge ruling item 3: *replace containment-based block membership with
    source-span/provenance-backed membership*. Containment asks "does this text
    appear somewhere in the delivered blob", which within-file repetition can
    answer yes to for a line that was in fact dropped — the weakness
    `fieldtest/VERIFIER_PROVENANCE.md` records. A span answers "which part of the
    file produced this delivered text", which repetition cannot forge.

    `verbatim` is False when the loader reshaped the text on the way out —
    OpenClaw's policy digest collapses whitespace and may truncate a line to 240
    units. For those, `delivered_text` carries what actually arrived, and
    membership is decided inside that one line's rendering rather than across the
    whole blob, so the provenance is still pinned to a source location.

    **Routes, and what each can establish.** Owner ruling 2026-10-02 splits spans
    into two routes and gives them different authority:

    * a **source-preserving** route (`verbatim`) hands the loader's output back
      byte-for-byte from the named source range, so it can support
      `DELIVERY_ESTABLISHED`;
    * a **transformed** route (the policy digest) re-selects lines, collapses
      whitespace, cuts at 240 UTF-16 units, and may emit the same source line
      twice. It cannot support `DELIVERY_ESTABLISHED` — not because the text is
      necessarily wrong, but because deciding that a rewritten line still carries
      the constraint is a semantic judgement v1 does not make.

    A block with both routes is established by the source-preserving one. The
    digest copy is an additional route and never the deciding one, which is what
    stops a substring coincidence inside a digest line from claiming the credit.
    """

    start: int
    end: int
    kind: str                  # head | tail | digest | whole
    verbatim: bool = True
    delivered_text: str = ""

    #: Span kinds whose delivered text is a byte-for-byte slice of the source.
    SOURCE_PRESERVING_KINDS = ("whole", "head", "tail")

    @property
    def source_preserving(self) -> bool:
        """Can this span's route establish delivery on its own?

        Both halves are required: a `kind` outside the known source-preserving set
        is treated as transformed even if some profile set `verbatim=True` on it, so
        adding a new reshaping route cannot silently inherit establishing authority.
        """
        return self.verbatim and self.kind in self.SOURCE_PRESERVING_KINDS

    @property
    def route(self) -> str:
        return "source-preserving" if self.source_preserving else "transformed"

    def covers(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end

    def overlaps(self, start: int, end: int) -> bool:
        return start < self.end and self.start < end


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def resolve_block_spans(block_text: str, start: int, end: int,
                        spans: "tuple[DeliveredSpan, ...]", *, unmodeled: bool
                        ) -> tuple[Status, str, Cause]:
    """Status, reason and cause of one block from delivered **source spans**.

    Order of questions matters and is pessimistic:

    1. an unmodeled step forbids a definite answer at all (A1);
    2. a **source-preserving** span that covers the block whole -> delivered;
    3. a **transformed** span (the policy digest) that covers it -> `PARTIAL` with
       `TRANSFORMED_BY_DIGEST`, whether or not its rendering happens to contain the
       block's text;
    4. any overlap at all -> partial;
    5. otherwise -> not delivered.

    Rule 3 is the owner ruling of 2026-10-02 and it **replaced** a containment
    test. The previous rule 3 asked whether the digest line's rendering contained
    the block's whitespace-normalised text and returned `DELIVERED` when it did.
    Three properties of the real digest make that unsafe: it re-selects lines from
    the whole file so the same source line can be emitted in both the head and the
    digest, it collapses whitespace, and it truncates at 240 UTF-16 units. A
    containment hit inside a transformed rendering is therefore not evidence that
    this source range was delivered intact — and v1 does not rule on whether a
    reshaped line still carries the constraint. So the digest route reports
    `PARTIAL` and fails closed.

    The cause is returned rather than inferred by the caller because the caller
    cannot see which route decided it, and a `PARTIAL` whose mechanism is "the
    loader rewrote this line" needs a different response from one whose mechanism
    is "the cut landed mid-block".
    """
    if unmodeled:
        return (Status.UNKNOWN,
                "an unmodeled step on this load path could change delivery",
                Cause.NONE)
    needle = block_text.strip()
    if not needle:
        return Status.DELIVERED, "empty", Cause.NONE

    # CF-014: compare the block's *content* extent, not its raw extent. Loaders
    # `.strip()` the text they deliver, so a delivered span legitimately ends at the
    # stripped length while the block runs to the end of the file. Holdout H1 caught
    # this on a `.cursorrules` ending in a trailing space and no newline: the span
    # was [0, 318) and the final block [.., 319), so a fully delivered clause came
    # back PARTIAL. Whitespace at a block's edges is not content, and the loader
    # removing it is not partial delivery.
    #
    # `fieldtest/VERIFIER_PROVENANCE.md` records the same shape being clamped in the
    # field-test *verifier*; the span layer added for CF-012 reintroduced it on the
    # product side, where no clamp existed yet.
    lead = len(block_text) - len(block_text.lstrip())
    trail = len(block_text) - len(block_text.rstrip())
    start, end = start + lead, max(start + lead, end - trail)

    covering = [s for s in spans if s.covers(start, end)]

    # The source-preserving route is asked first and wins outright. A block with
    # both routes is established by this one; the transformed copy is recorded as an
    # additional route below but never decides.
    for s in covering:
        if s.source_preserving:
            return (Status.DELIVERED, f"inside the delivered {s.kind} span",
                    Cause.NONE)

    transformed = [s for s in covering if not s.source_preserving]
    if transformed:
        s = transformed[0]
        carried = (_normalized(needle) in s.delivered_text) if s.delivered_text else False
        detail = ("its rendering does carry the text, but a transformed route cannot "
                  "establish delivery on its own"
                  if carried else
                  "its rendering does not carry the whole of the block")
        return (Status.PARTIAL,
                f"the only route covering this block is the {s.kind}, which "
                f"re-selects lines, collapses whitespace and cuts at 240 UTF-16 "
                f"units; {detail}",
                Cause.TRANSFORMED_BY_DIGEST)

    touching = [s for s in spans if s.overlaps(start, end)]
    if touching:
        preserving = [s for s in touching if s.source_preserving]
        if not preserving:
            s = touching[0]
            return (Status.PARTIAL,
                    f"only the {s.kind} route touches this block, and a transformed "
                    f"route cannot establish delivery",
                    Cause.TRANSFORMED_BY_DIGEST)
        kept = sum(min(end, s.end) - max(start, s.start) for s in preserving)
        return (Status.PARTIAL,
                f"{kept} of {end - start} characters fall inside a delivered span",
                Cause.TRUNCATED)
    return Status.NOT_DELIVERED, "no delivered span covers this block", Cause.TRUNCATED


def routes_for(start: int, end: int, spans: "tuple[DeliveredSpan, ...]") -> list[str]:
    """Every route that covers `[start, end)`, named, most authoritative first.

    Diagnostics only — `resolve_block_spans` decides the status. This exists so a
    report can say *delivered in the head, and also present in the policy digest*
    rather than hiding the second route: the owner ruling keeps the digest copy as
    an additional route and only forbids it from being the deciding one.
    """
    covering = [s for s in spans if s.covers(start, end)]
    covering.sort(key=lambda s: 0 if s.source_preserving else 1)
    return [f"{s.kind} ({s.route})" for s in covering]


def resolve_block(block_text: str, delivered: str, *, unmodeled: bool) -> tuple[Status, str]:
    """Status of one block against the profile's predicted delivered text.

    Containment is the test, not offset arithmetic: finding 5 in STATUS.md showed
    the capped unit differs per harness (JSON envelope, labelled section, raw
    bytes with separators applied afterwards), so a shared offset computation
    would be wrong somewhere. Each profile hands over the text it predicts will
    be delivered, and membership is decided in that text.
    """
    if unmodeled:
        return Status.UNKNOWN, "an unmodeled step on this load path could change delivery"

    needle = block_text.strip()
    if not needle:
        return Status.DELIVERED, "empty"
    if needle in delivered:
        return Status.DELIVERED, ""

    # A block may straddle the boundary. Report PARTIAL only on a real prefix or
    # suffix overlap, so that PARTIAL never stands in for "not delivered".
    longest = 0
    for cut in range(len(needle) - 1, 0, -1):
        if needle[:cut] in delivered or needle[-cut:] in delivered:
            longest = cut
            break
    if longest >= max(8, len(needle) // 10):
        return Status.PARTIAL, f"{longest} of {len(needle)} characters survive the boundary"
    return Status.NOT_DELIVERED, "absent from the delivered text"
