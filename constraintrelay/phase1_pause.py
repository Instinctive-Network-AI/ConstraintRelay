"""The Phase-1 pause, in code rather than in a paragraph.

Owner ruling 2026-10-15 scopes development to the **constraint container**:
collect, deliver, verify. Three things are paused with it — automatic semantic
extraction, runtime task-constraint extraction, and every paid experiment.
Existing code, data and conclusions are kept untouched.

A pause written only in a document is a pause that the next runner forgets, so
it lives here, in code, and is installed on the send path itself.

`cf/provider_guard.py` deliberately does NOT consult it
-------------------------------------------------------
It did for six review rounds, and that edit drifted
`fieldtest/phase15/binding_manifest.json`'s `gate_implementation` binding —
which pins the guard's bytes on the stated grounds that *"a gate whose own
implementation is unbound can be edited to agree with anything"*. Five Phase-15
suites went red and stayed red while the Owner's ruling was open.

**Owner ruling 2026-10-16: keep the original pin and clear the drift.** So the
guard is restored byte-identical to `a4f0594e…` and the pause lives entirely
outside the pinned set. Nothing monkey-patches the guard either: changing a
frozen gate's behaviour while keeping its bytes identical would satisfy the pin
and defeat its purpose at the same time.

The scope check in the guard is the layer this gives up, and it was the weakest
of the three: it only ever fired for code that chose to call the guard. What
remains covers strictly more.

Two layers, and why the first one alone was not a gate
------------------------------------------------------
The first version of the send-side layer wrapped only the entry points I could
name. A fresh-context review found **five senders that never call
`provider_guard` at all**:
`v2/exercise/run_scenarios.py`, `run_round3.py`, `run_build.py`,
`fieldtest/phase16/run_arm.py` and `creds.py` all build their request and hand
it straight to `urllib.request.urlopen`. `tests/test_release_readiness.py`'s
static scan does not reach them either — it globs `fieldtest/**`, `cf/*` and
`demo/*`, and the v2 runners are in none of those. So the pause was a gate on
one entry point, which is a gate on nothing, and the finding is exactly the
shape this project keeps rediscovering: *enumerate backwards from the
irreversible line.*

The irreversible line is the send. So:

1. **the send itself** — `arm()` wraps the three chokepoints in
   `_CHOKEPOINTS` process-wide and refuses any request whose host belongs to a
   provider. Armed from `cf/__init__.py`, so importing anything in this package
   installs it, and a runner that never heard of `provider_guard` is still
   refused;
2. **each sender** — `require()` inside the five senders' own send functions,
   because three of the five never import `cf` and so never arm anything;
3. **the declared entry** — `constraintrelay.api.discover` refuses `SEMANTIC_EXTRACTION`.

Round 2 of the review then walked past *that*: the first cut of layer 2 wrapped
`urllib.request.urlopen` and `requests.post`, which is the list of wrappers I
could name, and `build_opener().open` reached the vendor for real while
`requests.request` / `Session.post` never touch `requests.post`. The lesson was
the round-1 lesson one level up — widen to the layer everything funnels
through, not to a longer list — and `_CHOKEPOINTS` is that layer.

Round 3 found two more: the `Session.request` wrapper was reading the URL
positionally while `requests` passes it as a keyword, so it was **dead code**
masked by the `putrequest` floor underneath it; and `api.deepseek.com.`, the
fully-qualified form, slipped past exact host matching. Hence `normalise_host`,
the positional-**or**-keyword lookup, and a gate suite that exercises each
chokepoint with the others **disarmed** — a dead wrapper must not be able to
hide behind a live one.

Layer 2 is deliberately **narrow**: it refuses provider hosts only, taken from
`provider_guard.ALLOWED_HOSTS` rather than from a second hand-written list, so
adding a provider cannot leave the pause behind. Non-provider HTTP is
untouched, because a pause on paid experiments is not a reason to break an
unrelated fetch.

Every refusal happens **before** the socket work: `OpenerDirector.open` and
`Session.request` are entry methods, and `http.client`'s `putrequest` runs
before `endheaders` → `send` → `connect`. So a refused call resolves no DNS and
opens no connection, which is what lets the gate suite drive provider URLs
without egress.

What this is, and what it is not
--------------------------------
It is a **fail-closed default**: no script in this tree can reach a provider
while `PAUSE.active` is true, including one written next week by someone who
never read the ruling. It is **not** a security control — `PAUSE.active` is a
module attribute and a deliberate edit lifts it. The threat it addresses is
accidental spend and forgotten scope, which is the threat that has actually
materialised in this project twice.

Escapes that remain, named rather than assumed: a module that bound a send
primitive *before* arming (`from urllib.request import urlopen` held across the
patch — no file in this tree does, and a static gate asserts it); a subprocess
that never imports `cf`, which is why `require()` also sits inside each sender;
a socket-level client that bypasses `http.client` entirely. **Three review
rounds have each found send-path escapes, so assume more exist.** The matrix in
`tests/test_container_phase1.py` is the mechanism for adding them cheaply, not
a proof of closure.

`lift()` exists for one purpose: testing the layers underneath the pause, which
would otherwise become unreachable and therefore untested. It names a reason,
and `tests/test_container_phase1.py` asserts that the pause refuses by default.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

#: Paused work streams. The names are the ruling's own.
SEMANTIC_EXTRACTION = "automatic_semantic_extraction"
RUNTIME_TASK_EXTRACTION = "runtime_task_constraint_extraction"
PAID_EXPERIMENTS = "paid_experiments"
PAUSED_STREAMS = (SEMANTIC_EXTRACTION, RUNTIME_TASK_EXTRACTION,
                  PAID_EXPERIMENTS)


class PausedError(RuntimeError):
    """Work that the Phase-1 ruling put down. Never caught to retry."""


@dataclass
class Phase1Pause:
    """One ruling, with its date and its scope."""

    active: bool = True
    ruled_on: str = "2026-10-15"
    ruled_by: str = "owner"
    streams: tuple = PAUSED_STREAMS
    scope_note: str = (
        "Phase 1 is the constraint container: collect, deliver, verify. "
        "Semantic extraction, runtime task-constraint extraction and paid "
        "experiments are paused. Code, data and prior conclusions are kept.")
    #: Every lift taken in this process, with its reason. Reported, so a run
    #: that lifted the pause cannot present itself as a run that did not.
    lifts: list = field(default_factory=list)

    def check(self, stream: str = PAID_EXPERIMENTS) -> "str | None":
        """`None` when the work may proceed, else a sentence saying why not."""
        if not self.active:
            return None
        if stream not in self.streams:
            return None
        return (f"{stream} is PAUSED by the Phase-1 ruling of {self.ruled_on} "
                f"({self.ruled_by}). {self.scope_note} Refusing rather than "
                f"spending: a paused stream that still runs is not paused.")

    def require(self, stream: str = PAID_EXPERIMENTS) -> None:
        why = self.check(stream)
        if why:
            raise PausedError(why)

    def as_dict(self) -> dict:
        return {"active": self.active, "ruled_on": self.ruled_on,
                "ruled_by": self.ruled_by, "streams": list(self.streams),
                "scope_note": self.scope_note,
                "lifts_taken_in_this_process": list(self.lifts),
                "_not_a_security_control": (
                    "this is a fail-closed default against accidental spend "
                    "and forgotten scope. `active` is a module attribute; a "
                    "deliberate edit lifts it.")}


#: The live ruling. One object, so there is one place to read its state.
PAUSE = Phase1Pause()


def check(stream: str = PAID_EXPERIMENTS) -> "str | None":
    return PAUSE.check(stream)


def require(stream: str = PAID_EXPERIMENTS) -> None:
    PAUSE.require(stream)


def _provider_hosts() -> frozenset:
    """Provider hosts, taken from the guard's own registry, normalised.

    Derived rather than re-listed: a second hand-written copy is a copy that
    goes stale the first time a provider is added, and then the pause silently
    stops covering it.
    """
    from .provider_guard import ALLOWED_HOSTS   # local: avoid an import cycle
    out: set = set()
    for hosts in ALLOWED_HOSTS.values():
        out |= {normalise_host(h) for h in hosts}
    return frozenset(out)


#: The label separators IDNA treats as a dot. Taken from the same set
#: `encodings.idna` uses (`[.。．｡]`) rather than re-derived by eye: round 4 of
#: the review sent `api。deepseek。com` — U+3002 IDEOGRAPHIC FULL STOP — through
#: `http.client` and the pause did not recognise the host, so the resolver was
#: asked for the vendor's name. U+FF0E and U+FF61 are the same trap.
IDNA_DOTS = ("。", "．", "｡")


def normalise_host(host: str) -> str:
    """Canonical host for comparison. Case, port, dots and IDNA all folded.

    Four spellings that reach the same vendor, each found the hard way:

    * `API.DEEPSEEK.COM` — case. Worked from the start.
    * `api.deepseek.com:443` — port. Worked from the start.
    * `api.deepseek.com.` — the root label spelled out. Round 3.
    * `api。deepseek。com` — an IDNA-equivalent separator. Round 4.

    Each label is then IDNA-encoded where it can be, so a punycode spelling and
    its Unicode original fold together. Both sides of the comparison go through
    this function, so the two cannot drift apart.

    Encoding failures are **kept, not dropped**: a label this cannot encode is
    returned as-is and simply will not match, which is the conservative
    direction — it never turns an unrecognised host into a recognised one.
    """
    h = (host or "").strip()
    if h.startswith("[") and "]" in h:              # [::1]:443
        h = h[1:h.index("]")]
    elif h.count(":") == 1:
        h = h.split(":")[0]
    h = h.casefold()
    for dot in IDNA_DOTS:
        h = h.replace(dot, ".")
    labels = [lab for lab in h.split(".") if lab]
    out = []
    for lab in labels:
        try:
            out.append(lab.encode("idna").decode("ascii").casefold())
        except (UnicodeError, ValueError):
            out.append(lab)
    return ".".join(out)


def _host_of(target) -> str:
    """Host of a urlopen/requests target: a str, a Request, or something else."""
    url = getattr(target, "full_url", None) or getattr(target, "url", None)
    if url is None and isinstance(target, str):
        url = target
    try:
        return normalise_host(urlparse(url or "").hostname or "")
    except (ValueError, TypeError):
        return ""


def _refuse_if_provider(target, where: str) -> None:
    host = _host_of(target)
    if not host:
        return
    if host not in _provider_hosts():
        return
    why = PAUSE.check(PAID_EXPERIMENTS)
    if why:
        raise PausedError(
            f"{where} to provider host {host!r} is refused: {why} This is the "
            "SEND-side layer of the pause, which exists because five senders "
            "in this tree never call constraintrelay.provider_guard.")


#: Installed interceptions, by name. Reported rather than assumed.
_ARMED: dict = {}

#: What `arm()` must cover, as *chokepoints* rather than conveniences.
#:
#: Round 1 patched `urllib.request.urlopen` and `requests.post`, which is the
#: list of entry points I happened to think of. Round 2 of the review walked
#: past all of it in three lines — `urllib.request.build_opener().open` reached
#: `api.deepseek.com` for real, and `requests.request` / `Session.post` never
#: touch `requests.post`. Same defect as the round-1 finding one level up: I
#: enumerated wrappers instead of the layer they funnel through.
#:
#: So each entry here is a method every wrapper above it must call:
#:
#: * `OpenerDirector.open` — `urlopen` is `_opener.open`, and every
#:   `build_opener()`/`OpenerDirector()` instance is one of these;
#: * `Session.request` — `requests.post/get/put/request` and every
#:   `Session.*` method land here;
#: * `HTTPConnection.putrequest` — the stdlib floor beneath both, reached by
#:   `http.client`'s own `request()` and by urllib3's override, so a client
#:   nobody listed is still caught.
#:
#: `tests/test_container_phase1.py` sweeps a matrix of send paths against this,
#: because a list of patches proves nothing without the paths that must hit it.
#: `(label, module, class, attribute, positional index or None, kwarg name)`.
#:
#: The URL is read from the positional slot **or** the keyword, whichever the
#: caller used. Round 3 of the review found the `Session.request` wrapper was
#: **dead code**: `requests.api.request` calls
#: `session.request(method=method, url=url, **kwargs)` — all keyword — so a
#: positional-only lookup saw nothing and `requests.post/get/request` were
#: caught only by the `putrequest` floor underneath. One chokepoint was masking
#: another, and the layered claim was wrong even though the matrix was green.
#: `tests/test_container_phase1.py` now exercises each chokepoint in
#: **isolation**, with the others temporarily disarmed, so a dead wrapper
#: cannot hide behind a live one again.
_CHOKEPOINTS = (
    ("urllib.OpenerDirector.open", "urllib.request", "OpenerDirector", "open",
     0, "fullurl"),
    ("requests.Session.request", "requests.sessions", "Session", "request",
     1, "url"),
    ("http.client.HTTPConnection.putrequest", "http.client", "HTTPConnection",
     "putrequest", None, None),
)


def disarm(label: str) -> bool:
    """Restore one chokepoint to its original. For isolation testing only."""
    import importlib
    for lab, module_name, cls_name, attr, _i, _k in _CHOKEPOINTS:
        if lab != label:
            continue
        try:
            cls = getattr(importlib.import_module(module_name), cls_name, None)
        except ImportError:
            return False
        fn = getattr(cls, attr, None) if cls is not None else None
        inner = getattr(fn, "_cf_pause_original", None)
        if inner is None:
            return False
        setattr(cls, attr, inner)
        _ARMED.pop(label, None)
        return True
    return False


def _install(label: str, module_name: str, cls_name: str, attr: str,
             url_argindex, url_kwarg) -> bool:
    """Wrap one chokepoint. `url_argindex=None` means read the host off self."""
    import importlib
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        return False
    cls = getattr(mod, cls_name, None)
    if cls is None:
        return False
    original = getattr(cls, attr, None)
    if original is None or getattr(original, "_cf_pause_original", None):
        return False

    def guarded(self, *a, **kw):
        if url_argindex is None and url_kwarg is None:
            host = normalise_host(getattr(self, "host", "") or "")
            if host and host in _provider_hosts():
                why = PAUSE.check(PAID_EXPERIMENTS)
                if why:
                    raise PausedError(
                        f"{label} to provider host {host!r} is refused: {why}")
        else:
            target = None
            if url_argindex is not None and len(a) > url_argindex:
                target = a[url_argindex]
            elif url_kwarg is not None and url_kwarg in kw:
                target = kw[url_kwarg]
            if target is not None:
                _refuse_if_provider(target, label)
        return original(self, *a, **kw)

    guarded._cf_pause_original = original
    setattr(cls, attr, guarded)
    _ARMED[label] = guarded
    return True


def arm() -> dict:
    """Wrap the send chokepoints so a provider host cannot be reached.

    Idempotent. Returns what it installed, so a caller can assert coverage
    rather than assume it.
    """
    for label, mod, cls, attr, idx, kwarg in _CHOKEPOINTS:
        if label not in _ARMED:
            _install(label, mod, cls, attr, idx, kwarg)
    return dict(_ARMED)


def armed() -> dict:
    """Which chokepoints are live right now, re-read from the modules.

    Returns a dict rather than a bool: "armed" was a single boolean keyed on
    `urlopen`, and it answered True while two whole client stacks were
    unguarded.
    """
    import importlib
    out = {}
    for label, module_name, cls_name, attr, _i, _k in _CHOKEPOINTS:
        try:
            cls = getattr(importlib.import_module(module_name), cls_name, None)
        except ImportError:
            out[label] = False
            continue
        fn = getattr(cls, attr, None) if cls is not None else None
        out[label] = bool(getattr(fn, "_cf_pause_original", None))
    return out


@contextmanager
def lift(reason: str):
    """Lift the pause for a named reason, then restore it.

    For exercising the layers beneath the pause. The reason is recorded on
    `PAUSE.lifts` and travels into every report that prints `PAUSE.as_dict()`,
    so a lifted run is never indistinguishable from an unlifted one.
    """
    if not reason.strip():
        raise PausedError("lifting the pause requires a reason")
    was = PAUSE.active
    PAUSE.active = False
    PAUSE.lifts.append(reason)
    try:
        yield PAUSE
    finally:
        PAUSE.active = was
