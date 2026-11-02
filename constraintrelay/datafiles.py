"""Where the shipped profile data lives. **One definition, not five.**

The data used to sit in `profiles/data/`, a **sibling** of `cf/`, and four
modules each resolved it with their own
`Path(__file__).resolve().parents[1] / "profiles" / "data" / …`. That works in
a checkout and is wrong in a wheel: `packages = ["cf"]` ships only `cf/`, so
in an installed package `parents[1]` is `site-packages` and every one of those
paths points at a directory that does not exist.

The release review caught it on a real install. Two shipped commands were
affected, and the second is the worse one:

* `constraintrelay profiles` raised `FileNotFoundError` through an unhandled
  traceback, part-way through printing its output;
* `constraintrelay fingerprint` exited **rc=0** and reported every profile
  `UNRECORDED` — "no loader fingerprint has been recorded for this profile".
  That is a confident wrong answer, not an error: packaging turned *the file
  did not ship* into *nobody ever recorded it*.

So the data now lives **inside the package**, at `cf/data/`, declared in
`pyproject.toml` as `package-data`, and every reader — including the
generators under `tests/` that write these files — resolves it through
`DATA_DIR` here. A path spelled out in five places is five places to get it
wrong; this is the one.
"""

from __future__ import annotations

from pathlib import Path

#: The shipped data directory. Inside the package, so it travels with a wheel.
DATA_DIR = Path(__file__).resolve().parent / "data"


class MissingDataFile(FileNotFoundError):
    """A shipped data file is not where the package says it is.

    Raised instead of returning an empty record. An absent file and an empty
    record are different facts, and a reader cannot tell them apart from a
    result that spells both `{}` — which is exactly how a packaging defect
    came to be reported as "nobody recorded it".
    """


def path(name: str) -> Path:
    """The path to a shipped data file. Existence is not checked here."""
    return DATA_DIR / name


def require(p: Path, *, what: str) -> Path:
    """*p*, or `MissingDataFile` naming what is missing and why it matters."""
    if not p.is_file():
        raise MissingDataFile(
            f"{p} is missing. This is shipped data ({what}); if you are "
            "running from an installed package, the distribution did not "
            "carry it, which is a packaging defect rather than an empty "
            "record. Absent is not the same as unrecorded.")
    return p
