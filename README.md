# ConstraintRelay

**Prove the rules you wrote reached the model.**

Your guardrail blocked the call the model chose to make. **Was the rule you
wrote about that even in the request?**

ConstraintRelay answers that, per request, with a receipt that names its own
observation boundary: your declaration was in **this** payload, here is where
that was observed, and here is what the observation could not reach.

**A receipt evidences delivery and nothing past it.** It does not establish
that the model read the declaration, and it does not establish that a model
which received it would have chosen differently — **nothing in this project
has measured either**. Delivery is the single fact it settles. The reason to
want that fact is that today you cannot settle even that.

The neighbouring tools answer adjacent questions — which policy bundle a node
is serving (Cerbos Hub), which prompt version is in effect (LaunchDarkly), may
this tool call happen (Bedrock AgentCore Policy), was this answer acceptable
(the guardrail products), and what did we actually send (LangSmith, Langfuse
and the rest of the tracing lane — **a trace does hold your prompt**, and
[`COMPETITIVE.md` §2](COMPETITIVE.md) sets a receipt against one difference by
difference, including the three places a trace is the better instrument). They
are complementary, and of the ones checked none issues a per-request delivery
receipt scoped to named declarations at a confirmed version.
[`COMPETITIVE.md`](COMPETITIVE.md) names what was verified, on what date, and
what is still an unchecked lead.

---

## Five minutes

```bash
pip install .                   # from a checkout of this repository
                                # zero runtime dependencies, Python 3.11+
```

> **Not from an index — because nothing here has been published to one.**
> `pip install constraintrelay` installs nothing: the distribution is
> unregistered on PyPI (404, read 2026-11-01), and that is a statement about
> this project not having published, not about the name being unavailable.
>
> Do not reach for the old spelling either. `constraintflow` on PyPI is **an
> unrelated project** — *"A DSL and tools for neural network
> certification"*, version 0.1.1, MIT, uploaded 2025-09-12
> ([pypi.org/project/constraintflow](https://pypi.org/project/constraintflow/),
> read 2026-10-25) — so `pip install constraintflow` installs **that**, not
> this. The module you `import` is still spelled `cf`; the names are settled
> and the reasons the import name was left alone are in
> [`DEVELOPER_PREVIEW.md`](DEVELOPER_PREVIEW.md).

Every command below runs from your repository root and assumes this layout —
`--source` is the **root**, and a skill id is its path **under** that root:

```
my-agent/                      ← run everything from here
└── skills/
    └── my-skill/
        └── SKILL.md
```

Mark a declaration in your own skill document — an HTML comment, no new
language to learn:

```markdown
<!-- constraintflow: critical id=DS-R1 -->
The report must state its data cut-off date.
```

Already have a repository of skills? Let it propose the markers and review
the diff — **that review is the act of authorship**, which is all the marker
ever meant:

```bash
constraintrelay container propose --source . --skill skills/my-skill          # shows the edit
constraintrelay container propose --source . --skill skills/my-skill --write  # makes it
```

Then `git diff` — that is your review. It prints its own limits while it
works: extraction is heuristic, about two real constraints in three are never
proposed, and **absence from that list is not evidence a rule does not
exist**.

Only `critical` is an author marker. `<!-- constraintflow: operative … -->`
is **not** one — `operative` is a *tier* from the legacy route below — and a
marker the build does not recognise is counted as
`candidates_skipped_not_author_marked` rather than built.

Build a library, then adopt the manifest draft `attest` writes:

```bash
constraintrelay container build  --source . --skill skills/my-skill --source-version v1 --out lib.json
constraintrelay container attest --source . --skill skills/my-skill --library lib.json --confirmed-by "Your Name"
```

**`attest` writes a DRAFT, and a draft delivers nothing.** Adopting it is a
third step, and it is yours: open
`skills/my-skill/.constraintflow-attestation.json` and fill in

```json
"confirmation_method": "written_reply",
"evidence_ref": "where that decision can be found"
```

That is the act of confirmation. Until `confirmation_method` is set, the skill
reports `PENDING_CONFIRMATION` and nothing is delivered. `constraintrelay
container check --source . --skill skills/my-skill --library lib.json` should
then say `CONFIRMATION_RECORDED`. Detail: step 3 of the [container
quickstart](CONTAINER_QUICKSTART.md).

Now install the guard on your client:

```python
import cf

guard = cf.attach(client, host="openai-python",
                  library="lib.json", source=".")

with guard.active("skills/my-skill"):
    client.chat.completions.create(model=…, messages=[…], tools=[…])

guard.last_receipt.ok          # was it actually in the payload?
```

Two things about that last line, both of which a first run will hit:

* **`last_receipt` is `None` when no receipt was produced** — an unresolvable
  skill, or nothing due. `guard.last_receipt.ok` then raises `AttributeError`
  in *your* code. Write `r = guard.last_receipt; r is not None and r.ok`, and
  read the reason off `constraintrelay receipts`, which records the gap as a row;
* **`tools=` is load-bearing.** A request offering no tools is not a
  decision-input boundary, so delivery cannot be verified against it: the
  capsule still travels, `ok` is `False`, and the row says the delivery was
  not *confirmed* while recording `capsule_present: true`. That is the product
  refusing to verify at a boundary it cannot verify at, not a failed delivery.

  **Where to read that field**, because the two surfaces spell it
  differently: on the **row**, `constraintrelay receipts --json` — which carries
  the ledger row whole — it is `capsule_present`, and it is what separates
  *the capsule was there and could not be verified here* (`true`, delivery
  not confirmed) from *the capsule never went* (`false`). On the **`Receipt`
  object** `guard.last_receipt` hands you, the same fact is
  `capsule_message_index >= 0`, which is exactly how the row's value is
  computed.

And read the receipts back:

```bash
constraintrelay receipts
```

```
.constraintflow/receipts.jsonl  —  3 request(s) recorded, host: openai-python

  #1    delivered     demo-skill
  #2    none due      —
  #3    MISSING       other-skill
        1 of 1 due declaration set(s) are not in this payload: other-skill

  UNATTRIBUTED: 0  MISSING: 1  delivered: 1  none due: 1
```

That is the whole loop. The five-minute path above is
run on the installed wheel on every verification pass (`v2/install_verify.py`,
an internal file not shipped in this preview),
so a README whose opening snippet does not work is a failing check rather than
a surprise.

### What has been measured, and what follows from it

**Measured.** `cf.attach` wraps one method on one client object:
`client.chat.completions.create`. Sync and async clients, the capsule in the
payload the method received, the receipt, the four ledger states, and the same
against the real `openai` package in a clean venv. The patch is on **that
instance** — never the class, so it cannot reach a client some other library
built — and `guard.uninstall()` reverses it.

**Measured against a real provider, once** (2026-10-29). Everything above was
verified against a local HTTP stub, which is a complete test of this code and
no test at all of a provider. So the path on this page was driven once for
real: a live `openai.OpenAI` client, the capsule at message index 1 of 2 in
the payload the patched method passed on, `receipt.ok` true at that boundary,
and a provider response id recorded beside the ledger row that paid for the
call — the receipt's payload digest and that row's digest are the same value,
so the confirmation is about the request that was billed rather than an
adjacent one. `v2/out/CLOSEOUT_ATTACH.json`. It establishes that the wrapper
works end to end; it says **nothing** about the bytes on the wire, which are
downstream of this boundary inside the SDK, and nothing about whether the
model obeyed.

**Measured for one framework.** A real `langgraph` graph over a real
`langchain_openai.ChatOpenAI`, installed from PyPI, guarded at
`llm.root_client` — the client LangChain built for itself — with nothing in
LangChain or LangGraph modified or configured to cooperate. Requests go to a
**local HTTP stub**, so nothing is billed, and the capsule is found **in the
JSON body that server parsed**: downstream of the SDK's own serialisation.
With the guard uninstalled the same graph sends no capsule, which is the
control. **15/15 LangGraph host checks**, against pinned versions.

**Still not measured.** CrewAI, AutoGen, LlamaIndex and the OpenAI Agents SDK.
The reasoning for those is the same — they all reach a provider SDK method —
but reasoning is not a result, and one framework working does not establish
the next one does.

Two conditions it depends on, both yours to check:

* the framework must use **the client object you installed the guard on** — many build
  their own internally, and a client ConstraintRelay never saw produces no
  receipt and claims no absence;
* it must go through `chat.completions`. `responses.create` and Anthropic are
  **refused with their reasons** rather than approximated.

If a request you expected is missing from `constraintrelay receipts`, that is
the first thing to check — and the absence of a row is exactly the signal,
which is why gaps are recorded rather than dropped.

### Four properties worth knowing before you adopt it

| | |
|---|---|
| **It never blocks** | Any ConstraintRelay failure degrades to your original request, untouched, plus a ledger row naming the reason. No false-refusal rate, no outage in your agent |
| **It never guesses** | You name the active skills. Versions resolve from the **bytes** of your skill files, never from a label. A surface nobody has read is refused *with the reason*, not approximated |
| **It costs nothing to run** | Zero runtime dependencies, zero provider calls, deterministic. Nothing in its path imports `urllib`, `http`, `socket`, `requests` or `ssl`. Runs air-gapped |
| **It counts, it does not score** | Four states per request: delivered, nothing due, due-but-missing, attribution-unverified. No 0–100 safety number — that would be a judgement this project has not earned |

---

## The limit, stated before you need it

**Delivery is not compliance.** A verified receipt means the declared bytes
were in the payload at the named boundary. It is **not** evidence that the
model read them, understood them, or obeyed them, and **nothing in this
project has measured whether a model follows a declaration.**

**No remote provider's wire bytes have been observed.** Every receipt's
boundary is the in-process message list; serialisation happens downstream
inside the provider client. The LangGraph run above is the one place bytes
*were* read after serialisation — and that was a **local stub** this project
ran, which says nothing about what a remote provider receives.

Three more coverage limits — per-request not per-session, skills that load
after a decision, and one request hermes makes outside the boundary — are in
[`CONTAINER_QUICKSTART.md` § Coverage window](CONTAINER_QUICKSTART.md).

---

## Verified hosts

| Host | Driven against | Depth |
|---|---|---|
| `openai-python` | a recorded transport, sync **and async**, and separately the real `openai` package in a scratch venv | **87/87 provider-SDK host checks** and **22/22 provider-SDK host mutations caught**; the scratch-venv run is **90/90 with the real `openai` package**, recorded separately in `v2/out/OPENAI_HOST_REAL_SDK.json`. **Zero provider calls** in both |
| `langgraph` + `langchain-openai` | a real graph against a **local HTTP stub** | **15/15 LangGraph host checks**, with the capsule found in the bytes the server parsed and an uninstalled control that sends none. Versions pinned in `v2/out/LANGGRAPH_HOST.json` |
| `hermes-agent` 0.14.0 | a recorded reply, plus a **real-model** smoke on 2026-10-22 | 2 tasks × 2 arms complete, both receipts verified at run time |
| `vibe-trading` @ `dddf96e` | one **real model request**, 2026-10-18 | one skill, author-confirmed |

Each host's exact boundary, what it does **not** cover, and how loads are
observed: [`CONTAINER_QUICKSTART.md`](CONTAINER_QUICKSTART.md).
*No non-Python host has been driven through any of this.*

---

## Where the rest lives

| | |
|---|---|
| [`DEVELOPER_PREVIEW.md`](DEVELOPER_PREVIEW.md) | **the release list** — pinned version, verified client paths, an example receipt, what is still blocking |
| [`CONTAINER_QUICKSTART.md`](CONTAINER_QUICKSTART.md) | the full container interface, every host boundary, the adapter contract |
| [`COMPETITIVE.md`](COMPETITIVE.md) | what the field does, how a receipt differs from a trace, what to refuse |
| [`ROADMAP.md`](ROADMAP.md) | what is deferred and why — fleet view, the Anthropic adapter, open rulings |
| [`PRODUCT_1_0.md`](PRODUCT_1_0.md) | the 1.0 scope and what is deliberately out of it |
| [`LIMITATIONS.md`](LIMITATIONS.md) | every measured limit, with the number behind it |
| `STATUS.md` | the canonical status table — **internal, not shipped in this preview** |

---

## The v0.1 extraction route, kept working

Before the container, ConstraintRelay *extracted* candidate constraints from
skill documents. Those commands still work and are marked `[legacy]` in
`--help`. They carry a measurement that has to travel with them:

> On a hand-built gold set of 439 constraints over 14 skills, on the randomly
> drawn stratum, **precision ≈ 80% and recall ≈ 31%**. One proposed candidate
> in five is not an agent constraint, and about two real constraints in three
> are never proposed. Those figures come from a **development** set — read and
> used to choose fixes — so they are a diagnostic, not a held-out estimate.

That is why `import` requires confirmation and why **absence from the store is
not evidence that a rule does not exist**. Detail: `PHASE7.md` (internal,
not shipped in this preview) and
[`LIMITATIONS.md` §1](LIMITATIONS.md).

The semantic extraction path is **optional, experimental and not
reproducible**: three independently frozen backend configurations failed to
produce deterministic output on the same prompt and corpus. It is assisted
candidate discovery requiring human confirmation, never an extractor you can
pin. Evidence: `fieldtest/phase15/determinism_evidence/`.

```bash
constraintrelay init    ./my-skills-repo
constraintrelay import  ./my-skills-repo --confirm-all --tier operative
constraintrelay skills  ./my-skills-repo
constraintrelay active  ./my-skills-repo --skill my-skill --show
constraintrelay diff    ./my-skills-repo
```

Every command takes `--json`. Exit codes: `0` nothing needs a decision, `1`
tool or configuration error, `2` a reported finding.

---

## Licence

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

ConstraintRelay vendors no third-party source. It contains Python *ports* of
loader algorithms from the harnesses it was measured against, and `NOTICE`
attributes each one. All five upstream licences are verified as of 2026-10-04,
each read from the upstream repository at the exact commit the port derives
from, with the blob sha recorded so the reading is reproducible:
**HKUDS/Vibe-Trading** (MIT), **openclaw/openclaw** (MIT),
**NousResearch/hermes-agent** (MIT), **Conway-Research/automaton** (MIT) and
**openai/codex** (**Apache-2.0** — the one upstream placing a continuing
obligation on this distribution). See [`LIMITATIONS.md` §8](LIMITATIONS.md).
