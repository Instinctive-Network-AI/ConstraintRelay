"""Whose load was it? The question a module-level load observer cannot answer.

Owner direction 2026-10-21, item 1. The hermes adapter observes the host's load
boundary by replacing a **module attribute** —
`tools.skills_tool.skill_view` — which is what makes one wrapper cover the
tool, the slash command and the CLI preload at once. The same property is a
hazard: a second agent in the process loads through that same module
attribute, so its loads land in **this** record. `v2/HERMES_ACCEPTANCE.md`
recorded that as an attribution hazard and did not rely on it; this module is
the part that stops relying on it being absent.

The rule, and the direction it fails in
---------------------------------------
A load is delivered for **only** when it can be attributed to the agent this
container is delivering into. Anything else is `ATTRIBUTION_UNVERIFIED`: the
load contributes **no declaration**, the receipt says `load attribution not
verified`, and the receipt may not report the request verified. The host keeps
running — nothing here refuses, delays or alters a host call; an observer that
can break a load is not an observer.

Why a construction count is not the detector
--------------------------------------------
"A second agent was constructed" is a **conservative trigger**: it can say the
process stopped being single-agent, and it is used for exactly that. It cannot
say *which* agent made a given load, and a detector built on it alone would
answer `OWNED` for every load right up to the moment a child is constructed —
including the child's own loads if it is constructed first. So the decision
needs a signal that is about **the load**, and that is the call stack:

| signal | what it establishes | direction it fails in |
|---|---|---|
| `OWNER_ON_STACK` | the owned agent's own frame is in the call chain of this load | absent ⇒ no positive attribution, never ownership |
| `FOREIGN_ON_STACK` | some *other* agent-shaped object's frame is | present ⇒ `FOREIGN`, which is a refusal |
| `SOLE_AGENT` | no agent but the owner exists in this process | a peer construction, live or dead, removes it — and so does an **agent that already existed when construction observation was installed**, because an observer cannot report what happened before it |
| `OWNER_THREAD` | the load ran on the thread the owner was bound on | a different thread removes it |

`OWNED` needs either the owner's own frame (direct evidence), or **both** of
the two eliminating signals together (`SOLE_AGENT` and `OWNER_THREAD`) — two
independent facts, neither of which is the construction trigger by itself.
Everything else is `UNVERIFIED` with the reason named.

**One signal considered and rejected:** hermes passes a `task_id` into
`skill_view` and forwards it to its preprocessor, so a load carries a
host-supplied token. It is recorded (`LoadedSkill.session_id`) and is **not**
an attribution signal, because there is nothing on the agent to compare it
with: hermes takes `task_id` as an argument to `run_conversation` and does not
store it on the agent, so "the owner's current token" does not exist to match
against. A comparison against a value this module invented would be a check
that cannot fail.

Weak references, not ids
------------------------
A candidate frame's `self` is held as a `weakref`, never as an `id()`. An id
compared after the object died is a comparison against whatever now occupies
that address; a dead weakref simply is not the live owner, which is the answer
this module needs. Signals are **captured at load time** and **decided at
resolution time**, because the owner is bound after the host has already run
its CLI preload — deciding at load time would refuse the host's own preload.
"""

from __future__ import annotations

import sys
import threading
import weakref

#: The load is this agent's. Delivered for.
OWNED = "OWNED"
#: The load is positively another agent's. Not delivered for.
FOREIGN = "ATTRIBUTED_TO_ANOTHER_AGENT"
#: Could not be established either way. Not delivered for, and said so.
UNVERIFIED = "ATTRIBUTION_UNVERIFIED"
STATUSES = (OWNED, FOREIGN, UNVERIFIED)
#: Everything but `OWNED` withholds delivery. Spelled out so a new status has
#: to be classified deliberately rather than defaulting to deliverable.
WITHHOLDING = tuple(s for s in STATUSES if s != OWNED)

#: Signal names, so a receipt reader sees which fact carried the decision.
SIG_OWNER_ON_STACK = "OWNER_ON_STACK"
SIG_FOREIGN_ON_STACK = "FOREIGN_ON_STACK"
SIG_SOLE_AGENT = "SOLE_AGENT_IN_PROCESS"
SIG_OWNER_THREAD = "LOAD_RAN_ON_OWNER_THREAD"
SIGNALS = (SIG_OWNER_ON_STACK, SIG_FOREIGN_ON_STACK, SIG_SOLE_AGENT,
           SIG_OWNER_THREAD)

#: Reasons an observation resolves to `UNVERIFIED`. Named rather than a free
#: string so the suite can require each one to be *reached*.
NO_OWNER_BOUND = "NO_OWNER_BOUND"
PEER_AGENT_IN_PROCESS = "PEER_AGENT_CONSTRUCTED_AND_OWNER_NOT_ON_STACK"
PRE_EXISTING_AGENTS = "AGENTS_ALREADY_EXISTED_WHEN_OBSERVATION_STARTED"
OTHER_THREAD = "LOAD_RAN_ON_ANOTHER_THREAD_AND_OWNER_NOT_ON_STACK"
NO_SIGNAL = "NO_ATTRIBUTING_SIGNAL"
UNVERIFIED_REASONS = (NO_OWNER_BOUND, PEER_AGENT_IN_PROCESS,
                      PRE_EXISTING_AGENTS, OTHER_THREAD, NO_SIGNAL)

#: How deep the stack walk goes. A bound, because an observer must not become
#: the expensive part of a host's load, and because a frame 200 deep is not
#: evidence anybody would accept anyway. Reaching the bound is recorded.
MAX_FRAMES = 60


class AttributionError(Exception):
    """This guard must not run as configured."""


def _is_agent_shaped(obj, markers: tuple) -> bool:
    """Does *obj* carry every attribute that marks a host agent?

    Duck-typed on the attributes the **adapter** wraps, handed in by the host
    binding rather than chosen here: this module imports no host code, and a
    detector keyed on a class name would miss a subclass and match an
    unrelated class of the same name.
    """
    if obj is None or isinstance(obj, (str, bytes, int, float, bool, dict,
                                       list, tuple, set)):
        return False
    try:
        return all(hasattr(obj, m) for m in markers)
    except Exception:                                        # noqa: BLE001
        return False


class LoadObservation:
    """The signals present when one load happened. Decided later.

    Holds no verdict: `AttributionGuard.decide()` turns it into one, against
    the owner **as bound at resolution time**. That split is what lets the
    host's own CLI preload — which runs before anything could have bound an
    owner — still be attributed to the agent that was being constructed.
    """

    __slots__ = ("candidates", "candidate_classes", "thread_ident",
                 "thread_name", "peer_constructions", "frames_walked",
                 "frame_bound_hit", "observer_error")

    def __init__(self, *, candidates, candidate_classes, thread_ident,
                 thread_name, peer_constructions, frames_walked,
                 frame_bound_hit, observer_error="") -> None:
        self.candidates = candidates            # list[weakref]
        self.candidate_classes = candidate_classes
        self.thread_ident = thread_ident
        self.thread_name = thread_name
        self.peer_constructions = peer_constructions
        self.frames_walked = frames_walked
        self.frame_bound_hit = frame_bound_hit
        self.observer_error = observer_error

    def as_dict(self) -> dict:
        return {"agent_shaped_frames": len(self.candidates),
                "agent_shaped_frame_classes": list(self.candidate_classes),
                "thread_name": self.thread_name,
                "peer_constructions_at_load": self.peer_constructions,
                "frames_walked": self.frames_walked,
                "frame_bound_hit": self.frame_bound_hit,
                "observer_error": self.observer_error}


class Attribution:
    """One load's verdict: a status, the signals, and why."""

    __slots__ = ("status", "signals", "reason", "detail", "observation")

    def __init__(self, status: str, *, signals=(), reason: str = "",
                 detail: str = "", observation=None) -> None:
        if status not in STATUSES:
            raise AttributionError(
                f"{status!r} is not an attribution status; the set is "
                f"{STATUSES} and a new one has to be classified against "
                "WITHHOLDING deliberately")
        self.status = status
        self.signals = tuple(signals)
        self.reason = reason
        self.detail = detail
        self.observation = observation

    @property
    def owned(self) -> bool:
        return self.status == OWNED

    @property
    def withholds_delivery(self) -> bool:
        return self.status in WITHHOLDING

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "owned": self.owned,
            "withholds_delivery": self.withholds_delivery,
            "signals": list(self.signals),
            "reason": self.reason,
            "detail": self.detail,
            "observation": (self.observation.as_dict()
                            if self.observation is not None else None),
            "_what_this_is": (
                "whether this load can be attributed to the agent the "
                "container is delivering into. Anything but "
                f"{OWNED!r} contributes no declaration and is disclosed in "
                "the receipt as load attribution NOT verified — never as "
                "verified, and never silently."),
        }


class AttributionGuard:
    """Captures attribution signals at load time; decides at resolution time.

    *agent_markers* are the attributes that mark a host agent object, handed
    in by the host binding. *sole_agent_claim* is not a parameter: solitude is
    **measured**, by counting constructions through `note_construction()`, and
    a guard that was never told about constructions cannot use that signal at
    all — `assume_sole_agent=False` is the only setting and there is no way to
    assert solitude by hand.
    """

    def __init__(self, *, agent_markers: tuple) -> None:
        if not agent_markers:
            raise AttributionError(
                "agent_markers must name at least one attribute that marks a "
                "host agent object. An empty marker tuple makes every object "
                "on the stack agent-shaped, which would attribute every load "
                "to the owner — a check that cannot fail.")
        self._markers = tuple(agent_markers)
        self._owner = None                      # weakref | None
        self._owner_thread = None
        #: Every agent-shaped construction this guard was told about, as
        #: `(weakref, class name)`. A dead entry still counts: a child agent
        #: that has been collected still loaded through the module attribute.
        self._constructions: list = []
        #: Whether construction observation is installed at all. Without it,
        #: `SOLE_AGENT` is unavailable rather than assumed true.
        self._construction_observed = False
        #: How many agent-shaped objects already existed when construction
        #: observation started. Non-zero makes `SOLE_AGENT` unavailable: an
        #: observer cannot report a construction that preceded it, so the
        #: count has to be **measured at install** rather than inferred from
        #: an empty construction list.
        self._pre_existing = None
        self.errors: list = []

    # -- binding ------------------------------------------------------------
    def bind_owner(self, agent) -> None:
        if self._owner is not None and self._owner() is not None:
            raise AttributionError("this guard already has an owner bound")
        try:
            self._owner = weakref.ref(agent)
        except TypeError as exc:
            raise AttributionError(
                f"the owner is not weak-referenceable ({exc}); this guard "
                "holds the owner weakly on purpose and will not fall back to "
                "an id(), which compares against whatever later occupies the "
                "address") from exc
        self._owner_thread = threading.get_ident()

    def owner(self):
        return self._owner() if self._owner is not None else None

    def enable_construction_observation(self, *, pre_existing: int) -> None:
        """Constructions are now being reported, and *pre_existing* already were.

        Called by the host binding when it installs its construction
        observer. Until then `SOLE_AGENT` is **unavailable**: a guard that
        nobody reports constructions to would otherwise see zero peers and
        read that as solitude.

        *pre_existing* has no default. It is the number of agent-shaped
        objects the host binding **counted** as already alive at install
        time, and it is required because that is the one thing an observer
        installed mid-process cannot otherwise know. Any non-zero value
        withdraws `SOLE_AGENT` for the whole run.
        """
        if not isinstance(pre_existing, int) or pre_existing < 0:
            raise AttributionError(
                "pre_existing must be a counted, non-negative integer — the "
                "number of agent-shaped objects alive when observation "
                "started. It has no default because that is precisely the "
                "fact an observer cannot infer after the fact.")
        self._construction_observed = True
        self._pre_existing = pre_existing

    def pre_existing_agents(self) -> "int | None":
        return self._pre_existing

    def note_construction(self, agent) -> None:
        """One agent-shaped object was constructed. Owner or peer, decided late.

        Never raises out of a host constructor: a failure here is recorded and
        makes the guard **more** conservative, because an unrecorded
        construction is exactly the case `peer_constructions` must not miss.
        """
        try:
            self._constructions.append((weakref.ref(agent),
                                        type(agent).__name__))
        except Exception as exc:                             # noqa: BLE001
            self.errors.append({"where": "note_construction",
                                "error": repr(exc)[:200]})
            # An unreferenceable construction is counted as an opaque peer
            # rather than dropped.
            self._constructions.append((None, type(agent).__name__))

    def peer_constructions(self) -> int:
        """Constructions that are not the bound owner. Dead ones included."""
        owner = self.owner()
        n = 0
        for ref, _name in self._constructions:
            obj = ref() if ref is not None else None
            if obj is not None and owner is not None and obj is owner:
                continue
            n += 1
        return n

    def sole_agent_available(self) -> bool:
        """Can the `SOLE_AGENT` elimination be used at all?

        Three conditions, and the middle one is the reason this is a method
        rather than `peer_constructions() == 0`: an observer installed after
        an agent already existed cannot report that agent, so solitude would
        read as true for a process that is not. The count is measured at
        install (`enable_construction_observation(pre_existing=...)`), which
        makes the installation order a **mechanical** precondition instead of
        a comment asking the integrator to install it early.
        """
        return (self._construction_observed
                and self._pre_existing == 0
                and self.peer_constructions() == 0)

    # -- observation --------------------------------------------------------
    def observe(self) -> LoadObservation:
        """Capture this load's signals. Called from inside the host's load.

        Walks the live stack for agent-shaped `self` bindings. Reads only; it
        never touches the host's values and it cannot raise — a failure is
        recorded on the observation, and an observation carrying an error can
        produce no positive attribution.
        """
        candidates: list = []
        classes: list = []
        walked, bound_hit, err = 0, False, ""
        try:
            frame = sys._getframe(1)
            seen: set = set()
            while frame is not None:
                if walked >= MAX_FRAMES:
                    bound_hit = True
                    break
                walked += 1
                obj = frame.f_locals.get("self")
                if _is_agent_shaped(obj, self._markers) and id(obj) not in seen:
                    seen.add(id(obj))
                    try:
                        candidates.append(weakref.ref(obj))
                        classes.append(type(obj).__name__)
                    except TypeError:
                        # Not weak-referenceable: recorded as an opaque
                        # agent-shaped frame, which can never match the owner
                        # and therefore cannot grant ownership.
                        candidates.append(None)
                        classes.append(type(obj).__name__)
                frame = frame.f_back
        except Exception as exc:                             # noqa: BLE001
            err = repr(exc)[:200]
            self.errors.append({"where": "observe", "error": err})
        return LoadObservation(
            candidates=candidates, candidate_classes=classes,
            thread_ident=threading.get_ident(),
            thread_name=threading.current_thread().name,
            peer_constructions=self.peer_constructions(),
            frames_walked=walked, frame_bound_hit=bound_hit,
            observer_error=err)

    # -- decision -----------------------------------------------------------
    def decide(self, obs) -> Attribution:
        """Turn one observation into a verdict against the bound owner."""
        if obs is None:
            return Attribution(
                UNVERIFIED, reason=NO_SIGNAL,
                detail="no attribution signals were captured for this load, "
                       "so nothing ties it to any agent. A load with no "
                       "observation is not the owner's by default.")
        owner = self.owner()
        if owner is None:
            return Attribution(
                UNVERIFIED, reason=NO_OWNER_BOUND,
                detail="no owner is bound to this guard (or the bound owner "
                       "has been collected), so there is nothing to attribute "
                       "the load TO.",
                observation=obs)

        live = [ref() for ref in obs.candidates if ref is not None]
        owner_on_stack = any(o is owner for o in live)
        foreign_on_stack = any(o is not None and o is not owner for o in live)
        opaque_frames = sum(1 for ref in obs.candidates if ref is None)
        # A frame whose `self` could not be weak-referenced, or that has since
        # been collected, is agent-shaped and unidentifiable. It cannot grant
        # ownership and it is not proof of a foreign agent either, so it only
        # removes the elimination route.
        unidentified = opaque_frames + sum(1 for ref in obs.candidates
                                           if ref is not None
                                           and ref() is None)

        signals = []
        if owner_on_stack:
            signals.append(SIG_OWNER_ON_STACK)
        if foreign_on_stack:
            signals.append(SIG_FOREIGN_ON_STACK)
        # Solitude is required both **at the load** and **now**: an agent
        # constructed after this load but before the resolution would leave
        # `obs.peer_constructions` at zero, and that later agent is exactly
        # the one whose loads this record may already be mixing in.
        sole = self.sole_agent_available() and obs.peer_constructions == 0
        if sole:
            signals.append(SIG_SOLE_AGENT)
        same_thread = (self._owner_thread is not None
                       and obs.thread_ident == self._owner_thread)
        if same_thread:
            signals.append(SIG_OWNER_THREAD)

        if foreign_on_stack and not owner_on_stack:
            return Attribution(
                FOREIGN, signals=signals, reason=SIG_FOREIGN_ON_STACK,
                detail="an agent-shaped frame that is NOT the owned agent is "
                       "in this load's call chain "
                       f"({', '.join(obs.candidate_classes)}), and the owner's "
                       "is not. The load is another agent's and contributes "
                       "no declaration here.",
                observation=obs)
        if obs.observer_error:
            return Attribution(
                UNVERIFIED, signals=signals, reason=NO_SIGNAL,
                detail="the attribution observation itself failed "
                       f"({obs.observer_error}), so its signals are not "
                       "trustworthy in either direction.",
                observation=obs)
        if owner_on_stack:
            return Attribution(
                OWNED, signals=signals, reason=SIG_OWNER_ON_STACK,
                detail="the owned agent's own frame is in this load's call "
                       "chain — direct evidence, not an elimination.",
                observation=obs)
        if unidentified:
            return Attribution(
                UNVERIFIED, signals=signals, reason=NO_SIGNAL,
                detail=f"{unidentified} agent-shaped frame(s) in this load's "
                       "call chain could not be identified (not "
                       "weak-referenceable, or already collected), so neither "
                       "ownership nor foreignness was established.",
                observation=obs)
        if sole and same_thread:
            return Attribution(
                OWNED, signals=signals,
                reason=f"{SIG_SOLE_AGENT}+{SIG_OWNER_THREAD}",
                detail="no agent-shaped frame was in the call chain — the "
                       "host's own preload runs that way — but no agent other "
                       "than the owner has been constructed in this process "
                       "and the load ran on the owner's thread. Two "
                       "independent eliminations; neither is the construction "
                       "trigger on its own.",
                observation=obs)
        if not sole:
            if not self._construction_observed:
                reason, detail = NO_SIGNAL, (
                    "construction observation is not installed, so solitude "
                    "is UNAVAILABLE rather than true: this guard cannot know "
                    "whether a second agent exists.")
            elif self._pre_existing:
                reason, detail = PRE_EXISTING_AGENTS, (
                    f"{self._pre_existing} agent-shaped object(s) already "
                    "existed when construction observation started, so this "
                    "process is not known to be single-agent and the "
                    "elimination route is withheld.")
            else:
                reason, detail = PEER_AGENT_IN_PROCESS, (
                    f"{obs.peer_constructions} agent(s) other than the owner "
                    "have been constructed in this process and the owner's "
                    "frame is not in this load's call chain, so this record "
                    "cannot be attributed.")
            return Attribution(UNVERIFIED, signals=signals, reason=reason,
                               detail=detail, observation=obs)
        return Attribution(
            UNVERIFIED, signals=signals, reason=OTHER_THREAD,
            detail="the load ran on thread "
                   f"{obs.thread_name!r}, not the thread the owner was bound "
                   "on, and the owner's frame is not in its call chain.",
            observation=obs)

    # -- reporting ----------------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "owner_bound": self.owner() is not None,
            "owner_class": (type(self.owner()).__name__
                            if self.owner() is not None else None),
            "construction_observation_installed": self._construction_observed,
            "pre_existing_agents_at_install": self._pre_existing,
            "constructions_seen": len(self._constructions),
            "peer_constructions": self.peer_constructions(),
            "sole_agent_available": self.sole_agent_available(),
            "agent_markers": list(self._markers),
            "guard_errors": list(self.errors),
            "_sole_agent_availability": (
                "measured: construction observation is installed, nothing "
                "pre-existed it, and no peer has been constructed"
                if self.sole_agent_available() else
                "UNAVAILABLE — the SOLE_AGENT signal is withheld rather than "
                "presumed true, because "
                + ("no construction observer is installed"
                   if not self._construction_observed else
                   f"{self._pre_existing} agent(s) pre-existed observation"
                   if self._pre_existing else
                   # The owner cannot be excluded from the peer count until
                   # it is bound, so before `bind_owner` the owner's OWN
                   # construction counts as a peer. Withholding is still the
                   # right answer — nothing has said which agent this guard
                   # serves — but reporting it as "N peer agent(s) exist" is
                   # false, and that false reason travelled into receipts.
                   "no owner is bound yet, so this guard cannot tell its own "
                   "agent's construction from a peer's: the signal is "
                   f"withheld, and the {self.peer_constructions()} "
                   "construction(s) seen are NOT established as peers"
                   if self.owner() is None else
                   f"{self.peer_constructions()} peer agent(s) exist")),
        }


def summarise(attributions: dict, *, loads_expected=None) -> dict:
    """`load key -> Attribution` to one block a receipt can carry.

    `established` is **False** when any load withholds delivery, and the whole
    point of the field is that it is never `True` by default: an empty mapping
    is `None` — not established — because a host that observed nothing has not
    shown that nothing happened.

    *loads_expected* is how many loads the caller actually saw. When it is
    given and disagrees with the number of rows here, `established` is forced
    to **None**: rows were lost on the way in — a key collision, in the case
    this argument exists for — and a summary that cannot account for every
    load may not report one. The discrepancy is stated in `_reconciliation`
    rather than left for a reader to notice by comparing two numbers.
    """
    rows = {k: v for k, v in (attributions or {}).items()}
    by_status: dict = {}
    for v in rows.values():
        by_status[v.status] = by_status.get(v.status, 0) + 1
    withheld = sorted(k for k, v in rows.items() if v.withholds_delivery)
    established = (None if not rows else not withheld)
    reconciled = None
    if loads_expected is not None:
        reconciled = (int(loads_expected) == len(rows))
        if not reconciled:
            established = None
    return {
        "established": established,
        "loads_considered": len(rows),
        "loads_expected": (None if loads_expected is None
                           else int(loads_expected)),
        "rows_account_for_every_load": reconciled,
        "_reconciliation": (
            "not checked: the caller did not say how many loads it saw"
            if reconciled is None else
            "every load the caller saw has exactly one attribution row"
            if reconciled else
            f"MISMATCH: the caller saw {int(loads_expected)} load(s) and this "
            f"summary holds {len(rows)} row(s), so at least one load is "
            "unaccounted for. `established` is forced to null: a summary that "
            "cannot account for every load may not report attribution."),
        "status_census": by_status,
        "withheld_from_delivery": withheld,
        "by_load": {k: v.as_dict() for k, v in sorted(rows.items())},
        "_established_null_means": (
            "no load was considered, so nothing was attributed and nothing is "
            "claimed. Null is not 'clear': a receipt carrying null does not "
            "establish that the loads behind it are this agent's."),
        "_established_false_means": (
            "at least one load could not be attributed to the agent this "
            "container delivers into. Those loads contributed NO declaration, "
            "and this receipt may not be read as verified."),
    }
