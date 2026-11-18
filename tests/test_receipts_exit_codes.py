#!/usr/bin/env python3
"""`constraintrelay receipts` exit codes — the contract README publishes.

0 nothing needs a decision · 1 the ledger was not read · 2 a reported finding.

Written on 2026-11-18 after review found the contract **fail-open**: a sink
holding a truncated line, or a row whose status this version does not know,
exited 0 under a documented promise that 0 means nothing needs a decision. Both
figures were already computed and printed, and neither reached the exit code —
so a corrupted ledger went green in CI.

Every case below asserts the code AND a visible reason, over both output paths,
because a 1 with nothing on screen explaining it is a different defect.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

CLI = [sys.executable, "-m", "constraintrelay.cli"]
EXIT_OK, EXIT_ERROR, EXIT_FINDING = 0, 1, 2

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok and detail:
        print(f"         {detail}")


def row(status: str, seq: int = 1, skill: str = "skills/s") -> str:
    """A well-formed ledger row. `status` lives under `ledger_row`."""
    return json.dumps({
        "request_seq": seq, "host": "openai-python",
        "ledger_row": {"status": status, "skill_ids": [skill],
                       "why": "fixture", "detail": ""},
    })


def run(lines: list[str], *extra: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "receipts.jsonl"
        p.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")
        r = subprocess.run([*CLI, "receipts", "--path", str(p), *extra],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


def main() -> int:
    print("=== receipts exit-code contract ===")

    # -- the two controls come FIRST. Without them every refusal below is
    # -- also satisfied by a command that returns non-zero unconditionally.
    code, out = run([row("DELIVERED_COMPLETE")])
    check(code == EXIT_OK,
          "CONTROL: a clean, fully-delivered ledger exits 0 — without this, a "
          "command that always failed would satisfy every case below",
          f"exit {code}\n{out}")

    code, out = run([row("DUE_BUT_MISSING")])
    check(code == EXIT_FINDING,
          "CONTROL: a ledger holding a MISSING row exits 2, and 2 still means "
          "a reported finding rather than an error",
          f"exit {code}")

    code, out = run([row("ATTRIBUTION_UNVERIFIED")])
    check(code == EXIT_FINDING,
          "...and UNATTRIBUTED is a finding too, so the finding set is not "
          "just DUE_BUT_MISSING",
          f"exit {code}")

    code, out = run([row("NO_DECLARATION_DUE")])
    check(code == EXIT_OK,
          "...and `none due` is NOT a finding — reporting an ordinary request "
          "with nothing active would train a developer to ignore the code",
          f"exit {code}")

    # -- the defect: unreadable input must not read as clean -----------------
    code, out = run([row("DELIVERED_COMPLETE"), "{truncated"])
    check(code == EXIT_ERROR,
          "a ledger with a line that will not parse exits 1, NOT 0 — a "
          "truncated sink is the case this contract was failing open on",
          f"exit {code}\n{out}")
    check("would not parse" in out,
          "...and says so on the text surface, so a 1 arrives with its reason",
          out)

    code, out = run([row("TOTALLY_MADE_UP")])
    check(code == EXIT_ERROR,
          "a row whose status this version does not know exits 1: an "
          "unrecognised state is unestablished, not clean",
          f"exit {code}\n{out}")
    check("no status this version knows" in out,
          "...and that count is printed too, not only in --json",
          out)

    code, out = run([json.dumps({"request_seq": 1, "host": "openai-python"})])
    check(code == EXIT_ERROR,
          "a row with NO `ledger_row` at all exits 1 — shape drift in the "
          "sink is the same class as an unknown status, not a pass",
          f"exit {code}\n{out}")

    # -- shape drift BELOW the object level ---------------------------------
    # `json.loads` returns whatever the line held. `null`, `42`, `[1,2]` and
    # `"text"` are all valid JSON and none of them is a row. Round 1 of this
    # fix handled "an object whose status I cannot read" and these still
    # raised `AttributeError` — fail-closed by accident, since Python's
    # uncaught-exception exit is also 1, which is why the gap survived a
    # round. The exit code was right and the output was a traceback.
    for literal, name in (("null", "null"), ("42", "a bare number"),
                          ("[1, 2]", "a list"), ('"text"', "a bare string")):
        code, out = run([literal])
        check(code == EXIT_ERROR,
              f"a line holding {name} — valid JSON, not a row — exits 1",
              f"exit {code}\n{out[:400]}")
        check("Traceback" not in out,
              f"...and does NOT traceback on {name}: a command that crashes "
              f"is not a command that reported",
              out[:500])

    code, out = run([json.dumps({"request_seq": 1, "ledger_row": "oops"})])
    check(code == EXIT_ERROR and "Traceback" not in out,
          "a `ledger_row` that is not an object is unreadable, not a crash — "
          "drift one level down is still drift",
          f"exit {code}\n{out[:400]}")

    # -- `--json` must stay machine-readable on EVERY one of them -----------
    # The whole point of `--json` is a caller that parses it. A traceback
    # there is worse than a wrong number: the caller cannot even read the
    # refusal. Asserted by actually parsing, not by looking for "Traceback".
    for lines, name in (
            (["null"], "null"),
            (["[1, 2]"], "a list"),
            ([json.dumps({"request_seq": 1, "ledger_row": "oops"})],
             "a non-object ledger_row"),
            ([row("DELIVERED_COMPLETE"), "null", "{truncated"],
             "a mixed ledger: one good row, one non-row, one unparseable"),
    ):
        code, out = run(lines, "--json")
        try:
            payload = json.loads(out)
            parsed, why = isinstance(payload.get("summary"), dict), ""
        except Exception as exc:                               # noqa: BLE001
            parsed, why = False, f"{exc}"
        check(parsed and code == EXIT_ERROR,
              f"--json over {name} is still valid JSON carrying a summary, "
              f"and still exits 1",
              f"exit {code} parse={why or 'ok'}\n{out[:400]}")

    # `--failures` walks the rows through a different filter than the
    # listing does, so it is its own path and gets its own case.
    code, out = run([row("DUE_BUT_MISSING"), "null"], "--failures")
    check(code == EXIT_ERROR and "Traceback" not in out,
          "`--failures` filters rows through its own predicate and survives a "
          "non-row too — a second reader of the same drift",
          f"exit {code}\n{out[:400]}")

    # -- precedence, stated rather than left to reading order ---------------
    code, out = run([row("DUE_BUT_MISSING"), "{truncated"])
    check(code == EXIT_ERROR,
          "unreadable input OUTRANKS a finding: with rows unaccounted for the "
          "finding count is a floor, and exiting 2 would quote that floor as "
          "the answer",
          f"exit {code}")

    # -- both output paths, because only one of them was ever read ----------
    for extra, name in (((), "text"), (("--json",), "--json")):
        code, out = run([row("DELIVERED_COMPLETE"), "{truncated"], *extra)
        check(code == EXIT_ERROR,
              f"the {name} path returns the same code — the exit code is not "
              f"a property of the formatter",
              f"exit {code}")

    code, out = run([row("TOTALLY_MADE_UP")], "--json")
    try:
        payload = json.loads(out)
        bad = payload["summary"]["rows_without_a_known_status"]
    except Exception as exc:                                   # noqa: BLE001
        payload, bad = None, f"unreadable: {exc}"
    check(bad == 1,
          "...and --json still reports the count that decided it, so a caller "
          "can act on the cause rather than just the code",
          f"got {bad!r}")

    # -- a missing file is an error, and distinct from an empty one ----------
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([*CLI, "receipts", "--path",
                            str(Path(td) / "nope.jsonl")],
                           capture_output=True, text=True)
    check(r.returncode == EXIT_ERROR,
          "an absent sink is 1: an absent file is not evidence that no "
          "request was made",
          f"exit {r.returncode}")

    code, out = run([])
    check(code == EXIT_OK,
          "...while a present but EMPTY sink is 0 — a guard that observed "
          "nothing is not a guard that failed, and the two must not share a "
          "representation",
          f"exit {code}")

    bad = [lbl for ok, lbl, _ in RESULTS if not ok]
    print(f"\n=== receipts exit codes: {len(RESULTS) - len(bad)}/{len(RESULTS)}"
          + (f" ({len(bad)} failed)" if bad else "") + " ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
