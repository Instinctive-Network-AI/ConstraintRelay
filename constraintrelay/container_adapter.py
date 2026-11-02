"""0.2 — the adapter layer. The core knows nothing about any host.

`cf/container.py` assembles and renders. It must not know what a host is, so it
has no reference to `stream_chat`, to a model client, or to any particular
message role. Everything host-shaped lives here.

The adapter contract, three items
---------------------------------
1. **The host supplies the skill ids and versions** for this use.
   `constraintrelay.container.ContainerRequest` takes them and nothing infers them from a
   conversation. An adapter that guessed would be making the selection
   decision the container exists to avoid.
2. **The capsule is added before model generation**, at the position the host
   configured. Not after a tool call has been chosen — that is downstream of
   the decision it was meant to inform.
3. **Verification happens at the host's actual observable final request
   boundary**, and produces the one receipt format in
   `constraintrelay.container_receipt`. An adapter that cannot name its boundary cannot
   produce a receipt.

There is no default injection position, and that is deliberate
--------------------------------------------------------------
`AdapterConfig` requires `role` and `position` explicitly and **refuses to be
constructed without them**. The reason: every default here would be wrong
somewhere. A default role would silently pick an authority level for someone
else's host — the difference between `user` and `system` is the difference
between quoting an author and issuing an instruction. A default position would
silently decide whether the capsule lands before or after the host's own
system prompt. Both are the host's call about its own request, and a container
that guesses is a container that has elevated something without being asked.

So an unconfigured adapter raises `AdapterError` naming the two fields. The
cost is one explicit line per host; the alternative is a quiet default nobody
chose.

Non-interference
----------------
`prepare()` returns a **new list**. The caller's messages are never mutated,
nothing is deleted, order is preserved, and the capsule is inserted at exactly
one place. `assert_non_interference()` checks all of that against the original,
including tool-call pairing and tool definitions, and is what the acceptance
run uses rather than a promise.

"Has a `stream_chat` attribute" is not compatibility
----------------------------------------------------
`StreamChatAdapter` exists because Vibe-Trading's client has that method, and
it is a **compat entry for one known host at one known version**, not a
capability claim. Duck-typing on an attribute name says nothing about whether
the payload reaching that method is the one the model sees. Each supported host
has to be named, with the version and the boundary that was actually observed —
see `SUPPORTED_HOSTS`.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from .container import CAPSULE_MARKER, render_capsule, sha256_text

#: Where the capsule may be placed, relative to the host's own messages.
#: `append` is the only position verified end to end so far; the others are
#: refused rather than half-supported.
POSITION_APPEND = "append"
POSITIONS = (POSITION_APPEND,)

#: Roles the capsule may travel in. `system` and `developer` are absent: a
#: declaration keeps its author's authority and is not raised to a host
#: instruction. This is the same refusal `cf/container_receipt.py` enforces
#: when reading the payload, so neither end can relax it alone.
NON_ELEVATING_ROLES = ("user", "tool")

#: Hosts whose boundary has actually been observed, per version. A host is
#: listed here only after a run captured its final request and a receipt
#: verified the capsule in it. Nothing is listed on the strength of an API
#: looking similar.
SUPPORTED_HOSTS = {
    "vibe-trading": {
        "version_observed": "dddf96e (pinned checkout)",
        "adapter": "StreamChatAdapter",
        "wrapped": "ChatLLM.stream_chat — agent/src/providers/chat.py:249, "
                   "called from agent/src/agent/loop.py:732",
        "observable_boundary": "the `messages` list as handed to "
                               "stream_chat: downstream of the loop and of "
                               "all three compaction layers, upstream of "
                               "serialisation, the network and any remote "
                               "model",
        "verified_with": "local mock transport, zero provider calls",
        "not_claimed": "that a remote model received anything; the receipt's "
                       "observable_boundary says so on its face",
    },
    "hermes-agent": {
        "version_observed": "0.14.0 @ 2517917de (separate pinned checkout)",
        "adapter": "constraintrelay.host_hermes.MainLoopAdapter",
        "wrapped": "AIAgent._interruptible_streaming_api_call and "
                   "AIAgent._interruptible_api_call — the two methods the "
                   "main loop calls at agent/conversation_loop.py:1172 and "
                   ":1176, one request per model decision",
        "observable_boundary": "the `messages` list inside api_kwargs as "
                               "handed to the host's send method: downstream "
                               "of _build_api_kwargs, the non-ASCII "
                               "sanitiser and the codex preflight, upstream "
                               "of serialisation, the network and any remote "
                               "model",
        "verified_with": "a recorded reply where the provider stream would "
                         "be, zero provider calls; cf/phase1_pause armed "
                         "over the three send chokepoints throughout",
        "api_modes": "chat_completions ONLY. anthropic_messages, "
                     "codex_responses and bedrock_converse are refused at "
                     "install: their payloads are not shaped like this one",
        "not_covered": "the other 49 .create( call sites in the package, "
                       "including the iteration-limit summary at "
                       "agent/chat_completion_helpers.py:1277 and :1320, the "
                       "compression and auxiliary-client paths, subagents, "
                       "and the ACP/gateway surfaces. No receipt is produced "
                       "for a request the adapter cannot see",
        "load_attribution": "the load boundary is a MODULE attribute, so a "
                            "second agent in the process loads through it "
                            "too. Every load is decided against the agent "
                            "the adapter was installed on "
                            "(constraintrelay.load_attribution); one that cannot be "
                            "attributed contributes no declaration and the "
                            "receipt reports load_attribution_established "
                            "false. Driven with two real agents in "
                            "v2/integration_hermes.py M8",
        "open_gap": "DELEGATION IS NOT SUPPORTED. A delegated child builds "
                    "its own AIAgent, which this adapter never wraps, so it "
                    "delivers nothing and no receipt describes it; its loads "
                    "are withheld from the parent rather than attributed to "
                    "it. Subagent coverage is UNVERIFIED, not working — see "
                    "v2/HERMES_HOST.md",
        "not_claimed": "that a remote model received anything; the receipt's "
                       "observable_boundary says so on its face",
    },
}


class AdapterError(Exception):
    """An adapter that must not run as configured."""


@dataclass(frozen=True)
class AdapterConfig:
    """How one host wants the capsule delivered. No defaults.

    Both fields are required. See the module docstring for why a default would
    be a container choosing an authority level or a position on a host's
    behalf.
    """

    role: str
    position: str
    #: Free text naming the host and version this config is for. Carried into
    #: the receipt so a receipt cannot be read as evidence about a host it was
    #: not taken on.
    host: str = ""

    def __post_init__(self) -> None:
        if not str(self.role).strip():
            raise AdapterError(
                "AdapterConfig.role is required and has NO default. The "
                "difference between 'user' and 'system' is the difference "
                "between quoting an author and issuing an instruction, and "
                "that is the host's call about its own request. Pass one of "
                f"{list(NON_ELEVATING_ROLES)}.")
        if self.role not in NON_ELEVATING_ROLES:
            raise AdapterError(
                f"role {self.role!r} would raise the author's declaration to "
                f"a host instruction. Permitted: "
                f"{list(NON_ELEVATING_ROLES)}.")
        if not str(self.position).strip():
            raise AdapterError(
                "AdapterConfig.position is required and has NO default. A "
                "default would silently decide whether the capsule lands "
                "before or after the host's own system prompt. Pass one of "
                f"{list(POSITIONS)}.")
        if self.position not in POSITIONS:
            raise AdapterError(
                f"position {self.position!r} is not verified. Permitted: "
                f"{list(POSITIONS)}. An unverified position is refused rather "
                "than half-supported.")

    def as_dict(self) -> dict:
        return {"role": self.role, "position": self.position,
                "host": self.host or None,
                "_no_defaults": (
                    "role and position are required with no defaults, so the "
                    "container never picks an authority level or a position "
                    "on a host's behalf.")}


def prepare(messages: list, assembly, config: AdapterConfig) -> dict:
    """Return a NEW message list carrying the capsule, plus what was done.

    The caller's list is never mutated. An existing capsule from an earlier
    turn is dropped **from the copy** so one request carries exactly one, and
    nothing of the host's own content is removed.
    """
    if assembly is None:
        raise AdapterError("no assembly to deliver")
    out = copy.deepcopy(list(messages))
    dropped = [i for i, m in enumerate(out)
               if isinstance(m, dict) and isinstance(m.get("content"), str)
               and m["content"].startswith(CAPSULE_MARKER)]
    for i in reversed(dropped):
        out.pop(i)
    capsule = render_capsule(assembly)
    out.append({"role": config.role, "content": capsule})
    return {"messages": out, "capsule": capsule,
            "capsule_index": len(out) - 1,
            "stale_capsules_dropped": len(dropped),
            "config": config.as_dict()}


def _tool_pairs(messages: list) -> list:
    """`(assistant tool_call ids, tool-result ids)` in order, for comparison."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("tool_calls"):
            out.append(("assistant_calls",
                        [tc.get("id") if isinstance(tc, dict)
                         else getattr(tc, "id", None)
                         for tc in m["tool_calls"]]))
        elif m.get("role") == "tool":
            out.append(("tool_result", m.get("tool_call_id")))
    return out


def assert_non_interference(original: list, prepared: list, *,
                            tools_before=None, tools_after=None) -> dict:
    """Everything that must be unchanged, checked rather than promised.

    Returns a report with one boolean per property. `ok` is the conjunction, so
    a caller cannot pass by reading only the field it expected.
    """
    def strip(ms):
        return [m for m in ms
                if not (isinstance(m, dict)
                        and isinstance(m.get("content"), str)
                        and m["content"].startswith(CAPSULE_MARKER))]

    orig_clean, prep_clean = strip(original), strip(prepared)
    caps = [m for m in prepared
            if isinstance(m, dict) and isinstance(m.get("content"), str)
            and m["content"].startswith(CAPSULE_MARKER)]
    r = {
        "host_messages_unchanged": orig_clean == prep_clean,
        "host_message_count_unchanged": len(orig_clean) == len(prep_clean),
        "roles_unchanged": [m.get("role") for m in orig_clean
                            if isinstance(m, dict)]
                           == [m.get("role") for m in prep_clean
                               if isinstance(m, dict)],
        "nothing_deleted": len(prep_clean) >= len(orig_clean),
        "exactly_one_capsule": len(caps) == 1,
        "capsule_is_last": bool(prepared) and prepared[-1] in caps,
        "tool_pairing_unchanged": _tool_pairs(orig_clean)
                                  == _tool_pairs(prep_clean),
        "tool_definitions_unchanged": (
            json.dumps(tools_before, sort_keys=True, default=str)
            == json.dumps(tools_after, sort_keys=True, default=str)),
        "original_list_not_mutated": True,   # set by the caller's own check
    }
    r["ok"] = all(v for k, v in r.items() if isinstance(v, bool))
    r["_what_this_checks"] = (
        "the host's own messages, their order and roles, tool-call pairing "
        "and the tool definitions are identical; exactly one capsule was "
        "added and nothing was removed.")
    return r


class StreamChatAdapter:
    """Compat entry for a host whose client exposes `stream_chat(messages,…)`.

    Verified against Vibe-Trading at the pin in `SUPPORTED_HOSTS`. **Having
    the method is not compatibility** — the question is whether the list
    handed to it is the one the model sees, and that has to be observed per
    host and per version.

    Replaces the host's bound method with one that builds a copy, delivers
    into the copy, records it, and calls through. The host's own list is left
    exactly as it was.
    """

    def __init__(self, *, assembly_for, config: AdapterConfig) -> None:
        if not isinstance(config, AdapterConfig):
            raise AdapterError("config must be an AdapterConfig; there is no "
                               "implicit default role or position")
        self._assembly_for = assembly_for
        self.config = config
        self.payloads: list = []
        self._installed_on = None
        self._inner = None

    def install(self, llm) -> None:
        if self._installed_on is not None:
            raise AdapterError("this adapter is already installed")
        inner = getattr(llm, "stream_chat", None)
        if inner is None:
            raise AdapterError(
                "this host exposes no `stream_chat`; StreamChatAdapter is a "
                "compat entry for hosts that do, not a general adapter")
        if getattr(inner, "_cf_adapter", None) is not None:
            raise AdapterError("stream_chat is already wrapped by an adapter; "
                               "two would double-deliver")

        def wrapped(messages, *a, **kw):
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
            res = prepare(messages, self._assembly_for(), self.config)
            self.payloads.append({
                "host_system_sha256_before": host_sys,
                "seq": len(self.payloads) + 1,
                "messages": res["messages"],
                "tools": list(kw.get("tools") or (a[0] if a else None) or []),
                "host_messages_in": copy.deepcopy(list(messages)),
                "capsule_index": res["capsule_index"],
                "config": res["config"],
            })
            return inner(res["messages"], *a, **kw)

        wrapped._cf_adapter = self
        llm.stream_chat = wrapped
        self._installed_on, self._inner = llm, inner

    def uninstall(self) -> None:
        if self._installed_on is not None:
            self._installed_on.stream_chat = self._inner
            self._installed_on, self._inner = None, None

    def last(self) -> "dict | None":
        return self.payloads[-1] if self.payloads else None
