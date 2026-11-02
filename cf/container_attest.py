"""0.2 — the attest step, inside the package. Produces DRAFTS only.

This was `v2/make_attestation.py`. Moved into the package for the same reason
as the build step: the 0.2 acceptance installs a wheel outside the repository.

What this does, and the line it will not cross
----------------------------------------------
It writes the manifest's **mechanical** half: declaration ids and their seals,
transcribed from a built library. A human retyping 64-hex digests would only
introduce errors.

It will **not** write the half that makes a manifest a confirmation. The
default output carries `confirmation_method: ""`, which
`cf.container.Attestation.load` reports as **pending** — readable, named, and
delivering nothing. A draft this module produced cannot be delivered until a
human decides to adopt the text and records how that decision can be found.

`check()` validates format, version agreement and the presence of a
confirmation record. **It does not verify author identity and makes no claim
about legal effect**: no signature is checked and no `evidence_ref` is
resolved. Filling the fields is the *record* of a human act, never evidence
that the act happened.
"""

from __future__ import annotations

import json
from pathlib import Path

from .container import (ATTESTATION_FILE, METHOD_FIXTURE, REAL_METHODS,
                        Attestation, ManifestError, load_attestations)
from .library import Library, file_digest, sha256_text

DRAFT_NOTE = (
    "DRAFT. The ids and seals below are transcription from the built library; "
    "the confirmation is NOT. While confirmation_method is empty the "
    "container reports this skill PENDING_CONFIRMATION and delivers nothing. "
    "To adopt: decide whether the text is your declared position, then set "
    "confirmation_method to one of signed_git_commit / written_reply / "
    "in_person and put in evidence_ref where that decision can be found. "
    "Filling these fields RECORDS the decision; it is not evidence of it.")

FIXTURE_NOTE = (
    "ENGINEERING FIXTURE. Stands in for an author confirmation and IS NOT "
    "ONE: real_author_confirmation is False permanently, the label prints "
    "inside the delivered payload, and it appears in every receipt. Fixtures "
    "are REFUSED by default and travel only when a request explicitly sets "
    "accept_fixture_attestations.")


class AttestError(Exception):
    """An attest request that cannot be honoured as asked."""


def propose(*, library, skill_id: str, source_root, confirmed_by: str = "",
            confirmed_at: str = "", fixture: bool = False,
            evidence_ref: str = "") -> dict:
    """Build the manifest document for *skill_id*. A draft unless `fixture`."""
    lib = Library.load(Path(library))
    binding = lib.skills.get(skill_id)
    if binding is None:
        raise AttestError(f"{library} holds no binding for {skill_id!r}; "
                          f"known: {sorted(lib.skills)}")
    entries = sorted((e for e in lib.entries if e.skill_id == skill_id),
                     key=lambda e: e.constraint_id)
    return {
        "_doc": (FIXTURE_NOTE if fixture else DRAFT_NOTE),
        "_seal_is_not_the_manifest": (
            "declared_text_sha256 evidences content consistency only. Who "
            "wrote the text and whether they adopted it is evidenced by "
            "confirmed_by / confirmation_method / evidence_ref, which are "
            "separate fields and are not derived from any hash."),
        "skill_id": skill_id,
        "source_version": binding.source_version,
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
        "confirmation_method": (METHOD_FIXTURE if fixture else ""),
        "evidence_ref": evidence_ref,
        "fixture": bool(fixture),
        "fixture_note": (FIXTURE_NOTE if fixture else ""),
        "declarations": [
            {"declaration_id": e.constraint_id,
             "declared_text_sha256": sha256_text(e.source_text),
             "declared_text_chars": len(e.source_text),
             "source_file": e.source_file,
             "source_span": list(e.source_span)}
            for e in entries],
    }


def write_draft(*, library, skill_id: str, source_root, confirmed_by: str = "",
                confirmed_at: str = "", fixture: bool = False,
                evidence_ref: str = "") -> Path:
    """Write the manifest beside the skill and return its path."""
    doc = propose(library=library, skill_id=skill_id, source_root=source_root,
                  confirmed_by=confirmed_by, confirmed_at=confirmed_at,
                  fixture=fixture, evidence_ref=evidence_ref)
    out = Path(source_root).resolve() / skill_id / ATTESTATION_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    return out


def check(*, library, source_root, skill_ids: list) -> dict:
    """Format, version agreement and confirmation record. Nothing more.

    Three things are checked and said plainly; a fourth is refused:

    * **format** — the manifest parses and every declaration has a 64-hex seal;
    * **version** — the manifest's `source_version` matches the library's
      binding, and every sealed text still hashes to its seal;
    * **confirmation record** — whether a method and a locatable reference are
      present, and whether the manifest is a fixture or a pending draft.

    **Not checked, and not claimed:** that the named party really confirmed,
    that a signature is valid, that `evidence_ref` resolves, or that any of it
    has legal effect. `identity_verified` and `legal_effect_assessed` are
    reported as `false` so no reader has to infer the limit.
    """
    lib = Library.load(Path(library))
    atts = load_attestations(Path(source_root),
                             {s: s for s in skill_ids})
    out = {"library": str(library), "library_content_id":
           lib.library_content_id(), "skills": {},
           "identity_verified": False,
           "legal_effect_assessed": False,
           "_what_check_does_not_do": (
               "this checks format, version agreement and the presence of a "
               "confirmation record. It does NOT verify that the named party "
               "confirmed, validate any signature, resolve evidence_ref, or "
               "assess legal effect.")}
    for sid in skill_ids:
        att = atts.get(sid)
        binding = lib.skills.get(sid)
        row: dict = {"in_library": binding is not None,
                     "library_source_version":
                         binding.source_version if binding else None}
        if att is None:
            row.update(manifest="absent", status="UNDECLARED",
                       detail="no manifest beside this skill; nothing here "
                              "says the skill has no constraints")
        elif isinstance(att, ManifestError):
            row.update(manifest="unreadable", status="UNREADABLE",
                       detail=att.reason)
        else:
            entries = {e.constraint_id: e for e in lib.entries
                       if e.skill_id == sid}
            seals = []
            for d in att.declarations:
                e = entries.get(d.declaration_id)
                seals.append({
                    "declaration_id": d.declaration_id,
                    "resolves_to_entry": e is not None,
                    "seal_ok": (e is not None
                                and sha256_text(e.source_text)
                                == d.declared_text_sha256)})
            version_ok = (binding is not None
                          and att.source_version == binding.source_version)
            # The LABEL agreeing is not the bytes agreeing. `check` compared
            # only the label and the per-declaration seals, so a skill file
            # edited after the build passed — found by the clean-environment
            # acceptance run, where `preview` refused the same tree that
            # `check` had just called fine. Re-hash every bound file.
            drifted = []
            if binding is not None:
                for f in binding.files:
                    fp = Path(source_root) / f.rel_path
                    if not fp.is_file():
                        drifted.append({"rel_path": f.rel_path,
                                        "reason": "file missing"})
                    elif file_digest(fp) != f.content_sha256:
                        drifted.append({"rel_path": f.rel_path,
                                        "reason": "content changed since "
                                                  "the library was built",
                                        "built_sha256": f.content_sha256,
                                        "current_sha256": file_digest(fp)})
            row.update(
                manifest="readable",
                status=("PENDING_CONFIRMATION" if att.pending
                        else "FIXTURE" if att.fixture
                        else "CONFIRMATION_RECORDED"),
                skill_id_matches_request=(att.skill_id == sid),
                manifest_source_version=att.source_version,
                version_agrees_with_library=version_ok,
                confirmation_method=att.confirmation_method or None,
                confirmation_method_is_real=(
                    att.confirmation_method in REAL_METHODS),
                evidence_ref_present=bool(att.evidence_ref.strip()),
                real_author_confirmation=att.real_author_confirmation,
                declarations=seals,
                all_seals_ok=all(s["seal_ok"] for s in seals),
                source_bytes_match_library=not drifted,
                drifted_files=drifted)
        out["skills"][sid] = row
    return out


def summarise(report: dict) -> str:
    """One line per skill, for a terminal."""
    lines = []
    for sid, r in report["skills"].items():
        bits = [r["status"]]
        if r.get("version_agrees_with_library") is False:
            bits.append("VERSION MISMATCH (label)")
        if r.get("source_bytes_match_library") is False:
            bits.append("VERSION MISMATCH (bytes: "
                        + ", ".join(d["rel_path"]
                                    for d in r.get("drifted_files", []))
                        + ")")
        if r.get("all_seals_ok") is False:
            bits.append("SEAL MISMATCH")
        if r.get("skill_id_matches_request") is False:
            bits.append("MANIFEST NAMES ANOTHER SKILL")
        lines.append(f"  {sid}: {' | '.join(bits)}")
    lines.append("  identity_verified: false   legal_effect_assessed: false")
    return "\n".join(lines)
