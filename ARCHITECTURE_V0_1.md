# ConstraintRelay v0.1 — architecture, frozen

This is the frozen v0.1 product architecture. It is what Phases 1–5 converged
on, and it is not a research surface any more: the interfaces below are the ones
callers should depend on.

```text
Constraint Sources
    ↓
Import / Extraction
    ↓
Canonical Constraint Store
    ↓
Skill activation
    ↓
Active Constraint Set
    ↓
Exact dedup
    ↓
Budget-aware accounting
    ↓
Just-in-time restatement
    ↓
Model request
```

---

## 1. Layer by layer

### Constraint Sources → Import / Extraction

`cf/inventory.py`, `cf/candidates.py`

A **skill** is a directory containing `SKILL.md`. `skill_id` is the
repository-relative POSIX path of that directory — unique by construction,
independent of every other skill, and its own provenance. Support files are
scanned for prose suffixes only (`.md`, `.markdown`, `.txt`, `.rst`), at most 3
levels deep, 64 files, 512 KB each; everything skipped is *counted and
reported*, never silently dropped.

Extraction segments each file with the frozen block semantics in `cf/blocks.py`
and proposes **candidates**, each tiered:

* `operative` — must / must not / never / only / forbidden / required, and the
  Chinese equivalents 必须 / 禁止 / 不得 / 严禁 / 只能 / 仅限;
* `advisory` — should / avoid / ensure / recommended / 不要 / 应当;
* `author_marker` — an explicit `<!-- constraintrelay: critical id=… -->`, which
  is not a heuristic at all.

Four regions are excluded for reasons found in real corpora: YAML frontmatter
(the `description:` is a paraphrase of a constraint, not the constraint), fenced
code, markdown tables (a whole table segments as one block), and emphasis-only
labels.

**Extraction never promotes.** A candidate becomes a constraint only when
someone confirms it.

### Canonical Constraint Store

`cf/skillstore.py` → `.constraintrelay/skills.toml`

```text
[[skill]]
skill_id, name, prefix, rel_dir, first_imported, last_seen, status, hold_scope

[[constraint]]
constraint_id      <prefix>-C<nnn>, allocated once, never reused
skill_id           the owning skill
canonical_text     THE CONSTRAINT — the authority
source_text        the exact slice it was imported from
source_file, source_span, source_lines, source_hash
constraint_version status  created_at  updated_at
tier, extraction_reason, candidate_id, note
```

`canonical_text` is the authority; the `source_*` fields are a record of where
it came from, not a second copy with equal standing. Three consequences are
implemented, not merely documented:

* `diff` never writes `canonical_text` — a changed source is a reported delta
  and `update` is the only thing that moves the record;
* a constraint whose skill directory has vanished stays `active` — discovery
  failing is not an author retiring a constraint, and if discovery finds *no*
  skills at all the diff refuses to report anything as removed;
* `constraint_version` increments only on an explicit canonical-text update, so
  it counts author decisions, not re-scans.

Every write is serialised, parsed back, and compared before it reaches disk. The
TOML renderer prefers literal strings precisely because the obvious rendering
cannot round-trip a constraint quoting a regex.

### Skill activation → Active Constraint Set

`cf/lifetime.py`

`SkillActivationTracker` wraps the harness's `load_skill` and returns its result
**byte-identically** — it adds nothing to the tool result. Activation goes into
`ActiveSkillSet`, which holds skills in activation order (any other order would
be the first step of a ranking policy) and records every transition with its
`source`, `turn` and `active_size_after`.

### Exact dedup

Byte-identical `canonical_text` across active skills is emitted **once**. The
key is the text itself, not a similarity measure; the winner is the first
occurrence in activation order, so the same active set always renders the same
bytes. Every duplicate's id stays on the emitted line and the dropping section
records its count, so no constraint loses attribution. A section with nothing to
drop is byte-identical to the shared renderer.

### Budget-aware accounting

`cf/accounting.py`

The harness decides when to compact from its own token estimate, and that
estimate does not contain a capsule that is added at request time. So the
accounting layer makes the harness's own arithmetic run over
`messages + [capsule]` — the request that will actually be sent.

**This is adapter technical debt, not architecture.** The general requirement is:

> a harness must let a delivery layer declare what it will add to a request
> *before* the context budget for that request is computed.

See `CF-ADAPTER-DEBT-1` in `DEFECT_LEDGER.md`.

### Just-in-time restatement → Model request

The capsule is rebuilt from the store and appended to a **copy** of the message
list on every request. It is therefore never persisted into conversation
history, never written into the harness's transcript, and never fed to its
summarizer. Nothing is expected to survive compaction, so nothing has to.

The accounting layer and the request share **one memoized capsule object**, keyed
on the active set's version, so the harness cannot budget for a capsule that was
never sent.

---

## 2. Lifetime semantics

| Event | Trigger | Recorded `source` |
|---|---|---|
| activate | `load_skill` returns a skill | `load_skill` |
| deactivate | the model calls `release_skill` | `release_skill` |
| deactivate | a host calls `ActiveSkillSet.release` | `host` |
| reset | task/session termination (`task_scope`) | `task_end` |

* **`hold_scope` defaults to `task`** and is stored per skill.
* **`phase` is only ever set explicitly**, by `constraintrelay scope --hold phase`.
  Nothing infers it.
* **No timeout, no expiry, no idle heuristic.** A skill untouched for twenty
  turns is as active as one used last turn: "unused" and "no longer applicable"
  are different claims, and only the second justifies dropping a constraint.
* **No correctness dependency on voluntary release.** With the default scope,
  constraints apply until the task ends.
* **Task end always clears the set**, on success and on exception alike.

`hold_scope` is a declaration, not an enforcement: a release is honoured
whatever the declaration says, and the audit records that a `task`-scoped skill
was released early so a reviewer can see it.

---

## 3. Interfaces

### Public Python API — `cf/api.py`

```python
VERSION

# store + query
open_store(root) -> ConstraintRelay
store_path(root) -> Path
ConstraintRelay.list_skills(with_constraints_only=False) -> [SkillView]
ConstraintRelay.get_constraints(skill_id, include_retired=False) -> [ConstraintRecord]
ConstraintRelay.get_constraint(constraint_id) -> ConstraintRecord | None
ConstraintRelay.capsule(skill_id) -> str
ConstraintRelay.hold_scope(skill_id) -> "task" | "phase"
ConstraintRelay.phase_scoped_ids() -> [skill_id]
ConstraintRelay.resolve(token) -> (skill_id, candidates)

# import
init(root, skills_dir="") -> dict
scan(root, ...) -> (Inventory, Extraction)
import_constraints(root, tier="all", ...) -> dict

# change detection
diff(store, inventory, extraction, ...) -> DiffReport

# delivery
install_vibe(registry, llm, root, options=None) -> (tool, llm, active)
uninstall_vibe(registry)
install_accounting(loop_module, llm, observe_only=False) -> AccountingPatch
task_scope(active)                       # context manager
```

`ActiveSkillSet`: `activate`, `release`, `reset`, `state`, `transitions`,
`audit_rows`, `__len__`.
`ConstraintAwareLLM`: `current_injection`, `pending_messages`, `build_request`,
`working_set_stats`, `timeline`.

### CLI

Product surface:

```
constraintrelay init        <repo> [--skills-dir D] [--include-project] [--force]
constraintrelay import      <repo> [--confirm-all] [--tier operative|advisory|all]
constraintrelay confirm     <repo> [--skill S] [--candidate ID]… [--all]
constraintrelay skills      <repo> [--with-constraints]
constraintrelay constraints <repo> --skill S [--include-retired]
constraintrelay constraint  <repo> <constraint_id>
constraintrelay scope       <repo> [--skill S] [--hold task|phase] [--all]
constraintrelay diff        <repo> [--skill S] [--verbose]
constraintrelay update      <repo> --constraint ID… [--refresh-provenance]
constraintrelay retire      <repo> --constraint ID… [--reason R]
constraintrelay deliver     <repo> --skill S [--harness P] [--show-input N]
constraintrelay report      <repo>
constraintrelay internals
```

All take `--json`. Exit codes: `0` nothing needs a decision, `1` tool or
configuration error, `2` a reported finding. `1` never means "a finding was
reported" — a CI shell must be able to tell the two apart.

`constraintrelay internals` lists the paused harness-qualification commands
(`receipt`, `capsule`, `store`, `targets`, `verify`, `impact`, `budget`,
`fingerprint`, `scan`, `profiles`). They work and their evidence is preserved;
v0.1 does not use them and no user needs them.

### Files on disk

| Path | What |
|---|---|
| `.constraintrelay/skills.toml` | the canonical store — the authority |
| `.constraintrelay/candidates.json` | the last scan's candidate set (a report) |
| `.constraintrelay/store.toml` | the paused v0.7 store, if a project has one; unrelated to v0.1 |

---

## 4. What is frozen, and what that means

Frozen: the pipeline above, the store schema, the lifetime semantics, the public
API in `cf/api.py`, and the CLI product surface.

Not frozen: the extraction heuristics (they are tuned against one corpus and
will change), the capsule's exact wording, and anything behind
`constraintrelay internals`.

Deliberately absent, with measurements behind each: semantic compression,
learned or ranked selection, heuristic expiry, additional harness adapters,
swarm support. See `LIMITATIONS.md`.
