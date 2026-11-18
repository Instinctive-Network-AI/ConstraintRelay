# ConstraintRelay — roadmap, and the things deliberately not being built

This file holds **deferred work and the reasoning that deferred it**. It is
not a plan with dates. Anything here is out of the developer preview; what is
*in* it is [`DEVELOPER_PREVIEW.md`](DEVELOPER_PREVIEW.md).

Three kinds of entry, kept apart because they are different states:

* **DEFERRED** — a decision has been taken not to build it now, with the
  reason and the shape it would take if it is built later.
* **AWAITING RULING** — I have a proposal, the Owner has not ruled, and
  nothing has been implemented.
* **REOPENING CONDITION** — the thing is excluded from the supported range,
  and this is the evidence that would be required to widen the range.

---

## 0. RULED — the product's names, and the preview version

**Owner ruling 2026-10-25.** The product is **ConstraintRelay**; the distribution
and the CLI are **`constraintrelay`**; the preview version is **`0.1.0a1`** and
the proposed tag `v0.1.0a1` is a proposal — nothing is tagged and nothing is
published to any index.

The names live in `cf/naming.py` and `v2/naming_gate.py` binds them to
`pyproject.toml`, so a future rename is one edit and the gate says whether
the packaging followed.

**The import name is a separate decision and is deliberately unchanged.** The
deferred list, with what each name is, is in `v2/naming_gate.py`'s
`DEFERRED_NAMES`, and the gate asserts each is *still spelled the old way* —
so sweeping one along fails rather than being discovered later. The open
question the Owner still has to settle: the distribution `constraintrelay`
ships a single top-level package, `constraintrelay` — the `cf` and
`constraintflow` modules were merged into it on 2026-11-02.

**The historical record is not rewritten.** The phase reports, the defect
ledger and the status log keep what they said, and the gate requires that they
still contain the old name — "I swept the history too" fails there.

## 1. DEFERRED — fleet view

Owner ruling, 2026-10-24. **Not being built.**

`COMPETITIVE.md` §3 records Cerbos Hub's deployment state — which node serves
which bundle version, and when each instance was last seen — as the
architecture to learn from, and it still is. **The mechanism is refused.**
Cerbos' answer needs a central service collecting heartbeats from every
connected PDP. ConstraintRelay is a zero-dependency library that makes no
network call of any kind: nothing in its path imports `urllib`, `http`,
`socket`, `requests` or `ssl`, and it runs air-gapped. Adding a heartbeat
would change what the product *is*, not add a feature to it.

**If it is ever built, the shape is prescribed:** aggregate locally from the
receipts that already exist. Every receipt already records the library
`source_version` and `library_content_id` behind the request it describes, so
*"which requests actually received which library version"* is derivable from
`.constraintrelay/receipts.jsonl` with no new telemetry, no agent reporting in,
and no service to run. That answers a **narrower** question than Cerbos' —
it is about requests that were made, not nodes that are alive — and the
narrower question is the one this project can answer from evidence rather than
from a liveness signal.

Not scheduled. Not designed. Recorded so that the next person who wants a
fleet view does not reach for a heartbeat.

## 2. DEFERRED — the Anthropic adapter

Owner ruling, 2026-10-24. **Not being built before the developer preview
ships.**

`anthropic` payloads are shaped differently: a top-level `system` parameter
rather than a `system` message in the list, and a different tool-call
representation. The receipt's non-elevation properties are written against
"the host's own system message, as a member of the message list", and three of
the four would have to be redefined rather than ported. The current behaviour
is a **refusal with its reason at install time**, which is correct and stays.

Preview effort goes to the release list instead: verified hosts and clients,
known limitations, install and quickstart, competitive comparison.

## 3. RULED AND IMPLEMENTED — `host_system_count`

**Owner ruling 2026-10-25: approved, and the three states must be kept
apart.** Implemented at `cf/container_receipt.py`.

`non_elevation.host_system_message_unchanged` is now three-valued:

| value | meaning | established by |
|---|---|---|
| `true` | **established unchanged** | a digest pinned before injection that the payload's first system message still hashes to, **or** the adapter stating zero before and the payload carrying zero now |
| `false` | **established changed** | a pinned digest that no longer matches — including one with no system message left to hash — **or** a stated count that no longer matches the payload |
| `null` | **not established** | nothing pinned and nothing contradicting: neither "unchanged" nor "changed" was shown |

The field previously reported `false` for both of the last two, so a reader
could not tell a detected modification from an absence of evidence. A
`host_system_message_state` field carries the same answer as a word, so a
reader does not have to know whether their deserialiser distinguishes `null`
from `false`.

**`null` never passes.** The ruling requires that when the state is not
established, nothing depending on "the host's system message is unchanged" may
pass, and `non_elevation.verified` therefore requires the field to be `true`
**by identity**. `bool(None)` is already false, so truthiness would give the
right answer today — and that is the shape this project has lost to every time
it was allowed to stand. An identity test admits one value. Seven non-`true`
sentinels, six of them truthy, are driven through the real receipt in
`tests/test_container_phase1.py`; a truthiness gate admits all six.

**Other delivery facts stand on their own evidence.** The ruling withholds the
conclusions that depend on the host's system message, not the ones that do
not: the capsule's bytes, the envelope and the tool-call correspondence are
still recorded as observed on a `null` request.

A change established by the **stated count alone** reports `false` rather than
`null`, with the reason in the receipt's notes. Declining to say so would hide
a modification the code can actually see.

**Zero effect on either verified host**, as predicted: `openai-python` reports
the count, and `hermes-agent` always pins a real system message. All 18 frozen
artifacts were regenerated and resealed; the two new fields are the only
change to the committed receipts.

**One defect found while implementing it.** `False == 0` is `True` in Python,
so an adapter handing `system_absent_by_construction` a flag where a count
belongs took the "no host system message" path and its request **passed**.
That was pre-existing, and the bool-is-not-a-count attack written for the
ruling is what caught it.

## 4. REOPENING CONDITION — sequential multi-agent processes on `hermes-agent`

**Declared unsupported**, per `CONTAINER_QUICKSTART.md` → *Support
consequence*. The guard withdraws its `SOLE_AGENT` elimination whenever an
agent pre-existed observation, which refused cell `T2-B` of the 2026-10-22
smoke before it spent anything; the same cell delivered when run alone in its
own process.

**What retained the previous cell's agent is still unexplained.** Ruled out at
zero cost (`v2/HERMES_SMOKE.md`): the gate wiring including bound-method
factories, the class-level construction patch (uninstalled in a `finally`),
owner binding (a weakref, deliberately), lingering threads, and a complete
recorded conversation — 0 live agents after one `gc.collect()`. The untested
difference is a **real provider client plus the host's dispatch of a real
tool**.

To widen the range, in order:

1. a zero-cost probe that constructs the hermes agent against a real provider
   client object and dispatches a real tool against a **local HTTP stub** —
   the shape `v2/integration_langgraph.py` already uses — and reports live
   agent count after `gc.collect()`;
2. if that reproduces the retention, the cause, and a fix with a mutation that
   rebuilds the pre-fix behaviour;
3. if it does not reproduce, the range stays as it is. **No mitigation is
   added against a cause nobody has found** — a speculative fix here would be
   a guard written against the attack I thought of.

## 5. RULED — the public function is `constraintrelay.attach`

**Owner ruling 2026-10-25.** Not `protect`, not `observe`, and not renamed
again. Implemented, with **no alias**: an alias called `protect` would keep in
the public API the word the ruling removes.

The documentation states what attaching does, because the name alone does not:
`constraintrelay.attach(client, host=…, library=…, source=…)` wires one client into **both
mechanisms**. On every call through the patched method the activated
declarations are *injected* into the outgoing payload — a capsule in a
non-elevating role, after the host's own messages — and the payload that
actually left is handed to `constraintrelay.container_receipt.verify`, which issues the
**receipt**: which named declarations were in the request, at which version,
with which confirmation state, and what the observation could not reach.

It does not stop, filter or rewrite anything the model does. **Delivery is not
compliance**, and nothing in this project has measured whether a model follows
a declaration.

## 6. Already recorded elsewhere, not repeated here

| Deferred thing | Where it lives |
|---|---|
| `responses.create`, CrewAI, AutoGen, LlamaIndex, OpenAI Agents SDK | `README.md` → *Still not measured*; refused with reasons at install |
| a sealed extraction holdout before any further extractor change is scored | `PHASE7.md` §8, `RELEASE_CHECKLIST.md` |
| semantic extraction as a reproducible path | `PHASE15.md` — three frozen backends failed determinism; confirmation-only, permanently |
| publication channel and repository extraction | `DEVELOPER_PREVIEW.md` → *Blocking* |
| the 2–3 real non-scripted sessions | `DEVELOPER_PREVIEW.md` → B5. Not run, and **not** blocked by the credential rotation, which closed 2026-10-09 |
