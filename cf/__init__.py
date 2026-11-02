"""ConstraintRelay — an independent source of truth for agent constraints.

Start at `cf.api`, which is the stable v0.1 surface. Everything else in this
package is either a layer behind it or paused research whose evidence is
preserved; `LIMITATIONS.md` says which is which.

The Phase-1 pause arms itself here
----------------------------------
Importing anything in this package installs the send-side layer of
`cf.phase1_pause` (Owner ruling 2026-10-15). It is armed at import rather than
by each runner because a review found five senders that never call
`cf.provider_guard` at all — a gate they have to opt into is a gate they can
forget. The interception refuses **provider hosts only**, so unrelated HTTP is
unaffected, and `cf.phase1_pause.lift()` releases it for a named reason.

The public entry point is `cf.attach`
------------------------------------
Owner ruling 2026-10-26: the import name for this preview is **`cf`** and the
public entry point is **`cf.attach`**. A developer trial the same day found
that `import cf; cf.attach(...)` raised `AttributeError` — the whole product
surface existed only on `constraintflow`, so the one name the ruling specifies
was the one that did not work.

It is resolved **lazily, from `constraintflow.__all__`**, for two reasons.
`import cf` stays as cheap as it was, which matters because importing this
package arms the pause above; and the name set is *derived* rather than
copied, so the two spellings of the surface cannot drift apart — a second list
here would be a second thing to forget. `constraintflow` imports from `cf.*`
submodules, so by the time anyone asks `cf` for an attribute this package is
fully initialised and there is no cycle.
"""

from __future__ import annotations

#: Product version. `cf.version.IMPLEMENTATION` is the separate research-build
#: identifier the qualification evidence was taken against.
#:
#: `0.1.0a1` — the developer preview, per the Owner's ruling of 2026-10-25.
#: `pyproject.toml`, this literal and `cf.api.VERSION` must agree, and
#: `v2/naming_gate.py` asserts all three against each other. The proposed tag
#: is `v0.1.0a1`; NOTHING is tagged and nothing is published.
__version__ = "0.1.0a1"

from . import phase1_pause as _phase1_pause   # noqa: E402

_phase1_pause.arm()


#: Re-entrancy guard. `constraintflow` imports `cf.*` submodules, and those
#: import each other with `from . import x` — which asks the import system for
#: `getattr(cf, "x")` BEFORE it has bound the attribute, so it arrives here.
#: Without this flag, resolving the surface during that window re-entered
#: `constraintflow` while `cf.container_api` was half-built and raised
#: `ImportError: cannot import name 'AVAILABLE_COMPLETE' from partially
#: initialized module`. A lazy forward must not be reachable from inside the
#: import it forwards to.
_resolving = False


def _surface():
    """The product surface, as `constraintflow` defines it. One definition."""
    global _resolving
    if _resolving:
        return None
    _resolving = True
    try:
        import constraintflow
        return constraintflow
    finally:
        _resolving = False


def _submodule(name: str):
    """`cf.<name>` as a module, or `None` if this package has no such module.

    Tried **before** the product surface, because `from . import container`
    inside a sibling must resolve to the module and must not reach for
    `constraintflow`. A `ModuleNotFoundError` is only swallowed when it names
    the module being looked up: one raised *inside* an existing submodule (a
    dependency it could not import) is a real error and is re-raised.
    """
    if name.startswith("_"):
        return None
    import importlib
    target = f"{__name__}.{name}"
    try:
        return importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == target:
            return None
        raise


def __getattr__(name: str):
    """Resolve `cf.<name>`: a submodule, then the product surface.

    `__all__` is answered from `constraintflow.__all__` — derived, never
    retyped, and resolved here rather than assigned above so that `import cf`
    does not pull the surface in. Otherwise only names `constraintflow`
    publishes are forwarded: a typo raises `AttributeError` naming *this*
    module instead of reaching into `constraintflow`'s internals, which is why
    this is a filtered forward and not a blanket delegation.
    """
    mod = _submodule(name)
    if mod is not None:
        return mod
    surface = _surface()
    if surface is not None:
        if name == "__all__":
            return list(surface.__all__)
        if name in surface.__all__:
            return getattr(surface, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    import pkgutil
    surface = _surface()
    return sorted({*globals(), *(surface.__all__ if surface else ()),
                   *(m.name for m in pkgutil.iter_modules(__path__))})
