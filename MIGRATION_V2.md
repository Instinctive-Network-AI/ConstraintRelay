# Migration to v2 — pre-built library + runtime task constraints. **PROPOSAL ONLY.**

Owner direction 2026-10-13. Nothing here is implemented. No architecture change
is made, no paid experiment added, nothing published. Companion:
`STATE_SNAPSHOT_2026-10-13.md`.

Target shape, as given:

```
BUILD TIME (offline, reviewed, version-bound)
  skill@version ──▶ extract ──▶ human review ──▶ public library (content-hash bound)

RUN TIME
  current task ──▶ record task constraints ──▶ match against library
                                            ──▶ deliver at the call decision point
                                            ──▶ leave a record
  not in library / version mismatch ──▶ MARK EXPLICITLY. Never "no constraints".
```

---

## 1. Module inventory

### 1a. Reusable as-is

| module | lines | what it already does for v2 |
|---|---|---|
| `cf/structure.py` | 369 | lossless structural tiling + `verify_lossless()` + `heading_path`. Source-location layer for the library. |
| `cf/candidates.py` | 1,111 | deterministic extractor, frozen. Union partner at build time; the zero-dependency, fully reproducible half. |
| `cf/semantic.py` | 406 | `ground()` — exact / whitespace / **rejected** tiers, span resolved against source **bytes**, `union()` with `assert_no_suppression`. This is the provenance guarantee; keep it unchanged. |
| `cf/matching.py` | 231 | `DISCOVERED`. **Frozen — do not touch.** Build-time scoring only, not a runtime matcher. |
| `cf/review.py` | 332 | review cards + partition invariant (every candidate on exactly one card). `Card.members[].detail` already carries holder/action/condition/scope/ambiguous. |
| `cf/applicability.py` | — | author-declared, deterministic, four literal rules incl. **"a required dimension unknown → applicable"**. This is already the fail-loud default v2 needs for matching. |
| `cf/query.py` | — | `get_constraints(skill_id)` retrieval interface. |
| `cf/capsule.py` | — | v1 capsule: **all** applicable constraints, no semantic minimization. Matches "deliver rules, don't adjudicate". |
| `cf/sync.py` | — | `UNCHANGED` / `SOURCE_CHANGED` / `SOURCE_REMOVED` / `NEW_CANDIDATE`, writes nothing, three-strategy re-anchoring. **This is version-drift detection, already built.** |
| `cf/inventory.py` + `cf/discover.read_source` | 388 | skill-first discovery; reads with `newline=""` (CF-020) so hashes are over real bytes. |
| `cf/lifetime.py` | 856 | per-turn re-statement at `ChatLLM.stream_chat`, **after** all three compaction layers. Verified in the real model request. |
| `cf/vibe_integration.py` | — | installs into the **real** `ToolRegistry`; removes the "simulated injection point" label. |
| **CF-024's rule** | `cf/qualification.py` | `complete` = every gate closed **AND the gate set covers the adapter**. This is literally "cannot find a rule ≠ there is no rule", already mechanised. Reuse the shape for library coverage. |

### 1b. Small changes

| module | change | size |
|---|---|---|
| `cf/skillstore.py` | add **skill-level** `source_version` (pinned ref) + per-file `content_sha256`. Today `source_hash` is per-constraint, so drift is detectable per rule but **a skill has no version identity** — the exact thing "绑定具体版本或内容哈希" asks for. | ~3 fields + validator |
| `cf/skillstore.py` | add `exception: str` to `ConstraintRecord`. No schema in the project has one. | 1 field |
| `cf/skillstore.py` | add `policy_origin ∈ {user_explicit, system_default, model_inferred}`. Today `origin` means *which extraction path* (deterministic/semantic) and `store.provenance` is a source pointer — neither separates the three the advisory names. | 1 field + enum |
| `cf/review.py` | card must present the **enclosing unit + `heading_path`**, not only the span text, and must surface `exception`. Reviewer cannot judge a condition from a bare span. | render-only |
| `cf/lifetime.py` → new wrapper | move the injection from *every model request* to the **pre-invocation point**: `loop.py:1063 _process_tool_calls` → `:1254 _invoke_tool`. Reuse the discipline, change the position. | new thin module |
| `fieldtest/phase16/keyed.py` | promote out of `fieldtest/` into a build-time tool. It is build-only, so its non-determinism is absorbed by review (§3). | relocate + tests |

### 1c. Missing entirely

1. **Runtime task-constraint recorder.** Nothing records "what the current task is constrained by". No module, no schema.
2. **Library ↔ task matcher.** `applicability.py` matches *declared scope against static context*; it does not match a task description against library rules.
3. **Coverage status vocabulary.** `verdict.Status` has `UNKNOWN` / `NOT_DELIVERED` / `CONDITIONAL` about *delivery*. There is no `NOT_IN_LIBRARY` / `VERSION_MISMATCH` / `COVERAGE_INSUFFICIENT` about *the library*.
4. **Version-bound build record.** No artifact says "library entry E was built from skill S at version V by run R with raw response B".
5. **`exception` as a first-class concept** — see 1b.
6. **A pre-invocation interception point** on any platform other than an in-process Python wrap (§7).

---

## 2. The identity gate: actual definition, actual failures, and what offline building changes

**Not modifying any threshold.** Recording what the gate is.

### 2a. The canonical artifact (`fieldtest/phase15/canonical.py`)

Pipeline: provider output → `constraintrelay.semantic.validate` → `constraintrelay.semantic.ground` →
**canonicalization** → canonical candidate artifact.

The one structural claim: *a quotation's identity as a pointer into a document
is which source lines, inside which structural unit, it designates.* So the
canonical span is **the line range the grounded span intersects, clipped to its
enclosing `constraintrelay.structure` unit**. Only NFC + whitespace-run collapse are applied.
Deliberately **not** normalised: letter case, punctuation, wording, field order
within a value, synonymy.

### 2b. The six conditions, as applied to `deepseek-flash` v3

| condition | `volatility` | `market-microstructure` |
|---|---|---|
| valid schema, all runs | PASS | PASS |
| fabricated quotations = 0 | PASS (0) | PASS (0) |
| grounding succeeded, all runs | PASS (12/12, 14/14, 14/14 exact) | PASS |
| **identical canonical candidate sets** | **FAIL** — 12 / 14 / 14 | **FAIL** — 29 / 87 / 55 |
| **identical canonical grounded spans** | **FAIL** | **FAIL** |
| **identical semantic fields** | **FAIL** | **FAIL** |

`all_pass: false`. Common to all three calls: **1** of 12 and **3** of 29.
`volatility` was stable in *where* (all 12 span locations in all three calls,
same 5 units) and unstable in *what*. Earlier, `deepseek-v4-pro` failed v1 (raw
bytes) and v2 (canonical artifact, 14/9/14 and 21/24/24, **12 ungroundable
paraphrases**). Three specs, three failures, nothing adopted.

### 2c. Effect after offline building — per item

| gate item | after offline build + human review |
|---|---|
| schema valid | **Still fully binding, and passes.** A malformed build response is a build failure. |
| 0 fabricated quotations | **Still fully binding, and passes.** This is the property that lets a library entry quote the source. |
| grounding succeeds | **Still fully binding, and passes.** No span, no entry. |
| identical candidate **sets** | **Ceases to be a release gate; becomes a build-coverage question.** The delivered artifact is the reviewed, hashed TOML — not the model call — so run-to-run churn no longer makes the product irreproducible. But it does mean **one build call under-covers**: on the same backend, best single run reads 86.6 against the 3-run union's 93.0 on retired H-A. A one-off build should union several calls, which is affordable precisely because it is one-off. |
| identical grounded **spans** | Same. Review fixes the span; the reviewed span is the authority. Note Phase 16's largest residual error class is **span selection** (7 of 23 misses), so this is the item review must actually work on. |
| identical semantic **fields** | **Absorbed at build time, inherited at run time.** Fields are human-confirmed in the library. But the new **runtime** task-constraint extractor cannot be reviewed, so the field instability moves there and is live. |

**The one-line consequence:** *offline building absorbs the identity gate's
failures; runtime extraction inherits them.* So the runtime extractor must not
be the authority on whether a rule applies — it is a **retrieval key** into the
reviewed library, and any gap is marked. That is already the Owner's shape; this
is the mechanical reason it is the right one.

---

## 3. Old defects vs the new flow

| defect | blocks v2? | why |
|---|---|---|
| **B5** retry overwrites the response body | **YES — pre-condition** | a build run's raw responses are the provenance of every library entry; an overwritten body destroys the audit trail for that batch. The NEW-path fix exists; require it and **assert** `*.superseded.*` is written. |
| **CF-023** run path unmodelled | **Only for the file route** | predicting delivery from bootstrap files is what CF-023 breaks. An in-process pre-invocation wrap **observes** instead of predicting, so the defect does not reach it. This is an argument for §7's platform choice, not a repair item. |
| **CF-026** unguarded by-name shadow read | **Only if `phase15/call_ledger.py` is reused** | it is a call-accounting module. An 8-call build does not need a 273-call budget ledger. Either don't reuse it, or fix it first. Do not reuse it silently. |
| **B1 / B2 / B3** budget self-reference, mutable ledger rows, bypassable quarantine | **Only if that harness is reused** | same logic as CF-026. All three are paid-experiment integrity, not extraction logic. |
| **B4** 32768 justification unsound | no | the *choice* stands on affordability; record it, don't re-derive. |
| **CF-025** Hermes pin is legacy | no | only if Hermes is chosen. It is not. |
| **CF-002** `.clinerules` unconsumed | no | discovery-set disclosure. |
| **CF-024** coverage-aware `complete` | **already fixed, and load-bearing** | reuse it as the model for library coverage. |
| **CF-013 / CF-016 / CF-020 / CF-022** fingerprint surface, roster, CRLF, per-profile reads | **already fixed, and load-bearing** | content-hash binding is only meaningful if the bytes hashed are the bytes on disk. These four are exactly that. |

**Pre-implementation items: B5 only, plus a decision on CF-026/B1–B3 by
not reusing that harness.** Everything else is recorded, not reopened. No old-route
work is expanded to serve the switch.

---

## 4. Platform and first skills — one recommendation

### Platform: **Vibe-Trading agent loop**, pinned `dddf96e`

On-host at
`~/.openclaw/workspace/projects/research/experiments/skill-scope-bench/snapshots/Vibe-Trading`,
`git status` **clean**.

Why this one:

1. **It is the only platform where ConstraintRelay has a verified in-process delivery.** Phase 2 installed into the real `ToolRegistry`; Phase 3 wrapped the real `ChatLLM.stream_chat` and survives all three compaction layers (`_microcompact`, `_context_collapse`, `_auto_compact`).
2. **It has a real pre-invocation decision point we can block at.** `agent/src/agent/loop.py:1063 _process_tool_calls` → `:1254 _invoke_tool`, with an existing `tool_def.is_readonly` distinction at `:1153`. Synchronous Python → a pause/confirm is actually possible.
3. **The corpus and the runtime are the same repository.** The first skills *are* Vibe-Trading skills; no cross-corpus adaptation, and content-hash binding is trivially reproducible against a pinned clean checkout.
4. **It is the user-facing case.** The project's anchor failure — a Class C scope restriction (`not for direct live-trading signals`) lost to truncation — is a Vibe-Trading skill.

Rejected, with reasons:

* **OpenClaw** — its adapter is **provisional** for exactly the run-path reason (CF-023), and its hook `llm_input` (`attempt-prompt-support.ts:341-360`) is **`void`, not awaited, so it cannot delay or stop the call**. Delivery is possible; interception is not. Its route is the static bootstrap-file route the advisory says to demote.
* **Hermes** — `pre_api_request` does receive the real `request_messages` every API call, but the pinned commit is legacy (CF-025) and no checkout of the intended `v2026.9.24` target exists on this host.

### First skills: **`ichimoku`, `thesis-tracker`, `behavioral-finance`** — 3 skills, 5 files

All three are members of **Phase 7 `STRATUM_A`**, whose membership is published
in `fieldtest/phase7/GOLD_DEFINITION.md` §2 and whose role is declared
**development / diagnostic, labels readable, scored repeatedly** (§8). Its
evidentiary value is already zero, so building on it costs nothing.

Measured locally this round (no provider call):

| skill | files | chars | examination units |
|---|---|---|---|
| `behavioral-finance` | 1 | 10,618 | 56 |
| `ichimoku` | 3 | 2,642 | 65 |
| `thesis-tracker` | 1 | 8,604 | 80 |
| **total** | **5** | **21,864** | **201** |

By grain: `heading` 52 · `prose` 49 · `table_row` 45 · `list_item` 36 ·
`fence` 16 · `frontmatter` 3. Deterministic candidates: **22**.
`assert_inside_one_parent` passes on all 201.

Why these three: `ichimoku` is **multi-file** (exercises per-file hash binding);
`thesis-tracker` is table-heavy (45 `table_row` — the grain that was the
wholesale failure, 56.2 → 100.0); `behavioral-finance` carries 16 `fence` units
— the one kind that **regressed** under the keyed path (92.9 → 64.3 on dev A),
so the first batch deliberately includes the known-weak kind.

**Boundary note (Owner item 6).** I did not open any Phase 9 file to check
membership. `STRATUM_A` is certain *positively* — it is published in a
development-set definition. Its **disjointness from the sealed set is a protocol
property, not a fact I verified**: Phase 16 ran on `STRATUM_A` with
`assert_phase9_untouched()` passing. If that is not sufficient, the pre-condition
is a disjointness assertion run by someone who may read both lists under
protocol — and failing that, draw the first batch from skills with no gold
annotation at all, which serves an integration set equally well. `STRATUM_HA`
(Phase 8 retired) is **excluded** from the first batch: it carries a spent
read-budget history and is not needed.

---

## 5. First-batch rule production and review

### Extraction path: the Phase 16 **keyed** path, unioned with the deterministic extractor

`fieldtest/phase16/keyed.py` + `prompts.py` + `run_arm.py`, union'd with
`cf/candidates.py` via `cf/semantic.union`.

Chosen because it is the only path with a **denominator**: 1,555 of 1,555
required verdicts returned, silent-unit rate 0 by construction, and every miss
attributable under a proven 8-bucket partition. For building a *reviewed
library*, attributable silence is worth more than the 0.6-point recall gap
against a 3-run union. **The Phase 16 freeze bar failing is not an objection
here** — that bar was about freezing an extractor ahead of an unseen
measurement, which this is not.

### Expected volumes

| | figure | basis |
|---|---|---|
| examination units | **201** | measured locally, above |
| provider calls | **8** | `ceil(201 / BATCH_SIZE=28)` |
| expected positive records | **~78** | dev A ran 314 positives / 811 units = 38.7% |
| expected negative records | **~84** | dev A `NON_CONSTRAINT` 313 + `NO_SCOPE_EFFECT` 25 of 811 |
| **review cards** | **~30–40** | dev A 139 cards over 314 positives ≈ 0.44 cards/positive |
| **negative units to spot-check** | **~40** | 100% of `fence` + `table_row` negatives (the two measured failure kinds) + 20% random of the rest |

### What the reviewer must check, per card

1. **Source context** — the enclosing examination unit *and* its `heading_path`, not the span alone. A condition cannot be judged from a bare span.
2. **Condition** — is the recorded `condition` what the source says: not strengthened, not weakened, not dropped, not invented.
3. **Exceptions** — **the field does not exist yet** (§1b). Until it does, exceptions must be recorded in `note` and the gap stated.
4. **Scope** — is the inherited scope right, and is its cited heading unit the right one.
5. **Span selection** — is this the right span. Phase 16's single largest residual error class (7 of 23 misses) is "right unit, wrong clause".
6. **Holder** — is the obligation the agent's. Phase 7 measured this as **52% of all extraction errors**.
7. On the negative sample: is this unit really not a constraint. Phase 14's census found **46 of 102** "false positives" held a real constraint the gold never anchored, so negatives are not free.

### Cost

| | estimate | basis |
|---|---|---|
| **provider** | **≈ $0.054**, ≈ **$0.07** with 2 reserve calls | $0.4438 / 66 calls = $0.00672/call, conservative tariff. Against $3.55 remaining of $5.00. |
| **human review** | **≈ 2–2.5 h** total, ≈ 45 min/skill | 35 cards × ~2.5 min (with context, condition, exception, scope) + 40 negatives × ~1 min |

**Not authorised by this document.** The 8 calls are an estimate; they need
their own authorisation.

### Who reviews

Not pre-assigned to the Owner. Proposed split: I assemble the review packet
(evidence + context window + heading path + proposed field values) and take a
first pass; a second reviewer — Owner, or `davinci-reviewer` via
`sessions_spawn(context="isolated", model="openai/gpt-5.6-sol")` — adjudicates a
sample and yields a disagreement rate. If I am the only reviewer, the provenance
is **developer-exposed and output-inspected**, the weakest class in this project.
That is acceptable *here* and only here, because this batch is integration
acceptance, not accuracy evidence (§6).

---

## 6. Acceptance scenarios — written from source, independent of extractor output

Each scenario's expectation is hand-written from the skill text **before** any
extractor runs, and is a structured expectation (fields, ids, hashes), never a
prose match. **These are development and integration acceptance for the first
batch. They are not a test set and not evidence of product accuracy.** Frozen
evaluations are unchanged; large-scale library-quality measurement is a separate
design.

| # | checks | pass condition |
|---|---|---|
| **S1** | the current task's constraints are recorded correctly | for 3 hand-written task descriptions, the recorded task-constraint record matches a hand-written expectation field by field, incl. `policy_origin` for each of the three origins |
| **S2** | the rules that should arrive, arrive — and nothing irrelevant | for each task, a hand-picked **must-deliver** set and a hand-picked **must-not-deliver** set, both read off the source. Delivered ⊇ must-deliver, and Delivered ∩ must-not-deliver = ∅ |
| **S3** | uncovered / version-mismatched is marked explicitly | (a) query a skill absent from the library → explicit `NOT_IN_LIBRARY`; (b) change one byte of a built skill file → explicit `VERSION_MISMATCH`; (c) a library built on 2 of 3 skills, task touching the third → `COVERAGE_INSUFFICIENT`. In all three: **never an empty delivery presented as "no constraints"**, and **no automatic rebuild** |
| **S4** | the record is traceable | every delivered rule carries library entry id + `constraint_version` + source file + span + `content_sha256` + build-run id, and the hash **re-verifies** against the pinned file |
| **S5** | *pause and request confirmation* | **Included only on the Vibe-Trading `_process_tool_calls` wrap**, because that call is synchronous in-process Python and can actually block. **Excluded on OpenClaw** — `llm_input` is `void` and cannot delay or stop the call, so an acceptance there would test a pause that does not exist |

**No scenario asserts that the agent obeyed.** Successful delivery is delivery.
S2 passing means the rule reached the decision point, nothing more. Per Owner
item 10, unknown or mismatched marks coverage as insufficient and **does not**
trigger automatic library building; this is a design default for the minimal
loop, not a release decision, and existing authorisation policy still applies.

Explicitly **out of scope** for v1, per the advisory: general violation
adjudication, multi-agent authority delegation, runtime automatic library
building.

---

## 7. Steps to the first working loop

| # | step | gate / note |
|---|---|---|
| 0 | this snapshot + this proposal | **done** |
| 1 | pre-conditions: assert the B5 superseded-body writer; decide **not** to reuse `phase15/call_ledger.py` (closes CF-026 / B1–B3 by scope); obtain the `STRATUM_A` ∩ Phase 9 disjointness assertion | nothing paid runs before this |
| 2 | library schema: skill-level `source_version` + per-file `content_sha256`; `exception`; `policy_origin`. Additive, 3 field groups | `test_phase1_store` + `test_v01_surface` must stay green; re-examine `stash@{0}` first |
| 3 | build the library for the 3 skills: 8 calls → union → review cards → confirm → `.constraintflow/skills.toml` with hashes and build-run id | needs its own call authorisation |
| 4 | runtime: task-constraint recorder (new) + matcher over `applicability.py`/`query.py` + delivery wrap at `loop.py:1063` modelled on `cf/lifetime.py`'s discipline | observe the real request, never predict it |
| 5 | coverage vocabulary: `NOT_IN_LIBRARY` / `VERSION_MISMATCH` / `COVERAGE_INSUFFICIENT`, on the CF-024 pattern. No auto-build | silence is a bug, not a result |
| 6 | run S1–S5, record outcomes and failures verbatim | integration acceptance only |

**Total cost to the first green loop:** ≈ **$0.07** provider, ≈ **2.5 h** human
review, plus implementation. Steps 2 and 4 are the only new code of substance;
everything else is relocation, three schema fields, and a status vocabulary.
