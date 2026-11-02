# ConstraintRelay v0.1 — known limitations and adapter debt

Consolidated from `PHASE1.md`–`PHASE5.md`, which keep the detail and the
measurements. Nothing here is a surprise discovered after the fact; each item
was measured and written down when it was found.

---

## 1. Extraction

**This is the dominant correctness uncertainty in v0.1, and it is the reason
the product contract is candidate discovery plus human confirmation rather than
automatic extraction.** Full measurement in `PHASE7.md`, an internal file
not shipped in this preview.

**It is pattern-based.** A constraint written without a modal verb —
*"Backtest only."* — is not proposed. On the Vibe-Trading corpus, 16
constraint-shaped sections produce no candidate and are counted as unresolved.

**Precision and recall, measured against a gold set built from source.** 439
Class C constraints hand-enumerated over 14 skills, frozen at content sha256
`fe85ee87…` before any fix was written:

| | precision | recall | F1 |
|---|---|---|---|
| all 14 skills | 85.1% | 39.0% | 53.5 |
| **random stratum (8 skills)** | **80.0%** | **31.3%** | 45.0 |

The random stratum is the one that generalises; the pooled figure is lifted by
six skills *chosen* for rule density. **Take precision ≈ 80% and recall ≈ 31%:
one candidate in five is wrong, and two real constraints in three are never
proposed.**

**These are development-set figures, not held-out ones.** The set was frozen
before the fixes and has not been edited — but it was read, its misses were
analysed, and the fixes were written against that analysis, so the score
measures fit to these 14 skills as well as extraction quality. It is
reclassified as a development / diagnostic set in `PHASE7.md` §8, which also
records the dev / holdout / external-corpus protocol the next extraction change
must follow: freeze the extractor, then score once on a sealed holdout, then
check a second repository. **Until that holdout exists there is no unbiased
estimate of extraction quality on unseen skills, and no number here may be
offered as one.** The direction of the bias is known — a tuned-against score is
optimistic — so 31% recall is, if anything, the generous reading.

An earlier audit reported 67.5% strict precision on a 40-record sample. That
sample was drawn from extractor *output*, so it could not see a miss; it is
superseded by the figures above and kept in git history only.

**26% of constraints are in structures the extractor does not read.** 114 of
439 — table rows, fenced blocks, fence-internal lines, headings — a hard 74%
recall ceiling before any pattern work.

| structure | gold | recall | | subtype | gold | recall |
|---|---|---|---|---|---|---|
| list item | 244 | 44.7% | | **threshold** | **123** | **19.5%** |
| table row | 87 | 19.5% | | obligation | 101 | 46.5% |
| paragraph | 81 | 55.6% | | data | 64 | 34.4% |
| fence line | 14 | 0.0% | | prohibition | 49 | 81.6% |
| fenced block | 10 | 0.0% | | scope | 27 | 74.1% |
| heading | 3 | 0.0% | | **ordering** | **22** | **4.5%** |

(Six smaller subtypes omitted here — output 12, veto 11, procedure gate 9,
weight 9, format 6, style 6. All twelve, with per-round columns, are in
`PHASE7.md` §2; the structure and subtype columns each sum to 439.)

**Thresholds are the largest subtype in the corpus and the worst served.** A
threshold is written as a table row carrying a number and no modal verb —
`主板 | 净利润为负 且 营收 < 3 亿`. A pattern for bare numeric bounds was
measured and declined: it lifted table recall to 54% at a cost of 9 points of
precision, by importing descriptive reference tables (SEC form glossaries,
signal-interpretation tables) whose numbers belong to a filing rule, not to the
agent.

**Tables are now read at row level** — the one fix adopted in Phase 7. Table
recall 0% → 19.5%, overall 35.1% → 39.0%, for 1.9 points of precision. The
split is local to `cf/candidates.py`; `blocks.segment` keeps the frozen
semantics the marker and verdict layers index into.

**52% of all errors are one defect: the obligation's holder is not the agent.**
The text says *must* and means it — for a statute (`资本利得需计入企业所得税`),
an SEC filer (*>10% customer concentration must be disclosed*), a data API
(*tushare 3000 积分*), or a quoted investor (*Buffett: "a great company
should…"*). Two structural fixes aimed at this family — excluding blockquotes,
excluding lead-in labels — were measured and **both lost recall**, because
those structures carry real constraints too. The holder problem is semantic.
It needs a component that can identify the subject of an obligation, which v0.1
does not have and does not pretend to have.

**Single-rater, not blind.** The same party wrote the extractor, drew the
sample and labelled it. No inter-rater agreement figure exists. One corpus, one
house style: the *method* transfers, the rates do not.

**Three records in 1,019 are mid-sentence fragments.** `correlation-regime-C019`
is stored as *"6. **Scope of the attribution claim.** The alarm mechanically
names only fast,"* — block segmentation split a sentence, so no complete rule
was captured. Measured across the whole store: **3 of 1,019 (0.3%)** records
end in a comma. Recorded, not fixed.

**Prose files only**, max 3 levels deep, 64 files, 512 KB each. Python files in a
skill directory are recorded as skipped sources, not scanned — 195 source files
were skipped in this corpus, each with a reason.

**Calibrated on one corpus.** Every exclusion rule was tuned against
Vibe-Trading's house style.

### 1.1 The semantic path is experimental, and it is NOT reproducible

`cf.api.discover` accepts an optional model client that proposes candidates the
pattern layer cannot reach. It works: on the two probe skills it closes most of
what regex could not (tables 0 → 56%, fences 21 → 67%, lists 40 → 92%), with
grounding rejecting anything not present in the source. **But v0.1 makes no
claim of deterministic or reproducible semantic extraction, and that is a
measured conclusion rather than caution.**

Three independently frozen backend specifications were written *before* their
own probes, and all three failed:

| spec | backend | determinism required at | result |
|---|---|---|---|
| v1 `8789e38` | `deepseek-v4-pro`, thinking off, `temperature 0` | raw bytes | 2 distinct outputs in one 3-call probe, 3 in another |
| v2 `7c39fc5` | same | canonical ConstraintRelay artifact | candidate sets **14 / 9 / 14** and **21 / 24 / 24**; 12 ungroundable paraphrases |
| v3 `3fc2270` | `deepseek-flash`, thinking off, `temperature 0` | canonical ConstraintRelay artifact | candidate sets **12 / 14 / 14** and **29 / 87 / 55**; **1** and **3** candidates common to all three calls |

The variation is substantive, not representational: whole constraints appear
and disappear between identical calls, and the granularity of a finding moves
(one per formula line on one call, one per clause on the next).
Canonicalization at the structural level does not absorb it, and the gate was
**not** weakened to majority or k-of-n agreement — with 1 of 12 candidates
surviving three calls, an agreement rule would not have rescued it either.
`temperature: 0` was a real control in v3, not a fake pin: `thinking` was
explicitly disabled and verified absent on every response.

So the contract is:

* deterministic ConstraintRelay core — **supported**;
* semantic extraction — **optional / experimental assisted candidate
  discovery**;
* **human confirmation required**;
* **no claim of deterministic or reproducible semantic extraction.**

Enforced structurally, not by discipline: `client=None` is the default and
degrades to the deterministic extractor rather than erroring, `Discovery` holds
no store handle and exposes no promotion method, and `import_constraints` has
never read a semantic candidate. Evidence, verbatim and permanent:
`fieldtest/phase15/determinism_evidence/`, `probe_v2_evidence/`,
`probe_v3_evidence/`.

## 2. Identity

**`skill_id` is the skill directory path.** Renaming the directory produces a
new `skill_id`; the old skill's constraints stay in the store and are reported
`SOURCE_REMOVED` rather than re-attributed. Matching renames by content
similarity was rejected: that is inferred identity.

**`agent/SKILL.md`-style skills may be unreachable.** A `SKILL.md` the harness's
own loader does not offer to `load_skill` is still inventoried, but its
constraints can never enter an active set. Reported as `loadable: false` rather
than dropped — in this corpus that is 10 constraints.

## 3. Delivery

**One harness adapter: Vibe-Trading.** Hermes and OpenClaw are untouched.

**The provider call was stubbed in every measurement.** Requests were captured
at `stream_chat`'s argument — which *is* the request — but nothing measures what
a model *does* with a restated constraint, whether restating it sixty times
changes how it is weighted, or whether a released constraint is "forgotten" in
any behavioural sense.

**Subagents and cron are not covered.** `src/swarm/worker.py` runs its own loop
with its own `result[:10_000]`, and nothing here reaches it.

**Constraints are delivered on skill *load*, and restated thereafter.** There is
no separate signal for skill *use*.

## 4. Lifecycle

**There is no release policy.** Something has to call `release`. A model that
never calls `release_skill` keeps everything active until the task ends — which
is safe by design, and costs 0.8%–3.2% of the compaction threshold on
representative sessions.

**The measured benefit of releasing ranges from 0% to 53%**, and once it was
*negative*: `equity-research` finished 1.2% worse over the run because each
`release_skill` call costs a turn and its two releases did not pay for them. The
saving tracks the share of constraint mass in phase-scoped rather than
task-framing skills almost exactly.

**`release_skill` adds a tool to the model's surface.** Earlier phases refused
to add tools; this one does, because an explicit lifecycle with no actor inside
the run is unreachable. `LifetimeOptions(release_tool=False)` disables it.

**No model has voluntarily released anything in a measured run.** Every release
in the trajectory runs was scripted. The conservative-release arm is an upper
bound on a cooperative model, not a prediction.

**Activation is permanent within a task** unless released. The set only grows
until `task_scope` resets it.

## 5. Measurement methodology

**The trajectories are scripted, not observed.** They are grounded in the
corpus's own `category` metadata and every skill name is checked against the
live loader, but no real session was recorded. `fieldtest/phase6/` contains a
runner for real, non-scripted sessions; see §9.

**`hold_scope` assignments in those trajectories are a judgement.** Whether
`tushare` is task- or phase-scoped in a real session is arguable, and the
results depend on it heavily.

**The bulk tool is a local fixture.** The loop, the compaction layers and the
thresholds are real; the data is synthetic and inert.

**Token counts use the harness's own ÷4 heuristic**, not a tokenizer.

**`auto_compact` is reached only in the 80-turn stress run.** In the shorter
runs "compaction frequency" means microcompact and collapse.

## 6. Deliberately not built

Each of these was measured before being declined, not assumed away.

| Not built | Why |
|---|---|
| Semantic compression | Representative sessions cost **310–1,281 est. tokens/request, at worst 3.2%** of the compaction threshold, inside requests of ~28,000 tokens. The capsule is not what strains the budget; tool output is. Even the pathological 14-skill case (14.0%) is halved by conservative release with no compression. |
| Learned / ranked selection | Same numbers. Ranking is also the one thing that can silently drop a constraint that still applied, which is the failure this project exists to prevent. |
| Heuristic expiry | "Unused" and "no longer applicable" are different claims; only the second justifies dropping a constraint. |
| Hermes / OpenClaw adapters | Out of scope for v0.1. Their qualification work is preserved under `constraintrelay internals`. |
| Swarm support | `swarm/worker.py` is a second loop with its own truncation. |
| Exact dedup as an optimisation | It is **correct** and on by default, but it saves 0–88 characters here: only **2 of 463** distinct canonical texts in the corpus are duplicated at all. It is insurance against a corpus with shared boilerplate, not a saving on this one. |

## 7. Adapter technical debt

### `CF-ADAPTER-DEBT-1` — the `estimate_tokens` monkeypatch

`cf/accounting.py` replaces the module-level `src.agent.loop.estimate_tokens` in
the Vibe-Trading harness so that its compaction thresholds are evaluated against
the request that will actually be sent.

**This is adapter debt, accepted for the Vibe adapter. It is not a general
ConstraintRelay architecture requirement.** The general requirement is:

> a harness must let a delivery layer declare what it will add to a request
> *before* the context budget for that request is computed.

Why it was needed: the capsule is deliberately kept out of `messages`, so it is
absent from history, the transcript and the summarizer — and the harness offers
no hook by which a delivery layer can declare what it is about to add. Without
the patch, the harness sent requests over its own 40,000-token auto-compact
threshold on **37 of 80 turns** (peak 51,974).

Known limits: it is **process-wide**; `swarm/worker.py`'s private
`_estimate_tokens` is untouched; it is reversible only if the caller calls
`uninstall()`, which is why `AccountingPatch` is a context manager.

**Retirement condition:** delete the patching path once a harness-side budget
hook exists, keeping `pending_messages` as the declaration the hook consumes. No
second adapter should copy the patch by default.

Full entry: `DEFECT_LEDGER.md`.

### Upstream hazard found while packaging (not ConstraintRelay's defect)

Vibe-Trading's `_sync_provider_env` copies a provider's key into
`OPENAI_API_KEY` but only sets a base URL if one is configured — its docstring
says *"All base URLs must be set explicitly in .env — no hardcoded defaults."*
With `LANGCHAIN_PROVIDER=deepseek` and no `DEEPSEEK_BASE_URL`, the OpenAI SDK
defaults to `api.openai.com` and **the DeepSeek key is sent to OpenAI**. A
missing base URL for a non-OpenAI provider fails *unsafely*: it mis-routes a
credential instead of raising a configuration error.

This bit during packaging, before the guard existed. `fieldtest/phase6/`
therefore requires `--base-url` explicitly and refuses to start unless the host
is on an allow-list for that provider. If you run real sessions through any
other path, set the provider's base URL first.

## 8. Licence and attribution

The package is **Apache-2.0** (`LICENSE` is the canonical, unmodified text,
md5 `3b83ef96387f14655fc854ddc3c6bd57`; `NOTICE` carries the attributions).

ConstraintRelay **vendors no third-party source**, but it does contain Python
*ports* of algorithms read from the harnesses it was measured against, and a
port of a function is derived work. As of **2026-10-04 every upstream licence is
verified** and the checklist item is closed. Three are read from a checkout on
this machine:

* **HKUDS/Vibe-Trading** — MIT — `cf/vibe_trading_paged.py`, `cf/deliver.py`
* **openclaw/openclaw** — MIT — `cf/openclaw.py`
* **NousResearch/hermes-agent** — MIT — `cf/profiles.py` — at
  `~/.hermes/hermes-agent`, commit `2517917de`, the exact commit the port was
  derived from

Two are not checked out here, so their licence was read from the upstream
repository at the pinned commit and `NOTICE` records the git blob sha:
**Conway-Research/automaton** `d8f8168` (MIT, `cf/conway.py`) and
**openai/codex** `bcd6d9a` (**Apache-2.0**, `cf/codex.py`, `oracle/`). Both
belong to the paused qualification machinery and neither is in the v0.1 product
surface.

Two things this exercise turned up, both worth carrying forward:

* **hermes-agent was never actually unverifiable.** The checkout had been on
  disk the whole time — the hermetic test suite imports from it. The entry sat
  in an "unverified" section for want of a file that was already there, and no
  check ever tested the claim. `tests/test_release_readiness.py` now verifies
  the md5 of any checkout `NOTICE` says it read from.
* **openai/codex is Apache-2.0, not MIT** — the only upstream that places a
  continuing obligation on this distribution (§4 notice retention, §4(d) NOTICE
  propagation). Its `Copyright 2025 OpenAI` attribution is reproduced in our
  `NOTICE`. Had this stayed unverified until release, we would have shipped
  without it.

One structural note: the containing git repository (`dreamio/intent-checkpoint`)
has **no LICENSE of its own**, and ConstraintRelay is a subdirectory of it.
Publishing ConstraintRelay publicly most likely means extracting it into its own
repository; that is a packaging decision, not a code one, and it is in the
checklist.

## 9. Not yet done

**Real, non-scripted sessions have not been run, and are blocked by code.**
The runner is written and reviewable at
`fieldtest/phase6/run_real_sessions.py`, with endpoint and spend guards. It
currently refuses to start: the DeepSeek credential in this environment is the
one that was mis-routed on 2026-10-03, and its fingerprint is in
`cf.provider_guard.REVOKED_FINGERPRINTS`, so the guard rejects it before any
request is formed. Once a replacement key is in the environment the guard
passes and the one command in `RELEASE_CHECKLIST.md` runs it.

Every working-set figure in v0.1 therefore comes from scripted trajectories, and
that is stated wherever those figures appear.
