"""Phase 1 §4 — the one minimal delivery proof.

    skill selected -> Constraint Store lookup -> constraints returned
                   -> model-visible context

The brief bounds this deliberately: *"Do not build a general multi-harness
delivery system yet … This is a product demo, not a general adapter
qualification program."* So there is exactly one harness here, one injection
point, and one assertion.

What is real and what is simulated
----------------------------------
Real: the skill body, read from the Vibe-Trading checkout; the envelope the
`load_skill` tool builds around it; and the 10,000-character cut `loop.py`
applies to every tool result. In `harness` mode all three come from the upstream
code by import. In `ported` mode they come from `cf/vibe_trading_paged.py`,
which is a port of that loader differentially tested against it in
`tests/test_on_request.py`. The mode is named in the result either way, because
"we ran their code" and "we ran our tested copy of their code" are different
claims.

Simulated: the **injection point**. ConstraintFlow is not installed as a hook in
the Vibe-Trading agent; this module places the capsule at the front of the tool
result the way a delivery adapter would, and then lets the harness's own
truncation run. That is a genuine end-to-end path through real loader geometry
and a simulated hook, and it is labelled as such in `DeliveryProof.mode`.
Installing the hook is Phase 2.

Why the capsule goes first
--------------------------
The cut is a prefix cut: `result[:10_000]`. 33 of the 88 Vibe-Trading skills
produce an envelope longer than that, and the published anchor case is a scope
restriction at character 42,629 of a 42,678-character envelope — delivered
nowhere. A constraint placed after the skill body inherits exactly that fate.
Placed before it, the constraint survives and the *skill body* absorbs the cut,
which is the correct trade: the body is retrievable on request by section or by
page, and the constraint, in Phase 1, is not retrievable at all once it is cut.

The assertion
-------------
For the selected skill, every confirmed constraint's **complete** canonical text
must be present in the delivered model input. Checked twice:

* `escaped_present` — the text, JSON-escaped exactly as the envelope encodes it,
  is a substring of the delivered string. This is the literal claim: those
  characters are in the model's input. It is the primary check because the tool
  result the model receives *is* the serialized envelope, newline escapes and
  all.
* `decoded_present` — the delivered prefix still parses as JSON and the decoded
  `content` contains the text. Stronger, and it fails whenever the cut lands
  mid-envelope, which it does for a truncated skill. Reported separately rather
  than folded in, so a pass is never claimed on the weaker of the two without
  saying so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .query import ConstraintFlow
from .skillstore import ConstraintRecord

HARNESS = "harness"
PORTED = "ported"

#: `loop.py:53` at both surveyed commits.
TOOL_RESULT_LIMIT = 10_000

CAPSULE_OPEN = '<constraintrelay-constraints skill="{name}" count="{n}">'
CAPSULE_CLOSE = "</constraintrelay-constraints>"


@dataclass
class ConstraintCheck:
    constraint_id: str
    chars: int
    escaped_present: bool
    decoded_present: bool

    @property
    def ok(self) -> bool:
        return self.escaped_present

    def as_dict(self) -> dict:
        return {"constraint_id": self.constraint_id, "chars": self.chars,
                "escaped_present": self.escaped_present,
                "decoded_present": self.decoded_present}


@dataclass
class DeliveryProof:
    skill_id: str
    skill_name: str
    mode: str
    constraint_count: int
    capsule_chars: int
    body_chars: int
    envelope_chars: int
    delivered_chars: int
    limit: int
    truncated: bool
    checks: list[ConstraintCheck] = field(default_factory=list)
    model_input: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def established(self) -> bool:
        """Every confirmed constraint's complete text is in the model input."""
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def decoded_established(self) -> bool:
        return bool(self.checks) and all(c.decoded_present for c in self.checks)

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "skill_name": self.skill_name,
                "mode": self.mode,
                "constraint_count": self.constraint_count,
                "capsule_chars": self.capsule_chars,
                "body_chars": self.body_chars,
                "envelope_chars": self.envelope_chars,
                "delivered_chars": self.delivered_chars,
                "limit": self.limit, "skill_body_truncated": self.truncated,
                "constraints_delivered_complete": self.established,
                "envelope_still_parses_as_json": self.decoded_established,
                "checks": [c.as_dict() for c in self.checks],
                "notes": self.notes}


def render_capsule(skill_name: str, records: list[ConstraintRecord]) -> str:
    """The capsule block, carrying each constraint's complete canonical text."""
    lines = [CAPSULE_OPEN.format(name=skill_name, n=len(records))]
    for c in records:
        lines.append(f"[{c.constraint_id}] {c.canonical_text}")
    lines.append(CAPSULE_CLOSE)
    return "\n".join(lines)


def _harness_body(harness_root: Path, skill_name: str) -> tuple[str, int] | None:
    """`(body, limit)` from the upstream loader, or None when it is not importable."""
    agent_dir = harness_root / "agent"
    if not (agent_dir / "src" / "agent" / "skills.py").is_file():
        return None
    import sys
    added = str(agent_dir)
    if added not in sys.path:
        sys.path.insert(0, added)
    try:
        from src.agent.loop import TOOL_RESULT_LIMIT as limit   # type: ignore
        from src.agent.skills import SkillsLoader               # type: ignore
    except Exception:
        return None
    loader = SkillsLoader()
    for skill in loader.skills:
        if skill.name == skill_name:
            return skill.body, int(limit)
    return None


def _ported_body(skill_doc: Path) -> str:
    from .discover import read_source, strip_frontmatter
    return strip_frontmatter(read_source(skill_doc))


def prove(project_root: Path, skill_token: str, *,
          harness_root: Path | None = None,
          prefer_harness: bool = True) -> DeliveryProof:
    """Run the demo for one skill and return what was observed.

    *project_root* is the repository holding `.constraintrelay/skills.toml`.
    *harness_root* defaults to *project_root*, which is the normal case: the
    Vibe-Trading checkout is both the source of the skills and the harness.
    """
    cf = ConstraintFlow.open(project_root)
    skill_id, options = cf.resolve(skill_token)
    if not skill_id:
        raise KeyError(f"{skill_token!r} does not resolve to one skill in the "
                       f"store" + (f"; candidates: {', '.join(options)}"
                                   if options else ""))
    records = cf.get_constraints(skill_id)
    rec = cf.skill_record(skill_id)
    skill_name = rec.name if rec else skill_id
    notes: list[str] = []

    root = harness_root or project_root
    mode = PORTED
    limit = TOOL_RESULT_LIMIT
    body = ""
    if prefer_harness:
        got = _harness_body(root, skill_name)
        if got is not None:
            body, limit = got
            mode = HARNESS
        else:
            notes.append(
                f"the upstream loader under {root}/agent was not importable, so "
                f"the body and the cut come from cf/vibe_trading_paged.py, the "
                f"port differentially tested against it")
    if mode == PORTED:
        doc = Path(rec.rel_dir) if rec and rec.rel_dir else None
        path = (root / doc / "SKILL.md") if doc else None
        if path is None or not path.is_file():
            raise FileNotFoundError(
                f"neither the upstream loader nor a SKILL.md for {skill_id!r} "
                f"could be reached under {root}; the delivery demo needs the "
                f"skill body, which is the one part of this path the store does "
                f"not own")
        body = _ported_body(path)

    from .vibe_trading_paged import wrap

    capsule = render_capsule(skill_name, records)
    content = capsule + "\n" + wrap(skill_name, body)
    envelope = json.dumps({"status": "ok", "content": content},
                          ensure_ascii=False)
    delivered = envelope[:limit]

    decoded = ""
    try:
        decoded = json.loads(delivered).get("content", "")
    except (json.JSONDecodeError, AttributeError):
        notes.append("the delivered prefix is not valid JSON on its own, because "
                     "the harness cut landed inside the envelope. That is the "
                     "harness's own pre-existing behaviour for every oversized "
                     "skill — 33 of the 88 in this corpus — and not something "
                     "this delivery introduces: the constraint characters are in "
                     "the model's input either way, and the part the cut destroys "
                     "is the tail of the skill body.")

    checks = [
        ConstraintCheck(
            constraint_id=c.constraint_id,
            chars=len(c.canonical_text),
            escaped_present=json.dumps(c.canonical_text,
                                       ensure_ascii=False)[1:-1] in delivered,
            decoded_present=bool(decoded) and c.canonical_text in decoded)
        for c in records
    ]
    if not records:
        notes.append(f"{skill_id} has no confirmed constraints in the store; "
                     f"there is nothing to deliver and nothing is claimed")
    if len(capsule) >= limit:
        notes.append(f"the capsule alone is {len(capsule)} characters against a "
                     f"{limit}-character limit, so it cannot be delivered whole "
                     f"at this injection point. Phase 1 does not compress; this "
                     f"is a real limit, not a pass.")

    return DeliveryProof(
        skill_id=skill_id, skill_name=skill_name, mode=mode,
        constraint_count=len(records), capsule_chars=len(capsule),
        body_chars=len(body), envelope_chars=len(envelope),
        delivered_chars=len(delivered), limit=limit,
        truncated=len(envelope) > limit, checks=checks,
        model_input=delivered, notes=notes)


def render(proof: DeliveryProof, *, show_input: int = 0) -> str:
    lines = [
        f"Skill {proof.skill_name}  ({proof.skill_id})",
        "  ↓  Constraint Store lookup",
        *(f"  {c.constraint_id}" for c in proof.checks),
        f"  ↓  retrieved from the Constraint Store "
        f"({proof.constraint_count} constraint(s), {proof.capsule_chars} chars)",
        f"  ↓  capsule placed ahead of the skill body; envelope built by the "
        f"{proof.mode} loader",
        f"  ↓  harness cut at {proof.limit} chars "
        f"({proof.envelope_chars} → {proof.delivered_chars})",
        "  model-visible context",
        "",
    ]
    for c in proof.checks:
        mark = "OK  " if c.escaped_present else "LOST"
        caveat = "" if c.decoded_present else \
            "  (envelope truncated: characters present, JSON no longer parses)"
        lines.append(f"  {mark} {c.constraint_id:<22} {c.chars:>5} chars  "
                     f"complete text present in the delivered tool result{caveat}")
    lines.append("")
    lines.append(f"skill body truncated by the harness: "
                 f"{'yes' if proof.truncated else 'no'}")
    lines.append(f"all confirmed constraints delivered complete: "
                 f"{'YES' if proof.established else 'NO'}")
    for note in proof.notes:
        lines.append(f"note: {note}")
    if show_input:
        lines += ["", f"--- first {show_input} chars of the model input ---",
                  proof.model_input[:show_input]]
    return "\n".join(lines)
