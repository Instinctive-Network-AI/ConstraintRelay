"""v2 layer 4b — delivery into the REAL Vibe-Trading request, before generation.

Why this file exists, and what it corrects
------------------------------------------
`cf/predelivery.py` named the delivery point as
``agent/src/agent/loop.py:1063 _process_tool_calls``. **That was wrong for the
purpose.** Reading the real loop at `dddf96e`:

====  ===================================================================
:732  ``response = self.llm.stream_chat(messages, tools=tool_defs, ...)``
      the model produces the tool name **and its arguments** here
:953  ``format_assistant_tool_calls(response.tool_calls, ...)``
:962  ``self._process_tool_calls(response.tool_calls, ...)``
:1254 ``_invoke_tool``
====  ===================================================================

`_process_tool_calls` runs **after** the arguments exist. A capsule injected
there cannot influence the call in front of it — only the next iteration. It is
a **pre-execution** point, useful for stopping a call (deferred S5) and useless
for informing one.

The pre-generation point is the request itself. So the injector wraps
``ChatLLM.stream_chat``, which is where Phase 3 (`cf/lifetime.py`) already
injected — and which this module returns to. Phase 3 had the position right;
moving it was my error.

Both points are now named separately so neither can stand in for the other:

* ``PRE_GENERATION`` — before the request that produces tool calls. Rules can
  inform the call. **This is delivery.**
* ``PRE_EXECUTION`` — after arguments exist, before the tool runs. Rules can
  only block or annotate. **This is not delivery**, and a log written here is
  not evidence that the model saw anything.

Position inside the wrapper stack
---------------------------------
``loop -> injector -> transport``. The injector must be the OUTER wrapper so
whatever records or sends the request sees the post-injection payload. Install
it last and assert the ordering, because an injector placed inside a recorder
would produce a recording that proves nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

PRE_GENERATION = "PRE_GENERATION"
PRE_EXECUTION = "PRE_EXECUTION"

#: What was actually read in source, at the pinned commit. Scope is the entry
#: point and the version, never the product.
VIBE_ENTRY_POINTS = {
    PRE_GENERATION: {
        "site": "agent/src/providers/chat.py:249 ChatLLM.stream_chat, called "
                "from agent/src/agent/loop.py:732 and :765",
        "before_tool_arguments_exist": True,
        "can_inform_this_call": True,
        "can_block_this_call": False,
    },
    PRE_EXECUTION: {
        "site": "agent/src/agent/loop.py:1063 _process_tool_calls, before "
                ":1254 _invoke_tool",
        "before_tool_arguments_exist": False,
        "can_inform_this_call": False,
        "can_block_this_call": True,
    },
}


class InjectionError(Exception):
    """The injector cannot guarantee the capsule reaches the request."""


@dataclass
class OutboundRecord:
    """One request as it left the injector, with the capsule's fate in it."""

    seq: int
    phase: str
    message_count_in: int
    message_count_out: int
    capsule_present: bool
    capsule_sha256: str
    rule_ids: list = field(default_factory=list)
    coverage_status: str = ""
    tool_names_offered: list = field(default_factory=list)
    #: True when this payload already contains an assistant tool_call, i.e. the
    #: arguments were decided by an EARLIER request and this one is downstream.
    contains_assistant_tool_call: bool = False
    payload_sha256: str = ""

    def as_dict(self) -> dict:
        return {"seq": self.seq, "phase": self.phase,
                "message_count_in": self.message_count_in,
                "message_count_out": self.message_count_out,
                "capsule_present": self.capsule_present,
                "capsule_sha256": self.capsule_sha256,
                "rule_ids": list(self.rule_ids),
                "coverage_status": self.coverage_status,
                "tool_names_offered": list(self.tool_names_offered),
                "contains_assistant_tool_call":
                    self.contains_assistant_tool_call,
                "payload_sha256": self.payload_sha256}


def _digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()


class PreGenerationInjector:
    """Wraps `ChatLLM.stream_chat` and puts the capsule in the real payload.

    The capsule is appended as its own trailing message. Appended, not merged
    into an existing one: a separate message is visible in the payload as a
    discrete object, so "did the model receive it" is answerable by inspecting
    the request rather than by searching prose.

    Re-stated on **every** request, following Phase 3's finding — nothing is
    expected to survive compaction, so nothing has to.
    """

    MARKER = "<!-- constraintflow:capsule -->"

    def __init__(self, *, render, selection_for, role: str = "system") -> None:
        self._render = render
        self._selection_for = selection_for
        self._role = role
        self.records: list = []
        self._seq = 0
        self._installed_on = None
        self._inner = None

    def install(self, llm) -> None:
        """Wrap *llm*'s bound `stream_chat`. Must be the OUTERMOST wrapper."""
        if self._installed_on is not None:
            raise InjectionError("this injector is already installed")
        inner = llm.stream_chat
        if getattr(inner, "_cf_injector", None) is not None:
            raise InjectionError("stream_chat is already wrapped by an "
                                 "injector; two would double-inject")

        def wrapped(messages, *a, **kw):
            self._seq += 1
            rec = self._deliver_into(messages, kw.get("tools") or
                                     (a[0] if a else None))
            self.records.append(rec)
            return inner(messages, *a, **kw)

        wrapped._cf_injector = self
        llm.stream_chat = wrapped
        self._installed_on, self._inner = llm, inner

    def uninstall(self) -> None:
        if self._installed_on is not None:
            self._installed_on.stream_chat = self._inner
            self._installed_on, self._inner = None, None

    def _deliver_into(self, messages: list, tools) -> OutboundRecord:
        before = len(messages)
        sel, rendered = self._selection_for(), ""
        rule_ids, coverage = [], ""
        if sel is not None:
            rendered = self._render(sel)
            rule_ids = [r.constraint_id for r in sel.rules]
            coverage = sel.coverage_status
        has_tc = any(isinstance(m, dict) and m.get("tool_calls")
                     for m in messages)
        if rendered:
            # Drop a capsule from an earlier turn so the payload carries one,
            # then append the current one LAST — closest to the model's
            # attention and after every compaction layer has run.
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if isinstance(m, dict) and isinstance(m.get("content"), str) \
                        and m["content"].startswith(self.MARKER):
                    messages.pop(i)
            messages.append({"role": self._role,
                             "content": self.MARKER + "\n" + rendered})
        present = any(isinstance(m, dict)
                      and isinstance(m.get("content"), str)
                      and m["content"].startswith(self.MARKER)
                      for m in messages)
        names = []
        for t in (tools or []):
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                names.append(fn["name"])
        return OutboundRecord(
            seq=self._seq, phase=PRE_GENERATION, message_count_in=before,
            message_count_out=len(messages), capsule_present=present,
            capsule_sha256=(_digest(rendered) if rendered else ""),
            rule_ids=rule_ids, coverage_status=coverage,
            tool_names_offered=names, contains_assistant_tool_call=has_tc,
            payload_sha256=_digest(messages))


def assert_rules_precede_tool_arguments(records: list) -> dict:
    """The claim under test: rules were in the request that produced the call.

    A payload that already holds an assistant `tool_calls` message is
    **downstream** of the decision — the arguments were fixed by an earlier
    request. So the capsule has to be present in at least one payload that
    offers tools and does **not** yet contain an assistant tool call.

    Returns a verdict dict. `proven` is False when no such payload exists, and
    that is the honest outcome for an injector placed too late.
    """
    qualifying = [r for r in records
                  if r.tool_names_offered and not r.contains_assistant_tool_call]
    with_capsule = [r for r in qualifying if r.capsule_present]
    return {
        "claim": ("the capsule was present in a request that offered tools and "
                  "did not yet contain an assistant tool_call, i.e. in the "
                  "request whose output decided the tool and its arguments"),
        "payloads_total": len(records),
        "payloads_pre_decision": len(qualifying),
        "payloads_pre_decision_with_capsule": len(with_capsule),
        "pre_decision_seqs": [r.seq for r in qualifying],
        "capsule_seqs": [r.seq for r in records if r.capsule_present],
        "proven": bool(qualifying) and len(with_capsule) == len(qualifying),
        "note": ("a capsule seen ONLY in payloads that already contain an "
                 "assistant tool_call proves delivery after the decision, "
                 "which is not delivery to the decision"),
    }
