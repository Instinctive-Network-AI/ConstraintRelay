# ConstraintRelay container — ten-minute quickstart

**0.2 developer preview of a general interface.** Verified hosts and verified
boundaries are listed individually below; nothing here claims to support every
agent.

## Verified hosts

Two, listed separately because they were verified against different
boundaries and to different depths. Neither is a claim about any other agent.

### Host 1 — `vibe-trading`

| | |
|---|---|
| **Host** | `vibe-trading` @ **`dddf96e`** (pinned checkout) |
| **Driven against** | a **real model request** — `deepseek-v4-pro`, **2026-10-18** |
| **Verification boundary** | the `messages` list handed to `ChatLLM.stream_chat` (`agent/src/providers/chat.py:249`, called from `agent/src/agent/loop.py:732`). **Not** the bytes on the wire: serialisation happens downstream, inside the host's provider client, and nothing here observes it |
| **How loads are observed** | from the **host's own load record**, read by `constraintrelay.container_hostrecord` — items carrying `name` and `dir_path` (`src/agent/skills.Skill`). A skill the host never loaded cannot be declared for. **That record carries no version**: none of the host's bundled `SKILL.md` files has a `version:` key, so the version resolves **from the bytes** via the library's content-hash binding, never from a hand-written label |
| **Run configuration** | the runner sent `max_tokens: 1200` and **`thinking: {"type": "disabled"}`** via `extra_body`. **Both were set by the smoke runner, not by the host** — the host never sends a `thinking` request field. Recorded in `v2/out/SMOKE_RESULT.json` as `plan_deviation: "thinking:disabled"`, and those cells may not be used for behavioural observation |

### Real declaration connected

**One skill**: `backtest-sanity-check` @ `own-2026-10-18`, **author-confirmed**
(project lead, `written_reply`). The receipt reports
`fixture_attestations_accepted: false`, so this delivery does not stand on a
labelled fixture.

**Measured token cost:** in that run's A/B comparison over **two tasks**,
injection added **513 input tokens** in each — `T1` 6,987 → 7,500 and `T2`
6,960 → 7,473. **That is the observed figure for those two payloads and this
one capsule; it is not a per-request rate and does not generalise.** The
capsule's size depends on how many declarations a skill carries and how long
they are.

### Host 2 — `hermes-agent`

| | |
|---|---|
| **Host** | `hermes-agent` **0.14.0** @ **`2517917de`** (separate pinned checkout) |
| **Driven against** | a **recorded reply**, 2026-10-20/21 — **zero provider calls** in the acceptance suite. Nothing there measures model behaviour; it measures what the model would have received. Plus a **real-model smoke on 2026-10-22** (`deepseek-v4-flash`) in which a capsule-bearing request did reach the provider; that smoke is **still incomplete** and its receipt confirmation was not obtained — see *Real-model exposure* below |
| **Verification boundary** | the `messages` list inside `api_kwargs` as handed to `AIAgent._interruptible_streaming_api_call` / `_interruptible_api_call`, the two methods the main loop calls at `agent/conversation_loop.py:1172` and `:1176`. Downstream of `_build_api_kwargs`, the non-ASCII sanitiser and the codex preflight; upstream of serialisation and the network. **Not** the bytes on the wire |
| **How loads are observed** | at `tools.skills_tool.skill_view` — the one function the `skill_view` tool, the slash commands and the CLI preload all go through, so one observation point covers all three. A skill that only appears in hermes' available-skills index is **not** declared for. This host passes a skill's `metadata` through verbatim, so a version there is genuinely in the record (`host_declared_label`); its documented top-level `version:` field is **not**, so the ordinary case still resolves from the bytes |
| **api_mode** | `chat_completions` **only**. `anthropic_messages`, `codex_responses` and `bedrock_converse` are refused at install — their payloads are shaped differently |
| **Not covered** | the other **49** `.create(` call sites in the package, including the iteration-limit summary at `agent/chat_completion_helpers.py:1277` and `:1320`, the compression and auxiliary-client paths, subagents, and the ACP/gateway surfaces. No receipt is produced for a request the adapter cannot see, and no absence is claimed for them |
| **Decision boundary** | **the earlier gap is closed.** A *finished* historical tool call is permitted at the boundary — correspondence established by id and order, parallel calls included — so second-and-later loop iterations verify. An **unanswered** call, or a correspondence that cannot be established, still refuses: the arguments are already fixed, so that request is downstream of the decision it was meant to inform |
| **Load attribution** | the load boundary is a **module** attribute, so a second agent in the same process loads through it too. Every load is decided against the agent the adapter was installed on; one that cannot be attributed **contributes no declaration**. The receipt's `load_attribution_established` is **three-valued** — `true`, `false` (at least one load could not be attributed), or `null`. `null` covers **two** cases, told apart by `loads_considered`: the host supplied no attribution evidence (field absent) or supplied it having considered **zero** loads (`0`). Rows are keyed per load, and if they do not account for every load the host reported, `established` is forced to `null` rather than summarising a partial set. The host keeps running in every case |
| **Delegation** | **not supported**, and that is a deliberate refusal rather than a gap in the docs. A delegated child builds its own `AIAgent`, which the adapter never wraps: it delivers nothing, no receipt describes it, and its loads are withheld from the parent rather than attributed to it. Subagent coverage is **UNVERIFIED — not working**. **The 2026-10-22 attribution keying fix does not replace this**: keying rows per load stopped one load's row from being overwritten by another's, which is a *reporting* defect. Whether a foreign load may contribute a declaration is decided by the guard's elimination rules, and those are unchanged — a load the guard cannot attribute to the installed agent still contributes nothing |
| **Verified scope** | **one agent per process, no delegation.** A process that constructs agents **sequentially** is outside it: the guard withdraws its `SOLE_AGENT` elimination whenever any agent pre-existed observation, so a later agent in the same process can be refused. Observed 2026-10-22 — cell `T2-B` was refused before any call, spending nothing; re-run **alone in its own process** the same cell delivered and its receipt verified. That shows the isolated mode works; it does **not** establish the sequential refusal was a false one, and **what retained the previous agent is still unexplained** |
| **Support consequence of that** | **sequential multi-agent processes are DECLARED UNSUPPORTED on this host, not listed as a known limitation inside the supported range.** The distinction is the Owner's ruling of 2026-10-24 and it is the whole point: a blocker *inside* the supported range cannot be closed by writing it down. So the range is drawn to exclude it. One agent per process is what is supported; the refusal is **fail-closed and correct under the uncertainty** — the guard cannot attribute a load to the agent being delivered into, so it contributes nothing and the host keeps running. What a developer sees is a receipt with `load_attribution_established: null` or a refused delivery, never a silent pass. What they do: **one agent per process.** The reopening condition is in [`ROADMAP.md`](ROADMAP.md) — the cause must be established before the range widens, and no mitigation has been added against a cause nobody has found |

Hermes is the first host to exercise `host_declared_label` — see
*How loads are observed* in the table above, which is the single statement of
this host's load mechanism.

**The declaration package was reused byte for byte** — the same
`backtest-sanity-check` @ `own-2026-10-18` and the same
`task_library.json` (`lib-112774058333d443`) as host 1. Nothing was rebuilt,
relabelled or re-confirmed. Full detail: `v2/HERMES_HOST.md`.

*No non-Python host has been driven through any of this.*

Collect declarations from skills, deliver them unchanged into a model request,
and get a receipt for what was actually in that request. No semantic judgement,
no approval, no blocking.

## Coverage window — what a receipt is a claim about

Four limits, because each has been mistaken for something stronger:

* **Per request, not per session.** A receipt describes the one payload it was
  issued against. It says nothing about the request before it or after it.
* **Skills load on demand**, so a decision taken *before* a skill was loaded is
  not covered by that skill's declarations — and no receipt says otherwise.
  Each request's coverage is the load state as it stood at that request.
* **One request the host makes is outside the boundary, and it carries no
  capsule.** When hermes exhausts its iteration budget it sends a second
  decision request of its own — the iteration-limit summary — through a path
  the adapter does not wrap. Measured on the real 2026-10-22 run: the
  capsule was present in the main-loop request and **absent** in that
  summary request. No receipt describes it and no absence is claimed for it,
  but a reader should not assume every request in a turn is covered.
* **Delivery is not compliance.** Verified means the declared bytes were in the
  payload at the named boundary. It is not evidence the model read them,
  understood them, or followed them, and **nothing in this project has
  measured whether a model obeys a declaration.**

## Real-model exposure, stated plainly

| | |
|---|---|
| Host 1 `vibe-trading` | **one real request**, `deepseek-v4-pro`, 2026-10-18. The runner — not the host — set `max_tokens: 1200` and `thinking: disabled`, so those cells are unusable for behavioural observation |
| Host 2 `hermes-agent` | **complete: 2 tasks × 2 arms against a real provider** (2026-10-22, `deepseek-v4-flash`, one cumulative ledger). Both arm-B cells carried the capsule in the **main-loop** request and **not** in the host's out-of-boundary summary (`capsule_in_request: [1, 0]`), and **both receipts verified at run time** at the named boundary. Measured input-token cost of this capsule: **+1,246** on T1 and **+1,244** on T2 — this capsule on these payloads, not a rate. Reconciled by `v2/smoke_matrix.py`; corrections to the earlier evidence are kept **beside** it in `HERMES_SMOKE_CORRECTIONS.json` rather than edited into it. Detail: `v2/HERMES_SMOKE.md` |

| `constraintrelay.attach` — **the path this document recommends** | **one real request**, 2026-10-29, `deepseek-v4-flash`, on the same cumulative ledger. Until then this was the one path verified *everywhere except* against a provider: `v2/install_verify.py` runs the README's five-minute section on the built wheel against a **local HTTP stub**, the langgraph evidence reads the capsule out of a **local stub server's** parsed body, and both host rows above go through *adapters* rather than through `constraintrelay.attach`. Now: a real `openai.OpenAI` client, capsule at message index 1 of 2, `receipt.ok` true, provider response id `fcef3db8-…`, and the receipt's payload digest equal to the digest on the ledger row that paid for it. Detail: `v2/out/CLOSEOUT_ATTACH.json` |

**No wire bytes have been observed, on any of the three rows.** Every boundary
here is the in-process message list; serialisation happens downstream, inside
the provider client, and nothing here reads it.

**None of these rows says anything about whether a model obeyed a
declaration.** Delivery is not compliance, one request cannot be separated
from a stochastic model, and nothing in this project measures obedience.

---

## Install

```bash
python3 -m pip install .                   # or: pip install <wheel>
constraintrelay container --help
```

> **Not from an index.** This line said `pip install constraintflow` until
> 2026-10-25, and that command installs an **unrelated** published project of
> the same name — *"A DSL and tools for neural network certification"*, 0.1.1,
> MIT, uploaded 2025-09-12
> ([pypi.org](https://pypi.org/project/constraintflow/), read 2026-10-25).
> Nothing here is published to any index. Naming decision:
> [`DEVELOPER_PREVIEW.md`](DEVELOPER_PREVIEW.md) → *Blocking*, **B1**.

Zero runtime dependencies — the whole container runs on the standard library.
Nothing in its path imports `urllib`, `http`, `socket`, `requests` or `ssl`.

Verified as an install rather than as a source tree: `v2/install_verify.py`
builds a wheel from a clean clone, installs it into a fresh venv, and runs the
CLI, the shortest example below and **both** hosts' integration examples with
the repository's own `cf/` moved aside, so a source import cannot pass for an
install (**42/42**, `v2/out/INSTALL_VERIFY.json`) — all green as of 2026-11-07, re-measured twice. It read 41/42 on 2026-11-06 and this sentence named the wrong cause for it; see `DEVELOPER_PREVIEW.md` §2, where the change is recorded as measured and NOT explained. Since 2026-10-23 that includes every command the README's five-minute section prints, run against the installed console script from a **clean-clone copy of the document** — three of them were broken for eight review rounds because every checker ran its own retyped version instead. The 1.0 product surface is covered there too: `import constraintrelay` from the wheel, the README's five-minute path run end to end on it, and `constraintrelay receipts` reading back the sink that run wrote — counting the delivery **and** the gap. `import constraintrelay` passing says nothing about `import constraintrelay`; they are two entries in `packages` and a wheel can carry one without the other. Since 2026-10-26 the reverse is checked too, and for the same reason: every check here reached the surface through `constraintflow`, so `constraintrelay.attach` — the entry point the ruling names — could raise `AttributeError` from the wheel with all of them green. `constraintrelay.attach` and `constraintrelay.attach` are now asserted to be the **same object**, which a second definition of the surface would not be.

It also runs `constraintrelay profiles` and `constraintrelay fingerprint` from
the installed wheel, because those are the commands that read shipped data and
`import constraintrelay` does not. The pre-release review found that the data lived in a
**sibling** directory of `cf/` and a wheel therefore carried none of it:
`profiles` raised `FileNotFoundError`, and `fingerprint` exited **0** while
reporting every profile `UNRECORDED` — a wrong answer, not an error, because a
missing file and an empty record both read as `{}`. The data now lives in
`cf/data/` inside the package, a missing shipped file raises, and the check
compares the installed package's fingerprint count against the source tree's.

## 1. Mark declarations in your skill

```markdown
<!-- constraintrelay: critical id=DS-R1 -->
The report must state its data cut-off date.
```

The id is the identity, never the text. Same id with different text means the
declaration was *modified*; a new id means one was *added*.

## 2. Build a library

```bash
constraintrelay container build \
  --source demo --skill skills/demo-skill \
  --source-version demo-v1 --out lib.json
```

Offline and deterministic: `provider_calls: 0`, `cost_usd: 0.0`, and rebuilding
the same bytes reproduces the same `library_content_id`.

## 3. Write a manifest draft, then adopt it yourself

```bash
constraintrelay container attest \
  --source demo --skill skills/demo-skill --library lib.json \
  --confirmed-by "Your Name"
```

This writes a **draft**. While `confirmation_method` is empty the container
reports `PENDING_CONFIRMATION` and delivers nothing. To adopt, decide whether
the text is your declared position, then fill in:

```json
"confirmation_method": "written_reply",
"evidence_ref": "where that decision can be found"
```

Filling those fields **records** your decision. It is not evidence the decision
happened: no signature is checked and no reference is resolved. `check` reports
`identity_verified: false` and `legal_effect_assessed: false` for that reason.

For demo material use `--fixture`, which is permanently
`real_author_confirmation: false` and is **refused by default** at delivery.

## 4. Check consistency

```bash
constraintrelay container check --source demo --skill skills/demo-skill --library lib.json
```

Checks format, the version label, the per-declaration seals, and **re-hashes
every bound source file**. Editing a skill after the build fails here.

## 5. Preview, deliver, verify

```bash
constraintrelay container preview --source demo --skill skills/demo-skill \
  --source-version demo-v1 --library lib.json --json > preview.json
```

`--json` gives the capsule text plus the assembly identity, so a host that is
not written in Python can insert the capsule itself and then:

```bash
constraintrelay container verify --source demo --skill skills/demo-skill \
  --source-version demo-v1 --library lib.json \
  --request captured.json \
  --observation-point "where you read the payload" \
  --observable-boundary "what that reading cannot reach"
```

`--request` is a JSON file holding either a message list or an object with
`messages`, `tools` and `host_system_sha256`. Both observation arguments are
**required**: a receipt that does not say what it could see is not evidence.
*No non-Python host has been driven through this path yet.*

**`tools` and `host_system_sha256` are required for a *verified* receipt, not
optional.** A payload that offers no tools is not a decision input boundary,
and without the pre-injection digest of your own system message non-elevation
cannot be checked — either way every delivery claim degrades to
`UNOBSERVABLE`, `container verify` exits non-zero, and the receipt's notes
name which one was missing. This page called `tools` optional until a real
install verification ran the documented shape and got `{"FAILED": 1}`; the
control for that sentence is a check in `v2/install_verify.py` that asserts
the same request **without** `tools` still fails.

---

## Shortest Python integration

```python
import hashlib, json
from constraintrelay.container_api import (load_library, load_manifests, ContainerRequest,
                              SkillRequest, assemble, render, verify)
from constraintrelay.container_adapter import AdapterConfig, prepare, assert_non_interference

# The host decides role and position. There are NO defaults — see below.
cfg = AdapterConfig(role="user", position="append", host="my-agent")

lib  = load_library("lib.json")
mans = load_manifests("demo", ["skills/demo-skill"])
req  = ContainerRequest(request_id="ex-1",
                        skills=(SkillRequest("skills/demo-skill", "demo-v1"),))
asm  = assemble(lib, req, source_root="demo", manifests=mans)

host_system = "You are a demo assistant."
original = [{"role": "system", "content": host_system},
            {"role": "user",   "content": "Write the report."}]
tools = [{"type": "function", "function": {"name": "write_report"}}]

res = prepare(original, asm, cfg)          # returns a NEW list
assert assert_non_interference(original, res["messages"],
                               tools_before=tools, tools_after=tools)["ok"]

# ... your transport sends res["messages"] ...

rec = verify(res["messages"], asm, lib=lib, manifests=mans,
             observation_point="my-agent, just before transport.send",
             observable_boundary="the message list only; no network was used",
             host_system_sha256=hashlib.sha256(host_system.encode()).hexdigest(),
             tools_offered=tools)
print(rec.ok, rec.as_dict()["claims"])
```

`constraintrelay.container_api.__all__` is what 0.2 promises. Other symbols in
`constraintrelay.container*` are internal and may move.

---

## The adapter contract

An adapter is responsible for exactly three things:

1. **The host supplies the skill ids and versions.** Nothing is inferred from
   a conversation. If your host keeps a record of what it loaded, read them
   out of it rather than writing a list — see below.
2. **The capsule goes in before model generation**, at the position the host
   configured — not after a tool call has been chosen, which is downstream of
   the decision it was meant to inform.
3. **Verification happens at the host's actual observable final request
   boundary**, producing the one receipt format. An adapter that cannot name
   its boundary cannot produce a receipt.

### Injection position has no default, deliberately

`AdapterConfig` requires `role` and `position` and **refuses to be constructed
without them**. Every default here would be wrong somewhere: a default role
would pick an authority level for someone else's host — the difference between
`user` and `system` is the difference between quoting an author and issuing an
instruction — and a default position would decide whether the capsule lands
before or after the host's own system prompt. Both are the host's call about
its own request. The cost is one explicit line per host; the alternative is a
quiet default nobody chose.

Permitted roles: `user`, `tool`. `system` and `developer` are refused at both
ends — when writing the capsule and when reading it back.
Permitted positions: `append`. Anything else is refused rather than
half-supported.

### Reading the skills and versions out of your own load record

`constraintrelay.container_hostrecord.resolve_from_host_record(record, libraries,
source_roots=…, host=…)` takes your loader's record — anything with a
`skills` sequence whose items carry a `name` and a `dir_path` — and resolves
each loaded skill to **one** built version, or refuses it with a named
reason. A hand-written list is a claim about what your host loaded, written
by whoever wrote the demo; this makes the request a consequence of the load.

Two version sources and no third: a version **label in the record**, which
must also match the bytes on disk, or — when the record has no version field,
as Vibe-Trading's does not — the **one library binding whose file hashes match
the bytes the host loaded**. Zero matches, two matches, a binding with no file
hashes, a missing bound file, a skill the record gives no directory for, a
skill under no declared root, a skill no library holds, a declared label no
library holds, a declared label whose binding does not match the disk, a host
holding text that has since changed on disk, and a record with no text to
compare are **11 separate refusals** — the whole of `REFUSALS`, which is what
that number is gated against rather than counted by hand. Nothing is guessed,
and two labels over identical bytes are refused rather than picked.

Pass `body_parser=` your own frontmatter parser if you have one: the fallback
is a second implementation of a transformation your host already owns, and two
implementations of one rule drift.

Worked example over a real host, reaching **6 of the 11** refusals by name:
`v2/host_record_vibe.py`. It said *"all seven"* for a round — wrong in the
count and wrong in the word *all*, since a worked example over one real tree
cannot reach the refusals that tree does not produce. Both numbers here are
gated against the module and that file.

### Non-interference

`prepare()` returns a **new list**. Your messages are never mutated, nothing is
deleted, order is preserved, and exactly one capsule is added.
`assert_non_interference()` checks all of that plus tool-call pairing and tool
definitions, and is what the acceptance run uses instead of a promise.

---

## Categories, and what each does not mean

| category | confirmed by | delivered by default |
|---|---|---|
| `author_confirmed` | the author, by a locatable method | **yes** |
| `machine_candidate` | nobody | no — host must enable |
| `runtime_candidate` | nobody | no — host must enable |
| `regulatory_source` | nobody | no — host must enable |

A machine candidate is a *reading* of a source, not the author's word. A
runtime candidate is **not a user authorisation**. A regulatory source is **not
a finding of applicability**. Each note is printed in the capsule.

Enabling a category records that the **host** asked for unconfirmed material —
that is a different act by a different party and is **not** author
confirmation. The receipt records which categories were enabled, when, and the
host-supplied identity; an identity the host did not supply is recorded `null`,
never inferred.

`runtime_candidate` and `regulatory_source` have a reserved input path
(`CarriedItem`) and **no collector**. Nothing in 0.2 derives them.

## Three outcomes, per (skill, category)

| outcome | means |
|---|---|
| `AVAILABLE_COMPLETE` | every item entered the request, whole |
| `FAILED` | with a reason, and **nothing** of it was sent |
| `UNDECLARED` | no manifest for this skill — **not** "it has no constraints" |

Failure reasons: `VERSION_MISMATCH`, `UNREADABLE`, `CAPACITY_EXCEEDED`,
`UNOBSERVABLE`, `NOT_AUTHOR_CONFIRMED`, `PENDING_CONFIRMATION`.

## The receipt keeps four claims apart

```json
"claims": {
  "assembly_succeeded": true,
  "verified_at_named_send_boundary": true,
  "load_attribution_established": null,
  "received_by_remote_model": null
}
```

Four different facts: a capsule was assembled; it was verified in the payload
at a named boundary; the loads behind it belong to the agent being delivered
into; a model received it. Reporting one number would let the weakest travel
on the strongest's authority.

Both nulls are **null, not false**. Nothing in the receipt observes a remote
model, and `false` would assert it did *not* receive the capsule — also not
observed. `load_attribution_established: null` means the host supplied no
attribution evidence, so the receipt does not establish that the loads behind
the request are this agent's: **null is not false and it is certainly not
true.** An explicit `false` — some load could not be attributed — makes the
receipt not-ok, and those loads contribute nothing.

Delivery is not understanding and not compliance.

## The container does not pause or block

A delivery failure is **reported**; the host decides what to do. There is no
verdict, allow/deny or block type in the delivery modules.
`cf/phase1_pause.py` is a development-time guard against accidental paid
provider calls and has no reach into delivery — gates assert the delivery
modules do not reference it and that the assembled body is byte-identical with
it active or lifted.

## Legacy route

The v0.1 commands (`init`, `active`, `import`, `confirm`, …) still work and are
tagged `[legacy]` in `--help`. Running one prints a one-line note on stderr
pointing at its container equivalent — **only when stderr is a terminal.**

That condition is a compatibility guarantee, not a cosmetic choice: a script
that merges stdout and stderr and parses the result as JSON would be broken by
a diagnostic landing in its data, which is exactly what happened to two of
this repository's own test suites. On a pipe, a file, a captured buffer or in
CI, both streams stay byte-identical to 0.1.

0.2 does not migrate the v0.1 commands.
