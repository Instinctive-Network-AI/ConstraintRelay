"""The adapter registry.

Moved out of `cli.py` at v0.6 because three things now need it without needing
each other: `config` validates the `profile` named in `.constraintrelay.toml`,
`scenario` asks whether a profile's load set depends on the working directory,
and `budget` needs the profile's **native unit** (§11).

Two names per adapter, deliberately:

* `profile_id` — `"<family>/<loader-profile>"`, the form §4 uses in
  `.constraintrelay.toml`. It names a *loader surface*, not a commit, because
  §8 makes the loader fingerprint the compatibility key rather than the commit.
* `key` — the short CLI name, retained so existing invocations keep working.

`native_unit` is not cosmetic. Finding 9 in STATUS.md: one `🎭` costs 1 code
point, 4 UTF-8 bytes and 2 UTF-16 code units, so a margin printed without its
unit is wrong for most of this table. §11 allows exactly these three.
"""

from __future__ import annotations

from dataclasses import dataclass

from .codex import CodexCliProfile
from .conway import ConwayAutomatonProfile
from .openclaw import OpenClawProfile
from .profiles import HermesAgentProfile, VibeTradingProfile
from .vibe_trading_paged import VibeTradingPagedProfile

CODE_POINTS = "Unicode code points"
UTF8_BYTES = "UTF-8 bytes"
UTF16_UNITS = "UTF-16 code units"

#: v0.7 §5 requires a receipt to carry a **profile evidence level**. Three are
#: distinguished, and the middle one exists because `fieldtest/VERIFIER_PROVENANCE.md`
#: records a real asymmetry: hermes decides block membership from source spans
#: recovered out of the real loader's own output, while the other replayed profiles
#: still decide it by substring containment inside that file's delivered text.
#: Cross-file repetition cannot fool containment, but within-file repetition can,
#: so calling both "replayed" in a receipt would overstate three of four rows.
REPLAYED_SPAN = "replayed/span-verified"
REPLAYED_CONTAINMENT = "replayed/containment-verified"
SOURCE_VERIFIED = "source-verified only"


def measure_units(text: str, unit: str) -> int:
    """Length of *text* in one of §11's three native units.

    Finding 9 is the reason this is a function and not `len`: one `🎭` costs 1
    code point, 4 UTF-8 bytes and 2 UTF-16 code units, so a capsule size reported
    without its unit is wrong for two of the five profiles.
    """
    if unit == CODE_POINTS:
        return len(text)
    if unit == UTF8_BYTES:
        return len(text.encode("utf-8"))
    if unit == UTF16_UNITS:
        # Surrogate pairs count as two, which is what the loader counts.
        return len(text.encode("utf-16-le")) // 2
    raise KeyError(f"unknown native unit {unit!r}; §11 allows: "
                   f"{CODE_POINTS}, {UTF8_BYTES}, {UTF16_UNITS}")


@dataclass(frozen=True)
class Registered:
    key: str
    profile_id: str
    consumes: str              # "context" | "skills"
    build: object
    native_unit: str
    #: §8: the loader-relevant implementation surface, relative to the upstream
    #: checkout. Empty means no fingerprint surface has been recorded yet, which
    #: `fingerprint.py` reports as unrecorded rather than as valid.
    loader_files: tuple[str, ...] = ()
    #: §5: does moving the working directory change this profile's load set?
    #: False for skill loaders, whose load set is the activated skills.
    cwd_sensitive: bool = True
    replay_pending: bool = False
    #: Needs a caller declaration before it can say anything definite. A
    #: different condition from `replay_pending`: this profile *is* replayed, it
    #: just cannot see the agent's database from a repository.
    needs_declaration: str = ""
    #: §5: the receipt field. Defaults to the weaker replayed level, so a new
    #: profile has to claim span verification explicitly rather than inherit it.
    evidence_level: str = REPLAYED_CONTAINMENT
    #: CF-011: instruction filenames this profile's loader looks for in *ancestor*
    #: directories, not only the working directory. Empty for every profile whose
    #: load set is the working directory alone.
    ancestor_walk: tuple[str, ...] = ()
    #: Qualification ruling item 8: a generated artifact under `cf/data/`
    #: whose `files` map IS the loader-relevant fingerprint surface, derived by
    #: execution. When set it **replaces** `loader_files`, which for openclaw was
    #: one guessed entry out of 136 modules the loader actually resolves (CF-013).
    loader_surface_from: str = ""
    #: Pre-merge ruling item 6: loader inputs that come from **outside the
    #: repository**, so no amount of reading the repository can predict them. Each
    #: must be declared or snapshotted under `[target.external]`, or the affected
    #: result is indeterminate. These are not optional extras — hermes always
    #: appends `SOUL.md` from `HERMES_HOME`, and conway reads its enabled set from
    #: the agent's database, so a receipt that ignores them is describing a
    #: different load than the one that will happen.
    external_inputs: tuple[str, ...] = ()
    #: §8: declarations this profile needs before it can reach a definite
    #: endpoint. Each name is a `[target.parameters]` key. Non-empty and
    #: undeclared means the result is CONDITIONAL, which §8 fails closed, and the
    #: output must name the parameter — which is why these are names and not a
    #: boolean.
    required_parameters: tuple[str, ...] = ()
    #: CF-016: a **fixed roster** load set, in the loader's own order, for loaders
    #: whose candidate list is a literal in their source rather than a search.
    #: When set it replaces the shared `CONTEXT_FILENAMES` union, which was wrong
    #: for OpenClaw in four ways at once — extra files, a missing file, the wrong
    #: order, and absent files that still charge the shared budget.
    bootstrap_roster: tuple[str, ...] = ()
    #: Names from `bootstrap_roster` whose **absence** still occupies a slot and
    #: charges the shared budget. The complement is dropped before the budget is
    #: ever consulted, so the two sets are not interchangeable.
    absent_consumes_budget: tuple[str, ...] = ()
    #: Hermes-audit ruling item 4: reasons this adapter's qualification is **not
    #: COMPLETE**, whatever its gate list says. Non-empty is decisive and no gate can
    #: outvote it, because the thing being recorded here is that *the gate set itself*
    #: does not cover the adapter — which is exactly how OpenClaw came to show 12/12
    #: closed while its run path was unmodelled. A production receipt from a
    #: provisional adapter cannot exceed `DELIVERY_INDETERMINATE`.
    provisional: tuple[str, ...] = ()
    #: Run-path ruling item 6: newline handling **this harness's own reader** applies
    #: before its loader sees the text. Raw source coordinates stay raw; this is the
    #: per-profile transform into `loader_view`. The two shipped adapters disagree —
    #: OpenClaw reads bytes through Node, hermes reads through Python's
    #: `read_text(encoding="utf-8")`, which translates — and imposing either one on the
    #: other is CF-022. Defaults to universal, because every Python-implemented loader
    #: in the registry reads that way and a new one should have to opt out explicitly.
    source_newlines: str = "universal"
    #: Delivery regions where a larger budget can deliver *less*. §6 and §11 both
    #: reason about parameter ranges from endpoint outcomes, and that reasoning is
    #: invalid here: OpenClaw's policy digest selects greedily over the whole file,
    #: so raising the budget can let a high-priority line in and push out a line a
    #: smaller budget had kept. A profile naming a region here may not have a scalar
    #: margin inferred for a block delivered only through it.
    non_monotone_regions: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        return self.profile_id.split("/", 1)[0]


REGISTRY: dict[str, Registered] = {
    "hermes-agent": Registered(
        "hermes-agent", "hermes-agent/context-loader-v1", "context",
        HermesAgentProfile, CODE_POINTS,
        loader_files=("agent/prompt_builder.py", "tools/threat_patterns.py"),
        evidence_level=REPLAYED_SPAN,
        # Hermes-audit ruling items 1-3. `2517917de` is `heads/main` of the clone that
        # happened to be on this host, dated 2026-05-25 — not a tag, not a release, and
        # older than every published release tag. It was selected for replay
        # convenience, so it is a **legacy** profile; see RUNPATH_CLOSURE.md item 2.
        provisional=(
            "legacy profile: 2517917de is a local main-branch commit selected for "
            "replay convenience, not the Hermes version ConstraintRelay intends to "
            "support, and it predates every published release tag",
            "the run path is not traced to the final model-visible system prompt "
            "(ruling item 5); qualification stops at build_context_files_prompt",
            "no independent source-derived run-path audit exists for this commit "
            "(ruling item 7)"),
        ancestor_walk=(".hermes.md", "HERMES.md"),
        # `build_context_files_prompt` appends `load_soul_md()`, which reads
        # `HERMES_HOME/SOUL.md` — outside any repository. It is capped
        # independently of the project-context section, so it cannot displace
        # repository text; it is declared because "cannot displace" is a fact that
        # had to be checked, not an assumption a receipt may make.
        external_inputs=("HERMES_HOME/SOUL.md",)),
    "openclaw": Registered(
        "openclaw", "openclaw/context-loader-v1", "context",
        OpenClawProfile, UTF16_UNITS,
        # CF-013: `loader_files` is retained only as the entry point this surface
        # was derived FROM. The fingerprinted surface is the artifact below.
        loader_files=("dist/bootstrap-D7cACmY4.js",),
        loader_surface_from="openclaw_surface.json",
        # CF-012: raised to span-verified once the profile actually recovered
        # delivered source spans. The qualification gate does not read this
        # constant — it probes `predict` — so the two cannot disagree silently.
        evidence_level=REPLAYED_SPAN,
        # Run-path ruling: the twelve gates closed against the *materialization* path.
        # `resolveBootstrapContextForRun` sits above a filtering pipeline that can empty
        # the load set outright, and CF-023 is the false definite that follows.
        provisional=(
            "qualified against the materialization path only; the run path entered at "
            "resolveBootstrapContextForRun is unmodelled, and a subagent, cron, "
            "group-chat or lightweight run delivers less than the model predicts "
            "(CF-023)",
            "delivery is not traced to the final model-visible context; "
            "post-resolution injection behaviour is unmodelled (ruling item 5)"),
        # CF-016. The roster and its order are `WORKSPACE_BOOTSTRAP_FILENAMES`
        # (`workspace.ts:59-71`), confirmed by executing the real
        # `loadWorkspaceBootstrapFiles` through `replays/openclaw/loadset.mjs`.
        # `CLAUDE.md`, `TOOLS.md` and `.cursorrules` are *not* in it; `BOOTSTRAP.md`
        # is, and sits before `MEMORY.md`.
        # Node reads the file as bytes and `trimEnd` is the only normalisation, so a
        # CRLF file reaches the loader with its CRLFs intact. Verified against the real
        # loader: the delivered text for a CRLF `AGENTS.md` contains `\r\n`.
        source_newlines="raw",
        bootstrap_roster=("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md",
                          "BOOTSTRAP.md", "MEMORY.md"),
        # `USER.md` and `MEMORY.md` are dropped when absent by an
        # `exactWorkspaceEntryExists` check (`workspace.ts:1227-1231`); the other
        # four get a `missing: true` slot that costs budget.
        absent_consumes_budget=("AGENTS.md", "SOUL.md", "IDENTITY.md",
                                "BOOTSTRAP.md"),
        non_monotone_regions=("AGENTS.md policy digest",)),
    "vibe-trading": Registered(
        "vibe-trading", "vibe-trading/skill-tool-result-v1", "skills",
        VibeTradingProfile, CODE_POINTS,
        loader_files=("agent/src/agent/loop.py", "agent/src/agent/skills.py",
                      "agent/src/tools/load_skill_tool.py"),
        cwd_sensitive=False),
    "vibe-trading-paged": Registered(
        "vibe-trading-paged", "vibe-trading/skill-tool-result-paged-v1", "skills",
        VibeTradingPagedProfile, CODE_POINTS,
        loader_files=("agent/src/agent/loop.py", "agent/src/agent/skills.py",
                      "agent/src/config/limits.py",
                      "agent/src/tools/load_skill_tool.py"),
        cwd_sensitive=False),
    # Source-verified only. Excluded from the default set, and its findings
    # never set an enforcement exit code, so a replay-pending profile can never
    # fail somebody's build.
    "codex-cli": Registered(
        "codex-cli", "codex/project-doc-v1", "context",
        CodexCliProfile, UTF8_BYTES,
        loader_files=("codex-rs/core/src/agents_md.rs",
                      "codex-rs/config/src/config_toml.rs"),
        replay_pending=True, evidence_level=SOURCE_VERIFIED),
    "conway-automaton": Registered(
        "conway-automaton", "conway/skill-instructions-v1", "skills",
        ConwayAutomatonProfile, UTF16_UNITS,
        # Only the file this lane's own replay exercised. The surface is
        # incomplete until requalified against a checkout (CF-007).
        loader_files=("src/skills/loader.ts",),
        cwd_sensitive=False,
        needs_declaration="--conway-active",
        # `loader.ts:73` overwrites `enabled` from the agent's database, which no
        # repository can see.
        external_inputs=("agent-database/enabled-skills",)),
}

#: Profiles used when no target and no `--profile` is given: replayed, and able
#: to reach a definite verdict without extra input from the caller.
SHIPPED = [k for k, r in REGISTRY.items()
           if not r.replay_pending and not r.needs_declaration]

_BY_PROFILE_ID = {r.profile_id: k for k, r in REGISTRY.items()}


def resolve(name: str) -> str:
    """Registry key for a short key or a §4 `family/profile` id.

    Raises `KeyError` with the accepted spellings listed, because a typo in
    `.constraintrelay.toml` is a configuration error (§9 exit 1) and the message
    is the whole remedy.
    """
    if name in REGISTRY:
        return name
    if name in _BY_PROFILE_ID:
        return _BY_PROFILE_ID[name]
    raise KeyError(
        f"unknown profile {name!r}; accepted: "
        + ", ".join(sorted(list(REGISTRY) + list(_BY_PROFILE_ID))))


def profile_ids() -> list[str]:
    return sorted(_BY_PROFILE_ID)
