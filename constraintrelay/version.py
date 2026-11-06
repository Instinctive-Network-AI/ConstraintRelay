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
#:   3  2026-11-05 format rename: every author- and user-facing name moved to
#:      the `constraintrelay` prefix — the declaration and capsule markers, the
#:      payload markers, the state directory, the config file, the attestation
#:      and selector sidecars, the receipt field names and the on-disk state
#:      keys. The per-block verdict path is untouched, but the BYTES delivered
#:      to the model and the bytes a receipt compares both changed, which is
#:      exactly what this number exists to make visible.
ANALYZER_VERSION = 3

#: The analyzer identity recorded inside every receipt and frozen artifact.
#:
#: Owner ruling 2026-11-05 item 3(J): *历史工件一律不改；新产生的回执使用
#: constraintrelay 身份并带新版本号。这是版本变化，不是改写历史署名。*
#:
#: So this is a version change and NOT a retroactive re-signing. Artifacts
#: already written keep `constraintflow/0.7+analyzer.2` as the identity of the
#: analyzer that actually produced them; anything produced from here carries
#: `constraintrelay/0.7+analyzer.3`. The two are deliberately distinguishable:
#: a receipt that claimed the new identity for output the old analyzer made
#: would be a forged signature, and `require_same` below would stop comparing
#: like with like.
IMPLEMENTATION = f"constraintrelay/{SEMANTIC_VERSION}+analyzer.{ANALYZER_VERSION}"


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
