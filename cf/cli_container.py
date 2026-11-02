"""`constraintrelay container` — the 0.2 command group.

Five subcommands over the same code the Python API uses, so the CLI cannot
drift from it:

    build     read skills -> a version-bound library file
    check     format, version agreement, confirmation record. Nothing more
    preview   the capsule, with a machine-readable form for non-Python hosts
    attest    write a DRAFT manifest. Never a confirmation
    verify    read a captured request from a file, emit a receipt

`preview --json` and `verify --request` exist so a host that is not written in
Python can use the container over the command line: preview emits the capsule
text plus the assembly identity, the host inserts it into its own request, and
verify reads the captured request back and returns the receipt. Nothing here
has been exercised against a real non-Python host yet, and that is stated in
the help rather than implied.

`check` deliberately does not say whether a confirmation is genuine. It reports
format, version agreement, and whether a confirmation record is present, with
`identity_verified: false` and `legal_effect_assessed: false` in the output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import container as C
from . import naming
from . import container_api as api
from . import container_attest as attest
from . import container_receipt as R


def _manifests(source_root: str, skills: list) -> dict:
    return api.load_manifests(source_root, skills)


def cmd_build(a) -> int:
    res = api.build_library(source_root=a.source, skill_ids=a.skill,
                            source_version=a.source_version, out=a.out,
                            skills_dir=a.skills_dir,
                            reviewer=a.reviewer, built_at=a.built_at)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def cmd_check(a) -> int:
    rep = attest.check(library=a.library, source_root=a.source,
                       skill_ids=a.skill)
    if a.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(f"library {rep['library_content_id']}")
        print(attest.summarise(rep))
    bad = [s for s, r in rep["skills"].items()
           if r.get("version_agrees_with_library") is False
           or r.get("all_seals_ok") is False
           or r.get("source_bytes_match_library") is False
           or r.get("skill_id_matches_request") is False
           or r["status"] == "UNREADABLE"]
    return 1 if bad else 0


def cmd_attest(a) -> int:
    out = attest.write_draft(library=a.library, skill_id=a.skill,
                             source_root=a.source,
                             confirmed_by=a.confirmed_by,
                             confirmed_at=a.confirmed_at,
                             fixture=a.fixture,
                             evidence_ref=a.evidence_ref)
    print(json.dumps({
        "written": str(out),
        "fixture": bool(a.fixture),
        "real_author_confirmation": False,
        "status": "FIXTURE" if a.fixture else "PENDING_CONFIRMATION",
        "_next": ("this is a DRAFT. While confirmation_method is empty the "
                  "container reports PENDING_CONFIRMATION and delivers "
                  "nothing. A human decides whether to adopt the text and "
                  "records where that decision can be found; filling the "
                  "fields is the record of that act, not evidence of it."),
    }, indent=2, ensure_ascii=False))
    return 0


def _assemble(a):
    lib = api.load_library(a.library)
    mans = _manifests(a.source, a.skill)
    cats = tuple(a.category) if a.category else C.DEFAULT_DELIVER_CATEGORIES
    req = C.ContainerRequest(
        request_id=a.request_id,
        skills=tuple(C.SkillRequest(s, a.source_version) for s in a.skill),
        accept_fixture_attestations=a.accept_fixtures,
        deliver_categories=cats,
        categories_enabled_by=a.enabled_by,
        categories_enabled_at=a.enabled_at,
        decision_point=a.decision_point)
    return lib, mans, req, api.assemble(lib, req, source_root=a.source,
                                        manifests=mans)


def cmd_preview(a) -> int:
    lib, mans, req, asm = _assemble(a)
    capsule = api.render(asm)
    if a.json:
        print(json.dumps({
            "capsule": capsule,
            "assembly": {
                "request_id": asm.request.request_id,
                "library_content_id": asm.library_content_id,
                "body_sha256": asm.body_sha256,
                "assembled_at": asm.assembled_at,
                "capsule_chars": len(capsule),
                "declaration_chars": sum(
                    len(t) for o in asm.outcomes
                    for t in o.delivered_texts.values()),
                "outcome_census": asm.census(),
                "category_census": asm.category_census(),
                "outcomes": [o.as_dict() for o in asm.outcomes],
            },
            "_how_to_use": (
                "insert `capsule` into your request as one message, in a "
                "non-elevating role, after your own messages. Then capture "
                "the final request as JSON and run "
                f"`{naming.cmd('container', 'verify')} --request <file>` with "
                "the same --library/--skill arguments to get a receipt."),
            "_not_yet_exercised": (
                "no non-Python host has been driven through this path yet."),
        }, indent=2, ensure_ascii=False))
    else:
        print(capsule, end="")
        print(f"\n--- {len(capsule)} capsule chars, "
              f"assembly {asm.body_sha256[:12]}…, "
              f"library {asm.library_content_id} ---")
    return 0 if asm.census()[C.FAILED] == 0 else 1


def cmd_verify(a) -> int:
    lib, mans, req, asm = _assemble(a)
    raw = json.loads(Path(a.request).read_text(encoding="utf-8"))
    payload = raw["messages"] if isinstance(raw, dict) else raw
    tools = raw.get("tools") if isinstance(raw, dict) else None
    host_sha = raw.get("host_system_sha256", "") if isinstance(raw, dict) \
        else ""
    rec = api.verify(payload, asm, lib=lib, manifests=mans,
                     observation_point=a.observation_point,
                     observable_boundary=a.observable_boundary,
                     host_system_sha256=host_sha, tools_offered=tools)
    print(json.dumps(rec.as_dict(), indent=2, ensure_ascii=False))
    return 0 if rec.ok else 1


def cmd_propose(args) -> int:
    """Day-0: what is in this repo, and the markers that would declare it."""
    from . import propose as _p
    try:
        result = _p.plan(args.source, list(args.skill),
                         skills_dir=args.skills_dir)
    except _p.ProposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1   # tool or configuration error

    n, marked = len(result["proposals"]), len(result["already_marked"])
    if getattr(args, "json", False):
        out = dict(result)
        if args.write and n:
            out["applied"] = _p.apply(result)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return (2 if n else 0)   # 2 = a reported finding

    print(f"{len(result['skills'])} skill(s) · {marked} already marked · "
          f"{n} candidate declaration(s) the build would keep once marked\n")
    if n:
        print(_p.diff(result))
        print()
    if args.write and n:
        applied = _p.apply(result)
        print(f"wrote {applied['markers_inserted']} marker(s) into "
              f"{len(applied['files_written'])} file(s)")
        print(f"\n{applied['_next']}")
    elif n:
        print("Nothing on disk has changed. Add `--write` to insert these, "
              "then review with `git diff`.")
    else:
        print("No unmarked candidates. Either everything found is already "
              "marked, or the extractor found nothing here.")
    print(f"\n{_p.RECALL_NOTE}")
    return (2 if n else 0)   # 2 = a reported finding


def register(sub) -> None:
    """Register the `container` group. Called first, so help lists it first."""
    g = sub.add_parser(
        "container",
        help="[0.2] build, check, preview, attest and verify declarations",
        description="ConstraintRelay container — the 0.2 developer preview.")
    gs = g.add_subparsers(dest="container_cmd", required=True,
                          metavar="<subcommand>")

    def common(p, *, need_version=True):
        p.add_argument("--source", required=True,
                       help="root directory the skills live under")
        p.add_argument("--skill", action="append", required=True,
                       help="skill id, e.g. skills/mine; repeatable")
        if need_version:
            p.add_argument("--source-version", required=True,
                           help="the version identity for this build/request")

    pr = gs.add_parser(
        "propose",
        help="[1.0] day-0: show the declarations the extractor finds, and "
             "insert the markers you would have written")
    common(pr, need_version=False)
    pr.add_argument("--skills-dir", default="skills")
    pr.add_argument("--write", action="store_true",
                    help="insert the markers into your files. Without this "
                         "NOTHING on disk changes. Review with `git diff`.")
    pr.set_defaults(func=cmd_propose)

    b = gs.add_parser("build", help="read skills into a library file")
    common(b)
    b.add_argument("--out", required=True)
    b.add_argument("--skills-dir", default="skills")
    b.add_argument("--reviewer", default="developer:unnamed")
    b.add_argument("--built-at", default="",
                   help="pin the timestamp; library_content_id is reproducible "
                        "regardless, build_run_id is not")
    b.set_defaults(func=cmd_build)

    c = gs.add_parser("check",
                      help="format, version agreement and confirmation "
                           "record — NOT author identity or legal effect")
    common(c, need_version=False)
    c.add_argument("--library", required=True)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    at = gs.add_parser("attest",
                       help="write a DRAFT manifest; never a confirmation")
    # ONE skill, not a repeatable list: a manifest belongs to exactly one
    # skill, and `--skill` appending here produced a list where `write_draft`
    # wanted a string. Caught by the clean-environment acceptance run.
    at.add_argument("--source", required=True,
                    help="root directory the skills live under")
    at.add_argument("--skill", required=True,
                    help="the ONE skill this manifest is for")
    at.add_argument("--library", required=True)
    at.add_argument("--confirmed-by", default="")
    at.add_argument("--confirmed-at", default="")
    at.add_argument("--evidence-ref", default="")
    at.add_argument("--fixture", action="store_true",
                    help="write a clearly-labelled engineering fixture")
    at.set_defaults(func=cmd_attest)

    def assemble_opts(p):
        common(p)
        p.add_argument("--library", required=True)
        p.add_argument("--request-id", default="cli-1")
        p.add_argument("--category", action="append", default=[],
                       choices=list(C.CATEGORIES),
                       help="category to DELIVER; repeatable. Default: "
                            "author_confirmed only")
        p.add_argument("--accept-fixtures", action="store_true",
                       help="permit fixture manifests (refused by default)")
        p.add_argument("--enabled-by", default="",
                       help="identity enabling a non-default category, as you "
                            "supply it; unset is recorded unknown, never "
                            "inferred")
        p.add_argument("--enabled-at", default="")
        p.add_argument("--decision-point", default="cli")

    pv = gs.add_parser("preview",
                       help="the capsule; --json adds the assembly identity "
                            "for non-Python hosts")
    assemble_opts(pv)
    pv.add_argument("--json", action="store_true",
                    help="machine-readable: capsule text + assembly identity")
    pv.set_defaults(func=cmd_preview)

    vf = gs.add_parser("verify",
                       help="read a captured request from a file, emit a "
                            "receipt")
    assemble_opts(vf)
    vf.add_argument("--request", required=True,
                    help="JSON file: either a message list, or an object with "
                         "`messages`, optional `tools` and optional "
                         "`host_system_sha256`")
    vf.add_argument("--observation-point", required=True,
                    help="where you read the payload. Required: a receipt "
                         "that does not say what it could see is not evidence")
    vf.add_argument("--observable-boundary", required=True,
                    help="what that reading cannot reach. Required")
    vf.set_defaults(func=cmd_verify)


def dispatch(a) -> int:
    fn = getattr(a, "func", None)
    if fn is None:
        print("no container subcommand", file=sys.stderr)
        return 2
    return fn(a)
