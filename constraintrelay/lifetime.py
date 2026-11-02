"""Phase 3 — constraint lifetime: the active-skill constraint set.

Phase 2 delivered a skill's constraints once, inside the `load_skill` tool
result. That is **load-time-only delivery**, and `loop.py` ends it on a long run
in three different ways:

* `_microcompact` replaces every tool result but the last `KEEP_RECENT = 3`
  with the literal string `[cleared]`, once the transcript passes half the
  token threshold;
* `_context_collapse` folds any message longer than `COLLAPSE_TEXT_MIN` down to
  its first 900 and last 500 characters;
* `_auto_compact` calls `messages.clear()` and rebuilds the list from a
  model-written summary plus a token-budgeted tail.

A constraint delivered once, in a tool result, does not survive any of them. So
the constraint is visible when the skill is *loaded* and gone by the time the
skill is *used* — which is the gap between `delivered` and `representable` the
project exists to close, reopened one layer further in.

The replacement
---------------
An **active-skill constraint set**. A skill becomes active when `load_skill`
returns it, and from then on its constraints are re-stated in **every** model
request for the rest of the run. Re-statement, not persistence: nothing is
expected to survive compaction, so nothing has to.

The injection point is `ChatLLM.stream_chat`, wrapped the same way Phase 2
wrapped the tool. That position matters: all three compaction layers run at the
top of a loop iteration and `stream_chat` is called at the bottom of it, so the
wrapper sees the message list **after** compaction has done whatever it was
going to do, and the constraints it adds cannot be removed before the request
goes out.

Two deliberate differences from Phase 2
---------------------------------------
**The tool result is left alone.** `SkillActivationTracker` registers the skill
and returns the stock envelope byte-for-byte. Phase 2 prepended a capsule inside
the tool result and trimmed the skill body to make room, which cost 51,543
characters of author text across the corpus and newly truncated 5 skills. None
of that is needed once the constraints live in their own per-turn message, so
that cost goes to zero. What replaces it is a per-turn token cost, which is what
Phase 3's measurement is about.

The timing is unchanged, which is why this is a replacement and not a
regression: the tool runs during iteration *N*, its result is appended, and the
first request that carries it is iteration *N+1*'s — the same request the
wrapper injects into. The constraints arrive on exactly the turn they used to.

**The caller's message list is not mutated.** The wrapper builds
`list(messages) + [injection]` and hands *that* to the inner client. The loop's
own list stays clean, so the injection never accumulates, never needs removing,
is never written into `transcript_*.jsonl`, and is never fed to `_auto_compact`'s
summarizer as if it were something the conversation said.

The cost of that choice is an accounting gap, and it is reported rather than
hidden: `estimate_tokens(messages)` at the top of the next iteration does not
see the injection, so the loop's compaction thresholds are computed against a
list smaller than the request it actually sent. `fieldtest/phase3` measures the
gap.

Phase 4 additions
-----------------
Three, all of them about overhead *correctness* rather than overhead
optimization:

* **An explicit lifecycle.** Phase 3 had a `deactivate` method that nothing
  called, so in practice a skill was active for the rest of the run and the set
  only grew. Phase 4 gives the lifecycle states (`active` / `released`), a
  recorded transition history, and two ways to drive it: `ActiveSkillSet.release`
  for a host, and a `release_skill` tool for the model. A released skill stops
  contributing constraints immediately and can be re-activated by loading it
  again.
* **Deterministic exact deduplication.** Two active skills can hold
  byte-identical canonical text. The first occurrence in activation order
  carries every duplicate's id on its line and the later skills omit it. Exact,
  not semantic: the key is the canonical text itself. On the Vibe-Trading corpus
  this saves between 0 and 88 characters depending on the active set, because
  only 2 of 463 distinct texts are duplicated at all — so it is insurance
  against a corpus with shared boilerplate, not a saving on this one, and
  §4 of `PHASE4.md` says so.
* **Token accounting**, in `cf/accounting.py`, which is a separate module
  because it is the only part that has to touch the harness.

Phase 5 additions
-----------------
Lifecycle and audit only. The semantics are deliberately unchanged:

* `load_skill` activates;
* explicit `release_skill` (the model) or `ActiveSkillSet.release` (a host)
  deactivates;
* **session/task termination resets**, via `task_scope`.

**There is no timeout-based deactivation and there will not be one here.** A
skill untouched for twenty turns is as active as one used last turn, because
"unused" and "no longer applicable" are different claims and only the second
justifies dropping a constraint. Inferring the second from the first is the
heuristic this phase was told not to introduce.

What Phase 5 adds is the record: every `Transition` now carries its `source`
(`load_skill` / `release_skill` / `host` / `task_end`), the `turn` it happened
on, and the `active_size_after`, so an audit of the working set can be read off
`audit_rows()` rather than reconstructed from prose. `ConstraintAwareLLM` keeps
a per-turn `timeline`, and `working_set_stats()` reduces it to the peak and
median figures `fieldtest/phase5` reports.

What this module does not do
----------------------------
No compression, no ranking, no selection, no *automatic* deactivation. The whole
active set is restated every turn, in activation order, in full. A skill leaves
the set only when a host or the model says so, or when the task ends — deciding
*when* a constraint stops applying by inference would be a policy, and inventing
one here would be the decision made silently.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .deliver import CAPSULE_CLOSE, render_capsule
from .query import ConstraintFlow
from . import skillstore
from .skillstore import ConstraintRecord

#: Opening tag of the injected message. Also the marker used to recognise an
#: injection in a captured request, so a verifier does not have to guess.
ACTIVE_OPEN = ('<constraintflow-active-constraints skills="{skills}" '
               'constraints="{constraints}">')
ACTIVE_CLOSE = "</constraintflow-active-constraints>"

#: Why the block is there, addressed to the model. Phrased as a statement of
#: record rather than an instruction to obey, for the same reason Phase 1's
#: capsule header is: this layer delivers constraints and claims nothing about
#: enforcement.
ACTIVE_PREAMBLE = (
    "Constraints recorded for the skills loaded in this run. They are restated "
    "in full on every turn because the original load_skill results are "
    "compacted, collapsed or cleared as the conversation grows; this block is "
    "the durable copy, not a reminder of one."
)

#: `user`, not `system`. The harness itself injects mid-conversation context as
#: a user message — `<background-results>` and the `[SYSTEM] You have N
#: iterations remaining` nudge are both `role: user` — and a mid-conversation
#: system message is the form providers disagree about most.
INJECTION_ROLE = "user"


ACTIVE = "active"
RELEASED = "released"
CLEARED = "cleared"

#: Where a transition came from. Phase 5 requires these to be recorded rather
#: than inferred from a free-text reason, because "who deactivated this skill"
#: is the question an audit of the working set has to answer.
SRC_LOAD_SKILL = "load_skill"          # the model loaded it
SRC_RELEASE_SKILL = "release_skill"    # the model released it
SRC_HOST = "host"                      # a host called activate/release directly
SRC_TASK_END = "task_end"              # session/task termination reset the set
SOURCES = (SRC_LOAD_SKILL, SRC_RELEASE_SKILL, SRC_HOST, SRC_TASK_END)


@dataclass(frozen=True)
class Transition:
    """One lifecycle event, recorded so the set's state is explainable.

    Carries everything Phase 5 asks an audit to answer: what changed, who
    changed it, on which turn, and how large the active set was afterwards.
    `reason` stays free text for the human-readable detail; `source` is the
    machine-readable half, so a report never has to parse prose to find out
    whether the model or the host released a skill.
    """

    skill_id: str
    to_state: str
    reason: str
    version: int
    source: str = SRC_HOST
    turn: int = 0
    active_size_after: int = 0

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "to_state": self.to_state,
                "reason": self.reason, "version": self.version,
                "source": self.source, "turn": self.turn,
                "active_size_after": self.active_size_after}


@dataclass
class LifetimeOptions:
    """What the active set does beyond restating constraints.

    Both default on, because both are corrections. `LEGACY_PHASE3` below turns
    them off, and exists so the Phase-4 measurement can run the same scenario
    under the old behaviour instead of comparing against a remembered number.
    """

    #: Collapse byte-identical canonical text across active skills.
    dedup: bool = True
    #: Register a `release_skill` tool so the model can end a skill's lifetime.
    release_tool: bool = True


#: Phase 3's behaviour, named so the before/after comparison is a code path and
#: not a recollection.
LEGACY_PHASE3 = LifetimeOptions(dedup=False, release_tool=False)


@dataclass
class ActiveSkillSet:
    """Which skills are active, in activation order, and what happened to them.

    Order is activation order and not, for instance, constraint count: the set
    is restated in full, so there is nothing for an ordering to optimise, and a
    stable order makes two requests in the same run diffable. Any other order
    would be the first step of a ranking policy.

    `version` increments on every state change. It is the cache key for the
    rendered capsule, which is what lets the token accounting and the request
    share one object rather than two equal ones — see `cf/accounting.py`.
    """

    ids: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    version: int = 0
    history: list[Transition] = field(default_factory=list)
    #: The turn a transition is attributed to. Pushed in by
    #: `ConstraintAwareLLM` before each request, so a `load_skill` during
    #: iteration N is recorded against turn N — the turn whose request asked
    #: for it — rather than against the turn whose request will carry it.
    turn: int = 0

    def note_turn(self, turn: int) -> None:
        self.turn = turn

    def _record(self, skill_id: str, state: str, reason: str, source: str
                ) -> None:
        self.version += 1
        self.history.append(Transition(
            skill_id, state, reason, self.version, source=source,
            turn=self.turn, active_size_after=len(self.ids)))

    def state(self, skill_id: str) -> str:
        if skill_id in self.ids:
            return ACTIVE
        if skill_id in self.released:
            return RELEASED
        return ""

    def activate(self, skill_id: str, reason: str = SRC_LOAD_SKILL, *,
                 source: str = SRC_LOAD_SKILL) -> bool:
        """Make *skill_id* active. True when the state changed.

        Re-activating a released skill is allowed and is the point of keeping
        `released` separate from "never seen": a model that releases a skill and
        then loads it again gets its constraints back, and the history says so.
        """
        if not skill_id or skill_id in self.ids:
            return False
        if skill_id in self.released:
            self.released.remove(skill_id)
            self.ids.append(skill_id)
            self._record(skill_id, ACTIVE, f"re-{reason}", source)
            return True
        self.ids.append(skill_id)
        self._record(skill_id, ACTIVE, reason, source)
        return True

    def release(self, skill_id: str, reason: str = "explicit", *,
                source: str = SRC_HOST) -> bool:
        """End *skill_id*'s lifetime. Its constraints stop being injected.

        Explicit by construction: there is no caller inside this module that
        decides to. A host calls it, the model calls `release_skill`, or a task
        ends. Nothing *infers* that a skill has stopped mattering, and in
        particular there is no timeout — a skill untouched for twenty turns is
        as active as one used last turn, because "unused" and "no longer
        applicable" are different claims and only the second justifies dropping
        a constraint.
        """
        if skill_id not in self.ids:
            return False
        self.ids.remove(skill_id)
        self.released.append(skill_id)
        self._record(skill_id, RELEASED, reason, source)
        return True

    def reset(self, reason: str = "task ended", *,
              source: str = SRC_TASK_END) -> list[str]:
        """Clear the set at a session or task boundary. Returns what was active.

        The third conservative deactivation path, and the only one that is not
        per-skill: when a task ends, every skill it loaded stops applying,
        because the thing they were loaded *for* is over. One transition is
        recorded per cleared skill so the audit does not have to special-case a
        bulk event, and `released` is cleared too — a new task starts with no
        memory of the last one's working set, not with a list of skills it never
        loaded.
        """
        cleared = list(self.ids)
        for skill_id in cleared:
            self.ids.remove(skill_id)
            self._record(skill_id, CLEARED, reason, source)
        self.released.clear()
        if not cleared:
            # Still a version bump: a caller that resets an empty set has
            # changed nothing, and the capsule cache must not be invalidated
            # for nothing either. So: no record, no bump.
            pass
        return cleared

    #: Phase 3's name for `release`, kept so existing callers keep working.
    def deactivate(self, skill_id: str, reason: str = "explicit") -> bool:
        return self.release(skill_id, reason)

    def __len__(self) -> int:
        return len(self.ids)

    # -- audit ------------------------------------------------------------
    def transitions(self, *, source: str | None = None) -> list[Transition]:
        return [t for t in self.history
                if source is None or t.source == source]

    def audit_rows(self) -> list[dict]:
        """The transition log as flat rows — Phase 5 goal 2, one line each."""
        return [t.as_dict() for t in self.history]

    @property
    def log(self) -> list[tuple[str, str]]:
        """Phase 3's `(skill_id, reason)` view, retained for its callers."""
        return [(t.skill_id, t.reason if t.to_state == ACTIVE
                 else f"deactivated: {t.reason}") for t in self.history]

    def as_dict(self) -> dict:
        return {"active": list(self.ids), "released": list(self.released),
                "version": self.version,
                "history": [t.as_dict() for t in self.history]}


@dataclass
class Injection:
    """One turn's injected message, and what went into it."""

    content: str
    skill_ids: list[str]
    constraint_ids: list[str]
    chars: int
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: `emitted_id -> [ids whose text was identical and therefore not repeated]`
    merged: dict[str, list[str]] = field(default_factory=dict)
    #: Characters the dedup pass did not have to emit.
    deduped_chars: int = 0

    @property
    def message(self) -> dict:
        return {"role": INJECTION_ROLE, "content": self.content}

    @property
    def deduped_count(self) -> int:
        return sum(len(v) for v in self.merged.values())

    def as_dict(self) -> dict:
        return {"chars": self.chars, "skills": list(self.skill_ids),
                "constraint_count": len(self.constraint_ids),
                "constraint_ids": list(self.constraint_ids),
                "skipped": [{"skill_id": s, "reason": r} for s, r in self.skipped],
                "merged": {k: list(v) for k, v in self.merged.items()},
                "deduped_count": self.deduped_count,
                "deduped_chars": self.deduped_chars}


def _deduped_section(skill_name: str, records: "list[ConstraintRecord]",
                     first_of: "dict[str, str]",
                     merged: "dict[str, list[str]]") -> str:
    """One skill's section with duplicates omitted.

    Shaped exactly like `deliver.render_capsule`'s output so the two are
    interchangeable, plus a `merged_from_earlier_skills` attribute when this
    section dropped a line. A dropped constraint is **not** silently gone: its
    id is on the emitted line in the earlier skill's section, and the count here
    says how many went that way.
    """
    lines: list[str] = []
    dropped = 0
    for c in records:
        owner = first_of[c.canonical_text]
        if owner != c.constraint_id:
            dropped += 1
            continue
        ids = " | ".join([c.constraint_id, *merged.get(c.constraint_id, [])])
        lines.append(f"[{ids}] {c.canonical_text}")
    head = (f'<constraintflow-constraints skill="{skill_name}" '
            f'count="{len(lines)}"')
    if dropped:
        head += f' merged_from_earlier_skills="{dropped}"'
    head += ">"
    return "\n".join([head, *lines, CAPSULE_CLOSE])


def render_active(cf: ConstraintFlow, skill_ids: "list[str]", *,
                  dedup: bool = True) -> Injection | None:
    """The injected block for *skill_ids*, or None when there is nothing to say.

    With `dedup=False` each skill's section is produced by
    `deliver.render_capsule` — the same renderer Phase 1's demo and Phase 2's
    tool wrapper use — so a constraint's delivered text is byte-identical
    however it arrives.

    With `dedup=True` that property is preserved for every skill that has no
    duplicate to drop: such a section still comes from `render_capsule`
    verbatim, and only a section that actually dropped a line is rendered here.
    `tests/test_phase4_accounting.py` asserts the byte-identity rather than
    trusting it.

    Dedup is **exact and deterministic**: the key is the canonical text itself,
    not a similarity measure, and the winner is the first occurrence in
    activation order — so the same active set always produces the same bytes.
    """
    rendered: list[tuple[str, list[ConstraintRecord]]] = []
    used: list[str] = []
    constraint_ids: list[str] = []
    skipped: list[tuple[str, str]] = []
    for skill_id in skill_ids:
        rec = cf.skill_record(skill_id)
        if rec is None:
            skipped.append((skill_id, "not in the Constraint Store"))
            continue
        records: list[ConstraintRecord] = cf.get_constraints(skill_id)
        if not records:
            # Not a failure: 8 of the corpus's 89 skills have no recorded
            # constraint. Recorded so "nothing was injected for this skill" is
            # distinguishable from "this skill was forgotten".
            skipped.append((skill_id, "no recorded constraints"))
            continue
        rendered.append((rec.name, records))
        used.append(skill_id)
        constraint_ids += [c.constraint_id for c in records]
    if not rendered:
        return None

    first_of: dict[str, str] = {}
    merged: dict[str, list[str]] = {}
    deduped_chars = 0
    if dedup:
        for _, records in rendered:
            for c in records:
                owner = first_of.get(c.canonical_text)
                if owner is None:
                    first_of[c.canonical_text] = c.constraint_id
                    continue
                merged.setdefault(owner, []).append(c.constraint_id)
                deduped_chars += len(c.canonical_text)

    sections: list[str] = []
    for name, records in rendered:
        if dedup and any(first_of[c.canonical_text] != c.constraint_id
                         or c.constraint_id in merged for c in records):
            sections.append(_deduped_section(name, records, first_of, merged))
        else:
            sections.append(render_capsule(name, records))

    body = "\n".join(sections)
    content = "\n".join([
        ACTIVE_OPEN.format(skills=len(used), constraints=len(constraint_ids)),
        ACTIVE_PREAMBLE, "", body, ACTIVE_CLOSE])
    return Injection(content=content, skill_ids=used,
                     constraint_ids=constraint_ids, chars=len(content),
                     skipped=skipped, merged=merged,
                     deduped_chars=deduped_chars)


class SkillActivationTracker:
    """`load_skill`, unchanged, plus a note of which skill was loaded.

    Duck-typed against `BaseTool` for the same reason Phase 2's wrapper is: `cf`
    must stay importable with no Vibe-Trading checkout on the path. Every
    attribute the registry and the loop read is delegated to the wrapped
    instance, so the model sees a byte-identical tool definition.

    The returned string is the stock one, byte for byte. This tool adds nothing
    to the model's input; delivery is the LLM wrapper's job now.
    """

    def __init__(self, inner, cf: ConstraintFlow, active: ActiveSkillSet) -> None:
        self._inner = inner
        self._cf = cf
        self.active = active
        #: `(requested_name, skill_id or "", note)` per call.
        self.calls: list[tuple[str, str, str]] = []

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

    def execute(self, **kwargs) -> str:
        result = self._inner.execute(**kwargs)
        name = str(kwargs.get("name", ""))
        try:
            import json
            ok = json.loads(result).get("status") == "ok"
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            self.calls.append((name, "", "the wrapped tool did not report ok; "
                                        "no skill activated"))
            return result
        skill_id, options = self._cf.resolve(name)
        if not skill_id:
            self.calls.append((name, "", "does not resolve to one skill in the "
                                         "Constraint Store"
                               + (f"; candidates: {', '.join(options)}"
                                  if options else "")))
            return result
        added = self.active.activate(skill_id)
        self.calls.append((name, skill_id,
                           "activated" if added else "already active"))
        return result


class ReleaseSkillTool:
    """`release_skill` — the model's half of the lifecycle.

    **This adds a tool the harness was not offering**, which Phases 2 and 3
    deliberately refused to do. It is added here because an "explicit
    lifecycle" with no actor inside the run is unreachable: a host can call
    `ActiveSkillSet.release`, but nothing in a Vibe-Trading session can, so
    without this tool the set would still only grow. `LifetimeOptions(
    release_tool=False)` turns it off, and `LEGACY_PHASE3` does.

    Releasing is not deleting. The constraints stay in the store, the skill's
    lifecycle history records the transition, and loading the skill again brings
    them back.
    """

    name = "release_skill"
    description = ("Declare that you are finished with a previously loaded "
                   "skill. Its recorded constraints stop being restated on "
                   "each turn, which frees context. Load the skill again if you "
                   "need it later.")
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string",
                                "description": "Skill name, as passed to "
                                               "load_skill"}},
        "required": ["name"],
    }
    repeatable = True
    is_readonly = True

    def __init__(self, cf: ConstraintFlow, active: ActiveSkillSet) -> None:
        self._cf = cf
        self.active = active
        #: `(requested_name, skill_id or "", note)` per call.
        self.calls: list[tuple[str, str, str]] = []

    def to_openai_schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def execute(self, **kwargs) -> str:
        import json
        name = str(kwargs.get("name", ""))
        skill_id, options = self._cf.resolve(name)
        if not skill_id:
            note = (f"{name!r} does not resolve to one skill in the Constraint "
                    f"Store" + (f"; candidates: {', '.join(options)}"
                                if options else ""))
            self.calls.append((name, "", note))
            return json.dumps({"status": "error", "error": note},
                              ensure_ascii=False)
        declared = self._cf.hold_scope(skill_id)
        # The declared scope is recorded, not enforced. A model saying "I am
        # done with this" is an explicit act and is honoured whatever the
        # declaration says; what the audit gains is the ability to show that a
        # `task`-scoped skill was released early, which is the case a reviewer
        # would want to see.
        reason = ("release_skill" if declared == skillstore.HOLD_PHASE
                  else "release_skill (declared hold_scope=task)")
        if not self.active.release(skill_id, reason=reason,
                                   source=SRC_RELEASE_SKILL):
            note = "was not active"
            self.calls.append((name, skill_id, note))
            return json.dumps(
                {"status": "ok", "skill": name, "released": False,
                 "message": f"{name} was not active; nothing changed."},
                ensure_ascii=False)
        self.calls.append((name, skill_id, "released"))
        return json.dumps(
            {"status": "ok", "skill": name, "released": True,
             "declared_hold_scope": declared,
             "active_skills": len(self.active),
             "message": f"{name} released; its constraints are no longer "
                        f"restated. {len(self.active)} skill(s) still active."},
            ensure_ascii=False)


class ConstraintAwareLLM:
    """`ChatLLM`, with the active-skill constraint set appended to each request.

    `__getattr__` delegates everything not defined here, so the loop's own reads
    — `model_name` in `_new_llm_usage_summary`, and anything a later upstream
    change adds — keep working against the wrapped client rather than silently
    returning the wrapper's own defaults.
    """

    def __init__(self, inner, cf: ConstraintFlow, active: ActiveSkillSet, *,
                 options: LifetimeOptions | None = None) -> None:
        self._inner = inner
        self._cf = cf
        self.active = active
        self.options = options or LifetimeOptions()
        #: One entry per `stream_chat`, including the turns where nothing was
        #: injected. A verifier needs the zeroes as much as the rest.
        self.injections: list[Injection | None] = []
        #: `(turn, active_skill_count, injection_chars, injection_constraints)`
        #: per request — the working-set series Phase 5 measures over time. The
        #: active count and the injected size are kept separate because they
        #: differ: 8 of the corpus's 89 skills carry no constraints, so an
        #: active set of five can inject four sections.
        self.timeline: list[tuple[int, int, int, int]] = []
        self.turn: int = 0
        self._cached: Injection | None = None
        self._cached_version: int = -1

    def __getattr__(self, item):
        return getattr(self._inner, item)

    # -- the capsule -------------------------------------------------------
    def current_injection(self) -> Injection | None:
        """This turn's capsule, memoized on the active set's `version`.

        Memoized for correctness, not speed. `cf/accounting.py` asks what will
        be injected so the harness can budget for it, and `build_request` then
        injects it; if those two rendered separately they could differ, and the
        accounting would be correcting for a capsule that was never sent. The
        active set can only change during tool execution — which happens *after*
        `stream_chat` — so one memo per version is exact, and the test asserts
        the two callers get the *same object*.
        """
        if self._cached_version != self.active.version:
            self._cached = render_active(self._cf, self.active.ids,
                                         dedup=self.options.dedup)
            self._cached_version = self.active.version
        return self._cached

    def pending_messages(self, messages: "list[dict]") -> "list[dict]":
        """*messages* as they will be sent. Returns *messages* itself when
        there is nothing to inject, which is how the accounting patch tells
        "no capsule" from "a capsule of zero length"."""
        injection = self.current_injection()
        if injection is None:
            return messages
        return list(messages) + [injection.message]

    # -- the request path --------------------------------------------------
    def build_request(self, messages: "list[dict]") -> "list[dict]":
        """`messages` plus this turn's injection. Does not mutate *messages*.

        Also advances the turn counter and pushes it into the active set, so a
        transition caused by a tool call during this iteration is attributed to
        this turn. `build_request` is the right place for that because it is
        called once per model request, which is what a "turn" means here.
        """
        self.turn += 1
        self.active.note_turn(self.turn)
        injection = self.current_injection()
        self.injections.append(injection)
        self.timeline.append((
            self.turn, len(self.active),
            injection.chars if injection else 0,
            len(injection.constraint_ids) if injection else 0))
        if injection is None:
            return list(messages)
        return list(messages) + [injection.message]

    def stream_chat(self, messages, tools=None, on_text_chunk=None,
                    on_reasoning_chunk=None, timeout=None, should_cancel=None):
        return self._inner.stream_chat(
            self.build_request(messages), tools=tools,
            on_text_chunk=on_text_chunk, on_reasoning_chunk=on_reasoning_chunk,
            timeout=timeout, should_cancel=should_cancel)

    def chat(self, messages, tools=None, timeout=None):
        """Passed through **without** injection.

        `chat` is not the agent's request path in this harness. Its one caller is
        `_auto_compact`, which sends a single `user` message asking the model to
        summarize the transcript. Injecting constraints there would pay for them
        twice and, worse, invite the summarizer to fold them into the summary —
        at which point a constraint would be represented in the conversation as
        something the conversation *said*, paraphrased, with the store's wording
        lost. The active set is restated from the store every turn and needs no
        help from a summary.
        """
        return self._inner.chat(messages, tools=tools, timeout=timeout)

    # -- reporting ---------------------------------------------------------
    @property
    def last_injection(self) -> Injection | None:
        return self.injections[-1] if self.injections else None

    def injected_turns(self) -> int:
        return sum(1 for i in self.injections if i is not None)

    def injected_chars(self) -> int:
        return sum(i.chars for i in self.injections if i is not None)

    def working_set_stats(self, *, chars_per_token: int = 4) -> dict:
        """Peak and median working-set size and per-turn cost — Phase 5 goal 3.

        Median rather than mean: a trajectory that loads five skills over five
        turns and then works for forty spends almost all of its turns at the
        plateau, and a mean would be dragged down by the ramp. The peak is
        reported alongside because it is the figure a context budget has to
        survive.
        """
        import statistics
        if not self.timeline:
            return {"turns": 0}
        active = [row[1] for row in self.timeline]
        chars = [row[2] for row in self.timeline]
        return {
            "turns": len(self.timeline),
            "active_peak": max(active),
            "active_median": statistics.median(active),
            "active_final": active[-1],
            "per_turn_chars_peak": max(chars),
            "per_turn_chars_median": statistics.median(chars),
            "per_turn_est_tokens_peak": max(chars) // chars_per_token,
            "per_turn_est_tokens_median": int(statistics.median(chars))
            // chars_per_token,
            "total_injected_chars": sum(chars),
            "total_injected_est_tokens": sum(chars) // chars_per_token,
            "series": [{"turn": t, "active": a, "chars": c, "constraints": k}
                       for t, a, c, k in self.timeline],
        }


def install(registry, llm, project_root: Path, *,
            options: LifetimeOptions | None = None
            ) -> tuple[SkillActivationTracker, ConstraintAwareLLM, ActiveSkillSet]:
    """Wire the active set into a registry and an LLM client.

    Returns `(tool, llm, active)`. The returned client has to be the one handed
    to `AgentLoop`, because the loop captures `self.llm` at construction:

        tool, llm, active = lifetime.install(registry, ChatLLM(), repo)
        AgentLoop(registry, llm).run(...)

    `ToolRegistry.register` keyed on `tool.name` and `AgentLoop(registry, llm)`
    are both construction sites the harness already has, so no file in the
    Vibe-Trading checkout changes. `options.release_tool` is the one thing here
    that changes the model's tool set — see `ReleaseSkillTool`.

    Raises when the registry has no `load_skill`: ConstraintFlow attaches to the
    skill-selection path and has nothing to attach to without it.
    """
    opts = options or LifetimeOptions()
    inner = registry.get("load_skill")
    if inner is None:
        raise KeyError("this registry has no `load_skill` tool to wrap; "
                       "ConstraintRelay attaches to the skill-selection path "
                       "and does not add a tool of its own")
    cf = ConstraintFlow.open(project_root)
    active = ActiveSkillSet()
    if isinstance(inner, SkillActivationTracker):
        tool = inner
        active = inner.active
    else:
        tool = SkillActivationTracker(inner, cf, active)
        registry.register(tool)
    if opts.release_tool and not isinstance(registry.get("release_skill"),
                                            ReleaseSkillTool):
        registry.register(ReleaseSkillTool(cf, active))
    wrapped = llm if isinstance(llm, ConstraintAwareLLM) \
        else ConstraintAwareLLM(llm, cf, active, options=opts)
    return tool, wrapped, active


def uninstall(registry) -> None:
    """Put the stock `load_skill` back and remove `release_skill`.

    The LLM wrapper is dropped by the caller the same way it was supplied — by
    handing the inner client to `AgentLoop`. Token accounting is reversed by
    `constraintrelay.accounting.AccountingPatch.uninstall`.
    """
    current = registry.get("load_skill")
    if isinstance(current, SkillActivationTracker):
        registry.register(current._inner)
    if isinstance(registry.get("release_skill"), ReleaseSkillTool):
        # `ToolRegistry` has no `unregister`; its backing dict is the only way
        # to take a tool away, and leaving `release_skill` behind after an
        # uninstall would leave the model a tool with nothing behind it.
        try:
            del registry._tools["release_skill"]
        except (AttributeError, KeyError):
            pass


@contextmanager
def task_scope(active: ActiveSkillSet, *, reason: str = "task ended"):
    """Reset *active* when the task ends — the third conservative path.

        with lifetime.task_scope(active):
            AgentLoop(registry, llm).run(user_message)

    A context manager, and host-driven, because the harness gives no other
    handle on a task boundary: `AgentLoop.run`'s `end` event goes to the trace
    writer, not to `event_callback`, so there is nothing to subscribe to. The
    alternative would be a second monkeypatch — wrapping `run` — and Phase 4's
    patch is already recorded as adapter debt; adding more of it to learn a fact
    the caller already knows would be the wrong trade.

    Resets on the way out whether the run succeeded or raised: a task that
    failed is still over, and leaving its skills active would carry them into
    whatever runs next.
    """
    try:
        yield active
    finally:
        active.reset(reason)


def find_injection(messages: "list[dict]") -> dict | None:
    """The ConstraintFlow injection in a captured request, if any.

    Exported so a verifier identifies the block by the marker this module
    actually emits rather than by re-deriving its shape.
    """
    marker = ACTIVE_OPEN.split(" ")[0]
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, str) and content.startswith(marker):
            return msg
    return None
