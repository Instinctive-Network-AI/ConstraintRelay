#!/usr/bin/env python3
"""The 0.2 legacy pointer is a terminal diagnostic, never output. Zero cost.

    python3.13 tests/test_cli_legacy_pointer.py

Owner direction 2026-10-17, item 1. The 0.2 note broke two of this
repository's own suites, which merged stdout and stderr and parsed the result
as JSON. A user script would break the same way, so that is a **compatibility
problem rather than their bug**: `cf/cli.py` prints the pointer only when
`sys.stderr.isatty()`, and on a pipe the streams stay byte-identical to 0.1.

Why this is its own suite
-------------------------
It spawns child processes, and `tests/test_container_no_egress.py` re-runs the
container gate suite with **subprocess creation refused** — a child gets a
fresh interpreter with none of the blockers installed, so spawning one is
refused rather than trusted. Putting these checks in the container suite made
three egress gates fail for a reason that had nothing to do with egress. They
are CLI-contract checks and they live with their own kind.

Two traps this suite fell into, both worth keeping written down
---------------------------------------------------------------
1. The pipe half first probed `init --help`, and argparse exits on `--help`
   **before** the pointer is reached — so the note was absent because the code
   never ran, and the check passed for the wrong reason. It now runs a command
   that parses and dispatches.
2. The pty half read **zero bytes** and blamed the feature. On macOS, closing
   the slave fd only after the child has exited loses the buffered output: the
   slave must be closed in the parent *before* reading, with `Popen` rather
   than `run`.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from constraintrelay import naming                                          # noqa: E402

PASSES: list = []
FAILS: list = []

NOTE = "is the v0.1 route (legacy)"


def check_(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail and not ok else ""))
    (PASSES if ok else FAILS).append(name)
    return ok


def read_pty(argv: list, env: dict) -> bytes:
    """Run *argv* with stderr on a pty and return what it wrote there."""
    m, s = pty.openpty()
    out = b""
    try:
        proc = subprocess.Popen(argv, cwd=str(ROOT), env=env,
                                stdout=subprocess.DEVNULL, stderr=s)
        os.close(s)
        while True:
            try:
                chunk = os.read(m, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        proc.wait(timeout=60)
    finally:
        os.close(m)
    return out


env = dict(os.environ, PYTHONPATH=str(ROOT))
base = tempfile.mkdtemp(prefix="cf-legacy-")
try:
    target = Path(base) / "probe"
    target.mkdir()
    # A command that PARSES and dispatches: `--help` would exit first.
    argv = [sys.executable, "-m", "constraintrelay.cli", "init", str(target)]

    piped = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True,
                           text=True)
    check_("the command actually reached the dispatch path, so the absence "
           "below cannot be 'argparse exited first'",
           "error: no skills found" in piped.stderr
           or "skills" in (piped.stdout + piped.stderr),
           f"rc={piped.returncode} err={piped.stderr[:160]}")
    check_("the legacy note is ABSENT when stderr is a pipe, so a caller "
           "merging the streams and parsing JSON is unaffected",
           NOTE not in piped.stderr, piped.stderr[:200])
    check_("and absent from stdout too — moving a diagnostic to the data "
           "stream would be the same bug in the other direction",
           NOTE not in piped.stdout, piped.stdout[:200])

    tty_err = read_pty(argv, env).decode("utf-8", "replace")
    check_("and it IS printed when stderr is a terminal: the pointer still "
           "reaches a human, so the tty condition is not a dead switch",
           NOTE in tty_err, tty_err[:200])
    # Derived from `constraintrelay.naming`, not typed: the pointer is BUILT from the
    # same constant, so a rename that moved one and not the other would be
    # invisible to an assertion carrying its own copy of the old name.
    check_("the printed pointer names the 0.2 route to use instead",
           naming.cmd("container") in tty_err, tty_err[:200])

    nocmd = subprocess.run([sys.executable, "-m", "constraintrelay.cli", "container",
                            "--help"], cwd=str(ROOT), env=env,
                           capture_output=True, text=True)
    check_("a 0.2 container command prints no legacy pointer at all, on "
           "either stream",
           NOTE not in nocmd.stdout and NOTE not in nocmd.stderr)
    tty_new = read_pty([sys.executable, "-m", "constraintrelay.cli", "container", "check",
                        "--source", str(target), "--skill", "skills/none",
                        "--library", str(target / "nope.json")],
                       env).decode("utf-8", "replace")
    check_("not even on a terminal — the pointer is for v0.1 routes only, so "
           "a tty is not what makes it print",
           NOTE not in tty_new, tty_new[:200])

    top = subprocess.run([sys.executable, "-m", "constraintrelay.cli", "--help"],
                         cwd=str(ROOT), env=env, capture_output=True,
                         text=True)
    check_("the help text a pipe DOES see puts the container group first and "
           "marks the v0.1 commands legacy",
           "container" in top.stdout and "[legacy]" in top.stdout
           and top.stdout.index("container") < top.stdout.index("[legacy]")
           and "[0.2]" in top.stdout, top.stdout[:200])

finally:
    import shutil
    shutil.rmtree(base, ignore_errors=True)

print()
total = len(PASSES) + len(FAILS)
if FAILS:
    print(f"FAILED {len(FAILS)}: " + "; ".join(FAILS))
print(f"=== {len(PASSES)}/{total} legacy-pointer gates pass ===")
if FAILS:
    sys.exit(1)
