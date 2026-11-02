"""Codex CLI profile — source-verified, replay-pending.

Status: the load path was read from source at `openai/codex bcd6d9a` and is
implemented here, but the real loader has **never been executed**. There is no
Rust toolchain on this machine (`cargo`, `rustc`, `rustup` all absent) and the
Owner's ruling forbids bootstrapping one or building the ~1,473-crate workspace
to get one. Source can be cloned — the earlier "outbound network" blocker was a
bad `git ls-remote` invocation of mine, not the environment.

Consequences, enforced rather than documented:

* `replay_pending = True`, so `cf.cli` leaves it out of the default profile set
  and marks every finding advisory;
* an advisory profile can never set an enforcement exit code, so a
  replay-pending model cannot fail anybody's build;
* it does not count toward the replayed-profile release gate.

The mechanical qualification test that would move it to replayed is
`replays/codex_cli/CODEX_REPLAY_MANIFEST.md`. Until that is run on a machine
with Rust, every expected value here is oracle-predicted and unconfirmed.

Spec source (read, not executed): `codex-rs/core/src/agents_md.rs`,
`codex-rs/config/src/config_toml.rs` at `bcd6d9a`.
"""

from __future__ import annotations

from .profiles import InputFile, Prediction
from .verdict import Cause

DEFAULT_PROJECT_DOC_MAX_BYTES = 32 * 1024
AGENTS_MD_SEPARATOR = "\n\n--- project-doc ---\n\n"

#: Per directory the loader prefers an override file, then `AGENTS.md`, then
#: any configured fallback names.
LOCAL_AGENTS_MD_FILENAME = "AGENTS.override.md"
DEFAULT_AGENTS_MD_FILENAME = "AGENTS.md"


def _lossy(data: bytes) -> str:
    """Stand-in for Rust `String::from_utf8_lossy`.

    Python and Rust agree on a stray byte. Their grouping of a *truncated*
    multi-byte prefix into replacement characters is asserted only against
    Python here, and settling it against Rust is part of the pending replay.
    """
    return data.decode("utf-8", errors="replace")


class CodexCliProfile:
    """`AGENTS.md` chain under one shared byte budget.

    The budget is in **bytes**, unlike every other profile in this engine, and
    the separators and wrapper are applied *after* it — so the delivered text
    can legitimately be longer than the budget.
    """

    name = "codex-cli@bcd6d9a"
    verified_against = ("openai/codex bcd6d9a — SOURCE-VERIFIED ONLY, "
                        "replay pending: no Rust toolchain")
    replay_pending = True

    #: Declared per A1. The trust gate and the fallback-filename list depend on
    #: configuration this profile cannot see from a bare repository, so any
    #: file it would touch yields UNKNOWN rather than a confident verdict.
    UNMODELED = (
        "config.active_project trust state: an untrusted project drops every "
        "project doc, and trust is not visible in the repository",
        "project_doc_fallback_filenames and project_root_markers are "
        "configurable, so the load set and chain length are not determined by "
        "the repository alone",
    )

    def __init__(self, max_bytes: int = DEFAULT_PROJECT_DOC_MAX_BYTES) -> None:
        self.max_bytes = max_bytes

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        """Root-first chain order; each file cut to the remaining budget."""
        order = [f for f in files
                 if f.name in (LOCAL_AGENTS_MD_FILENAME, DEFAULT_AGENTS_MD_FILENAME)]
        others = [f for f in files if f not in order]

        out: dict[str, Prediction] = {}
        remaining = self.max_bytes
        for f in order:
            data = f.view.encode("utf-8")
            if remaining <= 0:
                out[f.path] = Prediction(
                    False, "", "not loaded: the shared 32 KiB project-doc budget "
                    "was already spent by files nearer the project root",
                    self.UNMODELED, cause=Cause.OMITTED_BY_BUDGET)
                continue
            if len(data) > remaining:
                data = data[:remaining]
            text = _lossy(data)
            if not text.strip():
                continue
            remaining = max(0, remaining - len(data))
            out[f.path] = Prediction(True, text, "", self.UNMODELED,
                                     cause=Cause.TRUNCATED)

        for f in others:
            out[f.path] = Prediction(
                False, "",
                f"not loaded: {f.name} is not an AGENTS.md project doc under "
                "this profile", self.UNMODELED, cause=Cause.NOT_IN_LOAD_SET)
        return out
