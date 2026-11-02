"""v2 layer 4 — delivery at the call decision point, and the record it leaves.

Phase 3 (`cf/lifetime.py`) re-states a skill's constraints in **every model
request**, injecting at `ChatLLM.stream_chat` after all three compaction layers.
That is the right discipline and the wrong position for v2: the Owner's shape
delivers at the **call decision point**, which on the Vibe-Trading loop is
`agent/src/agent/loop.py:1063 _process_tool_calls`, immediately before
`:1254 _invoke_tool`.

This module is the harness-independent half: it renders what to deliver and
writes the record. The loop wrap is a separate, platform-specific adapter, so
the acceptance scenarios can exercise delivery without running an agent.

Delivery is not compliance
--------------------------
A `DeliveryRecord` says the capsule was produced and handed to the call site at
a given moment with a given content hash. **It does not say the agent obeyed**,
and there is no field in which it could: nothing here observes the agent's
subsequent behaviour. `S2` passing means the rule reached the decision point.

Platform capability is recorded per version and per entry point
---------------------------------------------------------------
`PLATFORM_NOTES` records what was checked, where, and at which version. One
hook's inability to block is a fact about that hook at that version, not about
the platform: OpenClaw `2026.9.1`'s `llm_input` is dispatched with `void` and so
cannot delay or stop a call, which says nothing about any other OpenClaw entry
point, nor about any other version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .library import HUMAN_REVIEWED
from .selector import COVERED, Selection, verify_traceability

#: Verified entry points, each pinned to the version it was read at. Scope is
#: the entry point, never the product.
PLATFORM_NOTES = {
    "vibe-trading@dddf96e": {
        "delivery_point": "agent/src/agent/loop.py:1063 _process_tool_calls, "
                          "before :1254 _invoke_tool",
        "synchronous": True,
        "can_block": True,
        "basis": "in-process Python called synchronously by the loop; "
                 "source read on this host at the pinned commit",
        "scope_note": "this statement covers this entry point at this commit "
                      "only",
    },
    "openclaw@2026.9.1": {
        "delivery_point": "llm_input (attempt-prompt-support.ts:341-360)",
        "synchronous": False,
        "can_block": False,
        "basis": "dispatched with `void`, so it is not awaited and cannot "
                 "delay or stop the call; recorded in "
                 "HOOK_EXPERIMENT_ACCEPTANCE.md from source read on this host",
        "scope_note": "this is a property of THIS hook at THIS version. It is "
                      "not a statement about OpenClaw's other entry points or "
                      "other versions.",
    },
}


class DeliveryError(Exception):
    """A delivery that must not proceed as requested."""


@dataclass
class DeliveredRule:
    """One rule as handed to the call site, with its full provenance."""

    constraint_id: str
    constraint_version: int
    skill_id: str
    source_version: str
    source_file: str
    source_span: tuple
    source_hash: str
    file_content_sha256: str
    build_run_id: str
    review_tier: str
    text: str
    condition: str = ""
    exception: str = ""
    scope: str = ""
    condition_adjudicated: bool = False

    def as_dict(self) -> dict:
        return {"constraint_id": self.constraint_id,
                "constraint_version": self.constraint_version,
                "skill_id": self.skill_id,
                "source_version": self.source_version,
                "source_file": self.source_file,
                "source_span": list(self.source_span),
                "source_hash": self.source_hash,
                "file_content_sha256": self.file_content_sha256,
                "build_run_id": self.build_run_id,
                "review_tier": self.review_tier,
                "text": self.text, "condition": self.condition,
                "exception": self.exception, "scope": self.scope,
                "condition_adjudicated": self.condition_adjudicated}


@dataclass
class DeliveryRecord:
    """What was delivered, to which call, from which library state."""

    delivery_id: str
    task_id: str
    platform: str
    delivery_point: str
    tool_name: str
    at: str
    coverage_status: str
    coverage: dict = field(default_factory=dict)
    rules: list = field(default_factory=list)       # list[DeliveredRule]
    task_constraints: list = field(default_factory=list)
    library_path: str = ""
    library_sha256: str = ""
    structured_input: bool = False
    traceability: list = field(default_factory=list)

    @property
    def capsule_sha256(self) -> str:
        body = json.dumps([r.as_dict() for r in self.rules], sort_keys=True,
                          ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        human = sum(1 for r in self.rules if r.review_tier == HUMAN_REVIEWED)
        return {
            "delivery_id": self.delivery_id, "task_id": self.task_id,
            "platform": self.platform, "delivery_point": self.delivery_point,
            "tool_name": self.tool_name, "at": self.at,
            "coverage_status": self.coverage_status,
            "coverage": self.coverage,
            "library_path": self.library_path,
            "library_sha256": self.library_sha256,
            "capsule_sha256": self.capsule_sha256,
            "rules_delivered": len(self.rules),
            "rules_human_reviewed": human,
            "task_constraints_structured_input": self.structured_input,
            "_delivery_is_not_compliance": (
                "this record states the capsule was produced and handed to "
                "the call site. It does NOT state that the agent obeyed; "
                "nothing here observes the agent's behaviour."),
            "_conditions_not_adjudicated": (
                "conditions, exceptions and scope limits travel verbatim and "
                "were not evaluated"),
            "_review_provenance": (
                f"{human} of {len(self.rules)} delivered rules are "
                "human_reviewed. A count is not a library-wide label."),
            "rules": [r.as_dict() for r in self.rules],
            "task_constraints": self.task_constraints,
            "traceability": self.traceability,
        }

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deliver(selection: Selection, task, *, library, source_root,
            platform: str, tool_name: str, delivery_id: str = "") -> DeliveryRecord:
    """Render the capsule for one call site and build its record.

    Delivers **whatever the selection holds**, including an empty list — but
    never without `coverage_status`. An empty delivery under a status other
    than `COVERED` is a reported gap; an empty delivery presented bare would be
    the "no rules found means no rules exist" failure this architecture exists
    to prevent, and `DeliveryRecord` has no shape in which that can be
    expressed.
    """
    if platform not in PLATFORM_NOTES:
        raise DeliveryError(
            f"platform {platform!r} has no verified entry point on record. "
            f"Known: {sorted(PLATFORM_NOTES)}. A delivery point that was never "
            "read in source may not be asserted.")
    if selection.coverage is None:
        raise DeliveryError("a selection must carry a coverage report")

    note = PLATFORM_NOTES[platform]
    rules = [DeliveredRule(
        constraint_id=e.constraint_id,
        constraint_version=e.constraint_version,
        skill_id=e.skill_id, source_version=e.source_version,
        source_file=e.source_file, source_span=tuple(e.source_span),
        source_hash=e.source_hash,
        file_content_sha256=e.file_content_sha256,
        build_run_id=e.build_run_id, review_tier=e.review_tier,
        text=e.source_text, condition=e.condition, exception=e.exception,
        scope=e.scope,
        condition_adjudicated=selection.condition_adjudicated,
    ) for e in selection.to_deliver]

    lib_sha = hashlib.sha256(
        Path(library.path).read_bytes()).hexdigest() if Path(
            library.path).is_file() else ""

    rec = DeliveryRecord(
        delivery_id=delivery_id or f"d-{_now()}",
        task_id=task.task_id, platform=platform,
        delivery_point=note["delivery_point"], tool_name=tool_name,
        at=_now(), coverage_status=selection.coverage.status,
        coverage=selection.coverage.as_dict(), rules=rules,
        task_constraints=[c.as_dict() for c in task.active()],
        library_path=str(library.path), library_sha256=lib_sha,
        structured_input=task.structured_input,
        traceability=[verify_traceability(e, source_root=source_root)
                      for e in selection.to_deliver])
    return rec


def render_capsule(rec: DeliveryRecord) -> str:
    """The text handed to the call site. Every rule labelled with its tier.

    Scope limits, conditions and exceptions are printed verbatim and marked
    un-adjudicated, because a rendering that silently dropped them would make
    the capsule read like a decision.
    """
    lines = [f"## ConstraintRelay — rules for this call "
             f"({rec.tool_name or 'no tool'})",
             f"coverage: {rec.coverage_status}"]
    if rec.coverage_status != COVERED:
        unc = rec.coverage.get("uncovered") or []
        lines.append(f"**COVERAGE GAP — not all skills could be answered.** "
                     f"Uncovered: {', '.join(unc) or 'n/a'}. "
                     f"This is a coverage report, not permission to proceed.")
    if rec.task_constraints:
        lines.append("")
        lines.append("### Task constraints"
                     + (" (structured input, not extracted)"
                        if rec.structured_input else ""))
        for c in rec.task_constraints:
            q = f' — user said: "{c["verbatim_quote"]}"' if c.get(
                "verbatim_quote") else ""
            lines.append(f"- [{c['policy_origin']}/{c['authority']}] "
                         f"{c['text']}{q}")
    lines.append("")
    lines.append(f"### Skill rules ({len(rec.rules)})")
    if not rec.rules:
        lines.append("- none delivered. See the coverage status above; this is "
                     "NOT a statement that no constraints exist.")
    for r in rec.rules:
        lines.append(f"- [{r.constraint_id} · {r.review_tier}] {r.text}")
        if r.condition:
            lines.append(f"    condition (NOT adjudicated): {r.condition}")
        if r.exception:
            lines.append(f"    exception (NOT adjudicated): {r.exception}")
        if r.scope:
            lines.append(f"    scope (NOT adjudicated): {r.scope}")
    return "\n".join(lines) + "\n"
