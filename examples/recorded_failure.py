#!/usr/bin/env python3
"""Reproducible case: what a recorded constraint-delivery failure looks like.

Runs on the installed package and nothing else — no provider, no key, no
network, no third-party dependency. One command, and the three states a
developer actually meets are each driven out of the real code path.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import types

import constraintrelay

CLI = [sys.executable, "-m", "constraintrelay.cli"]


def cli(*args, cwd, expect=0):
    """Run the CLI and hold it to an EXPECTED exit code.

    `expect` is never defaulted away, because the exit code is the contract a
    CI job reads: **0** nothing needs a decision, **1** a tool or configuration
    error, **2** a reported finding. `receipts` returning 2 over a ledger
    holding a MISSING row is the product working, not failing.
    """
    p = subprocess.run([*CLI, *args], cwd=cwd, capture_output=True, text=True)
    print(f"\n$ constraintrelay {' '.join(args)}")
    if p.stdout.strip():
        print(p.stdout.rstrip())
    if p.stderr.strip():
        print("  stderr:", p.stderr.rstrip()[:900])
    print(f"  [exit {p.returncode}, expected {expect}]")
    assert p.returncode == expect, (
        f"{args} exited {p.returncode}, expected {expect}: {p.stderr[:600]}")
    return p.stdout


class FakeCompletions:
    """Stands in for `client.chat.completions`. Records what it was handed."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return {"id": f"chatcmpl-stub-{len(self.calls)}",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class FakeClient:
    """Same attach path as `openai.OpenAI`: .chat.completions.create."""

    def __init__(self):
        self.chat = types.SimpleNamespace(completions=FakeCompletions())


TOOLS = [{"type": "function",
          "function": {"name": "fetch_prices",
                       "description": "fetch prices",
                       "parameters": {"type": "object", "properties": {}}}}]


def main() -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="cr-case-"))
    skill_dir = root / "skills" / "report-writer"
    skill_dir.mkdir(parents=True)

    print("=" * 72)
    print("STEP 1 — a skill document with two declarations the author marked")
    print("=" * 72)
    skill_md = (
        "---\nname: report-writer\n---\n\n"
        "# Report writer\n\n"
        "<!-- constraintrelay: critical id=RW-R1 -->\n"
        "The report must state its data cut-off date.\n\n"
        "<!-- constraintrelay: critical id=RW-R2 -->\n"
        "Never quote a price without naming the source it came from.\n")
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    print(f"\n{skill_dir.relative_to(root)}/SKILL.md:")
    print("".join(f"  | {ln}\n" for ln in skill_md.splitlines()))

    print("=" * 72)
    print("STEP 2 — build a library, then ADOPT the attestation draft")
    print("=" * 72)
    cli("container", "build", "--source", ".", "--skill", "skills/report-writer",
        "--source-version", "v1", "--out", "lib.json", cwd=root)

    cli("container", "attest", "--source", ".", "--skill", "skills/report-writer",
        "--library", "lib.json", "--confirmed-by", "Example Author", cwd=root)

    att_path = skill_dir / ".constraintrelay-attestation.json"
    att = json.loads(att_path.read_text())
    print("\n  draft confirmation_method is empty -> the skill is "
          "PENDING_CONFIRMATION and delivers nothing.")
    print("  Adopting is a separate human act. Here it is, recorded:")
    att["confirmation_method"] = "written_reply"
    att["evidence_ref"] = "examples/README.md — this file is the decision record"
    att["confirmed_at"] = "2026-11-18T00:00:00Z"
    att_path.write_text(json.dumps(att, indent=2) + "\n", encoding="utf-8")
    for k in ("confirmed_by", "confirmation_method", "evidence_ref"):
        print(f"    {k}: {att[k]}")

    cli("container", "check", "--source", ".", "--skill", "skills/report-writer",
        "--library", "lib.json", cwd=root)

    print("=" * 72)
    print("STEP 3 — attach a guard and drive three requests")
    print("=" * 72)
    client = FakeClient()
    guard = constraintrelay.attach(client, host="openai-python",
                                   library=str(root / "lib.json"),
                                   source=str(root),
                                   receipts_path=str(root / "receipts.jsonl"))

    print("\n  (a) skill ACTIVE, request offers tools  -> verifiable boundary")
    with guard.active("skills/report-writer"):
        client.chat.completions.create(
            model="stub", messages=[{"role": "user", "content": "write the report"}],
            tools=TOOLS)
    r = guard.last_receipt
    print(f"      receipt.ok = {None if r is None else r.ok}")
    if r is not None:
        print(f"      capsule at message index = {r.capsule_message_index}")

    print("\n  (b) skill ACTIVE, request offers NO tools -> THE RECORDED FAILURE")
    print("      A payload with no tools is not a decision-input boundary, so")
    print("      delivery cannot be verified against it. The capsule still")
    print("      travels; the row refuses to call that a confirmed delivery.")
    with guard.active("skills/report-writer"):
        client.chat.completions.create(
            model="stub", messages=[{"role": "user", "content": "write the report"}])
    r = guard.last_receipt
    print(f"      receipt.ok = {None if r is None else r.ok}")

    print("\n  (c) CONTROL — guard uninstalled, same request")
    guard.uninstall()
    client.chat.completions.create(
        model="stub", messages=[{"role": "user", "content": "write the report"}],
        tools=TOOLS)
    sent = client.chat.completions.calls[-1]
    has_capsule = any("constraintrelay" in json.dumps(m).lower()
                      for m in sent["messages"])
    print(f"      messages in payload = {len(sent['messages'])}, "
          f"capsule present = {has_capsule}")
    print("      -> with no guard, nothing is injected and nothing is recorded.")
    assert not has_capsule, "control failed: a capsule travelled with no guard"

    print("\n" + "=" * 72)
    print("STEP 4 — read the ledger back")
    print("=" * 72)
    # Exit 2 = "a reported finding". That is what makes this usable in CI:
    # the MISSING row above is what the non-zero code is reporting.
    cli("receipts", "--path", str(root / "receipts.jsonl"), cwd=root, expect=2)

    rows = [json.loads(ln) for ln in
            (root / "receipts.jsonl").read_text().splitlines() if ln.strip()]
    statuses = [(row.get("ledger_row") or row).get("status") for row in rows]
    print("\n  statuses in order:", statuses)

    # The point of the whole example, asserted rather than narrated.
    assert statuses == ["DELIVERED_COMPLETE", "DUE_BUT_MISSING"], statuses
    assert len(rows) == 2, "the unguarded control request must leave NO row"
    print("\n  OK — one confirmed delivery, one recorded failure, and the")
    print("  unguarded control left no row at all.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
