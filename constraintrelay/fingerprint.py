"""Loader fingerprints — Brief v0.6 §8.

A profile is **not** keyed by upstream commit. The compatibility key is the
fingerprint of the loader-relevant implementation surface:

    LoaderFingerprint = { source/path/a: sha256(...), source/path/b: sha256(...) }

So an unrelated upstream commit does not force requalification, and a change to
a loader-relevant file does:

    any recorded hash differs  ->  PROFILE REQUALIFICATION REQUIRED
                               ->  affected definite results become UNKNOWN

Two states are distinguished carefully, because conflating them is the failure
this module exists to prevent:

* `UNRECORDED` — no fingerprint has ever been taken for this profile. Not a
  pass. A profile with no recorded fingerprint cannot claim its qualification
  still applies, so it is treated the same as a changed one.
* `QUALIFIED` — every recorded hash matches a surface we can see right now.

And a third, which §8 explicitly permits:

* `DECLARED` — the harness is not installed here, so there is nothing to
  compare against. §8: results mean *under the declared qualified target
  profile*, not *we detected that this machine is running that harness*. This
  is the normal CI state and is not a defect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .registry import REGISTRY, Registered

from constraintrelay import datafiles as _df

MANIFEST = _df.path("loader_fingerprints.json")

QUALIFIED = "QUALIFIED"
DECLARED = "DECLARED"
UNRECORDED = "UNRECORDED"
REQUALIFICATION_REQUIRED = "PROFILE REQUALIFICATION REQUIRED"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class FingerprintRecord:
    """What was recorded for one profile, and when."""

    profile_id: str
    source_root: str
    recorded_at: str
    files: dict[str, str]           # repo-relative path -> sha256
    note: str = ""


@dataclass
class FingerprintCheck:
    """The outcome of comparing a record against a checkout."""

    profile_id: str
    state: str
    checked_root: str = ""
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def requalification_required(self) -> bool:
        return self.state == REQUALIFICATION_REQUIRED

    @property
    def definite_allowed(self) -> bool:
        """§8: a changed loader surface makes affected definite results UNKNOWN.

        `UNRECORDED` is included because a profile whose surface was never
        fingerprinted cannot assert that its qualification still holds.
        """
        return self.state in (QUALIFIED, DECLARED)

    def as_dict(self) -> dict:
        return {"profile_id": self.profile_id, "state": self.state,
                "checked_root": self.checked_root, "changed": self.changed,
                "missing": self.missing, "matched_count": len(self.matched),
                "message": self.message}


def load_manifest(path: Path | None = None) -> dict[str, FingerprintRecord]:
    """The recorded loader fingerprints.

    A missing file **raises** rather than returning `{}`. Returning an empty
    mapping made "the data did not ship" indistinguishable from "nobody has
    recorded a fingerprint", and the CLI rendered the second: every profile
    `UNRECORDED`, at exit code 0. An explicit *path* that does not exist is
    the caller's own argument and is reported the same way.
    """
    p = path or MANIFEST
    _df.require(p, what="the recorded loader fingerprints")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        pid: FingerprintRecord(
            profile_id=pid,
            source_root=rec.get("source_root", ""),
            recorded_at=rec.get("recorded_at", ""),
            files=dict(rec.get("files", {})),
            note=rec.get("note", ""))
        for pid, rec in data.get("profiles", {}).items()
    }


def surface_files(reg: Registered) -> "tuple[str, ...]":
    """The loader-relevant surface for *reg*.

    Qualification ruling item 8: where a generated surface artifact exists, it is
    the surface — `loader_files` is then only the entry point it was derived from.
    CF-013 is why: openclaw's hand-written list was `dist/bootstrap-D7cACmY4.js`
    alone, while the loader resolves 136 modules out of 5,639 in `dist/`, so a
    change to any of the other 135 left the fingerprint QUALIFIED.
    """
    if not reg.loader_surface_from:
        return tuple(reg.loader_files)
    path = MANIFEST.parent / reg.loader_surface_from
    if not path.is_file():
        # Falling back to the guessed list silently is how a surface shrinks back
        # to one file without anyone noticing, so the absence is loud instead.
        raise FileNotFoundError(
            f"{reg.profile_id} declares loader_surface_from="
            f"{reg.loader_surface_from!r} but {path} does not exist; regenerate it "
            f"(replays/openclaw/surface.mjs) rather than falling back to the "
            f"guessed file list")
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(sorted(data.get("files", {})))


def fingerprint(reg: Registered, root: Path) -> dict[str, str]:
    """Hash *reg*'s loader-relevant surface under the checkout at *root*.

    A declared-but-absent file is recorded as `MISSING`, never skipped: a loader
    file that vanished is exactly the kind of upstream change that invalidates a
    profile, so it has to survive into the comparison.
    """
    out: dict[str, str] = {}
    for rel in surface_files(reg):
        path = root / rel
        out[rel] = sha256_file(path) if path.is_file() else "MISSING"
    return out


def check(profile_key: str, root: Path | None,
          manifest: dict[str, FingerprintRecord] | None = None) -> FingerprintCheck:
    """Compare the recorded fingerprint for *profile_key* against *root*.

    *root* is the upstream harness checkout, or `None` when the harness is not
    installed here — the normal CI case, which §8 resolves to `DECLARED`.
    """
    reg = REGISTRY[profile_key]
    records = manifest if manifest is not None else load_manifest()
    record = records.get(reg.profile_id)

    if record is None or not record.files:
        return FingerprintCheck(
            reg.profile_id, UNRECORDED,
            message=("no loader fingerprint has been recorded for this profile; "
                     "its qualification cannot be asserted (§8). Definite "
                     "statuses are withheld."))
    if root is None:
        return FingerprintCheck(
            reg.profile_id, DECLARED, matched=sorted(record.files),
            message=("harness source not supplied, so results hold under the "
                     "declared qualified target profile, not as a detection "
                     "that this machine runs that harness (§8)."))

    observed = fingerprint(reg, root)
    changed = sorted(k for k, v in record.files.items() if observed.get(k) != v)
    missing = sorted(k for k in record.files if observed.get(k) == "MISSING")
    matched = sorted(k for k, v in record.files.items() if observed.get(k) == v)
    if changed:
        return FingerprintCheck(
            reg.profile_id, REQUALIFICATION_REQUIRED, str(root),
            changed=changed, missing=missing, matched=matched,
            message=(f"{len(changed)} loader-relevant file(s) differ from the "
                     f"recorded surface: {', '.join(changed)}. Affected definite "
                     f"results become UNKNOWN until the new loader surface is "
                     f"qualified (§8)."))
    return FingerprintCheck(
        reg.profile_id, QUALIFIED, str(root), matched=matched,
        message=f"loader surface matches the recorded fingerprint "
                f"({len(matched)} file(s)).")


def record(profile_key: str, root: Path, when: str, note: str = "") -> dict:
    """A manifest entry for *profile_key* taken from the checkout at *root*."""
    reg = REGISTRY[profile_key]
    return {
        "source_root": str(root),
        "recorded_at": when,
        "note": note,
        "files": fingerprint(reg, root),
    }
