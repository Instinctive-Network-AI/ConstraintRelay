# ConstraintRelay 1.0 — product scope

**Thesis.** The neighbouring products answer adjacent questions — which
policy bundle a node serves (Cerbos Hub), which prompt version is in effect
(LaunchDarkly AgentControl), may this tool call happen (Bedrock AgentCore
Policy), was this answer acceptable (the guardrail field). ConstraintRelay
answers the one left over: **did the author-confirmed text reach THIS request,
and can I prove it afterwards** — with a receipt that names its own
observation boundary.

The first version of this line said "everyone else guards the output". That
was too broad and is withdrawn: LaunchDarkly is squarely on the input side.
What survives is smaller and checkable — of the products verified, none issues
a per-request delivery receipt. [`COMPETITIVE.md`](COMPETITIVE.md) records
what was read, when, and what is still an unchecked lead.

**Early customer.** An ordinary AI agent developer using the OpenAI or
Anthropic SDK, directly or through LangChain / LangGraph / CrewAI / AutoGen /
the OpenAI Agents SDK. They must get value in **under five minutes** without
reading `LIMITATIONS.md`.

---

## The one change that unlocks adoption

Our verified hosts are `vibe-trading` and `hermes-agent` — two checkouts our
customers have never heard of. But the adapter layer is host-agnostic: an
adapter wraps one call site, names its boundary, and hands a message list to
`prepare()`.

Every mainstream framework ultimately calls one of three provider methods:

```
client.chat.completions.create      # OpenAI-shaped, incl. every OpenAI-compatible provider
client.responses.create             # OpenAI Responses
client.messages.create              # Anthropic
```

Wrapping the **provider SDK** gives one boundary that sits downstream of every
framework's message assembly and upstream of serialisation — the same class of
boundary both verified hosts already use. One adapter, whole ecosystem.

---

## Scope

### S1 — Provider-SDK host (`cf/host_openai.py`) **[1.0 blocking]**

Wrap the bound method on the **client instance**, not the class: reversible,
and it cannot affect another client in the same process. Covers
`chat.completions.create` first; `responses.create` and Anthropic
`messages.create` are separate boundaries with separate payload shapes and are
listed separately in `SUPPORTED_HOSTS` or not at all.

**Non-negotiable invariants, inherited unchanged:**

- The developer supplies the active skills. Nothing is inferred from the
  conversation. `active()` is that explicit act.
- Versions resolve **from the bytes** via the library binding, never from a
  hand-written label — reuse `resolve_from_host_record`.
- Role stays non-elevating (`user` / `tool`). The capsule quotes an author; it
  does not become a host instruction.
- Failure never blocks. Any ConstraintRelay error degrades to pass-through plus
  a ledger row, and the developer's call completes.
- A request with no tools offered is **not** a decision-input boundary; its
  receipt degrades honestly rather than claiming more.

### S2 — The five-minute path (`constraintflow` top-level package) **[1.0 blocking]**

Target: parity with Guardrails AI's 7-line quickstart.

```python
import constraintflow as cf

guard = cf.attach(client, host="openai-python",
                  library="lib.json", source="./skills")

with guard.active("my-skill"):
    client.chat.completions.create(model=..., messages=[...], tools=[...])

guard.last_receipt.ok          # True
```

`import cf` stays the internal implementation and keeps working. `cf.attach`
still **requires** `host=` to be named — a recorded registry entry supplies
role and position, so nothing is defaulted silently, and the receipt records
which registry entry it came from.

### S3 — Receipts you can look at **[1.0 blocking]**

Receipts append to `.constraintflow/receipts.jsonl`; `constraintrelay receipts`
reads them back: per request, verified / failed / no-declaration-due /
attribution-unverified, reusing `cf.request_ledger`'s four states. Counted,
never scored.

### S4 — Day-0 value **[1.0]** — done

**Measured before building anything:** a repository with five plainly-worded
rules and no markers produced `"entries": 0` at exit code 0, with the only
signal in a JSON field nobody reads.

The rule that caused it is correct and stays — the container keeps
**author-marked** candidates only, because an unmarked line is the
extractor's guess. What was missing was anything that helped the developer
*become* the author.

`container propose` runs **the same `extract` the build runs**, so what it
offers is exactly what a build will accept once marked. `--write` inserts the
marker the author would have written; `git diff` is the review and the commit
is the act of authorship. Without `--write` nothing on disk changes, and
`--write` builds nothing, confirms nothing and adopts no manifest — those
stay separate acts.

The command prints its own recall limit: precision ~80%, recall ~31% on the
development set's random stratum, thresholds in table rows found about one
time in five, and **absence from the list is not evidence a rule does not
exist**. The acceptance suite asserts the proposal list is a strict SUBSET of
the rules present, because a day-0 command that implied completeness would be
worse than one that found nothing.

### S5 — Documentation split **[1.0 blocking]**

`README.md` leads with the five-minute path. The rigor moves behind links. The
caveats stay **true and reachable** — they do not stay on the front page.

---

## Explicitly out of scope for 1.0

- Semantic extraction in the request path. It failed determinism across three
  frozen backends (`PHASE15.md`) and stays confirmation-only.
- Blocking, refusing, scoring, or judging. No false-refusal rate is being
  acquired.
- A hosted service, dashboard, or account. Zero runtime dependencies and
  air-gapped operation are a differentiator, not a limitation.
- Delegation / subagent attribution. Still unsupported, and that is a
  deliberate refusal rather than a gap.
- Any claim about wire bytes. Both boundaries remain the in-process payload.

---

## Status

As of 2026-10-24.

**Two different facts, kept apart in this table.** "Implemented + gated" is
what I can establish on my own: the code exists and the suites are green.
"Closed" means an independent review returned ✅ on it. For most of this
project's history only the first was ever true, and the word "done" was
doing both jobs — S4's row once said **done** while the completion report
for the same commit said I was not claiming it closed. Two statements about
one fact, disagreeing, committed together.

So a row says **closed** only where a review round closed it, and names the
round. Everything else says **implemented + gated**, which is honest about
what stands behind it.

| | | | |
|---|---|---|---|
| S1 | provider-SDK host | **implemented + gated**, sync and async | `cf/host_openai.py`; 75/75 + 19/19 mutations, 73/73 against the real `openai` package, zero provider calls |
| S2 | five-minute path | **implemented + gated** | `constraintflow/__init__.py`; run on the installed wheel by `v2/install_verify.py` |
| S3 | receipt reader | **implemented + gated** | `cf/cli_receipts.py`; four states, counted not scored, gaps included |
| S4 | day-0 value | **CLOSED** by review 2026-10-24 (✅ 10/10) | `cf/propose.py` + `container propose`; an unmarked repo goes from `entries: 0` to a library `container check` passes, through one reviewable edit. 24/24 + 5/5 mutations; the README's own commands are executed on the source tree **and on the installed wheel**. It took five review rounds: the first four found the day-0 code sound and the checking around it unsound — a substituted command, a false-green exit code, a red artifact committed, a coverage list that missed the product package, and a count I reported over the wrong set |
| S5 | docs split | **implemented + gated** | README leads with the path; every caveat kept and reachable |

### What S1–S3 do not yet cover

* **Only `chat.completions`.** `responses.create` and Anthropic are refused
  with their reasons, which means a developer on either is not served at all
  yet. Anthropic is the larger gap and needs its own non-elevation treatment
  for a `system=` keyword.
* **One framework is now measured, the rest are not.** `langgraph` +
  `langchain-openai` is driven end to end at 15/15 against a local HTTP stub
  (`v2/integration_langgraph.py`), with the capsule read out of the bytes the
  server parsed and an uninstalled control. CrewAI, AutoGen, LlamaIndex and
  the OpenAI Agents SDK remain an argument from where they call.
* **Core change, flagged for ratification:** a payload with no system message
  now satisfies non-elevation *vacuously* when the adapter reports zero
  before injection — without it, the ordinary LangGraph shape could never
  earn a verified receipt. Silence still degrades, and a reported count of 1
  with zero present still FAILS, so a deleted system message cannot use this
  path. Three controls and one mutation.
* **Threads do not inherit an activation.** Documented and pinned by a test,
  but a developer who activates on the main thread and calls from a worker
  gets `NO_DECLARATION_DUE` rather than a delivery.
