"""Phase 1 layer E — the retrieval interface.

The brief asks for *"one simple programmatic interface and one CLI interface"*,
with `get_constraints(skill_id)` named explicitly. This module is the
programmatic one; `cli_phase1.py` is the CLI, and it is a client of this module
rather than a parallel implementation.

One property matters more than the API shape, and it is what the Phase-1 store
exists for: **retrieval reads the store and nothing else.** No discovery, no
re-scan, no access to the original skill directory. So

    constraintrelay constraints --skill agent/src/skills/tushare

answers correctly after the skill has been deleted from the repository, which is
the brief's *"If an original skill file later disappears or changes, the
canonical constraint must not silently disappear."* `tests/test_phase1_store.py`
asserts it by deleting the corpus between the import and the query.

`resolve` accepts a skill_id, a frontmatter name, or a directory basename, using
only what the store recorded — so the same token works from the CLI whether or
not the source tree is still present.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import naming
from . import skillstore
from .skillstore import ConstraintRecord, SkillRecord, SkillStore, StoreError

#: Header for the model-visible capsule. Phrased as a statement of record —
#: "these are the constraints recorded for this skill" — rather than as an
#: instruction to obey them, because Phase 1 delivers constraints and makes no
#: claim about enforcement. Overclaiming here is exactly what §7 of the earlier
#: brief called out: text that is shown but not enforced, described as enforced.
CAPSULE_TITLE = "ConstraintRelay — recorded constraints for skill {name}"


@dataclass(frozen=True)
class SkillView:
    skill_id: str
    name: str
    rel_dir: str
    constraint_count: int
    status: str
    #: `task` (default) or `phase` — the declared lifetime of this skill's
    #: constraints in the active set. Never inferred; see `skillstore.HOLD_SCOPES`.
    hold_scope: str = skillstore.HOLD_TASK

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "name": self.name,
                "rel_dir": self.rel_dir,
                "constraint_count": self.constraint_count,
                "status": self.status, "hold_scope": self.hold_scope}


class ConstraintFlow:
    """A read interface onto one project's Constraint Store."""

    def __init__(self, store: SkillStore) -> None:
        self.store = store

    # -- construction -----------------------------------------------------
    @classmethod
    def open(cls, project_root: Path) -> "ConstraintFlow":
        """Open the store at *project_root*, raising when there is none.

        Deliberately not "open or empty": a query against a project with no
        store should say so, not return zero constraints, because zero
        constraints and no inventory are different answers.
        """
        found = skillstore.find(project_root)
        if found is None:
            raise StoreError(
                f"no Constraint Store at {skillstore.store_path(project_root)}; "
                f"run `{naming.cmd('import', project_root)}` first")
        return cls(skillstore.load(found))

    # -- the interface the brief names ------------------------------------
    def list_skills(self, *, with_constraints_only: bool = False) -> list[SkillView]:
        out: list[SkillView] = []
        for s in sorted(self.store.skills, key=lambda s: s.skill_id):
            n = len(self.store.for_skill(s.skill_id))
            if with_constraints_only and not n:
                continue
            out.append(SkillView(s.skill_id, s.name, s.rel_dir, n, s.status,
                                 s.hold_scope))
        return out

    def get_constraints(self, skill_id: str, *, include_retired: bool = False
                        ) -> list[ConstraintRecord]:
        """Every constraint owned by *skill_id*, newest id last.

        The result carries canonical text and full provenance, which is the
        brief's bar: *"enough to display or inject the relevant constraints
        later."*
        """
        return sorted(self.store.for_skill(skill_id, include_retired=include_retired),
                      key=lambda c: c.number)

    def get_constraint(self, constraint_id: str) -> ConstraintRecord | None:
        return self.store.by_id.get(constraint_id)

    # -- helpers ----------------------------------------------------------
    def skill_record(self, skill_id: str) -> SkillRecord | None:
        return self.store.skills_by_id.get(skill_id)

    def hold_scope(self, skill_id: str) -> str:
        """The declared lifetime of *skill_id*'s constraints.

        Returns the conservative default for a skill the store does not know,
        because the safe answer to "how long does this apply?" is "until the
        task ends".
        """
        rec = self.skill_record(skill_id)
        return rec.hold_scope if rec else skillstore.HOLD_TASK

    def phase_scoped_ids(self) -> list[str]:
        """Skills explicitly declared `phase`. Empty until someone declares one."""
        return [s.skill_id for s in self.store.skills
                if s.hold_scope == skillstore.HOLD_PHASE]

    def resolve(self, token: str) -> tuple[str, list[str]]:
        """`(skill_id, candidates)` for a user-supplied reference.

        `skill_id` is `""` when the token is ambiguous or unknown, and
        `candidates` is then what it could have meant.
        """
        if token in self.store.skills_by_id:
            return token, [token]
        for attr in ("name", "basename"):
            hits = [s.skill_id for s in self.store.skills
                    if (s.name if attr == "name"
                        else s.rel_dir.rsplit("/", 1)[-1]) == token]
            if len(hits) == 1:
                return hits[0], hits
            if len(hits) > 1:
                return "", hits
        suffix = [s.skill_id for s in self.store.skills
                  if s.skill_id.endswith("/" + token.strip("/"))]
        return (suffix[0], suffix) if len(suffix) == 1 else ("", suffix)

    def capsule(self, skill_id: str, *, include_ids: bool = True) -> str:
        """The constraints for *skill_id* as model-visible text.

        Each constraint is rendered as its **complete canonical text**, never a
        summary and never truncated. Phase 1 has no compression and no subset
        selection — those are Phase 3 — so the only correct rendering is all of
        it.
        """
        records = self.get_constraints(skill_id)
        rec = self.skill_record(skill_id)
        name = rec.name if rec else skill_id
        if not records:
            return ""
        lines = [CAPSULE_TITLE.format(name=name),
                 f"skill_id: {skill_id}",
                 f"{len(records)} constraint(s), from the Constraint Store "
                 f"(not re-read from the skill files).", ""]
        for c in records:
            head = f"[{c.constraint_id}] " if include_ids else ""
            lines.append(f"{head}{c.canonical_text}")
            lines.append(f"    source: {c.locator}  v{c.constraint_version}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Module-level convenience, so an embedding agent needs one call
# ---------------------------------------------------------------------------

def get_constraints(project_root: Path | str, skill_id: str
                    ) -> list[ConstraintRecord]:
    """`get_constraints(skill_id)` as the brief writes it, given a project root."""
    return ConstraintFlow.open(Path(project_root)).get_constraints(skill_id)


def get_capsule(project_root: Path | str, skill_id: str) -> str:
    return ConstraintFlow.open(Path(project_root)).capsule(skill_id)
