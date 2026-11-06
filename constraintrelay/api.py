"""ConstraintFlow v0.1 — the stable public API.

Everything another program should need, in one place, so an embedder never has
to know which research phase produced which module. The modules behind this file
keep their own names and their own docstrings; this is the surface that will not
move under you.

    from constraintrelay import api

    api.init("/path/to/repo")                      # what is in here?
    api.import_constraints("/path/to/repo")        # confirm them
    cf = api.open_store("/path/to/repo")
    cf.get_constraints("agent/src/skills/tushare")

Delivery into a Vibe-Trading agent is three lines:

    registry = ToolRegistry(); registry.register(LoadSkillTool(...))
    tool, llm, active = api.install_vibe(registry, ChatLLM(), repo)
    with api.task_scope(active):
        AgentLoop(registry, llm).run(user_message)

The v0.1 pipeline, frozen:

    Constraint Sources
        -> Import / Extraction          constraintrelay.inventory, constraintrelay.candidates
        -> Canonical Constraint Store    constraintrelay.skillstore
        -> Skill activation              constraintrelay.lifetime.SkillActivationTracker
        -> Active Constraint Set         constraintrelay.lifetime.ActiveSkillSet
        -> Exact dedup                   constraintrelay.lifetime.render_active
        -> Budget-aware accounting       constraintrelay.accounting
        -> Just-in-time restatement      constraintrelay.lifetime.ConstraintAwareLLM
        -> Model request

Lifetime semantics, also frozen:

* `hold_scope` defaults to `task` and is never inferred;
* `phase` only when declared, via `constraintrelay scope --hold phase`;
* no timeout or expiry anywhere;
* **no correctness dependency on anything choosing to release a skill** — with
  the default scope, constraints apply until the task ends;
* task end always clears the active set.

What is deliberately *not* here: the paused harness-qualification machinery
(`constraintrelay.receipt`, `constraintrelay.qualification`, `constraintrelay.scenario`, `constraintrelay.profiles`, `constraintrelay.targets`,
`constraintrelay.impact`, `constraintrelay.budget`, `constraintrelay.fingerprint`, `constraintrelay.store`). It still works and its
evidence is preserved, but it is not part of the v0.1 product and a user does
not need to understand it. See `LIMITATIONS.md`.
"""

from __future__ import annotations

from pathlib import Path

from . import accounting as _accounting
from . import lifetime as _lifetime
from . import skillstore as _skillstore
from .candidates import ADVISORY, OPERATIVE, Extraction, extract
from .matching import UnitIndex
from .inventory import Inventory, discover_skills
from .lifetime import (ActiveSkillSet, ConstraintAwareLLM, LifetimeOptions,
                       ReleaseSkillTool, SkillActivationTracker, task_scope)
from .query import ConstraintFlow, SkillView
from .review import (ACCEPT, DETERMINISTIC, EDIT, PENDING, REJECT, SEMANTIC,
                     Card, Member, assert_partition, build_cards, burden)
from .semantic import SemanticCandidate, assert_no_suppression, ground, union
from .skillstore import (HOLD_PHASE, HOLD_SCOPES, HOLD_TASK, ConstraintRecord,
                         SkillRecord, StoreError)
from .sync import DiffReport, diff
from .version import IMPLEMENTATION

__all__ = [
    "VERSION", "IMPLEMENTATION",
    # store + query
    "open_store", "store_path", "ConstraintFlow", "SkillView",
    "ConstraintRecord", "SkillRecord", "StoreError",
    "HOLD_TASK", "HOLD_PHASE", "HOLD_SCOPES",
    # import
    "init", "scan", "import_constraints", "Inventory", "Extraction",
    "OPERATIVE", "ADVISORY",
    # discovery: deterministic + semantic UNION, then review cards
    "discover", "Discovery", "SemanticCandidate", "ground", "union",
    "Card", "Member", "build_cards", "burden",
    "DETERMINISTIC", "SEMANTIC", "PENDING", "ACCEPT", "REJECT", "EDIT",
    "UnitIndex",
    # sync
    "diff", "DiffReport",
    # delivery
    "install_vibe", "uninstall_vibe", "install_accounting", "task_scope",
    "ActiveSkillSet", "ConstraintAwareLLM", "SkillActivationTracker",
    "ReleaseSkillTool", "LifetimeOptions",
]

#: The product version. Distinct from `constraintrelay.version.IMPLEMENTATION`, which
#: identifies the research implementation the qualification evidence was taken
#: against.
#:
#: Read from `constraintrelay.__version__` rather than retyped: a second literal is a
#: second thing to forget to bump, which is the defect class this project
#: calls "a document that retypes a computed figure".
from . import __version__ as VERSION    # noqa: E402


# ---------------------------------------------------------------------------
# Store + query
# ---------------------------------------------------------------------------

def store_path(project_root: Path | str) -> Path:
    """Where the store lives for *project_root*."""
    return _skillstore.store_path(Path(project_root))


def open_store(project_root: Path | str) -> ConstraintFlow:
    """Open the Constraint Store, raising `StoreError` when there is none.

    Retrieval reads the store and nothing else — no discovery, no re-scan — so
    this answers correctly after the skill directories are gone.
    """
    return ConstraintFlow.open(Path(project_root))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def scan(project_root: Path | str, *, skills_dir: str = "",
         include_project: bool = False) -> tuple[Inventory, Extraction]:
    """Discover skills and extract candidate constraints. Writes nothing."""
    inv = discover_skills(Path(project_root), skills_dir=skills_dir,
                          include_project=include_project)
    return inv, extract(inv)


def init(project_root: Path | str, *, skills_dir: str = "") -> dict:
    """What is in this repository? Creates `.constraintrelay/` and reports.

    Writes no constraint: extraction is heuristic, and confirmation is a
    separate, explicit act.
    """
    root = Path(project_root)
    inv, ex = scan(root, skills_dir=skills_dir)
    (root / _skillstore.STORE_DIR).mkdir(parents=True, exist_ok=True)
    return {"root": str(root), "skills_found": len(inv.skills),
            "candidates": len(ex.candidates), "tiers": ex.tier_counts(),
            "unresolved": len(ex.unresolved)}


def import_constraints(project_root: Path | str, *, tier: str = "all",
                       skills_dir: str = "", include_project: bool = False,
                       when: str = "") -> dict:
    """Confirm candidates into the store. **This is the act of confirmation.**

    `tier="operative"` imports obligations, prohibitions and restrictions and
    leaves advisory text as candidates. Idempotent: re-importing adds only what
    is new.
    """
    from .cli_phase1 import _promote, _tier_filter
    root = Path(project_root)
    inv, ex = scan(root, skills_dir=skills_dir, include_project=include_project)
    store = _skillstore.load_or_empty(root)
    stamp = when or _skillstore.today()
    for skill in sorted(inv.skills, key=lambda s: s.skill_id):
        store.upsert_skill(skill.skill_id, skill.name, skill.basename,
                           skill.rel_dir, stamp)
    confirmed = _promote(store, inv, _tier_filter(ex.candidates, tier),
                         when=stamp)
    path = _skillstore.write(store)
    return {"store": str(path), "skills": len(store.skills),
            "confirmed_now": len(confirmed),
            "active_constraints": len(store.active)}


# ---------------------------------------------------------------------------
# Discovery — deterministic + semantic UNION, grouped into review cards
# ---------------------------------------------------------------------------

#: What a semantic client must look like. Duck-typed, not subclassed, so no
#: provider is wired into the package: `cf` has zero dependencies and names no
#: vendor. `fieldtest/phase12/client_cli.py` is the reference implementation.
#:
#:     complete(skill_name: str, prompt: str) -> object with .ok and .payload
#:     identity() -> dict           # model, version, prompt/config hashes
SEMANTIC_CLIENT_PROTOCOL = ("complete(skill_name, prompt) -> result with .ok "
                            "and .payload; identity() -> dict")


class Discovery:
    """The candidate set a reviewer is shown. **Confirms nothing.**

    Owner ruling 2026-10-08, ruling 2: *"Semantic extraction is candidate
    discovery only; it must not auto-confirm authoritative constraints."*

    That is enforced structurally rather than by discipline: this object holds
    no store handle, imports nothing that can write one, and exposes no method
    that promotes anything. The only way into the store remains
    `import_constraints`, which reads the **deterministic** extraction and has
    never seen a semantic candidate. A reviewer's card decisions are recorded on
    the cards; turning an accepted card into a constraint is a separate, later,
    explicit act that this release does not perform.
    """

    __slots__ = ("inventory", "deterministic", "semantic", "cards",
                 "union_result", "grounding", "client_identity")

    def __init__(self, *, inventory, deterministic, semantic, cards,
                 union_result, grounding, client_identity) -> None:
        self.inventory = inventory
        self.deterministic = deterministic
        self.semantic = semantic
        self.cards = cards
        self.union_result = union_result
        self.grounding = grounding
        self.client_identity = client_identity

    @property
    def raw_candidates(self) -> int:
        return len(self.deterministic) + len(self.semantic)

    def burden(self) -> dict:
        """Raw candidates, card count, and the cards/skill distribution."""
        out = burden(self.cards)
        out["raw_candidates"] = self.raw_candidates
        out["deterministic"] = len(self.deterministic)
        out["semantic"] = len(self.semantic)
        return out

    def as_dict(self) -> dict:
        return {"raw_candidates": self.raw_candidates,
                "burden": self.burden(),
                "union": self.union_result.counts(),
                "grounding": self.grounding,
                "client": self.client_identity,
                "cards": [c.as_dict() for c in self.cards]}


def discover(project_root: Path | str, *, client=None, prompt: str = "",
             skills_dir: str = "", include_project: bool = False,
             skill_ids: "list[str] | None" = None) -> Discovery:
    """Deterministic + semantic candidates, unioned and grouped into cards.

    `client=None` yields the deterministic path alone, which is the shipped
    v0.1 behaviour when no model is configured: the semantic path is additive
    and its absence degrades to exactly Phase 10's extractor rather than to an
    error.

    **The deterministic set is never suppressed.** `assert_no_suppression`
    checks the union by id, and `assert_partition` checks that every candidate
    from both paths lands on exactly one card. Writes nothing.

    Reproducibility, stated plainly
    -------------------------------
    **The semantic path is experimental and is NOT reproducible, and v0.1
    makes no claim that it is.** Three independently frozen backend
    specifications — two on `deepseek-v4-pro`, one on `deepseek-flash`, each
    with thinking disabled and `temperature: 0`, each committed before its own
    probe — all failed a repeated-call identity gate at the canonical artifact
    level. Whole constraints appear and disappear between identical calls: on
    the last probe, 1 of 12 and 3 of 29 canonical candidates were common to all
    three calls of one configuration.

    So two calls to this function with the same client, prompt and corpus may
    legitimately return different candidate sets, and neither is the answer.
    That is why the semantic set is *candidate discovery requiring human
    confirmation* and why `client=None` — the deterministic path alone — is the
    reproducible configuration. See `LIMITATIONS.md` §1.1 and the evidence
    under `fieldtest/phase15/`.
    """
    inv = discover_skills(Path(project_root), skills_dir=skills_dir,
                          include_project=include_project)
    ex = extract(inv, skill_ids=skill_ids)
    det = list(ex.candidates)

    name_of = {s.skill_id: s.skill_id.rsplit("/", 1)[-1] for s in inv.skills}
    indexes: dict = {}
    for s in inv.skills:
        for f in s.files:
            indexes[(name_of[s.skill_id], f.rel_path)] = UnitIndex.of(f.text)

    semantic: list = []
    grounding_counts: dict = {}
    identity: dict = {}
    if client is not None:
        # Phase-1 pause, Owner ruling 2026-10-15: automatic semantic
        # extraction is down. This is the package's declared entry point for
        # it, so it refuses here rather than letting the refusal surface as a
        # transport error further in.
        #
        # Honest about which layer is load-bearing: this gate is on ONE entry
        # point and `constraintrelay.semantic.ground` can still be called directly. The
        # un-bypassable layer is the send — `constraintrelay.phase1_pause.arm()` plus the
        # check inside each sender. `cf/semantic.py` itself is deliberately
        # NOT edited: it is a pinned Phase-15 artifact, and drifting a second
        # frozen gate before the Owner has ruled on the first would be worse
        # than leaving a parser that cannot run without a provider response
        # ungated.
        from . import phase1_pause as _pause
        _pause.require(_pause.SEMANTIC_EXTRACTION)
        if not prompt:
            raise ValueError(
                "a semantic client needs a prompt, and the prompt is part of "
                "the configuration identity the Owner requires preserved "
                "(model, prompt hash, parameters). Passing a client without "
                "one would produce candidates nobody could reproduce.")
        identity = dict(client.identity())
        semantic, grounding_counts = _run_semantic(
            inv, client, prompt, name_of, indexes, skill_ids)

    merged = union(det, semantic)
    assert_no_suppression(merged, det)

    members = _members(det, semantic, name_of)
    cards = build_cards(members, indexes)
    assert_partition(cards, members)

    return Discovery(inventory=inv, deterministic=det, semantic=semantic,
                     cards=cards, union_result=merged,
                     grounding=grounding_counts, client_identity=identity)


def _members(det: list, semantic: list, name_of: dict) -> list:
    out: list = []
    for d in det:
        m = Member(candidate_id=d.candidate_id, origin=DETERMINISTIC,
                   source_file=d.source_file,
                   source_span=tuple(d.source_span), source_text=d.source_text,
                   detail={"tier": d.tier,
                           "extraction_reason": d.extraction_reason,
                           "block_kind": d.block_kind})
        m.skill = name_of[d.skill_id]
        out.append(m)
    for s in semantic:
        m = Member(candidate_id=s.candidate_id, origin=SEMANTIC,
                   source_file=s.source_file,
                   source_span=tuple(s.source_span), source_text=s.source_text,
                   detail={"holder": s.holder, "action_kind": s.action_kind,
                           "action": s.action, "condition": s.condition,
                           "scope": s.scope, "ambiguous": s.ambiguous,
                           "confidence": s.confidence,
                           "match_tier": s.match_tier})
        m.skill = name_of[s.skill_id]
        out.append(m)
    return out


def _run_semantic(inv, client, prompt: str, name_of: dict, indexes: dict,
                  skill_ids) -> "tuple[list, dict]":
    """One call per skill, grounded. A failed call costs that skill, not the run.

    A model error, a timeout or an unparseable answer is recorded and skipped:
    the deterministic candidates for that skill are already in the union, so a
    partial semantic pass degrades the yield rather than the result.
    """
    from .structure import parse, verify_lossless
    wanted = set(skill_ids) if skill_ids else None
    out: list = []
    agg = {"proposed": 0, "accepted": 0, "rejected": 0, "failed_calls": 0,
           "by_reason": {}}
    for skill in inv.skills:
        if wanted is not None and skill.skill_id not in wanted:
            continue
        parts, per_file, unit_spans = [], {}, {}
        for f in skill.files:
            doc = parse(f.text)
            verify_lossless(doc)
            per_file[f.rel_path] = f.text
            units = doc.signal_units
            for i, u in enumerate(units):
                unit_spans[f"{f.rel_path}#u{i}"] = (u.start, u.end)
            parts.append(_render_units(units, f.rel_path))
        if not parts:
            continue
        name = name_of[skill.skill_id]
        full = (f"{prompt}\n\n=== SKILL DOCUMENT: {name} ===\n\n"
                + "\n".join(parts)
                + f"\n=== END OF SKILL DOCUMENT: {name} ===\n")
        call = client.complete(name, full)
        if not getattr(call, "ok", False):
            agg["failed_calls"] += 1
            continue
        for rel, text in per_file.items():
            g = ground(getattr(call, "payload", None), source=text,
                       skill_id=skill.skill_id, source_file=rel,
                       unit_spans=unit_spans)
            # Only candidates grounded in THIS file are kept from this pass; a
            # quote belonging to a sibling file grounds on that file's turn.
            out.extend(g.accepted)
            c = g.counts()
            agg["accepted"] += c["accepted"]
        agg["proposed"] += len(_payload_items(getattr(call, "payload", None)))
    seen: set = set()
    deduped: list = []
    for c in out:
        if c.candidate_id in seen:
            continue
        seen.add(c.candidate_id)
        deduped.append(c)
    agg["accepted"] = len(deduped)
    agg["rejected"] = max(0, agg["proposed"] - agg["accepted"])
    agg["ungrounded_rate"] = (round(100.0 * agg["rejected"] / agg["proposed"], 1)
                              if agg["proposed"] else 0.0)
    return deduped, agg


def _payload_items(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("constraints", "candidates", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _render_units(units, rel_path: str) -> str:
    out = [f"### FILE: {rel_path}"]
    for i, u in enumerate(units):
        head = f"[{rel_path}#u{i}] kind={u.kind}"
        if u.kind == "fence":
            head += f" fence_info={u.info or '(bare)'}"
        if u.heading_path:
            head += f" under={u.heading_path!r}"
        out.append(head)
        out.append(u.text.rstrip("\n"))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def install_vibe(registry, llm, project_root: Path | str, *,
                 options: LifetimeOptions | None = None
                 ) -> tuple[SkillActivationTracker, ConstraintAwareLLM,
                            ActiveSkillSet]:
    """Wire ConstraintFlow into a Vibe-Trading `ToolRegistry` and `ChatLLM`.

    Returns `(tool, llm, active)`. **The returned `llm` must be the one handed
    to `AgentLoop`**, because the loop captures `self.llm` at construction.

    Both attachment points are construction sites the harness already has, so no
    file in the Vibe-Trading checkout changes, and `uninstall_vibe` reverses the
    registry half.
    """
    return _lifetime.install(registry, llm, Path(project_root), options=options)


def uninstall_vibe(registry) -> None:
    """Restore the stock `load_skill` and drop `release_skill`."""
    _lifetime.uninstall(registry)


def install_accounting(loop_module, llm_wrapper, *, observe_only: bool = False
                       ) -> _accounting.AccountingPatch:
    """Make the harness's context budget account for the pending capsule.

    **Adapter technical debt, not architecture** — this replaces a module-level
    function in the harness. `CF-ADAPTER-DEBT-1` in `DEFECT_LEDGER.md` carries
    the reasoning and the retirement condition. Use it as a context manager so
    the patch is always removed:

        with api.install_accounting(loop_module, llm):
            AgentLoop(registry, llm).run(...)
    """
    return _accounting.install(loop_module, llm_wrapper,
                               observe_only=observe_only)
