"""The ConstraintFlow command surface.

**Phase 1 is the current development priority**, and its commands — `import`,
`confirm`, `skills`, `constraints`, `constraint`, `diff`, `update`, `retire`,
`deliver`, `report` — live in `cli_phase1.py` and are registered by `main()`
here. Everything below this paragraph describes the v0.7 surface, which is
preserved unchanged and paused as a product surface rather than removed.

v0.7 moves the centre of the product. The store is authoritative (§3), the unit
of delivery is a capsule (§4), and the thing a build consumes is a **receipt**
(§5). So the commands that matter are:

    receipt  §5   can initial-load delivery of the applicable capsule be established?
    capsule  §4   what is in the capsule, and does the managed mirror match the store?
    store    §16  import author-confirmed markers into the authoritative store
    targets  §9   is the target configuration itself unchanged, or weakened?

and the v0.6 surface is retained because v0.7 supersedes it on *semantics*, not by
deleting working checks:

    verify   evaluate marked critical constraints in the current revision
    budget   per-profile, per-scenario delivery margin in the native unit
    impact   **paused as a product surface** (§10); retained as an internal
             primitive, and labelled as one on every invocation
    scan     the v0.5 block-level surface, used by the field-test runners
    profiles, fingerprint

Exit codes are one fixed namespace across `verify` and `impact`, and `1` never
means "a finding was reported" — a CI shell seeing `$? == 1` has to be able to
tell "ConstraintFlow found a problem" from "ConstraintFlow itself broke":

    0  completed; no configured critical delivery failure / no blocking change
    1  tool, configuration, profile or runtime error
    2  confirmed critical delivery failure / confirmed blocking change
    3  the critical result is indeterminate

Precedence is `1 > 2 > 3 > 0`, tracked with explicit flags rather than `max()` —
`max()` would let 3 shadow 2, which is backwards.

§15 is enforced in the output, not promised by it: every figure is labelled with
its profile and scenario, no total spans two profiles, and the word
"guaranteed" does not appear. The claim is *delivered on initial load under
profile X, scenario Y*.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from . import budget as budget_mod
from . import naming
from . import capsule as capsule_mod
from . import cli_container
from . import cli_phase1
from . import cli_receipts
from . import config as config_mod
from . import impact as impact_mod
from . import receipt as receipt_mod
from . import skillstore as skillstore_mod
from . import store as store_mod
from . import targets as targets_mod
from .discover import (CONTEXT_FILENAMES, collect_bootstrap_files,  # noqa: F401
                       collect_context_files, collect_skills, discover,
                       strip_frontmatter, with_loader_view)
from .fingerprint import check as fingerprint_check
from .markers import scan_project
from .profiles import InputFile
from .registry import REGISTRY, SHIPPED, Registered, profile_ids, resolve
from .scan import ProfileResult, render, scan_under_profile, to_json
from .scenario import (Scenario, ScenarioResult, evaluate_scenario, expand_set,
                       generate, load_set)
from .verdict import Cause, Status
from .version import IMPLEMENTATION

EXIT_OK, EXIT_ERROR, EXIT_ENFORCE, EXIT_INDETERMINATE = 0, 1, 2, 3

_PROFILE_CHOICES = sorted(set(REGISTRY) | set(profile_ids()))


# ---------------------------------------------------------------------------
# Target and scenario selection
# ---------------------------------------------------------------------------

def _implicit_targets(keys: list[str]) -> list[config_mod.Target]:
    """Targets for an invocation with no `.constraintflow.toml`.

    §8: a profile named on the command line is still a *declared* target. The
    only difference from a configured one is that nothing recorded the
    parameters, so §6's conditional analysis has nothing to work with.
    """
    return [config_mod.Target(name=k, profile=REGISTRY[k].profile_id,
                              profile_key=k, cwd=".", implied=True)
            for k in keys]


def select_targets(root: Path, profiles: "list[str] | None",
                   target_names: "list[str] | None") -> tuple[list, str]:
    """Declared targets for this invocation, and a one-line provenance note."""
    cfg_path = config_mod.find(root)
    if profiles:
        keys = [resolve(p) for p in profiles]
        return _implicit_targets(keys), "profiles named on the command line"
    if cfg_path is not None:
        cfg = config_mod.load(cfg_path)
        if target_names:
            return [cfg.target(n) for n in target_names], f"targets from {cfg_path.name}"
        return list(cfg.targets), f"all targets in {cfg_path.name}"
    if target_names:
        raise config_mod.ConfigError(
            f"--target given but no {config_mod.CONFIG_NAME} found at {root}")
    return (_implicit_targets(SHIPPED),
            "no .constraintflow.toml; defaulting to the replayed profiles")


def evaluate_targets(root: Path, targets: list, *, allow_on_request: bool,
                     harness_root: Path | None,
                     conway_active: "list[str] | None" = None
                     ) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for target in targets:
        if conway_active and REGISTRY[target.profile_key].needs_declaration:
            # A CLI declaration is the same kind of input as `[target.state]`,
            # so it is merged into the target's state rather than threaded
            # through as a special case.
            # Keyword arguments on purpose: `Target` grew a `delivery` field
            # between `context` and `implied` (§13), and a positional rebuild
            # silently shifted `implied` into it.
            target = config_mod.Target(
                name=target.name, profile=target.profile,
                profile_key=target.profile_key, cwd=target.cwd,
                parameters=dict(target.parameters),
                state={**target.state, "active_skills": conway_active},
                context=dict(target.context), delivery=dict(target.delivery),
                implied=target.implied)
        for scenario in generate(target, root):
            results.append(evaluate_scenario(
                scenario, allow_on_request=allow_on_request,
                harness_root=harness_root))
    return results


# ---------------------------------------------------------------------------
# v0.7 §5 receipt, §4 capsule, §16 store, §9 targets
# ---------------------------------------------------------------------------

def _load_store(root: Path):
    path = store_mod.find(root)
    if path is None:
        raise store_mod.StoreError(
            f"no {store_mod.STORE_DIR}/{store_mod.STORE_NAME} at {root}. The store "
            f"is authoritative in v0.7 §3; `{naming.cmd('store', 'import')}` "
            f"creates one from the markers already in this repository.")
    return store_mod.load(path)


def _receipt_exit(receipts: list) -> int:
    """§8's table, in the exit-code namespace the rest of the tool already uses.

        DELIVERY_ESTABLISHED   -> 0
        DELIVERY_FAILED        -> 2
        DELIVERY_INDETERMINATE -> 3  (fail closed)
        CONDITIONAL            -> 3  (fail closed; reported as indeterminate)

    2 outranks 3, unchanged from v0.6: a confirmed failure is more actionable than
    an undetermined one, and `max()` would put it the other way round.
    """
    if any(r.outcome == receipt_mod.FAILED for r in receipts):
        return EXIT_ENFORCE
    if any(r.outcome == receipt_mod.INDETERMINATE for r in receipts):
        return EXIT_INDETERMINATE
    return EXIT_OK


def cmd_receipt(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    try:
        store = _load_store(root)
        targets, provenance = select_targets(root, args.profiles, args.targets)
    except (store_mod.StoreError, config_mod.ConfigError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    protected: tuple[str, ...] = ()
    cfg_path = config_mod.find(root)
    if cfg_path is not None:
        try:
            protected = config_mod.load(cfg_path).protected
        except config_mod.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    harness_root = Path(args.harness_root).expanduser().resolve() \
        if args.harness_root else None
    conway = ([n.strip() for n in args.conway_active.split(",") if n.strip()]
              if args.conway_active else None)

    receipts = []
    for target in targets:
        if conway and REGISTRY[target.profile_key].needs_declaration:
            # Keyword arguments on purpose: `Target` grew a `delivery` field
            # between `context` and `implied` (§13), and a positional rebuild
            # silently shifted `implied` into it.
            target = config_mod.Target(
                name=target.name, profile=target.profile,
                profile_key=target.profile_key, cwd=target.cwd,
                parameters=dict(target.parameters),
                state={**target.state, "active_skills": conway},
                context=dict(target.context), delivery=dict(target.delivery),
                implied=target.implied)
        for scenario in generate(target, root):
            try:
                receipts.append(receipt_mod.issue(
                    scenario, store, root, protected=protected,
                    harness_root=harness_root,
                    development_evidence=args.development_evidence))
            except OSError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_ERROR

    code = _receipt_exit(receipts)
    if args.json:
        print(json.dumps({
            "command": "receipt",
            "note": ("One receipt per profile and scenario. No receipt spans two "
                     "profiles, and no figure is summed across them."),
            "implementation": IMPLEMENTATION,
            "protected_patterns": list(protected),
            "receipts": [r.as_dict() for r in receipts],
            "ci": {"exit_code": code},
        }, indent=2, ensure_ascii=False))
        return code

    print(f"target selection: {provenance}")
    print(f"store: {store.path} — {len(store.constraints)} constraint(s), "
          f"{len(store.active)} active; {store.delivery.describe()}")
    if args.development_evidence:
        print("DEVELOPMENT EVIDENCE requested: the uncapped delivery computation is "
              "recorded alongside each receipt and is NOT a receipt.")
    if not protected:
        print("protected blocks: none configured — §6 fails a capsule only for a "
              "*configured* protected block")
    else:
        print(f"protected blocks: {', '.join(protected)}")
    print()
    for r in receipts:
        print(r.render())
        print()
    if not receipts:
        print("no scenario was generated for any declared target")
    return code


def cmd_capsule(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    try:
        store = _load_store(root)
        targets, provenance = select_targets(root, args.profiles, args.targets)
    except (store_mod.StoreError, config_mod.ConfigError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    worst = EXIT_OK
    for target in targets:
        for scenario in generate(target, root):
            context = scenario.declared_context(root)
            cap = capsule_mod.build(store, context)
            label = scenario.label(root)
            if args.json:
                print(json.dumps({"command": "capsule", "scenario": label,
                                  **cap.as_dict()}, indent=2, ensure_ascii=False))
            else:
                print(f"## {label}")
                print(f"declared context: {context.describe()}")
                print(f"members: {len(cap.members)} {capsule_mod.CLAIM}; "
                      f"excluded: {len(cap.excluded)}; no minimization (§4)")
                for m in cap.members:
                    print(f"  IN   {m.constraint.id:<14} v{m.constraint.version}  "
                          f"{m.decision.reason}")
                for m in cap.excluded:
                    print(f"  OUT  {m.constraint.id:<14} {m.decision.reason}")
                print(f"digest: {cap.digest}")

            # §13: the target may place the capsule somewhere else, because one
            # canonical copy has to reach two harnesses through their own
            # mechanisms. The store's `[delivery]` is the project default.
            try:
                delivery = (store_mod._parse_delivery(scenario.delivery, store.path)
                            if scenario.delivery else store.delivery)
            except store_mod.StoreError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_ERROR
            if delivery.mode != store_mod.MANAGED_MIRROR:
                continue
            where = delivery.mirror_path
            mirror = root / where
            if not mirror.is_file():
                print(f"  mirror {where}: file does not exist", file=sys.stderr)
                worst = max(worst, EXIT_ERROR)
                continue
            source = mirror.read_text(encoding="utf-8")
            try:
                state = capsule_mod.check_mirror(cap, source)
            except capsule_mod.MirrorError as exc:
                print(f"  mirror {where}: {exc}", file=sys.stderr)
                worst = max(worst, EXIT_ERROR)
                continue
            if args.write:
                mirror.write_text(capsule_mod.write_region(cap, source),
                                  encoding="utf-8")
                print(f"  mirror {where}: written")
            elif state.present and state.matches:
                print(f"  mirror {where}: matches the store")
            elif state.present:
                print(f"  mirror {where}: DRIFT — {state.detail}")
                worst = EXIT_ENFORCE if worst != EXIT_ERROR else worst
            else:
                print(f"  mirror {where}: no managed region yet — run with --write")
                worst = EXIT_ENFORCE if worst != EXIT_ERROR else worst
            break          # one mirror per target; no need to repeat per scenario
    print(f"\ntarget selection: {provenance}")
    return worst


def cmd_store(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR

    if args.action == "show":
        try:
            store = _load_store(root)
        except store_mod.StoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if args.json:
            print(json.dumps({"command": "store show",
                              "path": str(store.path),
                              "delivery": store.delivery.describe(),
                              "constraints": [c.as_dict() for c in store.constraints]},
                             indent=2, ensure_ascii=False))
            return EXIT_OK
        print(f"{store.path}\n{store.delivery.describe()}\n")
        for c in store.constraints:
            print(f"{c.id:<14} v{c.version:<3} {c.status:<8} {c.scope.describe()}")
            print(f"  provenance: {c.provenance}")
            if c.machine_enforcement_declared:
                print(f"  machine enforcement declared at "
                      f"{c.enforcement_point or '(point not named)'} — §7 accounting "
                      f"only; still in the capsule")
        return EXIT_OK

    # import
    existing = store_mod.find(root)
    if existing is not None and not args.force:
        print(f"error: {existing} already exists; §3 makes it authoritative, so "
              f"importing over it would discard the canonical copy. Use --force to "
              f"replace it deliberately.", file=sys.stderr)
        return EXIT_ERROR

    # Markers live in instruction files, and which files those are is a discovery
    # question rather than a profile question, so every context file and skill is
    # scanned regardless of which profile would load it. A marker in a file this
    # profile ignores is still an author-confirmed constraint.
    context_files, skills = discover(root)
    files = [(f.name, f.file_text) for f in context_files + skills]
    scan = scan_project(files)
    if scan.issues:
        for issue in scan.issues:
            print(f"CONFIG {issue}", file=sys.stderr)
        return EXIT_ERROR

    constraints = store_mod.importable(scan, when=args.when or "imported")
    if not constraints:
        print("no author-confirmed markers found. §16: only an explicit "
              "`<!-- constraintflow: critical id=... -->` marker is an author "
              "confirmation; a --critical regex hit is a candidate and is never "
              "promoted automatically.", file=sys.stderr)
        return EXIT_ERROR

    delivery = store_mod.Delivery(
        mode=args.mode,
        mirror_path=args.mirror_path or "",
        generator=args.generator or "",
        source_layer=args.source_layer or "")
    text = store_mod.render_store(constraints, delivery)
    # Parsed back before it is written, so an invalid store is never left on disk
    # to be treated as authoritative on the next run. This is also where a bad
    # `--mode` / `--mirror-path` combination is caught, by the same validation the
    # loader uses rather than by a second copy of the rules here.
    try:
        store_mod.parse(tomllib.loads(text), store_mod.store_path(root))
    except (store_mod.StoreError, tomllib.TOMLDecodeError) as exc:
        print(f"error: the imported store would be invalid: {exc}", file=sys.stderr)
        return EXIT_ERROR
    out = store_mod.store_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"imported {len(constraints)} author-confirmed constraint(s) into {out}")
    for c in constraints:
        print(f"  {c.id:<14} {c.provenance}")
    print("\nThe store is now authoritative (§3). The original marker sites are no "
          "longer independent authoritative copies of these constraints.")
    if delivery.mode == store_mod.MANAGED_MIRROR:
        print(f"Run `{naming.cmd('capsule', root)} --write` to generate the "
              f"managed mirror in {delivery.mirror_path}.")
    return EXIT_OK


def cmd_targets(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    cfg_path = config_mod.find(root)
    if cfg_path is None:
        print(f"error: no {config_mod.CONFIG_NAME} at {root}; §9 compares declared "
              f"targets, and there are none declared", file=sys.stderr)
        return EXIT_ERROR
    try:
        cfg = config_mod.load(cfg_path)
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    current = targets_mod.snapshot(cfg, root, when=args.when or "")
    path = targets_mod.baseline_path(root)

    if args.record:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current.as_dict(), indent=2) + "\n",
                        encoding="utf-8")
        print(f"recorded {len(current.targets)} target(s) in {path}")
        for name, rec in sorted(current.targets.items()):
            print(f"  {name:<20} {rec.profile_id:<44} "
                  f"{len(rec.scenarios)} scenario(s)")
        return EXIT_OK

    if not path.is_file():
        print(f"error: no baseline at {path}. §9 makes target configuration a "
              f"reviewed input, so the baseline is written deliberately: "
              f"`{naming.cmd('targets', root)} --record`. It is not created "
              f"automatically, because a baseline that reappears when deleted is "
              f"not a baseline.", file=sys.stderr)
        return EXIT_ERROR
    try:
        before = targets_mod.load(path)
    except targets_mod.BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if before.implementation != IMPLEMENTATION:
        # §10: a difference must be attributable to the project, not to us. The
        # baseline's scenario list is analyzer-derived, so a version change makes
        # the comparison meaningless rather than merely stale.
        print(f"INDETERMINATE the baseline was recorded by {before.implementation!r} "
              f"and this is {IMPLEMENTATION!r}. §10 requires the same "
              f"implementation version on both sides of an internal comparison; "
              f"re-record the baseline and review the diff.")
        return EXIT_INDETERMINATE

    # §9 / item 6: approvals are read from the persisted record, never from the
    # command line. `--approve` WRITES one (with `--reason`) rather than waiving a
    # change for the duration of one invocation.
    apath = targets_mod.approvals_path(root)
    try:
        approvals = targets_mod.load_approvals(apath)
    except targets_mod.BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.approve:
        if not args.reason:
            print("error: --approve requires --reason. §9 makes a target change a "
                  "reviewed input, and an unexplained waiver is not a review.",
                  file=sys.stderr)
            return EXIT_ERROR
        declared = {t.name for t in cfg.targets} | set(before.targets)
        unknown = sorted(set(args.approve) - declared)
        if unknown:
            print(f"error: cannot approve unknown target(s) "
                  f"{', '.join(unknown)}; declared or baselined: "
                  f"{', '.join(sorted(declared))}", file=sys.stderr)
            return EXIT_ERROR
        for name in args.approve:
            baselined = before.targets.get(name)
            approvals[name] = targets_mod.Approval(
                name, args.reason, args.when or "",
                before.recorded_at,
                # The id from the baseline where the target existed there, so an
                # approval for a REMOVED target still records which evaluation was
                # waived rather than only its name.
                target_id=(baselined.target_id if baselined else
                           targets_mod.stable_target_id(cfg.target(name))
                           if any(t.name == name for t in cfg.targets) else ""))
        targets_mod.write_approvals(apath, approvals)
        print(f"recorded {len(args.approve)} approval(s) in {apath}")
        for name in args.approve:
            print(f"  {name}: {args.reason}")

    changes = targets_mod.compare(before, current, approved=set(approvals))
    blocking = [c for c in changes if c.blocking]
    code = EXIT_ENFORCE if blocking else EXIT_OK
    if args.json:
        print(json.dumps({
            "command": "targets", "implementation": IMPLEMENTATION,
            "baseline": str(path), "recorded_at": before.recorded_at,
            "approvals_record": str(apath),
            "changes": [{"classification": c.classification, "target": c.name,
                         "detail": c.detail, "approved": c.approved,
                         "approval_reason": (approvals[c.name].reason
                                             if c.approved and c.name in approvals
                                             else None),
                         "blocking": c.blocking} for c in changes],
            "ci": {"exit_code": code}}, indent=2, ensure_ascii=False))
        return code

    print(f"baseline {path} recorded {before.recorded_at or 'at an unstated time'}")
    if not changes:
        print("target configuration is unchanged")
        return EXIT_OK
    for c in changes:
        flag = ""
        if c.approved:
            record = approvals.get(c.name)
            flag = (f"  [approved: {record.reason}]" if record
                    else "  [approved]")
        tag = "FAIL" if c.blocking else "note"
        print(f"{tag} {c.classification:<28} {c.name}: {c.detail}{flag}")
    if blocking:
        print("\nA blocking target change is how a check disappears without the "
              "finding being fixed (§9). Approve it explicitly with --approve "
              "<target> if it is intended.")
    return code


# ---------------------------------------------------------------------------
# §9 verify
# ---------------------------------------------------------------------------

def verify_exit(results: list[ScenarioResult], *, allow_on_request: bool
                ) -> tuple[int, list[str]]:
    """§9's exit code and the lines that explain it."""
    notes: list[str] = []
    enforce = False
    indeterminate = False
    for r in results:
        reg = r.scenario.registered
        advisory = reg.replay_pending
        tail = "  (advisory: profile is replay-pending)" if advisory else ""
        if r.markers.issues:
            # Configuration error -> exit 1, handled by the caller. Listed here
            # so the operator sees every issue at once.
            notes += [f"CONFIG {r.profile_id}: {i}" for i in r.markers.issues]
            continue
        for c in r.constraints:
            where = f"[{r.profile_id}] {c.constraint.label} {c.constraint.locator}"
            if c.status is Status.UNKNOWN:
                notes.append(f"INDETERMINATE {where}: {c.reason}{tail}")
                if not advisory:
                    indeterminate = True
            elif c.status is Status.AVAILABLE_ONLY_ON_REQUEST and allow_on_request:
                notes.append(f"warning {where}: available only on request, "
                             f"allowed by --allow-on-request{tail}")
            elif c.blocking:
                notes.append(f"FAIL {where}: {c.status.value} "
                             f"({c.cause.value}){tail}")
                if not advisory:
                    enforce = True
    if enforce:
        return EXIT_ENFORCE, notes
    if indeterminate:
        return EXIT_INDETERMINATE, notes
    return EXIT_OK, notes


def render_verify(results: list[ScenarioResult], root: Path, *,
                  all_scenarios: bool) -> str:
    """§5/§15 rendering: grouped by profile, folded unless §5 says to expand."""
    expand = set(range(len(results))) if all_scenarios else expand_set(results)
    by_profile: dict[str, list[int]] = {}
    for i, r in enumerate(results):
        by_profile.setdefault(r.profile_id, []).append(i)

    out: list[str] = []
    if len(by_profile) > 1:
        out += ["Each block below is an evaluation under one qualified profile. "
                "Counts are per profile and per scenario, and are never summed "
                "across profiles.", ""]
    for pid, idxs in by_profile.items():
        out.append(f"## {pid}")
        fp = results[idxs[0]].fingerprint
        out.append(f"loader surface: {fp.state} — {fp.message}")
        folded = 0
        for i in idxs:
            r = results[i]
            if i not in expand:
                folded += 1
                continue
            out.append(f"\n### scenario {r.scenario.label(root)}")
            if r.scenario.parameters:
                out.append(f"declared parameters: "
                           f"{json.dumps(r.scenario.parameters, sort_keys=True)}")
            if r.skipped:
                out.append(f"not evaluated: {r.skipped}")
                for issue in r.markers.issues:
                    out.append(f"  config error: {issue}")
                continue
            if not r.constraints:
                out.append("no critical constraints marked in this scenario")
                continue
            for c in r.constraints:
                label = c.constraint.label
                tag = (c.status.value if c.cause in (Cause.NONE, Cause.TRUNCATED)
                       else f"{c.status.value}/{c.cause.value}")
                out.append(f"  {tag:<34} {label} {c.constraint.locator}")
                if c.reason:
                    out.append(f"      {c.reason}")
                for path in c.retrieval[:2]:
                    out.append(f"      reachable by: {path}")
                if c.lost_excerpt:
                    out.append(f"      lost: {c.lost_excerpt}")
        if folded:
            out.append(f"\n{folded} further scenario(s) under {pid} summarised: "
                       f"no critical failure, no indeterminate critical result, "
                       f"and no difference from a neighbouring scenario. "
                       f"Use --all-scenarios to expand.")
        total = sum(len(results[i].constraints) for i in idxs)
        untracked = sum(1 for i in idxs for c in results[i].constraints
                        if not c.tracked)
        out += [f"\n{pid}: {len(idxs)} scenario(s), {total} critical constraint "
                f"evaluation(s)" + (f", {untracked} UNTRACKED (legacy marker, "
                                    f"excluded from Impact)" if untracked else ""),
                ""]
    return "\n".join(out)


def verify_json(results: list[ScenarioResult], root: Path, code: int,
                notes: list[str]) -> dict:
    profiles: dict[str, dict] = {}
    for r in results:
        entry = profiles.setdefault(r.profile_id, {
            "profile_id": r.profile_id,
            "loader_surface": r.fingerprint.as_dict(),
            "scenarios": []})
        entry["scenarios"].append({
            "scenario": r.scenario.label(root),
            "target": r.scenario.target,
            "declared_parameters": r.scenario.parameters,
            "declared_state": r.scenario.state,
            "not_evaluated": r.skipped or None,
            "marker_issues": [str(i) for i in r.markers.issues],
            "constraints": [c.as_dict() for c in r.constraints]})
    return {
        "command": "verify",
        "note": ("Results hold under the declared qualified target profile and "
                 "the stated scenario. No figure spans two profiles."),
        "profiles": list(profiles.values()),
        "ci": {"exit_code": code, "notes": notes},
    }


def cmd_verify(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    try:
        targets, provenance = select_targets(root, args.profiles, args.targets)
    except (config_mod.ConfigError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    harness_root = Path(args.harness_root).expanduser().resolve() \
        if args.harness_root else None
    conway = ([n.strip() for n in args.conway_active.split(",") if n.strip()]
              if args.conway_active else None)
    try:
        results = evaluate_targets(root, targets, allow_on_request=args.allow_on_request,
                                   harness_root=harness_root, conway_active=conway)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    code, notes = verify_exit(results, allow_on_request=args.allow_on_request)
    if any(r.markers.issues for r in results):
        code = EXIT_ERROR          # §9 precedence: 1 outranks everything

    if args.json:
        print(json.dumps(verify_json(results, root, code, notes), indent=2,
                         ensure_ascii=False))
    else:
        print(f"target selection: {provenance}\n")
        print(render_verify(results, root, all_scenarios=args.all_scenarios))
        if notes:
            print("\n".join(notes))
    return code


# ---------------------------------------------------------------------------
# §10 impact
# ---------------------------------------------------------------------------

def _side(root: Path, profiles, targets, *, allow_on_request: bool,
          harness_root: Path | None, conway) -> tuple[list[ScenarioResult], Path]:
    sel, _ = select_targets(root, profiles, targets)
    return evaluate_targets(root, sel, allow_on_request=allow_on_request,
                            harness_root=harness_root, conway_active=conway), root


#: §10: the standalone `impact` product surface is paused. The mechanism is
#: retained — pre/post capsule insertion comparison in `receipt.py` is the same
#: before/after idea, and repository, target, and profile-upgrade comparisons all
#: still need it — so the command is not removed. It is labelled on every
#: invocation instead, because a paused surface that looks like a shipping one is
#: how a v1 promise gets made by accident.
IMPACT_BANNER = (
    "note: `impact` is an internal primitive in v0.7, not a v1 product surface "
    "(§10). The v1 build-facing command is `receipt`.")


def cmd_impact(args) -> int:
    print(IMPACT_BANNER, file=sys.stderr)
    after_root = Path(args.path).resolve()
    if not after_root.is_dir():
        print(f"error: {after_root} is not a directory", file=sys.stderr)
        return EXIT_ERROR

    conway = ([n.strip() for n in args.conway_active.split(",") if n.strip()]
              if args.conway_active else None)
    harness_root = Path(args.harness_root).expanduser().resolve() \
        if args.harness_root else None
    workspace = None
    try:
        if args.before_rev:
            workspace = impact_mod.revision_workspace()
            before_root = impact_mod.materialize(
                after_root, args.before_rev, Path(workspace.name))
        elif args.before:
            before_root = Path(args.before).resolve()
            if not before_root.is_dir():
                print(f"error: --before {before_root} is not a directory",
                      file=sys.stderr)
                return EXIT_ERROR
        else:
            print("error: impact needs --before-rev or --before", file=sys.stderr)
            return EXIT_ERROR

        try:
            before, before_root = _side(
                before_root,
                args.before_profiles or args.profiles, args.targets,
                allow_on_request=args.allow_on_request,
                harness_root=harness_root, conway=conway)
            after, after_root = _side(
                after_root,
                args.after_profiles or args.profiles, args.targets,
                allow_on_request=args.allow_on_request,
                harness_root=harness_root, conway=conway)
        except (config_mod.ConfigError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR

        config_issues = [str(i) for side in (before, after)
                         for r in side for i in r.markers.issues]
        if config_issues:
            for line in config_issues:
                print(f"CONFIG {line}", file=sys.stderr)
            return EXIT_ERROR

        approved = set(args.approve_removal or [])
        impacts = impact_mod.compare(before, after, before_root, after_root,
                                     approved)
    except impact_mod.RevisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if workspace is not None:
            workspace.cleanup()

    blocking = [e for si in impacts for e in si.blocking]
    indeterminate = [e for si in impacts for e in si.indeterminate]
    code = (EXIT_ENFORCE if blocking else
            EXIT_INDETERMINATE if indeterminate else EXIT_OK)

    if args.json:
        print(json.dumps({
            "command": "impact",
            "surface": "internal primitive — paused as a product surface (v0.7 §10)",
            "implementation": IMPLEMENTATION,
            "version_identity": ("both sides were analyzed by this one process, so "
                                 "§10's same-implementation requirement holds by "
                                 "construction"),
            "note": ("Each scenario below is one profile and one scenario. "
                     "Persistent baseline debt is reported but is not an "
                     "Impact regression (§10.5)."),
            "before": {"root": str(before_root), "rev": args.before_rev or None},
            "after": {"root": str(after_root)},
            "untracked_excluded": {
                "before": impact_mod.untracked_count(before),
                "after": impact_mod.untracked_count(after)},
            "scenarios": [{
                "scenario": si.key,
                "before_profile": si.before_profile,
                "after_profile": si.after_profile,
                "profile_changed": si.profile_changed,
                "note": si.note or None,
                "categories": si.counts(),
                "constraints": [e.as_dict() for e in si.entries]}
                for si in impacts],
            "ci": {"exit_code": code,
                   "blocking": [e.id for e in blocking],
                   "indeterminate": [e.id for e in indeterminate]},
        }, indent=2, ensure_ascii=False))
        return code

    print(render_impact(impacts, before, after))
    if blocking:
        for e in blocking:
            print(f"FAIL {e.classification} {e.id} {e.locator}: {e.reason}")
    if indeterminate:
        for e in indeterminate:
            print(f"INDETERMINATE {e.classification} {e.id} {e.locator}: "
                  f"{e.reason}")
    if not blocking and not indeterminate:
        print("no newly introduced blocking delivery regression or removal")
    return code


def render_impact(impacts: list, before: list[ScenarioResult],
                  after: list[ScenarioResult]) -> str:
    out: list[str] = []
    untracked = impact_mod.untracked_count(before) + impact_mod.untracked_count(after)
    if untracked:
        out += [f"{untracked} legacy marker(s) carry no id and are UNTRACKED for "
                f"cross-revision Impact (§3).", ""]
    for si in impacts:
        out.append(f"## scenario {si.key}")
        if si.profile_changed:
            out.append(f"profile: {si.before_profile} -> {si.after_profile}")
        else:
            out.append(f"profile: {si.after_profile}")
        if si.note:
            out.append(si.note)
        counts = si.counts()
        out.append("  ".join(f"{k.lower()} {v}" for k, v in sorted(counts.items()))
                   or "no tracked critical constraints")
        for e in si.entries:
            if not e.classification:
                continue
            arrow = (f"{e.before_status.value if e.before_status else '—'} -> "
                     f"{e.after_status.value if e.after_status else '—'}")
            flag = "  [approved]" if e.approved else ""
            out.append(f"  {e.classification:<28} {e.id:<14} {arrow}{flag}")
            if e.reason:
                out.append(f"      {e.reason}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# §11 budget
# ---------------------------------------------------------------------------

def cmd_budget(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    try:
        targets, provenance = select_targets(root, args.profiles, args.targets)
    except (config_mod.ConfigError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    conway = ([n.strip() for n in args.conway_active.split(",") if n.strip()]
              if args.conway_active else None)
    results = evaluate_targets(root, targets, allow_on_request=True,
                               harness_root=None, conway_active=conway)
    issues = [str(i) for r in results for i in r.markers.issues]
    if issues:
        for line in issues:
            print(f"CONFIG {line}", file=sys.stderr)
        return EXIT_ERROR

    wanted = set(args.ids or [])
    reports: list[budget_mod.BudgetReport] = []
    for r in results:
        for c in r.constraints:
            if wanted and c.constraint.id not in wanted:
                continue
            reports.append(budget_mod.measure(r.scenario, c.constraint, root))

    if args.json:
        print(json.dumps({
            "command": "budget",
            "note": ("Margin is per profile and per scenario, in that harness's "
                     "native unit. No cross-profile total exists."),
            "reports": [r.as_dict() for r in reports]}, indent=2,
            ensure_ascii=False))
        return EXIT_OK

    print(f"target selection: {provenance}\n")
    if not reports:
        print("no critical constraints to measure")
        return EXIT_OK
    current = ""
    for rep in reports:
        head = f"{rep.profile_id} @ {rep.scenario_location}"
        if head != current:
            current = head
            print(f"## {head}")
            print(f"native unit: {rep.unit_name}")
            if rep.parameters:
                print(f"declared parameters: "
                      f"{json.dumps(rep.parameters, sort_keys=True)}")
            else:
                print("declared parameters: none — a conditional analysis under "
                      "§6 would be needed for a parametric profile")
            print()
        print(f"  {rep.constraint_id or 'UNTRACKED':<14} {rep.locator:<28} "
              f"{rep.status.value}")
        for m in (rep.prefix, rep.suffix):
            if m is not None:
                print(f"      {m.render()}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------

def cmd_fingerprint(args) -> int:
    harness_root = Path(args.harness_root).expanduser().resolve() \
        if args.harness_root else None
    keys = [resolve(p) for p in args.profiles] if args.profiles else sorted(REGISTRY)
    worst = EXIT_OK
    for key in keys:
        reg = REGISTRY[key]
        c = fingerprint_check(key, harness_root)
        print(f"{reg.profile_id:<44} {c.state}")
        print(f"  {c.message}")
        if reg.loader_files:
            print(f"  loader-relevant surface: {', '.join(reg.loader_files)}")
        else:
            print("  loader-relevant surface: not declared")
        if c.requalification_required:
            worst = EXIT_ENFORCE
    return worst


# ---------------------------------------------------------------------------
# scan — the v0.5 surface, retained
# ---------------------------------------------------------------------------

def scan_repo(root: Path, profile_keys: list[str],
              critical: "list[re.Pattern[str]] | None" = None,
              conway_active: "list[str] | None" = None) -> list[ProfileResult]:
    context, skills = discover(root)
    results: list[ProfileResult] = []
    for key in profile_keys:
        reg = REGISTRY[key]
        profile = (reg.build(active=conway_active) if key == "conway-automaton"
                   else reg.build())
        if reg.consumes == "context":
            # CF-016, second site. `discover` returns one shared union for every
            # context profile, and for a loader whose roster is a literal in its own
            # source that union is simply the wrong load set — `scan --profile openclaw`
            # reported `CLAUDE.md`'s clauses DELIVERED_ON_LOAD, a false definite, and
            # said nothing about a delivered `BOOTSTRAP.md`. The receipt path was fixed
            # through `scenario.load_set`; this path had its own copy of the decision.
            #
            # A3's no-combining rule is why the union exists at all: one discovery pass
            # is cheap and several profiles want overlapping files. It stays for
            # searching loaders; a profile declaring a roster gets the roster.
            files = with_loader_view(
                collect_bootstrap_files(root, reg.bootstrap_roster,
                                        reg.absent_consumes_budget)
                if reg.bootstrap_roster else context,
                # Ruling item 6: the newline transform is the harness's reader, not a
                # property of the files, so it is applied per profile over one shared
                # discovery pass rather than baked into discovery.
                reg.source_newlines)
        else:
            files = [InputFile(f.name, f.path, f.file_text,
                               loader_view=strip_frontmatter(f.file_text))
                     for f in skills]
        if not files:
            continue
        results.append(scan_under_profile(files, profile, critical))
    return results


def evaluate(results: list[ProfileResult], fail_on: str, allow_on_request: bool,
             advisory: set[str]) -> tuple[int, list[str]]:
    """Exit code and the lines explaining it, for the v0.5 `scan` surface."""
    notes: list[str] = []
    enforce_hit = False
    indeterminate_hit = False
    enforcing = fail_on != "none"
    for r in results:
        binding = r.profile not in advisory
        for f in r.files:
            for b in f.blocks:
                if not b.critical:
                    if (binding and enforcing and fail_on == "loss"
                            and b.status in (Status.NOT_DELIVERED, Status.PARTIAL)):
                        enforce_hit = True
                    continue
                where = f"[{r.profile}] {f.name}:{b.locator}"
                tail = "" if binding else "  (advisory: profile is replay-pending)"
                if b.status is Status.UNKNOWN:
                    notes.append(f"INDETERMINATE {where} critical block is UNKNOWN{tail}")
                    if binding and enforcing:
                        indeterminate_hit = True
                elif b.status is Status.AVAILABLE_ONLY_ON_REQUEST:
                    if allow_on_request:
                        notes.append(f"warning {where} critical block is only "
                                     f"available on request{tail}")
                    else:
                        notes.append(f"FAIL {where} critical block is only "
                                     f"available on request{tail}")
                        if binding and enforcing:
                            enforce_hit = True
                elif b.status in (Status.NOT_DELIVERED, Status.PARTIAL):
                    notes.append(f"FAIL {where} critical block is "
                                 f"{b.status.value.lower()} ({b.cause.value}){tail}")
                    if binding and enforcing:
                        enforce_hit = True
    if enforce_hit:
        return EXIT_ENFORCE, notes
    if indeterminate_hit:
        return EXIT_INDETERMINATE, notes
    return EXIT_OK, notes


def summarize(results: list[ProfileResult]) -> str:
    lines = []
    for r in results:
        c = r.counts()
        total = sum(c.values())
        lost = c[Status.NOT_DELIVERED.value] + c[Status.PARTIAL.value]
        lines.append(
            f"{r.profile}: {total} blocks, {lost} not fully delivered, "
            f"{c[Status.AVAILABLE_ONLY_ON_REQUEST.value]} only on request, "
            f"{c[Status.UNKNOWN.value]} unknown")
    return "\n".join(lines)


def cmd_scan(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    try:
        critical = [re.compile(p, re.IGNORECASE) for p in args.critical]
    except re.error as exc:
        print(f"error: bad --critical regex: {exc}", file=sys.stderr)
        return EXIT_ERROR

    keys = [resolve(p) for p in (args.profiles or SHIPPED)]
    try:
        conway_active = ([n.strip() for n in args.conway_active.split(",") if n.strip()]
                         if args.conway_active else None)
        results = scan_repo(root, keys, critical, conway_active)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not results:
        print("no instruction files found under any selected profile", file=sys.stderr)
        return EXIT_ERROR

    advisory = {REGISTRY[k].build().name for k in keys if REGISTRY[k].replay_pending}
    code, notes = evaluate(results, args.fail_on, args.allow_on_request, advisory)

    if args.json:
        payload = to_json(results)
        payload["ci"] = {"exit_code": code, "fail_on": args.fail_on,
                         "allow_on_request": args.allow_on_request,
                         "critical_patterns": args.critical,
                         "critical_matched_against": "canonical full source-block text",
                         "advisory_profiles": sorted(advisory), "notes": notes}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render(results))
        print(summarize(results))
        if notes:
            print()
            print("\n".join(notes))
    return code


# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--profile", action="append", dest="profiles",
                   choices=_PROFILE_CHOICES, default=None,
                   help="repeatable; overrides .constraintflow.toml. Accepts the "
                        "short key or the family/profile id.")
    p.add_argument("--target", action="append", dest="targets", default=None,
                   help="repeatable; a target name from .constraintflow.toml")
    p.add_argument("--json", action="store_true")
    p.add_argument("--allow-on-request", action="store_true",
                   help="treat AVAILABLE_ONLY_ON_REQUEST as allowed rather than "
                        "a critical delivery failure")
    p.add_argument("--conway-active", default=None,
                   help="comma-separated skill names, in activation order, for "
                        "the conway profile. Equivalent to [target.state] "
                        "active_skills. Without it that profile reports UNKNOWN, "
                        "because the enabled set lives in the agent's database.")
    p.add_argument("--harness-root", default=None,
                   help="upstream harness checkout, for the §8 loader "
                        "fingerprint check. Omitted means results hold under the "
                        "declared qualified target profile.")


#: The getting-started list, with the program name DERIVED rather than typed
#: into each row. Column alignment is computed too: a rename of a different
#: length would otherwise leave this block ragged, and a ragged block is how a
#: reader learns the help text is not maintained.
_EPILOG_ROWS = (
    ("init <repo>", "what skills and candidate\nconstraints are in here?"),
    ("import <repo> --confirm-all", "confirm them into the store"),
    ("skills <repo>", "what the store now knows"),
    ("constraints <repo> --skill <s>", "one skill's constraints"),
    ("diff <repo>", "what changed since the import"),
)


def _product_epilog() -> str:
    left = [naming.cmd(inv) for inv, _ in _EPILOG_ROWS]
    width = max(len(s) for s in left) + 2
    lines = ["getting started", ""]
    for text, (_, what) in zip(left, _EPILOG_ROWS):
        head, *rest = what.split("\n")
        lines.append(f"  {text:<{width}}{head}")
        lines.extend(f"  {'':<{width}}{r}" for r in rest)
    lines += [
        "",
        "The paused harness-qualification commands are hidden from this list; "
        "they still",
        f"work and `{naming.cmd('internals')}` names them. You do not need "
        f"them to use",
        f"{naming.PRODUCT}. See LIMITATIONS.md.",
    ]
    return "\n".join(lines) + "\n"


PRODUCT_EPILOG = _product_epilog()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog=naming.CLI,
        description="An independent source of truth for agent constraints.",
        epilog=PRODUCT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    #: A developer's first reflex on an unfamiliar CLI, and this one answered
    #: it with a usage error and exit 2 — on a product whose whole claim is
    #: that a version resolves from bytes rather than from a label. Found by
    #: the developer trial of 2026-10-27. The string is the package's, read
    #: through `constraintrelay.__version__`, so it cannot disagree with what was
    #: installed; `v2/naming_gate.py` already binds that to `pyproject.toml`.
    from . import __version__ as _installed_version
    ap.add_argument("--version", action="version",
                    version=f"{naming.PRODUCT} {naming.CLI} "
                            f"{_installed_version}")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")

    # 0.2: the `container` group is registered FIRST so it is what `--help`
    # lists first. Dispatch does not depend on registration order — every
    # command carries a `func` default and is dispatched on that — so order is
    # purely about what a new user sees.
    cli_container.register(sub)
    # 1.0: reading the receipts back. Registered beside `container` because it
    # is the other half of the same loop — one writes them, this reads them.
    cli_receipts.register(sub)

    # Everything below is the v0.1 route, kept working and marked legacy. No
    # mass migration: these commands have evidence behind them and callers.
    _before = set(sub.choices)
    cli_phase1.register(sub)
    # Tag the v0.1 commands `[legacy]` in --help without editing the v0.1
    # module: argparse keeps the help strings on the parent's choice actions,
    # so they are relabelled here. The commands themselves are untouched and
    # keep working — 0.2 does not migrate them.
    _legacy_names = set(sub.choices) - _before
    for _act in getattr(sub, "_choices_actions", []):
        if _act.dest in _legacy_names and not _act.help.startswith("[legacy]"):
            _act.help = "[legacy] " + (_act.help or "")

    sub.add_parser("internals",
                   help="list the paused harness-qualification commands")

    rc = sub.add_parser("receipt")
    _add_common(rc)
    rc.add_argument("--development-evidence", action="store_true",
                    help="additionally record what the delivery computation would "
                         "have said if the adapter's qualification were complete. "
                         "This CANNOT produce a successful receipt (pre-merge "
                         "ruling item 1); it only adds a labelled "
                         "`development_evidence_only` field.")

    cp = sub.add_parser("capsule")
    _add_common(cp)
    cp.add_argument("--write", action="store_true",
                    help="write the managed mirror region into the store's "
                         "mirror_path. The only command here that edits the repo.")

    stp = sub.add_parser("store")
    stp.add_argument("action", choices=["show", "import"])
    stp.add_argument("path", nargs="?", default=".")
    stp.add_argument("--json", action="store_true")
    stp.add_argument("--profile", action="append", dest="profiles",
                     choices=_PROFILE_CHOICES, default=None)
    stp.add_argument("--mode", choices=list(store_mod.DELIVERY_MODES),
                     default=store_mod.NO_MIRROR,
                     help="delivery mode to record for the imported store (§3)")
    stp.add_argument("--mirror-path", default=None,
                     help="required by --mode managed_mirror")
    stp.add_argument("--generator", default=None,
                     help="required by --mode external_generator: the tool that "
                          "owns the output file ConstraintRelay must not patch")
    stp.add_argument("--source-layer", default=None,
                     help="where to integrate with that generator")
    stp.add_argument("--when", default=None,
                     help="timestamp recorded in each entry's provenance")
    stp.add_argument("--force", action="store_true",
                     help="replace an existing store")

    tg = sub.add_parser("targets")
    tg.add_argument("path", nargs="?", default=".")
    tg.add_argument("--json", action="store_true")
    tg.add_argument("--record", action="store_true",
                    help="write the baseline instead of comparing against it")
    tg.add_argument("--approve", action="append", default=None, metavar="TARGET",
                    help="repeatable; write a persisted approval for a blocking "
                         "change to this target. Requires --reason.")
    tg.add_argument("--reason", default=None,
                    help="why the blocking target change is intended; persisted "
                         "with the approval (§9)")
    tg.add_argument("--when", default=None)

    v = sub.add_parser("verify")
    _add_common(v)
    v.add_argument("--all-scenarios", action="store_true",
                   help="expand every scenario instead of folding the ones §5 "
                        "says to summarise")

    im = sub.add_parser("impact")
    _add_common(im)
    im.add_argument("--before-rev", default=None,
                    help="git revision to use as Before, exported read-only")
    im.add_argument("--before", default=None,
                    help="directory to use as Before")
    im.add_argument("--before-profile", action="append", dest="before_profiles",
                    choices=_PROFILE_CHOICES, default=None,
                    help="profile for the Before side only — this is how "
                         "'repository unchanged, harness/profile changed' is "
                         "expressed")
    im.add_argument("--after-profile", action="append", dest="after_profiles",
                    choices=_PROFILE_CHOICES, default=None,
                    help="profile for the After side only")
    im.add_argument("--approve-removal", action="append", default=None,
                    metavar="ID",
                    help="repeatable; approve an intentional CRITICAL_REMOVED "
                         "(§10.3). Without it, removal blocks.")

    b = sub.add_parser("budget")
    _add_common(b)
    b.add_argument("--id", action="append", dest="ids", default=None,
                   help="repeatable; measure only these constraint ids")

    f = sub.add_parser("fingerprint")
    f.add_argument("--profile", action="append", dest="profiles",
                   choices=_PROFILE_CHOICES, default=None)
    f.add_argument("--harness-root", default=None)

    sc = sub.add_parser("scan")
    sc.add_argument("path")
    sc.add_argument("--profile", action="append", dest="profiles",
                    choices=_PROFILE_CHOICES, default=None)
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--fail-on", choices=["none", "loss", "critical"], default="none")
    sc.add_argument("--critical", action="append", default=[],
                    help="regex marking a block critical; repeatable")
    sc.add_argument("--allow-on-request", action="store_true")
    sc.add_argument("--conway-active", default=None)

    sub.add_parser("profiles")

    args = ap.parse_args(argv)

    # 0.2: running a legacy command says so, once, on stderr, and points at
    # the container subcommand that replaces it. stderr so it cannot corrupt
    # machine-readable stdout.
    _LEGACY_POINTER = {
        "init": "container build",
        "active": "container preview",
        "import": "container build",
        "confirm": "container attest",
        "scope": "",
        "receipt": "container verify",
        "capsule": "container preview",
        "verify": "container check",
    }
    # Printed ONLY when stderr is a terminal. A user script that merges the
    # two streams and parses the result as JSON would be broken by a
    # diagnostic landing in its data — exactly what happened to two of this
    # repo's own suites. That is a compatibility problem, not their bug, so
    # the note is suppressed whenever stderr is not a tty: pipes, files,
    # captured buffers and CI all stay byte-identical to 0.1.
    if args.command in _LEGACY_POINTER and sys.stderr.isatty():
        _to = _LEGACY_POINTER[args.command]
        _where = (f"The 0.2 container route is `{naming.cmd(_to)}`."
                  if _to else
                  "There is no container equivalent: this is a v0.1 concept.")
        print(f"note: `{args.command}` is the v0.1 route (legacy). {_where}",
              file=sys.stderr)

    if getattr(args, "func", None) is not None:
        # `ValueError` is included because discovery raises one for an argument
        # that cannot mean what it says (`--skills-dir ../elsewhere`). Either
        # reading of a `ValueError` here — bad argument, or a defect — is exit 1
        # in this namespace, so catching it cannot turn a finding into a
        # tool error or the reverse.
        try:
            return args.func(args)
        except (skillstore_mod.StoreError, store_mod.StoreError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if args.command == "internals":
        print("Paused harness-qualification commands. They work, their evidence "
              "is preserved, and the\nv0.1 product does not need them:\n")
        for name, what in (
                ("receipt", "can initial-load delivery of a capsule be established?"),
                ("capsule", "show the capsule; verify or write a managed mirror"),
                ("store", "the v0.7 constraint store (store.toml, not skills.toml)"),
                ("targets", "target configuration as a reviewed input"),
                ("verify", "evaluate marked critical constraints in a revision"),
                ("impact", "cross-revision change impact (paused surface)"),
                ("budget", "per-profile, per-scenario delivery margin"),
                ("fingerprint", "loader-surface qualification state"),
                ("scan", "v0.5 block-level delivery verdicts"),
                ("profiles", "list registered harness profiles")):
            print(f"  {name:<12} {what}")
        print(f"\n`{naming.CLI} <command> --help` still works for each. "
              "See LIMITATIONS.md\nfor what is paused and why.")
        return EXIT_OK

    if args.command == "profiles":
        for key, reg in sorted(REGISTRY.items()):
            profile = reg.build()
            flag = " [replay-pending, not shippable]" if reg.replay_pending else ""
            if reg.needs_declaration:
                flag += f" [needs {reg.needs_declaration}]"
            print(f"{reg.profile_id:<44} {key:<20} consumes {reg.consumes:<8} "
                  f"unit {reg.native_unit:<20} verified against "
                  f"{profile.verified_against}{flag}")
        return EXIT_OK

    try:
        if args.command == "receipt":
            return cmd_receipt(args)
        if args.command == "capsule":
            return cmd_capsule(args)
        if args.command == "store":
            return cmd_store(args)
        if args.command == "targets":
            return cmd_targets(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "impact":
            return cmd_impact(args)
        if args.command == "budget":
            return cmd_budget(args)
        if args.command == "fingerprint":
            return cmd_fingerprint(args)
        if args.command == "scan":
            return cmd_scan(args)
    except KeyError as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
