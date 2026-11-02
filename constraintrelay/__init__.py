"""ConstraintRelay — an independent source of truth for agent constraints.

Start at `constraintrelay.api`, which is the stable v0.1 surface. Everything else in this
package is either a layer behind it or paused research whose evidence is
preserved; `LIMITATIONS.md` says which is which.

The Phase-1 pause arms itself here
----------------------------------
Importing anything in this package installs the send-side layer of
`constraintrelay.phase1_pause` (Owner ruling 2026-10-15). It is armed at import rather than
by each runner because a review found five senders that never call
`constraintrelay.provider_guard` at all — a gate they have to opt into is a gate they can
forget. The interception refuses **provider hosts only**, so unrelated HTTP is
unaffected, and `constraintrelay.phase1_pause.lift()` releases it for a named reason.

The public entry point is `constraintrelay.attach`
-------------------------------------------------
Owner ruling 2026-10-26 fixed the public entry point at **`attach`** on the
product surface. The *package* it lives on changed on 2026-11-02: it was `cf`,
with the surface itself in a second top-level package called `constraintflow`,
and both were renamed into this one because both COLLIDE ON INSTALL — `cf`
with the unrelated `cf-python`, `constraintflow` with UIUC's project of that
name on PyPI. Two distributions that write the same `site-packages` directory
overwrite each other, so this is a packaging defect rather than a preference.

No compatibility alias is kept, by decision: there are no external users to
keep working. `import cf` and `import constraintflow` therefore raise
`ModuleNotFoundError`, which is the honest outcome — an alias that resolved to
this package would reintroduce exactly the name it was renamed to avoid.

The surface is resolved lazily in `__getattr__` below rather than imported at
module scope, for the reason the old cross-package forward also had:
`import constraintrelay` arms the pause above and must stay cheap, and the
submodules import each other with `from . import x`, which asks this module
for an attribute while it is still half-initialised.
"""

from __future__ import annotations

#: Product version. `constraintrelay.version.IMPLEMENTATION` is the separate research-build
#: identifier the qualification evidence was taken against.
#:
#: `0.1.0a1` — the developer preview, per the Owner's ruling of 2026-10-25.
#: `pyproject.toml`, this literal and `constraintrelay.api.VERSION` must agree, and
#: `v2/naming_gate.py` asserts all three against each other. The proposed tag
#: is `v0.1.0a1`; NOTHING is tagged and nothing is published.
__version__ = "0.1.0a1"

from . import phase1_pause as _phase1_pause   # noqa: E402

_phase1_pause.arm()


#: The re-entrancy guard that used to live here is GONE, and deliberately so.
#: It existed because the surface lived in a second package: resolving it from
#: inside `constraintrelay`'s own import re-entered that package while
#: `container_api` was half-built, raising `ImportError: cannot import name
#: 'AVAILABLE_COMPLETE' from partially initialized module`. With one package
#: there is no second module to re-enter — `_submodule()` is tried first and
#: resolves `from . import x` directly, so the window the flag protected can
#: no longer be opened.


#: The product surface, in ONE place. Until 2026-11-02 this list lived in a
#: second top-level package, `constraintflow`, and this module forwarded to it
#: lazily. Both packages were renamed into this one because they COLLIDED ON
#: INSTALL: `cf` with the unrelated `cf-python`, and `constraintflow` with
#: UIUC's project of that name on PyPI — two distributions writing the same
#: site-packages directory overwrite each other. There is no compatibility
#: alias, by decision: there are no external users to keep working.
#:
#: Resolved lazily below rather than imported here, for the reason the old
#: cross-package forward also had: `import constraintrelay` arms the pause
#: above and must stay cheap, and the submodules import each other with
#: `from . import x`, which asks this module for an attribute while it is
#: still half-initialised.
__all__ = [
    # the short path
    "attach", "ProviderGuard", "ProviderHostError",
    "PROVIDER_PROFILES", "REFUSED_SURFACES",
    # building and checking a library
    "build_library", "load_library", "load_manifests", "check_manifests",
    "write_manifest_draft", "assemble", "render", "verify",
    "ContainerRequest", "SkillRequest", "AdapterConfig",
    "Assembly", "Receipt", "SkillVerdict",
    "ContainerError", "ReceiptError",
    "AVAILABLE_COMPLETE", "FAILED", "UNDECLARED",
    # per-request accounting
    "RequestLedger", "STATUSES", "DELIVERED_COMPLETE", "DUE_BUT_MISSING",
    "NO_DECLARATION_DUE", "ATTRIBUTION_UNVERIFIED",
    "__version__",
]

#: Which submodule each surface name lives in. Declared so a name in `__all__`
#: that no module exports fails loudly at the first access instead of being
#: silently absent from `dir()`.
_SURFACE_MODULES = ("host_openai", "container_api", "request_ledger")


def _submodule(name: str):
    """`constraintrelay.<name>` as a module, or `None` if there is none.

    Tried **before** the surface, because `from . import container` inside a
    sibling must resolve to the module. A `ModuleNotFoundError` is swallowed
    only when it names the module being looked up: one raised *inside* an
    existing submodule is a real error and is re-raised.
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
    """Resolve `constraintrelay.<name>`: a submodule, then the surface.

    Only names in `__all__` are forwarded, so a typo raises `AttributeError`
    naming this module rather than reaching into a submodule's internals.
    """
    mod = _submodule(name)
    if mod is not None:
        return mod
    if name in __all__:
        import importlib
        for where in _SURFACE_MODULES:
            sub = importlib.import_module(f".{where}", __name__)
            if hasattr(sub, name):
                return getattr(sub, name)
        raise AttributeError(
            f"{name!r} is declared in {__name__}.__all__ but no module in "
            f"{_SURFACE_MODULES} exports it")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    import pkgutil
    return sorted({*globals(), *__all__,
                   *(m.name for m in pkgutil.iter_modules(__path__))})
