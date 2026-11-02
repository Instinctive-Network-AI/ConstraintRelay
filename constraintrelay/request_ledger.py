"""One row per decision request, with exactly one of four statuses.

Owner direction 2026-10-21, item 2. A receipt answers *this payload carried
these declarations*. It does not answer *was every request accounted for* —
and a host whose loop runs several requests per turn can satisfy every receipt
it issued while saying nothing about the requests it issued none for. This
ledger is the second question, and it is deliberately a separate module from
the receipt for the same reason `cf/container_receipt.py` is separate from
`cf/container.py`: the thing that delivers may not also be the thing that
certifies the delivery was complete.

The four statuses, and why they are four
----------------------------------------
======================== =====================================================
`NO_DECLARATION_DUE`     the host's load state held nothing deliverable at
                         this request, so no declaration was due and none was
                         sent. A request before any load is this, and so is
                         one where every load was refused a version
`DELIVERED_COMPLETE`     every due declaration was present in this request's
                         payload, verified by a receipt over that payload
`DUE_BUT_MISSING`        something was due and the payload does not carry it.
                         The failure case, and the one a count of receipts
                         hides
`ATTRIBUTION_UNVERIFIED` at least one load behind this request could not be
                         attributed to the agent being delivered into
                         (`constraintrelay.load_attribution`). Its declarations were
                         withheld, so this is **not** `NO_DECLARATION_DUE`:
                         the difference between "nothing was due" and "what
                         was due could not be established" is the whole point
======================== =====================================================

They are **ordered**: attribution first, then missing, then complete, then
nothing due. A request can be several of those things at once — an owned skill
delivered while an unattributable load sat beside it — and the status reports
the **worst**, with every component fact kept beside it so nothing collapses
into the label.

What a row is not promised for
------------------------------
A request this container cannot see produces **no row, and no claim that
there was nothing to see**. `observed_paths` names the send paths rows come
from, and `unobservable_paths` names the ones they do not, both carried in the
output. `completeness()` is therefore stated **relative to the observed
paths** and says so on its face: this ledger is exhaustive over what was
observed and silent over what was not. Claiming otherwise would be the
`AVAILABLE_COMPLETE` mistake one layer up — asserting a complete census from a
partial observation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

#: In precedence order, worst first. `classify()` returns the first that
#: applies, and `STATUSES` is used to assert a row carries exactly one.
ATTRIBUTION_UNVERIFIED = "ATTRIBUTION_UNVERIFIED"
DUE_BUT_MISSING = "DUE_BUT_MISSING"
DELIVERED_COMPLETE = "DELIVERED_COMPLETE"
NO_DECLARATION_DUE = "NO_DECLARATION_DUE"
STATUSES = (ATTRIBUTION_UNVERIFIED, DUE_BUT_MISSING, DELIVERED_COMPLETE,
            NO_DECLARATION_DUE)


class LedgerError(Exception):
    """A ledger that must not be written as asked."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(*, due_ids, delivered_ids, withheld_attribution_ids,
             receipt_ok, capsule_present=None) -> tuple:
    """`(status, why)` for one request. Pure, so a selftest can drive it.

    *due_ids* is the deliverable set the **host's load state** produced for
    this request — not the assembly's own idea of what it delivered, which is
    the claim being checked. *withheld_attribution_ids* are loads that were
    excluded from `due_ids` because they could not be attributed; passing them
    inside `due_ids` as well would double-count them, and passing them nowhere
    would make an unattributable load look like an empty load state.

    *receipt_ok* is three-valued on purpose: `True` a receipt verified this
    payload, `False` one failed it, **`None` no receipt was produced**. None
    cannot be `DELIVERED_COMPLETE`: a delivery nobody verified is not a
    verified delivery.

    *capsule_present* is what the row records beside this sentence, and it is
    read for one reason: **the sentence may not contradict it.** A developer
    trial on 2026-10-26 hit a row that said `capsule_present: true` and, in
    the same row, *"1 of 1 due declaration set(s) are not in this payload"* —
    and the CLI printed the false half. Both halves were produced from the
    same inputs: `delivered_ids` carries ids whose delivery was **verified**,
    so a payload the receipt could not verify (one offering no tools is not a
    decision-input boundary) empties it while the capsule sits in the payload.
    The status stays `DUE_BUT_MISSING` — an unverified delivery is not a
    complete one, and that is deliberate — but with the reason it actually
    has. `None` keeps the original wording, so a caller that cannot observe
    presence says nothing about it rather than guessing.
    """
    due = sorted(set(due_ids or ()))
    delivered = sorted(set(delivered_ids or ()))
    withheld = sorted(set(withheld_attribution_ids or ()))
    if withheld:
        return (ATTRIBUTION_UNVERIFIED,
                f"{len(withheld)} load(s) could not be attributed to the "
                f"agent being delivered into ({', '.join(withheld)}); their "
                "declarations were withheld, so what was due at this request "
                "is not established")
    if not due:
        return (NO_DECLARATION_DUE,
                "the host's load state held nothing deliverable at this "
                "request, so no declaration was due")
    missing = [d for d in due if d not in delivered]
    if missing and capsule_present is True:
        return (DUE_BUT_MISSING,
                f"{len(missing)} of {len(due)} due declaration set(s) are not "
                f"CONFIRMED delivered in this payload: {', '.join(missing)}. "
                f"A capsule WAS present in it, so this is not a missing "
                f"capsule — the delivery could not be VERIFIED here. This "
                f"row's `receipt_ok` and the receipt's own notes name the "
                f"check that was not met; a payload offering no tools is not "
                f"a decision-input boundary, which is the common case.")
    if missing:
        return (DUE_BUT_MISSING,
                f"{len(missing)} of {len(due)} due declaration set(s) are not "
                f"in this payload: {', '.join(missing)}")
    if receipt_ok is None:
        return (DUE_BUT_MISSING,
                "every due skill is claimed delivered but NO receipt was "
                "produced over this payload, so the delivery is unverified — "
                "and an unverified delivery is not a complete one")
    if receipt_ok is False:
        return (DUE_BUT_MISSING,
                "every due skill is claimed delivered and the receipt over "
                "this payload FAILED, so the bytes were not confirmed at the "
                "send boundary")
    return (DELIVERED_COMPLETE,
            f"all {len(due)} due declaration set(s) were present in this "
            "payload and a receipt verified them at the named send boundary")


class RequestLedger:
    """Rows for the requests that were observed. Nothing for the rest."""

    def __init__(self, *, host: str, observed_paths, unobservable_paths,
                 observation_point: str) -> None:
        if not observed_paths:
            raise LedgerError(
                "observed_paths must name the send path(s) these rows come "
                "from. A ledger that does not say what it watched cannot "
                "state its own completeness.")
        if not unobservable_paths:
            raise LedgerError(
                "unobservable_paths must be stated, even as 'none known'. "
                "Leaving it empty by omission is how a partial census reads "
                "as a total one.")
        self.host = host
        self.observed_paths = list(observed_paths)
        self.unobservable_paths = list(unobservable_paths)
        self.observation_point = observation_point
        self._rows: list = []

    def record(self, *, request_seq: int, send_path: str, due_ids,
               delivered_ids, withheld_attribution_ids=(), receipt_ok=None,
               payload_sha256: str = "", capsule_present=None,
               detail: str = "") -> dict:
        if send_path not in self.observed_paths:
            raise LedgerError(
                f"{send_path!r} is not one of this ledger's observed paths "
                f"{self.observed_paths}. A row from an unobserved path would "
                "be a claim about a request this ledger cannot see.")
        status, why = classify(
            due_ids=due_ids, delivered_ids=delivered_ids,
            withheld_attribution_ids=withheld_attribution_ids,
            receipt_ok=receipt_ok, capsule_present=capsule_present)
        row = {
            "request_seq": int(request_seq),
            "send_path": send_path,
            "status": status,
            "why": why,
            "due_ids": sorted(set(due_ids or ())),
            "delivered_ids": sorted(set(delivered_ids or ())),
            "withheld_attribution_ids": sorted(
                set(withheld_attribution_ids or ())),
            "receipt_ok": receipt_ok,
            "payload_sha256": payload_sha256,
            "capsule_present": capsule_present,
            "detail": detail,
            "at": _now(),
        }
        self._rows.append(row)
        return row

    @property
    def rows(self) -> list:
        return list(self._rows)

    def census(self) -> dict:
        out = {s: 0 for s in STATUSES}
        for r in self._rows:
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    def completeness(self) -> dict:
        """Does every observed request carry exactly one status?

        Three separate assertions, because one of them holding does not cover
        for another failing: every row's status is in the known set, the
        census sums to the row count, and the sequence numbers are the
        contiguous run `1..n` with no gap and no repeat. A gap is the shape
        this ledger exists to catch — a request that was sent and never
        recorded.
        """
        census = self.census()
        seqs = [r["request_seq"] for r in self._rows]
        unknown = sorted({r["status"] for r in self._rows} - set(STATUSES))
        expected = list(range(1, len(self._rows) + 1))
        return {
            "rows": len(self._rows),
            "status_census": census,
            "census_sums_to_rows": sum(census.values()) == len(self._rows),
            "every_status_known": not unknown,
            "unknown_statuses": unknown,
            "sequence_is_contiguous_from_one": sorted(seqs) == expected,
            "sequence_observed": seqs,
            "complete_over_observed_paths": bool(
                not unknown
                and sum(census.values()) == len(self._rows)
                and sorted(seqs) == expected),
            "_scope": (
                "complete over the observed send path(s) only: "
                + "; ".join(self.observed_paths)),
            "_not_in_scope": (
                "no row is produced, and no absence is claimed, for: "
                + "; ".join(self.unobservable_paths)),
        }

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "observation_point": self.observation_point,
            "observed_paths": list(self.observed_paths),
            "unobservable_paths": list(self.unobservable_paths),
            "statuses_defined": list(STATUSES),
            "completeness": self.completeness(),
            "rows": self.rows,
            "_what_this_is": (
                "one row per decision request observed at the named send "
                "path(s), each carrying exactly one of four statuses. The due "
                "set comes from the HOST's load state at that request, not "
                "from what the assembly says it delivered."),
            "_what_this_is_not": (
                "a census of every request the host made. A request this "
                "container cannot observe produces no row and no claim; see "
                "unobservable_paths."),
        }

    def save(self, path) -> None:
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
