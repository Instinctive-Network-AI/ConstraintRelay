"""v2 layer 2 — the current task's constraints, recorded with their origin.

The library holds what a *skill* requires. This module holds what the *current
task* requires, which the library cannot know. Three kinds, kept apart because
conflating them is how a model's guess acquires a user's authority:

    user_explicit    the user said it. Keeps the verbatim words and where.
    system_default   a standing policy. No user utterance behind it.
    model_inferred   the model's guess. Lowest authority, always labelled.

`policy_origin` is **supplied by the caller, never inferred from the text.**
Nothing here reads a record's prose to decide what kind it is.

Interface test, not an extraction test
--------------------------------------
This module records and carries structured constraints. Where those structures
come from is somebody else's problem, and in the v2 first closure they come from
a hand-written fixture. **Exercising this module proves recording and carriage.
It proves nothing about natural-language task-constraint extraction**, which is
unbuilt and unmeasured. Any report that mixes the two is wrong.

A revoked user instruction keeps its provenance
-----------------------------------------------
Superseding does not delete. `T1` superseded by `T4` stays in the record with
its verbatim quote and its (source, span) intact, marked `superseded_by=T4` and
absent from `active()`. "What did the user originally say, and when did they
change it?" has to stay answerable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

USER_EXPLICIT = "user_explicit"
SYSTEM_DEFAULT = "system_default"
MODEL_INFERRED = "model_inferred"
POLICY_ORIGINS = (USER_EXPLICIT, SYSTEM_DEFAULT, MODEL_INFERRED)

#: Authority, strongest first. A lower-authority record may not supersede a
#: higher-authority one — a model's inference cannot retract a user's words.
_AUTHORITY = {USER_EXPLICIT: 0, SYSTEM_DEFAULT: 1, MODEL_INFERRED: 2}
AUTHORITY_LABEL = {USER_EXPLICIT: "highest", SYSTEM_DEFAULT: "middle",
                   MODEL_INFERRED: "lowest"}


class TaskScopeError(Exception):
    """An invalid task-constraint record or an illegal supersede."""


@dataclass(frozen=True)
class TaskConstraint:
    """One constraint that applies to the current task."""

    ref: str
    policy_origin: str
    text: str
    #: For `user_explicit`: the user's own words, verbatim. Empty otherwise.
    verbatim_quote: str = ""
    #: Where the quote came from — a turn id, a policy key, or "inference".
    quote_source: str = ""
    #: Offsets of the quote inside `quote_source`'s text, when known.
    quote_span: "tuple[int, int] | None" = None
    recorded_at: str = ""
    #: Set when a later record replaces this one. Never deletes.
    superseded_by: str = ""
    #: Set when the user retracts without replacement.
    revoked_by: str = ""
    #: The three qualifiers, mirroring `cf/library.py`'s entry fields so a task
    #: constraint and a skill constraint carry the same shape.
    #:
    #: Added 2026-10-14 for the task-extraction scenarios, which test exactly
    #: the failure these fields exist to make visible: a condition folded into
    #: `text` reads as unconditional, and an exception dropped turns "all of
    #: them except the summary" into "all of them". `exception` has never been
    #: measured anywhere in this project.
    #:
    #: Empty means **not extracted**, never "none". A reader who needs the
    #: difference has `text` and `verbatim_quote` to go back to.
    condition: str = ""
    exception: str = ""
    scope: str = ""

    @property
    def qualified(self) -> bool:
        return bool(self.condition or self.exception or self.scope)

    @property
    def authority(self) -> str:
        return AUTHORITY_LABEL[self.policy_origin]

    @property
    def active(self) -> bool:
        return not self.superseded_by and not self.revoked_by

    def as_dict(self) -> dict:
        return {"ref": self.ref, "policy_origin": self.policy_origin,
                "text": self.text, "verbatim_quote": self.verbatim_quote,
                "quote_source": self.quote_source,
                "quote_span": (list(self.quote_span) if self.quote_span
                               else None),
                "recorded_at": self.recorded_at,
                "superseded_by": self.superseded_by,
                "revoked_by": self.revoked_by,
                "condition": self.condition, "exception": self.exception,
                "scope": self.scope,
                "authority": self.authority}

    @staticmethod
    def from_dict(d: dict) -> "TaskConstraint":
        qs = d.get("quote_span")
        return TaskConstraint(
            ref=d["ref"], policy_origin=d["policy_origin"], text=d["text"],
            verbatim_quote=d.get("verbatim_quote", ""),
            quote_source=d.get("quote_source", ""),
            quote_span=(tuple(qs) if qs else None),
            recorded_at=d.get("recorded_at", ""),
            superseded_by=d.get("superseded_by", ""),
            revoked_by=d.get("revoked_by", ""),
            condition=d.get("condition", ""),
            exception=d.get("exception", ""), scope=d.get("scope", ""))


@dataclass
class TaskRecord:
    """Everything the current task is constrained by, plus which skills it names."""

    task_id: str
    description: str = ""
    skills: tuple = ()
    tool_names: tuple = ()
    action_tags: tuple = ()
    constraints: list = field(default_factory=list)   # list[TaskConstraint]
    #: Set true by the caller when `constraints` came from a hand-written
    #: fixture rather than from extraction. Carried into every report.
    structured_input: bool = False

    def add(self, c: TaskConstraint) -> TaskConstraint:
        if c.policy_origin not in POLICY_ORIGINS:
            raise TaskScopeError(
                f"policy_origin {c.policy_origin!r} is not one of "
                f"{list(POLICY_ORIGINS)}. The origin is supplied, never "
                "inferred from the text.")
        if c.policy_origin == USER_EXPLICIT and not c.verbatim_quote.strip():
            raise TaskScopeError(
                f"{c.ref}: a user_explicit constraint must carry the user's "
                "verbatim words. Without them 'what exactly did they say?' is "
                "unanswerable.")
        if c.policy_origin == USER_EXPLICIT and not c.quote_source.strip():
            raise TaskScopeError(
                f"{c.ref}: a user_explicit constraint must name where the "
                "quote came from.")
        if any(x.ref == c.ref for x in self.constraints):
            raise TaskScopeError(f"duplicate ref {c.ref!r}")
        self.constraints.append(c)
        return c

    def by_ref(self, ref: str) -> "TaskConstraint | None":
        for c in self.constraints:
            if c.ref == ref:
                return c
        return None

    def supersede(self, old_ref: str, new_ref: str) -> None:
        """`new_ref` replaces `old_ref`. The old record is kept, not deleted."""
        old, new = self.by_ref(old_ref), self.by_ref(new_ref)
        if old is None:
            raise TaskScopeError(f"no record {old_ref!r} to supersede")
        if new is None:
            raise TaskScopeError(f"no record {new_ref!r} to supersede with")
        if _AUTHORITY[new.policy_origin] > _AUTHORITY[old.policy_origin]:
            raise TaskScopeError(
                f"{new_ref} ({new.policy_origin}, authority "
                f"{new.authority}) may not supersede {old_ref} "
                f"({old.policy_origin}, authority {old.authority}). A "
                "lower-authority origin cannot retract a higher one — a "
                "model's inference does not cancel a user's instruction.")
        if old.superseded_by:
            raise TaskScopeError(
                f"{old_ref} is already superseded by {old.superseded_by}")
        i = self.constraints.index(old)
        self.constraints[i] = TaskConstraint(
            **{**{k: v for k, v in old.as_dict().items() if k != "authority"},
               "quote_span": old.quote_span, "superseded_by": new_ref})

    def revoke(self, ref: str, by: str) -> None:
        """The user retracts without replacement. Provenance is kept."""
        rec = self.by_ref(ref)
        if rec is None:
            raise TaskScopeError(f"no record {ref!r} to revoke")
        if not by.strip():
            raise TaskScopeError("a revocation must name its source")
        i = self.constraints.index(rec)
        self.constraints[i] = TaskConstraint(
            **{**{k: v for k, v in rec.as_dict().items() if k != "authority"},
               "quote_span": rec.quote_span, "revoked_by": by})

    def active(self) -> list:
        return [c for c in self.constraints if c.active]

    def by_origin(self) -> dict:
        """Active records grouped by origin. Three keys, always present."""
        out = {o: [] for o in POLICY_ORIGINS}
        for c in self.active():
            out[c.policy_origin].append(c)
        return out

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "description": self.description,
                "skills": list(self.skills),
                "tool_names": list(self.tool_names),
                "action_tags": list(self.action_tags),
                "structured_input": self.structured_input,
                "_kind": ("INTERFACE TEST — constraints are hand-written "
                          "structured input, not extracted from natural "
                          "language" if self.structured_input else
                          "constraints were produced by extraction"),
                "constraints": [c.as_dict() for c in self.constraints]}

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "TaskRecord":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        rec = TaskRecord(task_id=raw["task_id"],
                         description=raw.get("description", ""),
                         skills=tuple(raw.get("skills", ())),
                         tool_names=tuple(raw.get("tool_names", ())),
                         action_tags=tuple(raw.get("action_tags", ())),
                         structured_input=bool(raw.get("structured_input")))
        rec.constraints = [TaskConstraint.from_dict(c)
                           for c in raw.get("constraints", [])]
        return rec
