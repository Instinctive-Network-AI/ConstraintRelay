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

What is deliberately NOT renamed here
-------------------------------------
Owner ruling 2026-10-26 settled the separate question: the **import name is
`cf`** for this preview and the public entry point is **`constraintrelay.attach`**. The
product name is no longer a pending item. So the spellings below keep their
current form by decision rather than by deferral — renaming them would
invalidate receipts already written and state already on disk:

* NOTHING in this list is an importable package any more. `cf` and
  `constraintflow` were both renamed into `constraintrelay` on 2026-11-02
  because they collided on install, and the Owner ruled out a compatibility
  alias. What remains below is state, payload bytes and field names — none of
  it is installed, so none of it can collide, which is where the rename
  stopped;
* `constraintrelay.version.IMPLEMENTATION` (`constraintflow/0.7+analyzer.2`), which is
  recorded inside every receipt and every frozen artifact as the identity of
  the analyzer that produced them;
* `.constraintflow/` — the on-disk state directory holding `receipts.jsonl`,
  and the `.constraintflow-attestation.json` / `.constraintflow-selectors`
  files beside it. Renaming these makes existing checkouts' state invisible;
* `constraintflow-declarations`, `constraintflow-constraints` and
  `constraintflow-active` — markers that travel **inside the model request**
  and are checked byte-for-byte by `cf/container_receipt.py`;
* `_constraintflow_guard`, `_constraintflow_patch`,
  `_constraintflow_original` — private attributes set on patched objects.

Each of those is either evidence already written down, state already on disk,
or bytes a receipt compares. None is a developer-facing name.
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
