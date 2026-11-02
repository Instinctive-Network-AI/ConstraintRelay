#!/usr/bin/env python3
"""CLI contract tests — criticality matching and the exit-code namespace.

Two invariants the Owner fixed on 2026-09-30.

**Criticality matches the canonical full source-block text**, never the
rendered label, `lost_excerpt`, or any truncated preview. Letting a display
rule decide CI would mean the renderer silently changes build outcomes. The
four fixtures put the same keyword at the first line, the last line, the middle
of a multi-line block, and past the label truncation point; all four must
classify identically.

**Exit codes are a fixed namespace** and `1` never means "a finding":

    0  completed; no configured enforcement failure
    1  tool / configuration / runtime error
    2  configured finding / enforcement failure
    3  indeterminate: a critical block came back UNKNOWN

Usage: python test_cli_contract.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from constraintrelay.blocks import Block                       # noqa: E402
from constraintrelay.cli import EXIT_ENFORCE, EXIT_ERROR, EXIT_OK, main  # noqa: E402
from constraintrelay.profiles import InputFile                 # noqa: E402
from constraintrelay.openclaw import OpenClawProfile           # noqa: E402
from constraintrelay.scan import scan_under_profile            # noqa: E402

KEYWORD = "not for direct live-trading signals"
PAD = "padding sentence that carries no policy vocabulary at all."


def filler(n: int, tag: str) -> str:
    unit = f"{tag} ordinary prose line with nothing notable in it.\n"
    return (unit * (n // len(unit) + 2))[:n]


def criticality_fixtures() -> dict[str, str]:
    """One paragraph block per fixture, keyword in a different position."""
    clause = f"Scope: quantitative research / factor mining ({KEYWORD})"
    long_prefix = "x" * (Block.LABEL_MAX + 40)   # pushes the keyword past the label cut
    return {
        "first_line": f"{clause}\n{PAD}\n{PAD}\n",
        "last_line": f"{PAD}\n{PAD}\n{clause}\n",
        "middle_line": f"{PAD}\n{clause}\n{PAD}\n",
        "past_label_cut": f"{long_prefix} {clause}\n{PAD}\n",
    }


def check_criticality() -> tuple[bool, list[dict]]:
    pattern = [re.compile(re.escape(KEYWORD), re.IGNORECASE)]
    rows = []
    for label, body in criticality_fixtures().items():
        text = f"# Team\n\n{body}\n"
        f = InputFile("SOUL.md", "/w/SOUL.md", text)
        result = scan_under_profile([f], OpenClawProfile(), pattern)
        blocks = result.files[0].blocks
        crit = [b for b in blocks if b.critical]
        # The keyword really is in exactly one block's source text.
        holders = [b for b in blocks if KEYWORD in text[b.start:b.end]]
        shown_in_label = any(KEYWORD in b.label for b in holders)
        rows.append({
            "fixture": label,
            "critical_blocks": len(crit),
            "keyword_blocks": len(holders),
            "keyword_visible_in_label": shown_in_label,
            "ok": len(crit) == 1 and len(holders) == 1 and crit[0] is holders[0],
        })
    # The point of the fourth fixture: the label cannot see the keyword, and the
    # classification is identical anyway.
    hidden = [r for r in rows if not r["keyword_visible_in_label"]]
    all_same = len({r["critical_blocks"] for r in rows}) == 1
    ok = all(r["ok"] for r in rows) and all_same and len(hidden) >= 1
    return ok, rows


def run_cli(args: list[str]) -> int:
    """Call the CLI in-process with its own output captured.

    The CLI writes its report to stdout; letting that through would interleave
    with this test's own JSON and make the result unparseable.
    """
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        return main(args)


def run_cli_text(args: list[str]) -> tuple[int, str]:
    """`run_cli`, but returning the captured output too.

    A sibling rather than a wider signature on `run_cli`: the exit-code cases below
    only ever want the code, and widening it would make every one of them unpack a
    value it discards.
    """
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = main(args)
    return code, buf.getvalue() + err.getvalue()


def check_version_flag() -> tuple[bool, dict]:
    """`--version` answers, and answers with the INSTALLED version.

    A developer trial on 2026-10-27 ran `constraintrelay --version` first, as
    developers do, and got a usage error and exit 2 — on a product whose
    claim is that a version resolves from bytes rather than from a label.

    The expected string is read from `constraintrelay.__version__` rather than typed here:
    a literal would be a second copy of the version, and this repository's
    own gates exist because copies of a figure drift. `argparse`'s `version`
    action exits with `SystemExit(0)`, which is the contract being asserted,
    so it is caught rather than allowed to end the suite.
    """
    from constraintrelay import __version__ as installed
    buf, err = io.StringIO(), io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            main(["--version"])
    except SystemExit as exc:
        code = exc.code
    out = (buf.getvalue() + err.getvalue()).strip()
    row = {"exit_code": code, "printed": out,
           "installed_version": installed,
           "ok": code == 0 and installed in out and bool(installed)}
    return row["ok"], row


def check_exit_codes() -> tuple[bool, list[dict]]:
    cases = []
    with tempfile.TemporaryDirectory(prefix="cf-cli-") as tmp:
        root = Path(tmp)
        # An AGENTS.md far over the OpenClaw cap, with a critical clause in the
        # dropped middle so it is genuinely NOT_DELIVERED.
        clause = f"Scope: quantitative research / factor mining ({KEYWORD})"
        (root / "AGENTS.md").write_text(
            f"# Team\n\n{filler(20_000, 'h')}\n{clause}\n{filler(20_000, 't')}\n",
            encoding="utf-8")

        base = ["scan", str(root), "--profile", "openclaw"]
        cases += [
            {"name": "clean report, no enforcement",
             "argv": base + ["--fail-on", "none", "--critical", KEYWORD],
             "want": EXIT_OK},
            {"name": "critical loss, fail-on critical",
             "argv": base + ["--fail-on", "critical", "--critical", KEYWORD],
             "want": EXIT_ENFORCE},
            {"name": "fail-on loss is 2, never 1",
             "argv": base + ["--fail-on", "loss"],
             "want": EXIT_ENFORCE},
            {"name": "no critical pattern, fail-on critical",
             "argv": base + ["--fail-on", "critical"],
             "want": EXIT_OK},
            {"name": "missing directory is a tool error",
             "argv": ["scan", str(root / "nope"), "--fail-on", "critical"],
             "want": EXIT_ERROR},
            {"name": "bad regex is a tool error",
             "argv": base + ["--critical", "([unclosed"],
             "want": EXIT_ERROR},
        ]
        for c in cases:
            c["got"] = run_cli(c["argv"])
            c["ok"] = c["got"] == c["want"]

        # A replay-pending profile is advisory: reported, never enforcing.
        adv = run_cli(["scan", str(root), "--profile", "codex-cli",
                       "--fail-on", "critical", "--critical", KEYWORD])
        cases.append({"name": "replay-pending profile cannot fail a build",
                      "argv": ["--profile codex-cli"], "want": EXIT_OK,
                      "got": adv, "ok": adv == EXIT_OK})

        # Default profile set excludes replay-pending profiles.
        out = subprocess.run(
            [sys.executable, "-m", "constraintrelay.cli", "scan", str(root), "--json"],
            cwd=ROOT, capture_output=True, text=True)
        payload = json.loads(out.stdout)
        names = [p["profile"] for p in payload["profiles"]]
        default_clean = not any("codex" in n for n in names)
        cases.append({"name": "default profile set excludes replay-pending",
                      "argv": ["(default)"], "want": True, "got": default_clean,
                      "ok": default_clean})

    return all(c["ok"] for c in cases), cases


def check_duplicate_discovery() -> tuple[bool, dict]:
    """CF-001 regression: one real file must be enumerated once.

    `CONTEXT_FILENAMES` lists both `AGENTS.md` and `agents.md`. On a
    case-insensitive filesystem those are the same file; enumerating it twice
    made the duplicate lose the priority contest and be reported
    NOT_DELIVERED — a false definite status for delivered content. Found by
    the field test on 2026-09-30.
    """
    from constraintrelay.cli import collect_context_files
    with tempfile.TemporaryDirectory(prefix="cf-dup-") as tmp:
        root = Path(tmp)
        (root / "AGENTS.md").write_text("# Team\n\n- one rule\n", encoding="utf-8")
        files = collect_context_files(root)
        resolved = {str(Path(f.path).resolve()) for f in files}
        case_insensitive = (root / "agents.md").is_file()
        ok = len(files) == len(resolved) == 1
        # On a case-sensitive filesystem the hazard cannot arise; say so rather
        # than reporting a pass the platform handed us for free.
        return ok, {"files_enumerated": len(files),
                    "distinct_real_paths": len(resolved),
                    "filesystem_is_case_insensitive": case_insensitive,
                    "exercised": case_insensitive, "ok": ok}


def check_single_scenario_verify():
    """CF-009: a profile's only scenario must not be folded out of the report.

    Reviewer finding F3 on commit 305b417: nothing in `tests/` referenced
    `expand_set`, and this file had no `verify` case at all. `M4` in
    `tests/test_receipt_falsification.py` now reverts the fix and requires the
    defect to be detected, but a mutation bank answers "is the guard still armed",
    not "what is the guarantee". This is the guarantee, stated directly: a healthy
    repository with exactly one scenario prints a per-constraint verdict line.

    The failure it guards against is not a crash. `verify` exited 0 and printed a
    footer saying "no difference from a neighbouring scenario" — of which there was
    none — with no verdict anywhere in the output. A report that says nothing about
    what it checked is worse than a loud failure, because it reads as a pass.
    """
    filler = "\n\n".join(f"Filler paragraph {i} in a healthy repository."
                          for i in range(8))
    with tempfile.TemporaryDirectory(prefix="cf-cf009-") as tmp:
        root = Path(tmp)
        (root / "AGENTS.md").write_text(
            "# Team\n\n<!-- constraintflow: critical id=FIN-001 -->\n"
            "Research outputs must not be used directly as live-production "
            "actions.\n\n" + filler + "\n", encoding="utf-8")
        (root / ".constraintflow.toml").write_text(
            'version = 1\n\n[[target]]\nname = "prod"\n'
            'profile = "hermes-agent/context-loader-v1"\ncwd = "."\n',
            encoding="utf-8")
        code, out = run_cli_text(["verify", str(root)])

    scenarios = out.count("### scenario ")
    row = {
        "exit_code": code,
        "scenarios_in_report": scenarios,
        "names_the_constraint": "FIN-001" in out,
        "prints_a_verdict": "DELIVERED_ON_LOAD" in out,
        "claims_a_folded_neighbour": "further scenario(s)" in out,
    }
    row["ok"] = (code == EXIT_OK and scenarios == 1
                 and row["names_the_constraint"] and row["prints_a_verdict"]
                 and not row["claims_a_folded_neighbour"])
    return row["ok"], row


def main_() -> int:
    dup_ok, dup_row = check_duplicate_discovery()
    crit_ok, crit_rows = check_criticality()
    exit_ok, exit_rows = check_exit_codes()
    fold_ok, fold_row = check_single_scenario_verify()
    ver_ok, ver_row = check_version_flag()
    report = {
        "criticality_matches_full_block_text": crit_ok,
        "criticality_fixtures": crit_rows,
        "exit_code_contract_holds": exit_ok,
        "exit_code_cases": [{k: v for k, v in c.items() if k != "argv"}
                            for c in exit_rows],
        "cf001_duplicate_discovery": dup_row,
        "cf009_single_scenario_verify": fold_row,
        "version_flag_answers": ver_row,
        "all_pass": crit_ok and exit_ok and dup_ok and fold_ok and ver_ok,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main_())
