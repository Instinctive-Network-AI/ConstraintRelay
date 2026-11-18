# examples/

```bash
python examples/recorded_failure.py
```

One command. No provider, no API key, no network, no third-party package — the
installed `constraintrelay` and the standard library. It writes into a fresh
temporary directory and touches nothing you own.

## What it shows

The product's whole claim is one fact per request: **was the declaration you
wrote in this payload?** There are three answers, and a developer meets all
three in the first hour. The script drives each out of the real code path and
then asserts the result, so this file cannot quietly stop being true:

| | request | recorded as |
|---|---|---|
| **(a)** | skill active, request offers `tools=` | `DELIVERED_COMPLETE` — capsule in the payload, verified at the boundary |
| **(b)** | skill active, request offers **no** tools | `DUE_BUT_MISSING` — **the recorded failure** |
| **(c)** | guard uninstalled, same request | **no row at all** — the control |

### Why (b) is the interesting one

A request offering no tools is not a decision-input boundary, so delivery
cannot be *verified* against it. The capsule still travels — the row says
`capsule_present: true` — and the status is still `DUE_BUT_MISSING`, because an
unverified delivery is not a complete one.

That is the product refusing to certify at a boundary it cannot certify at. It
is not the same event as a capsule that never went, and the row's text keeps
the two apart rather than collapsing them into one word. **A receipt that
cannot tell those apart is worth nothing**, which is why the distinction is in
the example rather than in a footnote.

### Why (c) is in the script at all

Without a control, (a) proves less than it appears to. If the stand-in client
reported a capsule whether or not a guard was installed, (a) would be green for
the wrong reason. (c) asserts that with the guard uninstalled **nothing is
injected and no row is written** — so (a)'s pass is attributable to the guard.

## The exit code is part of the output

Step 4 runs `constraintrelay receipts` and requires it to exit **2**, not 0.
That is the CLI's contract — `0` nothing needs a decision, `1` a tool or
configuration error, `2` a reported finding — and the `MISSING` row is what the
2 is reporting. The script passes `expect=` explicitly on every CLI call for
that reason: a helper that accepts any exit code would make the whole example
unfalsifiable.

## What it does not show

Nothing here measures whether the model **read** the declaration, or whether a
model that received it would have chosen differently. **Delivery is not
compliance.** Nothing in this project has measured compliance, and this example
is not evidence about it.

The stand-in client is also not a provider. It has the same attach surface as
`openai.OpenAI` — `.chat.completions.create` — and that is the point: the
example is a complete test of *this code* and no test at all of a provider. The
real-provider runs are recorded in [`../README.md`](../README.md) → *What has
been measured*.

## This file is also a record

The script sets an attestation's `evidence_ref` to this README, because adopting
a declaration is a human act that has to point somewhere. In the example that
pointer is honest: the text in `recorded_failure.py` is the declared position
of this example and nothing more. **In your own repository, point it at the
decision — a commit, a reply, a meeting — not at the file that needs it.**
