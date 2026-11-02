"""0.2 — take the skill ids and versions from the HOST's own load record.

Owner direction 2026-10-17: a second host qualifies only if "the adapter can
read the skill and version from the host's own skill load record, rather than
having them specified by hand". This module is that reader, and it is
duck-typed on purpose: a host record is anything with a `skills` sequence whose
items carry a `name` and a `dir_path`.

Why this is not just convenience
--------------------------------
`cf.container.ContainerRequest` takes skill ids and versions, and the adapter
contract says the host supplies them. Up to 0.2 that meant *the demo script*
supplied them, which is the weakest possible link in the chain: a hand-written
list is a claim about what the host loaded, made by the person writing the
demo. Reading the host's record instead makes the request a *consequence* of
what the host actually loaded, so a skill the host never loaded cannot be
declared for, and a skill the host did load cannot be silently skipped.

The version problem, stated plainly
-----------------------------------
**Vibe-Trading's load record carries no version.** `src/agent/skills.Skill`
has `name`, `description`, `category`, `body`, `dir_path` and the parsed
frontmatter `metadata`; none of the 88 bundled `SKILL.md` files has a
`version:` key. So for that host there is no version label to read, and
inventing one would be exactly the hand-filling this module exists to remove.

What is available is better than a label anyway: the library binds every
source file by content hash, so the question *"which built version is the host
holding?"* is decidable from the bytes. Two version sources, and no third:

* `host_declared_label` — the record itself carried a version string. It is
  then required to name a library binding **and** that binding's file hashes
  must match the bytes on disk. A label the bytes contradict is refused, never
  preferred.
* `library_binding_matched_host_bytes` — the record carries no label, and the
  bytes the host loaded hash-match exactly one binding across the libraries
  supplied. That binding's `source_version` is used.

Zero matches is a refusal naming the files that differ. More than one match is
a refusal too: two libraries built from identical bytes under different version
labels make the choice arbitrary, and an arbitrary choice recorded as a
resolution would be a fabricated version. **Nothing here guesses.**

Stale host bytes are a separate failure
---------------------------------------
The host's record holds the text it read *when it read it*. If `SKILL.md` has
changed since, the library and the disk can agree while the running host is
holding something else — and declarations resolved from the disk would then
describe bytes that are not in the host's prompt. So the loaded `body` is also
checked against the file text, and a disagreement is
`HOST_BYTES_DIFFER_FROM_DISK`: a refusal about the host, not about the library.

**And a record with no body to compare is refused, not resolved.** The first
version of this skipped the check whenever the record carried no text, so the
*least* verifiable record received the *fewest* checks — and it reported
`host_body_matches_disk = False`, which reads as "checked and differs" rather
than "never checked". A review found it. Now `HOST_BODY_UNAVAILABLE` refuses
it, `require_host_body_match=False` is the explicit way to accept it, and the
field is three-valued so "not checked" cannot be mistaken for a result.

What this module does not do
----------------------------
It does not read a conversation, infer relevance, rank, filter, or decide
which skills *should* be declared for. It reports what the host loaded, with
each skill either resolved to one version or refused with a named reason. The
refusals are the point: a resolver that always answers is a resolver that
answers wrongly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .container import SkillRequest
from .library import sha256_text

#: How a version was established. There is no third way and no default.
HOST_DECLARED_LABEL = "host_declared_label"
LIBRARY_BINDING_MATCHED_HOST_BYTES = "library_binding_matched_host_bytes"
VERSION_SOURCES = (HOST_DECLARED_LABEL, LIBRARY_BINDING_MATCHED_HOST_BYTES)

#: Every way a loaded skill can fail to resolve. Each one names what was
#: wrong, because "not resolved" alone would let a caller treat a stale host,
#: an unbuilt skill and an ambiguous version as the same event.
NO_DIR_PATH = "NO_DIR_PATH"
SKILL_OUTSIDE_SOURCE_ROOT = "SKILL_OUTSIDE_SOURCE_ROOT"
NOT_IN_ANY_LIBRARY = "NOT_IN_ANY_LIBRARY"
NO_VERSION_MATCHES_HOST_BYTES = "NO_VERSION_MATCHES_HOST_BYTES"
AMBIGUOUS_VERSIONS = "AMBIGUOUS_VERSIONS"
HOST_LABEL_NOT_IN_LIBRARY = "HOST_LABEL_NOT_IN_LIBRARY"
HOST_LABEL_BYTES_DIFFER = "HOST_LABEL_BYTES_DIFFER"
HOST_BYTES_DIFFER_FROM_DISK = "HOST_BYTES_DIFFER_FROM_DISK"
HOST_BODY_UNAVAILABLE = "HOST_BODY_UNAVAILABLE"
MISSING_SOURCE_FILE = "MISSING_SOURCE_FILE"
UNVERIFIABLE_BINDING = "UNVERIFIABLE_BINDING"
REFUSALS = (NO_DIR_PATH, SKILL_OUTSIDE_SOURCE_ROOT, NOT_IN_ANY_LIBRARY,
            NO_VERSION_MATCHES_HOST_BYTES, AMBIGUOUS_VERSIONS,
            HOST_LABEL_NOT_IN_LIBRARY, HOST_LABEL_BYTES_DIFFER,
            HOST_BYTES_DIFFER_FROM_DISK, HOST_BODY_UNAVAILABLE,
            MISSING_SOURCE_FILE, UNVERIFIABLE_BINDING)


class HostRecordError(Exception):
    """The host record cannot be read as a load record at all."""


@dataclass(frozen=True)
class Resolved:
    """One loaded skill, bound to one built version."""

    name: str
    skill_id: str
    source_version: str
    version_source: str
    library_content_id: str
    dir_path: str
    files_matched: tuple = ()
    #: `True` checked and agrees, `False` checked and differs, **`None` not
    #: checked** — the record carried no text to compare. Three values and not
    #: two: a review found this field reporting `False` for a record with no
    #: body, which reads as "checked and differs" when the truth is "never
    #: checked", and the two call for different actions.
    host_body_matches_disk: "bool | None" = None

    def as_dict(self) -> dict:
        return {"name": self.name, "skill_id": self.skill_id,
                "source_version": self.source_version,
                "version_source": self.version_source,
                "library_content_id": self.library_content_id,
                "dir_path": self.dir_path,
                "files_matched": list(self.files_matched),
                "host_body_matches_disk": self.host_body_matches_disk,
                "_host_body_matches_disk_null_means": (
                    "the record carried no text to compare, so the staleness "
                    "of the host's copy was NOT checked. Only reachable with "
                    "require_host_body_match=False; the default refuses such "
                    "a record outright."),
                }


@dataclass(frozen=True)
class Refused:
    """One loaded skill that resolves to no version, and why."""

    name: str
    skill_id: str
    reason: str
    detail: str = ""
    candidates: tuple = ()

    def as_dict(self) -> dict:
        return {"name": self.name, "skill_id": self.skill_id,
                "reason": self.reason, "detail": self.detail,
                "candidates": list(self.candidates)}


@dataclass
class HostResolution:
    """What the host loaded, resolved or refused — never partially both."""

    host: str
    source_root: str
    resolved: tuple = ()
    refused: tuple = ()
    #: Which fields the record actually carried, so a reader can see that the
    #: absence of a version is the host's, not this module's omission.
    record_fields: dict = field(default_factory=dict)
    loaded_total: int = 0

    def requests(self, *, library_content_id: str = "") -> tuple:
        """`SkillRequest`s for the resolved skills, optionally one library.

        Filtering by library is not cosmetic: `cf.container.assemble` takes a
        single library, so a caller resolving across several must assemble per
        library rather than mix versions from different ones into one request.
        """
        rs = [r for r in self.resolved
              if not library_content_id
              or r.library_content_id == library_content_id]
        return tuple(SkillRequest(r.skill_id, r.source_version) for r in rs)

    def libraries(self) -> tuple:
        out = []
        for r in self.resolved:
            if r.library_content_id not in out:
                out.append(r.library_content_id)
        return tuple(out)

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "source_root": self.source_root,
            "loaded_total": self.loaded_total,
            "resolved_count": len(self.resolved),
            "refused_count": len(self.refused),
            "resolved": [r.as_dict() for r in self.resolved],
            "refused": [r.as_dict() for r in self.refused],
            "record_fields": dict(self.record_fields),
            "version_sources_used": sorted({r.version_source
                                            for r in self.resolved}),
            "_what_this_is": (
                "the skills the host's own loader recorded, each resolved to "
                "exactly one built version or refused with a named reason. "
                "Nothing here was supplied by hand and no version was "
                "inferred: see cf/container_hostrecord.py for the two "
                "permitted version sources."),
        }


def host_record_skills(record) -> list:
    """The loaded-skill items out of a host record, duck-typed.

    Accepts the record object (anything with `.skills`) or the sequence
    itself, so a host that exposes its list directly needs no wrapper.
    """
    items = getattr(record, "skills", record)
    if isinstance(items, (str, bytes)) or not hasattr(items, "__iter__"):
        raise HostRecordError(
            "a host load record must expose a `skills` sequence; got "
            f"{type(record).__name__} with no iterable `skills`")
    out = list(items)
    for it in out:
        if not hasattr(it, "name"):
            raise HostRecordError(
                "every loaded skill must carry a `name`; got an item of type "
                f"{type(it).__name__}")
    return out


def _skill_id(dir_path: Path, roots: list) -> tuple:
    """`(skill_id, root)` for the first declared root containing *dir_path*.

    Several roots is the real shape, not a convenience: Vibe-Trading's loader
    reads a bundled directory **and** a user directory, and a skill from the
    second is not outside the host's scope just because it is outside the
    first. A single root would have refused every user skill.
    """
    for r in roots:
        try:
            rel = dir_path.resolve().relative_to(r.resolve())
        except ValueError:
            continue
        return rel.as_posix(), r
    return None, None


def _binding_matches_disk(binding, root: Path, skill_dir: Path) -> dict:
    """Hash every file the binding names, against the bytes on disk now.

    `rel_path` is relative to the **source root** (`cf.container_build` writes
    it as `source_root / rel_path`), not to the skill directory — resolving it
    the other way would silently flatten `references/notes.md` onto
    `notes.md` and hash the wrong file, or none. A binding naming a path
    outside the host's own skill directory is counted as missing rather than
    read: the library may not reach outside the skill it is a binding for.
    """
    matched, differing, missing = [], [], []
    for f in binding.files:
        p = (root / f.rel_path)
        try:
            inside = p.resolve().is_relative_to(skill_dir.resolve())
        except OSError:
            inside = False
        if not inside or not p.is_file():
            missing.append(f.rel_path)
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            missing.append(f.rel_path)
            continue
        (matched if f.verify(text) else differing).append(f.rel_path)
    return {"matched": matched, "differing": differing, "missing": missing,
            "ok": bool(matched) and not differing and not missing}


def _file_body(text: str) -> str:
    """The file text with a leading `---` frontmatter block removed.

    The host's loader strips frontmatter before storing `body`, so comparing
    the record against the raw file would always disagree. This reproduces
    that one transformation and nothing else.
    """
    if not text.startswith("---"):
        return text
    parts = text.split("\n")
    if parts and parts[0].strip() == "---":
        for i in range(1, len(parts)):
            if parts[i].strip() == "---":
                return "\n".join(parts[i + 1:])
    return text


def _host_body_matches_disk(item, skill_dir: Path,
                            body_parser=None) -> "bool | None":
    """Is the text the host is holding still the text on disk?

    `None` when the record carries no body to compare, which is reported
    rather than treated as agreement.

    **Equality, not containment.** The first version of this asked whether the
    record's body was *contained in* the file, and my own H4 scenario walked
    straight through it: text appended after the host read the file leaves the
    old body a perfect substring of the new one, so an edited skill reported
    "the host's bytes still match". Containment answers a weaker question than
    the one being asked.

    *body_parser* lets a host hand in **its own** frontmatter parser, which is
    the only way this comparison is guaranteed sound: `_file_body` is a second
    implementation of a transformation the host already owns, and two
    implementations of one rule drift. It agrees with Vibe-Trading's
    `parse_frontmatter` on all 95 `SKILL.md` files in this tree today — which
    is evidence, not a guarantee, and is why the hook exists and why
    `v2/host_record_vibe.py` passes the host's parser and gates the agreement.
    """
    body = getattr(item, "body", None)
    if not isinstance(body, str) or not body.strip():
        return None
    f = skill_dir / "SKILL.md"
    if not f.is_file():
        return False
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return False
    parse = body_parser or _file_body
    return body.strip() == str(parse(text)).strip()


def resolve_from_host_record(record, libraries, *, source_roots, host: str,
                             version_field: str = "version",
                             require_host_body_match: bool = True,
                             body_parser=None) -> HostResolution:
    """Resolve every skill in a host's load record to one built version.

    *libraries* is one `Library` or several. Several is the honest shape: a
    skill_id holds one binding per library, so two versions of one skill are
    two libraries, and that is where `AMBIGUOUS_VERSIONS` becomes reachable.

    *source_roots* is one path or several, matching the host's own loader: a
    skill id is computed against the first declared root that contains it.
    """
    libs = [libraries] if hasattr(libraries, "skills") \
        and isinstance(getattr(libraries, "skills"), dict) else list(libraries)
    roots = ([Path(source_roots)]
             if isinstance(source_roots, (str, Path))
             else [Path(p) for p in source_roots])
    if not roots:
        raise HostRecordError(
            "at least one source root is required: a skill id is only "
            "meaningful relative to the root its library was built against")
    items = host_record_skills(record)
    resolved, refused = [], []
    fields: dict = {}

    for it in items:
        name = str(getattr(it, "name", "") or "")
        dir_path = getattr(it, "dir_path", None)
        meta = getattr(it, "metadata", None) or {}
        if not fields:
            fields = {
                "name": True,
                "dir_path": dir_path is not None,
                "body": isinstance(getattr(it, "body", None), str),
                "metadata": bool(meta) or hasattr(it, "metadata"),
                version_field: version_field in meta,
                "_note": (
                    "fields observed on the FIRST loaded item. A false "
                    f"{version_field!r} means this host records no version "
                    "label, so the only sound source is the bytes."),
            }
        if dir_path is None:
            refused.append(Refused(name, "", NO_DIR_PATH,
                                   "the record carries no directory for this "
                                   "skill, so its source bytes cannot be "
                                   "located or hashed"))
            continue
        skill_dir = Path(dir_path)
        sid, root = _skill_id(skill_dir, roots)
        if sid is None:
            refused.append(Refused(
                name, "", SKILL_OUTSIDE_SOURCE_ROOT,
                f"{skill_dir} is under none of the declared source roots "
                f"{[str(r) for r in roots]}"))
            continue

        held = [(lb, lb.skills[sid]) for lb in libs if sid in lb.skills]
        if not held:
            refused.append(Refused(
                name, sid, NOT_IN_ANY_LIBRARY,
                "the host loaded this skill and no supplied library holds a "
                "binding for it; nothing is declared for it here"))
            continue

        body_ok = _host_body_matches_disk(it, skill_dir, body_parser)
        if require_host_body_match and body_ok is False:
            refused.append(Refused(
                name, sid, HOST_BYTES_DIFFER_FROM_DISK,
                "the text in the host's load record is not the text in "
                "SKILL.md now: the host is holding bytes that were since "
                "changed, so a version resolved from disk would not describe "
                "what the host is actually running"))
            continue
        # FAIL CLOSED when there is nothing to compare. A review found this
        # path open: a record carrying no `body` skipped the staleness check
        # entirely and resolved anyway, so the weakest possible record got
        # the fewest checks — and `host_body_matches_disk` then said `False`,
        # which reads as "checked and differs". Unverifiable is a failure in
        # this phase, not a delivery.
        if require_host_body_match and body_ok is None:
            refused.append(Refused(
                name, sid, HOST_BODY_UNAVAILABLE,
                "the record carries no text for this skill, so whether the "
                "host is holding the bytes now on disk CANNOT be checked. "
                "Resolving anyway would give the least verifiable record the "
                "fewest checks. Pass require_host_body_match=False to accept "
                "that explicitly; the result then reports "
                "host_body_matches_disk = null rather than false"))
            continue

        label = meta.get(version_field)
        if isinstance(label, str) and label.strip():
            label = label.strip()
            named = [(lb, b) for lb, b in held if b.source_version == label]
            if not named:
                refused.append(Refused(
                    name, sid, HOST_LABEL_NOT_IN_LIBRARY,
                    f"the host record declares version {label!r} and no "
                    "supplied library holds that version of this skill",
                    tuple(sorted(b.source_version for _, b in held))))
                continue
            lb, b = named[0]
            m = _binding_matches_disk(b, root, skill_dir)
            if not m["ok"]:
                refused.append(Refused(
                    name, sid, HOST_LABEL_BYTES_DIFFER,
                    f"the host record declares version {label!r} but the "
                    "bytes on disk do not match that binding: differing="
                    f"{m['differing']} missing={m['missing']}"))
                continue
            resolved.append(Resolved(
                name, sid, label, HOST_DECLARED_LABEL,
                lb.library_content_id(), str(skill_dir),
                tuple(m["matched"]), body_ok))
            continue

        # No label anywhere in the record: decide from the bytes, or refuse.
        hits, misses, unverifiable = [], [], []
        for lb, b in held:
            if not b.files:
                unverifiable.append((lb, b))
                continue
            m = _binding_matches_disk(b, root, skill_dir)
            (hits if m["ok"] else misses).append((lb, b, m))
        if len(hits) == 1:
            lb, b, m = hits[0]
            resolved.append(Resolved(
                name, sid, b.source_version,
                LIBRARY_BINDING_MATCHED_HOST_BYTES,
                lb.library_content_id(), str(skill_dir),
                tuple(m["matched"]), body_ok))
        elif len(hits) > 1:
            refused.append(Refused(
                name, sid, AMBIGUOUS_VERSIONS,
                "the bytes on disk match more than one built version, so no "
                "version is determined by them; picking one would be a "
                "fabricated resolution",
                tuple(sorted(b.source_version for _, b, _ in hits))))
        elif unverifiable and not misses:
            refused.append(Refused(
                name, sid, UNVERIFIABLE_BINDING,
                "the only bindings for this skill record no source files "
                "(an imported legacy binding), so the host's bytes cannot be "
                "matched to a version at all",
                tuple(sorted(b.source_version for _, b in unverifiable))))
        else:
            det = []
            for _lb, b, m in misses:
                det.append(f"{b.source_version}: differing={m['differing']} "
                           f"missing={m['missing']}")
            reason = (MISSING_SOURCE_FILE
                      if misses and all(m["missing"] and not m["differing"]
                                        for _l, _b, m in misses)
                      else NO_VERSION_MATCHES_HOST_BYTES)
            refused.append(Refused(
                name, sid, reason,
                "the host loaded this skill but its bytes match no built "
                "version — " + "; ".join(det),
                tuple(sorted(b.source_version for _l, b, _m in misses))))

    return HostResolution(host=host,
                          source_root=", ".join(str(r) for r in roots),
                          resolved=tuple(resolved), refused=tuple(refused),
                          record_fields=fields, loaded_total=len(items))
