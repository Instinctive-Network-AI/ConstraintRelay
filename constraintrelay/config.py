"""`.constraintflow.toml` — Brief v0.6 §4.

**Not a constraint registry.** It declares what the project wants ConstraintFlow
to evaluate: which qualified profile, from which working directory, with which
runtime parameters, and — where a qualified profile requires it — which
externally supplied state.

    version = 1

    [[target]]
    name = "hermes-production"
    profile = "hermes-agent/context-loader-v2"
    cwd = "."

    [target.parameters]
    context_window_tokens = 200000

The file contains evaluation configuration only. It contains no policy text, so
nothing in here can mark a constraint critical — that is §3's marker, in the
author's own instruction files.

Every problem in this file is a *configuration error*: §9 maps it to exit 1, not
to exit 2. A project that cannot be read has not been found compliant and has
not been found failing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import applicability
from .registry import REGISTRY, resolve

CONFIG_NAME = ".constraintflow.toml"

SUPPORTED_VERSION = 1

_TARGET_KEYS = {"name", "profile", "cwd", "parameters", "state", "context",
                "delivery", "external"}

#: v0.7 §6: the blocks whose initial-load delivery a capsule may not take away.
#: Project-level rather than per-target, because a protected instruction block is
#: a property of the repository and not of one evaluation.
_PROTECTED_KEYS = {"paths"}


class ConfigError(Exception):
    """Unreadable or invalid target configuration. Always exit 1."""


@dataclass(frozen=True)
class Target:
    """One declared evaluation target.

    `profile` keeps the author's spelling for the report; `profile_key` is the
    registry key it resolved to. §8 is the reason these are separate: the
    spelling names a loader surface, and whether that surface is still qualified
    is a fingerprint question answered elsewhere.
    """

    name: str
    profile: str
    profile_key: str
    cwd: str = "."
    parameters: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    #: v0.7 §1/§2: the build/session context this target declares. Only §15's
    #: static dimensions are accepted; `applicability.Context` resolves absences
    #: to "unknown", which §2.2 reads as applicable.
    context: dict = field(default_factory=dict)
    #: v0.7 §13: where this target's capsule is placed. The store owns the
    #: canonical text; *where* it goes depends on the harness, and §13 requires one
    #: canonical copy delivered through two different harnesses' mechanisms. Empty
    #: means "use the store's project-level `[delivery]`".
    delivery: dict = field(default_factory=dict)
    #: Pre-merge ruling item 6: declarations or snapshots for this profile's
    #: repository-external loader inputs. Keyed by the name the profile declares in
    #: `Registered.external_inputs`; the value is the declaration — a path, a
    #: snapshot digest, or the word `absent`. Undeclared leaves the qualification
    #: gate open, which makes the receipt indeterminate rather than wrong.
    external: dict = field(default_factory=dict)
    #: True when nothing in the repository declared this target — it came from a
    #: `--profile` on the command line, and `name` is therefore the profile key.
    #: Impact pairing has to know: `impact._scenario_key` deliberately excludes the
    #: profile so that "repository unchanged, harness changed" can pair, and an
    #: implied target would smuggle the profile back in through `name`.
    implied: bool = False

    @property
    def registered(self):
        return REGISTRY[self.profile_key]

    def root(self, project_root: Path) -> Path:
        return (project_root / self.cwd).resolve()


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    version: int
    targets: tuple[Target, ...]
    #: v0.7 §6 protected instruction blocks, as repo-relative glob patterns.
    protected: tuple[str, ...] = ()

    def target(self, name: str) -> Target:
        for t in self.targets:
            if t.name == name:
                return t
        raise ConfigError(
            f"no target named {name!r} in {self.path}; declared: "
            + ", ".join(t.name for t in self.targets))


def find(project_root: Path) -> Path | None:
    path = project_root / CONFIG_NAME
    return path if path.is_file() else None


def load(path: Path) -> ProjectConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    return parse(raw, path)


def parse(raw: dict, path: Path) -> ProjectConfig:
    """Validate a decoded config. Separate from `load` so tests need no file."""
    if "version" not in raw:
        raise ConfigError(f"{path}: missing `version`")
    version = raw["version"]
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"{path}: version {version!r} is not supported by this build "
            f"(expected {SUPPORTED_VERSION})")

    declared = raw.get("target")
    if declared is None:
        raise ConfigError(f"{path}: no `[[target]]` declared; there is nothing "
                          f"to evaluate")
    if not isinstance(declared, list):
        raise ConfigError(f"{path}: `target` must be an array of tables "
                          f"(`[[target]]`), not a single table")

    targets: list[Target] = []
    seen: set[str] = set()
    for i, entry in enumerate(declared):
        where = f"{path}: target #{i + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: must be a table")
        unknown = set(entry) - _TARGET_KEYS
        if unknown:
            # Rejected rather than ignored: a silently ignored key is how a
            # project ends up believing it declared state it never declared.
            raise ConfigError(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                f"accepted: {', '.join(sorted(_TARGET_KEYS))}")
        for required in ("name", "profile"):
            if not entry.get(required):
                raise ConfigError(f"{where}: missing `{required}`")
        name = str(entry["name"])
        if name in seen:
            raise ConfigError(f"{where}: duplicate target name {name!r}")
        seen.add(name)

        profile = str(entry["profile"])
        try:
            key = resolve(profile)
        except KeyError as exc:
            raise ConfigError(f"{where}: {exc.args[0]}") from exc

        cwd = str(entry.get("cwd", "."))
        if Path(cwd).is_absolute():
            raise ConfigError(f"{where}: `cwd` must be relative to the project "
                              f"root, got {cwd!r}")
        for block, label in (("parameters", "parameters"), ("state", "state"),
                             ("context", "context"), ("delivery", "delivery"),
                             ("external", "external")):
            if block in entry and not isinstance(entry[block], dict):
                raise ConfigError(f"{where}: `[target.{label}]` must be a table")

        context = dict(entry.get("context", {}))
        for dim in sorted(context):
            if dim in applicability.DEFERRED:
                raise ConfigError(
                    f"{where}: `[target.context]` declares {dim!r}, which v1 does "
                    f"not support — {applicability.DEFERRED[dim]}. §15 defers it "
                    f"to the runtime layer.")
            if dim not in applicability.SUPPORTED:
                raise ConfigError(
                    f"{where}: `[target.context]` unknown dimension {dim!r}; v1 "
                    f"supports: {', '.join(applicability.SUPPORTED)}")

        delivery = dict(entry.get("delivery", {}))
        if delivery:
            # Validated by the store's own parser rather than a second copy of the
            # rules, so a per-target override cannot accept a mode the store would
            # reject.
            from . import store as store_mod
            try:
                store_mod._parse_delivery(delivery, path)
            except store_mod.StoreError as exc:
                raise ConfigError(f"{where}: `[target.delivery]`: {exc}") from exc

        external = {k: str(v) for k, v in dict(entry.get("external", {})).items()}
        known = set(REGISTRY[key].external_inputs)
        stray = sorted(set(external) - known)
        if stray:
            # Rejected rather than ignored: a declaration for an input this loader
            # never reads would read as coverage the project does not have.
            raise ConfigError(
                f"{where}: `[target.external]` declares {', '.join(stray)}, which "
                f"{REGISTRY[key].profile_id} does not read. This loader's "
                f"repository-external inputs are: "
                + (", ".join(known) if known else "(none)"))

        targets.append(Target(
            name=name, profile=profile, profile_key=key, cwd=cwd,
            parameters=dict(entry.get("parameters", {})),
            state=dict(entry.get("state", {})),
            context=context, delivery=delivery, external=external))

    protected = _parse_protected(raw.get("protected"), path)
    return ProjectConfig(path=path, version=version, targets=tuple(targets),
                         protected=protected)


def _parse_protected(raw: object, path: Path) -> tuple[str, ...]:
    """`[protected] paths = [...]` — v0.7 §6.

    Absent means no block is protected, which is the honest default: §6 fails a
    capsule for taking delivery away from a *configured* protected block, and
    inventing a protected set would make the tool fail builds over material the
    project never asked it to defend.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: `[protected]` must be a table")
    unknown = set(raw) - _PROTECTED_KEYS
    if unknown:
        raise ConfigError(f"{path}: `[protected]` unknown key(s) "
                          f"{', '.join(sorted(unknown))}; accepted: "
                          f"{', '.join(sorted(_PROTECTED_KEYS))}")
    paths = raw.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ConfigError(f"{path}: `[protected] paths` must be a list of strings")
    for p in paths:
        if not p.strip():
            raise ConfigError(f"{path}: `[protected] paths` has an empty pattern")
        if Path(p).is_absolute():
            raise ConfigError(f"{path}: protected pattern {p!r} must be relative "
                              f"to the project root")
    return tuple(paths)
