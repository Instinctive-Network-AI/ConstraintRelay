"""Phase 1 layer B — candidate constraint extraction.

The brief's one hard rule for this layer: *"heuristic extraction must not
silently promote text into authoritative constraints."* So this module produces
`Candidate` records and nothing else. It cannot write the store, it does not
import `skillstore`, and a candidate becomes a constraint only when
`cli_phase1.cmd_confirm` is given an explicit confirmation.

What is scanned
---------------
Every `SourceFile` of a skill, segmented by `blocks.segment` — the same frozen
block semantics the rest of the system uses, so a candidate's span means the
same thing here as in a `scan` verdict. Four regions are excluded, each for a
reason that showed up in the real corpus:

* **YAML frontmatter.** `description:` is a one-line summary written for the
  harness's skill menu. Several read like constraints (*"Trade execution
  modeling (backtest only)"*) and none of them *are* the constraint — the
  operative sentence is further down the document. Importing the summary would
  put a paraphrase in the store with the authority of a quote.
* **Language-tagged fenced code.** 70 of the 442 signal-bearing blocks in the
  Vibe-Trading corpus sit inside ``` fences: Python `assert` messages,
  `if not ...: raise`, and SQL. They are implementation, not instruction.
  **A fence with no info string is not**: see "Prose fences", below.
* **Markdown table header and delimiter rows.** A column label is not a rule.
  Data rows are *not* excluded: see "Tables", below.
* **Emphasis-only labels.** `**Must comply with**` on its own line is a section
  label; the constraints are the list items under it, and each of those is
  already its own block.

Each exclusion is counted and reported, so the report can say what was *not*
looked at rather than leaving it implied.

Tables
------
`blocks.segment` has no notion of tables, so it joins a whole table into one
paragraph block. This layer used to exclude such a block outright, on the
reasoning that a 40-row table matching *must* once is not a constraint. That
was right about the block and wrong about the table: the Phase 7 gold set found
**87 Class C constraints written as table rows, none of them reachable** — 20%
of all the constraints in the sample, at 0% recall.

So a table block is split into its data rows here, and each row is matched on
its own. The split is local to this module: `blocks.segment` keeps its frozen
semantics, because the marker and verdict layers resolve verdicts by its block
indices and a second segmentation that disagreed by one block would attribute a
verdict to the wrong constraint. A row candidate therefore carries a span
*inside* a block rather than a block's own span, which is exactly the
granularity a reader needs — the rule is the row, not the table.

Measured on the frozen gold set: table recall 0% → 19.5%, overall recall
35.1% → 39.0%, for 1.9 points of precision. The rows that stay out of reach are
threshold rows carrying a bound but no modal verb (*营收 < 3 亿*). A pattern for
those was measured and **declined**: it lifted table recall to 54% but cost 9
points of precision against this row-level baseline (85.1% → 76.1%, overall
recall 39.0% → 45.8%), by importing descriptive reference tables — SEC form
glossaries, signal-interpretation tables — whose numbers belong to a filing
rule or a market, not to the agent. `fieldtest/phase7/probe_table.json` records
both variants.

Prose fences (Phase 10)
-----------------------
The blanket fence exclusion was measured as a precision win in Phase 7a and
left alone, on the reasoning that a fence holds implementation. The retired
Phase 8 set shows where that stops: **43 of its random stratum's 157 Class C
constraints are fenced, and 25 of those are in bare or ``markdown`` fences** —
numbered screening criteria, checklists, output templates. Prose in a monospace
box.

The **info string** is the mechanism. ``` ```python ``` announces a body written
for an interpreter; a bare ``` ``` ``` announces nothing. So a fence whose info
string is in `PROSE_FENCE_INFO` becomes one candidate — the whole fence,
delimiters included, which is the unit both gold sets annotate — and the
existing pattern layer runs over it unchanged. Language-tagged fences stay
excluded, so Phase 7a's measured win still covers the case it was measured on.

Measured on the two retired sets: dev random stratum 31.3% → 34.1% recall for
2.4 points of precision; retired H-A 22.3% → 28.0% recall for **+0.4** points.
Corpus-wide 1,019 → 1,116 candidates, and the unresolved-section signal falls
from 16 to 10. `PHASE10.md` §3 and `fieldtest/phase10/probe.json`.

The *table* mechanism measured in the same phase — classify a table by whether
one of its columns is systematically a condition, then admit the rows that
participate — is implemented here and **ships switched off**, having cost far
more precision than the pre-registered bar allowed. `TABLE_ROLE_CLASSIFICATION`
carries the numbers and the reason.

Tiers
-----
Each candidate carries `tier`:

* `operative` — the text states an obligation, prohibition or restriction:
  must / must not / never / only / forbidden / required, and the Chinese
  equivalents 必须 / 禁止 / 不得 / 严禁 / 只能 / 仅限.
* `advisory` — the text recommends: should / avoid / ensure / always /
  recommended / 不要 / 应当 / 建议.
* `author_marker` — an explicit `<!-- constraintflow: critical id=... -->`.
  This is not a heuristic at all; it is the author having already confirmed, and
  it is the only tier that arrives with an id of its own.

The tier is one field and it exists because batch confirmation is the expected
Phase-1 path: a reviewer who wants the obligations without the advice should not
have to re-read 372 blocks to find them.

Unresolved extraction cases
---------------------------
A heading whose title is constraint-shaped (*Constraints*, *Limitations*,
*Risk*, *注意事项*, …) and whose section produced **no** candidate is recorded
as an unresolved case. That is the honest shape of this layer's recall gap: a
constraint written without a modal verb — *"Backtest only."* — is invisible to
every pattern here, and the section heading is the evidence that one was
probably meant.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .blocks import MARKER_LINE, segment
from .inventory import Inventory, Skill, SourceFile
from .markers import MarkerIssue, scan_file

OPERATIVE = "operative"
ADVISORY = "advisory"
AUTHOR_MARKER = "author_marker"
TIERS = (AUTHOR_MARKER, OPERATIVE, ADVISORY)

#: Ordered so a block's reasons read in a stable order regardless of where in
#: the text each pattern happened to match.
_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (OPERATIVE, "prohibition",
     r"\b(?:must|may|shall|can|will)\s+(?:not|never)\b|\bmust\s?n[o’']t\b"
     r"|\bcannot\b|\bcan\s?not\b|\bdo\s+not\b|\bdon\s?[’']?t\b|\bnever\b"
     r"|\bforbidden\b|\bprohibit(?:ed|s)?\b|\bdisallow(?:ed|s)?\b"
     r"|\bnot\s+permitted\b|\bnot\s+allowed\b"),
    (OPERATIVE, "obligation",
     r"\bmust\b|\bshall\b|\bis\s+required\b|\bare\s+required\b|\brequired\s+to\b"
     r"|\bmandatory\b|\bhas\s+to\b|\bhave\s+to\b"),
    (OPERATIVE, "restriction",
     r"\bonly\b|\bsolely\b|\bexclusively\b|\bnot\s+for\b|\blimited\s+to\b"
     r"|\bat\s+most\b|\bno\s+more\s+than\b|\bdo\s+not\s+exceed\b"),
    (OPERATIVE, "zh_prohibition", r"禁止|严禁|不得|切勿|不可|请勿|严格禁止"),
    (OPERATIVE, "zh_obligation", r"必须|必需"),
    (OPERATIVE, "zh_restriction", r"只能|仅限|仅可|仅用于|不适用于"),
    (ADVISORY, "recommendation",
     r"\bshould\b|\bought\s+to\b|\brecommend(?:ed|s)?\b|\bprefer(?:red)?\b"
     r"|\bavoid\b|\bensure\b|\balways\b|\bbe\s+careful\b|\bbeware\b"),
    (ADVISORY, "zh_recommendation", r"不要|应当|应该|建议|注意|谨慎"),

    # --- added in Phase 7, each on its own measured delta ---------------------
    # Every pattern below was scored individually against the frozen gold set in
    # `fieldtest/phase7/` and kept only if it added at least 1 point of recall
    # for no more than 3 points of precision. `ablation.json` records the
    # decision for each, including the three that were measured and declined.

    (OPERATIVE, "obligation_zh_xu", r"(?<!如)需(?![求点])|须(?!要)"),
    (OPERATIVE, "prohibition_zh_bare",
     r"(?m)^[ \t]*(?:\d+[.)]|[-*+])?[ \t]*(?:\*\*|>[ \t]*)*不(?!同|一致|足)"
     r"|不允许|不能|不应|不适合|不适用|无法"),
    (OPERATIVE, "restriction_zh_bare",
     r"只(?:取|用|看|给|计|有)|仅(?![限可]|用于)|至少|最多|不超过|最高"),
    (OPERATIVE, "requirement",
     r"\brequires?\b|\brequired\b|\bneeds?\s+to\b"),
    (OPERATIVE, "quantified_bound",
     r"\bat\s+least\b|\bmax(?:imum)?\b|\bmin(?:imum)?\b|\bup\s+to\b"
     r"|\bno\s+fewer\s+than\b"),
    (OPERATIVE, "negated_obligation",
     r"\bdoes\s+not\b|\bdoesn\s?[’']?t\b|\bcan\s?[’']?t\b|\bwon\s?[’']?t\b"
     r"|\bbanned\b|\bnot\s+suitable\b"),
    (OPERATIVE, "negated_noun",
     r"\bno\s+(?:[a-z]+[- ])?[a-z]+(?:ing|s)?\b"),
    (ADVISORY, "zh_recommendation_avoid", r"避免|尽量|优先|推荐|慎"),
)

_COMPILED = tuple((tier, name, re.compile(pat, re.IGNORECASE))
                  for tier, name, pat in _PATTERNS)

#: A comparison bound: an operator beside a number, a numeric range, or a
#: percentage. Deliberately about *shape* and not about any field name, so it
#: carries no knowledge of this corpus.
_BOUND = re.compile(
    r"(?:[<>≥≤]=?|>=|<=|±)\s*-?\d"
    r"|\d\s*(?:-|~|–|—|to|至|到)\s*\d"
    r"|\d+(?:\.\d+)?\s*%")

#: Fence info strings whose contents are prose rather than implementation.
#: Empty means a bare ``` fence. See `_prose_fences` for why this list is the
#: mechanism and not a pattern.
PROSE_FENCE_INFO = frozenset({"", "text", "txt", "plain", "md", "markdown",
                              "yaml", "yml"})

#: Phase 10 switches. Each is a *mechanism*, and `fieldtest/phase10/pinned.py`
#: reconstructs the pre-Phase-10 baseline by setting them to these defaults'
#: opposites — never by hoping the working tree is old, which is the standing
#: lesson from `fieldtest/phase7/pinned.py`.
#:
#: `TABLE_ROLE_CLASSIFICATION` decides whether a table's rows are read in the
#: context of its header. `PROSE_FENCE_CANDIDATES` decides whether a bare or
#: prose-tagged fence is looked at at all.
#:
#: **The table mechanism ships OFF: measured and declined.** It is the fix Phase
#: 7 §5 said was needed, it works — it finds the thresholds — and it costs far
#: more precision than the pre-registered bar allows, on both retired sets and
#: in every tightening measured (`fieldtest/phase10/probe.json`):
#:
#: | variant | dev-A ΔP / ΔR | retired H-A ΔP / ΔR |
#: |---|---|---|
#: | every row of a decision table | −22.8 / +12.8 | −11.8 / +19.7 |
#: | only rows in the condition column | −19.0 / +10.6 | −8.4 / +15.9 |
#: | …and at most 6 rows per table | −14.2 / +10.6 | −5.0 / +11.5 |
#:
#: The bar is −3.0. The code stays, switched off and reachable through
#: `fieldtest/phase10/pinned.py`, because the bar encodes a product stance — a
#: confirmation-gated contract may rationally prefer +15.9 recall at −8.4
#: precision — and that is the Owner's call, recorded as the open question in
#: `PHASE10.md` §6. Deleting it would mean re-deriving the measurement to ask.
TABLE_ROLE_CLASSIFICATION = False
PROSE_FENCE_CANDIDATES = True

ROLE_REFERENCE = "reference"
ROLE_NUMERIC = "decision_numeric"
ROLE_DIRECTIVE = "decision_directive"

#: Which roles admit a row that carries no constraint language of its own.
#: Separate from the classification switch because "can this table be
#: recognised" and "does recognising it admit its rows" are two decisions, and
#: the probes have to be able to answer them one at a time.
TABLE_DECISION_ROLES = frozenset({ROLE_NUMERIC, ROLE_DIRECTIVE})

#: Admit a prose fence that carries two or more bounds and no constraint
#: language — a screening checklist written as a list of numbers.
#:
#: **Measured and declined**: −9.9 precision on the development stratum for
#: +1.1 further recall over `PROSE_FENCE_CANDIDATES` alone. Two numbers in a box
#: turns out to describe a worked example as often as a screening rule.
FENCE_BOUND_ADMISSION = False

#: **Experimental mechanisms, and the value each must carry in a shipped
#: extractor.** Owner ruling 2026-10-07, Decision 1: *"Keep the precommitted
#: development bar intact … Keep the implementation and evidence available
#: behind a disabled experimental switch, but the shipped extractor remains the
#: mechanism set that actually passed the frozen protocol."*
#:
#: A comment saying "declined, off by default" is a comment. This is the same
#: statement as a value a test compares against, so flipping a default to run a
#: probe and forgetting to flip it back fails
#: `tests/test_release_readiness.py` instead of shipping quietly.
#: `fieldtest/phase10/pinned.py` is how a probe turns one on, scoped to a
#: context manager that restores it.
EXPERIMENTAL_SWITCHES: dict[str, object] = {
    # Phase 10, declined on precision cost.
    "TABLE_ROLE_CLASSIFICATION": False,
    "FENCE_BOUND_ADMISSION": False,
    # Phase 11, declined under fieldtest/phase11/DEV_PROTOCOL_ADDENDUM.md §1.
    # Each raises precision on ONE of the two retired strata and is flat on the
    # other, because the error family it targets occurs in only one of them —
    # so none clears a bar that asks for a rise on both.
    "CODE_HEAD_IS_DOCUMENTATION": False,
    "CODE_CELL_ROWS_ARE_DOCUMENTATION": False,
    "BOUND_NEEDS_QUANTITY": False,
    "PROSE_FENCE_NEEDS_ENUMERATION": False,
    "SUBJECT_ATTRIBUTION": "off",
    # Ships OFF. **Owner ruling 2026-10-08: DO NOT ADOPT, and no one-time
    # exception.** It is the one mechanism that worsens no measured figure on
    # either retired set — recall unchanged on all six — and that was not
    # enough: support is too sparse at four corpus hits, with no meaningful
    # retired-set support for the family it targets (0 of 16 adjudicated errors
    # there). The implementation and its evidence are preserved as disabled
    # experimental work. fieldtest/phase11/DEV_PROTOCOL_ADDENDUM.md §6.
    "QUOTED_BLOCKQUOTE_IS_QUOTATION": False,
}


def assert_experimental_disabled() -> None:
    """Raise if any experimental mechanism is not at its shipped value."""
    here = globals()
    missing = [n for n in EXPERIMENTAL_SWITCHES if n not in here]
    if missing:
        raise RuntimeError(
            f"EXPERIMENTAL_SWITCHES names {missing!r}, which this module does "
            "not define. A registry that lists a switch nobody declares cannot "
            "report on it — rename it here, or delete the entry and say so in "
            "PHASE10.md.")
    wrong = {n: here[n] for n, shipped in EXPERIMENTAL_SWITCHES.items()
             if here[n] != shipped}
    if wrong:
        raise RuntimeError(
            f"experimental extractor mechanism(s) are enabled: {wrong}. Each "
            "was measured and DECLINED under "
            "fieldtest/phase10/DEV_PROTOCOL.md §5, and a shipped extractor "
            f"must carry {EXPERIMENTAL_SWITCHES!r}. To measure one, use "
            "fieldtest/phase10/pinned.py's `variant()`, which restores it.")


#: A decision table must have at least this many data rows. One or two rows is
#: as likely to be a two-row summary as a rule set, and the column evidence is
#: weakest exactly there.
TABLE_MIN_ROWS = 2

#: …and at most this many. Past some length a table is a catalogue, not a rule
#: set. See `_table_role` for the measurement behind it.
TABLE_MAX_ROWS = 12

#: Admit only the rows that participate in the table's own rule, judged by the
#: condition column. Off makes every row of a recognised decision table a
#: candidate, which is the looser variant the probe measures.
STRICT_ROW_IN_RULE = True

# ---------------------------------------------------------------------------
# Phase 11 — precision mechanisms. Each is a property a block either has or
# does not have, never a reference to content: the test applied in
# `fieldtest/phase11/DEV_PROTOCOL_ADDENDUM.md` §2 is *would this be meaningful
# in a corpus about something other than trading?*
# ---------------------------------------------------------------------------

#: `n1a` — a block whose head is an inline-code identifier followed by a
#: separator is documenting a field, flag or option, and the operative word in
#: it describes the option rather than instructing the agent:
#: ``- `decay_horizon`: recommended holding period``. Risk: a real rule written
#: as ``- `force`: never set this in production``.
CODE_HEAD_IS_DOCUMENTATION = False

#: `n1b` — a table row with two or more cells that are *wholly* inline code is
#: an API/endpoint mapping table, not a rule table.
CODE_CELL_ROWS_ARE_DOCUMENTATION = False
#: How many wholly-code cells it takes. Two, because one is just a row that
#: mentions an identifier.
CODE_CELLS_MIN = 2

#: `n2` — a bound word counts as a quantified bound only when it is adjacent to
#: a digit and is not a function call. `max drawdown`, `Min Volatility` and
#: `max(a, b)` are a metric name, an index name and a call; none is a bound.
BOUND_NEEDS_QUANTITY = False
#: Reason names this applies to. English only: the Chinese 至少 / 最多 / 不超过
#: family is operative without a numeral far more often, and narrowing it was
#: not what the adjudicated errors asked for.
QUANTITY_DEPENDENT = frozenset({"quantified_bound"})
#: Characters either side of the bound word in which a digit counts as adjacent.
QUANTITY_WINDOW = 14

#: `n3` — a blockquote carrying a quoted span is a quotation, and its obligation
#: belongs to whoever is being quoted. The NARROW form of the exclusion Phase 7
#: measured and declined: a blanket blockquote exclusion scored +1.6 precision
#: for −0.5 recall because blockquotes do carry real rules
#: (`fieldtest/phase7/probe_holder.json`). Requiring a quoted span is what
#: separates `> Buffett: "A great company …"` from a rule in a callout.
QUOTED_BLOCKQUOTE_IS_QUOTATION = False
#: A quoted span shorter than this is a quoted *term*, not a quoted sentence.
QUOTE_MIN_CHARS = 20

#: `n4` — a prose fence is admitted only if it is enumerated. The fenced Class C
#: spans in both retired sets are checklists and screening criteria; the fenced
#: false positives are formulas and pseudocode. Borderline, and marked so in the
#: addendum: it is a property of the fence, but "enumerated" is a proxy for
#: "instructional" rather than the thing itself.
PROSE_FENCE_NEEDS_ENUMERATION = False
ENUMERATED_LINES_MIN = 2

#: `n5` — subject attribution. `off` | `flag` | `filter`.
#:
#: * `flag` records a `holder` on each candidate and changes no verdict;
#: * `filter` additionally drops candidates whose holder is not the agent.
#:
#: **Ships `off`. Measured and declined in BOTH modes**, and the phase is closed
#: on it (addendum §6). This was the Owner's named priority and the answer is
#: negative:
#:
#: * as a `filter` it costs 24.0 and 23.5 points of recall on the two random
#:   strata, for 17.1 and 4.5 points of precision;
#: * as a `flag` it is **uninformative**. It marks 74% of all corpus candidates
#:   `third_party`, and `holder == third_party` predicts an error with 20.1%
#:   precision against a 15.3% base rate on the development set, 36.1% against
#:   34.0% on the retired one. A flag that fires on 88% of real constraints
#:   would mislead the reviewer it is meant to help, so shipping it is worse
#:   than shipping nothing.
#:
#: The reason is structural and is the finding, not the excuse. An operative
#: sentence in a skill document normally carries a nominal subject that is the
#: *object* of the agent's action — `数据必须来自公开披露渠道`, `All figures
#: must be sourced from filings` — rather than the imperative or second person
#: this mechanism assumes. "Has an explicit nominal subject" therefore does not
#: separate the agent's rules from anyone else's. Doing that requires knowing
#: whether the named entity IS an external actor, which is semantic knowledge
#: and not grammar. `PHASE7.md` §5 said the holder problem is semantic rather
#: than structural; two structural attempts there and one grammatical attempt
#: here are now the evidence for it.
SUBJECT_ATTRIBUTION = "off"
HOLDER_AGENT = "agent"
HOLDER_THIRD_PARTY = "third_party"
HOLDER_UNKNOWN = ""

#: Second-person reference: wherever the text addresses a reader, the reader is
#: the agent. Pronouns and not nouns, so this is closed-class and does not grow.
_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself)\b|你|您", re.I)
#: Clause boundaries. A subject is looked for only back to the nearest one.
_CLAUSE_BOUNDARY = re.compile(r"[.;:!?。；：！？\n|]|(?:^|\s)[-*+]\s|\d+[.)]\s")
#: Leading decoration that is not a subject: list markers, emphasis, quote
#: markers, checkbox syntax.
_LEADING_DECORATION = re.compile(
    r"\A(?:[\s>]*(?:[-*+]|\d+[.)])?\s*(?:\[[ xX]\])?\s*(?:\*\*|__|\*|_)*)+")
#: An inline-code span.
_INLINE_CODE = re.compile(r"`[^`\n]+`")
#: A block head that is an inline-code identifier followed by a separator.
_CODE_HEAD = re.compile(
    r"\A[\s>]*(?:[-*+]|\d+[.)])?\s*(?:\*\*|__)?\s*`[^`\n]+`\s*(?:\*\*|__)?"
    r"\s*[:：—–-]")
#: A line that opens an enumeration.
_ENUM_LINE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)、]|[①-⑳])\s*\S")

_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
#: The fence opener's info string, for `_prose_fences`.
_FENCE_INFO = re.compile(r"^[ \t]{0,3}(?:`{3,}|~{3,})[ \t]*([A-Za-z0-9_.+-]*)")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")
_EMPHASIS_ONLY = re.compile(r"\A(?:\*\*|__|\*|_)[^\n]{0,60}?(?:\*\*|__|\*|_)[:：]?\Z")
_TABLE_ROW = re.compile(r"^[ \t]{0,3}\|")
#: The `|---|:--:|` rule under a table's header row.
_TABLE_DELIM = re.compile(r"^[ \t]{0,3}\|?[ \t:|-]+\|[ \t:|-]*$")

#: A heading that announces constraints. Used only to find *unresolved* cases —
#: never as a trigger, because "this section is about limits" is not evidence
#: that any particular sentence in it is a constraint.
_CONSTRAINT_HEADING = re.compile(
    r"constraint|limitation|restrict|prohibit|forbidden|not\s+for|caveat|"
    r"disclaimer|compliance|complian|legal|risk\s+(?:control|warning|disclosure)|"
    r"safety|guardrail|boundar|scope|do\s+not|don'?t|must|rule|requirement|"
    r"约束|限制|禁止|注意事项|免责|合规|风险提示|安全|边界|规则|要求",
    re.IGNORECASE)

#: Shorter than this and a block cannot carry a constraint; it is a fragment,
#: a column label or a stray word.
MIN_CHARS = 12

EXCL_FRONTMATTER = "frontmatter"
EXCL_FENCED = "fenced_code"
EXCL_TABLE = "table_row"
EXCL_LABEL = "emphasis_label"
EXCL_HEADING = "heading"
EXCL_SHORT = "too_short"
#: Phase 11. Counted like every other exclusion, so the report can say what was
#: rejected and why rather than leaving the drop implied.
EXCL_QUOTATION = "attributed_quotation"
EXCL_CODE_DOC = "code_documentation"
EXCL_HOLDER = "holder_not_agent"
EXCL_FENCE_UNENUMERATED = "fence_not_enumerated"
EXCLUSIONS = (EXCL_FRONTMATTER, EXCL_FENCED, EXCL_TABLE, EXCL_LABEL,
              EXCL_HEADING, EXCL_SHORT, EXCL_QUOTATION, EXCL_CODE_DOC,
              EXCL_HOLDER, EXCL_FENCE_UNENUMERATED)


def source_hash(text: str) -> str:
    """sha256 of the exact source slice.

    Over `source_text` and not over a normalised form: this hash is the thing
    `sync` compares to decide `SOURCE_CHANGED`, and a normalisation is exactly
    a class of edit it would then stop seeing.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Candidate:
    """One candidate constraint, as the brief specifies it, plus provenance."""

    candidate_id: str
    skill_id: str
    source_file: str
    source_span: tuple[int, int]        # char offsets into the file, [start, end)
    source_text: str
    extraction_reason: str              # human-readable, comma-joined
    tier: str
    source_lines: tuple[int, int] = (0, 0)
    source_hash: str = ""
    block_kind: str = ""
    heading_path: str = ""
    declared_id: str = ""               # only for tier == author_marker
    #: Who the obligation appears to belong to — `agent`, `third_party`, or ""
    #: when `SUBJECT_ATTRIBUTION` is off. Carried so the confirm step can put
    #: `PHASE7.md` §5.4's question ("whose rule is this?") in front of a
    #: reviewer, which it owes whether or not the filter variant ever ships.
    holder: str = ""

    @property
    def locator(self) -> str:
        lo, hi = self.source_lines
        span = f"{lo}" if hi <= lo else f"{lo}-{hi}"
        return f"{self.source_file}:{span}"

    @property
    def label(self) -> str:
        first = self.source_text.strip().splitlines()[0] if self.source_text.strip() else ""
        return first if len(first) <= 140 else first[:139] + "…"

    def as_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "skill_id": self.skill_id,
                "source_file": self.source_file,
                "source_span": list(self.source_span),
                "source_lines": list(self.source_lines),
                "source_hash": self.source_hash,
                "source_text": self.source_text,
                "extraction_reason": self.extraction_reason,
                "tier": self.tier, "block_kind": self.block_kind,
                "heading_path": self.heading_path,
                "declared_id": self.declared_id,
                "holder": self.holder}


@dataclass(frozen=True)
class Unresolved:
    """A constraint-shaped section that produced no candidate."""

    skill_id: str
    source_file: str
    line: int
    heading: str
    reason: str = "constraint-shaped heading, no candidate extracted in section"

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "source_file": self.source_file,
                "line": self.line, "heading": self.heading, "reason": self.reason}


@dataclass
class Extraction:
    """Candidates for one repository, plus what extraction could not settle."""

    candidates: list[Candidate] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    marker_issues: list[MarkerIssue] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=lambda: {k: 0 for k in EXCLUSIONS})
    blocks_seen: int = 0

    @property
    def by_skill(self) -> dict[str, list[Candidate]]:
        out: dict[str, list[Candidate]] = {}
        for c in self.candidates:
            out.setdefault(c.skill_id, []).append(c)
        return out

    @property
    def by_id(self) -> dict[str, Candidate]:
        return {c.candidate_id: c for c in self.candidates}

    def tier_counts(self) -> dict[str, int]:
        out = {t: 0 for t in TIERS}
        for c in self.candidates:
            out[c.tier] += 1
        return out


def candidate_id(skill_id: str, source_file: str, text: str) -> str:
    """A deterministic id: the same text in the same place is the same candidate.

    Keyed on the source *text* rather than the span, so inserting a paragraph
    above a candidate does not change its id — which is what makes
    `confirm --candidate <id>` reproducible against a re-scan.
    """
    key = f"{skill_id}\x00{source_file}\x00{source_hash(text)}"
    return "cand-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _fence_ranges(source: str) -> list[tuple[int, int]]:
    """Half-open char ranges covered by fenced code blocks.

    An unclosed fence runs to end of file, which is how the real markdown
    readers treat it and is also the conservative reading: text after a stray
    ``` is excluded rather than imported.
    """
    out: list[tuple[int, int]] = []
    offset = 0
    open_at: int | None = None
    token = ""
    for line in source.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        m = _FENCE.match(stripped)
        if m:
            fence = m.group(1)[:3]
            if open_at is None:
                open_at, token = offset, fence
            elif fence == token:
                out.append((open_at, offset + len(line)))
                open_at, token = None, ""
        offset += len(line)
    if open_at is not None:
        out.append((open_at, len(source)))
    return out


def _heading_index(source: str) -> list[tuple[int, int, str]]:
    """`(offset, level, title)` for each ATX heading outside a fence."""
    fences = _fence_ranges(source)
    out: list[tuple[int, int, str]] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        m = _HEADING.match(stripped)
        if m and not any(lo <= offset < hi for lo, hi in fences):
            out.append((offset, len(m.group(1)), m.group(2)))
        offset += len(line)
    return out


def _heading_path(index: list[tuple[int, int, str]], offset: int) -> str:
    stack: list[tuple[int, str]] = []
    for head_offset, level, title in index:
        if head_offset > offset:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    return " > ".join(title for _, title in stack)


def _is_table(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    rows = sum(1 for ln in lines if _TABLE_ROW.match(ln))
    return rows >= 2 and rows >= len(lines) - 1


def _table_rows(text: str, start: int) -> list[tuple[str, int, int, int]]:
    """`(row_text, char_start, char_end, line_offset)` for each **data** row.

    The header row and the `|---|` rule are dropped: a column label is not a
    rule, and the delimiter is punctuation. Everything before the delimiter is
    treated as header, so a table written without one yields no rows at all
    rather than importing its labels.
    """
    out: list[tuple[str, int, int, int]] = []
    offset = 0
    past_header = False
    for i, raw in enumerate(text.splitlines(keepends=True)):
        line = raw.rstrip("\r\n")
        if _TABLE_ROW.match(line):
            if _TABLE_DELIM.match(line):
                past_header = True
            elif past_header:
                out.append((line, start + offset, start + offset + len(line), i))
        offset += len(raw)
    return out


def _cells(row: str) -> list[str]:
    """The cells of one markdown table row, outer pipes stripped."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _table_role(text: str, rows: list[tuple[str, int, int, int]]
                ) -> tuple[str, int]:
    """`(role, condition column)` for this table, read off its own columns.

    This is the mechanism `PHASE7.md` §5 named and did not build: *read a table
    row in the context of its table.* Phase 7 instead measured a row-level
    "does this row contain a number" pattern, which cost 9 points of precision
    because it could not tell a threshold row from a row of an SEC form
    glossary — the two look identical one row at a time.

    A table is a **decision table** when one of its *columns* is systematically
    a condition or a consequence:

    * `decision_numeric` — at least two data rows carry a comparison bound at
      the same column index. A parameter dictionary's `Default` column holds
      bare values (`20`, `0.05`, `True`); a threshold column holds `> 0.03`,
      `<110`, `100-120`.
    * `decision_directive` — at least two data rows carry operative or advisory
      language at the same column index. This is the categorical case:
      `| Change Type | Signal | Action |`, where the condition is a category and
      the consequence is an instruction.

    Same column index, not merely two rows anywhere, because "a number appears
    twice in this table" is true of almost every table in the corpus. A column
    is what makes it a rule: the table is asserting the same relation of every
    row. The index is returned as well as the role, because it is also what
    decides which *rows* participate — see `_row_in_rule`.

    `TABLE_MAX_ROWS` is the other half of the mechanism, and it is Phase 7's own
    reasoning put back in a defensible place. Phase 7 excluded whole tables
    because "a 40-row table matching *must* once is not a constraint", which was
    right about catalogues and wrong about rule sets. The distinction is size: a
    table asserting one rule per row is a rule set and rule sets are short,
    while `tushare`'s 231-row endpoint index is a document. Without the cap, one
    catalogue classified as a decision table took the retired set's purposive
    stratum from 36 candidates to 263 at 11% precision.

    Everything else is `reference`, and a reference table keeps the pre-Phase-10
    behaviour exactly — its rows are emitted only when the row itself carries
    constraint language.
    """
    if (not TABLE_ROLE_CLASSIFICATION or len(rows) < TABLE_MIN_ROWS
            or len(rows) > TABLE_MAX_ROWS):
        return ROLE_REFERENCE, -1
    bounds: dict[int, int] = {}
    directives: dict[int, int] = {}
    for row_text, _lo, _hi, _line in rows:
        for i, cell in enumerate(_cells(row_text)):
            if _BOUND.search(cell):
                bounds[i] = bounds.get(i, 0) + 1
            if _reasons(cell)[0]:
                directives[i] = directives.get(i, 0) + 1
    best = max(bounds.items(), key=lambda kv: (kv[1], -kv[0]), default=None)
    if best and best[1] >= 2:
        return ROLE_NUMERIC, best[0]
    best = max(directives.items(), key=lambda kv: (kv[1], -kv[0]), default=None)
    if best and best[1] >= 2:
        return ROLE_DIRECTIVE, best[0]
    return ROLE_REFERENCE, -1


def _row_in_rule(row_text: str, role: str, col: int) -> bool:
    """Does this row participate in its table's rule?

    The missing half of "read the row in the context of its header". Admitting
    *every* row of a recognised decision table is what cost 22.8 points of
    precision on the development stratum: a rule table still carries notes,
    totals and blank continuation rows, and each of those became a candidate on
    the strength of its neighbours.

    So the condition column decides. A row belongs to the rule when its own cell
    in that column is the kind of thing the column is made of — a bound for a
    numeric table, constraint language for a directive one. `STRICT_ROW_IN_RULE`
    switches this off so the looser variant stays measurable.
    """
    if not STRICT_ROW_IN_RULE:
        return True
    cells = _cells(row_text)
    if not 0 <= col < len(cells):
        return False
    cell = cells[col]
    if role == ROLE_NUMERIC:
        return bool(_BOUND.search(cell))
    if role == ROLE_DIRECTIVE:
        return bool(_reasons(cell)[0])
    return False


def _prose_fences(source: str) -> list[tuple[int, int, int, str]]:
    """`(start, end, opener_line, info)` for prose-bearing fences.

    The blanket fence exclusion was measured as a precision win in Phase 7a and
    left alone, on the reasoning that a fence holds *implementation, not
    instruction*. The retired Phase 8 set shows where that reasoning stops: 43
    of its random stratum's 157 Class C spans are fenced, and 25 of those sit in
    **bare** or `markdown` fences holding numbered screening criteria, checklists
    and output templates — prose that happens to be in a monospace box.

    The info string is the mechanism. ```` ```python ```` announces a body
    written for an interpreter; a bare ```` ``` ```` announces nothing and in
    this corpus usually holds a list. So prose-tagged fences become candidate
    blocks and language-tagged ones stay excluded, which leaves Phase 7a's
    measured win intact for the case it was measured on.

    The span is the **whole fence**, opening delimiter through the newline after
    the closing one, and not the body. Body-only was the first attempt and it
    measured as zero recall on the retired set while the candidate count went
    up: both gold sets anchor a fenced rule to the entire fence, so a body-only
    candidate *overlaps* the gold span without *containing* it — a lenient match
    and a strict miss, and the headline figure is strict. The unit the
    annotation uses is the unit to emit. It also keeps `source_text` equal to
    `source[start:end]`, which `sync` depends on: `source_hash` is over the
    stored text, so a text that is not its own span would make `SOURCE_CHANGED`
    compare a slice against a hash of something else.

    An unclosed fence yields nothing here — it already runs to end of file under
    `_fence_ranges`, and importing the rest of a document on a stray backtick is
    the opposite of conservative.
    """
    if not PROSE_FENCE_CANDIDATES:
        return []
    out: list[tuple[int, int, int, str]] = []
    offset = 0
    open_at: int | None = None
    body_start = 0
    token = ""
    info = ""
    line_no = 0
    open_line = 0
    for line in source.splitlines(keepends=True):
        line_no += 1
        stripped = line.rstrip("\r\n")
        m = _FENCE.match(stripped)
        if m:
            fence = m.group(1)[:3]
            if open_at is None:
                open_at, token = offset, fence
                body_start = offset + len(line)
                open_line = line_no
                info = (_FENCE_INFO.match(stripped).group(1) or "").lower()
            elif fence == token:
                if info in PROSE_FENCE_INFO and offset > body_start:
                    out.append((open_at, offset + len(line), open_line, info))
                open_at, token, info = None, "", ""
        offset += len(line)
    return out


def _quantity_adjacent(text: str, pattern: "re.Pattern[str]") -> bool:
    """Does any match of *pattern* sit next to a digit, and not before a `(`?

    The fix for `quantified_bound`, whose `min` / `max` alternatives fire on
    `max drawdown` (a metric), `Min Volatility` (an index) and
    `max(n_income_est, profit_dedt_est)` (a call). A *quantified* bound has a
    quantity; requiring one is the mechanism, and it needs the match POSITION,
    which is why this cannot be done by editing the pattern's alternatives.
    """
    for m in pattern.finditer(text):
        lo, hi = m.span()
        if text[hi:hi + 1] == "(":
            continue                    # a function call, not a quantifier
        window = text[max(0, lo - QUANTITY_WINDOW):hi + QUANTITY_WINDOW]
        if any(ch.isdigit() for ch in window):
            return True
    return False


def _is_blockquote(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(ln.lstrip().startswith(">") for ln in lines)


def _has_quoted_sentence(text: str) -> bool:
    """A paired quotation long enough to be a sentence rather than a term."""
    for opener, closer in (('"', '"'), ("“", "”"),
                           ("「", "」"), ("‘", "’")):
        start = text.find(opener)
        if start == -1:
            continue
        end = text.find(closer, start + 1)
        if end != -1 and end - start - 1 >= QUOTE_MIN_CHARS:
            return True
    return False


def _is_attributed_quotation(text: str) -> bool:
    """A blockquote carrying a quoted sentence. See `n3` in the switch block."""
    return (QUOTED_BLOCKQUOTE_IS_QUOTATION and _is_blockquote(text)
            and _has_quoted_sentence(text))


def _is_code_documentation(text: str, kind: str) -> bool:
    """Field / flag / endpoint documentation, by one of two shapes.

    `n1a` the block's head is an inline-code identifier followed by a
    separator, so what follows is a gloss of that identifier. `n1b` a table row
    with `CODE_CELLS_MIN` cells that are *wholly* inline code is a mapping
    table — the `ashare-pre-st-filter` tushare/akshare endpoint tables are five
    columns of identifiers and a notes column, and the notes column is where
    the operative word lives.
    """
    if CODE_HEAD_IS_DOCUMENTATION and _CODE_HEAD.match(text):
        return True
    if CODE_CELL_ROWS_ARE_DOCUMENTATION and kind == "table_row":
        wholly = sum(1 for c in _cells(text)
                     if c and _INLINE_CODE.fullmatch(c.strip()))
        if wholly >= CODE_CELLS_MIN:
            return True
    return False


def _is_enumerated(text: str) -> bool:
    lines = text.splitlines()
    return sum(1 for ln in lines if _ENUM_LINE.match(ln)) >= ENUMERATED_LINES_MIN


def holder_of(text: str, kind: str = "") -> str:
    """Who the obligation belongs to: `agent`, `third_party`, or unknown.

    The grammatical mechanism, with no list of actors in it. The agent is
    addressed, never named: a rule for the agent is an imperative (`Must
    include a disclaimer`) or second-person (`you must`). A rule for somebody
    else names them (`material cybersecurity incidents must be disclosed`,
    `资本利得需计入企业所得税`).

    So: find the first operative match, take the text back to the nearest
    clause boundary, strip list markers and emphasis, and ask whether anything
    is left. Nothing left means imperative mood, which means the agent.

    For a table row the clause boundary is the cell wall, so a match in a later
    cell has an empty prefix and would read as imperative. The row's **first
    cell is its implicit subject** — `| Amihud illiquidity | … | Requires only
    daily data |` is a property of the metric — so that is what is examined.

    Returns `HOLDER_UNKNOWN` when no operative pattern matches at all, because
    then there is no obligation to attribute.
    """
    first: "int | None" = None
    for _tier, name, pattern in _COMPILED:
        m = pattern.search(text)
        if m and (first is None or m.start() < first):
            first = m.start()
    if first is None:
        return HOLDER_UNKNOWN

    if kind == "table_row":
        cells = _cells(text)
        # Which cell does the match fall in? Everything before the first wall
        # that lies past `first`.
        prefix = text[:first]
        cell_index = prefix.count("|") - (1 if text.strip().startswith("|") else 0)
        if cell_index > 0 and cells:
            subject = cells[0]
        else:
            subject = ""
    else:
        boundaries = [m.end() for m in _CLAUSE_BOUNDARY.finditer(text[:first])]
        subject = text[boundaries[-1] if boundaries else 0:first]

    if _SECOND_PERSON.search(text[:first] if kind != "table_row" else text):
        return HOLDER_AGENT
    bare = _LEADING_DECORATION.sub("", subject).strip(" \t`*_>“\"'()[]（）")
    return HOLDER_THIRD_PARTY if bare else HOLDER_AGENT


def _reasons(text: str) -> tuple[str, list[str]]:
    """`(tier, reason names)` for *text*, or `("", [])` when nothing matches."""
    names: list[str] = []
    tier = ""
    for candidate_tier, name, pattern in _COMPILED:
        if not pattern.search(text):
            continue
        # A quantity-dependent reason has to earn its match on position, not
        # just on the word being present. See `_quantity_adjacent`.
        if (BOUND_NEEDS_QUANTITY and name in QUANTITY_DEPENDENT
                and not _quantity_adjacent(text, pattern)):
            continue
        names.append(name)
        if not tier or (tier == ADVISORY and candidate_tier == OPERATIVE):
            tier = candidate_tier
    return tier, names


def extract_file(skill: Skill, f: SourceFile, out: Extraction) -> None:
    """Candidates in one file, appended to *out*."""
    source = f.text
    fences = _fence_ranges(source)
    headings = _heading_index(source)
    fm_end = f.frontmatter_end

    marker_constraints, marker_issues = scan_file(f.rel_path, source)
    out.marker_issues += marker_issues
    marked: dict[int, str] = {}
    for mc in marker_constraints:
        if mc.tracked:
            marked[mc.start] = mc.id

    produced_at: list[int] = []     # offsets of blocks that yielded a candidate

    for block in segment(source):
        text = block.text
        if MARKER_LINE.match(text.strip()):
            continue
        out.blocks_seen += 1
        declared = marked.get(block.start, "")

        if block.start < fm_end:
            out.excluded[EXCL_FRONTMATTER] += 1
            continue
        # Any overlap with a fence, not just a start inside one. `segment` has
        # no notion of fences, so a label line immediately above a fence
        # absorbs the whole code block into one paragraph; testing only
        # `block.start` let that paragraph through and imported the fence's
        # contents as a constraint. Measured in fieldtest/phase7: two false
        # positives in the gold sample, both an emphasis label glued to a
        # reference block of exchange mechanics.
        if any(block.start < hi and lo < block.end for lo, hi in fences):
            out.excluded[EXCL_FENCED] += 1
            continue
        if block.kind == "heading":
            out.excluded[EXCL_HEADING] += 1
            continue
        if len(text.strip()) < MIN_CHARS:
            out.excluded[EXCL_SHORT] += 1
            continue
        if _EMPHASIS_ONLY.match(text.strip()):
            out.excluded[EXCL_LABEL] += 1
            continue
        def emit(body: str, lo: int, hi: int, line_lo: int, line_hi: int,
                 kind: str, tier: str, names: list[str], marker_id: str = "") -> None:
            # Phase 11 exclusions, applied at the one place every candidate
            # passes through. Author markers are exempt: an explicit
            # `<!-- constraintflow: critical -->` is the author having already
            # confirmed, and no heuristic may overrule it.
            if tier != AUTHOR_MARKER:
                if _is_attributed_quotation(body):
                    out.excluded[EXCL_QUOTATION] += 1
                    return
                if _is_code_documentation(body, kind):
                    out.excluded[EXCL_CODE_DOC] += 1
                    return
            holder = ""
            if SUBJECT_ATTRIBUTION in ("flag", "filter") and tier != AUTHOR_MARKER:
                holder = holder_of(body, kind)
                if (SUBJECT_ATTRIBUTION == "filter"
                        and holder == HOLDER_THIRD_PARTY):
                    out.excluded[EXCL_HOLDER] += 1
                    return
            out.candidates.append(Candidate(
                candidate_id=candidate_id(skill.skill_id, f.rel_path, body),
                skill_id=skill.skill_id,
                source_file=f.rel_path,
                source_span=(lo, hi),
                source_text=body,
                extraction_reason=", ".join(names),
                tier=tier,
                source_lines=(line_lo, line_hi),
                source_hash=source_hash(body),
                block_kind=kind,
                heading_path=_heading_path(headings, lo),
                declared_id=marker_id,
                holder=holder,
            ))
            produced_at.append(lo)

        if _is_table(text):
            # One rule per row, not one candidate per table. See "Tables" in the
            # module docstring for the measurement that changed this.
            rows = _table_rows(text, block.start)
            role, col = _table_role(text, rows)
            for row_text, lo, hi, line_off in rows:
                if len(row_text.strip()) < MIN_CHARS:
                    out.excluded[EXCL_TABLE] += 1
                    continue
                row_tier, row_names = _reasons(row_text)
                if (not row_tier and role in TABLE_DECISION_ROLES
                        and _row_in_rule(row_text, role, col)):
                    # The table, not the row, is the evidence. A threshold row
                    # carries a bound and no modal verb — `| IC mean | > 0.03 |
                    # Factor has basic predictive power |` — and is invisible to
                    # every pattern in `_PATTERNS` by construction.
                    row_tier, row_names = OPERATIVE, [f"{role}_row"]
                if not row_tier:
                    out.excluded[EXCL_TABLE] += 1
                    continue
                emit(row_text, lo, hi, block.line + line_off,
                     block.line + line_off, "table_row", row_tier, row_names)
            continue

        if declared:
            tier, names = AUTHOR_MARKER, ["explicit author marker"]
        else:
            tier, names = _reasons(text)
            if not tier:
                continue

        emit(text, block.start, block.end, block.line, block.end_line,
             block.kind, tier, names, declared)

    # Prose fences, in a second pass. Separate from the block loop on purpose:
    # `segment` has no notion of fences, so the block holding a fence is the
    # label line plus the whole code body, and that block stays excluded. One
    # candidate per fence body — which is also the granularity the gold sets
    # annotate, a fenced checklist being one rule and not one rule per line.
    for fence_start, fence_end, fence_line, info in _prose_fences(source):
        if fence_start < fm_end:
            continue
        body = source[fence_start:fence_end]
        if len(body.strip()) < MIN_CHARS:
            out.excluded[EXCL_FENCED] += 1
            continue
        if PROSE_FENCE_NEEDS_ENUMERATION and not _is_enumerated(body):
            # The fenced Class C spans in both retired sets are checklists and
            # screening criteria; the fenced false positives are formulas and
            # pseudocode. Borderline — `n4` in the addendum says why.
            out.excluded[EXCL_FENCE_UNENUMERATED] += 1
            continue
        tier, names = _reasons(body)
        if not tier and FENCE_BOUND_ADMISSION and len(_BOUND.findall(body)) >= 2:
            # A screening checklist is a list of bounds and nothing else:
            # `筛选条件: 1. 双低值 < 130（严格）或 < 150（宽松）`. Two, not one,
            # because a single number appears in most prose.
            tier, names = OPERATIVE, ["fence_bounds"]
        if not tier:
            out.excluded[EXCL_FENCED] += 1
            continue
        names = [f"prose_fence[{info or 'bare'}]", *names]
        emit(body, fence_start, fence_end, fence_line,
             fence_line + body.count("\n"), "prose_fence", tier, names)

    # Unresolved: a constraint-shaped heading whose section produced nothing.
    for i, (offset, level, title) in enumerate(headings):
        if offset < fm_end or not _CONSTRAINT_HEADING.search(title):
            continue
        end = len(source)
        for later_offset, later_level, _ in headings[i + 1:]:
            if later_level <= level:
                end = later_offset
                break
        if any(offset <= p < end for p in produced_at):
            continue
        line = source.count("\n", 0, offset) + 1
        out.unresolved.append(Unresolved(skill.skill_id, f.rel_path, line, title))


def extract(inv: Inventory, *, skill_ids: "list[str] | None" = None) -> Extraction:
    """Candidates for every skill in *inv*, or only the named ones."""
    out = Extraction()
    wanted = set(skill_ids) if skill_ids else None
    for skill in inv.skills:
        if wanted is not None and skill.skill_id not in wanted:
            continue
        for f in skill.files:
            extract_file(skill, f, out)
    out.candidates.sort(key=lambda c: (c.skill_id, c.source_file, c.source_span))
    out.unresolved.sort(key=lambda u: (u.skill_id, u.source_file, u.line))
    return out
