"""ConstraintFlow container — the 0.2 public Python API.

**Developer preview.** The names in `__all__` are what 0.2 promises. Everything
else in `constraintrelay.container*` is internal: it works, it is readable, and it may move.

Five verbs, which is the whole product:

    build_library(...)    read skills -> a version-bound library file
    load_library(...)     read that file back
    resolve_from_host_record(...)
                          your host's load record -> skill ids and versions,
                          each resolved to one built version or refused
    load_manifests(...)   read the per-skill author manifests beside the skills
    assemble(...)         decide what is deliverable, per (skill, category)
    render(...)           the capsule text to put in a request
    verify(...)           a receipt, read off the request that actually left

Plus the request/config types you need to call them, and the adapter contract
in `constraintrelay.container_adapter`.

What this API does not do, by construction
------------------------------------------
No condition judging. No tool-level filtering. No approval, blocking or
verdicts. No elevation — the capsule travels in a non-elevating role at a
position the host configures explicitly. No network: nothing in the container
path imports `urllib`, `http`, `socket`, `requests` or `ssl`.

Three things are kept apart in the receipt, and must not be collapsed:
assembly succeeded, the capsule was verified at a named send boundary, and a
remote model received it. The third is never claimed — see
`Receipt.observable_boundary`.

Minimal use
-----------
    from constraintrelay.container_api import (build_library, load_library, load_manifests,
                                  ContainerRequest, SkillRequest, assemble,
                                  render, verify)

    build_library(source_root="skills-root", skill_ids=["skills/mine"],
                  source_version="v1", out="library.json")
    lib = load_library("library.json")
    mans = load_manifests("skills-root", ["skills/mine"])
    req = ContainerRequest(request_id="r1",
                           skills=(SkillRequest("skills/mine", "v1"),))
    asm = assemble(lib, req, source_root="skills-root", manifests=mans)
    capsule = render(asm)
    # ... put `capsule` in your request, send it, then:
    receipt = verify(payload, asm, lib=lib, manifests=mans,
                     observation_point="where you read the payload",
                     observable_boundary="what that reading cannot reach",
                     host_system_sha256=...)
"""

from __future__ import annotations

from pathlib import Path

from . import container as _c
from . import container_receipt as _r
from .container_adapter import AdapterConfig, AdapterError  # noqa: F401
from .container_attest import check as check_manifests      # noqa: F401
from .container_hostrecord import (HostRecordError,         # noqa: F401
                                   HostResolution, Refused, Resolved,
                                   resolve_from_host_record)
from .container_attest import write_draft as write_manifest_draft  # noqa: F401
from .container_build import build_library as _build
from .library import Library

__all__ = [
    # verbs
    "build_library", "load_library", "load_manifests", "assemble", "render",
    "verify", "check_manifests", "write_manifest_draft",
    "resolve_from_host_record",
    # request / config types
    "ContainerRequest", "SkillRequest", "AdapterConfig", "CarriedItem",
    # result types
    "Assembly", "SkillOutcome", "Receipt", "SkillVerdict",
    "HostResolution", "Resolved", "Refused",
    # categories and the vocabularies a caller has to compare against
    "CATEGORIES", "CAT_AUTHOR_CONFIRMED", "CAT_MACHINE_CANDIDATE",
    "CAT_RUNTIME_CANDIDATE", "CAT_REGULATORY_SOURCE",
    "OUTCOMES", "AVAILABLE_COMPLETE", "FAILED", "UNDECLARED",
    "FAILURE_REASONS", "DECLARATION_STATES",
    # errors
    "ContainerError", "ReceiptError", "AdapterError",
    "HostRecordError",
    "VERSION", "INTERNAL_NOTICE",
]

#: The container's own interface version. Separate from `constraintrelay.__version__`,
#: which is the v0.1 product's.
VERSION = "0.2.0-dev-preview"

INTERNAL_NOTICE = (
    "Only the names in constraintrelay.container_api.__all__ are promised by 0.2. Other "
    "symbols in constraintrelay.container, constraintrelay.container_receipt, constraintrelay.container_build and "
    "constraintrelay.container_attest are internal and may move.")

# -- re-exported types ------------------------------------------------------
ContainerRequest = _c.ContainerRequest
SkillRequest = _c.SkillRequest
CarriedItem = _c.CarriedItem
Assembly = _c.Assembly
SkillOutcome = _c.SkillOutcome
ContainerError = _c.ContainerError
Receipt = _r.Receipt
SkillVerdict = _r.SkillVerdict
ReceiptError = _r.ReceiptError

CATEGORIES = _c.CATEGORIES
CAT_AUTHOR_CONFIRMED = _c.CAT_AUTHOR_CONFIRMED
CAT_MACHINE_CANDIDATE = _c.CAT_MACHINE_CANDIDATE
CAT_RUNTIME_CANDIDATE = _c.CAT_RUNTIME_CANDIDATE
CAT_REGULATORY_SOURCE = _c.CAT_REGULATORY_SOURCE
OUTCOMES = _c.OUTCOMES
AVAILABLE_COMPLETE = _c.AVAILABLE_COMPLETE
FAILED = _c.FAILED
UNDECLARED = _c.UNDECLARED
FAILURE_REASONS = _c.FAILURE_REASONS
DECLARATION_STATES = _c.DECLARATION_STATES


# -- verbs ------------------------------------------------------------------
def build_library(*, source_root, skill_ids: list, source_version: str, out,
                  skills_dir: str = "skills",
                  reviewer: str = "developer:unnamed",
                  built_at: str = "") -> dict:
    """Read author-marked declarations from skills into a library file.

    Offline and deterministic: zero provider calls, and the returned summary
    says so rather than leaving it to be assumed. Rebuilding the same bytes
    reproduces the same `library_content_id`.
    """
    return _build(source_root=source_root, skills_dir=skills_dir,
                  skill_ids=list(skill_ids), source_version=source_version,
                  out=out, reviewer=reviewer, built_at=built_at)


def load_library(path) -> Library:
    """Read a library file back, verifying its recorded content id."""
    return Library.load(Path(path))


def load_manifests(source_root, skill_ids) -> dict:
    """`skill_id -> manifest` for each skill that has one beside it.

    A skill with no manifest is absent from the result, which is what makes
    `UNDECLARED` reachable. A manifest that exists and cannot be read comes
    back as an error object, not as an absence.
    """
    return _c.load_attestations(Path(source_root),
                               {s: s for s in skill_ids})


def assemble(library, request, *, source_root, manifests):
    """Decide what is deliverable for this request. Returns an `Assembly`.

    One outcome per `(skill, category)`. Confirmed declarations are delivered
    by default; any other category travels only if the request enabled it, and
    then labelled with its verbatim source beside it. Nothing is summarised,
    truncated or filtered.
    """
    return _c.assemble(library, request, source_root=Path(source_root),
                       attestations=manifests)


def render(assembly) -> str:
    """The capsule text for this assembly."""
    return _c.render_capsule(assembly)


def verify(payload: list, assembly, *, lib, manifests,
           observation_point: str, observable_boundary: str,
           host_system_sha256: str = "", tools_offered=None,
           load_attribution=None, host_system_count=None) -> Receipt:
    """A receipt for *assembly*, read off the payload that actually left.

    Both `observation_point` and `observable_boundary` are required; a receipt
    that does not say what it could see is not evidence. Re-derives every
    expectation from the library and the manifests rather than trusting the
    assembly, and searches the payload for exact bytes.

    *load_attribution* carries whose loads produced the declarations — see
    `constraintrelay.load_attribution`. Omitted, the receipt reports it as **null** and
    states that it establishes nothing about attribution.

    *host_system_count* is how many system messages the payload held BEFORE
    injection. Supply it and a payload that legitimately has none — the
    ordinary LangGraph shape — can satisfy non-elevation vacuously instead of
    degrading to `UNOBSERVABLE`. Omit it and the stricter behaviour is
    unchanged: without that number a DELETED host system message is
    indistinguishable from one that never existed.
    """
    return _r.verify(payload, assembly, lib=lib, attestations=manifests,
                     observation_point=observation_point,
                     observable_boundary=observable_boundary,
                     host_system_sha256=host_system_sha256,
                     tools_offered=tools_offered,
                     load_attribution=load_attribution,
                     host_system_count=host_system_count)
