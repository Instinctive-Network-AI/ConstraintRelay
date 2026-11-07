"""Phase 1 — the constraint container. Collect, deliver; verification is next
door in `cf/container_receipt.py`, deliberately not here.

Owner ruling 2026-10-15. One acceptance question: **are the author's declared
constraints completely available at the decision point?** Everything this module
does or refuses to do follows from that sentence.

What the container does
-----------------------
For each `(skill_id, source_version)` the **host explicitly names**, it emits
that skill's declarations verbatim, whole, or it fails that skill and says why.

What the container refuses to do, as contract and not as caveat
---------------------------------------------------------------
* **No condition judging.** A declaration reading *"when confidence is below
  0.6 … except in backtest mode"* is carried verbatim. Nothing here decides
  whether the condition holds.
* **No tool-level filtering.** `cf/selector.py` filters by `tool_names` and
  `action_tags`; the container does not call it. Narrowing by tool is a
  selection decision, and a container that selects can drop a declaration the
  author expected to arrive. `v2/container_vibe.py` asserts the two code paths
  disagree, so this is a measured difference rather than a claim.
* **No approval, no interception, no blocking.** There is no verdict type in
  this module.
* **No elevation.** The payload block carries the author's authority and says
  so. `cf/container_receipt.py` mechanically refuses a `system` role and
  mechanically refuses any injection that modified the host's own system
  message.
* **No packing.** Capacity is consumed in the host's declared order. The first
  skill that does not fit fails, and so does every skill after it. Reordering or
  best-fit packing would make the outcome depend on a choice the container made,
  which is selection by another name.

Three outcomes, and the fourth thing that is not an outcome
-----------------------------------------------------------
Per decision request, per skill, exactly one of:

    AVAILABLE_COMPLETE   every declaration entered the request, whole
    FAILED               with a reason, and NOTHING of that skill was sent
    UNDECLARED           no declaration source for this skill

`DECLARED_EMPTY` is **not** a fourth outcome. A skill whose author confirmed a
manifest holding zero declarations is `AVAILABLE_COMPLETE` with
`declaration_state = DECLARED_EMPTY`: complete is complete, and zero of zero is
complete. It is recorded in its own field precisely so it never reads as
`UNDECLARED` — "the author says there are none" and "nobody ever said" are
different sentences and the Owner asked for both to survive.

The seal is not the manifest
----------------------------
A content hash proves the bytes did not move. It cannot prove **who** wrote
them, and it cannot prove the author **confirmed** them. So there are two
independent pieces of evidence per declaration, two independent booleans, and
`cf/container_receipt.py` reports them separately and never infers one from the
other:

* **seal** — `declared_text_sha256` over the declaration text (content
  consistency);
* **manifest** — an `Attestation`: who confirmed, when, by what method, and
  where that confirmation can be found (author identity and confirmation).

A draft is not a declaration
----------------------------
A marker in a source file is a **candidate**. It becomes a declaration when the
author's manifest names it. The repository being ours changes nothing: this
module cannot mark its own drafts confirmed, and `Attestation.fixture` is a
one-way label — a fixture never reports real author confirmation, and
`REAL_METHODS` requires an `evidence_ref` that a fixture does not have.

Categories, and why collection is not delivery
----------------------------------------------
Owner direction 2026-10-16. The container holds four kinds of material —
author-confirmed declarations, machine-extracted candidates, runtime task
candidates, and regulatory source text — each with its own source, version,
confirmation status and verbatim evidence. They are **never merged into one
weighted ranking**, nothing ranks one above another, and emission order carries
no authority.

**Collecting is not sending.** `ContainerRequest.deliver_categories` defaults to
the confirmed category alone; any other category travels only because the host
asked for it, and then always labelled *machine-extracted, unconfirmed* with its
verbatim source excerpt beside it. An item that was collected and withheld is
reported `FAILED` / `NOT_AUTHOR_CONFIRMED` with the ids — never as an absence,
because "we did not send it" and "there is none" must not be the same sentence.

One outcome per **(skill, category)**: the confirmed set and the candidate set
are complete or failed independently, because they are different claims. This
replaced an earlier rule where any unconfirmed entry failed the whole skill.
That rule was right while the container had one category — confirmed and
extracted material were being mixed, so a partial send was a real risk — and is
wrong now that they are separate: the confirmed set is complete on its own.

**The author not replying is not confirmation.** There is no timeout, no
default-to-yes, and no code path that promotes a candidate without a manifest.

This module does not pause, block or decide
-------------------------------------------
Owner direction 2026-10-16. `cf/phase1_pause.py` is a **development-time guard
against accidental paid provider calls** and nothing else. It is not a
production control and it has no reach into delivery: neither this module nor
`cf/container_receipt.py` imports it, consults it, or changes behaviour when it
is active, and `tests/test_container_phase1.py` asserts both files contain no
reference to it.

So a **production container never self-pauses and never blocks a host
request.** When delivery fails it reports the failure, with a reason and the
ids, and the host decides what to do about it. There is no verdict type here,
no retry, no refusal to proceed, and no code path in which the container
withholds something the host asked for on its own initiative — withholding
happens only where the host did not enable the category.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .library import Library, file_digest, sha256_text

#: The per-skill author manifest, beside the skill's own files.
ATTESTATION_FILE = ".constraintrelay-attestation.json"

# --- outcomes, exactly three -----------------------------------------------
AVAILABLE_COMPLETE = "AVAILABLE_COMPLETE"
FAILED = "FAILED"
UNDECLARED = "UNDECLARED"
OUTCOMES = (AVAILABLE_COMPLETE, FAILED, UNDECLARED)

# --- failure reasons -------------------------------------------------------
#: The host asked for a version the library was not built from, or the bytes
#: behind the built version moved.
VERSION_MISMATCH = "VERSION_MISMATCH"
#: A declaration source, manifest or span could not be read or did not parse.
UNREADABLE = "UNREADABLE"
#: The complete block does not fit the capacity the host allowed. Never
#: truncated — that is the whole reason this reason exists.
CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
#: Delivery could not be observed or could not be verified. Added by the Owner
#: correction of 2026-10-15: evidence that is missing is a failure, never a
#: delivery. Raised by `cf/container_receipt.py`, never by assembly.
UNOBSERVABLE = "UNOBSERVABLE"
#: The CALLER did not supply an input the receipt needs in order to observe
#: anything. A strictly narrower case of the one above, split out by the Owner
#: ruling of 2026-11-06 item 3, and the reason it is worth its own name is a
#: measured cost: `UNOBSERVABLE` with an EMPTY violations list and every
#: declaration found verbatim in the payload is a correct, fail-closed verdict
#: that reads, to a reader, exactly like *the host changed and broke the
#: adapter*. It cost real time during the 2026-11-06 Vibe-Trading v0.1.19
#: compatibility check: the probe omitted `host_system_sha256` and passed no
#: tools, and the red was first taken for a possible host incompatibility.
#:
#: Why a reason and not a raised error: refusing the call outright would have
#: closed the only route to `host_system_message_unchanged: null`, and the
#: Owner's 2026-10-25 ruling requires that third state to be REACHABLE and to
#: never pass. So the state survives and the verdict now says whose omission
#: it was. `receipt.missing_inputs` names them, and the note says in as many
#: words that this is not a host incompatibility.
RECEIPT_INPUT_MISSING = "RECEIPT_INPUT_MISSING"
#: The skill holds a candidate the author's manifest does not confirm.
NOT_AUTHOR_CONFIRMED = "NOT_AUTHOR_CONFIRMED"
#: A manifest that PARSES and is waiting for its author. Owner direction
#: 2026-10-16: a readable draft is "pending confirmation, not delivered" — it
#: is not `UNREADABLE`, which says the file could not be used at all and sends
#: the reader to look for corruption that is not there.
PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
FAILURE_REASONS = (VERSION_MISMATCH, UNREADABLE, CAPACITY_EXCEEDED,
                   UNOBSERVABLE, NOT_AUTHOR_CONFIRMED, PENDING_CONFIRMATION)

# --- categories, kept apart on purpose ------------------------------------
#: Owner direction 2026-10-16: the container holds **several kinds** of
#: material. They are recorded and delivered as separate categories, each with
#: its own source, version, confirmation status and verbatim evidence, and they
#: are **never merged into one weighted ranking**. Nothing here ranks one
#: category above another, and nothing uses emission order to express
#: authority. `ORDER_IS_NOT_AUTHORITY` states the rule in the payload itself.
#:
#: The rule, written down rather than left to be inferred: **skills in the
#: order the host listed them; within a skill, `machine_candidate` before
#: `author_confirmed`.** The inner order is a property of the single assembly
#: pass — the candidate branch runs before the confirmed block is built — not
#: a judgement about either category.
#:
#: What is promised is exactly that: a deterministic rule, and order not used
#: to express authority. **It is not claimed that a reader cannot recover a
#: ranking from position** — that was an overclaim in an earlier revision and
#: the Owner struck it. What is tested is narrower and true:
#: `v2/container_vibe.py` permutes the host's category list and asserts the
#: delivered set and every label come out identical.
#:
#: The author confirmed these, by a locatable method. Delivered by default.
CAT_AUTHOR_CONFIRMED = "author_confirmed"
#: Extracted by a machine and NOT confirmed by anyone. Delivered only when the
#: host asks for the category, and then always labelled as such, with the
#: verbatim source excerpt beside it.
CAT_MACHINE_CANDIDATE = "machine_candidate"
#: Derived from the current task. **Not a user authorisation** — that a runtime
#: candidate exists says a task mentioned something, not that the user
#: permitted it.
CAT_RUNTIME_CANDIDATE = "runtime_candidate"
#: Statute, rule or policy text. **Not a finding of applicability** — carrying
#: a regulation is not judging that it applies here.
CAT_REGULATORY_SOURCE = "regulatory_source"
CATEGORIES = (CAT_AUTHOR_CONFIRMED, CAT_MACHINE_CANDIDATE,
              CAT_RUNTIME_CANDIDATE, CAT_REGULATORY_SOURCE)

#: What each category does **not** mean. Printed inside the payload next to the
#: items, because the misreading each one invites is the whole reason the
#: categories are separate.
CATEGORY_NOTES = {
    CAT_AUTHOR_CONFIRMED:
        "The skill's author confirmed these, by the method named in the "
        "envelope. This is the only category that carries author authority, "
        "and it still carries nothing else: conditions and exceptions are "
        "unjudged and this is not permission to act.",
    CAT_MACHINE_CANDIDATE:
        "STATUS: machine-extracted, not confirmed by anyone. A candidate is a "
        "reading of the source, not the author's word. FORMAT: the verbatim "
        "source excerpt accompanies each item, so the reading is checkable "
        "against its source. The author not replying is not confirmation.",
    CAT_RUNTIME_CANDIDATE:
        "Derived from the current task. THIS IS NOT A USER AUTHORISATION: it "
        "records that the task mentioned something, not that the user "
        "permitted it, and nobody has confirmed the reading.",
    CAT_REGULATORY_SOURCE:
        "Source material from a statute, rule or policy. THIS IS NOT A "
        "FINDING OF APPLICABILITY: carrying it here is not a judgement that "
        "it applies to this call, and no such judgement was made.",
}

#: Categories whose items may only travel with their verbatim source excerpt.
#: Author-confirmed items are the author's own words; a candidate is somebody's
#: *reading* of a source, so the source has to travel with it.
CATEGORIES_REQUIRING_EVIDENCE = (CAT_MACHINE_CANDIDATE, CAT_RUNTIME_CANDIDATE,
                                 CAT_REGULATORY_SOURCE)

#: The default delivery set. One category, the confirmed one. Owner direction:
#: "默认交付已确认声明；宿主可以明确启用未确认候选" — enabling anything else is
#: an explicit, recorded act by the host.
DEFAULT_DELIVER_CATEGORIES = (CAT_AUTHOR_CONFIRMED,)

# --- confirmation status, recorded per item -------------------------------
CONF_BY_AUTHOR = "confirmed_by_author"
CONF_UNCONFIRMED = "unconfirmed"
CONFIRMATION_STATES = (CONF_BY_AUTHOR, CONF_UNCONFIRMED)

DECLARED_NONEMPTY = "DECLARED_NONEMPTY"
DECLARED_EMPTY = "DECLARED_EMPTY"
NOT_DECLARED = "NOT_DECLARED"
#: A manifest exists and cannot be read, so what was declared is UNKNOWN.
#: A fourth *state*, not a fourth outcome — the outcome is FAILED/UNREADABLE.
#: Reusing `NOT_DECLARED` here would say "nobody declared", which is precisely
#: the confusion this field exists to prevent.
DECLARATION_UNREADABLE = "DECLARATION_UNREADABLE"
#: The manifest reads cleanly and names its declarations; the author has not
#: adopted it yet. Distinct from every state above: the declarations are known,
#: they are simply not anybody's word yet.
DECLARED_PENDING_CONFIRMATION = "DECLARED_PENDING_CONFIRMATION"
DECLARATION_STATES = (DECLARED_NONEMPTY, DECLARED_EMPTY, NOT_DECLARED,
                      DECLARATION_UNREADABLE, DECLARED_PENDING_CONFIRMATION)

# --- confirmation methods --------------------------------------------------
#: A clearly-labelled engineering fixture. Permitted by the Owner correction of
#: 2026-10-15 for engineering tests, and never real author confirmation.
METHOD_FIXTURE = "fixture"
#: Methods that assert a real human confirmed. Each requires `evidence_ref`.
REAL_METHODS = ("signed_git_commit", "written_reply", "in_person")
CONFIRMATION_METHODS = (METHOD_FIXTURE,) + REAL_METHODS

#: Envelope delimiters. A declaration whose text contains either one would make
#: the framing ambiguous, so that skill fails rather than shipping something a
#: receipt cannot parse unambiguously.
OPEN_TAG = "<constraintrelay-declarations"
CLOSE_TAG = "</constraintrelay-declarations>"

#: Printed inside every block. The non-elevation statement is part of the
#: payload, not only of the report, because the reader that matters is the
#: model.
#: Printed in the payload. Order expresses nothing here: the categories are
#: emitted in the order the HOST listed them, and `v2/container_vibe.py`
#: permutes that list to prove it. A reader who infers priority from position
#: is reading something the container did not say.
ORDER_IS_NOT_AUTHORITY = (
    "FORMAT: sections are emitted by a fixed, deterministic rule — the skills "
    "in the order the host listed them, and within each skill the "
    "machine_candidate section before the author_confirmed one. That rule is "
    "a property of how this block is assembled; order is not used to express "
    "authority and is not a statement of importance, precedence or "
    "confidence. Nothing here is ranked against anything else, and each "
    "section's own label states what it is and is not.")

NON_ELEVATION_NOTICE = (
    "FORMAT: this block is reference material quoted verbatim from the skill "
    "author's own declaration. It is not a system instruction, it does not "
    "modify the host's instructions or any permission, and it grants nothing. "
    "It carries the author's authority and no more. STATUS: nothing in it has "
    "been evaluated, filtered, adjudicated or approved — conditions, "
    "exceptions and scope limits are the author's words, unjudged.")


class ContainerError(Exception):
    """A misuse of the container, including reading an outcome as a decision."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- manifest --
@dataclass(frozen=True)
class DeclarationAttestation:
    """One declaration as the author confirmed it. The seal lives here too,
    but as a *separate field* that proves a separate thing."""

    declaration_id: str
    declared_text_sha256: str
    declared_text_chars: int

    def as_dict(self) -> dict:
        return {"declaration_id": self.declaration_id,
                "declared_text_sha256": self.declared_text_sha256,
                "declared_text_chars": self.declared_text_chars}


@dataclass(frozen=True)
class Attestation:
    """A skill's author manifest: who confirmed what, when, and how.

    This is the *manifest*. The per-declaration hash inside it is the *seal*.
    `real_author_confirmation` is False for a fixture no matter what else the
    file says, and there is no code path that flips it.
    """

    skill_id: str
    source_version: str
    confirmed_by: str
    confirmed_at: str
    confirmation_method: str
    #: Where the confirmation itself can be found — a signed commit sha, a
    #: message id, a meeting note. Required for every `REAL_METHODS` value,
    #: because an unlocatable confirmation cannot be checked by anyone.
    evidence_ref: str = ""
    declarations: tuple = ()        # tuple[DeclarationAttestation, ...]
    fixture: bool = False
    fixture_note: str = ""
    path: str = ""
    #: True when the manifest parses and names its declarations but carries no
    #: confirmation method yet — an agent-prepared draft awaiting its author.
    #: Owner direction 2026-10-16: this is "pending confirmation", reported as
    #: such and NOT delivered. It is not a read error.
    pending: bool = False

    @property
    def real_author_confirmation(self) -> bool:
        """True only for a locatable confirmation by a real method.

        A fixture is excluded first, so no combination of the other fields can
        make an engineering fixture read as a real confirmation.
        """
        if self.pending:
            return False
        if self.fixture or self.confirmation_method == METHOD_FIXTURE:
            return False
        return (self.confirmation_method in REAL_METHODS
                and bool(self.confirmed_by.strip())
                and bool(self.evidence_ref.strip()))

    @property
    def ids(self) -> frozenset:
        return frozenset(d.declaration_id for d in self.declarations)

    def seal_for(self, declaration_id: str) -> "DeclarationAttestation | None":
        for d in self.declarations:
            if d.declaration_id == declaration_id:
                return d
        return None

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id,
                "source_version": self.source_version,
                "confirmed_by": self.confirmed_by,
                "confirmed_at": self.confirmed_at,
                "confirmation_method": self.confirmation_method,
                "evidence_ref": self.evidence_ref,
                "fixture": self.fixture, "fixture_note": self.fixture_note,
                "real_author_confirmation": self.real_author_confirmation,
                "pending_confirmation": self.pending,
                "_fields_are_a_record_not_evidence": (
                    "confirmed_by / confirmation_method / evidence_ref are the "
                    "RECORD of a human act. Nothing here verifies that the act "
                    "happened: no signature is checked and no reference is "
                    "resolved, so filling these fields is not itself a "
                    "confirmation basis."),
                "confirmation_basis_machine_verified": False,
                "declarations": [d.as_dict() for d in self.declarations],
                "path": self.path,
                "_manifest_is_not_the_seal": (
                    "this manifest is the evidence of author identity and "
                    "author confirmation. declared_text_sha256 is the seal and "
                    "evidences content consistency only. A matching seal does "
                    "NOT establish who wrote the text or that they confirmed "
                    "it, and neither field is derived from the other.")}

    @staticmethod
    def load(path: Path) -> "Attestation":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        method = raw.get("confirmation_method", "")
        pending = not str(method).strip()
        if not pending and method not in CONFIRMATION_METHODS:
            raise ContainerError(
                f"{path}: confirmation_method {method!r} is not one of "
                f"{list(CONFIRMATION_METHODS)}. How a confirmation was "
                "obtained may not be left open.")
        fixture = bool(raw.get("fixture"))
        if fixture and method != METHOD_FIXTURE:
            raise ContainerError(
                f"{path}: fixture=true with confirmation_method {method!r}. A "
                "fixture may not claim a real confirmation method.")
        if method == METHOD_FIXTURE and not fixture:
            raise ContainerError(
                f"{path}: confirmation_method 'fixture' must set fixture=true, "
                "so no reader can miss the label.")
        if fixture and not raw.get("fixture_note", "").strip():
            raise ContainerError(
                f"{path}: a fixture must carry a fixture_note saying what it "
                "stands in for.")
        if method in REAL_METHODS and not raw.get("evidence_ref", "").strip():
            raise ContainerError(
                f"{path}: confirmation_method {method!r} requires an "
                "evidence_ref. A confirmation nobody can locate cannot be "
                "checked, so it may not be recorded as one.")
        if not pending and not raw.get("confirmed_by", "").strip():
            raise ContainerError(f"{path}: confirmed_by is empty")
        decls = []
        seen = set()
        for d in raw.get("declarations", []) or []:
            did = d.get("declaration_id", "")
            if not did:
                raise ContainerError(f"{path}: a declaration has no id")
            if did in seen:
                raise ContainerError(f"{path}: duplicate declaration id {did!r}")
            seen.add(did)
            sha = d.get("declared_text_sha256", "")
            if len(sha) != 64:
                raise ContainerError(
                    f"{path}: {did} has no usable declared_text_sha256")
            decls.append(DeclarationAttestation(
                declaration_id=did, declared_text_sha256=sha,
                declared_text_chars=int(d.get("declared_text_chars", 0))))
        return Attestation(
            skill_id=raw.get("skill_id", ""),
            source_version=raw.get("source_version", ""),
            confirmed_by=raw.get("confirmed_by", ""),
            confirmed_at=raw.get("confirmed_at", ""),
            confirmation_method=method,
            evidence_ref=raw.get("evidence_ref", ""),
            declarations=tuple(decls), fixture=fixture,
            fixture_note=raw.get("fixture_note", ""), path=str(path),
            pending=pending)


@dataclass(frozen=True)
class ManifestError:
    """A manifest that exists and cannot be used. **Present, not absent.**

    Returned in place of an `Attestation` so an unreadable manifest becomes a
    per-skill `FAILED` / `UNREADABLE` outcome. The first version let
    `Attestation.load` raise straight out of `load_attestations`, which killed
    the whole decision request — so `UNREADABLE`, one of the three failure
    reasons the Owner named, was not reachable for the commonest way a manifest
    goes wrong. Found by a fresh-context review.

    It is deliberately not an absent entry either: a corrupt manifest is not
    `UNDECLARED`. Somebody declared; the declaration cannot be read.
    """

    skill_id: str
    path: str
    reason: str

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id, "path": self.path,
                "reason": self.reason,
                "_present_not_absent": (
                    "a manifest that cannot be read is FAILED/UNREADABLE, "
                    "never UNDECLARED. Somebody declared; the declaration "
                    "cannot be read.")}


def load_attestations(source_root: Path, skill_dirs: dict) -> dict:
    """`skill_id -> Attestation | ManifestError` for each skill with a manifest.

    *skill_dirs* maps `skill_id -> directory relative to source_root`. A skill
    with no manifest is simply absent, which is what makes `UNDECLARED`
    reachable rather than theoretical.

    Never raises for a bad manifest. One corrupt file must cost its own skill
    and no other.
    """
    out = {}
    for sid, rel in skill_dirs.items():
        p = Path(source_root) / rel / ATTESTATION_FILE
        # `exists()`, not `is_file()`. A review replaced the manifest with a
        # DIRECTORY of the same name: `is_file()` was False, the entry was
        # skipped, and the skill came back `UNDECLARED` — "nobody declared" for
        # a path that plainly exists. Anything occupying the manifest's name is
        # a declaration that cannot be read.
        if not (p.exists() or p.is_symlink()):
            continue
        if not p.is_file():
            out[sid] = ManifestError(
                skill_id=sid, path=str(p),
                reason=f"the manifest path exists but is not a regular file "
                       f"(directory={p.is_dir()}, symlink={p.is_symlink()}). "
                       "Something occupies the manifest's name, so the "
                       "declaration cannot be read — which is not the same as "
                       "nobody having declared.")
            continue
        try:
            out[sid] = Attestation.load(p)
        except (ContainerError, json.JSONDecodeError, OSError,
                UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
            out[sid] = ManifestError(
                skill_id=sid, path=str(p),
                reason=f"{type(exc).__name__}: {exc}")
    return out


# ----------------------------------------------------------------- request --
@dataclass(frozen=True)
class SkillRequest:
    """One skill the host explicitly names, at the version it names."""

    skill_id: str
    source_version: str

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id,
                "source_version": self.source_version}


@dataclass
class ContainerRequest:
    """A decision request. The host supplies the skills; nothing is inferred.

    `capacity_chars` is the host's budget for the whole capsule body. `None`
    means unbounded, which is a different statement from a large number and is
    reported as such.
    """

    request_id: str
    skills: tuple = ()               # tuple[SkillRequest, ...]
    capacity_chars: "int | None" = None
    decision_point: str = ""
    #: Permit clearly-labelled fixture manifests. **Default False.** The Owner
    #: allows a labelled fixture for engineering tests and requires real
    #: confirmation for formal delivery, so the permission is an explicit,
    #: recorded argument rather than a property of the manifest. A fixture
    #: under the default fails with `NOT_AUTHOR_CONFIRMED`.
    accept_fixture_attestations: bool = False
    #: Which categories may be DELIVERED. Collection is not delivery: the
    #: container holds every category it was given, and sends only these.
    #: Default is the confirmed category alone; anything else is an explicit,
    #: recorded choice by the host and travels labelled.
    deliver_categories: tuple = DEFAULT_DELIVER_CATEGORIES
    #: Who turned on a non-default category, **as the host supplied it**. Left
    #: empty when the host supplied nothing: an unknown identity is recorded as
    #: unknown and never guessed at, inferred from the process owner, or filled
    #: from a git config. Owner direction 2026-10-16.
    categories_enabled_by: str = ""
    #: When, as the host supplied it. Empty means the host did not say; the
    #: container stamps `enabling_observed_at` separately so the two cannot be
    #: confused.
    categories_enabled_at: str = ""

    def enabling_record(self) -> dict:
        """What is known about the decision to enable a non-default category.

        **Enabling a category is not author confirmation.** It records that the
        host asked for unconfirmed material, which is a different act by a
        different party; the items still travel labelled unconfirmed.
        """
        extra = [c for c in self.deliver_categories
                 if c not in DEFAULT_DELIVER_CATEGORIES]
        return {
            "non_default_categories_enabled": extra,
            "enabled_by": self.categories_enabled_by or None,
            "enabled_by_known": bool(self.categories_enabled_by.strip()),
            "enabled_at_per_host": self.categories_enabled_at or None,
            "enabling_observed_at": _now(),
            "_identity_not_fabricated": (
                "enabled_by is exactly what the host supplied. When the host "
                "supplied nothing it is null and enabled_by_known is false: "
                "an identity that cannot be obtained is recorded as unknown, "
                "never inferred from the process owner or a git config."),
            "_enabling_is_not_confirmation": (
                "enabling a category records that the HOST asked for "
                "unconfirmed material. It is not author confirmation, it is "
                "not evidence about the items, and every item still travels "
                "labelled unconfirmed with its verbatim source."),
        }

    def delivers(self, category: str) -> bool:
        return category in self.deliver_categories

    def __post_init__(self) -> None:
        # This flag decides whether unconfirmed drafts enter delivery, so it
        # takes a bool and nothing else. A review passed the STRING "false"
        # and the fixture was delivered — `not "false"` is False, so a value
        # that reads as off turned the gate off. `"no"`, `"0"` and `1` did the
        # same. Coercing them would be guessing at intent on the one flag where
        # guessing wrong ships an unconfirmed draft, so a non-bool is refused.
        if not self.deliver_categories:
            raise ContainerError(
                "deliver_categories is empty. A request that delivers no "
                "category is not a request; say which categories you want.")
        unknown = [c for c in self.deliver_categories if c not in CATEGORIES]
        if unknown:
            raise ContainerError(
                f"unknown categories {unknown}; expected a subset of "
                f"{list(CATEGORIES)}. A typo must not silently become an "
                "empty delivery.")
        if len(set(self.deliver_categories)) != len(self.deliver_categories):
            raise ContainerError(
                f"deliver_categories {list(self.deliver_categories)} repeats a "
                "category; one category gets one outcome per skill.")

        if self.accept_fixture_attestations is not True \
                and self.accept_fixture_attestations is not False:
            raise ContainerError(
                f"accept_fixture_attestations must be a bool, got "
                f"{type(self.accept_fixture_attestations).__name__} "
                f"{self.accept_fixture_attestations!r}. This flag decides "
                "whether a manifest that is NOT a real author confirmation may "
                "be delivered; a truthy string like 'false' would silently "
                "turn it on, so anything other than True or False is refused.")

        # One skill, one outcome. The ruling allows exactly three results per
        # decision request PER SKILL, so the same skill named twice produced
        # two outcomes for one skill and `by_skill` silently answered with the
        # first. A malformed request is refused loudly instead of being
        # half-honoured. Found by a fresh-context review.
        seen: dict = {}
        for s in self.skills:
            if s.skill_id in seen:
                raise ContainerError(
                    f"skill {s.skill_id!r} appears twice in request "
                    f"{self.request_id!r} (versions {seen[s.skill_id]!r} and "
                    f"{s.source_version!r}). One skill gets one outcome, so a "
                    "duplicate has no well-defined result and the request is "
                    "refused rather than answered twice.")
            seen[s.skill_id] = s.source_version

    def as_dict(self) -> dict:
        return {"request_id": self.request_id,
                "deliver_categories": list(self.deliver_categories),
                "enabling": self.enabling_record(),
                "collected_not_delivered_is_not_absent": (
                    "categories outside deliver_categories are still "
                    "COLLECTED. An item withheld is reported FAILED with a "
                    "reason, never as an absence."),
                "accept_fixture_attestations":
                    self.accept_fixture_attestations,
                "skills": [s.as_dict() for s in self.skills],
                "capacity_chars": self.capacity_chars,
                "capacity_bounded": self.capacity_chars is not None,
                "decision_point": self.decision_point}


# ---------------------------------------------------------------- outcomes --
@dataclass
class SkillOutcome:
    """What happened to ONE skill in ONE request. Not a decision.

    `__bool__` raises for the reason `constraintrelay.selector.Coverage.__bool__` does: the
    recurring defect in this project is a report that reads as a verdict, and
    `if outcome:` is exactly that mistake.
    """

    skill_id: str
    requested_version: str
    outcome: str
    declaration_state: str
    #: Which kind of material this outcome is about. One outcome per
    #: (skill, category): the confirmed set and the candidate set are complete
    #: or failed *independently*, because they are different claims.
    category: str = CAT_AUTHOR_CONFIRMED
    #: Per-item confirmation status for this category. `confirmed_by_author`
    #: only where an author actually confirmed.
    confirmation_status: str = CONF_BY_AUTHOR
    #: `id -> verbatim source excerpt`. Required for every category except the
    #: confirmed one, where the item IS the author's own text.
    evidence_texts: dict = field(default_factory=dict)
    failure_reason: str = ""
    detail: dict = field(default_factory=dict)
    #: Declaration ids that entered the request, in emission order. Empty for
    #: every outcome other than AVAILABLE_COMPLETE.
    delivered_ids: tuple = ()
    #: `id -> exact text` for everything delivered. The receipt searches the
    #: payload for these bytes; it does not trust this field as evidence.
    delivered_texts: dict = field(default_factory=dict)
    declared_count: int = 0
    block_chars: int = 0
    attestation: dict = field(default_factory=dict)
    #: Per declaration: `seal_ok` and `manifest_ok`, separately.
    evidence: list = field(default_factory=list)

    def __bool__(self) -> bool:
        raise ContainerError(
            "a SkillOutcome is a report, not a decision. It states whether the "
            "author's declarations became available; it carries no allow/deny "
            "meaning. Read `.outcome` and decide explicitly.")

    @property
    def complete(self) -> bool:
        return self.outcome == AVAILABLE_COMPLETE

    def as_dict(self) -> dict:
        return {"skill_id": self.skill_id,
                "requested_version": self.requested_version,
                "category": self.category,
                "confirmation_status": self.confirmation_status,
                "_category_does_not_mean": CATEGORY_NOTES.get(self.category, ""),
                "outcome": self.outcome,
                "declaration_state": self.declaration_state,
                "failure_reason": self.failure_reason,
                "detail": dict(self.detail),
                "delivered_ids": list(self.delivered_ids),
                "declared_count": self.declared_count,
                "delivered_count": len(self.delivered_ids),
                "block_chars": self.block_chars,
                "attestation": dict(self.attestation),
                "evidence": list(self.evidence),
                "_not_a_decision": (
                    "an outcome reports whether the author's declarations "
                    "became available. It is not an authorisation, and "
                    "AVAILABLE_COMPLETE does not mean the agent obeyed."),
                "_empty_vs_undeclared": (
                    "declaration_state DECLARED_EMPTY means the author "
                    "confirmed a manifest with no declarations. NOT_DECLARED "
                    "means no manifest exists. They are recorded apart on "
                    "purpose and neither implies the other.")}


@dataclass
class Assembly:
    """The capsule body and one outcome per requested skill.

    This object is the **claim**, not the evidence. `cf/container_receipt.py`
    re-derives everything from the payload that actually left, because a store
    write and a successful render are not delivery.
    """

    request: ContainerRequest
    body: str
    outcomes: list = field(default_factory=list)      # list[SkillOutcome]
    assembled_at: str = ""
    library_content_id: str = ""

    def by_skill(self, skill_id: str,
                 category: str = CAT_AUTHOR_CONFIRMED
                 ) -> "SkillOutcome | None":
        """One outcome, for one skill, in one category.

        The category argument is not optional in spirit: a skill now has an
        outcome per category, and a lookup that ignored the category would
        answer with whichever came first — the defect a duplicate skill caused
        in review round 1, re-introduced through a different door.
        """
        for o in self.outcomes:
            if o.skill_id == skill_id and o.category == category:
                return o
        return None

    def for_skill(self, skill_id: str) -> list:
        """Every category's outcome for one skill, in emission order."""
        return [o for o in self.outcomes if o.skill_id == skill_id]

    def category_census(self) -> dict:
        """Outcomes per category. Separate counts, never one ranking."""
        out: dict = {c: {o: 0 for o in OUTCOMES} for c in CATEGORIES}
        for o in self.outcomes:
            out.setdefault(o.category, {o: 0 for o in OUTCOMES})
            out[o.category][o.outcome] = out[o.category].get(o.outcome, 0) + 1
        return out

    def census(self) -> dict:
        out = {o: 0 for o in OUTCOMES}
        for o in self.outcomes:
            out[o.outcome] = out.get(o.outcome, 0) + 1
        return out

    def declaration_state_census(self) -> dict:
        out = {s: 0 for s in DECLARATION_STATES}
        for o in self.outcomes:
            out[o.declaration_state] = out.get(o.declaration_state, 0) + 1
        return out

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {"request": self.request.as_dict(),
                "assembled_at": self.assembled_at,
                "library_content_id": self.library_content_id,
                "body_chars": len(self.body), "body_sha256": self.body_sha256,
                "outcome_census": self.census(),
                "category_census": self.category_census(),
                "declaration_state_census": self.declaration_state_census(),
                "delivered_categories": list(self.request.deliver_categories),
                "_categories_are_not_a_ranking": (
                    "each category records its own source, version, "
                    "confirmation status and verbatim evidence. They are not "
                    "merged into one weighted list, nothing ranks one above "
                    "another, and emission order carries no authority."),
                "fixture_attestations_accepted":
                    self.request.accept_fixture_attestations,
                "_fixture_disclosure": (
                    "this assembly ACCEPTED fixture manifests, so no delivery "
                    "in it rests on a real author confirmation"
                    if self.request.accept_fixture_attestations else
                    "fixture manifests were refused; every delivered skill "
                    "carries a real author confirmation"),
                "outcomes": [o.as_dict() for o in self.outcomes],
                "_this_is_a_claim": (
                    "an assembly is what the container intends to send. It is "
                    "NOT evidence of delivery. Only cf/container_receipt.py, "
                    "reading the payload that actually reached the send "
                    "boundary, can report delivery — and only as far as that "
                    "boundary.")}


# ---------------------------------------------------------------- assembly --
def _version_check(lib: Library, req: SkillRequest, source_root: Path) -> dict:
    """Two independent version checks. Both must pass.

    The declared version is a *label* the host and the build agree on; the file
    hashes are the *bytes*. A build labelled `v1` whose file later changed
    satisfies the label and fails the bytes, and only checking both catches it.
    """
    binding = lib.skills.get(req.skill_id)
    if binding is None:
        return {"ok": False, "reason": "no binding in the library"}
    if binding.source_version != req.source_version:
        return {"ok": False, "reason": "declared version differs",
                "requested": req.source_version,
                "library_holds": binding.source_version}
    if not binding.files:
        return {"ok": False,
                "reason": "the binding carries no per-file hash, so the "
                          "requested version cannot be re-verified against "
                          "bytes on disk",
                "unverifiable": True}
    drifted = []
    for f in binding.files:
        p = Path(source_root) / f.rel_path
        if not p.is_file():
            drifted.append({"rel_path": f.rel_path, "reason": "file missing",
                            "built_sha256": f.content_sha256,
                            "current_sha256": None})
            continue
        now = file_digest(p)
        if now != f.content_sha256:
            drifted.append({"rel_path": f.rel_path,
                            "reason": "content changed since build",
                            "built_sha256": f.content_sha256,
                            "current_sha256": now})
    if drifted:
        return {"ok": False, "reason": "source bytes moved since the build",
                "changed_files": drifted}
    return {"ok": True, "source_version": binding.source_version,
            "files": len(binding.files)}


#: The two categories Phase 1 **reserves an interface for and does not
#: collect**. Owner direction 2026-10-16: "运行时提取和法规采集先预留接口,
#: 不因此启动新的工程线."
#:
#: So the container will *carry* these if a host hands them in — carrying is
#: its job — and there is no extractor, no scraper and no scheduler for either.
#: The absence is recorded here rather than left to be inferred from a missing
#: file, because "no collector exists" and "the collector found nothing" are
#: different sentences and only one of them is true.
RESERVED_COLLECTORS = {
    CAT_RUNTIME_CANDIDATE: (
        "INTERFACE RESERVED, NO COLLECTOR. `CarriedItem` accepts runtime task "
        "candidates from a host. Nothing in Phase 1 derives them from a task: "
        "there is no runtime extractor, and a runtime candidate reaching the "
        "container therefore came from the host, not from us."),
    CAT_REGULATORY_SOURCE: (
        "INTERFACE RESERVED, NO COLLECTOR. `CarriedItem` accepts regulatory "
        "source text from a host. Nothing in Phase 1 fetches, parses or "
        "updates a statute, and nothing judges applicability."),
}


@dataclass(frozen=True)
class CarriedItem:
    """One item the **host** hands to the container, with its provenance.

    The reserved input path for `runtime_candidate` and `regulatory_source`
    (see `RESERVED_COLLECTORS`), and usable for any category. Every field that
    makes the item checkable is required, and the ones that would let it
    impersonate a confirmed declaration are refused:

    * `verbatim_evidence` — the exact source excerpt. Mandatory for every
      category that is not author-confirmed, because a candidate is somebody's
      *reading* and the reader has to be able to check it.
    * `confirmation_status` — may only be `confirmed_by_author` for the
      author-confirmed category. A runtime or regulatory item claiming author
      confirmation is refused at construction, not filtered later.
    """

    item_id: str
    category: str
    #: Where it came from: a file path, a turn id, a statute section.
    source_ref: str
    source_version: str
    text: str
    verbatim_evidence: str = ""
    #: How to find the evidence in the source: `file#start-end`, `turn-3#0-18`,
    #: `GDPR Art.5(1)(c)`.
    evidence_locator: str = ""
    confirmation_status: str = CONF_UNCONFIRMED

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ContainerError(
                f"{self.item_id}: unknown category {self.category!r}; "
                f"expected one of {list(CATEGORIES)}")
        if self.confirmation_status not in CONFIRMATION_STATES:
            raise ContainerError(
                f"{self.item_id}: confirmation_status "
                f"{self.confirmation_status!r} is not one of "
                f"{list(CONFIRMATION_STATES)}")
        if (self.confirmation_status == CONF_BY_AUTHOR
                and self.category != CAT_AUTHOR_CONFIRMED):
            raise ContainerError(
                f"{self.item_id}: a {self.category} item may not claim "
                f"{CONF_BY_AUTHOR}. Author confirmation is evidenced by a "
                "manifest against a skill's own source, and a runtime or "
                "regulatory item has no such author. Refused at construction "
                "rather than filtered later, so the claim cannot exist.")
        if self.category in CATEGORIES_REQUIRING_EVIDENCE \
                and not self.verbatim_evidence.strip():
            raise ContainerError(
                f"{self.item_id}: a {self.category} item must carry its "
                "verbatim source excerpt. It is a reading of a source, and a "
                "reading delivered without its source is a reading presented "
                "as a fact.")
        if self.category in CATEGORIES_REQUIRING_EVIDENCE \
                and not self.evidence_locator.strip():
            raise ContainerError(
                f"{self.item_id}: a {self.category} item must say where its "
                "evidence is (file#span, turn#span, statute section), or "
                "nobody can go and check it.")
        if not self.source_ref.strip() or not self.source_version.strip():
            raise ContainerError(
                f"{self.item_id}: source_ref and source_version are required; "
                "an item whose origin and version are unknown cannot be "
                "re-verified by anyone.")

    def as_dict(self) -> dict:
        return {"item_id": self.item_id, "category": self.category,
                "source_ref": self.source_ref,
                "source_version": self.source_version,
                "text": self.text,
                "verbatim_evidence": self.verbatim_evidence,
                "evidence_locator": self.evidence_locator,
                "confirmation_status": self.confirmation_status,
                "_category_does_not_mean": CATEGORY_NOTES[self.category],
                "_collector": RESERVED_COLLECTORS.get(
                    self.category, "collected by this phase's own build")}


def _candidate_outcome(req: SkillRequest, entries: dict, unconfirmed: list,
                       state: str, att_view: dict, *, delivered: bool
                       ) -> SkillOutcome:
    """The MACHINE_CANDIDATE outcome for one skill.

    `delivered=False` — the host did not ask for the category — is reported as
    `FAILED` / `NOT_AUTHOR_CONFIRMED` rather than as success-with-nothing: the
    items exist, they were collected, and they were **not** sent. Calling that
    `AVAILABLE_COMPLETE` would make "nothing was delivered" and "nothing
    exists" the same sentence, which is the confusion this whole container
    exists to prevent.
    """
    texts = {i: entries[i].source_text for i in unconfirmed}
    broken = [i for i in unconfirmed
              if not entries[i].verify_source_hash()
              or OPEN_TAG in entries[i].source_text
              or CLOSE_TAG in entries[i].source_text]
    if broken:
        return SkillOutcome(
            skill_id=req.skill_id, requested_version=req.source_version,
            category=CAT_MACHINE_CANDIDATE,
            confirmation_status=CONF_UNCONFIRMED,
            outcome=FAILED, declaration_state=state,
            failure_reason=UNREADABLE,
            detail={"reason": "a candidate failed its own span hash or would "
                              "make the envelope ambiguous",
                    "candidates_affected": broken},
            declared_count=len(unconfirmed), attestation=att_view)
    if not delivered:
        return SkillOutcome(
            skill_id=req.skill_id, requested_version=req.source_version,
            category=CAT_MACHINE_CANDIDATE,
            confirmation_status=CONF_UNCONFIRMED,
            outcome=FAILED, declaration_state=state,
            failure_reason=NOT_AUTHOR_CONFIRMED,
            detail={"reason": "this skill holds machine-extracted candidates "
                              "that nobody confirmed, and the request did not "
                              "enable the machine_candidate category. They "
                              "were COLLECTED and NOT DELIVERED — which is "
                              "not the same as there being none.",
                    "candidates_withheld": sorted(unconfirmed),
                    "to_deliver_them": "add CAT_MACHINE_CANDIDATE to "
                                       "ContainerRequest.deliver_categories; "
                                       "they will travel labelled "
                                       "machine-extracted, unconfirmed, with "
                                       "their verbatim source beside them"},
            declared_count=len(unconfirmed), attestation=att_view)
    return SkillOutcome(
        skill_id=req.skill_id, requested_version=req.source_version,
        category=CAT_MACHINE_CANDIDATE,
        confirmation_status=CONF_UNCONFIRMED,
        outcome=AVAILABLE_COMPLETE, declaration_state=state,
        detail={"reason": "every machine-extracted candidate for this skill, "
                          "labelled unconfirmed and carrying its verbatim "
                          "source"},
        delivered_ids=tuple(unconfirmed),
        delivered_texts=dict(texts), evidence_texts=dict(texts),
        declared_count=len(unconfirmed), attestation=att_view)


def envelope_header(skill_id: str, source_version: str, att: Attestation,
                    count: int, category: str = CAT_AUTHOR_CONFIRMED,
                    confirmation: str = CONF_BY_AUTHOR) -> str:
    """The envelope's opening tag. **One formatter, two independent callers.**

    `render_block` calls it with the values it is about to emit;
    `constraintrelay.container_receipt` calls it with values re-derived from the library and
    the manifest, then requires the result verbatim in the payload. Sharing the
    *format* while deriving the *values* twice is the point: a review forged
    `real-author-confirmation="true"`, `confirmation-method="signed_git_commit"`
    and `source-version="synthetic-v9"` into a payload for a v1 fixture and the
    receipt passed it, because it only ever checked declaration text and the
    `declarations=` count. A payload that can lie about who confirmed it is the
    acceptance question failing.
    """
    confirm = (f"fixture:{att.confirmed_by}" if not
               att.real_author_confirmation else att.confirmed_by)
    if category != CAT_AUTHOR_CONFIRMED:
        # A candidate has no confirming author, so the author fields would be
        # a lie if they were carried over. They are replaced, not blanked:
        # "(none — machine-extracted)" cannot be misread as an absent value.
        confirm = "(none — machine-extracted, unconfirmed)"
    return (f'{OPEN_TAG} skill="{skill_id}" source-version="{source_version}" '
            f'category="{category}" confirmation="{confirmation}" '
            f'declarations="{count}" complete="true" '
            f'author-confirmed-by="{confirm}" '
            f'confirmation-method='
            f'"{att.confirmation_method if category == CAT_AUTHOR_CONFIRMED else "none"}" '
            f'real-author-confirmation='
            f'"{str(att.real_author_confirmation and category == CAT_AUTHOR_CONFIRMED).lower()}">')


def render_block(skill_id: str, source_version: str, att: Attestation,
                 items: list, category: str = CAT_AUTHOR_CONFIRMED,
                 evidence: "dict | None" = None) -> str:
    """One skill's complete block. *items* is `[(id, text)]` in emission order.

    The declaration text is emitted **byte-for-byte** as the library recorded
    it, including its original line wrapping. Re-wrapping would be a silent
    edit, and it would also break the receipt's verbatim search — which is the
    property that makes the receipt worth anything.
    """
    confirmation = (CONF_BY_AUTHOR if category == CAT_AUTHOR_CONFIRMED
                    else CONF_UNCONFIRMED)
    # 0.2 outer trim: the non-elevation notice and the category note used to
    # be repeated in EVERY block — 1592 + 714 of 5034 capsule chars for three
    # skills, against 497 chars of actual declaration text. They are now
    # stated once in the capsule header. What stays in the envelope is exactly
    # what the model needs to identify the material: skill, source version,
    # category, confirmation status and the author fields. The detailed audit
    # record lives in the receipt, which is where it belongs.
    #
    # No declaration text was touched by this trim.
    head = envelope_header(skill_id, source_version, att, len(items),
                           category, confirmation)
    lines = [head]
    if not items:
        lines.append("The author's confirmed manifest for this skill declares "
                     "NO constraints. This is the author's statement that "
                     "there are none — it is not a failure to find any.")
    needs_evidence = category in CATEGORIES_REQUIRING_EVIDENCE
    for did, text in items:
        lines.append(f"[{did}]"
                     if not needs_evidence
                     else f"[{did}] (machine-extracted, unconfirmed)")
        lines.append(text)
        if needs_evidence:
            # The verbatim source excerpt travels WITH the candidate, so the
            # reader can check the reading rather than take it on trust.
            excerpt = (evidence or {}).get(did, "")
            lines.append("    verbatim source excerpt:")
            lines.append(excerpt)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    lines.append(CLOSE_TAG)
    return "\n".join(lines)


def assemble(lib: Library, request: ContainerRequest, *, source_root: Path,
             attestations: dict) -> Assembly:
    """Build the capsule body and one outcome per requested skill.

    Order of checks per skill, each ending the skill when it fails:

    1. **manifest present** — absent means `UNDECLARED`, never "no rules";
    2. **version** — both the label and the bytes (`_version_check`);
    3. **declaration set agrees** — every manifest id resolves to a library
       entry and every library entry for the skill is named by the manifest. A
       mismatch either way fails the whole skill;
    4. **seal** — the recorded text hashes to what the author confirmed;
    5. **framing** — no declaration text may contain an envelope delimiter;
    6. **capacity** — the complete block fits what is left. Never truncated.

    Nothing is filtered by tool, action tag, condition or review tier.
    """
    body_parts: list = []
    outcomes: list = []
    remaining = request.capacity_chars
    capacity_closed = False

    for req in request.skills:
        att = attestations.get(req.skill_id)
        if att is None:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=UNDECLARED, declaration_state=NOT_DECLARED,
                detail={"reason": f"no {ATTESTATION_FILE} for this skill. "
                                  "Nothing here says the skill has no "
                                  "constraints — only that nobody declared."}))
            continue

        if isinstance(att, ManifestError):
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=DECLARATION_UNREADABLE,
                failure_reason=UNREADABLE,
                detail={"reason": "a manifest exists for this skill and could "
                                  "not be read, so what the author declared is "
                                  "unknown. This is NOT 'undeclared' — "
                                  "somebody declared and the declaration "
                                  "cannot be read.",
                        # Under its own key, not splatted: `**att.as_dict()`
                        # carries a `reason` of its own and silently replaced
                        # the sentence above with the parse error. Caught by
                        # the harness check that reads this text.
                        "manifest_error": att.as_dict()}))
            continue

        att_view = att.as_dict()
        state = (DECLARED_EMPTY if not att.declarations else DECLARED_NONEMPTY)

        # A readable draft awaiting its author. Reported as pending and NOT
        # delivered — Owner direction 2026-10-16. The declarations are known
        # and quotable; they are simply not anybody's word yet, and calling
        # that UNREADABLE sent the reader hunting for corruption instead of
        # for a confirmation.
        if att.pending:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED,
                declaration_state=DECLARED_PENDING_CONFIRMATION,
                confirmation_status=CONF_UNCONFIRMED,
                failure_reason=PENDING_CONFIRMATION,
                detail={"reason": "this manifest reads cleanly and names its "
                                  "declarations; its author has not adopted "
                                  "it. PENDING CONFIRMATION, NOT DELIVERED — "
                                  "the draft is usable, it is simply not yet "
                                  "anybody's word.",
                        "declarations_pending": sorted(att.ids),
                        "what_is_needed": "the author decides whether to "
                                          "adopt this text, then records the "
                                          "method and where that decision can "
                                          "be found. Filling the fields is "
                                          "the RECORD of that act, not "
                                          "evidence of it.",
                        "confirmation_basis_machine_verified": False},
                declared_count=len(att.declarations), attestation=att_view))
            continue

        # The manifest must say it is about THIS skill. Keying the dict by
        # directory is not a binding: a review put `skill_id:
        # "skills/TOTALLY-OTHER"` in alpha-report's manifest and the container
        # delivered it as AVAILABLE_COMPLETE, because nothing ever compared the
        # field to the request.
        if att.skill_id != req.skill_id:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=UNREADABLE,
                detail={"reason": "the manifest found for this skill declares "
                                  "a different skill_id, so it is not a "
                                  "confirmation of what was requested",
                        "manifest_skill_id": att.skill_id,
                        "requested_skill_id": req.skill_id},
                declared_count=len(att.declarations), attestation=att_view))
            continue

        # A fixture is not a confirmation. The Owner permits a labelled fixture
        # for engineering tests and requires real confirmation for formal
        # delivery, so the permission is an explicit argument on the request —
        # recorded, and off by default. Without it a fixture reached normal
        # delivery indistinguishably from a real confirmation at the gate.
        if not att.real_author_confirmation \
                and not request.accept_fixture_attestations:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=NOT_AUTHOR_CONFIRMED,
                detail={"reason": "this manifest is not a real author "
                                  "confirmation, and the request did not set "
                                  "accept_fixture_attestations. A labelled "
                                  "fixture may be used for engineering tests "
                                  "only, and that has to be asked for.",
                        "confirmation_method": att.confirmation_method,
                        "fixture": att.fixture,
                        "real_author_confirmation":
                            att.real_author_confirmation},
                declared_count=len(att.declarations), attestation=att_view))
            continue

        if att.source_version != req.source_version:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=VERSION_MISMATCH,
                detail={"reason": "the author's manifest confirms a different "
                                  "version",
                        "manifest_version": att.source_version,
                        "requested": req.source_version},
                declared_count=len(att.declarations), attestation=att_view))
            continue

        ver = _version_check(lib, req, source_root)
        if not ver["ok"]:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=VERSION_MISMATCH, detail=ver,
                declared_count=len(att.declarations), attestation=att_view))
            continue

        entries = {e.constraint_id: e
                   for e in lib.entries if e.skill_id == req.skill_id}
        manifest_ids, entry_ids = att.ids, frozenset(entries)

        # Entries the manifest does not confirm are MACHINE CANDIDATES — a
        # separate category, not a hole in the confirmed set.
        #
        # This replaces the earlier behaviour, where any unconfirmed entry
        # failed the WHOLE skill with NOT_AUTHOR_CONFIRMED. That was right
        # while the container had one category: the confirmed set and the
        # extracted set were being mixed, so a partial send was a real risk.
        # Owner direction 2026-10-16 separates them — "提取结果默认是候选",
        # collection is not delivery — so the confirmed set is complete on its
        # own, and candidates travel only when the host asks, labelled, with
        # their verbatim source beside them.
        unconfirmed = sorted(entry_ids - manifest_ids)
        if unconfirmed:
            cand_outcome = _candidate_outcome(
                req, entries, unconfirmed, state, att_view,
                delivered=request.delivers(CAT_MACHINE_CANDIDATE))
            if cand_outcome.outcome == AVAILABLE_COMPLETE:
                block = render_block(
                    req.skill_id, req.source_version, att,
                    [(i, entries[i].source_text) for i in unconfirmed],
                    category=CAT_MACHINE_CANDIDATE,
                    evidence={i: entries[i].source_text for i in unconfirmed})
                cost = len(block) + (1 if body_parts else 0)
                if remaining is not None and (capacity_closed
                                              or cost > remaining):
                    capacity_closed = True
                    cand_outcome = SkillOutcome(
                        skill_id=req.skill_id,
                        requested_version=req.source_version,
                        category=CAT_MACHINE_CANDIDATE,
                        confirmation_status=CONF_UNCONFIRMED,
                        outcome=FAILED, declaration_state=state,
                        failure_reason=CAPACITY_EXCEEDED,
                        detail={"reason": "the complete candidate block does "
                                          "not fit the capacity left; nothing "
                                          "of it was sent",
                                "block_chars": len(block),
                                "chars_remaining": remaining},
                        declared_count=len(unconfirmed),
                        block_chars=len(block), attestation=att_view)
                else:
                    if remaining is not None:
                        remaining -= cost
                    body_parts.append(block)
                    cand_outcome.block_chars = len(block)
            outcomes.append(cand_outcome)

        missing = sorted(manifest_ids - entry_ids)
        if missing:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=UNREADABLE,
                detail={"reason": "the manifest confirms declarations that no "
                                  "library entry resolves, so the confirmed "
                                  "set cannot be produced whole",
                        "unresolved_declarations": missing},
                declared_count=len(att.declarations), attestation=att_view))
            continue

        items, evidence, broken = [], [], []
        for d in att.declarations:
            e = entries[d.declaration_id]
            seal_ok = (sha256_text(e.source_text) == d.declared_text_sha256)
            span_ok = e.verify_source_hash()
            evidence.append({
                "declaration_id": d.declaration_id,
                "seal_ok": seal_ok,
                "manifest_ok": True,
                "span_self_hash_ok": span_ok,
                "declared_text_sha256": d.declared_text_sha256,
                "recomputed_text_sha256": sha256_text(e.source_text),
                "_seal_is_not_authorship": (
                    "seal_ok proves the text did not move. It does not prove "
                    "who wrote it or that they confirmed it; manifest_ok and "
                    "the attestation block carry that, separately.")})
            if not (seal_ok and span_ok):
                broken.append(d.declaration_id)
                continue
            if OPEN_TAG in e.source_text or CLOSE_TAG in e.source_text:
                broken.append(d.declaration_id)
                evidence[-1]["framing_conflict"] = True
                continue
            items.append((d.declaration_id, e.source_text))

        if broken:
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=UNREADABLE,
                detail={"reason": "a confirmed declaration failed its seal, "
                                  "its own span hash, or would make the "
                                  "envelope ambiguous",
                        "declarations_affected": broken},
                declared_count=len(att.declarations), attestation=att_view,
                evidence=evidence))
            continue

        block = render_block(req.skill_id, req.source_version, att, items)
        cost = len(block) + (1 if body_parts else 0)
        if remaining is not None and (capacity_closed or cost > remaining):
            capacity_closed = True
            outcomes.append(SkillOutcome(
                skill_id=req.skill_id, requested_version=req.source_version,
                outcome=FAILED, declaration_state=state,
                failure_reason=CAPACITY_EXCEEDED,
                detail={"reason": "the complete block does not fit the "
                                  "capacity left. It was NOT truncated and NOT "
                                  "summarised; nothing of this skill was sent. "
                                  "Capacity is consumed in the host's declared "
                                  "order and is not re-packed, so every skill "
                                  "after the first overflow fails too.",
                        "block_chars": len(block),
                        "chars_remaining": remaining,
                        "capacity_chars": request.capacity_chars},
                declared_count=len(att.declarations), block_chars=len(block),
                attestation=att_view, evidence=evidence))
            continue

        if remaining is not None:
            remaining -= cost
        body_parts.append(block)
        outcomes.append(SkillOutcome(
            skill_id=req.skill_id, requested_version=req.source_version,
            outcome=AVAILABLE_COMPLETE, declaration_state=state,
            detail={"source_version": req.source_version,
                    "files_verified": ver.get("files", 0)},
            delivered_ids=tuple(i for i, _ in items),
            delivered_texts={i: t for i, t in items},
            declared_count=len(att.declarations), block_chars=len(block),
            attestation=att_view, evidence=evidence))

    return Assembly(request=request, body="\n".join(body_parts),
                    outcomes=outcomes, assembled_at=_now(),
                    library_content_id=lib.library_content_id())


# ----------------------------------------------------------------- capsule --
#: Marks the container's own message in a payload. One discrete message, so
#: "did it arrive" is answered by inspecting the request rather than by
#: searching prose.
CAPSULE_MARKER = "<!-- constraintrelay:declarations -->"


def render_capsule(assembly: Assembly) -> str:
    """The whole container message: an outcome summary, then the blocks.

    The summary names every requested skill and what happened to it, because a
    reader handed three of four skills and no notice would reasonably conclude
    it had everything. It carries **no declaration id and no declaration text**
    for a skill that was not delivered — "nothing of that skill was sent" has to
    stay literally true, including here.
    """
    lines = [CAPSULE_MARKER,
             "## ConstraintRelay — author-declared constraints for this call",
             f"request: {assembly.request.request_id}",
             f"library: {assembly.library_content_id}",
             "",
             NON_ELEVATION_NOTICE,
             "",
             ORDER_IS_NOT_AUTHORITY,
             "",
             # Once per capsule, for the categories actually present, instead
             # of once per block. Same information, read in the same place.
             *[f"[{c}] {CATEGORY_NOTES[c]}"
               for c in CATEGORIES
               if any(o.category == c and o.outcome == AVAILABLE_COMPLETE
                      for o in assembly.outcomes)],
             "",
             f"### Per-skill outcome, by category "
             f"(delivering: {', '.join(assembly.request.deliver_categories)})"]
    for o in assembly.outcomes:
        tag = f"{o.skill_id} [{o.category}]"
        if o.outcome == AVAILABLE_COMPLETE:
            lines.append(f"- {tag}: AVAILABLE_COMPLETE "
                         f"({o.declaration_state}, {o.confirmation_status}, "
                         f"{len(o.delivered_ids)} item(s) below)")
        elif o.outcome == UNDECLARED:
            lines.append(f"- {tag}: UNDECLARED — nobody declared for "
                         f"this skill. This is NOT a statement that it has no "
                         f"constraints.")
        elif o.failure_reason == NOT_AUTHOR_CONFIRMED:
            lines.append(f"- {tag}: FAILED (NOT_AUTHOR_CONFIRMED) — "
                         f"{o.declared_count} item(s) of this kind were "
                         f"COLLECTED and NOT DELIVERED, because this request "
                         f"does not enable the category. That is not a "
                         f"statement that there are none.")
        else:
            lines.append(f"- {tag}: FAILED ({o.failure_reason}) — "
                         f"nothing of this skill's {o.category} was sent. "
                         f"Not summarised, not truncated.")
    if assembly.body:
        lines.extend(["", assembly.body])
    return "\n".join(lines) + "\n"


# `OutboundPayload` and `DeclarationInjector` used to live here. They are
# host-shaped — they know about `stream_chat`, about message roles and about a
# model client — and 0.2 requires the core to know about none of that. They now
# live in `cf/container_adapter.py` as `StreamChatAdapter`, behind an
# `AdapterConfig` that has no default role and no default position.
#
# `constraintrelay.container_adapter` imports `CAPSULE_MARKER` and `render_capsule` from
# here; nothing here imports it back. `tests/test_container_phase1.py` asserts
# this file contains no reference to `stream_chat`.
