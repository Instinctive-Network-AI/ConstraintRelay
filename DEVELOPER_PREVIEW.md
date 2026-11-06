# ConstraintRelay — developer preview release list

**One page, six questions.** What version, how to install and run it, which
hosts and which client paths are verified, what a receipt actually looks like,
how it sits against the field, and what is still blocking. Every figure here
is gated by `v2/verify_claims.py` against the suite that produces it — this
document is a registered claim surface, so a number that drifts fails a check
rather than ageing quietly.

**Status: NOT RELEASED.** The blockers in §6 are real and two of them are
mine, not the environment's.

---

## 1. The pinned version

| | |
|---|---|
| product version | `constraintrelay.__version__` = **0.1.0a1** — the preview version fixed by the Owner's ruling of 2026-10-25. `pyproject.toml`, `constraintrelay.__version__` and `constraintrelay.api.VERSION` are asserted equal by `v2/naming_gate.py`; `constraintrelay.api.VERSION` reads the package rather than restating it |
| names | product **ConstraintRelay**, distribution and CLI **`constraintrelay`**. One source, `cf/naming.py`, bound to `pyproject.toml` by the same gate |
| proposed tag | **`v0.1.0a1`** — a *proposal*. Nothing is tagged, and the gate fails if it ever is this round |
| container API version | `constraintrelay.container_api.VERSION` = **0.2.0-dev-preview** |
| commit this list describes | **whatever commit `v2/out/INSTALL_VERIFY.json` records** — not retyped here. `v2/verify_claims.py` resolves that sha and fails if anything the install verification covers (every shipped package, the licence files, **this README it executes**, both host examples, and the checker itself) has changed since. A hand-typed sha in this row would be a second path to the same fact, and the one that goes stale |
| Python | **3.11+**; every suite here was run on 3.13 |
| runtime dependencies | **none.** Standard library only. Nothing in the delivery path imports `urllib`, `http`, `socket`, `requests` or `ssl` |
| licence | **Apache-2.0**, `LICENSE` + `NOTICE`, both shipped via `license-files`. Five upstream licences verified at the exact commit each port derives from, blob shas in `NOTICE` |
| published to an index | **no.** The ruling is no PyPI release for this preview. `constraintrelay` is unregistered on PyPI (404, read 2026-11-01) — so the name is free, and that is not a reason to publish |

Only the names in `constraintrelay.container_api.__all__` are promised by 0.2. Everything
else in `constraintrelay.container*` works, is readable, and may move.

## 2. Install and quick start

```bash
pip install .      # from a checkout of this repository
```

**Not from PyPI.** Nothing is published under `constraintrelay`, so an index
install cannot work. Under the previous name it was worse than not working:
`constraintflow` on PyPI is an unrelated project — *"A DSL and tools for
neural network certification"*, 0.1.1, MIT, uploaded 2025-09-12
([pypi.org](https://pypi.org/project/constraintflow/), read 2026-10-25) — and
two documents sent readers to it. The gate that forbids a runnable index
install is unchanged and now reads the name from `pyproject.toml`, so it
followed the rename on its own. See §6.

The five-minute path is in [`README.md`](README.md) and is **not retyped
here** — it is executed from the document's own bytes by
`v2/readme_commands.py`, which both `v2/integration_propose.py` and
`v2/install_verify.py` import, so there is one parser and no second copy to
drift. In outline:

1. mark a declaration in your own skill file — an HTML comment, no new
   language;
2. or let `container propose` suggest the markers and review the `git diff`,
   which **is** the act of authorship;
3. `container build` → a version-bound library; `container attest` → a
   manifest draft you adopt, which **is** the act of confirmation;
4. install the guard on your client and open `guard.active(...)`;
5. `constraintrelay receipts` to read the ledger back.

Install verification: **41/42** (`v2/out/INSTALL_VERIFY.json`) — a wheel built
from a clean clone, installed into a fresh venv, with the repository's own
`cf/` **moved aside** so a source import cannot pass for an install. Every
command the README prints is run against the installed console script.
The one red since 2026-11-06 is the hermes example: `backtest-sanity-check` is deliberately PENDING_CONFIRMATION for its re-anchor (Owner ruling 2026-11-05 item 3) and a pending manifest delivers nothing, so the example cannot complete until the project lead confirms. It is not a defect in the host integration.

## 3. Verified hosts, and the exact client path

The claim is per **client path**, not per framework name. A framework that
builds its own client internally is a client ConstraintRelay never saw: it
produces no receipt and **claims no absence**.

| Host | The object that is guarded | Boundary the receipt names | Depth |
|---|---|---|---|
| `openai-python` | `client.chat.completions.create` on **that instance** — never the class, so it cannot reach a client another library built. `guard.uninstall()` reverses it | the `messages` list the method received | **87/87 provider-SDK host checks**, **22/22 provider-SDK host mutations caught**, sync **and** async; in a scratch venv, **90/90 with the real `openai` package** (`v2/out/OPENAI_HOST_REAL_SDK.json`). **Zero provider calls** |
| `langgraph` + `langchain-openai` | `llm.root_client` — the client LangChain built for itself. Nothing in LangChain or LangGraph modified or configured to cooperate | the `messages` list, plus **the JSON body a local HTTP stub parsed** — downstream of the SDK's own serialisation | **15/15 LangGraph host checks**, versions pinned in `v2/out/LANGGRAPH_HOST.json`. An uninstalled control sends no capsule. Nothing billed |
| `hermes-agent` 0.14.0 @ `2517917de` | the agent instance; delivery into `api_kwargs["messages"]` at the two methods the main loop calls (`conversation_loop.py:1172` / `:1176`) | that `messages` list. `chat_completions` **only** — `anthropic_messages`, `codex_responses` and `bedrock_converse` are refused at install | **141/141 hermes host checks**, **15/15 hermes host mutations caught** on a recorded reply, plus a **real-model** smoke 2026-10-22 whose two arm-B receipts verified at run time |
| `vibe-trading` @ `dddf96e` | `ChatLLM.stream_chat` | the `messages` list at that call | one **real model request**, 2026-10-18, one skill, author-confirmed |

**Reasoned about and NOT measured:** `responses.create`, Anthropic, CrewAI,
AutoGen, LlamaIndex, the OpenAI Agents SDK. They all reach a provider SDK
method, which is a reason and not a result. The first two are **refused with
their reasons** rather than approximated.

**No non-Python host has been driven through any of this.**

### Two declared scope boundaries

* **One agent per process, no delegation.** A delegated child builds its own
  agent, which the adapter never wraps. A process that builds agents
  **sequentially** is also outside the supported range — not listed as a
  limitation inside it. The cause of the 2026-10-22 `T2-B` refusal is **still
  unexplained**, so the range excludes it rather than documenting around it;
  reopening condition in [`ROADMAP.md`](ROADMAP.md) §4.
* **The boundary is the in-process message list.** **No remote provider's wire
  bytes have ever been observed by this project.** The LangGraph run is the
  one place bytes were read after serialisation, and that server was a local
  stub this project ran.

## 4. A receipt, from a real run

From `v2/out/HERMES_SMOKE_CELL_T1-B.json` — hermes 0.14.0 against a real
provider, 2026-10-29, one author-confirmed skill:

```json
"capsule_in_request": [1, 0],
"payload_sha256": "787ecf7d…",
"receipt_claims": {
  "assembly_succeeded": true,
  "verified_at_named_send_boundary": true,
  "load_attribution_established": true,
  "received_by_remote_model": null
},
"load_state": {
  "due_ids": ["skills/backtest-sanity-check"],
  "delivered_ids": ["skills/backtest-sanity-check"],
  "withheld_attribution_ids": []
}
```

Four things in it are the whole design:

* `capsule_in_request: [1, 0]` — present in the main-loop request the adapter
  observes, **absent** in the host's iteration-limit summary, which is a
  second decision request **outside the boundary**. The gap is a recorded row,
  not a silence.
* `received_by_remote_model: null` — never `true`. Assembly succeeded,
  verification at a named send boundary succeeded, and what a remote model
  received is a third fact this project does not claim.
* `load_attribution_established` is **three-valued**. `null` means the host
  gave no evidence, or gave it having considered zero loads — told apart by
  `loads_considered`, never collapsed into `false`.
* a verbatim `payload_sha256`, so the reading is re-checkable. An earlier run
  stored none, and byte identity with that request is **permanently
  unavailable** — recorded in `v2/HERMES_SMOKE.md` rather than smoothed over.

  **This cell was re-run on 2026-10-29** to bind the receipt to the ledger row
  that paid for it, so the artifact named above now holds that run's digest.
  The 2026-10-22 reading (`af84bd6c…`) is **not** lost evidence: it is
  preserved in `v2/out/HERMES_SMOKE_T1B_POSTHOC.json`, which is dated to that
  run and holds nothing from any later one.

  **Not** in `v2/out/HERMES_SMOKE_MATRIX.json`, and an earlier version of this
  sentence said it was. The matrix is a **reconciliation**: it re-reads
  whichever cell artifact is current, so the first sweep run after the 10-29
  re-run replaced its `af84bd6c…` / `run_at: 2026-10-22` pair with the 10-29
  one. A file that recomputes itself is not a place to point at for an older
  reading, and naming it as one was the mistake.

  **Re-checkable against what.** The digest is over the message list at the
  observation boundary, canonicalised as

  ```python
  hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False,
                            default=str).encode("utf-8")).hexdigest()
  ```

  A developer trial on 2026-10-27 reproduced it from the **bytes a local HTTP
  stub parsed**, matching exactly — so the receipt names the list that
  travelled, and that is established without trusting the receipt. Until that
  trial, no shipped document said what was hashed, which made "re-checkable"
  a claim a reader could not act on without reading
  `cf/container_receipt.py`. The rule above is itself checked against a real
  receipt by `v2/integration_openai.py`, so a change to the canonicalisation
  turns this paragraph red rather than leaving it stale.

The ledger reads back in four states — `delivered`, `none due`, `MISSING`,
`UNATTRIBUTED` — and **counts, never scores**. There is no 0–100 safety
number, because that would be a judgement this project has not earned.

## 5. Where it sits against the field

Full version, with read dates and what is still an unchecked lead:
[`COMPETITIVE.md`](COMPETITIVE.md).

Six lanes. Five are other people's and are **complementary**: policy
distribution (Cerbos Hub), runtime instruction delivery (LaunchDarkly), action
authorization (Bedrock AgentCore Policy), output validation (the guardrail
products), and request observability (LangSmith, Langfuse, the OTel GenAI
conventions). The sixth is delivery evidence, and of everything checked
nothing else issues a per-request receipt scoped to named declarations at a
confirmed version.

**Two sentences that are withdrawn, because they were not true.** *"Everyone
else guards the output"* — false, LaunchDarkly is on the input side.
*"You cannot see your prompt otherwise"* — false, **a trace holds the full
prompt**; LangSmith's own words are *"every run, with full inputs and
outputs"*. `COMPETITIVE.md` §2 sets a receipt against a trace difference by
difference and names **three places the trace is the better instrument**:
observation depth, coverage, and anything about behaviour.

**The limit that stays in the pitch.** Delivery is not compliance. A verified
receipt means the declared bytes were in the payload at the named boundary. It
is **not** evidence the model read them, understood them, or obeyed them, and
**nothing in this project has measured obedience.** Against a field whose
neighbours claim judgements, claiming one checkable fact is the asset.

## 6. Remaining blockers

**Mine, and they block the preview:**

| # | Blocker | What it needs |
|---|---|---|
| B1 | **CLOSED 2026-10-25** by the Owner's ruling, and re-settled 2026-11-01 when the product was renamed again. The distribution is `constraintrelay`, unregistered on PyPI (404, read 2026-11-01). The gate that forbids a runnable index install stays armed on the stronger reason — nothing is published — rather than being relaxed because the name conflict went away | — |
| B2 | **The release directory is prepared; the repository is not created.** `v2/release_package.py` builds `release/constraintrelay/` from **19 whitelist patterns**: **95 whitelisted files**, with **96 tracked in the fresh history** — the extra one is the `.gitignore` the packager generates, which is shipped but was never copied in (**1 commit, no remote, no tag**), no old `.git`, and both artifacts built and scanned. The ruling says **do not create a remote**, so none exists | An Owner decision to create the public repository and push that history. Nothing here can be pushed by accident |
| B3 | **No tag, by ruling.** `v0.1.0a1` is in `pyproject.toml` and is a *proposal*; `v2/naming_gate.py` fails if the tag exists | An Owner decision |

**Pre-existing, carried from `RELEASE_CHECKLIST.md` (internal, not shipped in
this preview):**

| # | Blocker | State |
|---|---|---|
| B4 | Rotate the DeepSeek credential exposed on 2026-10-03 | Fingerprint `8d573fa5` is in `constraintrelay.provider_guard.REVOKED_FINGERPRINTS`, so **no runner can use it** — refused before a request is formed. The rotation itself is the Owner's to do |
| B5 | 2–3 real non-scripted sessions | Blocked by B4. **Not a qualification gate** — it records an ordinary limitation |
| B6 | A "why this exists" page for someone who has not read the paper | Not written. This document is not it |

### What the B1 gate covers, and what it does not

I described this gate as covering *"any spelling"* in a report. That was wider
than the code, so the coverage is now a table the suite drives — **24
must-catch and 14 must-pass through the real scanner** — rather than a
sentence.

**Caught:** `pip` / `pip3` / `pip3.13` / path-qualified pip; `python -m pip`
at any interpreter path; an option before the verb (`pip -q install`);
`uv pip`, `uv add`, `pipx`, `poetry add`, `pdm add`, `conda`, `mamba`;
leading `sudo`, `$`, `!`, `RUN`; extras, version pins and case variation;
and a command split over a `\` continuation.

**Not seen, and stated rather than implied:** a requirements or constraints
file that names the distribution; a shell variable or heredoc holding the
name; a VCS, URL or `--index-url` install; an instruction written in prose
instead of in a fence; and any file that is not tracked markdown at the
top of the source tree.

**Ruled on 2026-10-25 and implemented** (detail in
[`ROADMAP.md`](ROADMAP.md)): `host_system_message_unchanged` is now
three-valued and `null` never passes; `constraintrelay.attach` is now **`constraintrelay.attach`**,
with no alias.

### What the release directory contains, and what it does not

Built and audited by `v2/release_package.py`. A **whitelist** decides what is
copied — nothing reaches the directory that a pattern did not ask for — and
the exclusions are *proved* rather than asserted: each category's pattern must
still match tracked files in the source tree, so an exclusion that has stopped
matching anything fails the gate instead of passing it.

| Not published | Files held back | Why |
|---|---|---|
| internal field tests | 378 | the measured corpora, their runners, their recorded evidence |
| sealed sets | 167 | frozen artifacts, the seal over them, the replay corpora |
| the acceptance harness | 64 | the suites import the sealed set and the field-test tree |
| work logs | 24 | phase reports, the status log, closure records, the internal checklist |
| recorded host profiles | 4 | prose write-ups of specific third-party checkouts |
| protocols and review queues | 2 | review material and in-flight handoffs |
| ledgers | 1 | the defect ledger |

**Three suites are held back with their reasons**, rather than quietly
absent — and two of them were in the public set until the audit ran them
under a scratch `HOME` and they went red:
`tests/test_container_phase1.py` (231 gates; imports `fieldtest/phase16` and
reads two sealed artifacts), `tests/test_container_no_egress.py` (18 of 20
gates are self-contained, but the last two *run* the suite above), and
`tests/test_v06_contract.py` (its §8 check compares against a hermes-agent
checkout on the machine running it). **Four suites ship and pass** on
published material with no checkout on this machine reachable.

**Scanned, in the directory and inside both built artifacts:** 11 credential
shapes, 2 credential-fingerprint shapes, 6 machine-local path shapes. The scan
found four real leaks in **shipped package data** and they are fixed at source,
not in the copy: `cf/data/hermes_scanner.json` and
`cf/data/loader_fingerprints.json` carried this machine's home directory, and
`loader_fingerprints.json` also carried a Homebrew prefix. A fifth finding was
`NOTICE` keeping the old product name on its first line — it is **shipped
inside the wheel** under `dist-info/licenses/`, and the naming gate had missed
it because its document set was `*.md`.

**Third-party content:** nothing is vendored. Five upstreams are *ported* —
re-implementations, which are derived work — and each is named in `NOTICE`
with its licence: Vibe-Trading, openclaw and hermes-agent (MIT), automaton
(MIT), and openai/codex (Apache-2.0). Only codex imposes an ongoing §4(d)
obligation, and its NOTICE attribution is reproduced verbatim rather than
absorbed into this project's licence. The register is a table the gate reads,
so a published file cannot be third-party-derived without an entry.

### The import name stays `cf` for this preview — decided

**Ruling 2026-10-26:** the import name is **`cf`** for this preview and the
public entry point is **`constraintrelay.attach`**. The product name is **ConstraintRelay** and
is no longer an open question; the distribution and CLI are `constraintrelay`.

So the internal spellings below keep their current form, and
`v2/naming_gate.py` asserts each is still spelled the old way — "I renamed the
import name too" fails the gate:

`import constraintrelay` · `import constraintrelay` · `constraintrelay.version.IMPLEMENTATION`
(`constraintflow/0.7+analyzer.2`, recorded inside every receipt) ·
`.constraintrelay/` (the state directory holding `receipts.jsonl`) ·
`.constraintrelay.toml` · `constraintrelay-declarations`,
`constraintrelay-constraints` and `constraintrelay-active-constraints`
(markers that travel
**inside the model request** and are compared byte-for-byte) ·
`_constraintrelay_guard` · `constraintrelay_commit` (a receipt field).

Each is either evidence already written down, state already on disk, or bytes
a receipt compares. The distribution `constraintrelay` therefore still ships a
module called `constraintflow` alongside `cf` — **by decision, for this
preview**, because renaming it would invalidate receipts already written and
state already on disk. What a developer writes is `constraintrelay.attach`.

**Deliberately out of the preview:** fleet view (a heartbeat would change what
the product is), the Anthropic adapter, and a sealed extraction holdout.

---

## What this list does not establish

* No competitor product was installed or run; every external capability claim
  is from published documentation read on the date given.
* The extraction route's **precision ≈ 80% / recall ≈ 31%** figures are from a
  **development** set, read while choosing fixes. **Absence from a proposal
  list is not evidence that a rule does not exist**, and no number here is
  offered as a held-out estimate.
* Nothing here is evidence about model behaviour.
