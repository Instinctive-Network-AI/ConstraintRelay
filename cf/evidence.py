"""The one canonical evidence artifact — qualification ruling items 1 and 5.

> Gate CLOSED/PASS state must derive from reproducible evidence artifacts.
> `qualification.json` may contain descriptions and references, but not unsupported
> hand-copied current counts.

> Produce one canonical machine-generated results artifact for current
> fixture/corpus counts and gate evidence. Documentation and qualification records
> reference that artifact. Add a consistency check against stale duplicated current
> numbers.

Before this, a current count lived in however many places somebody had typed it.
That is not a hypothetical failure mode: `15/15` survived in `STATUS.md` after the
suite had reported 14 for weeks, and `186/186` survived in three files — including
the gate evidence string that let hermes reach qualification-complete — after the
corpus it was measured over had been deleted from the host. Both were found by a
reviewer, not by us.

So there is exactly one generated file, `cf/data/evidence.json`, produced by
`tools/gen_evidence.py` from the instruments' own output. Everything else
**references** it. `check_stale_numbers` then greps the documentation for the
patterns that carry current counts and fails when a document disagrees with the
artifact — which is the half that makes the rule enforceable rather than aspirational.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from cf import datafiles as _df

DATA = _df.path("evidence.json")


class EvidenceError(Exception):
    """The evidence artifact is missing or unreadable. A configuration error."""


@dataclass(frozen=True)
class Evidence:
    path: Path
    generated_at: str
    implementation: str
    metrics: dict
    sources: dict

    def get(self, key: str, default=None):
        """One metric, by dotted path. Absent is `default`, never a guess."""
        node = self.metrics
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, key: str):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise EvidenceError(
                f"{self.path}: no metric {key!r}. A gate that depends on it cannot "
                f"be decided; regenerate with tools/gen_evidence.py.")
        return value


_MISSING = object()


def load(path: Path | None = None) -> Evidence | None:
    """The artifact, or `None` when it has never been generated.

    `None` rather than an exception, because "no evidence has been generated" is a
    state the qualification layer must be able to report as open gates rather than
    a crash — that is the difference between an unqualified adapter and a broken
    tool.
    """
    p = path or DATA
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{p}: cannot read evidence artifact: {exc}") from exc
    return Evidence(path=p, generated_at=raw.get("generated_at", ""),
                    implementation=raw.get("implementation", ""),
                    metrics=raw.get("metrics", {}), sources=raw.get("sources", {}))


# ---------------------------------------------------------------------------
# item 5: the consistency check against stale duplicated current numbers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StaleNumber:
    file: str
    line: int
    claim: str
    metric: str
    document_says: str
    evidence_says: str

    def __str__(self) -> str:
        return (f"{self.file}:{self.line}: claims {self.document_says} for "
                f"{self.metric}, artifact says {self.evidence_says} — {self.claim}")


@dataclass(frozen=True)
class Claim:
    """A documentation pattern that carries a current count.

    `pattern` must capture the number in group 1. `metric` is the dotted key in the
    artifact. `allow_historical` marks patterns whose surrounding sentence may
    legitimately quote an old figure — those lines are skipped when they also carry
    a word from `HISTORICAL_MARKERS`, because a document that *says* a number is
    historical is doing the right thing and must not be punished for it.
    """

    metric: str
    pattern: str
    note: str = ""
    allow_historical: bool = True
    #: Substrings that must / must not also appear on the line. Needed because two
    #: adapters can state the same shape of fact in the same words: the fixtures
    #: pattern matched Conway's legitimate "10/10 fixtures byte-identical" as a
    #: stale OpenClaw count. A checker that cries wolf gets switched off.
    requires: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()


#: Words that mark a number as deliberately historical rather than current.
HISTORICAL_MARKERS = ("historical", "superseded", "2026-09-30", "was ", "previously",
                      "no longer reproducible", "not reproducible")

CLAIMS: tuple[Claim, ...] = (
    Claim("openclaw.profile_fixtures",
          r"\*\*(\d+)/\d+ fixtures byte-identical\*\*",
          "the OpenClaw port differential fixture count",
          excludes=("conway",)),
    Claim("hermes.layers",
          r"hermes \*\*(\d+)/\d+\*\*",
          "the hermes loader-layer inventory size"),
    Claim("openclaw.layers",
          r"openclaw \*\*(\d+)/\d+\*\*",
          "the openclaw loader-layer inventory size"),
    Claim("layer_coverage.can_fail",
          r"\*\*(\d+) proven able to fail a receipt\*\*",
          "delivery-affecting layers proven able to fail a receipt"),
    Claim("slice.cases",
          r"slice is \*\*(\d+) cases\*\*",
          "vertical-slice case count"),
    Claim("hermes.scanner.rules_total",
          r"(\d+) scanner rules",
          "scanner rules enumerated from the pinned implementation"),
    Claim("openclaw.loader_surface_files",
          r"\*\*(\d+) modules\*\*",
          "the openclaw loader fingerprint surface size"),
    Claim("hermes.scanner.corpus_files",
          r"(\d+) corpus file\(s\)",
          "scanner corpus files compared against the real scanner"),
    Claim("holdout.frozen_pairs",
          r"\*\*(\d+) frozen pair",
          "holdout pairs frozen in the qualifying tranche"),
    Claim("holdout.evaluated_pairs",
          r"holdout[^\n]*?\*\*(\d+) repositor",
          "holdout repositories evaluated"),
)


def check_stale_numbers(docs: "list[Path]", ev: Evidence) -> list[StaleNumber]:
    """Every documented current count that disagrees with the artifact.

    Deliberately conservative about what it calls a disagreement: a line carrying a
    historical marker is skipped, and a metric absent from the artifact is skipped
    rather than reported as a mismatch against nothing. The point is to catch a
    number that has silently gone stale, not to forbid writing about the past.
    """
    out: list[StaleNumber] = []
    compiled = [(c, re.compile(c.pattern)) for c in CLAIMS]
    for doc in docs:
        if not doc.is_file():
            continue
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            for claim, rx in compiled:
                for m in rx.finditer(line):
                    expected = ev.get(claim.metric, _MISSING)
                    if expected is _MISSING:
                        continue
                    if str(m.group(1)) == str(expected):
                        continue
                    if claim.allow_historical and any(
                            w in lowered for w in HISTORICAL_MARKERS):
                        continue
                    if claim.requires and not all(
                            r.lower() in lowered for r in claim.requires):
                        continue
                    if any(x.lower() in lowered for x in claim.excludes):
                        continue
                    out.append(StaleNumber(
                        file=str(doc), line=lineno, claim=claim.note,
                        metric=claim.metric, document_says=m.group(1),
                        evidence_says=str(expected)))
    return out
