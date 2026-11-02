"""Phase 4 — make the harness's context-budget arithmetic include the capsule.

Phase 3's injection is added at `stream_chat` and the caller's message list is
deliberately never mutated, which keeps the capsule out of conversation history,
out of `transcript_*.jsonl` and away from `_auto_compact`'s summarizer. The
cost, reported at the time, is that `loop.py` decides when to compact from a
list that does not contain the capsule:

    tokens = estimate_tokens(messages)                  # capsule not counted
    if tokens > threshold * 0.5:  _microcompact(...)
    if tokens > threshold * 0.7:  _context_collapse(...)
    if tokens > threshold:        self._auto_compact(...)

So the harness under-counts every request by the size of the capsule — 433 to
475 tokens per turn with one active skill, ~6,279 with twenty — and therefore
compacts **later** than its own thresholds intend. Over-running a context window
is exactly the failure those thresholds exist to prevent.

The seam
--------
`loop.py` calls the module-level `estimate_tokens` by global lookup at every one
of its four call sites, so replacing `src.agent.loop.estimate_tokens` corrects
all of them at once — the three threshold tests in `run()` and the
`tokens_before` figure `_auto_compact` reports. The replacement calls the
original on `messages + [capsule_message]`, so the correction is the harness's
own arithmetic applied to the request that will actually be sent, not an
estimate of it.

Nothing else achieves this. A sentinel message inside `messages` would be
counted, but it would also be persisted into history, written to the transcript
and handed to the summarizer — the three things Phase 3's design exists to
avoid, and the three things Phase 4 was told to keep avoiding. Subclassing
`AgentLoop` means forking a 500-line `run()`. The harness exposes no budget hook.

**This patch is Vibe-adapter technical debt, not a ConstraintFlow architecture
requirement** — Owner's ruling at the close of Phase 4, recorded as
`CF-ADAPTER-DEBT-1` in `DEFECT_LEDGER.md`. The general requirement ConstraintFlow
should state to any harness is:

> a harness must let a delivery layer declare what it will add to a request
> *before* the context budget for that request is computed.

A monkeypatch is one adapter's way of satisfying that against a harness that
offers no such hook. No second adapter should copy it by default; the right
first move is to look for a budget hook and, failing that, to ask upstream for
one. The retirement condition is in the ledger entry: delete the patching path
and keep `pending_messages` as the declaration a real hook consumes.

This is a module-attribute patch and it is treated as one: it is idempotent,
it records what it replaced, `uninstall()` restores it, and
`AccountingPatch` is a context manager so a caller cannot forget. The harness
itself reads monkeypatched module attributes on purpose — `loop._override`
exists for exactly that — so patching at module level is an idiom this code
base anticipates, even though patching a *function* is a stronger step than
patching a constant.

Two limits, stated rather than discovered later:

* **Process-wide.** A patched `estimate_tokens` affects every `AgentLoop` in the
  interpreter, not just the one whose client was wrapped. The patch asks the
  wrapper for the pending capsule, and a loop with no active skills contributes
  zero, so the effect on an unrelated loop is nil — but the function really is
  global and `uninstall()` really does matter.
* **`swarm/worker.py` is untouched.** It has its own private
  `_estimate_tokens` and its own loop. Swarm support is explicitly out of scope
  for this phase, so the patch deliberately does not reach it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AccountingPatch:
    """A reversible replacement of a harness module's `estimate_tokens`."""

    module: Any
    original: Callable[[list], int]
    attribute: str = "estimate_tokens"
    installed: bool = True
    #: Measure the under-count but return the harness's own figure, so the run
    #: behaves as it did before Phase 4. Used by the before/after comparison.
    observe_only: bool = False
    #: One entry per corrected call: `(base_tokens, corrected_tokens)`. The
    #: difference is the under-count the harness would otherwise have had, and
    #: it is the number Phase 4 exists to drive to zero.
    calls: list[tuple[int, int]] = field(default_factory=list)

    @property
    def corrections(self) -> int:
        return sum(1 for base, corrected in self.calls if corrected != base)

    @property
    def total_gap_tokens(self) -> int:
        return sum(corrected - base for base, corrected in self.calls)

    @property
    def max_gap_tokens(self) -> int:
        return max((corrected - base for base, corrected in self.calls),
                   default=0)

    def uninstall(self) -> None:
        if not self.installed:
            return
        setattr(self.module, self.attribute, self.original)
        self.installed = False

    @property
    def peak_base_tokens(self) -> int:
        return max((base for base, _ in self.calls), default=0)

    @property
    def peak_true_tokens(self) -> int:
        return max((corrected for _, corrected in self.calls), default=0)

    def as_dict(self) -> dict:
        return {"attribute": self.attribute, "installed": self.installed,
                "observe_only": self.observe_only,
                "calls": len(self.calls), "corrections": self.corrections,
                "total_gap_tokens": self.total_gap_tokens,
                "max_gap_tokens": self.max_gap_tokens,
                "peak_tokens_as_the_loop_saw_them": self.peak_base_tokens,
                "peak_tokens_actually_sent": self.peak_true_tokens}

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "AccountingPatch":
        return self

    def __exit__(self, *exc) -> None:
        self.uninstall()


def install(loop_module, llm_wrapper, *, attribute: str = "estimate_tokens",
            observe_only: bool = False) -> AccountingPatch:
    """Correct *loop_module*'s token accounting for *llm_wrapper*'s capsule.

    *llm_wrapper* must expose `pending_messages(messages)` — Phase 3's
    `ConstraintAwareLLM` does — returning the message list as it will actually
    be sent. The same memoized capsule object serves the accounting and the
    request, so the two cannot disagree: `tests/test_phase4_accounting.py`
    asserts object identity, not equality.

    `observe_only=True` measures the under-count and then returns the harness's
    original figure anyway, so the run behaves exactly as it did before Phase 4.
    That is how the before/after comparison measures both arms with the same
    instrument instead of inferring the "before" gap from the "after" one.
    """
    original = getattr(loop_module, attribute)
    if getattr(original, "_constraintflow_patch", None) is not None:
        return original._constraintflow_patch           # already installed

    patch = AccountingPatch(module=loop_module, original=original,
                            attribute=attribute, observe_only=observe_only)

    def corrected(messages: list) -> int:
        base = original(messages)
        try:
            pending = llm_wrapper.pending_messages(messages)
        except Exception:
            # Accounting must never be the thing that breaks a run. An
            # unexpected failure here falls back to the harness's own figure,
            # which is the pre-Phase-4 behaviour.
            patch.calls.append((base, base))
            return base
        value = base if pending is messages else original(pending)
        patch.calls.append((base, value))
        return base if observe_only else value

    corrected._constraintflow_patch = patch             # type: ignore[attr-defined]
    corrected._constraintflow_original = original       # type: ignore[attr-defined]
    setattr(loop_module, attribute, corrected)
    return patch


def is_installed(loop_module, *, attribute: str = "estimate_tokens") -> bool:
    return getattr(getattr(loop_module, attribute), "_constraintflow_patch",
                   None) is not None
