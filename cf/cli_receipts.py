"""`constraintrelay receipts` — read back what was actually delivered.

Why this command exists
-----------------------
`cf.host_openai` appends a receipt plus its ledger row to
`.constraintflow/receipts.jsonl` on every observed request. Until this
command, nothing read them. A receipt nobody can look at is a compliance
artefact for an auditor who never arrives.

What it reports, and what it refuses to
---------------------------------------
Counts, per the four request-ledger states, and one line per request. **No
score.** A 0–100 "safety number" over these rows would be a judgement this
project has not earned — `DELIVERED_COMPLETE` means the declared bytes were
in the payload at a named boundary, and nothing here knows whether the model
read them.

The file is append-only and may hold rows from several runs and several
hosts, so every summary states the span it covered rather than implying it is
everything that ever happened.
"""

from __future__ import annotations

import json
from pathlib import Path

from .request_ledger import (ATTRIBUTION_UNVERIFIED, DELIVERED_COMPLETE,
                             DUE_BUT_MISSING, NO_DECLARATION_DUE, STATUSES)

DEFAULT_PATH = Path(".constraintflow") / "receipts.jsonl"

#: Exit codes match the rest of the CLI: 0 nothing needs a decision, 1 a tool
#: or configuration error, 2 a reported finding.
EXIT_OK, EXIT_ERROR, EXIT_FINDING = 0, 1, 2

#: Only these two states are a finding. `NO_DECLARATION_DUE` is a normal
#: request with nothing active, and reporting it as a problem would train a
#: developer to ignore the exit code.
FINDING_STATES = (DUE_BUT_MISSING, ATTRIBUTION_UNVERIFIED)

_SHORT = {
    DELIVERED_COMPLETE: "delivered",
    DUE_BUT_MISSING: "MISSING",
    ATTRIBUTION_UNVERIFIED: "UNATTRIBUTED",
    NO_DECLARATION_DUE: "none due",
}


def register(sub) -> None:
    p = sub.add_parser(
        "receipts",
        help="[1.0] read the per-request delivery receipts written by a guard",
        description=__doc__.split("\n\n")[0])
    p.add_argument("--path", default=str(DEFAULT_PATH),
                   help=f"receipt sink to read (default: {DEFAULT_PATH})")
    p.add_argument("--json", action="store_true",
                   help="machine-readable summary on stdout")
    p.add_argument("--failures", action="store_true",
                   help="list only the requests that are a finding")
    p.add_argument("--limit", type=int, default=20,
                   help="how many rows to print (0 for all); the SUMMARY "
                        "always covers every row in the file")
    p.set_defaults(func=cmd_receipts)


def read_rows(path: Path) -> tuple:
    """`(rows, malformed)` — a line that will not parse is counted, not dropped.

    Silently skipping a bad line would make a truncated sink read as a
    complete one, which is the same defect as a missing file and an empty
    record sharing a representation.
    """
    rows, malformed = [], 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                malformed += 1
    return rows, malformed


def summarise(rows: list, malformed: int) -> dict:
    census = {s: 0 for s in STATUSES}
    hosts, unreadable = set(), 0
    for r in rows:
        row = r.get("ledger_row") or {}
        st = row.get("status")
        if st in census:
            census[st] += 1
        else:
            unreadable += 1
        if r.get("host"):
            hosts.add(r["host"])
    return {
        "requests_recorded": len(rows),
        "census": census,
        "hosts": sorted(hosts),
        "malformed_lines": malformed,
        "rows_without_a_known_status": unreadable,
        "findings": sum(census[s] for s in FINDING_STATES),
        "_what_this_covers": (
            "the rows in this file, which is append-only and may span several "
            "runs and hosts. It is not a census of every request the process "
            "made: a request through an unobserved path produces no row, and "
            "no absence is claimed for one."),
        "_not_a_score": (
            "DELIVERED_COMPLETE means the declared bytes were in the payload "
            "at the named boundary. It is not evidence the model read, "
            "understood or obeyed them."),
    }


def cmd_receipts(args) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"error: no receipt sink at {path}. A guard writes one on its "
              f"first observed request; an absent file is not evidence that "
              f"no request was made.")
        return EXIT_ERROR

    rows, malformed = read_rows(path)
    summary = summarise(rows, malformed)

    shown = [r for r in rows
             if not args.failures
             or (r.get("ledger_row") or {}).get("status") in FINDING_STATES]
    if args.limit > 0:
        shown = shown[-args.limit:]

    if args.json:
        print(json.dumps({"summary": summary,
                          "rows": [_line(r) for r in shown]},
                         indent=2, ensure_ascii=False))
    else:
        print(f"{path}  —  {summary['requests_recorded']} request(s) recorded"
              + (f", host(s): {', '.join(summary['hosts'])}"
                 if summary["hosts"] else ""))
        if malformed:
            print(f"  ! {malformed} line(s) would not parse and are counted, "
                  f"not dropped")
        print()
        # Numbered by position in THIS file, with the guard's own sequence
        # beside it. `request_seq` restarts at 1 for every `attach`, so a
        # four-row sink printed `#1` four times — a developer trial on
        # 2026-10-26 read that as one request listed four times. The row
        # number is the sink's; `seq` is the session's, and they are different
        # facts rather than one number doing both jobs.
        first = summary["requests_recorded"] - len(shown) + 1
        for n, r in enumerate(shown, first):
            d = _line(r)
            print(f"  {n:>4}. {_SHORT.get(d['status'], d['status']):<13}"
                  f" {d['skills'] or '—'}"
                  f"   (seq {d['request_seq']})")
            if d["status"] in FINDING_STATES and d["why"]:
                print(f"        {d['why']}")
            # The actionable half. `detail` is where the adapter puts the
            # refusal CODE — NO_VERSION_MATCHES_HOST_BYTES tells a developer
            # to rebuild, NOT_IN_ANY_LIBRARY tells them the skill was never
            # declared — and this listing used to withhold it, leaving
            # "not in this payload" as the only thing a user ever saw.
            if d["status"] in FINDING_STATES and d["detail"]:
                print(f"        cause: {d['detail']}")
        if len(shown) < summary["requests_recorded"] and not args.failures:
            print(f"\n  … {summary['requests_recorded'] - len(shown)} earlier "
                  f"request(s) not shown; the counts below cover all of them")
        print("\n  " + "  ".join(
            f"{_SHORT[s]}: {summary['census'][s]}" for s in STATUSES))
        print("\n  Delivered means the declared bytes were in the payload at "
              "the named boundary.\n  It is not evidence the model read, "
              "understood or obeyed them.")

    return EXIT_FINDING if summary["findings"] else EXIT_OK


#: Fields this listing ADDS to the row. Everything else in `--json` is the
#: ledger row itself, carried whole.
#:
#: The previous version hand-listed ten keys and so DROPPED five the ledger
#: writes: `capsule_present`, `payload_sha256`, `delivered_ids`,
#: `withheld_attribution_ids`, `send_path`. The cost was found by a developer
#: trial on 2026-10-27, and it was the README's own instruction that broke:
#: README § *`tools=` is load-bearing* tells a developer the row records
#: `capsule_present: true`, and no surface reachable from this command emitted
#: that name. `row.get("capsule_present")` read `None`, which a reader takes
#: for *no capsule* — the exact inversion `request_ledger.classify` exists to
#: prevent. `UNATTRIBUTED` was in the census while the ids naming which skill
#: was unattributed were unreachable.
#:
#: This is the SECOND time this projection withheld the actionable half: the
#: 2026-10-26 trial found `detail` missing and the fix added one key to the
#: hand-list. Adding a key leaves the shape that loses the next field. The
#: row is now carried whole, and `test_receipts_listing_covers_the_row`
#: derives the required key set from `RequestLedger.record` rather than from a
#: list anyone can forget to extend.
_ADDED_BY_LISTING = ("skills", "host", "observation_point",
                     "observable_boundary")


def _line(r: dict) -> dict:
    row = r.get("ledger_row") or {}
    rec = r.get("receipt") or {}
    out = dict(row)
    #: Defaults only for the keys this listing's own output reads, so a
    #: malformed row prints rather than raising. Every other key is whatever
    #: the ledger wrote, present or absent as written.
    out.setdefault("request_seq", "?")
    out.setdefault("status", "UNKNOWN")
    out.setdefault("why", "")
    out.setdefault("detail", "")
    out.setdefault("receipt_ok", None)
    out.setdefault("at", "")
    #: `skills` is a RENDERING of `due_ids` for the human listing, and both
    #: are emitted: collapsing a list into a comma-joined string is a loss
    #: a machine reader should not have to undo.
    out["skills"] = ", ".join(row.get("due_ids") or [])
    out["host"] = r.get("host", "")
    out["observation_point"] = rec.get("observation_point", "")
    out["observable_boundary"] = rec.get("observable_boundary", "")
    return out
