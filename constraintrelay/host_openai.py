"""1.0 — the provider-SDK host: one boundary, most of the ecosystem.

Why this host exists
--------------------
The two hosts verified before this one are agent checkouts: `vibe-trading`
and `hermes-agent`. An ordinary agent developer has never heard of either.
The frameworks they actually use — LangChain, LangGraph, CrewAI, AutoGen,
LlamaIndex, the OpenAI Agents SDK — were supported zero times.

But every one of those frameworks ends up calling the same handful of
provider-SDK methods. Wrapping **the SDK client** therefore gives a boundary
that is downstream of every framework's message assembly and upstream of
serialisation — the same class of boundary both earlier hosts already use.
One adapter, most of the ecosystem.

What this does NOT make true
----------------------------
Wrapping `chat.completions.create` observes **the keyword arguments handed to
that method**. It does not observe the bytes on the wire: serialisation,
retries, and any client-side middleware all happen downstream, inside the SDK.
Nor does it observe a framework that builds its own HTTP request rather than
calling the SDK. A receipt from here says what it saw and names what it could
not, exactly like the other two hosts.

The instance, not the class
---------------------------
`attach()` patches the bound method on **one client object**. Patching
`openai.resources.chat.Completions.create` at class level would reach every
client in the process, including ones built by a library the developer did not
write — the same module-level reach that made `constraintrelay.load_attribution` necessary
for hermes. An instance patch cannot do that, and `uninstall()` reverses it.

Nothing is inferred
-------------------
The developer names the active skills through `active()`. This module never
reads the conversation to guess which skill is in play: that selection is the
decision the container exists to avoid making for someone else. Versions
resolve from the **bytes** through `constraintrelay.container_hostrecord`, never from a
label written here.

Failure never blocks
--------------------
Every ConstraintFlow error inside the wrapper degrades to passing the
developer's original call through untouched, plus a ledger row naming the
reason. A declaration that fails to deliver is a reported gap; it is never an
outage in someone else's agent.
"""

from __future__ import annotations

import collections.abc
import contextvars
import copy
import inspect
import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .container import sha256_text
from .container_adapter import (AdapterConfig, AdapterError, prepare,
                                assert_non_interference)
from .container_api import (AVAILABLE_COMPLETE, assemble, load_library,
                            load_manifests, verify, ContainerRequest)
from .container_hostrecord import resolve_from_host_record
from .container_receipt import _text as _message_text
from .request_ledger import RequestLedger


class ProviderHostError(Exception):
    """This client cannot be attached to as asked."""


#: Provider surfaces whose payload shape has been read and whose boundary can
#: be named. A surface is listed here only when this module knows where the
#: messages live, where the tools live, and what the observation cannot reach.
#:
#: `anthropic_messages` is deliberately absent: Anthropic carries the system
#: prompt in a separate `system=` keyword rather than a message with
#: `role="system"`, so the non-elevation check — which reads the first
#: `system` message out of the payload — would silently find none and pin
#: nothing. That is a different boundary, not a near-enough one, and it is
#: refused at install rather than half-supported. Same for
#: `responses.create`, whose `input` is not a `messages` list.
PROVIDER_PROFILES = {
    "openai-python": {
        "attach": ("chat", "completions", "create"),
        "messages_kwarg": "messages",
        "tools_kwarg": "tools",
        # Verified-value, not a default: `append` is the only position any
        # host has driven end to end, and `user` keeps the declaration at its
        # author's authority. `AdapterConfig` still refuses to be built
        # without both, so this is a recorded choice rather than a silent one.
        "role": "user",
        "position": "append",
        "send_path": "client.chat.completions.create",
        "observation_point": (
            "the keyword arguments handed to "
            "`client.chat.completions.create` on one client this guard is "
            "attached to"),
        "observable_boundary": (
            "the `messages` list as handed to the SDK method: downstream of "
            "whatever framework assembled it, upstream of the SDK's own "
            "serialisation, retries, client middleware and the network. NOT "
            "the bytes on the wire, and not a request any other client in "
            "this process makes"),
        "unobservable_paths": [
            "any request made through a different client instance",
            "any request a framework builds as raw HTTP without calling this "
            "SDK method",
            "the SDK's internal retries, which re-send a payload this "
            "wrapper sees only once",
            "`client.responses.create` and every non-chat surface on the "
            "same client",
        ],
    },
}

#: Surfaces named here are refused at install with the reason, so a developer
#: who asks for one gets the reason rather than silence.
REFUSED_SURFACES = {
    "anthropic-python": (
        "Anthropic carries the system prompt in a separate `system=` keyword, "
        "not as a message with role='system'. The receipt's non-elevation "
        "check reads the first system message out of the payload, so against "
        "this shape it would pin nothing and report nothing wrong. That is a "
        "separate boundary needing its own verification, not a near-enough "
        "one."),
    "openai-responses": (
        "`responses.create` takes `input`, not a `messages` list, and its "
        "item shapes differ. Supporting it by renaming a keyword would make "
        "every check in `assert_non_interference` read a structure it was "
        "not written against."),
}


@dataclass(frozen=True)
class _LoadedSkill:
    """One activated skill, in the shape `resolve_from_host_record` reads.

    The developer's `active()` call *is* this host's load record. There is no
    loader to read it out of, so the record is built from the explicit
    activation and nothing else — which is why `name` and `dir_path` are the
    only fields: this host records no version label, so the only sound source
    for a version is the bytes on disk.
    """

    name: str
    dir_path: str


#: Per-thread **and** per-async-task, because a `ContextVar` is both. A plain
#: instance attribute would let one thread's activation decide another
#: thread's request, which is the selection decision this module refuses to
#: make even for itself.
#:
#: It holds a mapping keyed by guard, not a bare tuple: two attached clients
#: in one process are two separate load states, and one `active()` must not
#: decide what the other delivers. Keyed by an opaque per-guard token rather
#: than by the guard object, so a context cannot keep a guard alive, and
#: declared at module level rather than per instance, which is what the
#: `contextvars` documentation requires.
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar(
    "constraintrelay_active_skills", default={})


class _DeferredAwaitable(collections.abc.Coroutine):
    """A coroutine that records once it has run, and closes without leaking.

    Three constraints, and the obvious implementations satisfy only two.

    * **It must not leak.** Wrapping an already-created inner coroutine inside
      a second coroutine means closing the outer never runs its body, so the
      inner is never closed: `coroutine ... was never awaited`, one per
      abandoned call.
    * **It must still schedule like a coroutine.** A bare awaitable is not
      one. Measured: `asyncio.create_task(create(...))` worked before
      `attach()` and raised `TypeError: a coroutine was expected` after —
      the same class of harm as handing back a plain `def` where the SDK had
      an `async def`, which is the defect this object was introduced to fix.
      Registering as `collections.abc.Coroutine` is what `asyncio.iscoroutine`
      accepts, and driving goes through `send`/`throw` rather than `__await__`
      under a task — so the recording lives in the wrapped coroutine, not in
      `__await__`, or a task would record nothing.
    * **It must record at most once.** Awaiting the same object twice used to
      produce TWO ledger rows: the second await raised `cannot reuse already
      awaited coroutine`, the `except` branch ran, and a request that happened
      once was counted twice. The original error is preserved; the second row
      is not.

    `inspect.iscoroutine` still reports `False` — this is an ABC-registered
    coroutine, not a native one. `asyncio.iscoroutine`, `create_task`,
    `gather` and `await` all work; that one stricter predicate does not, and
    is named here rather than left to be discovered.
    """

    __slots__ = ("_inner", "_after", "_wrapped", "_recorded")

    def __init__(self, inner, after) -> None:
        self._inner = inner
        self._after = after
        self._wrapped = None
        self._recorded = False

    # -- the recording, once -----------------------------------------------
    def _record(self, raised: bool) -> None:
        if self._recorded:
            return
        self._recorded = True
        self._after(raised)

    async def _run(self):
        try:
            out = await self._inner
        except BaseException:
            self._record(True)
            raise
        self._record(False)
        return out

    def _coro(self):
        """The wrapped coroutine, built on first drive and never rebuilt."""
        if self._wrapped is None:
            self._wrapped = self._run()
        return self._wrapped

    # -- the coroutine protocol --------------------------------------------
    def __await__(self):
        return self._coro().__await__()

    def send(self, value):
        return self._coro().send(value)

    def throw(self, *args, **kwargs):
        return self._coro().throw(*args, **kwargs)

    def close(self) -> None:
        """Close both, in order. Either-or leaves a hole between them.

        This was written as a choice — close the wrapped coroutine if one
        exists, otherwise close the inner — and `__await__()` creates the
        wrapped one **without driving it**. A caller that builds the iterator
        and then abandons the object therefore hit the first branch, closed a
        coroutine that had never started (a no-op), and never reached the
        inner: `coroutine ... was never awaited`.

        Closing the wrapped one first matters when it HAS started, because
        unwinding it throws `GeneratorExit` into the `await self._inner` and
        closes the inner properly. Closing the inner afterwards is then a
        no-op. Closing a finished or already-closed coroutine is also a
        no-op. So doing both is safe in every state, and doing one is not.
        """
        if self._wrapped is not None:
            self._wrapped.close()
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


async def _await_then(awaitable, write):
    """Await, then record — recording even when the awaited call raises.

    The method was entered with this payload either way; what must not happen
    is a row for a coroutine body that never ran.
    """
    try:
        out = await awaitable
    except BaseException:
        write()
        raise
    write()
    return out


def _active_for(key: str) -> tuple:
    return tuple(_ACTIVE.get().get(key, ()))


class ProviderGuard:
    """A provider client with a guard attached.

    One boundary, one receipt per request.
    """

    def __init__(self, client, *, host: str, library, source,
                 receipts_path=None) -> None:
        if host in REFUSED_SURFACES:
            raise ProviderHostError(
                f"{host!r} is refused, not unsupported-by-omission: "
                + REFUSED_SURFACES[host])
        if host not in PROVIDER_PROFILES:
            raise ProviderHostError(
                f"{host!r} is not a provider surface this module has read. "
                f"Known: {sorted(PROVIDER_PROFILES)}. Refused with a stated "
                f"reason: {sorted(REFUSED_SURFACES)}. A surface is named here "
                "only when the messages, the tools and the boundary are all "
                "known; guessing one would produce a receipt about a payload "
                "nobody read.")
        self.host = host
        self._p = PROVIDER_PROFILES[host]
        self._client = client
        self.source_root = Path(source)
        self._lib = (library if hasattr(library, "skills")
                     else load_library(library))
        self._config = AdapterConfig(role=self._p["role"],
                                     position=self._p["position"],
                                     host=host)
        self.receipts_path = Path(receipts_path) if receipts_path else \
            Path(".constraintrelay") / "receipts.jsonl"
        self.ledger = RequestLedger(
            host=host,
            observed_paths=[self._p["send_path"]],
            unobservable_paths=list(self._p["unobservable_paths"]),
            observation_point=self._p["observation_point"])
        self.receipts: list = []
        #: `(request sequence, receipt)` for the newest recording, or None.
        #: ONE value, published under `_seq_lock`, so the number and the
        #: object it describes cannot be observed apart. `last_receipt`
        #: compares the sequence to the highest handed out, so a request that
        #: produced NO receipt reports `None` instead of the previous
        #: request's. See that property.
        self._last: "tuple[int, object] | None" = None
        #: Opaque identity for this guard's slot in the activation context.
        self._key = f"{host}:{uuid.uuid4().hex}"
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._owner = None
        self._original = None
        self._async_send = False
        self._installed = False

    # -- activation --------------------------------------------------------
    @contextmanager
    def active(self, *skills: str):
        """Declare which skills are active for the requests made inside.

        *skills* are skill ids relative to the source root — the same
        vocabulary as `--skill` on the command line. Nested calls add to the
        set rather than replacing it, and the previous set is restored on
        exit even if the body raises.
        """
        ids = tuple(str(s).strip().strip("/") for s in skills if str(s).strip())
        if not ids:
            raise ProviderHostError(
                "active() needs at least one skill id. An empty activation "
                "would read as 'nothing is due', which is a claim about the "
                "load state rather than a no-op.")
        current = _ACTIVE.get()
        merged = dict(current)
        merged[self._key] = tuple(
            dict.fromkeys(current.get(self._key, ()) + ids))
        token = _ACTIVE.set(merged)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    def active_skills(self) -> tuple:
        """What `active()` has declared for THIS guard, here, right now.

        "Here" is this thread or this async task. A thread started inside an
        `active()` block begins with a fresh context and therefore sees
        **nothing** active — its requests are recorded `NO_DECLARATION_DUE`
        rather than silently carrying another thread's activation. Async
        tasks copy the context at creation, so a task started inside the
        block does see it.
        """
        return _active_for(self._key)

    # -- install / uninstall ----------------------------------------------
    def install(self) -> "ProviderGuard":
        if self._installed:
            return self
        owner = self._client
        for attr in self._p["attach"][:-1]:
            owner = getattr(owner, attr, None)
            if owner is None:
                raise ProviderHostError(
                    f"this client has no `{'.'.join(self._p['attach'][:-1])}` "
                    f"attribute, so it is not the {self.host!r} shape. "
                    "Nothing was patched.")
        method = self._p["attach"][-1]
        original = getattr(owner, method, None)
        if not callable(original):
            raise ProviderHostError(
                f"`{'.'.join(self._p['attach'])}` is not callable on this "
                "client. Nothing was patched.")
        self._owner, self._original = owner, original
        #: Whether the attach point is declared `async def`. Checked here as
        #: well as on the returned object: `iscoroutinefunction` is the cheap
        #: signal, `isawaitable` on the result is the one a decorated or
        #: proxied `create` cannot slip past.
        self._async_send = inspect.iscoroutinefunction(original)

        if self._async_send:
            # An `async def` attach point must keep an `async def` wrapper.
            # A plain `def` returning a coroutine behaves the same under
            # `await` but reports `inspect.iscoroutinefunction(...) == False`,
            # so any caller that introspects the client — and frameworks do —
            # sees a sync method where the SDK has an async one. Measured:
            # before=True, after=False, restored=True. Changing the shape of
            # someone else's API is the same class of harm as mutating their
            # message list.
            async def _wrapped(*args, **kwargs):
                out = self._call(original, *args, **kwargs)
                if inspect.isawaitable(out):
                    return await out
                return out
        else:
            def _wrapped(*args, **kwargs):
                return self._call(original, *args, **kwargs)

        _wrapped.__name__ = getattr(original, "__name__", method)
        _wrapped.__doc__ = getattr(original, "__doc__", None)
        #: So a second `attach()` on the same client can see it is already
        #: wrapped rather than nesting two wrappers, which would emit two
        #: receipts and two ledger rows for one request.
        _wrapped._constraintrelay_guard = self
        setattr(owner, method, _wrapped)
        self._installed = True
        return self

    def uninstall(self) -> None:
        """Put the client back exactly as it was."""
        if self._installed and self._owner is not None:
            setattr(self._owner, self._p["attach"][-1], self._original)
        self._installed = False

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.uninstall()
        return False

    # -- the boundary ------------------------------------------------------
    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _call(self, original, *args, **kwargs):
        seq = self._next_seq()
        skills = _active_for(self._key)
        messages = kwargs.get(self._p["messages_kwarg"])
        tools = kwargs.get(self._p["tools_kwarg"])

        if not skills:
            return self._degrade(
                original, args, kwargs, seq, (),
                "no skill was active at this request, so the load state held "
                "nothing deliverable")

        if not isinstance(messages, list):
            # Positional-only or a shape this profile was not read against.
            # Pass through rather than reach into an unknown structure.
            return self._degrade(
                original, args, kwargs, seq, skills,
                f"the {self._p['messages_kwarg']!r} keyword was absent or not "
                f"a list (got {type(messages).__name__}), so this payload was "
                "not read and nothing was inserted")

        try:
            # The baseline is captured BEFORE anything can move it, and
            # INSIDE the fail-open boundary. Both halves were learned the
            # hard way: reading the digest after `prepare()` let an in-place
            # deletion be read back as "there were none before injection",
            # and capturing it outside this `try` meant a message that
            # refuses `deepcopy` — legal, JSON-serialisable, and real for
            # anything holding a lock or a file handle — raised straight
            # into the host. Measured: 0 provider calls, 0 ledger rows, the
            # developer's request dead. No ConstraintFlow failure may do
            # that.
            pre, pre_system_digest, pre_system_count = \
                self._capture_baseline(messages)
            asm, mans = self._assemble(skills)
            res = prepare(messages, asm, self._config)
            # And "nothing moved it" is now CHECKED rather than assumed: if
            # the caller's own list differs from the baseline, something
            # mutated it and every comparison downstream is against the wrong
            # original.
            if self._baseline_moved(messages, pre):
                raise AdapterError(
                    "the caller's message list was mutated during assembly, "
                    "so the pre-injection baseline no longer describes what "
                    "the host built. Nothing was sent with a capsule. The "
                    "list is NOT repaired: ConstraintRelay did not make the "
                    "change and restoring it would be the interference this "
                    "check exists to detect, so the host's request goes out "
                    "as whatever its own list now holds")
            ni = assert_non_interference(pre, res["messages"],
                                         tools_before=tools, tools_after=tools)
            if not ni["ok"]:
                failed = [k for k, v in ni.items()
                          if v is False and k != "ok"]
                raise AdapterError(
                    "non-interference failed, so nothing was sent with a "
                    f"capsule: {failed}")
        except Exception as exc:                      # never block the host
            return self._degrade(original, args, kwargs, seq, skills,
                                 f"{type(exc).__name__}: {exc}")

        payload = res["messages"]
        receipt = None
        try:
            receipt = verify(
                payload, asm, lib=self._lib, manifests=mans,
                observation_point=self._p["observation_point"],
                observable_boundary=self._p["observable_boundary"],
                host_system_sha256=pre_system_digest,
                host_system_count=pre_system_count,
                tools_offered=tools)
        except Exception as exc:
            return self._degrade(
                original, args, kwargs, seq, skills,
                f"the capsule was inserted but no receipt could be issued: "
                f"{type(exc).__name__}: {exc}",
                capsule_present=True, payload=payload)

        kwargs[self._p["messages_kwarg"]] = payload

        # The row is written only once the method has actually been ENTERED
        # with this payload, never before. Writing it first produced a
        # verified receipt for a request that had not happened — see
        # `_record_delivery`.
        if self._async_send:
            return self._await_then_record(original, args, kwargs, seq,
                                           skills, receipt)
        try:
            out = original(*args, **kwargs)
        except BaseException:
            # The method WAS entered with this payload, which is exactly what
            # the receipt's boundary claims; a transport failure afterwards
            # does not unmake the handoff. Dropping the row here lost the
            # request from the census entirely — the opposite error to the
            # async one, and found by the same check.
            self._record_delivery(
                seq, skills, receipt,
                detail="the call raised after the payload was handed over")
            raise
        if inspect.isawaitable(out):
            # The attach point was not declared async but handed back
            # something that has not run yet. Detecting only by
            # `iscoroutinefunction` is a gate written against the client I
            # thought of: a decorated, proxied or lazily-wrapped `create`
            # defeats it. Nothing is recorded until this awaits.
            return _DeferredAwaitable(
                out,
                lambda raised: self._record_delivery(
                    seq, skills, receipt,
                    detail=("the awaited call raised after the payload was "
                            "handed over") if raised else ""))
        self._record_delivery(seq, skills, receipt)
        return out

    async def _await_then_record(self, original, args, kwargs, seq, skills,
                                 receipt):
        return await self._record_after_await(original(*args, **kwargs), seq,
                                              skills, receipt)

    async def _record_after_await(self, awaitable, seq, skills, receipt):
        """Record once the coroutine has actually run, not when it was built.

        `AsyncOpenAI.chat.completions.create` returns a **coroutine**: its body
        has not executed, nothing has been handed to any transport, and a
        caller that never awaits it sends nothing at all. Recording at call
        time therefore wrote `DELIVERED_COMPLETE` with a verified receipt for
        a request the provider never received — measured: 0 calls reached the
        stand-in, census `{"DELIVERED_COMPLETE": 1}`, `receipt.ok` True.

        This is the same shape as the hermes defect where a non-streaming call
        site was handed a generator whose body never ran. A returned object is
        not a performed call.

        A raise still records: the method was entered with this payload, which
        is exactly what the receipt's boundary claims. What it must not do is
        record for a body that never ran.
        """
        try:
            out = await awaitable
        except BaseException:
            self._record_delivery(seq, skills, receipt,
                                  detail="the awaited call raised after the "
                                         "payload was handed over")
            raise
        self._record_delivery(seq, skills, receipt)
        return out

    def _record_delivery(self, seq, skills, receipt, detail: str = "") -> None:
        """One row for one request that reached the method. Never raises."""
        delivered = [v.skill_id for v in receipt.verdicts
                     if v.verified == AVAILABLE_COMPLETE and not v.violation]
        #: The receipt and the sequence it belongs to are published as ONE
        #: value, under the lock that hands sequences out. The first version
        #: of this fix appended outside the lock and stored only the number,
        #: leaving `last_receipt` to take the VALUE from `receipts[-1]` — a
        #: position — while pairing on an identity. Review found the
        #: interleaving on 2026-10-27: with two requests in flight, B appends,
        #: A appends, A stores 1, B stores 2; the number then reads current
        #: and `receipts[-1]` is A's receipt. Pairing by identity and then
        #: reading by position is not pairing by identity.
        #:
        #: `self.receipts` is still append-only and still in completion
        #: order; it is simply no longer the thing `last_receipt` reads.
        with self._seq_lock:
            self.receipts.append(receipt)
            self._last = (seq, receipt)
        # Bookkeeping comes after the delivery and must not be able to undo
        # it: a ledger or sink failure is this tool's problem, never an
        # exception raised into someone else's agent.
        try:
            row = self._row(seq, due_ids=self._due_ids(skills),
                            delivered_ids=delivered, receipt_ok=receipt.ok,
                            payload_sha256=receipt.payload_sha256,
                            capsule_present=receipt.capsule_message_index >= 0,
                            detail=detail)
            self._sink(receipt, row)
        except Exception:                                    # noqa: BLE001
            pass

    def _capture_baseline(self, messages) -> tuple:
        """A deep copy of the caller's list, plus what the receipt needs of it.

        Separate from `_call` so the whole capture — the copy, the digest and
        the count — sits in one place that is plainly inside the fail-open
        `try`. A `deepcopy` that raises is then a degraded request with a
        named reason, not an exception in someone else's agent.
        """
        pre = copy.deepcopy(list(messages))
        return (pre,
                self._host_system_digest(pre),
                sum(1 for m in pre
                    if isinstance(m, dict) and m.get("role") == "system"))

    @staticmethod
    def _baseline_moved(messages, pre) -> bool:
        """Did anything change the caller's list between baseline and send?

        A named method rather than an inline comparison, for the same reason
        `system_absent_by_construction` is a named function: a rule written
        inline is a rule no mutation can replace, and this one decides
        whether the digest and the count describe the payload the host
        actually built.
        """
        return list(messages) != pre

    def _degrade(self, original, args, kwargs, seq, skills, detail, *,
                 capsule_present=False, payload=None):
        """Pass the developer's call through, recording why nothing was added.

        Every early return in `_call` goes through here, so there is **one**
        place that can fail open rather than one per branch. The recording is
        itself wrapped: a mutation test showed the old shape could raise out
        of its own failure handler — `self._row(...)` sat inside the `except`
        block, so a bookkeeping error became the host's error, which is the
        exact outcome "failure never blocks" promises against.
        """
        def _write():
            try:
                row = self._row(seq, due_ids=self._due_ids(skills),
                                delivered_ids=(), receipt_ok=None,
                                capsule_present=capsule_present, detail=detail)
                # The sink gets the gaps too, with a null receipt. Writing
                # only the successes would make `constraintrelay receipts`
                # report a census of deliveries that never counted the
                # deliveries that did not happen — the one thing a reader is
                # looking for.
                self._sink(None, row)
            except Exception:                                # noqa: BLE001
                pass

        if payload is not None:
            kwargs[self._p["messages_kwarg"]] = payload
        try:
            out = original(*args, **kwargs)
        except BaseException:
            # The same omission the delivery path had, left behind on this
            # one: a provider that raises here HAD been called, and dropping
            # the row lost the request from the census entirely — 1 provider
            # call, 0 ledger rows. Found in review, not by me.
            _write()
            raise
        # Same rule as the delivery path, for the same reason: a row says a
        # request reached the method. On an async client the coroutine has
        # not run yet, so recording here would count a request a caller who
        # never awaits does not make. A gap row makes no delivery claim, but
        # it still claims a request, and one ledger must mean one thing.
        if inspect.isawaitable(out):
            return _DeferredAwaitable(out, lambda raised: _write())
        _write()
        return out

    # -- pieces ------------------------------------------------------------
    def _due_ids(self, skills) -> list:
        """The deliverable set the developer's activation produced.

        Computed from the activation alone, so a skill that later fails to
        resolve to a built version still counts as due and its absence shows
        up as `DUE_BUT_MISSING` rather than vanishing from the census.
        """
        return sorted(set(skills))

    def _assemble(self, skills):
        record = [_LoadedSkill(name=Path(s).name,
                               dir_path=str(self.source_root / s))
                  for s in skills]
        res = resolve_from_host_record(
            record, self._lib, source_roots=[self.source_root],
            host=self.host,
            # This host's record carries no body: the activation is a name and
            # a directory, not a loaded copy. `None` ("not checked") is the
            # honest value, and `container check` is where staleness is caught.
            require_host_body_match=False)
        if res.refused:
            raise AdapterError(
                "activated skill(s) resolve to no built version: "
                + "; ".join(f"{r.skill_id or r.name} ({r.reason})"
                            for r in res.refused))
        # `library_content_id()` is a method, not a field: it is recomputed
        # from what the library says rather than stored, which is what makes
        # a rebuild of the same bytes reproduce it.
        content_id = self._lib.library_content_id()
        reqs = res.requests(library_content_id=content_id)
        if not reqs:
            raise AdapterError(
                f"no activated skill resolved into this library ({content_id})")
        ids = [r.skill_id for r in reqs]
        mans = load_manifests(self.source_root, ids)
        asm = assemble(self._lib,
                       ContainerRequest(request_id=f"{self.host}-{self._seq}",
                                        skills=reqs),
                       source_root=self.source_root, manifests=mans)
        return asm, mans

    @staticmethod
    def _host_system_digest(original_messages: list) -> str:
        """Digest of the host's own first system message, before injection.

        Uses the receipt's **own** text reader rather than a second one, so
        the two sides cannot disagree about what the content was. When that
        reader returns no text — a system message carrying a content *list*,
        which OpenAI permits — this returns `""` on purpose: a digest of the
        empty string would match the same empty digest at verification time
        and report `host_system_message_unchanged: true` having compared
        nothing. Returning `""` instead degrades the delivery claim to
        `UNOBSERVABLE`, which is what actually happened.
        """
        for m in original_messages:
            if isinstance(m, dict) and m.get("role") == "system":
                text = _message_text(m)
                return sha256_text(text) if text else ""
        return ""

    def _row(self, seq, **kw) -> dict:
        return self.ledger.record(request_seq=seq,
                                  send_path=self._p["send_path"], **kw)

    def _sink(self, receipt, row) -> None:
        """Append the ledger row and its receipt, if there is one.

        *receipt* is `None` for a request that delivered nothing. `null` and a
        receipt are different facts and are written as different values — a
        row with no receipt is not a row whose receipt passed.
        """
        try:
            self.receipts_path.parent.mkdir(parents=True, exist_ok=True)
            with self.receipts_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"receipt": (receipt.as_dict()
                                                 if receipt is not None
                                                 else None),
                                     "ledger_row": row,
                                     "host": self.host},
                                    ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:                                    # noqa: BLE001
            # Including OSError. A sink that cannot write is a lost record,
            # not a reason to fail someone else's request.
            pass

    # -- reading back ------------------------------------------------------
    @property
    def last_receipt(self):
        """The receipt for the MOST RECENT request, or None if it made none.

        `None` is the documented answer when no receipt was produced — an
        unresolvable skill, nothing due, a payload this profile could not
        read. The previous version returned `self.receipts[-1]`, which is the
        last receipt this guard ever made, so on the second of those requests
        it handed back the PREVIOUS request's receipt. A developer trial on
        2026-10-27 drove it: one delivered request, then one naming a skill
        in no library, and the README's own recommended line

            r = guard.last_receipt; r is not None and r.ok

        evaluated **True** — reporting a verified delivery for a request that
        produced no receipt at all. That is worse than the `AttributeError`
        the line was written to avoid: it answers, and about the wrong
        request. On a FRESH guard the same path already returned `None`,
        which is why eight rounds of checks that each started clean never saw
        it.

        Paired by sequence number, never by list position — **including the
        value returned**. The sequence and the receipt are read as one tuple
        published under one lock; taking the number from here and the object
        from `receipts[-1]` was the first version of this fix and review
        reproduced the interleaving that breaks it, so the two halves are not
        allowed to come from different places.

        If two requests overlap on one guard and the earlier one finishes
        last, this answers `None` rather than attributing a receipt to the
        request that did not make it. `self.receipts` is unchanged and still
        holds every receipt in completion order.
        """
        with self._seq_lock:
            current, last = self._seq, self._last
        if last is None or last[0] != current:
            return None
        return last[1]

    def census(self) -> dict:
        return self.ledger.census()

    def completeness(self) -> dict:
        return self.ledger.completeness()


def attach(client, *, host: str, library, source,
           receipts_path=None) -> ProviderGuard:
    """Attach to one provider client, returning the guard that reverses it.

    What attaching does, in the order it happens on every call through the
    patched method: the declarations the developer activated are **injected**
    into the outgoing payload as a capsule in a non-elevating role after the
    host's own messages, and the payload that actually left is then handed to
    `constraintrelay.container_receipt.verify`, which issues the **receipt** — the record
    of which named declarations were in the request, at which version, with
    which confirmation state, and what the observation could not reach.

    So this is the one call that wires a client into both mechanisms. It does
    not stop, filter or rewrite anything the model does: **delivery is not
    compliance**, and nothing in this project measures whether a model follows
    a declaration.

    *host* must be named. A recorded profile then supplies the role and the
    position, so neither is defaulted silently and the receipt carries which
    profile it came from.
    """
    existing = _installed_guard(client, host)
    if existing is not None:
        raise ProviderHostError(
            "this client already has a guard attached. Two wrappers over one "
            "method "
            "would emit two receipts and two ledger rows for a single "
            "request, which reads as two requests. Call `uninstall()` on the "
            "existing guard first.")
    return ProviderGuard(client, host=host, library=library, source=source,
                         receipts_path=receipts_path).install()


def _installed_guard(client, host: str):
    p = PROVIDER_PROFILES.get(host)
    if not p:
        return None
    owner = client
    for attr in p["attach"][:-1]:
        owner = getattr(owner, attr, None)
        if owner is None:
            return None
    return getattr(getattr(owner, p["attach"][-1], None),
                   "_constraintrelay_guard", None)
