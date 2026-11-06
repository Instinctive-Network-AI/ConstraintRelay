"""The product's names, in one place, so a rename is one edit.

Owner ruling 2026-11-01: the product is **ConstraintRelay**, superseding
the 2026-10-25 name ConstraintRelay. The ruling also says
to handle the product name, the distribution name, the CLI name and the
repository name *together* and to check the **import name** separately, so
that the rename happens once instead of in rounds.

Why a module and not a find-and-replace
---------------------------------------
Before this file, the CLI's own name was a literal typed into 38 places across
13 modules — every `next: <old-cli> skills <repo>` hint, every help
listing, every error that suggests a command. A rename done by replacing those
literals is a rename that is *almost* done: the one copy the sweep missed goes
on printing a command the user cannot run, and nothing fails. This project has
already lost that exact bet twice — a gate scoped to `README.md` while the bad
line also sat in `CONTAINER_QUICKSTART.md`, and a count fixed in one document
while review found the live one in another.

So the invocation strings are **derived** from `CLI` below, and
`v2/naming_gate.py` asserts that this file and `pyproject.toml` agree. A
future rename edits one constant and the gate tells you whether the packaging
followed.

The format names, renamed 2026-11-05
------------------------------------
There used to be a list here headed *what is deliberately NOT renamed*, and
the reasoning was that these spellings are state on disk and bytes a receipt
compares, so moving them would invalidate what already exists. That reasoning
had the timing backwards, and the Owner's ruling of 2026-11-05 item 3 says so:
these are the names an **author** writes into their own skill document and a
**user** writes into their own project. Once published they are a format.
Changing them *after* publication is a breaking change for somebody else's
files; changing them *before* costs nothing, because there are no external
users yet.

So they are renamed, and the inventory lives in `v2/naming_gate.py` —
`FORMAT_FAMILIES`, eleven families with both spellings — rather than in this
docstring. The gate derives its coverage set from the committed package at the
pre-rename commit, so a family nobody wrote down fails there instead of
surviving in someone's file. A list in prose here would be a second copy that
nothing checks.

What the rename did NOT touch, and why:

* **Existing `.constraintflow/` state and `receipts.jsonl` are not migrated.**
  The ruling is explicit. A checkout that already has state keeps it; nothing
  reads the old directory any more, and nothing deletes it either. Silently
  rewriting a user's receipt file is worse than leaving it where they put it.
* **Historical artifacts keep the old analyzer identity.** `IMPLEMENTATION` is
  now `constraintrelay/0.7+analyzer.3`, and everything already written stays at
  `constraintflow/0.7+analyzer.2` — see `version.py`. That is a version change,
  not a re-signing of past output.
* **`ConstraintFlow`, the query class**, is unchanged. It is a Python
  identifier a developer types, not a format written into anyone's file, and
  it is not in the ruling's list. The naming gate exempts it by exact match
  and asserts all of its uses are still there, so "exempt" cannot drift into
  "deleted".
* **Two repository paths** that contain the old word —
  `docs/constraintflow/inbound/` and `constraintflow/profiles/` — are
  directories on disk, not formats. The rename guarded them explicitly.
"""

from __future__ import annotations

#: The product, as prose names it.
PRODUCT = "ConstraintRelay"

#: The distribution name — what a package index would call it. Must equal
#: `[project] name` in `pyproject.toml`; `v2/naming_gate.py` asserts it.
#:
#: Not published. The previous name `constraintflow` was already taken on PyPI
#: by an unrelated project (a DSL for neural-network certification), which is
#: how `pip install constraintflow` came to be documented for two rounds while
#: installing somebody else's package.
DISTRIBUTION = "constraintrelay"

#: The console script. Must equal the `[project.scripts]` key; same gate.
CLI = "constraintrelay"


def cmd(*args: str) -> str:
    """One CLI invocation, spelled the way this build actually installs it.

    `cmd("skills", repo)` rather than `f"{CLI} skills {repo}"` at 38 call
    sites, so the program name cannot drift away from `CLI` in any one of
    them.
    """
    return " ".join((CLI, *(str(a) for a in args)))
