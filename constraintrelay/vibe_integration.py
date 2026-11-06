"""Phase 2 — delivery through the Vibe-Trading execution path.

Phase 1's `cf/deliver.py` proved the capsule *could* reach model-visible context
by assembling the envelope itself. That was labelled a simulated injection
point, and this module is what removes the label: it installs a tool into the
**real** `ToolRegistry`, so the real `AgentLoop` calls it, the real
`_finalize_tool_result` truncates its output, and the real `messages` list
carries it into `ChatLLM.stream_chat`. Nothing between the tool and the model
request is reimplemented here.

Where the integration attaches
------------------------------
"When a skill is selected" has exactly one meaning in this harness: the model
calls `load_skill(name=...)`. So the attachment point is that tool.

`ConstraintAwareLoadSkill` **wraps** the stock `LoadSkillTool` rather than
replacing or subclassing it:

* `name`, `description`, `parameters`, `repeatable` and `is_readonly` are read
  off the wrapped instance, so the model sees a byte-identical tool definition
  and the two cannot drift apart when upstream edits the description;
* the skill body comes from the wrapped tool's own `execute`, so the loader
  path, the frontmatter handling and the `<skill name="…">` wrapper are the
  upstream ones;
* `ToolRegistry.register` keys on `tool.name`, so registering this one
  **replaces** `load_skill` in the registry with no edit to the Vibe checkout.
  There is no fork to maintain and `install()` is undone by re-registering the
  original.

Why the body is trimmed and the capsule is not
----------------------------------------------
`loop.py` applies `result[:TOOL_RESULT_LIMIT]` — a prefix cut over the
*serialized* envelope. Phase 1 put the capsule first, which saved the
constraint text but left a truncated envelope that is no longer valid JSON. A
model reading that sees the characters; a JSON consumer sees a parse error, and
"the exact decoded constraint text in the final model request" cannot be
asserted at all.

So this module sizes the envelope to the limit **before** returning it: the
capsule is placed first and kept whole, and the skill body is trimmed to make
room. The envelope the loop receives is already within the limit, the prefix cut
is a no-op, and the result decodes. The trim is announced inside `content`,
where the model can see it, because a silently shortened instruction document is
the exact failure this project exists to report.

What this module refuses to do
------------------------------
If the capsule alone does not fit the budget, it does **not** choose which
constraints to drop by importance. Constraints are emitted in the store's own
order until the next one would not fit whole, every dropped `constraint_id` is
named in the delivery record, and the record says `partial`. Deciding *which*
constraints matter is minimal-sufficient-subset selection — Phase 3 — and
inventing a priority order here would be that decision made silently.

A constraint is never delivered in part. Half a prohibition is worse than a
named absent one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .deliver import CAPSULE_CLOSE, CAPSULE_OPEN, TOOL_RESULT_LIMIT, render_capsule
from .query import ConstraintFlow
from .skillstore import ConstraintRecord

COMPLETE = "complete"
PARTIAL = "partial"
NONE = "none"

#: Skill-body delivery status, reported **separately** from the constraint
#: status above. Owner ruling 2026-10-07: *"A result must not report generic
#: `delivery: complete` merely because all constraints arrived when the skill
#: body was effectively absent."*
#:
#: Phase 10's delivery re-proof found `ashare-pre-st-filter` arriving with all
#: 73 of its constraints and **0 of its 19,535 body characters**, under a
#: record that read `delivery: complete`. That status is quantified over
#: constraints, so it could not see the skill vanish, and `error` was empty too
#: — the "capsule plus trim notice overflows" branch that would have set one is
#: not reached, because a body cut to zero genuinely fits.
BODY_COMPLETE = "complete"      # every character of the body was delivered
BODY_TRUNCATED = "truncated"    # some delivered, some cut
BODY_DROPPED = "dropped"        # none delivered, and there was a body
BODY_ABSENT = "absent"          # there was no body to deliver
BODY_STATES = (BODY_COMPLETE, BODY_TRUNCATED, BODY_DROPPED, BODY_ABSENT)

#: Trim notice, placed inside `content` so it is model-visible. `{n}` is the
#: number of characters of the skill document that were not delivered.
TRIM_NOTICE = ("\n[constraintrelay: {n:,} characters of this skill document were "
               "not delivered, so that {k} recorded constraint(s) could be "
               "delivered whole. The omitted text is the tail of the document.]")

#: **The skill-body delivery floor.** Owner ruling 2026-10-08, closing
#: `task_2647d492`: *"enforce a minimum skill-body delivery floor."*
#:
#: Before this, the capsule was sized first and the body got whatever was left,
#: which on `ashare-pre-st-filter` was **nothing** — a 9,765-character capsule
#: for 73 constraints inside a 10,000-character budget, and the agent received
#: every constraint and none of the skill it had asked for. A skill delivered as
#: nothing but its own constraints is not a delivered skill.
#:
#: So the capsule's budget is now capped so that at least this share of the
#: limit survives for the body. Expressed as a **fraction of the limit** rather
#: than an absolute character count because the limit is the harness's and can
#: change; a fraction keeps the guarantee proportional to whatever budget
#: exists.
#:
#: 0.25 is a judgement, stated as one: it is the smallest reserve at which the
#: delivered head of a document still carries a skill's purpose and first
#: instructions rather than only its title. It is not tuned against any
#: measurement, and nothing in this module reads it except `body_floor`.
#:
#: The cost is real and is reported, not hidden: when the floor binds, the
#: capsule is **shortened**, constraints are dropped in store order, each one is
#: named in `dropped_ids`, and `constraint_delivery` becomes `partial`. The
#: trade is deliberate — a named, visible omission of some constraints beats a
#: silent total loss of the document.
BODY_FLOOR_FRACTION = 0.25


def body_floor(limit: int) -> int:
    """Characters of the budget reserved for the skill body.

    A function so the policy has one implementation and the test can drive it
    across limits. Zero when there is no budget to divide.
    """
    return max(0, int(limit * BODY_FLOOR_FRACTION))


#: Rounds of binary search over the kept body length. JSON escaping makes
#: envelope length a non-linear function of body length (one `"` costs two
#: characters, one control character six), and the trim notice's own width
#: changes with the number it reports, so the relationship is searched rather
#: than solved. 24 rounds covers a body of 16 million characters.
MAX_FIT_ROUNDS = 24


@dataclass
class DeliveryRecord:
    """What was delivered for one `load_skill` call. Not model-visible.

    Captured on the tool instance so a verifier — or a future receipt — can
    state what happened without re-deriving it from the envelope.
    """

    skill_name: str
    skill_id: str = ""
    #: **Constraint** delivery only — complete | partial | none. It says nothing
    #: about the skill body; read `body_delivery` for that. Kept under this name
    #: because it is the attribute every existing reader uses, while `as_dict`
    #: emits it as `constraint_delivery` so no serialized result carries a
    #: generic "delivery" verdict again.
    delivery: str = NONE
    constraint_ids: list[str] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)
    capsule_chars: int = 0
    body_chars: int = 0
    body_delivered_chars: int = 0
    envelope_chars: int = 0
    limit: int = TOOL_RESULT_LIMIT
    body_trimmed: bool = False
    error: str = ""

    @property
    def fits(self) -> bool:
        """The envelope is within the harness budget, so the prefix cut in
        `loop.py` is a no-op and the result still decodes."""
        return self.envelope_chars <= self.limit

    @property
    def body_trimmed_chars(self) -> int:
        return max(0, self.body_chars - self.body_delivered_chars)

    @property
    def constraint_delivery(self) -> str:
        """The same value as `delivery`, under a name that says what it covers."""
        return self.delivery

    @property
    def body_delivery(self) -> str:
        """Skill-body status, **derived** from the character counts.

        A property and not a field, deliberately. A field would have to be set
        at each of `build_envelope`'s five exits, which is five chances to
        forget one — and the exit that was forgotten is exactly how the
        zero-body case came to report `complete`. Derived from
        `body_chars` / `body_delivered_chars`, it cannot disagree with the
        numbers printed beside it.
        """
        if self.body_chars <= 0:
            return BODY_ABSENT
        if self.body_delivered_chars >= self.body_chars:
            return BODY_COMPLETE
        if self.body_delivered_chars <= 0:
            return BODY_DROPPED
        return BODY_TRUNCATED

    @property
    def body_warning(self) -> str:
        """A sentence for the dropped case, or "" — also derived.

        `error` is written by the constraint path and was empty for the
        zero-body delivery, so a reader had nothing to go on but two integers.
        """
        if self.body_delivery != BODY_DROPPED:
            return ""
        return (f"the skill body was NOT delivered: 0 of {self.body_chars:,} "
                f"characters. The capsule for {len(self.constraint_ids)} "
                f"constraint(s) is {self.capsule_chars:,} characters and the "
                f"{self.limit:,}-character budget left no room for any of the "
                f"document. The constraints arrived; the skill did not.")

    @property
    def fully_protected(self) -> bool:
        """Did everything arrive? **Derived, and the only answer to that.**

        Owner ruling 2026-10-08, ruling 4: *"Such a run must not be represented
        as fully protected or generically complete."*

        A caller that wanted to know "is this run clean" previously had to
        assemble the answer from `constraint_delivery` and `body_delivery` and
        could reach the wrong one by reading either alone — which is how
        `delivery: complete` came to describe a skill that arrived with none of
        its own text. There is now one property, it is derived from both halves
        plus `dropped_ids`, and it is False whenever anything was given up.

        `truncated` does NOT disqualify a run: trimming the tail of a long
        document is the designed, announced behaviour and the constraints are
        whole. `dropped` does, and so does any dropped constraint id.
        """
        return (self.delivery == COMPLETE
                and not self.dropped_ids
                and self.body_delivery in (BODY_COMPLETE, BODY_TRUNCATED,
                                           BODY_ABSENT))

    @property
    def protection_summary(self) -> str:
        """One sentence naming what was given up, or that nothing was.

        Built from the same two statuses, so a report cannot quote a cheerful
        summary beside a degraded status.
        """
        if self.fully_protected:
            if self.body_delivery == BODY_TRUNCATED:
                return ("all recorded constraints delivered whole; the skill "
                        f"body was trimmed to {self.body_delivered_chars:,} of "
                        f"{self.body_chars:,} characters, which is announced in "
                        "the envelope")
            return "all recorded constraints and the whole skill body delivered"
        parts = []
        if self.dropped_ids:
            parts.append(f"{len(self.dropped_ids)} constraint(s) NOT delivered "
                         f"({', '.join(self.dropped_ids)})")
        if self.delivery == NONE:
            parts.append("no constraint was delivered")
        if self.body_delivery == BODY_DROPPED:
            parts.append(f"the skill body was NOT delivered "
                         f"(0 of {self.body_chars:,} characters)")
        return ("NOT fully protected: " + "; ".join(parts)) if parts else (
            "NOT fully protected")

    def as_dict(self) -> dict:
        return {"skill_name": self.skill_name, "skill_id": self.skill_id,
                # Reported so no consumer has to assemble the answer, and so
                # none can assemble a wrong one. Ruling 4.
                "fully_protected": self.fully_protected,
                "protection_summary": self.protection_summary,
                # NOT "delivery". A single generic key is what let a result
                # claim `complete` while the skill body was absent; the two
                # statuses are now reported side by side and neither can stand
                # in for the other.
                "constraint_delivery": self.delivery,
                "body_delivery": self.body_delivery,
                "body_warning": self.body_warning,
                "constraint_ids": list(self.constraint_ids),
                "dropped_ids": list(self.dropped_ids),
                "capsule_chars": self.capsule_chars,
                "body_chars": self.body_chars,
                "body_delivered_chars": self.body_delivered_chars,
                "body_trimmed": self.body_trimmed,
                "body_trimmed_chars": self.body_trimmed_chars,
                "envelope_chars": self.envelope_chars,
                "limit": self.limit, "envelope_fits": self.fits,
                "error": self.error}


def _envelope(status: str, content: str) -> str:
    """Exactly the stock serialization — `load_skill_tool.py:execute`."""
    return json.dumps({"status": status, "content": content}, ensure_ascii=False)


def _fit_capsule(skill_name: str, records: list[ConstraintRecord], budget: int
                 ) -> tuple[str, list[ConstraintRecord], list[ConstraintRecord]]:
    """The longest prefix of *records* whose capsule fits *budget*.

    Store order, no prioritisation — see the module docstring. Returns
    `(capsule, kept, dropped)`; `kept` may be empty, in which case the capsule
    is the empty string and the caller reports a delivery failure.
    """
    if not records:
        return "", [], []
    capsule = render_capsule(skill_name, records)
    if len(capsule) <= budget:
        return capsule, list(records), []
    kept: list[ConstraintRecord] = []
    for i in range(1, len(records) + 1):
        trial = render_capsule(skill_name, records[:i])
        if len(trial) > budget:
            break
        kept = records[:i]
    if not kept:
        # Not even one constraint fits beside the envelope scaffolding. Report
        # it; do not emit a fragment.
        return "", [], list(records)
    return render_capsule(skill_name, kept), kept, records[len(kept):]


#: How many characters a cut will give up to land on a line boundary. Ending
#: mid-word is ugly; discarding a 900-character paragraph to avoid it is worse,
#: and "worse" here means author text the model does not receive. A proportional
#: rule (snap whenever the boundary is past 60% of the cut) gave up 172
#: characters of a 2,000-character budget on a document of long paragraphs, so
#: the allowance is absolute instead.
MAX_LINE_SNAP = 200


def _trim_to_line(text: str, length: int) -> str:
    """*text* cut to at most *length* characters, preferring a line boundary."""
    if length >= len(text):
        return text
    cut = text[:length]
    nl = cut.rfind("\n")
    return cut[:nl] if nl != -1 and length - nl <= MAX_LINE_SNAP else cut


def build_envelope(skill_name: str, body: str,
                   records: list[ConstraintRecord], *,
                   limit: int = TOOL_RESULT_LIMIT,
                   skill_id: str = "") -> tuple[str, DeliveryRecord]:
    """The tool result for one skill, sized so the harness cut is a no-op.

    *body* is the stock `content` — `<skill name="…">\\n{body}\\n</skill>` as the
    upstream loader produced it. The capsule is prepended to it.
    """
    rec = DeliveryRecord(skill_name=skill_name, skill_id=skill_id,
                         body_chars=len(body), limit=limit)

    if not records:
        envelope = _envelope("ok", body)
        rec.delivery = NONE
        rec.body_delivered_chars = len(body)
        rec.envelope_chars = len(envelope)
        rec.error = "no constraints recorded for this skill"
        # Nothing was added, so nothing is trimmed: an un-integrated skill must
        # behave exactly as it did before.
        return envelope, rec

    # The capsule's budget is the limit minus the smallest possible envelope
    # around it — the scaffolding, with no body at all — and minus the body
    # floor, so the document cannot be squeezed out entirely.
    #
    # **Capped at the body's own length**, which is the whole condition. A body
    # shorter than the floor needs only its own length held back, and a body of
    # zero length needs nothing: reserving more than exists would drop
    # constraints to make room for text that is not there.
    #
    # This was briefly written as `body_floor(limit) if body else 0` *and* the
    # cap. Mutation testing found the pair mutually masking — each alone handled
    # the empty-body case, so neither was individually necessary for it and
    # removing either left the suite green. The cap is the one that does the
    # work, because it is also the one that handles a short non-empty body,
    # which the conditional does not.
    scaffolding = len(_envelope("ok", "")) + len(TRIM_NOTICE.format(n=0, k=0))
    reserve = min(body_floor(limit), len(body))
    capsule, kept, dropped = _fit_capsule(skill_name, records,
                                          max(0, limit - scaffolding - reserve))
    rec.constraint_ids = [c.constraint_id for c in kept]
    rec.dropped_ids = [c.constraint_id for c in dropped]
    rec.capsule_chars = len(capsule)

    if not kept:
        envelope = _envelope("ok", body)
        rec.delivery = NONE
        rec.body_delivered_chars = len(body)
        rec.envelope_chars = len(envelope)
        rec.error = (f"the capsule for {len(records)} constraint(s) does not fit "
                     f"a {limit}-character tool-result budget even with no skill "
                     f"body; no constraint was delivered and none was truncated")
        return envelope, rec

    rec.delivery = COMPLETE if not dropped else PARTIAL
    if dropped:
        rec.error = (f"{len(dropped)} constraint(s) did not fit the "
                     f"{limit}-character budget and were not delivered: "
                     f"{', '.join(rec.dropped_ids)}. Phase 1/2 do not choose "
                     f"which constraints to drop by importance; store order was "
                     f"used and every omission is named here.")
        if reserve:
            # Name the floor as the cause when it is. Without this the message
            # reads as "the budget was too small", which is true of the total
            # but hides that `BODY_FLOOR_FRACTION` is what decided where the
            # capsule stopped — and that is the number a reader would want to
            # change.
            rec.error += (
                f" {reserve:,} character(s) of the budget "
                f"({BODY_FLOOR_FRACTION:.0%} of {limit:,}, capped at the body's "
                f"own length) were reserved for the skill body under the "
                f"delivery floor, so the capsule was cut to leave the document "
                f"room to arrive.")

    # Full body first: the common case is that everything fits and no trim
    # notice is needed at all.
    content = capsule + "\n" + body
    envelope = _envelope("ok", content)
    if len(envelope) <= limit:
        rec.body_delivered_chars = len(body)
        rec.envelope_chars = len(envelope)
        return envelope, rec

    # Binary search for the **longest** body that still fits, rather than the
    # first one that happens to. Shrinking by the overflow and stopping at the
    # first fit left 1,687 characters of a 10,000-character budget unused on
    # `social-media-intelligence` — 1,687 characters of the author's document
    # discarded for nothing, and an overhead figure inflated by the same
    # amount. The trim notice is inside the sized region, so it can never push
    # the result back over the limit.
    rec.body_trimmed = True

    def attempt(keep: int) -> tuple[str, int]:
        """`(envelope, delivered_body_chars)` for a body cut to *keep*."""
        trimmed = _trim_to_line(body, keep)
        notice = TRIM_NOTICE.format(n=len(body) - len(trimmed), k=len(kept))
        return _envelope("ok", capsule + "\n" + trimmed + notice), len(trimmed)

    lo, hi = 0, len(body)
    best = attempt(0)
    if len(best[0]) > limit:
        # Even the capsule plus a trim notice overflows. The capsule budget was
        # computed to prevent this, so reaching here means `limit` changed under
        # us; deliver the capsule alone and say so.
        envelope = _envelope("ok", capsule)
        rec.body_delivered_chars = 0
        rec.envelope_chars = len(envelope)
        rec.error = (rec.error + " " if rec.error else "") + (
            f"the skill body was dropped entirely: capsule plus trim notice "
            f"already exceeds the {limit}-character budget")
        return envelope, rec
    for _ in range(MAX_FIT_ROUNDS):
        if lo >= hi:
            break
        mid = (lo + hi + 1) // 2
        trial = attempt(mid)
        if len(trial[0]) <= limit:
            best, lo = trial, mid
        else:
            hi = mid - 1
    envelope, delivered = best
    rec.body_delivered_chars = delivered
    rec.envelope_chars = len(envelope)
    return envelope, rec


class ConstraintAwareLoadSkill:
    """`load_skill`, with the skill's recorded constraints prepended.

    Duck-typed against `BaseTool` rather than subclassing it, so `cf` stays
    importable with no Vibe-Trading checkout on the path. The registry needs
    `name`, `execute`, `to_openai_schema` and `is_readonly`; all four come from
    or delegate to the wrapped tool.
    """

    def __init__(self, inner, cf: ConstraintFlow, *,
                 limit: int = TOOL_RESULT_LIMIT) -> None:
        self._inner = inner
        self._cf = cf
        self._limit = limit
        #: Every delivery this tool performed, in call order. The verifier reads
        #: this; the model never sees it.
        self.deliveries: list[DeliveryRecord] = []

    # -- the BaseTool surface, delegated so it cannot drift ----------------
    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self) -> dict:
        return self._inner.parameters

    @property
    def repeatable(self) -> bool:
        return getattr(self._inner, "repeatable", False)

    @property
    def is_readonly(self) -> bool:
        return getattr(self._inner, "is_readonly", True)

    def to_openai_schema(self) -> dict:
        return self._inner.to_openai_schema()

    # -- execution ---------------------------------------------------------
    def execute(self, **kwargs) -> str:
        """Load the skill through the stock tool, then prepend its constraints.

        Every failure mode falls back to the stock result. An integration that
        can break `load_skill` is worse than one that occasionally delivers no
        constraints, and the fallback is recorded rather than swallowed.
        """
        stock = self._inner.execute(**kwargs)
        name = kwargs.get("name", "")
        try:
            parsed = json.loads(stock)
        except json.JSONDecodeError:
            self.deliveries.append(DeliveryRecord(
                skill_name=str(name), limit=self._limit, envelope_chars=len(stock),
                error="the wrapped tool did not return JSON; passed through"))
            return stock
        if parsed.get("status") != "ok":
            self.deliveries.append(DeliveryRecord(
                skill_name=str(name), limit=self._limit, envelope_chars=len(stock),
                error=f"the wrapped tool reported {parsed.get('status')!r}; "
                      f"passed through"))
            return stock

        skill_id, options = self._cf.resolve(str(name))
        if not skill_id:
            self.deliveries.append(DeliveryRecord(
                skill_name=str(name), limit=self._limit, envelope_chars=len(stock),
                error=(f"{name!r} does not resolve to one skill in the Constraint "
                       f"Store" + (f"; candidates: {', '.join(options)}"
                                   if options else "")) ))
            return stock

        records = self._cf.get_constraints(skill_id)
        envelope, rec = build_envelope(
            str(name), parsed.get("content", ""), records,
            limit=self._limit, skill_id=skill_id)
        self.deliveries.append(rec)
        return envelope

    @property
    def last_delivery(self) -> DeliveryRecord | None:
        return self.deliveries[-1] if self.deliveries else None


def harness_limit(default: int = TOOL_RESULT_LIMIT) -> int:
    """`TOOL_RESULT_LIMIT` from the harness on the path, else *default*.

    Read from upstream rather than hard-coded, because the whole point of
    sizing the envelope is to agree with the cut the harness will actually
    apply. A stale constant here would silently reintroduce the truncation this
    module exists to avoid.
    """
    try:
        from src.agent.loop import TOOL_RESULT_LIMIT as limit  # type: ignore
    except Exception:
        return default
    return int(limit)


def install(registry, project_root: Path, *, limit: int | None = None
            ) -> ConstraintAwareLoadSkill:
    """Replace `load_skill` in *registry* with the constraint-aware version.

    Returns the installed tool, which carries the delivery records. Raises if
    the registry has no `load_skill` to wrap — silently registering a tool the
    harness was not already offering would change which tools the model sees,
    which is a bigger change than this phase is allowed to make.
    """
    inner = registry.get("load_skill")
    if inner is None:
        raise KeyError("this registry has no `load_skill` tool to wrap; "
                       "ConstraintRelay attaches to the skill-selection path "
                       "and does not add a tool of its own")
    if isinstance(inner, ConstraintAwareLoadSkill):
        return inner
    tool = ConstraintAwareLoadSkill(
        inner, ConstraintFlow.open(project_root),
        limit=limit if limit is not None else harness_limit())
    registry.register(tool)
    return tool


def uninstall(registry) -> None:
    """Put the stock `load_skill` back. `install` is reversible by design."""
    current = registry.get("load_skill")
    if isinstance(current, ConstraintAwareLoadSkill):
        registry.register(current._inner)
