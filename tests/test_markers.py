"""Brief v0.6 §3 — critical-constraint identity and extent.

Every case here is a sentence of §3 turned into an assertion. The cases that
matter most are the ones where a mistake would be silent: a marker glued into
the paragraph it precedes, a duplicate id, and an unclosed range resolving to a
single-block constraint instead of an error.

    python3.13 tests/test_markers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from constraintrelay.markers import scan_project, scan_file  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# --- §3 single-block form ---------------------------------------------------

SINGLE = """\
# Team instructions

<!-- constraintrelay: critical id=FIN-003 -->
Research outputs must not be used directly as live-production actions.

Some other paragraph.
"""

cs, issues = scan_file("AGENTS.md", SINGLE)
check("single-block: one constraint, no issues", len(cs) == 1 and not issues,
      f"{len(cs)} constraints, {len(issues)} issues")
if cs:
    c = cs[0]
    check("single-block: id carried", c.id == "FIN-003", c.id)
    check("single-block: form", c.form == "single_block", c.form)
    check("single-block: covers exactly the next block",
          c.canonical_text == "Research outputs must not be used directly as "
                              "live-production actions.",
          repr(c.canonical_text))
    check("single-block: marker is not part of the text",
          "constraintrelay" not in c.canonical_text)
    check("single-block: cites the authored line", c.line == 4, str(c.line))
    check("single-block: tracked", c.tracked)
    check("single-block: does not swallow the following paragraph",
          "other paragraph" not in c.canonical_text)

# The marker sits directly above the prose with no blank line — §3's own
# example layout. Without masking, segmentation glues them into one paragraph.
GLUED = ("<!-- constraintrelay: critical id=A -->\n"
         "Line one of the clause.\n"
         "Line two of the same clause.\n")
cs, issues = scan_file("A.md", GLUED)
check("glued marker: no issues", not issues, "; ".join(str(i) for i in issues))
check("glued marker: marker excluded from canonical text",
      bool(cs) and "constraintrelay" not in cs[0].canonical_text)
check("glued marker: both prose lines covered",
      bool(cs) and cs[0].canonical_text.count("\n") == 1,
      repr(cs[0].canonical_text) if cs else "")


# --- §3 multi-block form ----------------------------------------------------

RANGE = """\
<!-- constraintrelay: critical id=FIN-003 -->

First paragraph.

Second paragraph.

- Additional requirement.

<!-- constraintrelay: end id=FIN-003 -->

Outside the range.
"""
cs, issues = scan_file("AGENTS.md", RANGE)
check("range: one constraint, no issues", len(cs) == 1 and not issues,
      f"{len(cs)} / {len(issues)}")
if cs:
    c = cs[0]
    check("range: form", c.form == "range", c.form)
    check("range: covers three blocks", len(c.blocks) == 3, str(len(c.blocks)))
    check("range: stops at the end marker",
          "Outside the range" not in c.canonical_text)
    check("range: keeps the list item",
          "- Additional requirement." in c.canonical_text)


# --- §3 configuration errors ------------------------------------------------

cases = [
    ("unmatched end", "UNMATCHED_END",
     "Prose.\n\n<!-- constraintrelay: end id=X -->\n"),
    ("id mismatch", "ID_MISMATCH",
     "<!-- constraintrelay: critical id=X -->\n\nProse.\n\n"
     "<!-- constraintrelay: end id=Y -->\n"),
    ("nested range", "NESTED_RANGE",
     "<!-- constraintrelay: critical id=X -->\n\nA.\n\n"
     "<!-- constraintrelay: critical id=Y -->\n\nB.\n\n"
     "<!-- constraintrelay: end id=Y -->\n\n"
     "<!-- constraintrelay: end id=X -->\n"),
    ("marker with nothing after it", "NO_COVERED_BLOCK",
     "Prose.\n\n<!-- constraintrelay: critical id=X -->\n"),
]
for name, code, src in cases:
    # `end_ids` has to be project-wide for the range forms to be recognised.
    scan = scan_project([("f.md", src)])
    got = {i.code for i in scan.issues}
    check(f"error: {name} -> {code}", code in got, f"got {sorted(got) or 'none'}")

# An unclosed explicit range must not quietly become a single-block constraint.
UNCLOSED_A = "<!-- constraintrelay: critical id=X -->\n\nFirst.\n\nSecond.\n"
STRAY_B = "Other file.\n\n<!-- constraintrelay: end id=X -->\n"
scan = scan_project([("a.md", UNCLOSED_A), ("b.md", STRAY_B)])
codes = {i.code for i in scan.issues}
check("unclosed range reported from both ends",
      {"UNCLOSED_RANGE", "UNMATCHED_END"} <= codes, f"got {sorted(codes)}")
check("unclosed range yields no constraint", not scan.constraints,
      f"{len(scan.constraints)} constraints")

# Duplicate ids, §3.
DUP_A = "<!-- constraintrelay: critical id=FIN-003 -->\nA clause.\n"
DUP_B = "<!-- constraintrelay: critical id=FIN-003 -->\nAnother clause.\n"
scan = scan_project([("a.md", DUP_A), ("b.md", DUP_B)])
check("duplicate id is a configuration error",
      any(i.code == "DUPLICATE_ID" for i in scan.issues),
      "; ".join(str(i) for i in scan.issues))
check("duplicate id reported against the second site, naming the first",
      any(i.file == "b.md" and "a.md" in i.message for i in scan.issues))


# --- §3 legacy markers are UNTRACKED ---------------------------------------

LEGACY = "<!-- constraintrelay: critical -->\nA clause with no id.\n"
scan = scan_project([("a.md", LEGACY)])
check("legacy marker is accepted", len(scan.constraints) == 1 and scan.ok,
      f"{len(scan.constraints)} / {[str(i) for i in scan.issues]}")
check("legacy marker is untracked",
      bool(scan.constraints) and not scan.constraints[0].tracked)
check("legacy marker form", bool(scan.constraints) and
      scan.constraints[0].form == "legacy")
check("legacy markers never collide as duplicates",
      scan_project([("a.md", LEGACY), ("b.md", LEGACY)]).ok)


# --- §3 identity is the id, never the text ---------------------------------

V1 = "<!-- constraintrelay: critical id=FIN-003 -->\nNo live-production use.\n"
V2 = "<!-- constraintrelay: critical id=FIN-003 -->\nNo live-production use, ever.\n"
V3 = "<!-- constraintrelay: critical id=FIN-004 -->\nNo live-production use.\n"
a = scan_project([("f.md", V1)]).tracked["FIN-003"]
b = scan_project([("f.md", V2)]).tracked["FIN-003"]
c = scan_project([("f.md", V3)]).tracked["FIN-004"]
check("same id, edited text -> same constraint, different canonical hash",
      a.id == b.id and a.canonical_sha256 != b.canonical_sha256)
check("identical text under a new id is a different constraint",
      a.canonical_sha256 == c.canonical_sha256 and a.id != c.id)

# Canonical text must be insensitive to blank-line reflow and trailing space,
# and sensitive to everything else.
R1 = ("<!-- constraintrelay: critical id=X -->\n\nA.\n\nB.\n\n"
      "<!-- constraintrelay: end id=X -->\n")
R2 = ("<!-- constraintrelay: critical id=X -->\n\n\nA.   \n\n\n\nB.\n\n"
      "<!-- constraintrelay: end id=X -->\n")
check("canonical text ignores blank-line reflow and trailing space",
      scan_project([("f.md", R1)]).tracked["X"].canonical_sha256
      == scan_project([("f.md", R2)]).tracked["X"].canonical_sha256)


# --- marker recognition is strict ------------------------------------------

NOT_A_MARKER = ("Text <!-- constraintrelay: critical id=X --> more text.\n\n"
                "A clause.\n")
scan = scan_project([("f.md", NOT_A_MARKER)])
check("an inline marker is not recognised as a marker",
      not scan.constraints and scan.ok,
      f"{len(scan.constraints)} constraints")

QUOTED = '<!-- constraintrelay: critical id="FIN-003" -->\nA clause.\n'
check("quoted id parses", "FIN-003" in scan_project([("f.md", QUOTED)]).tracked)

SPACED = "<!--  constraintrelay : critical  id = FIN-003  -->\nA clause.\n"
check("whitespace-tolerant marker parses",
      "FIN-003" in scan_project([("f.md", SPACED)]).tracked)


print()
if FAILS:
    print(f"FAILED {len(FAILS)}: " + ", ".join(FAILS))
    sys.exit(1)
print("markers: all checks pass")
