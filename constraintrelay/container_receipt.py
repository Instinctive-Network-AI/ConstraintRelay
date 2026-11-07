"""Phase 1 — verification. The receipt, read off the payload that actually left.

Deliberately a separate module from `cf/container.py`. A receipt produced by the
thing that did the work is not a receipt, and keeping the two apart is why this
one can re-derive every expectation from the library and the author's manifest
instead of trusting the assembler's own `delivered_texts`.

What counts as delivery here
----------------------------
**Not** a store write. **Not** a successful render. **Not** a log line written
before execution. A declaration is delivered when its exact bytes are present in
the message payload handed to the host's send boundary for the request that
decides the call — and that is what `verify()` searches for, byte-for-byte.

Observation point and observable boundary are mandatory
-------------------------------------------------------
`verify()` refuses to produce a receipt without both. The first says where the
payload was read; the second says, in the receipt itself, what this evidence
cannot reach. For the Vibe harness the boundary is the `messages` list as handed
to `ChatLLM.stream_chat` — downstream of the loop and of all three compaction
layers, and **upstream of serialisation, of the network, and of any remote
model**. No receipt from this module may be read as "the model received it".

Unverifiable is a failure, never a delivery
-------------------------------------------
Owner correction, 2026-10-15. Every path that cannot see what it needs returns
`FAILED` with `UNOBSERVABLE`. There is no "probably arrived" and no silent pass:

* no capsule in any payload;
* the capsule only in a payload whose tool-call history is **unfinished** —
  a call nobody answered means the arguments were already fixed and this
  request is downstream of the decision that fixed them;
* whether the host's own system message is unchanged NOT ESTABLISHED, so
  non-elevation cannot be checked.

One of those paths is the CALLER's omission, and says so (2026-11-06)
---------------------------------------------------------------------
Owner ruling item 3. When `tools_offered` was never passed, or neither
`host_system_sha256` nor `host_system_count` was, the verdicts carry
`RECEIPT_INPUT_MISSING` instead of `UNOBSERVABLE`, `receipt.missing_inputs`
names them, and a note states that **this is not a host incompatibility**.

The two are worth telling apart because they look identical and mean opposite
things: `FAILED` / `UNOBSERVABLE` with an EMPTY violations list and every
declaration found verbatim in the payload is a correct, fail-closed verdict
that reads exactly like *the host changed*. It cost real time during the
2026-11-06 Vibe-Trading v0.1.19 compatibility check.

The call is **not** refused outright, and that was considered: refusing would
close the only route to `host_system_message_unchanged: null`, which the
2026-10-25 ruling requires to be reachable and to never pass. The legitimate
empty forms are also not omissions and are not listed — `tools_offered=[]`
states that the host offered none, and `host_system_count=0` states that the
payload carries no system message by construction.

Three states, not two (2026-10-25)
----------------------------------
Owner ruling. `non_elevation.host_system_message_unchanged` is three-valued:
`true` established unchanged, `false` established **changed**, `null` not
established. It previously reported `false` for both of the last two, so a
reader could not tell a detected modification from an absence of evidence.

`null` never passes — `non_elevation.verified` requires the field to be `true`
**by identity**, so every delivery claim on such a request degrades to
`UNOBSERVABLE`. Delivery facts that rest on their own evidence (the capsule's
bytes, the envelope, the tool-call correspondence) are still recorded as they
were observed; the ruling withholds the conclusions that depend on the host's
system message, not the ones that do not.

A finished tool call in the history is permitted (2026-10-20)
-------------------------------------------------------------
Owner direction. The first rule here refused **any** assistant `tool_call` in
the payload, which was written for Vibe-Trading's single pre-generation site
and could not distinguish a call that is *finished* from one that is *pending*.
A multi-iteration loop carries every earlier iteration's call and its result
into each later request, so a second host — hermes-agent — reported
`UNOBSERVABLE` on every request after its first, with the capsule verifiably
present.

The boundary now requires two things **together**: tools are offered, and
`tool_call_correspondence()` resolves the whole history — every call answered
exactly once, by id, by a result that comes after it, **parallel calls
included**. Anything that function cannot establish — an unreadable id, a
duplicate, an orphan result, an answer preceding its call — leaves the payload
refused. That direction is deliberate: each of those is a state in which "the
arguments were already fixed" cannot be ruled out.

**What this does not widen.** The receipt still covers only the decision *this*
request generates; see `coverage_window` in the receipt. A finished call is
evidence that this payload is pre-generation, never a claim about the request
that produced that call. And nothing here blocks or alters a host request —
`verify()` reads a payload and returns a receipt.

The leak check, which is the half that is easy to forget
--------------------------------------------------------
A skill that `FAILED` or is `UNDECLARED` must have sent **nothing**. So the
receipt recomputes that skill's declaration texts too, and fails the receipt if
any of them appear in the payload. Without it, "all-or-nothing" would be
asserted only on the branch that sends, and a half-send on the failing branch
would pass every check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .container import (AVAILABLE_COMPLETE, CAPSULE_MARKER,              # noqa: F401
                        CAT_AUTHOR_CONFIRMED, CAT_MACHINE_CANDIDATE,
                        CATEGORIES_REQUIRING_EVIDENCE, CATEGORY_NOTES,
                        CLOSE_TAG, CONF_BY_AUTHOR, CONF_UNCONFIRMED, FAILED,
                        NON_ELEVATION_NOTICE, OPEN_TAG,
                        ORDER_IS_NOT_AUTHORITY, RECEIPT_INPUT_MISSING,
                        UNDECLARED, UNOBSERVABLE, ManifestError,
                        envelope_header, sha256_text)

#: Roles a declaration block may travel in. `system` is absent on purpose: the
#: ruling is that a declaration keeps the author's authority and is not raised
#: to a host instruction. Vibe's own loop appends `system` messages mid-run, so
#: the role is available — not using it is the point.
NON_ELEVATING_ROLES = ("user", "tool")
ELEVATED_ROLES = ("system", "developer")


class ReceiptError(Exception):
    """A receipt that must not be issued as requested."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()


def _text(m) -> str:
    return m.get("content") if isinstance(m, dict) and isinstance(
        m.get("content"), str) else ""


def envelope_of(payload: list, skill_id: str,
                category: str = CAT_AUTHOR_CONFIRMED
                ) -> "tuple[int, str] | None":
    """`(message index, envelope text)` for *skill_id*, or None.

    Found by scanning for the opening tag carrying this skill, then to the
    matching close. Parsed out of the payload rather than taken from the
    renderer, so a renderer that emitted a malformed block is caught here.

    An envelope ends at **whichever comes first**: its own closing tag, or the
    next opening tag. Stopping only at the first `CLOSE_TAG` was a real defect
    my own attack suite found: with this skill's close tag deleted, the span ran
    on into the *next* skill's block, picked up that block's close tag, and so
    reported a truncated envelope as terminated — with every declaration still
    inside the over-long span. A boundary that can swallow a sibling is not a
    boundary.
    """
    # Pinned to the category as well: a skill now has one envelope
    # per category, and a skill-only needle would let the confirmed
    # envelope answer for the candidate one.
    needle = (f'{OPEN_TAG} skill="{skill_id}" '
              f'source-version=')
    for i, m in enumerate(payload):
        body = _text(m)
        start, cat_marker = -1, f'category="{category}"'
        probe = 0
        while True:
            hit = body.find(needle, probe)
            if hit < 0:
                break
            nxt_open = body.find(OPEN_TAG, hit + len(needle))
            span_end = nxt_open if nxt_open >= 0 else len(body)
            if cat_marker in body[hit:span_end]:
                start = hit
                break
            probe = hit + len(needle)
        if start < 0:
            continue
        end = body.find(CLOSE_TAG, start)
        nxt = body.find(OPEN_TAG, start + len(needle))
        if end < 0 or (nxt >= 0 and nxt < end):
            # Unterminated, or another envelope opens first. Hand back the span
            # up to that point so the caller sees it is not terminated.
            stop = nxt if (nxt >= 0 and (end < 0 or nxt < end)) else len(body)
            return (i, body[start:stop])
        return (i, body[start:end + len(CLOSE_TAG)])
    return None


def expected_texts(lib, attestations: dict, skill_id: str,
                   category: str = CAT_AUTHOR_CONFIRMED) -> dict:
    """`id -> exact text` this skill WOULD contribute, from the sources.

    Recomputed from the library entry and the author's manifest, never from the
    assembly. This is what makes the receipt independent of the thing it checks,
    and it is also what lets the leak check work on skills that failed.
    """
    att = attestations.get(skill_id)
    entries = {e.constraint_id: e for e in lib.entries
               if e.skill_id == skill_id}
    if isinstance(att, ManifestError):
        # The declared set is unknown, so the leak check falls back to every
        # library candidate for the skill — the conservative superset. An
        # unreadable manifest must not make the leak check vacuous.
        return {cid: e.source_text for cid, e in entries.items()}
    if att is None:
        return {}
    confirmed = {d.declaration_id for d in att.declarations}
    if category == CAT_MACHINE_CANDIDATE:
        # A candidate is exactly an entry the manifest does NOT confirm.
        # Derived from the two sets rather than read off the assembly, so the
        # receipt still answers independently.
        return {cid: e.source_text for cid, e in entries.items()
                if cid not in confirmed}
    out = {}
    for d in att.declarations:
        e = entries.get(d.declaration_id)
        if e is not None:
            out[d.declaration_id] = e.source_text
    return out


#: A failing skill that nevertheless put something in the payload. Named, not
#: folded into `failure_reason`, because the two say different things: the
#: reason is why the skill was not delivered, and this is the fact that the
#: all-or-nothing rule was broken anyway.
PARTIAL_SEND_ON_FAILED_BRANCH = "PARTIAL_SEND_ON_FAILED_BRANCH"


@dataclass
class SkillVerdict:
    """The receipt's own finding for one skill. Overrides the claim."""

    skill_id: str
    claimed: str
    verified: str
    failure_reason: str = ""
    checks: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)
    #: A rule the receipt found broken regardless of whether the outcome
    #: matched. Without this field a leak on the failing branch produced
    #: `verified == claimed == FAILED` and therefore `agrees` — the receipt
    #: recorded the leak in `detail` and still reported itself satisfied. My
    #: own attack suite found that; it is exactly the project's recurring
    #: shape, a check that cannot fail.
    violation: str = ""

    @property
    def agrees(self) -> bool:
        return self.claimed == self.verified and not self.violation

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "claimed_outcome": self.claimed,
                "verified_outcome": self.verified,
                "failure_reason": self.failure_reason,
                "violation": self.violation,
                "receipt_agrees_with_claim": self.agrees,
                "checks": dict(self.checks), "detail": dict(self.detail)}


@dataclass
class Receipt:
    """What was observed, where, and what the observation cannot reach."""

    request_id: str
    observation_point: str
    observable_boundary: str
    verdicts: list = field(default_factory=list)   # list[SkillVerdict]
    payload_sha256: str = ""
    payload_messages: int = 0
    capsule_message_index: int = -1
    capsule_role: str = ""
    at: str = ""
    non_elevation: dict = field(default_factory=dict)
    decision_payload: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    #: Whether the assembly accepted fixture manifests. Carried so a receipt
    #: can never be read as author evidence it does not have.
    fixture_accepted: bool = False
    #: Which non-default categories the host enabled, when, and by whom as far
    #: as that is knowable. Owner direction 2026-10-16; an identity the host
    #: did not supply is recorded null, never invented.
    category_enabling: dict = field(default_factory=dict)
    #: `constraintrelay.load_attribution.summarise()` for the loads behind this request, or
    #: `{}` when the host supplied none. Owner direction 2026-10-21, item 1.
    load_attribution: dict = field(default_factory=dict)
    #: Which of the receipt's required inputs the CALLER did not supply. Owner
    #: ruling 2026-11-06 item 3. Non-empty means the verdicts below read
    #: `RECEIPT_INPUT_MISSING` rather than `UNOBSERVABLE`, and that **this is
    #: not a host incompatibility** — at the top level, so a reader meets it
    #: before reading a single per-skill verdict.
    missing_inputs: list = field(default_factory=list)

    @property
    def attribution_established(self):
        """`True` / `False` / **`None` nothing attributed**. Three, not two.

        `None` is not "clear", and it covers two different situations that a
        reader can tell apart by `loads_considered`: **absent** means the host
        handed this receipt no attribution evidence at all; **0** means it
        handed over evidence that considered no loads. Neither shows that the
        loads behind the request belong to the agent being delivered into, so
        both report null — but the receipt may not say "no evidence was
        supplied" when evidence was, which is what this used to claim.
        """
        if not self.load_attribution:
            return None
        return self.load_attribution.get("established")

    @property
    def ok(self) -> bool:
        """Every claim verified, nothing leaked, attribution not refuted.

        Not an authorisation. Attribution enters here in one direction only:
        an **explicit** `False` — some load behind this request could not be
        attributed — makes the receipt not-ok, because Owner direction
        2026-10-21 forbids reporting such a request verified. `None` leaves
        this property as it was before the field existed, and the receipt
        discloses the null; silently treating null as clear is the shape this
        project has already paid for.
        """
        return (all(v.agrees for v in self.verdicts) and bool(self.verdicts)
                and self.attribution_established is not False)

    def census(self) -> dict:
        out: dict = {}
        for v in self.verdicts:
            out[v.verified] = out.get(v.verified, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id, "at": self.at,
            "observation_point": self.observation_point,
            "observable_boundary": self.observable_boundary,
            "payload_sha256": self.payload_sha256,
            "payload_messages": self.payload_messages,
            "capsule_message_index": self.capsule_message_index,
            "capsule_role": self.capsule_role,
            "non_elevation": dict(self.non_elevation),
            "decision_payload": dict(self.decision_payload),
            "verified_outcome_census": self.census(),
            "all_claims_verified": self.ok,
            "load_attribution": dict(self.load_attribution),
            "_load_attribution_is": (
                "whose loads the declarations in this request came from. A "
                "null `load_attribution_established` means nothing was "
                "attributed — either no evidence was supplied, or the "
                "evidence considered zero loads — and NOT that the loads are "
                "this agent's. False means at least one load could not be "
                "attributed; those loads contributed nothing and this receipt "
                "is not verified."),
            #: FOUR DIFFERENT CLAIMS, never collapsed. 0.2 requires a reader
            #: to be able to tell which one this receipt supports.
            "claims": {
                "assembly_succeeded": bool(self.verdicts),
                "verified_at_named_send_boundary": self.ok,
                "load_attribution_established": self.attribution_established,
                "_load_attribution_established_null_means": (
                    "nothing was attributed, by one of TWO routes: the host "
                    "supplied no attribution evidence at all, or it supplied "
                    "evidence that considered zero loads. Read "
                    "`load_attribution.loads_considered` to tell them apart "
                    "— absent means the first, 0 means the second. Either "
                    "way this receipt does not establish that the loads "
                    "behind this request belong to the agent being delivered "
                    "into. Null is not false and it is certainly not true."),
                "received_by_remote_model": None,
                "_received_by_remote_model_is_null_because": (
                    "nothing in this receipt observes a remote model. The "
                    "field is null rather than false: false would assert the "
                    "model did not receive it, which is also not observed. "
                    "See observable_boundary for what was."),
                "_why_four": (
                    "assembling a capsule, verifying it in the payload at a "
                    "named boundary, the loads behind it belonging to this "
                    "agent, and a model receiving it are four different "
                    "facts. A receipt that reported one number would let the "
                    "weakest travel on the strongest's authority."),
            },
            "fixture_attestations_accepted": self.fixture_accepted,
            "category_enabling": dict(self.category_enabling),
            "_fixture_disclosure": (
                "the assembly behind this receipt ACCEPTED fixture manifests. "
                "Nothing verified here rests on a real author confirmation, "
                "and this receipt may not be quoted as evidence that an author "
                "confirmed anything."
                if self.fixture_accepted else
                "fixture manifests were refused; every verified delivery "
                "carries a real author confirmation"),
            "violations": [{"skill_id": v.skill_id, "violation": v.violation}
                           for v in self.verdicts if v.violation],
            "verdicts": [v.as_dict() for v in self.verdicts],
            "notes": list(self.notes),
            "_what_this_does_not_say": (
                "this receipt observes the message payload at the stated "
                "observation point only. It does not observe serialisation, "
                "network transmission, or receipt by any remote model, and it "
                "does not state that the agent obeyed."),
            #: Owner direction 2026-10-20. Permitting a finished tool call in
            #: the history makes the coverage WINDOW the thing a reader can
            #: get wrong, so the receipt states it rather than leaving it to
            #: be inferred from the boundary rule.
            "coverage_window": {
                "covers": (
                    "the next decision this request generates. The receipt "
                    "verifies that the declarations were present in the "
                    "payload whose output is that decision."),
                "does_not_cover": (
                    "any decision already made. A finished tool call in the "
                    "history is permitted as evidence that THIS payload is "
                    "pre-generation; it is not a claim about the request that "
                    "produced that call. Receipts do not apply backwards."),
                "on_demand_loading": (
                    "skills load on demand, so a decision taken BEFORE a "
                    "skill was loaded is not covered by that skill's "
                    "declarations, and no receipt here says otherwise. Each "
                    "request's coverage is the load state as it stood at that "
                    "request."),
            },
            "_unverifiable_is_failure": (
                "a claim that could not be observed is reported FAILED with "
                "reason UNOBSERVABLE. Absent evidence is never recorded as "
                "delivery."),
        }

    def save(self, path) -> None:
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")


def _tool_calls_of(msg) -> list:
    """The `(id, index)` of every tool call an assistant message declares.

    Dicts and attribute objects both, because a host may hand either in, and
    a call whose id this cannot read is reported rather than skipped — an
    unreadable id is precisely a correspondence that cannot be established.
    """
    raw = msg.get("tool_calls") if isinstance(msg, dict) \
        else getattr(msg, "tool_calls", None)
    out = []
    for i, c in enumerate(raw or []):
        cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
        out.append((str(cid) if cid else "", i))
    return out


def tool_call_correspondence(payload: list) -> dict:
    """Pair every assistant tool call in the history with the result answering it.

    Owner direction 2026-10-20 permits a **completed** historical tool call at
    the decision boundary. Completed is not "a tool message exists somewhere":
    it is a one-to-one pairing by **id**, with the answer **after** the call
    that declared it, and it has to hold for **parallel** calls — one assistant
    message declaring several ids is answered by several tool messages, and
    every one of those ids must be matched.

    Everything this cannot establish is reported in `why_not` and leaves
    `all_resolved` false. The failure direction is deliberate: an unreadable
    id, a duplicate, an orphan result or an answer that precedes its call are
    all states in which "the arguments were already fixed" cannot be ruled
    out, and a receipt that cannot rule it out may not say verified.

    This function **reads**. It never raises on a malformed payload and
    nothing here can block or alter the host's request.
    """
    groups: list = []
    declared: dict = {}            # id -> first declaring message index
    dup_declared: list = []
    unreadable: list = []
    for idx, m in enumerate(payload):
        calls = _tool_calls_of(m)
        if not calls:
            continue
        ids = []
        for cid, pos in calls:
            if not cid:
                unreadable.append({"message_index": idx, "call_index": pos})
                continue
            if cid in declared:
                dup_declared.append(cid)
            else:
                declared[cid] = idx
            ids.append(cid)
        groups.append({"message_index": idx, "declared_ids": ids,
                       "parallel": len(ids) > 1})

    answers: dict = {}             # id -> [message indices]
    for idx, m in enumerate(payload):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "tool":
            continue
        cid = m.get("tool_call_id")
        answers.setdefault(str(cid) if cid else "", []).append(idx)

    unanswered = sorted(i for i in declared if i not in answers)
    orphan = sorted(i for i in answers if i not in declared)
    dup_answered = sorted(i for i, at in answers.items() if len(at) > 1)
    out_of_order = sorted(i for i, at in answers.items()
                          if i in declared and min(at) < declared[i])
    for g in groups:
        g["answered_ids"] = [i for i in g["declared_ids"] if i in answers
                             and i not in orphan]
        g["fully_answered"] = (len(g["answered_ids"])
                               == len(g["declared_ids"]) > 0)

    why_not = []
    if unreadable:
        why_not.append(f"{len(unreadable)} tool call(s) with no readable id")
    if unanswered:
        why_not.append(f"unanswered: {unanswered}")
    if orphan:
        why_not.append(f"tool results answering nothing declared: {orphan}")
    if dup_declared:
        why_not.append(f"id declared more than once: {sorted(set(dup_declared))}")
    if dup_answered:
        why_not.append(f"id answered more than once: {dup_answered}")
    if out_of_order:
        why_not.append(f"answered before it was declared: {out_of_order}")

    return {
        "assistant_tool_call_groups": groups,
        "declared_ids": sorted(declared),
        "answered_ids": sorted(i for i in answers if i in declared),
        "unanswered_ids": unanswered,
        "orphan_result_ids": orphan,
        "ids_declared_twice": sorted(set(dup_declared)),
        "ids_answered_twice": dup_answered,
        "answered_before_declared": out_of_order,
        "calls_with_unreadable_id": unreadable,
        "parallel_groups": sum(1 for g in groups if g["parallel"]),
        "all_resolved": not why_not,
        "why_not": "; ".join(why_not),
        "_what_resolved_means": (
            "every tool call in the history is answered exactly once, by id, "
            "by a tool message that comes after the call declaring it — "
            "parallel calls included. An empty history is resolved: there is "
            "nothing pending. Anything this function could not establish "
            "leaves all_resolved FALSE, because a correspondence that cannot "
            "be shown is not a correspondence."),
    }


def system_absent_by_construction(host_system_count, system_messages,
                                  pinned: bool) -> bool:
    """Is "no host system message" a fact the adapter stated, or an absence?

    A named function rather than an inline expression, so it can be attacked:
    a rule written inline is a rule no mutation can replace, and this one
    decides whether a delivery claim stands.

    All three conditions are required and each refuses a different mistake:

    * the adapter must SAY there were none, **as the integer** `0`. Silence
      (`None`) is not a statement, and a host system message that was deleted
      between pinning and sending arrives as zero exactly like one that never
      existed. `False` is not a statement either, and it used to be one:
      `False == 0` is **True** in Python, so an adapter handing this a flag
      where a count belongs took the vacuous path and its request passed.
      Found by this project's own bool-is-not-a-count attack, 2026-10-25.
    * no system message in the payload now — if one is present, there is
      something to check and it must be checked.
    * nothing pinned — when a digest was supplied, compare it; the vacuous
      path is for the case where there is nothing to compare, not a cheaper
      alternative to comparing.
    """
    return (_stated_count(host_system_count) and host_system_count == 0
            and not system_messages and not pinned)


#: The three states the Owner's ruling of 2026-10-25 requires be kept apart.
#: Named constants rather than bare literals, because the whole point of the
#: ruling is that two of these were previously the same value: "we checked and
#: it changed" and "we could not check" both reported `false`, and a reader
#: cannot tell a detected modification from an absence of evidence.
ESTABLISHED_UNCHANGED = True
ESTABLISHED_CHANGED = False
NOT_ESTABLISHED = None

#: What each state licenses. Travels in the receipt, because the reader who
#: needs it is reading the JSON, not this file.
UNCHANGED_STATES = (
    "three-valued. `true` = ESTABLISHED UNCHANGED: a digest was pinned before "
    "injection and the payload's first system message still hashes to it, or "
    "the adapter stated there were zero before and there are zero now. "
    "`false` = ESTABLISHED CHANGED: something was compared and differed — a "
    "pinned digest that no longer matches, or a stated count that no longer "
    "matches the payload. `null` = NOT ESTABLISHED: nothing was pinned and "
    "nothing was stated, so neither 'unchanged' nor 'changed' was shown. "
    "`null` NEVER passes: `non_elevation.verified` requires this field to be "
    "`true` by identity, so every delivery claim on such a request degrades "
    "to UNOBSERVABLE. Other delivery facts that stand on their own evidence "
    "are still recorded as they are.")


def _stated_count(host_system_count) -> bool:
    """Did the adapter state a count, as an integer?

    `bool` is excluded explicitly. `True` passes `isinstance(x, int)` because
    `bool` subclasses `int`, so an adapter handing this a flag instead of a
    count would otherwise be read as "one system message" (`True == 1`) —
    the defect class this project calls *typing is not binding*.
    """
    return isinstance(host_system_count, int) and not isinstance(
        host_system_count, bool)


def host_system_message_state(host_system_count, system_messages,
                              pinned: bool, digest_before: str,
                              digest_now: str):
    """`True` / `False` / `None` for the host's own system message.

    A named function rather than an inline expression, for the same reason
    `system_absent_by_construction` is one: a rule written inline is a rule no
    mutation can replace, and this one decides whether a delivery claim
    stands.

    The branches, in the order they are tried, and what each refuses:

    1. **A digest was pinned, and it is not empty.** Then there is a
       comparison to make and its result is binary. A pinned digest that no
       longer matches is a *detected* modification, and so is a pinned digest
       with no system message left to hash — `digest_now` is `""` in exactly
       that case. The emptiness test is not decoration: `pinned` is derived
       from the digest here, but a *recorded* receipt can carry the flag and
       an empty digest together, and "a digest was taken" with no digest
       compares nothing. That combination falls through to *not established*,
       and `v2/frozen_artifacts.rederive_unchanged` takes the same branch for
       the same reason.
    2. **Nothing pinned, absent by construction.** The adapter *stated* zero
       before and the payload carries zero now, so the property holds
       vacuously; `system_absent_by_construction` keeps all three of its
       conditions.
    3. **Nothing pinned, but a stated count that disagrees with the payload.**
       A host system message that is gone, or one that appeared, is
       established as changed by the count alone. Declining to say so would
       hide a modification this function can actually see.
    4. **Anything else.** Nothing was pinned and nothing contradicts: the
       count was never stated, or it was stated and matches, which says
       nothing about the *content*. Not established.
    """
    if pinned and digest_before:
        return (ESTABLISHED_UNCHANGED if digest_now == digest_before
                else ESTABLISHED_CHANGED)
    if system_absent_by_construction(host_system_count, system_messages,
                                     pinned):
        return ESTABLISHED_UNCHANGED
    if _stated_count(host_system_count) and (
            host_system_count != len(system_messages)):
        return ESTABLISHED_CHANGED
    return NOT_ESTABLISHED


def verify(payload: list, assembly, *, lib, attestations: dict,
           observation_point: str, observable_boundary: str,
           host_system_sha256: str = "", tools_offered=None,
           load_attribution=None, host_system_count=None) -> Receipt:
    """Issue a receipt for *assembly* against the payload that actually left.

    *payload* is the message list as it stood at the send boundary.
    *host_system_sha256* is the digest of the host's own system message taken
    **before** injection; without it non-elevation cannot be checked and every
    delivery claim degrades to `UNOBSERVABLE` rather than passing.

    *load_attribution* is `constraintrelay.load_attribution.summarise()` for the loads
    behind this request. Omitted, the receipt reports
    `load_attribution_established: null` and says in as many words that it
    does not establish attribution — it does **not** quietly assume it.
    """
    if not observation_point.strip() or not observable_boundary.strip():
        raise ReceiptError(
            "a receipt must name its observation point and its observable "
            "boundary. A receipt that does not say what it could see is not "
            "evidence of anything.")

    notes: list = []
    capsule_idx, capsule_role, capsule_text = -1, "", ""
    for i, m in enumerate(payload):
        if CAPSULE_MARKER in _text(m):
            capsule_idx, capsule_role = i, (m.get("role") or "")
            capsule_text = _text(m)
            break
    if capsule_idx < 0:
        notes.append("no container capsule is present in this payload")

    # --- non-elevation, three independent properties -----------------------
    sys_msgs = [i for i, m in enumerate(payload)
                if isinstance(m, dict) and m.get("role") == "system"]
    host_sys_now = (sha256_text(_text(payload[sys_msgs[0]]))
                    if sys_msgs else "")
    host_sys_pinned = bool(host_system_sha256.strip())
    #: A payload can legitimately carry NO system message — a LangGraph graph
    #: built from `MessagesState` sends `["user"]` and nothing else, which is
    #: the ordinary shape for the frameworks this project's early users bring.
    #: There is then no host system message to leave unchanged, and demanding
    #: a digest of one meant every such request degraded to UNOBSERVABLE:
    #: measured, a real LangGraph run put the capsule on the wire and could
    #: not be given a verified receipt.
    #:
    #: "Vacuous" is NOT the same as "unverifiable", and it is not assumed —
    #: the adapter must state how many system messages the payload held
    #: BEFORE injection. Without that number, a host system message that was
    #: DELETED between pinning and sending would look identical to one that
    #: never existed, and the deletion would pass. `None` means the adapter
    #: said nothing and the old, stricter behaviour applies unchanged.
    host_sys_vacuous = system_absent_by_construction(
        host_system_count, sys_msgs, host_sys_pinned)
    #: Three-valued per the Owner's ruling of 2026-10-25. `false` used to mean
    #: both "compared and different" and "could not compare", and a reader
    #: cannot act on a field that conflates a detected modification with an
    #: absence of evidence. `null` is the third state and it never passes.
    host_sys_state = host_system_message_state(
        host_system_count, sys_msgs, host_sys_pinned,
        host_system_sha256, host_sys_now)
    non_elev = {
        "capsule_role": capsule_role,
        "role_is_non_elevating": capsule_role in NON_ELEVATING_ROLES,
        "role_is_elevated": capsule_role in ELEVATED_ROLES,
        "capsule_is_not_first_message": capsule_idx != 0,
        "host_system_message_pinned_before_injection": host_sys_pinned,
        "host_system_message_sha256_before": host_system_sha256,
        "host_system_message_sha256_now": host_sys_now,
        "host_system_message_unchanged": host_sys_state,
        "_unchanged_states": UNCHANGED_STATES,
        #: Which of the three states this is, as a word, so a reader does not
        #: have to know that `null` is distinguishable from `false` in
        #: whatever deserialiser they used.
        "host_system_message_state":
            "ESTABLISHED_UNCHANGED" if host_sys_state is ESTABLISHED_UNCHANGED
            else ("ESTABLISHED_CHANGED"
                  if host_sys_state is ESTABLISHED_CHANGED
                  else "NOT_ESTABLISHED"),
        "host_system_message_count": len(sys_msgs),
        #: How many the adapter reported BEFORE injection. `None` means it
        #: did not say, which is why the count alone can never establish
        #: this: 0 now with no statement about then is also what a deletion
        #: looks like.
        "host_system_message_count_before": host_system_count,
        "host_system_message_absent_by_construction": host_sys_vacuous,
        "_vacuous_means": (
            "the adapter reported ZERO host system messages before "
            "injection and the payload carries zero now, so there is no "
            "host system message to leave unchanged and the property holds "
            "vacuously. Reported as its own field rather than folded into "
            "`unchanged`, because 'nothing to check' and 'checked and "
            "identical' are different facts."),
        #: The disclaimer has to be in the payload, not only in the report —
        #: the reader that matters here is the model.
        "non_elevation_notice_in_payload": NON_ELEVATION_NOTICE in capsule_text
                                           if capsule_idx >= 0 else False,
        #: Order must not be read as authority, and the payload has to say so.
        "order_is_not_authority_in_payload":
            ORDER_IS_NOT_AUTHORITY in capsule_text if capsule_idx >= 0
            else False,
        "_why": ("the declaration keeps the author's authority. Travelling in "
                 "a non-elevating role, after the host's own messages, "
                 "leaving the host's system message byte-identical, and "
                 "saying so inside the payload are four separate properties, "
                 "so one of them holding does not cover for another failing."),
    }
    #: `is True`, not truthiness. The ruling of 2026-10-25 requires that when
    #: the state is NOT ESTABLISHED, no check depending on "the host's system
    #: message is unchanged" may pass. `bool(None)` is already false, so
    #: truthiness would give the right answer today — and that is exactly the
    #: shape that has failed in this project every time it was allowed to
    #: stand: the rule would then be a property of `None` rather than a
    #: property of the gate, and the first non-empty sentinel anyone puts in
    #: this field (`"unknown"`, `"null"`, `0.0`) would pass it. An identity
    #: comparison admits one value and that value is the established one.
    non_elev["verified"] = bool(
        capsule_idx >= 0
        and non_elev["role_is_non_elevating"]
        and not non_elev["role_is_elevated"]
        and non_elev["capsule_is_not_first_message"]
        and non_elev["host_system_message_unchanged"] is ESTABLISHED_UNCHANGED
        and non_elev["non_elevation_notice_in_payload"]
        and non_elev["order_is_not_authority_in_payload"])
    if host_sys_state is NOT_ESTABLISHED:
        notes.append(
            "whether the host's own system message is unchanged was NOT "
            "ESTABLISHED on this request: no digest was pinned before "
            "injection and the adapter stated no count that contradicts the "
            "payload. This is not a detected modification and is not "
            "recorded as one — it is an absence of evidence, so "
            "non-elevation is unverified and every delivery claim degrades "
            "to UNOBSERVABLE. Delivery facts resting on their own evidence "
            "are still recorded as observed")
    elif host_sys_state is ESTABLISHED_CHANGED and not host_sys_pinned:
        notes.append(
            "the host's own system message is ESTABLISHED CHANGED by count "
            f"alone: the adapter stated {host_system_count} before "
            f"injection and the payload carries {len(sys_msgs)}. No digest "
            "was pinned, so what it changed to is not established — that it "
            "changed is")
    if host_sys_vacuous:
        notes.append(
            "this payload carries no host system message and the adapter "
            "reported none before injection, so non-elevation's "
            "'unchanged' property holds VACUOUSLY — there was nothing to "
            "change. The other three non-elevation properties were checked "
            "normally")
    #: There is deliberately no "not pinned" note here any more. It said
    #: UNVERIFIABLE for two different states, one of which — a stated count
    #: that disagrees with the payload — is a *detected* change rather than
    #: an absence of evidence. The two notes above say which one happened.

    # --- is this the payload that decides? ---------------------------------
    has_assistant_tool_call = any(
        isinstance(m, dict) and m.get("tool_calls") for m in payload)
    names = []
    for t in (tools_offered or []):
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(fn["name"])
    corr = tool_call_correspondence(payload)
    decision = {
        "tool_names_offered": names,
        "contains_assistant_tool_call": has_assistant_tool_call,
        #: Kept even though the rule no longer turns on it, so no information
        #: is lost and a reader can see WHICH history was present.
        "tool_call_correspondence": corr,
        "is_decision_input_boundary": bool(names) and corr["all_resolved"],
        "_why": ("a payload decides when tools are offered and every tool "
                 "call already in its history is FINISHED — answered by a "
                 "later tool result, matched by id, after the call that "
                 "declared it. A PENDING call means the arguments are already "
                 "fixed and this request is downstream of the decision that "
                 "fixed them. Owner direction 2026-10-20: a completed "
                 "historical call is permitted; an unanswered one, or a "
                 "correspondence this function cannot establish, is not."),
    }
    if not decision["is_decision_input_boundary"]:
        notes.append(
            "this payload is not the decision input boundary (tools offered: "
            f"{bool(names)}, tool-call history resolved: "
            f"{corr['all_resolved']}"
            + (f", unresolved: {corr['why_not']}" if corr["why_not"] else "")
            + "), so delivery claims cannot be verified against it")

    blocking = (not non_elev["verified"]
                or not decision["is_decision_input_boundary"])

    #: Owner ruling 2026-11-06 item 3. Which of the receipt's required inputs
    #: the CALLER did not supply — named, so the verdict stops reading like a
    #: host incompatibility. Both are distinguished from their legitimate
    #: empty forms, because those are statements and this is silence:
    #:
    #:  * `tools_offered=[]` STATES that the host offered none. Still not a
    #:    decision input boundary, still degrades — and not listed here.
    #:    `None` means nobody said, and is.
    #:  * `host_system_sha256=""` with `host_system_count` given is the
    #:    vacuous case, recorded as vacuous. Neither supplied is silence.
    missing_inputs = []
    if tools_offered is None:
        missing_inputs.append("tools_offered")
    if not host_system_sha256.strip() and host_system_count is None:
        missing_inputs.append("host_system_sha256")
    blocking_reason = (RECEIPT_INPUT_MISSING if missing_inputs
                       else UNOBSERVABLE)
    if missing_inputs:
        notes.append(
            "RECEIPT_INPUT_MISSING — the caller did not supply "
            + " and ".join(f"`{m}`" for m in missing_inputs)
            + ". **This is not a host incompatibility.** Without "
              "`tools_offered` the receipt cannot tell a payload that offered "
              "no tools from a caller who did not say; pass the host's own "
              "tool schemas, or `[]` to state that it offered none. Without "
              "`host_system_sha256` — the digest of the host's own system "
              "message taken BEFORE the injection — non-elevation cannot be "
              "checked; pass it, or pass `host_system_count=0` if the payload "
              "carries no system message by construction. Every declaration "
              "may well be present in the payload: check `violations` and the "
              "per-skill `checks` before suspecting the host.")

    verdicts: list = []
    for o in assembly.outcomes:
        cat = getattr(o, "category", CAT_AUTHOR_CONFIRMED)
        exp = expected_texts(lib, attestations, o.skill_id, cat)
        env = envelope_of(payload, o.skill_id, cat)
        env_text = env[1] if env else ""
        present = {did: (t in env_text) for did, t in exp.items()}
        anywhere = {did: any(t in _text(m) for m in payload)
                    for did, t in exp.items()}
        # Re-derive the whole opening tag from the library and the manifest,
        # then require it verbatim. Checking the `declarations=` count alone
        # left every other attribute forgeable: a review rewrote
        # `real-author-confirmation`, `confirmation-method`, `source-version`
        # and `author-confirmed-by` in the payload and the receipt passed it.
        att = attestations.get(o.skill_id)
        want_header = ""
        if att is not None and not isinstance(att, ManifestError):
            want_header = envelope_header(
                o.skill_id, att.source_version, att, len(exp), cat,
                CONF_BY_AUTHOR if cat == CAT_AUTHOR_CONFIRMED
                else CONF_UNCONFIRMED)
        checks = {
            "envelope_found": env is not None,
            "envelope_message_index": (env[0] if env else -1),
            "envelope_terminated": env_text.endswith(CLOSE_TAG) if env else
                                   False,
            "declarations_expected": len(exp),
            "declarations_found_verbatim_in_envelope":
                sum(1 for v in present.values() if v),
            "declarations_found_anywhere_in_payload":
                sum(1 for v in anywhere.values() if v),
            "missing_from_envelope": sorted(k for k, v in present.items()
                                            if not v),
            "declared_count_attribute":
                f'declarations="{len(exp)}"' in env_text if env else False,
            #: Every envelope attribute at once, re-derived not parsed. Covers
            #: source-version, author-confirmed-by, confirmation-method and
            #: real-author-confirmation together with the count.
            "envelope_header_matches_sources":
                bool(want_header) and env_text.startswith(want_header),
            "expected_envelope_header": want_header,
            "category": cat,
            #: For every category except the confirmed one: the unconfirmed
            #: label, the "what this does not mean" note, and one verbatim
            #: source excerpt per item must all be in the envelope. A
            #: candidate travelling without its source is a reading presented
            #: as a fact.
            #: 0.2: the category note is stated ONCE in the capsule header
            #: rather than repeated in every envelope (an outer trim that left
            #: declaration text untouched). So it is checked against the
            #: capsule, and the envelope still has to carry the category and
            #: confirmation attributes, which the header re-derivation covers.
            "category_note_in_capsule":
                f"[{cat}] " + CATEGORY_NOTES.get(cat, "\0") in capsule_text
                if capsule_idx >= 0 else False,
            "unconfirmed_label_present":
                ("(machine-extracted, unconfirmed)" in env_text
                 if cat in CATEGORIES_REQUIRING_EVIDENCE and env else True),
            "evidence_excerpts_present":
                (env_text.count("verbatim source excerpt:") == len(exp)
                 if cat in CATEGORIES_REQUIRING_EVIDENCE and env else True),
        }

        if o.outcome == AVAILABLE_COMPLETE:
            if blocking:
                verdicts.append(SkillVerdict(
                    o.skill_id, o.outcome, FAILED, blocking_reason, checks,
                    {"reason": (
                        "the caller did not supply "
                        + " and ".join(f"`{m}`" for m in missing_inputs)
                        + ", so there was nothing to verify against. This is "
                          "NOT a host incompatibility — see the receipt's "
                          "notes and `missing_inputs`."
                        if missing_inputs else
                        "the claim could not be verified at this "
                        "boundary; see the receipt's notes. Absent "
                        "evidence is a failure, not a delivery."),
                     #: Present only when there IS an omission. An
                     #: unconditional key — even an empty list — moves the
                     #: projection digest of every sealed receipt, and this
                     #: one drifted `container_receipt_s_f.json` for a field
                     #: that said nothing. The information belongs exactly
                     #: where it applies.
                     **({"missing_inputs": list(missing_inputs)}
                        if missing_inputs else {}),
                     "notes": list(notes)}))
                continue
            if env is None:
                verdicts.append(SkillVerdict(
                    o.skill_id, o.outcome, FAILED, UNOBSERVABLE, checks,
                    {"reason": "no envelope for this skill is in the payload"}))
                continue
            whole = (checks["envelope_terminated"]
                     and len(exp) == len(o.delivered_ids)
                     and all(present.values())
                     and checks["declared_count_attribute"]
                     and checks["envelope_header_matches_sources"]
                     and checks["category_note_in_capsule"]
                     and checks["unconfirmed_label_present"]
                     and checks["evidence_excerpts_present"])
            if not whole:
                verdicts.append(SkillVerdict(
                    o.skill_id, o.outcome, FAILED, UNOBSERVABLE, checks,
                    {"reason": "the skill was claimed complete but the payload "
                               "does not carry every confirmed declaration "
                               "byte-for-byte inside a terminated envelope. "
                               "A partial arrival is a failure, not a partial "
                               "success.",
                     "claimed_delivered": list(o.delivered_ids)}))
                continue
            verdicts.append(SkillVerdict(
                o.skill_id, o.outcome, AVAILABLE_COMPLETE, "", checks,
                {"verbatim": "every confirmed declaration was found as exact "
                             "bytes inside the skill's terminated envelope",
                 "declaration_state": o.declaration_state}))
            continue

        # --- FAILED / UNDECLARED: nothing of this skill may have been sent --
        leaked = sorted(k for k, v in anywhere.items() if v)
        if env is not None or leaked:
            verdicts.append(SkillVerdict(
                o.skill_id, o.outcome, FAILED, UNOBSERVABLE, checks,
                {"reason": "this skill was not delivered, yet the payload "
                           "carries part of it. All-or-nothing was violated on "
                           "the failing branch.",
                 "leaked_declarations": leaked,
                 "envelope_present": env is not None},
                violation=PARTIAL_SEND_ON_FAILED_BRANCH))
            continue
        verdicts.append(SkillVerdict(
            o.skill_id, o.outcome, o.outcome, o.failure_reason, checks,
            {"reason": "nothing of this skill is in the payload, which is what "
                       f"outcome {o.outcome} requires",
             "declaration_state": o.declaration_state,
             "claimed_failure_reason": o.failure_reason}))

    att = dict(load_attribution or {})
    if not att:
        notes.append(
            "no load-attribution evidence was supplied, so this receipt does "
            "NOT establish that the loads behind this request belong to the "
            "agent being delivered into (load_attribution_established: null)")
    elif att.get("established") is False:
        notes.append(
            "load attribution NOT verified for "
            f"{att.get('withheld_from_delivery')}: those loads contributed no "
            "declaration and this receipt may not be read as verified")

    return Receipt(
        request_id=assembly.request.request_id,
        observation_point=observation_point,
        observable_boundary=observable_boundary,
        verdicts=verdicts, payload_sha256=_digest(payload),
        payload_messages=len(payload), capsule_message_index=capsule_idx,
        capsule_role=capsule_role, at=_now(), non_elevation=non_elev,
        decision_payload=decision, notes=notes,
        fixture_accepted=bool(
            getattr(assembly.request, "accept_fixture_attestations", False)),
        category_enabling=(assembly.request.enabling_record()
                           if hasattr(assembly.request, "enabling_record")
                           else {}),
        load_attribution=dict(load_attribution or {}),
        missing_inputs=list(missing_inputs))
