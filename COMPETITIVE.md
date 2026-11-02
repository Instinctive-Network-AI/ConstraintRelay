# Competitive position — where ConstraintRelay sits, and what it must become

**Read dates: 2026-10-23, 2026-10-24 and 2026-10-25.** Every external claim
carries the source it came from, and each row says whether I verified it or
took it from a list. A comparison table that quietly fills gaps is the same
defect class as a store presented as complete.

---

## 0. A correction to the first version of this document

The first version opened with *"everyone else guards the model's output"* and
rested on a nine-product guardrail survey. **That sentence was too broad and
is withdrawn.**

It was true of the products I had looked at and false of the field. The Owner
supplied a second list on 2026-10-24 naming categories I had not scanned, and
checking three of them myself shows at least one product squarely on the
**input** side: LaunchDarkly delivers prompts and instructions into the
request at runtime. The lane ConstraintRelay occupies is narrower than I
claimed, and the honest version is below.

What survives is not the breadth of the claim but its substance: of the
products checked, **none issues a per-request receipt that names its own
observation boundary**. That is a smaller statement and a checkable one.

---

## 1. Five adjacent lanes, and which one is ours

| Lane | Question it answers | Who does it | Verified? |
|---|---|---|---|
| **Policy distribution** | which policy *bundle version* is each node serving right now? | **Cerbos Hub** | ✅ read 2026-10-24 |
| **Runtime instruction delivery** | which *prompt version* is in effect for this user/segment? | **LaunchDarkly AI Configs / AgentControl** | ✅ read 2026-10-24 |
| **Action authorization** | may this *tool call* happen? | **Bedrock AgentCore Policy**, Permit.io | ✅ AgentCore read; Permit.io **not verified** |
| **Output validation** | was this *answer* acceptable? | Guardrails AI, NeMo, Lakera, Bedrock Guardrails, … | ✅ survey read 2026-10-23 |
| **Request observability** | what did we actually send, and what came back? | **LangSmith**, **Langfuse**, Arize Phoenix, Braintrust, W&B Weave; **OpenTelemetry GenAI semconv** | ✅ LangSmith + Langfuse + semconv read 2026-10-25; the other three **not verified** |
| **Delivery evidence** | did the author-confirmed text reach **this** request, and can I prove it afterwards? | *this is the gap* | — |

The five lanes are **complementary, not competing**. A buyer could run all of
them. That matters more than it sounds, and §5 says why it is also the
commercial danger.

The observability lane is the one that produces an immediate objection rather
than a comparison — *"my trace already shows the prompt"* — so it gets its own
section, §2, and the objection is **correct** as stated.

### Cerbos Hub — the architecture to learn from

Verified ([docs](https://docs.cerbos.dev/cerbos-hub/index.html),
[audit logs](https://www.cerbos.dev/blog/cerbos-hub-audit-logs-live-in-beta)):
compiles policies into **signed bundles**, pushes them to every connected PDP,
shows **which policies each PDP is serving, the exact bundle version, and when
the instance was last seen**, and streams decision logs that can be traced to
the policy version that produced a given decision.

That is a mature answer to a question ConstraintRelay answers badly. Our
library has a reproducible `library_content_id`, and nothing else: no fleet
view, no "which agent is running which library version right now", no
deployment state. **Operationally we are years behind, and that is the gap to
close by borrowing rather than inventing.**

What we must NOT copy is the decision engine. Cerbos decides
*allow/deny for a principal on a resource*. ConstraintRelay decides nothing.

### LaunchDarkly AgentControl — the nearest neighbour, and the reason §0 exists

Verified ([AI Configs GA](https://launchdarkly.com/blog/ai-configs-ga-runtime-control-prompts-models/),
[AgentControl](https://launchdarkly.com/platform/agent-control/)): prompts and
model choices become flags, changed at runtime without redeploying; every
change is *"versioned, auditable, and access-controlled"*; instant rollback;
progressive rollout with automatic regression pause; and an SDK whose
`openai_messages` handler **applies the model and messages LaunchDarkly
selected**.

So it is in the delivery lane, not the output lane. The distinction is what
it does *afterwards*. Fetched and read specifically for per-request
attestation:

> The page **does not mention** per-request receipts, delivery verification,
> attestation, or proof that specific prompt versions reached particular
> requests. … it does not explicitly document mechanisms to **verify which
> exact prompt/instruction version was actually sent to a specific model
> request**.

What it offers instead is *"full traces across every agent invocation"*,
continuous evals, and *"when metrics shift, you'll know which config change
caused it."* **Attribution over aggregates, not proof per request.**

The honest summary: LaunchDarkly owns *which instruction version is in
effect*. ConstraintRelay's claim is the next sentence — *and here is the
evidence it was in **this** payload, at a boundary this receipt names.*

### Bedrock AgentCore Policy — the opposite end of the same turn

Verified ([AWS](https://aws.amazon.com/blogs/machine-learning/govern-ai-agent-tool-access-with-amazon-bedrock-agentcore-gateway/)):
GA 2026-03-03. Cedar policies at the Gateway, *"deterministic — identical
requests always produce identical decisions, with the decision logic living
entirely outside the agent's code and outside the model."* Guardrails
integration shipped 2026-07.

It acts **after** the model has decided and **before** the tool runs.
ConstraintRelay acts **before** the model decides. Its own pitch — that no
prompt injection can bypass a gateway check — concedes the input side is
unreliable; ConstraintRelay attacks that from the other direction, by making
what entered the input *checkable*.

### Not verified in this pass

Taken from the Owner's list and **not** independently checked: **Permit.io**
(MCP gateway, delegation, low-intrusion integration) and **ServiceNow AI
Passport** (cross-agent verifiable authorization, credentials, revocation).
Both are named here as leads, not as findings. The ServiceNow one is the more
important to check next: *verifiable authorization passed between agents* is
the closest description to delivery evidence in the whole list, and if it
carries per-hop proof it is the first real overlap.

---

## 2. Against a trace — the objection, and only the differences that hold

**The objection is right.** A trace does contain the prompt. LangSmith's own
description of a trace is *"Every run, with full inputs and outputs"*
([observability concepts](https://docs.langchain.com/langsmith/observability-concepts),
read 2026-10-25). Anyone who says ConstraintRelay is needed because you
otherwise cannot see what you sent is wrong, and that argument is not made
anywhere in this project.

Two facts about the lane, both read 2026-10-25, because they bound the
objection rather than defeating it:

* **Content capture is opt-in at the standard level.** The OpenTelemetry GenAI
  semantic conventions
  ([gen-ai-spans.md](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)):
  model instructions, user messages and outputs *"are considered sensitive and
  are often large in size … OpenTelemetry instrumentations **SHOULD NOT
  capture them by default**, but SHOULD provide an option for users to opt
  in."* Pattern 1, the listed default, is *"Don't record instructions, inputs,
  or outputs."* The same page warns the content *"may be larger than
  observability backend limits for telemetry envelopes or attribute values"* —
  so a span can hold a **truncated** prompt. Neither is a defect; both mean
  "the prompt is in my trace" is a statement about a configuration, not about
  the stack.
* **Self-hosting is normal here.** Langfuse is open source and self-hostable
  on your own infrastructure via Docker Compose or Kubernetes
  ([self-hosting](https://langfuse.com/self-hosting)). So *"we are local and
  they are cloud"* is **false and is not claimed**. What differs is
  operational shape — Langfuse self-hosted runs Web + Worker + Postgres +
  Redis against a receipts file written by a zero-dependency library — and
  that is a deployment preference, not a privacy argument.

### The four differences, each with its own verdict

| Difference | Holds? | What is actually true |
|---|---|---|
| **A receipt is scoped to named declarations, a version and a confirmation state** | **Yes** | A receipt names the declaration ids it was answering for, the library's `source_version` and `library_content_id`, and per skill whether the material is `author_confirmed` or `unconfirmed` — with the confirming name and method, re-derived and required verbatim in the payload. A trace has no declaration set to be scoped to. The data is not missing from a trace; there is **nothing in it bound to an authored, versioned, confirmed rule set**, so "which of my rules were due on this request" is not a question it is shaped to answer |
| **Verbatim verification, not as-recorded capture** | **Partly** | A trace stores what was sent. A receipt re-derives the expected text from library + manifest and requires it **byte-for-byte** in the payload, reporting present/absent per id. But the honest half: **nothing stops anyone writing that comparison over trace data.** This is a difference in what ships, not a capability only this design can have. The part that is not merely unshipped is the independent source of truth — the library and the author's manifest — which a trace backend does not have and would have to be given |
| **The decision point is classified, not just recorded** | **Partly** | A receipt refuses to verify a payload that is not the decision input boundary: tools must be offered and every tool call in the history finished — answered by id, after the call that declared it, parallel calls included. Traces hold the raw material (OTel defines an `execute_tool` span; LangSmith records tool runs with their nesting), so the correspondence is **derivable** from a trace. What no trace product checked here emits is a per-request verdict that *this* payload is the one the model decided from, and a refusal when it cannot be established |
| **Explicit failure states and a stated coverage window** | **Yes, with a symmetric blindness** | Four states per request — delivered, nothing due, due-but-missing, attribution-unverified — plus a named observation point and a statement of what that observation could not reach. A trace's failure mode is a span that is not there, and a missing span is indistinguishable from a request that never happened. **The same shape of blindness applies here**: a request through a client ConstraintRelay never saw produces no row either. The difference is that the receipt ledger records its own gaps as rows (`UNATTRIBUTED`, `MISSING`) rather than as silence |

### Where a trace is better, and this has to be said

* **Observation depth.** A proxy- or server-side trace can record the actual
  HTTP body. ConstraintRelay's boundary is the **in-process message list**, and
  **no remote provider's wire bytes have ever been observed by this project**
  (`LIMITATIONS.md`, `README.md`). On *how close to the wire the reading is*,
  a trace can beat a receipt outright.
* **Coverage.** Traces cover `responses.create`, Anthropic, streaming,
  embeddings, retrieval and tool spans. ConstraintRelay covers
  `chat.completions` on four hosts and **refuses the rest with their reasons**.
* **Behaviour.** A trace plus an eval measures the output. A receipt measures
  nothing about behaviour. Delivery is not compliance, and on the question
  *"did this rule change what the agent did"* the trace-and-eval stack is
  closer to an answer than a receipt will ever be.

### One sentence, and the pitch that follows from it

A trace answers *what did we send?* A receipt answers *of the declarations
this author confirmed at this version, which were due on this request, which
are verbatim present, and what could this observation not reach?*

So the pitch is not "replace your tracing". It is: **keep the trace, and point
a receipt at the handful of rules you would have to prove something about.**
A buyer who already has LangSmith has the prompt; what they do not have is a
per-request, per-declaration, author-confirmation-scoped answer, and they
would have to build it.

---

## 3. What to absorb

| From | What | Why it beats what we have |
|---|---|---|
| **Cerbos Hub** | signed bundles; **deployment state** — which node serves which version, last seen | We have a content id and no fleet view at all. "Which of my agents is running which library?" has no answer today |
| **LaunchDarkly** | approval workflow and **instant rollback** on an instruction change | Our library is rebuilt and re-attested by hand. A wrong declaration cannot be pulled back in one action |
| **Guardrails AI** | `pip install` → `Guard().use(…)` → done, 7 lines ([quickstart](https://www.guardrailsai.com/docs/getting_started/quickstart), read 2026-10-23) | Closed as of 1.0: `constraintrelay.attach(client, …)` + `active()` is three lines |
| **Permit.io** *(unverified)* | low-intrusion integration via a gateway | Our instance-level patch is already low-intrusion; worth checking whether a gateway covers hosts we refuse |

## 4. What to refuse

| Pattern | Why |
|---|---|
| **Becoming another policy engine** | Cerbos and Cedar are mature, and a worse copy helps nobody. ConstraintRelay decides nothing and should keep deciding nothing |
| **A bespoke DSL** | Cedar and Colang are each a language a team must learn. Declarations stay an HTML comment in the author's own markdown |
| **LLM-as-judge in the request path** | Non-deterministic and billed per request. Our own semantic path failed determinism across three frozen backends (`PHASE15`), which is why it is confirmation-only |
| **Blocking / refusing** | Every blocking product owns a false-refusal rate. We deliver and report; the host keeps running in every failure mode |
| **Scoring** | A 0–100 "safety score" is a judgement we have not earned. Receipts are counted, never scored |

---

## 5. The commercial danger is budget, not capability

The lanes do not overlap technically. They overlap **in the sentence a buyer
says**: *"we need constraint governance for our agents."* An enterprise that
hears that and buys AgentCore Policy has spent the budget and ticked the box,
and no amount of lane-clarity gets it back.

So the pitch cannot be "we are different from AgentCore". It has to be the
question AgentCore's own design implies and does not answer:

> Your gateway blocked the call the model chose to make. **Did the rule that
> should have stopped the model choosing it ever reach the model?**

That question is answerable, per request, with a receipt — and of everything
checked here, nothing else answers it.

### The limit that stays in the pitch

**Delivery is not compliance.** A verified receipt means the declared bytes
were in the payload at the named boundary. It is not evidence the model read
them, understood them, or obeyed them, and nothing in this project has
measured obedience. Against this field that limit is an asset: the
neighbouring products claim judgements and are wrong some of the time;
ConstraintRelay claims a fact and can be checked.

---

## 6. What this document does not establish

- No competitor product was installed or run. Every capability claim is from
  published documentation, read on the date given.
- Permit.io and ServiceNow AI Passport are **unverified leads**.
- No pricing, adoption, or market-size claim is made; none was gathered.
- "Nothing else issues a per-request delivery receipt" covers **the products
  checked here**. It is not a claim about every product that exists, and the
  next list the Owner sends may narrow it again — as this one did.
