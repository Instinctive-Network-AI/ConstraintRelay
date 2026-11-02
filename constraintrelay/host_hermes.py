"""hermes-agent adapter: the host's own load state, and its own send boundary.

Second host, 2026-10-20. The container core is untouched by this file: nothing
here is imported by `cf/container.py`, `cf/container_receipt.py` or
`cf/container_hostrecord.py`, and this module imports no hermes code — the
caller owns that import, as the Vibe-Trading adapter does too, so
this file stays readable and testable without a hermes checkout.

Supported version
-----------------
`hermes-agent` **0.14.0** at git **`2517917de`**, driven from a separate
pinned checkout. See `SUPPORTED` below and `v2/HERMES_HOST.md`.

The two boundaries, named
-------------------------
**1. The load boundary — `tools.skills_tool.skill_view`.**

hermes has no `SkillsLoader` object holding a list of loaded skills the way
Vibe-Trading does. What it has is `skill_view(name, file_path=None, …)`, the
one function that reads a `SKILL.md` off disk and returns the host's own record
of that read: `name`, `path`, `skill_dir`, `content`, and `metadata` when the
frontmatter carries one. Every way hermes loads a skill goes through it:

* the `skill_view` **tool** the model calls (`tools/skills_tool.py:1535
  _skill_view_with_bump` → `skill_view`);
* a **slash command** (`agent/skill_commands.py:94`, inside
  `_load_skill_payload`);
* **CLI preload** (`build_preloaded_skills_prompt`, same helper).

Both call sites import the name from the module *at call time*, so observing
the module attribute observes all three. That is the reason this adapter wraps
`skill_view` rather than the three callers: a gate on the callers is a list of
the entry points somebody thought of, and this project has already paid five
review rounds for that shape.

**What `available_skills` is not.** hermes puts an index of every discovered
skill into the system prompt (`agent/prompt_builder.py:984
build_skills_system_prompt`) and `skills_list` returns the same set. That is a
list of what the host *could* load. Nothing in this module reads it, and no
declaration is delivered for a skill that only appears there — per Owner
direction 2026-10-20, delivery follows the load, not the catalogue.

**2. The send boundary — `AIAgent._interruptible_api_call` and
`AIAgent._interruptible_streaming_api_call`.**

The normal main loop (`agent/conversation_loop.py:675`) runs one iteration per
model decision — the next tool call, or the final answer. Each iteration builds
`api_kwargs` at `conversation_loop.py:1084` (`agent._build_api_kwargs`) and
hands it to one of those two methods at `:1172` / `:1176`. Both are wrapped,
because the host picks between them per call. The boundary is therefore
*downstream* of `_build_api_kwargs`, of the non-ASCII sanitiser at `:1086` and
of the `codex_responses` preflight at `:1088`, and *upstream* of the provider
client, its serialisation and the network.

`api_mode` is restricted to `chat_completions` and every other mode is
**refused**: `anthropic_messages` carries the system prompt outside `messages`
and `codex_responses` keys the payload `input`, so delivering into
`api_kwargs["messages"]` there would be a different claim about a different
payload shape. Refused rather than half-supported.

**What is NOT covered, and is not claimed.** hermes has 51 distinct
`.messages.create(` / `.chat.completions.create(` call sites outside its tests.
This adapter covers the two the main loop uses and says nothing about the other
49 — among them the iteration-limit summary
(`agent/chat_completion_helpers.py:1277` and `:1320`, which call
`chat.completions.create()` directly), the compression and auxiliary-client
paths, subagents, and the ACP/gateway surfaces. Per Owner direction 2026-10-20
those were not classified or funnelled as a precondition; they are named here
as the observation boundary instead. **No receipt is produced for a request
this adapter cannot see, and none claims to be.**

Why the record's text is not treated as the file's bytes
--------------------------------------------------------
`skill_view` has a `preprocess` switch, and the two load paths differ:

* slash commands and CLI preload call it with `preprocess=False`, so `content`
  is the file's own text — frontmatter included;
* the tool call leaves the default `preprocess=True`, so `content` has been
  through `agent/skill_preprocessing.preprocess_skill_content` — template
  substitution, and inline shell expansion when the host enables it.

A rendered string is a transformation of the file, not a copy of it. So a
record carrying one is labelled `TEXT_RENDERED` and its text is **not** handed
to `constraintrelay.container_hostrecord` as the host's body: the staleness comparison is
declined for that skill and reported as not performed, rather than being run
against bytes that were never on disk. Owner direction 2026-10-20: a trimmed or
rewritten fragment may not establish the version of a whole file. The version
itself never comes from the record's text in either case — it comes from
hashing the files the record's `skill_dir` points at against the library's
per-file bindings, which is what makes the version a fact about bytes.

**And declining the comparison is not enough.** A fresh-context review on
2026-10-20 ran the timing a declined comparison leaves open: tamper `SKILL.md`,
let the host load it through the tool path, then put the file back. The load
record held the tampered render, the disk held the original, and
`resolve_loads` reported `own-2026-10-18` with `host_body_matches_disk = null`
— a version claim about a load that *nothing bound*. Hashing the files the
`skill_dir` points at makes the version a fact about the bytes **on disk at
resolution time**, which is only the host's load while nobody moved the file
in between. `null` disclosed that the check had not run; it did not stop the
resolution that needed it. The fail-closed rule the core already applies to a
record with no text at all (`HOST_BODY_UNAVAILABLE`) was reachable past, by way
of the `require_host_body_match=False` escape hatch this wrapper was passing.

So a rendered load is now **bound by re-deriving the host's own transform**:
`render_binding` re-runs `agent.skill_preprocessing.preprocess_skill_content`
— the host's function, handed in by the caller, never a second
implementation — over the file on disk and requires the result to equal the
text in the record. Equal, and the rendered group resolves. Different, and it
is refused `HOST_RENDER_DIFFERS_FROM_DISK`: that is the timing above. Not
reproducible — the transform is not a function of the file alone, because
inline-shell expansion is enabled and the text carries a snippet — and it is
refused `HOST_RENDER_NOT_REPRODUCIBLE` rather than accepted on the strength of
two runs that happened to agree. See
[[feedback-reproduce-the-hosts-own-transform]].

The version label, and what hermes actually records
---------------------------------------------------
hermes **documents** a top-level `version:` frontmatter field
(`tools/skills_tool.py:32`) and two installed skills use it. It is **not in the
load record**: `skill_view`'s payload has no `version` key, and the only
frontmatter it passes through verbatim is `metadata`. So for a skill that
declares `version:` the ordinary way, this host records no version label and
the resolver falls to `library_binding_matched_host_bytes` — Vibe-Trading's
path, which the Owner permitted for exactly this case.

A skill that puts its version under `metadata:` *is* carried into the record,
and then `constraintrelay.container_hostrecord`'s `host_declared_label` path applies, with
the label required to name a library binding **and** to match the bytes on
disk. `v2/integration_hermes.py` drives both.

Whose load was it — and why the guard is not optional
-----------------------------------------------------
Owner direction 2026-10-21, item 1. Observing the **module attribute** is what
makes one wrapper cover all three load paths, and it is also why a second
agent's loads would land in this record. So every load carries an attribution
observation (`constraintrelay.load_attribution`), every load is decided against the agent
the adapter was installed on, and a load that is not the owner's **contributes
no declaration**: it is refused `LOAD_ATTRIBUTION_UNVERIFIED` or
`LOAD_ATTRIBUTED_TO_ANOTHER_AGENT` and the receipt reports load attribution as
NOT verified rather than reporting the request verified.

`SkillViewObserver` and `resolve_loads` both **require** the guard. Not a
default-off option: a guard nobody passed would leave every load unobserved,
and an unobserved load resolving normally is exactly the shape where a
declined check still licenses the claim it was meant to gate. Multi-agent
support is **not** extended by any of this — a delegated child remains
uncovered, and `v2/HERMES_HOST.md` says so; what changes is that the child's
loads can no longer be delivered for as though they were the parent's.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from . import load_attribution as LA
from .container import CAPSULE_MARKER, sha256_text
from .container_adapter import AdapterConfig, AdapterError, prepare

#: The pin this adapter was written and verified against.
SUPPORTED = {
    "host": "hermes-agent",
    "version": "0.14.0",
    "commit": "2517917de",
    "load_boundary": "tools.skills_tool.skill_view — the one function every "
                     "hermes skill load goes through (the skill_view tool, "
                     "slash commands, CLI preload)",
    "send_boundary": "AIAgent._interruptible_api_call and "
                     "AIAgent._interruptible_streaming_api_call, as called "
                     "from agent/conversation_loop.py:1172 and :1176",
    "api_modes_supported": ("chat_completions",),
    "not_covered": "the other 49 .create( call sites in the package, "
                   "including the iteration-limit summary at "
                   "agent/chat_completion_helpers.py:1277 and :1320, the "
                   "compression and auxiliary-client paths, subagents, and "
                   "the ACP/gateway surfaces",
}

#: The record's `content` is the file's own text (`preprocess=False`).
TEXT_RAW = "raw"
#: The record's `content` is a rendered transformation of the file
#: (`preprocess=True`). Not usable as evidence of the file's bytes.
TEXT_RENDERED = "rendered"
TEXT_FIDELITIES = (TEXT_RAW, TEXT_RENDERED)

#: Why a staleness comparison was not performed. Reported, never silent.
BODY_DECLINED_RENDERED = (
    "the host's record holds RENDERED skill text (skill_view was called with "
    "preprocess=True, so agent/skill_preprocessing rewrote it). A rendered "
    "string is not a copy of the file, so it was not used to decide whether "
    "the host holds the bytes now on disk. The version was resolved from the "
    "bytes themselves, via the library's per-file bindings, and the record's "
    "rendered text was bound to those bytes separately, by re-deriving the "
    "host's own transform over the file — see `render_binding`.")

#: How a rendered load was bound to the file on disk. Three outcomes and no
#: default: a rendered record that cannot be bound is REFUSED, because the
#: version would otherwise describe the disk at resolution time rather than
#: the text the host is holding.
RENDER_BOUND = "re_derived_host_transform_matches_record"
RENDER_DIFFERS = "HOST_RENDER_DIFFERS_FROM_DISK"
RENDER_NOT_REPRODUCIBLE = "HOST_RENDER_NOT_REPRODUCIBLE"
RENDER_NO_PREPROCESSOR = "HOST_RENDER_UNVERIFIABLE_NO_PREPROCESSOR"
RENDER_SOURCE_MISSING = "HOST_RENDER_SOURCE_FILE_MISSING"
RENDER_NO_RECORD_TEXT = "HOST_RENDER_NO_RECORD_TEXT"
RENDER_OUTCOMES = (RENDER_BOUND, RENDER_DIFFERS, RENDER_NOT_REPRODUCIBLE,
                   RENDER_NO_PREPROCESSOR, RENDER_SOURCE_MISSING,
                   RENDER_NO_RECORD_TEXT)
#: Every outcome but one is a refusal. Spelled out so a new outcome has to be
#: classified deliberately rather than defaulting to "accepted".
RENDER_REFUSALS = tuple(o for o in RENDER_OUTCOMES if o != RENDER_BOUND)

#: Why a load contributed nothing because of **whose** load it is. Two
#: reasons, not one: "another agent's" and "could not be established" call for
#: different actions, and collapsing them would hide which of the two the run
#: actually hit. Owner direction 2026-10-21, item 1.
ATTRIBUTION_UNVERIFIED = "LOAD_ATTRIBUTION_UNVERIFIED"
ATTRIBUTION_FOREIGN = "LOAD_ATTRIBUTED_TO_ANOTHER_AGENT"
ATTRIBUTION_REFUSALS = (ATTRIBUTION_UNVERIFIED, ATTRIBUTION_FOREIGN)

#: The marker hermes' inline-shell expansion keys on
#: (`agent/skill_preprocessing._INLINE_SHELL_RE`). Its presence in the source
#: text, with the feature enabled, means the transform is not a function of the
#: file: re-deriving it runs a shell command again. Detected from the mechanism
#: rather than from whether two sample runs agreed, because `!`date +%s`` twice
#: inside one second agrees by luck.
INLINE_SHELL_MARKER = "!`"


class HostHermesError(Exception):
    """This adapter must not run as configured."""


def record_text_transform(text: str) -> str:
    """The transformation hermes applies between `SKILL.md` and its record.

    It is the **identity**, and that is the point of this function existing.

    `constraintrelay.container_hostrecord` compares the text a host is holding against the
    file on disk, and to do that it has to reproduce whatever the host did to
    the text on the way in. Vibe-Trading's loader strips the frontmatter
    block, so the module's fallback — `_file_body` — strips it too. hermes does
    **not**: `skill_view` sets `content = skill_md.read_text()` and returns the
    whole file, frontmatter included.

    Passing the fallback here is not a near-miss, it is a total failure: the
    record's text (with frontmatter) never equals the file's text with the
    frontmatter removed, so **every single load** is refused
    `HOST_BYTES_DIFFER_FROM_DISK` — a staleness alarm for a host that is not
    stale. That happened on the first run of `v2/integration_hermes.py`, and
    it is why `resolve_from_host_record` takes `body_parser` as a parameter
    rather than owning one implementation: two implementations of one rule
    drift, and the drift is silent when the result is a refusal that looks
    like a finding.
    """
    return text


# ---------------------------------------------------------------- loads ----
@dataclass(frozen=True)
class LoadedSkill:
    """One successful `skill_view` load, as the host reported it.

    Shaped for `constraintrelay.container_hostrecord.resolve_from_host_record`, which is
    duck-typed on `name` / `dir_path` / `body` / `metadata`. `body` is `None`
    whenever the host's text may not stand in for the file's bytes — see the
    module docstring.
    """

    name: str
    dir_path: str
    body: "str | None"
    metadata: dict
    #: `TEXT_RAW` or `TEXT_RENDERED`, from the `preprocess` flag the host used.
    text_fidelity: str
    #: `path` as the host reported it, relative to its own skills dir.
    host_rel_path: str
    #: 1-based order of the load within the session.
    seq: int
    #: The host-side argument the load was requested by.
    requested_as: str = ""
    #: The record's text when it is RENDERED. Kept — and kept *out* of `body`,
    #: which is what `constraintrelay.container_hostrecord` reads as the file's bytes — so
    #: that `render_binding` has something to bind the load to. Withholding it
    #: from the core is the precaution; discarding it was the hole.
    rendered_text: "str | None" = None
    #: The `task_id` the host passed to `skill_view`, which it forwards as
    #: `session_id` to its own preprocessor. Re-deriving the transform with a
    #: different value would compare two different renders.
    session_id: "str | None" = None
    #: `constraintrelay.load_attribution.LoadObservation` for this load — the signals as
    #: they stood **while the host was loading**, decided later against the
    #: agent the adapter is installed on. `None` means the load was never
    #: observed, and an unobserved load is refused rather than resolved.
    observation: object = None

    def as_dict(self) -> dict:
        return {"name": self.name, "dir_path": self.dir_path,
                "requested_as": self.requested_as,
                "host_rel_path": self.host_rel_path,
                "text_fidelity": self.text_fidelity,
                "body_offered_to_resolver": self.body is not None,
                "rendered_text_retained_for_binding":
                    self.rendered_text is not None,
                "metadata_keys": sorted(self.metadata),
                "attribution_observed": self.observation is not None,
                "seq": self.seq}


class HermesLoadRecord:
    """The host's current load state: one entry per loaded skill directory.

    A second load of the same skill replaces the first, because the question
    the container asks is *what is the host holding now*, not *what has it ever
    read*. `.skills` is the sequence `resolve_from_host_record` reads.
    """

    def __init__(self) -> None:
        self._by_dir: dict = {}
        self._failed: list = []
        self._linked_file_reads: list = []
        self._seq = 0

    # -- what the resolver reads -------------------------------------------
    @property
    def skills(self) -> list:
        return [self._by_dir[k] for k in sorted(self._by_dir)]

    def __len__(self) -> int:
        return len(self._by_dir)

    # -- what the observer writes ------------------------------------------
    def record_load(self, payload: dict, *, preprocess: bool,
                    requested_as: str,
                    session_id: "str | None" = None,
                    observation=None) -> "LoadedSkill | None":
        skill_dir = payload.get("skill_dir")
        if not skill_dir:
            # hermes still serves legacy flat `<name>.md` skills, and for those
            # `skill_dir` is `null`. Recorded as unbindable rather than
            # resolved from the name: a path guessed from a slug is exactly
            # the hand-filling the host record exists to remove. Not raised,
            # either — an observer that can break a host load is not an
            # observer.
            self.record_refusal(
                requested_as,
                "the host reported a successful load with skill_dir=null (a "
                "legacy flat <name>.md skill). Its directory is unknown, so "
                "its bytes cannot be bound to a library version and it "
                "contributes no skill.")
            return None
        self._seq += 1
        fidelity = TEXT_RAW if not preprocess else TEXT_RENDERED
        text = payload.get("content")
        body = text if (fidelity == TEXT_RAW and isinstance(text, str)) else None
        rendered = text if (fidelity == TEXT_RENDERED
                            and isinstance(text, str)) else None
        meta = payload.get("metadata")
        item = LoadedSkill(
            name=str(payload.get("name") or ""),
            dir_path=str(skill_dir),
            body=body,
            metadata=dict(meta) if isinstance(meta, dict) else {},
            text_fidelity=fidelity,
            host_rel_path=str(payload.get("path") or ""),
            seq=self._seq,
            requested_as=requested_as,
            rendered_text=rendered,
            session_id=session_id,
            observation=observation)
        self._by_dir[str(Path(skill_dir).resolve())] = item
        return item

    def record_refusal(self, requested_as: str, error: str) -> None:
        """A `skill_view` that did not succeed. Contributes no skill."""
        self._failed.append({"requested_as": requested_as,
                             "error": str(error)[:200]})

    def record_linked_file(self, requested_as: str, file_path: str) -> None:
        """A read of a file *inside* a skill. Not a skill load."""
        self._linked_file_reads.append({"requested_as": requested_as,
                                        "file_path": file_path})

    # -- reporting ---------------------------------------------------------
    def by_fidelity(self, fidelity: str) -> list:
        return [s for s in self.skills if s.text_fidelity == fidelity]

    @property
    def failed(self) -> list:
        return list(self._failed)

    @property
    def linked_file_reads(self) -> list:
        return list(self._linked_file_reads)

    def as_dict(self) -> dict:
        return {
            "loaded_now": [s.as_dict() for s in self.skills],
            "loads_observed": self._seq,
            "failed_or_unbindable_loads": self.failed,
            "linked_file_reads": self.linked_file_reads,
            "_what_this_is": (
                "the host's own load state, observed at "
                "tools.skills_tool.skill_view. Only a load the host reported "
                "SUCCESSFUL, and only a whole-skill load, is here: a refused "
                "skill_view and a read of a file inside a skill are both "
                "recorded separately and contribute no skill. A skill that "
                "merely appears in the host's available-skills index is "
                "absent."),
        }


#: The attributes an object must have to be a hermes agent for attribution
#: purposes: the two methods the adapter wraps. Chosen as the marker set
#: because they are exactly what makes an object a *send boundary* on this
#: host — a class-name test would miss a subclass and match a stranger.
AGENT_MARKERS = ("_interruptible_streaming_api_call",
                 "_interruptible_api_call", "api_mode")


class AgentConstructionObserver:
    """Counts hermes agent constructions, as a conservative trigger only.

    Installed on `run_agent.AIAgent.__init__` — the **class**, deliberately,
    because the point is to see an agent this process did not construct
    through the adapter. It reports each construction to the guard and nothing
    else: `constraintrelay.load_attribution` uses the count only to withdraw the
    `SOLE_AGENT` elimination, never as the detector. Per Owner direction
    2026-10-21 a new agent may be a trigger and may not be the only basis.

    It cannot break the host. The wrapper calls the real `__init__` first and
    reports afterwards, inside a `try` — a construction that fails to be
    reported makes the guard *more* conservative, and an exception raised out
    of a host constructor would be this observer changing the host.

    **What it cannot see:** an agent constructed *before* it was installed —
    and that gap is closed by **counting** rather than by a comment.
    `install()` walks the garbage collector for live instances of the class
    and hands the count to the guard, which withdraws its `SOLE_AGENT`
    elimination for the whole run if the count is not zero. So installing
    late degrades attribution instead of making it look fine.
    """

    def __init__(self, guard) -> None:
        self.guard = guard
        self._cls = None
        self._inner = None
        #: Live instances of the class at install time, counted.
        self.pre_existing = None
        self.errors: list = []

    @staticmethod
    def count_live_instances(agent_cls) -> int:
        """Live instances of *agent_cls* right now, from the GC's own graph.

        `gc.get_objects()` rather than a registry this module keeps, because
        a registry only knows about objects it was told about — which is the
        very blind spot being measured. Subclass instances count: the hazard
        is a second **agent**, and `delegate_tool` constructing one through a
        subclass would otherwise walk past this.
        """
        import gc
        n = 0
        for obj in gc.get_objects():
            try:
                if isinstance(obj, agent_cls):
                    n += 1
            except Exception:                                # noqa: BLE001
                continue
        return n

    def install(self, agent_cls) -> None:
        if self._cls is not None:
            raise HostHermesError("this construction observer is installed")
        inner = agent_cls.__init__
        if getattr(inner, "_cf_construction_observer", None) is not None:
            raise HostHermesError(
                "AIAgent.__init__ is already observed; two observers would "
                "count every construction twice and inflate the peer count")
        self.pre_existing = self.count_live_instances(agent_cls)

        observer = self

        def observed_init(self, *a, **kw):
            inner(self, *a, **kw)
            try:
                observer.guard.note_construction(self)
            except Exception as exc:                         # noqa: BLE001
                observer.errors.append(repr(exc)[:200])

        observed_init._cf_construction_observer = self
        agent_cls.__init__ = observed_init
        self._cls, self._inner = agent_cls, inner
        self.guard.enable_construction_observation(
            pre_existing=self.pre_existing)

    def uninstall(self) -> None:
        if self._cls is not None:
            self._cls.__init__ = self._inner
            self._cls, self._inner = None, None

    def as_dict(self) -> dict:
        return {"installed": self._cls is not None,
                "class_observed": (self._cls.__name__ if self._cls
                                   else None),
                "live_instances_at_install": self.pre_existing,
                "observer_errors": list(self.errors)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


class SkillViewObserver:
    """Observes hermes' load boundary. Changes nothing about the load.

    The wrapper calls through with the caller's arguments untouched and returns
    the host's own JSON string unmodified; it only reads the result. Installed
    on the module attribute so the tool, slash-command and preload paths are
    all observed by one wrapper — see the module docstring.

    *guard* is **required**. It is an `constraintrelay.load_attribution.AttributionGuard`,
    and it is what makes the module-attribute observation safe: the signals
    for each load are captured here, while the host's own call chain is still
    on the stack, and decided later in `resolve_loads`. There is no
    guard-less mode, because an unobserved load would resolve as the owner's
    without anything having established that.
    """

    def __init__(self, guard) -> None:
        if not isinstance(guard, LA.AttributionGuard):
            raise HostHermesError(
                "SkillViewObserver needs an AttributionGuard: this observer "
                "replaces a MODULE attribute, so a second agent in the "
                "process loads through it too, and a load nobody attributed "
                "may not be delivered for. Construct one with "
                "constraintrelay.load_attribution.AttributionGuard(agent_markers="
                "constraintrelay.host_hermes.AGENT_MARKERS).")
        self.guard = guard
        self.record = HermesLoadRecord()
        #: Anything that went wrong *inside the observation*, recorded instead
        #: of raised: an observer must not be able to break a host load. Empty
        #: is the expected state and the suite asserts it, so this is a
        #: disclosure channel rather than a place for failures to go quiet.
        self.observer_errors: list = []
        self._module = None
        self._inner = None

    def install(self, skills_tool_module) -> None:
        if self._module is not None:
            raise HostHermesError("this observer is already installed")
        inner = getattr(skills_tool_module, "skill_view", None)
        if inner is None:
            raise HostHermesError(
                "the module passed in exposes no `skill_view`; this observer "
                "is for hermes' tools.skills_tool and not a general hook")
        if getattr(inner, "_cf_observer", None) is not None:
            raise HostHermesError(
                "skill_view is already observed; two observers would record "
                "every load twice")

        def observed(name, file_path=None, task_id=None, preprocess=True):
            out = inner(name, file_path=file_path, task_id=task_id,
                        preprocess=preprocess)
            # Captured HERE, not in `resolve_loads`: the caller's frames — the
            # host's loop, its tool dispatch, its slash-command handler — are
            # on the stack only while this wrapper is running. By resolution
            # time they are gone, and the one signal that is direct evidence
            # rather than an elimination would be unavailable.
            obs = self.guard.observe()
            try:
                payload = json.loads(out)
                if isinstance(payload, dict):
                    if not payload.get("success"):
                        self.record.record_refusal(str(name),
                                                   payload.get("error", ""))
                    elif file_path:
                        # A reference/template/script read. The skill was
                        # already loaded by its own call; counting this would
                        # record one skill twice and, worse, record a *file's*
                        # text as the skill's.
                        self.record.record_linked_file(str(name),
                                                       str(file_path))
                    else:
                        self.record.record_load(
                            payload, preprocess=bool(preprocess),
                            requested_as=str(name),
                            session_id=(None if task_id is None
                                        else str(task_id)),
                            observation=obs)
            except Exception as exc:                 # noqa: BLE001 - see below
                # The host's result is returned unchanged either way. An
                # observer that can raise out of a host load would change the
                # host's behaviour, which is the one thing this wrapper must
                # not do; the failure is disclosed in `observer_errors`
                # instead of travelling as a missing skill.
                self.observer_errors.append(
                    {"requested_as": str(name), "error": repr(exc)[:200]})
            return out

        observed._cf_observer = self
        skills_tool_module.skill_view = observed
        self._module, self._inner = skills_tool_module, inner

    def uninstall(self) -> None:
        if self._module is not None:
            self._module.skill_view = self._inner
            self._module, self._inner = None, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


# ------------------------------------------------- binding a rendered load --
@dataclass(frozen=True)
class RenderBinding:
    """Whether the host's rendered text re-derives from the file on disk.

    One of `RENDER_OUTCOMES`. `RENDER_BOUND` is the only one that permits a
    resolution: every other outcome means the record's text and the file could
    not be tied together, and a version resolved from that file would then be
    a claim about the disk rather than about the host.
    """

    outcome: str
    detail: str = ""
    #: The `session_id` the re-derivation used, so a reader can see it was the
    #: host's own and not a default this wrapper chose.
    session_id: "str | None" = None

    @property
    def bound(self) -> bool:
        return self.outcome == RENDER_BOUND

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "bound": self.bound,
                "detail": self.detail, "session_id_used": self.session_id,
                "_what_this_is": (
                    "the host's own preprocessor, re-run over the file on "
                    "disk and compared with the text in the load record. "
                    "Anything but "
                    f"{RENDER_BOUND!r} refuses the load.")}


def render_binding(item, *, preprocess_fn, skills_cfg=None) -> RenderBinding:
    """Re-derive the host's transform over the file and compare.

    *preprocess_fn* must be the **host's own**
    `agent.skill_preprocessing.preprocess_skill_content`, passed in by the
    caller. This module imports no hermes code, and a second implementation of
    the host's transform would be the very drift `record_text_transform`
    documents — there, getting it wrong refused every load; here it would
    refuse every rendered load, which looks like a tamper alarm.

    *skills_cfg* is the host's own `skills` config section, used only to ask
    whether inline-shell expansion is enabled. `None` means unknown, and
    unknown is treated as hazardous.
    """
    if getattr(item, "text_fidelity", None) != TEXT_RENDERED:
        raise HostHermesError(
            "render_binding is for a TEXT_RENDERED load; a raw record is "
            "compared by constraintrelay.container_hostrecord against the file itself")
    sid = getattr(item, "session_id", None)
    recorded = getattr(item, "rendered_text", None)
    if not isinstance(recorded, str) or not recorded.strip():
        return RenderBinding(
            RENDER_NO_RECORD_TEXT,
            "the host reported a rendered load carrying no text, so there is "
            "nothing to bind the load to", sid)
    if preprocess_fn is None:
        return RenderBinding(
            RENDER_NO_PREPROCESSOR,
            "no host preprocessor was supplied, so the host's transform could "
            "not be re-derived. Pass the host's own "
            "agent.skill_preprocessing.preprocess_skill_content", sid)
    skill_dir = Path(getattr(item, "dir_path", "") or "")
    f = skill_dir / "SKILL.md"
    if not f.is_file():
        return RenderBinding(
            RENDER_SOURCE_MISSING,
            f"{f} is not a file now, so the rendered text cannot be "
            "re-derived from it", sid)
    try:
        disk = f.read_text(encoding="utf-8")
    except OSError as exc:
        return RenderBinding(RENDER_SOURCE_MISSING, repr(exc)[:200], sid)

    inline_enabled = (skills_cfg.get("inline_shell", False)
                      if isinstance(skills_cfg, dict) else None)
    if INLINE_SHELL_MARKER in disk and inline_enabled is not False:
        return RenderBinding(
            RENDER_NOT_REPRODUCIBLE,
            "the source text carries an inline-shell snippet "
            f"({INLINE_SHELL_MARKER}…`) and the host's inline_shell setting is "
            + ("enabled" if inline_enabled else "unknown")
            + ", so the host's transform is not a function of the file: "
              "re-deriving it would run a command again and two renders of "
              "one file may legitimately differ", sid)
    try:
        first = preprocess_fn(disk, skill_dir, session_id=sid)
        second = preprocess_fn(disk, skill_dir, session_id=sid)
    except Exception as exc:                                # noqa: BLE001
        return RenderBinding(
            RENDER_NO_PREPROCESSOR,
            f"the host's preprocessor raised: {exc!r}"[:200], sid)
    if first != second:
        return RenderBinding(
            RENDER_NOT_REPRODUCIBLE,
            "two re-derivations of the same file disagreed, so the host's "
            "transform is not a function of the file alone", sid)
    if first == recorded:
        return RenderBinding(
            RENDER_BOUND,
            "the host's own preprocessor, re-run over SKILL.md as it stands, "
            "reproduces the text in the load record exactly", sid)
    detail = (f"re-derived {len(first)} chars, record holds {len(recorded)}")
    if str(first).strip() == recorded.strip():
        detail += " — equal apart from surrounding whitespace, which is still "\
                  "a difference this comparison does not forgive"
    return RenderBinding(RENDER_DIFFERS, detail, sid)


# ----------------------------------------------------------- resolution ----
@dataclass
class HermesResolution:
    """Two resolutions kept apart, because they were checked differently.

    `strict` holds the skills whose record carried the file's own text, and was
    resolved with the staleness check **on**. `relaxed` holds the skills whose
    record carried rendered text, resolved with `require_host_body_match=False`
    and `host_body_matches_disk = null` — not checked, and said so.

    They are two fields rather than one merged list so a reader cannot take a
    relaxed resolution for a strict one by reading a count.
    """

    host: str
    strict: object = None
    relaxed: object = None
    body_declined_reason: str = BODY_DECLINED_RENDERED
    #: `name -> RenderBinding`, one per rendered load, bound or refused.
    render_bindings: dict = None
    #: The rendered loads that could not be bound to the file, as `Refused`.
    #: They never reach the core resolver: an unbound load has no version to
    #: resolve, so there is nothing for the core to decide.
    render_refused: tuple = ()
    #: `name -> constraintrelay.load_attribution.Attribution`, one per load, owned or not.
    attributions: dict = None
    #: Loads withheld because of whose load they are, as `Refused`. Kept apart
    #: from `render_refused` because the two are different findings: one is
    #: about the bytes, this one is about the agent.
    attribution_refused: tuple = ()

    def __post_init__(self) -> None:
        if self.render_bindings is None:
            self.render_bindings = {}
        if self.attributions is None:
            self.attributions = {}

    def _parts(self) -> list:
        return [r for r in (self.strict, self.relaxed) if r is not None]

    def resolved(self) -> tuple:
        return tuple(r for part in self._parts() for r in part.resolved)

    def refused(self) -> tuple:
        return (tuple(r for part in self._parts() for r in part.refused)
                + tuple(self.render_refused)
                + tuple(self.attribution_refused))

    def loaded_total(self) -> int:
        return (sum(part.loaded_total for part in self._parts())
                + len(self.render_refused)
                + len(self.attribution_refused))

    def attribution_summary(self) -> dict:
        """The block a receipt carries. `established` is never True by default.

        Reconciled against `loaded_total()`: one attribution row per load, or
        the summary refuses to establish anything. Those two numbers were
        printed side by side in the same receipt and compared nowhere, which
        is how a name collision came to report `established: true` for a
        request that withheld a load.
        """
        return LA.summarise(self.attributions,
                            loads_expected=self.loaded_total())

    def _one_by_name(self, mapping: dict, name: str, what: str):
        """The single entry whose load key is *name*'s, or a refusal.

        These mappings are keyed per LOAD (`name#seq`), because a name is not
        unique. A lookup by name is still the natural thing to write, so it
        is offered here — and it **raises on ambiguity** rather than picking
        one, since picking is the defect the keys were changed to fix.
        """
        hits = {k: v for k, v in mapping.items()
                if k.rsplit("#", 1)[0] == name}
        if not hits:
            raise KeyError(f"no {what} for a load named {name!r}; keys are "
                           f"{sorted(mapping)}")
        if len(hits) > 1:
            raise KeyError(
                f"{len(hits)} loads are named {name!r} ({sorted(hits)}), so "
                f"a lookup by name cannot identify one {what}. Use the load "
                "key.")
        return next(iter(hits.values()))

    def attribution_for(self, name: str):
        """This load's attribution, by skill name. Ambiguity raises."""
        return self._one_by_name(self.attributions, name, "attribution")

    def render_binding_for(self, name: str):
        """This load's render binding, by skill name. Ambiguity raises."""
        return self._one_by_name(self.render_bindings, name, "render binding")

    def withheld_attribution(self) -> tuple:
        """Load keys withheld because attribution was not established.

        Keys, not bare names: two loads of one name are two findings, and
        reporting a name would merge them back together.
        """
        return tuple(sorted(n for n, a in self.attributions.items()
                            if a.withholds_delivery))

    def requests(self, *, library_content_id: str = "") -> tuple:
        out = []
        for part in self._parts():
            out.extend(part.requests(library_content_id=library_content_id))
        return tuple(out)

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "strict_staleness_checked": (self.strict.as_dict()
                                         if self.strict else None),
            "relaxed_staleness_declined": (self.relaxed.as_dict()
                                           if self.relaxed else None),
            "_why_two": (
                "the two groups were checked differently and are reported "
                "separately. The relaxed group's host_body_matches_disk is "
                "null — the CORE did not compare, because it cannot reproduce "
                "the host's rendering. It is not 'checked and agrees', and it "
                "is not unbound either: every skill in the relaxed group has "
                "a render_binding with outcome "
                f"{RENDER_BOUND!r}, or it was refused and is not in the "
                "group at all."),
            "_body_declined_reason": self.body_declined_reason,
            "render_bindings": {k: v.as_dict()
                                for k, v in sorted(self.render_bindings
                                                   .items())},
            "render_refused": [r.as_dict() for r in self.render_refused],
            "load_attribution": self.attribution_summary(),
            "attribution_refused": [r.as_dict()
                                    for r in self.attribution_refused],
            "resolved_count": len(self.resolved()),
            "refused_count": len(self.refused()),
            "version_sources_used": sorted({r.version_source
                                            for r in self.resolved()}),
        }

    def unbound_rendered(self) -> tuple:
        """Rendered loads whose text no file reproduces. Should be empty."""
        return tuple(sorted(n for n, b in self.render_bindings.items()
                            if not b.bound))


def load_key(item) -> str:
    """A key that is unique per LOAD rather than per skill name.

    `name` is not unique: the host's own record keys loads by resolved
    directory, two directories can hold the same skill name, and a payload
    with no name yields `""`. `seq` is the record's own monotonic load
    counter, so `name#seq` is unique within a record and carries no
    filesystem path into a receipt.
    """
    return f"{getattr(item, 'name', '') or '<unnamed>'}#{getattr(item, 'seq', '?')}"


def resolve_loads(record: HermesLoadRecord, libraries, *, source_roots,
                  host: str, guard, body_parser=None,
                  version_field: str = "version",
                  preprocess_fn=None, skills_cfg=None) -> HermesResolution:
    """Resolve the host's load state to built versions. Reuses the core.

    Delegates the resolution entirely to
    `constraintrelay.container_hostrecord.resolve_from_host_record`. This wrapper adds
    three things — one about **whose** load it is, two about text fidelity:

    0. it decides each load's **attribution** against the agent the adapter is
       installed on (*guard*, required) and drops the ones it cannot attribute
       **before** any version is resolved. Order matters: resolving first and
       filtering after would compute a version for a child agent's load and
       leave it in the resolution object for a later reader to pick up;

    1. it splits the record, so the core's staleness check runs on the raw
       group where it is sound and is declined — visibly — on the rendered
       group where the core cannot reproduce the host's transform;
    2. it **binds** each rendered load itself, by re-deriving the host's own
       transform over the file (`render_binding`), and refuses the load when
       that fails. Without (2) the declined check left the rendered group's
       version resolved from the disk at resolution time, which a tamper-load-
       restore sequence makes a false statement about the host; with it, that
       sequence is refused `HOST_RENDER_DIFFERS_FROM_DISK`.

    *preprocess_fn* is the host's own `preprocess_skill_content`. Omitting it
    does not weaken the result — it refuses every rendered load instead.

    *guard* is the `constraintrelay.load_attribution.AttributionGuard` the observer
    captured with. It has no default: the hazard this host presents is that
    the load boundary is a module attribute, and a resolution that ran without
    deciding attribution would be exactly the silence the guard exists to
    break.
    """
    from .container_hostrecord import Refused, resolve_from_host_record

    if not isinstance(guard, LA.AttributionGuard):
        raise HostHermesError(
            "resolve_loads needs the AttributionGuard the loads were observed "
            "with. Without it no load can be attributed to the agent being "
            "delivered into, and a load that is not attributed may not be "
            "delivered for — see Owner direction 2026-10-21, item 1.")

    out = HermesResolution(host=host)
    owned: list = []
    attribution_refused: list = []
    for it in record.skills:
        verdict = guard.decide(getattr(it, "observation", None))
        # Keyed per LOAD, not per name. `record` keys its own loads by
        # resolved directory, so two loads of the same `name` from different
        # directories are both real — and keying this by name let the second
        # overwrite the first. A stranger agent's FOREIGN load plus the
        # owner's OWNED load of the same name then left ONE row, and
        # `summarise()` reported `established: true` with an empty
        # `withheld_from_delivery` while that foreign load was in fact being
        # withheld. `name` is also `str(payload.get("name") or "")`, so every
        # load whose payload omits a name collided on `""`.
        out.attributions[load_key(it)] = verdict
        if verdict.owned:
            owned.append(it)
            continue
        attribution_refused.append(Refused(
            it.name, "",
            ATTRIBUTION_FOREIGN if verdict.status == LA.FOREIGN
            else ATTRIBUTION_UNVERIFIED,
            "this load could not be attributed to the agent this container "
            "delivers into, so it contributes no declaration: "
            + verdict.detail))
    out.attribution_refused = tuple(attribution_refused)

    raw = [s for s in owned if s.text_fidelity == TEXT_RAW]
    rendered = [s for s in owned if s.text_fidelity == TEXT_RENDERED]

    class _Part:
        def __init__(self, items):
            self.skills = items

    if raw:
        out.strict = resolve_from_host_record(
            _Part(raw), libraries, source_roots=source_roots, host=host,
            version_field=version_field, require_host_body_match=True,
            body_parser=body_parser)

    bound, refused = [], []
    for it in rendered:
        b = render_binding(it, preprocess_fn=preprocess_fn,
                           skills_cfg=skills_cfg)
        out.render_bindings[load_key(it)] = b
        if b.bound:
            bound.append(it)
        else:
            refused.append(Refused(
                it.name, "", b.outcome,
                "the host's record for this skill holds RENDERED text that "
                "the file on disk does not reproduce, so no version resolved "
                "from that file would describe what the host is holding: "
                + b.detail))
    out.render_refused = tuple(refused)
    if bound:
        out.relaxed = resolve_from_host_record(
            _Part(bound), libraries, source_roots=source_roots, host=host,
            version_field=version_field, require_host_body_match=False,
            body_parser=body_parser)
    return out


# ----------------------------------------------------------- the adapter ---
class MainLoopAdapter:
    """Delivers into hermes' main-loop request, before the model generates.

    Wraps both send methods on one `AIAgent` **instance** — the class is left
    alone, so a second agent in the same process is unaffected. Each call:

    1. pins the host's own system digest, before anything is added;
    2. asks `assembly_for()` for an assembly built from the host's load state
       *as it stands at this request*. `None` means nothing was loaded, and
       then nothing is delivered — a request before any load carries no
       capsule;
    3. builds a NEW kwargs dict with a NEW message list carrying the capsule,
       records it, and calls the host's own method with that;
    4. leaves the caller's `api_kwargs` and its message list untouched.
    """

    def __init__(self, *, assembly_for, config: AdapterConfig,
                 load_state_for=None, guard=None) -> None:
        if not isinstance(config, AdapterConfig):
            raise AdapterError("config must be an AdapterConfig; there is no "
                               "implicit default role or position")
        if guard is not None and not isinstance(guard, LA.AttributionGuard):
            raise AdapterError("guard must be a "
                               "constraintrelay.load_attribution.AttributionGuard")
        self._assembly_for = assembly_for
        #: Optional, called **immediately after** `assembly_for` on the same
        #: request: returns the host load state behind that assembly, so
        #: `constraintrelay.request_ledger` has the DUE set from the host rather than from
        #: what the assembly says it delivered. Two callbacks that could
        #: disagree, so `v2/integration_hermes.py` asserts they describe the
        #: same resolution object rather than trusting the pair.
        self._load_state_for = load_state_for
        #: The attribution guard, bound to this agent at `install()`. The
        #: adapter is where the owner becomes knowable: it is the object the
        #: container was asked to deliver into.
        self.guard = guard
        self.config = config
        self.payloads: list = []
        self._agent = None
        #: `attr -> the bound method install() found`, plus whether it lived in
        #: the instance dict. Both are needed to put things back exactly:
        #: the two methods are normally class attributes, but a harness may
        #: have set one on the instance, and `delattr` would then restore the
        #: class method instead of what was actually there.
        self._restore: dict = {}

    #: The two instance attributes wrapped, and the loop line that calls each.
    WRAPPED = {
        "_interruptible_streaming_api_call":
            "agent/conversation_loop.py:1172 (the host's default)",
        "_interruptible_api_call":
            "agent/conversation_loop.py:1176",
    }

    def install(self, agent) -> None:
        if self._agent is not None:
            raise AdapterError("this adapter is already installed")
        mode = getattr(agent, "api_mode", None)
        if mode not in SUPPORTED["api_modes_supported"]:
            raise AdapterError(
                f"api_mode {mode!r} is not supported by this adapter. "
                f"Verified: {list(SUPPORTED['api_modes_supported'])}. "
                "anthropic_messages carries the system prompt outside "
                "`messages` and codex_responses keys the payload `input`, so "
                "delivering into api_kwargs['messages'] there would be a "
                "different claim about a different payload. Refused rather "
                "than half-supported.")
        for attr in self.WRAPPED:
            inner = getattr(agent, attr, None)
            if inner is None:
                raise AdapterError(
                    f"this host exposes no `{attr}`; MainLoopAdapter is a "
                    f"compat entry for hermes-agent {SUPPORTED['commit']}, "
                    "not a general adapter")
            if getattr(inner, "_cf_adapter", None) is not None:
                raise AdapterError(f"{attr} is already wrapped by an adapter; "
                                   "two would double-deliver")
        for attr, where in self.WRAPPED.items():
            self._restore[attr] = (getattr(agent, attr),
                                   attr in vars(agent))
            setattr(agent, attr, self._wrap(getattr(agent, attr), attr, where))
        self._agent = agent
        if self.guard is not None:
            # The agent the container was asked to deliver into IS the owner.
            # Bound here rather than by the caller so the two cannot drift:
            # an owner bound to one agent while the adapter wraps another
            # would attribute every load to an agent that sends nothing.
            self.guard.bind_owner(agent)

    def _wrap(self, inner, attr: str, where: str):
        def wrapped(api_kwargs, *a, **kw):
            messages = api_kwargs.get("messages")
            if not isinstance(messages, list):
                raise AdapterError(
                    f"{attr} was handed kwargs with no `messages` list "
                    f"(keys: {sorted(api_kwargs)}). This adapter delivers "
                    "into api_kwargs['messages'] and refuses to guess where "
                    "else a payload might live.")
            # Pinned BEFORE delivery: the receipt cannot check non-elevation
            # without the host's own system message as it stood before the
            # capsule was added, and an unpinned digest degrades every
            # delivery claim to UNOBSERVABLE rather than passing.
            host_sys = ""
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "system" \
                        and isinstance(m.get("content"), str):
                    host_sys = sha256_text(m["content"])
                    break
            assembly = self._assembly_for()
            # Read immediately after, so it describes the load state THIS
            # request resolved — not the one a later request will.
            load_state = (self._load_state_for()
                          if self._load_state_for is not None else None)
            record = {
                "seq": len(self.payloads) + 1,
                "wrapped_attr": attr,
                "loop_call_site": where,
                "host_system_sha256_before": host_sys,
                "host_messages_in": copy.deepcopy(list(messages)),
                "tools": copy.deepcopy(list(api_kwargs.get("tools") or [])),
                "config": self.config.as_dict(),
                "load_state": load_state,
            }
            if assembly is None:
                # Nothing is deliverable, so nothing is declared for. This is
                # a delivery decision the HOST's load state made, and it is
                # recorded rather than skipped silently. WHICH of the two
                # reasons it is matters — "nothing was loaded" and "what was
                # loaded could not be attributed to this agent" are different
                # findings, and the second must never be reported as the
                # first.
                withheld = list((load_state or {}).get(
                    "withheld_attribution_ids") or [])
                record.update({"delivered": False,
                               "reason": (
                                   "no skill was loaded in the host's record "
                                   "at this request"
                                   if not withheld else
                                   "every deliverable load at this request "
                                   "was WITHHELD because its attribution to "
                                   "this agent could not be established: "
                                   + ", ".join(withheld)),
                               "withheld_attribution_ids": withheld,
                               "messages": copy.deepcopy(list(messages)),
                               "capsule_index": None})
                self.payloads.append(record)
                return inner(api_kwargs, *a, **kw)
            res = prepare(messages, assembly, self.config)
            out_kwargs = dict(api_kwargs)
            out_kwargs["messages"] = res["messages"]
            record.update({"delivered": True, "reason": "",
                           "messages": res["messages"],
                           "capsule_index": res["capsule_index"],
                           "withheld_attribution_ids": list(
                               (load_state or {}).get(
                                   "withheld_attribution_ids") or []),
                           "stale_capsules_dropped":
                               res["stale_capsules_dropped"]})
            self.payloads.append(record)
            return inner(out_kwargs, *a, **kw)

        wrapped._cf_adapter = self
        return wrapped

    def uninstall(self) -> None:
        if self._agent is None:
            return
        for attr, (inner, was_instance_attr) in self._restore.items():
            if was_instance_attr:
                setattr(self._agent, attr, inner)
            else:
                # It was a class attribute; dropping the instance override
                # restores exactly what `install()` found, rather than
                # pinning a bound method onto the instance.
                try:
                    delattr(self._agent, attr)
                except AttributeError:
                    setattr(self._agent, attr, inner)
        self._restore = {}
        self._agent = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False

    def delivered(self) -> list:
        return [p for p in self.payloads if p["delivered"]]

    def last(self) -> "dict | None":
        return self.payloads[-1] if self.payloads else None

    def capsule_counts(self) -> list:
        """Capsules per recorded request. `[0, 1, 1]` is the shape to want."""
        out = []
        for p in self.payloads:
            out.append(sum(1 for m in p["messages"]
                           if isinstance(m, dict)
                           and isinstance(m.get("content"), str)
                           and m["content"].startswith(CAPSULE_MARKER)))
        return out
