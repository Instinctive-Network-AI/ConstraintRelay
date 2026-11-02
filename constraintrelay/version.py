"""The implementation version, and why it is a module of its own.

v0.7 §10: *"Internal Impact must use the same ConstraintFlow implementation
version on both sides of a comparison. Differences must not arise merely because
the analyzer changed between Before and After."*

That is a real failure mode and not a theoretical one: a Before side evaluated by
an older analyzer and an After side evaluated by a newer one produces a diff that
reads as a repository regression when the only thing that changed was us. The
field test already hit the same shape from the other direction — protocol v1
results were invalidated rather than merged with v2 after CF-001, for exactly
this reason (`fieldtest/FREEZE_v2.md`).

So the version is recorded in every comparison and asserted equal, rather than
assumed equal because both sides happen to run in one process.

`SEMANTIC_VERSION` is the governing brief. `ANALYZER_VERSION` is this
implementation's delivery-affecting behaviour and must be bumped whenever a fix
changes a verdict — CF-001 and CF-004 were both such fixes.
"""

from __future__ import annotations

#: The governing semantic freeze. Pinned document:
#: docs/constraintflow/inbound/Brief_v0_7_2026-10-01.md
#: sha256 1c1bde5759c93290353d43c427b402d89a4fc9bd183ae2055bac8764a166ca81
SEMANTIC_VERSION = "0.7"

#: Bump on any change that can move a delivery verdict.
#:   1  v0.6 engine as frozen at commit ed2abcb (post CF-001, CF-004, CF-005)
#:   2  v0.7: store/capsule/receipt; no change to the per-block verdict path
ANALYZER_VERSION = 2

IMPLEMENTATION = f"constraintflow/{SEMANTIC_VERSION}+analyzer.{ANALYZER_VERSION}"


class VersionMismatch(Exception):
    """Two sides of an internal comparison were produced by different analyzers.

    §10 makes this an error rather than a warning: a comparison that cannot
    attribute its differences to the repository has no meaning, and reporting it
    as a finding would be the `definite false status` class of bug that §11
    calls the worst one.
    """


def require_same(before: str, after: str) -> None:
    if before != after:
        raise VersionMismatch(
            f"internal Impact compared {before!r} against {after!r}; §10 requires "
            f"the same implementation version on both sides, because otherwise a "
            f"difference cannot be attributed to the repository")
