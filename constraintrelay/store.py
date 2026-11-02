"""The authoritative Constraint Store — v0.7 §3, §7, §16.

This supersedes v0.6 §3. `markers.py` opens with *"v1 has no central constraint
registry"*; v0.7 §3 introduces one and makes it authoritative. Both statements
cannot hold, and §11 resolves the conflict in v0.7's favour. Markers are not
deleted — §16 makes them the **import path** — but after import the store is the
authority and the file is at most a mirror of it.

Why that matters beyond bookkeeping: under v0.6 the same sentence living in two
instruction files was a `DUPLICATE_ID` configuration error, and a sentence edited
in one file and not the other was indistinguishable from an intentional change.
With one canonical copy, `MODIFIED` is a store-version event and a repository
file that disagrees with the store is **drift**, which is an error rather than a
second opinion.

File
----
`.constraintflow/store.toml`, TOML because `.constraintflow.toml` already is and
`tomllib` is in the standard library.

    version = 1

    [delivery]
    mode = "managed_mirror"
    mirror_path = "AGENTS.md"

    [[constraint]]
    id = "C-001"
    text = "Research outputs must not be used directly as live-production actions."
    version = 1
    status = "active"
    provenance = "imported_source:AGENTS.md:3 (2026-10-01)"

    [constraint.scope]
    harness_target = ["hermes-production"]

§7 is implemented as accounting and nothing else
------------------------------------------------
`machine_enforcement_declared` and `enforcement_point` are recorded and reported.
They do **not** affect capsule membership, and `capsule.py` never reads them. §7
names the failure mode this prevents — *not shown to the model and not actually
enforced anywhere* — so the fields are deliberately inert here rather than
plumbed in and then suppressed somewhere further along, where a later edit could
quietly connect them.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .applicability import Scope, ScopeError, parse_scope

STORE_DIR = ".constraintflow"
STORE_NAME = "store.toml"
SUPPORTED_VERSION = 1

ACTIVE = "active"
RETIRED = "retired"
STATUSES = (ACTIVE, RETIRED)

#: §3's migration modes, on the output side.
IMPORTED_SOURCE = "imported_source"
MANAGED_MIRROR = "managed_mirror"
EXTERNAL_GENERATOR = "external_generator"
NO_MIRROR = "none"
DELIVERY_MODES = (MANAGED_MIRROR, EXTERNAL_GENERATOR, NO_MIRROR)

_CONSTRAINT_KEYS = {"id", "text", "version", "status", "provenance", "scope",
                    "machine_enforcement_declared", "enforcement_point"}
_DELIVERY_KEYS = {"mode", "mirror_path", "generator", "source_layer"}


class StoreError(Exception):
    """An unreadable or invalid store. Always a configuration error (exit 1)."""


@dataclass(frozen=True)
class StoredConstraint:
    """§3's minimum record, plus §7's accounting fields."""

    id: str
    text: str                      # canonical text; the store owns it
    version: int
    status: str
    provenance: str
    scope: Scope
    machine_enforcement_declared: bool = False
    enforcement_point: str = ""

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def canonical_text(self) -> str:
        """Normalised exactly as far as `markers.canonical_text` was: each line's
        trailing whitespace is not touched, but the whole text is stripped. Any
        further normalisation would make drift undetectable, which is the one
        thing §3 asks this layer to detect."""
        return self.text.strip()

    @property
    def canonical_sha256(self) -> str:
        """Pre-merge ruling item 6: a receipt carries the constraint's hash.

        Over `canonical_text`, so a whitespace-only edit to the store does not
        change it while any edit to the words does. The id answers *which
        constraint*; this answers *which wording of it*, and the pair is what makes
        a stored receipt checkable after the fact instead of merely archived.
        """
        return hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {"id": self.id, "version": self.version,
                "canonical_sha256": self.canonical_sha256, "status": self.status,
                "provenance": self.provenance, "scope": self.scope.describe(),
                "machine_enforcement_declared": self.machine_enforcement_declared,
                "enforcement_point": self.enforcement_point or None}


@dataclass(frozen=True)
class Delivery:
    """How the store reaches a repository, §3's migration modes.

    `external_generator` carries `generator` and `source_layer` because §3's rule
    is *do not directly patch a generated output file that another tool will
    overwrite* — so the only useful thing ConstraintFlow can say about such a
    project is which layer to integrate at, and it cannot say that unless the
    project declared it.
    """

    mode: str = NO_MIRROR
    mirror_path: str = ""
    generator: str = ""
    source_layer: str = ""

    def describe(self) -> str:
        if self.mode == MANAGED_MIRROR:
            return f"managed mirror in {self.mirror_path}"
        if self.mode == EXTERNAL_GENERATOR:
            return (f"external generator {self.generator or '(unnamed)'}; "
                    f"integrate at {self.source_layer or '(source layer not declared)'}")
        return "no repository mirror declared"


@dataclass(frozen=True)
class Store:
    path: Path
    version: int
    constraints: tuple[StoredConstraint, ...]
    delivery: Delivery

    @property
    def by_id(self) -> dict[str, StoredConstraint]:
        return {c.id: c for c in self.constraints}

    @property
    def active(self) -> tuple[StoredConstraint, ...]:
        return tuple(c for c in self.constraints if c.active)


def store_path(project_root: Path) -> Path:
    return project_root / STORE_DIR / STORE_NAME


def find(project_root: Path) -> Path | None:
    p = store_path(project_root)
    return p if p.is_file() else None


def load(path: Path) -> Store:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"{path}: cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise StoreError(f"{path}: not valid TOML: {exc}") from exc
    return parse(raw, path)


def parse(raw: dict, path: Path) -> Store:
    if "version" not in raw:
        raise StoreError(f"{path}: missing `version`")
    if raw["version"] != SUPPORTED_VERSION:
        raise StoreError(f"{path}: store version {raw['version']!r} is not "
                         f"supported by this build (expected {SUPPORTED_VERSION})")

    delivery = _parse_delivery(raw.get("delivery"), path)

    declared = raw.get("constraint")
    if declared is None:
        raise StoreError(f"{path}: no `[[constraint]]` entries; an empty store "
                         f"cannot be the authority for anything")
    if not isinstance(declared, list):
        raise StoreError(f"{path}: `constraint` must be an array of tables "
                         f"(`[[constraint]]`)")

    out: list[StoredConstraint] = []
    seen: set[str] = set()
    for i, entry in enumerate(declared):
        where = f"{path}: constraint #{i + 1}"
        if not isinstance(entry, dict):
            raise StoreError(f"{where}: must be a table")
        unknown = set(entry) - _CONSTRAINT_KEYS
        if unknown:
            raise StoreError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                             f"accepted: {', '.join(sorted(_CONSTRAINT_KEYS))}")
        cid = str(entry.get("id") or "").strip()
        if not cid:
            raise StoreError(f"{where}: missing `id`; identity is the id and never "
                             f"the text (v0.6 §3, retained)")
        if cid in seen:
            raise StoreError(f"{where}: duplicate id {cid!r}")
        seen.add(cid)
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            raise StoreError(f"{where}: `text` must be non-empty; the store holds "
                             f"the canonical text, so there is nowhere else for it")
        version = entry.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise StoreError(f"{where}: `version` must be a positive integer")
        status = str(entry.get("status", ACTIVE))
        if status not in STATUSES:
            raise StoreError(f"{where}: `status` must be one of "
                             f"{', '.join(STATUSES)}, got {status!r}")
        provenance = str(entry.get("provenance", "")).strip()
        if not provenance:
            raise StoreError(f"{where}: missing `provenance`; §3 lists it in the "
                             f"minimum record, and an unattributed canonical copy "
                             f"cannot be reviewed")
        mech = entry.get("machine_enforcement_declared", False)
        if not isinstance(mech, bool):
            raise StoreError(f"{where}: `machine_enforcement_declared` must be a "
                             f"boolean")
        point = str(entry.get("enforcement_point", "")).strip()
        if point and not mech:
            raise StoreError(f"{where}: `enforcement_point` is set but "
                             f"`machine_enforcement_declared` is false")
        try:
            scope = parse_scope(entry.get("scope"), where)
        except ScopeError as exc:
            raise StoreError(str(exc)) from exc

        out.append(StoredConstraint(
            id=cid, text=text, version=version, status=status,
            provenance=provenance, scope=scope,
            machine_enforcement_declared=mech, enforcement_point=point))

    return Store(path=path, version=raw["version"], constraints=tuple(out),
                 delivery=delivery)


def _parse_delivery(raw: object, path: Path) -> Delivery:
    if raw is None:
        return Delivery()
    if not isinstance(raw, dict):
        raise StoreError(f"{path}: `[delivery]` must be a table")
    unknown = set(raw) - _DELIVERY_KEYS
    if unknown:
        raise StoreError(f"{path}: `[delivery]` unknown key(s) "
                         f"{', '.join(sorted(unknown))}; accepted: "
                         f"{', '.join(sorted(_DELIVERY_KEYS))}")
    mode = str(raw.get("mode", NO_MIRROR))
    if mode not in DELIVERY_MODES:
        raise StoreError(f"{path}: `[delivery] mode` must be one of "
                         f"{', '.join(DELIVERY_MODES)}, got {mode!r}")
    mirror_path = str(raw.get("mirror_path", ""))
    generator = str(raw.get("generator", ""))
    source_layer = str(raw.get("source_layer", ""))
    if mode == MANAGED_MIRROR and not mirror_path:
        raise StoreError(f"{path}: `managed_mirror` needs `mirror_path`")
    if mode == MANAGED_MIRROR and Path(mirror_path).is_absolute():
        raise StoreError(f"{path}: `mirror_path` must be relative to the project root")
    if mode == EXTERNAL_GENERATOR and not generator:
        raise StoreError(
            f"{path}: `external_generator` needs `generator` — §3 forbids patching "
            f"a generated output file, so the tool that owns it has to be named "
            f"before anything can be placed")
    return Delivery(mode=mode, mirror_path=mirror_path, generator=generator,
                    source_layer=source_layer)


# ---------------------------------------------------------------------------
# §16 migration
# ---------------------------------------------------------------------------

def render_store(constraints: "list[StoredConstraint]", delivery: Delivery) -> str:
    """Serialise a store. Hand-written rather than via a TOML writer because the
    standard library has no writer and the file is meant to be read and edited by
    the author — so comments and a stable key order matter more than generality.
    """
    lines = [f"version = {SUPPORTED_VERSION}", ""]
    if delivery.mode != NO_MIRROR:
        lines.append("[delivery]")
        lines.append(f'mode = "{delivery.mode}"')
        if delivery.mirror_path:
            lines.append(f'mirror_path = "{_esc(delivery.mirror_path)}"')
        if delivery.generator:
            lines.append(f'generator = "{_esc(delivery.generator)}"')
        if delivery.source_layer:
            lines.append(f'source_layer = "{_esc(delivery.source_layer)}"')
        lines.append("")
    for c in constraints:
        lines.append("[[constraint]]")
        lines.append(f'id = "{_esc(c.id)}"')
        lines.append(f'text = """\n{c.canonical_text}\n"""')
        lines.append(f"version = {c.version}")
        lines.append(f'status = "{c.status}"')
        lines.append(f'provenance = "{_esc(c.provenance)}"')
        if c.machine_enforcement_declared:
            lines.append("machine_enforcement_declared = true")
            if c.enforcement_point:
                lines.append(f'enforcement_point = "{_esc(c.enforcement_point)}"')
        if not c.scope.global_:
            lines.append("")
            lines.append("[constraint.scope]")
            for dim, values in sorted(c.scope.dimensions.items()):
                rendered = ", ".join(f'"{_esc(v)}"' for v in values)
                lines.append(f"{dim} = [{rendered}]")
        lines.append("")
    return "\n".join(lines)


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def importable(scan, *, when: str) -> list[StoredConstraint]:
    """§16: turn an author's explicit markers into store entries.

    Only `markers.CriticalConstraint`s that carry an **id** are returned. Two
    things are therefore never imported, and both are deliberate:

    * a legacy marker with no id — it has no identity to key a canonical copy on;
    * anything matched by `scan --critical <regex>` — §16: *"ConstraintFlow must
      not automatically promote heuristic high-signal candidates into the
      store."* A regex hit is a candidate for the author to look at, not an
      author confirmation, and this function is never given one: it takes a
      `MarkerScan`, whose members exist only because somebody wrote a marker.
    """
    out: list[StoredConstraint] = []
    for c in sorted(scan.constraints, key=lambda c: (c.file, c.marker_line)):
        if not c.tracked:
            continue
        out.append(StoredConstraint(
            id=c.id, text=c.canonical_text, version=1, status=ACTIVE,
            provenance=f"{IMPORTED_SOURCE}:{c.file}:{c.marker_line} ({when})",
            scope=Scope({})))
    return out
