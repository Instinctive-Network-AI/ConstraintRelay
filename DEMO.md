# ConstraintRelay v0.1 — the demo

One command, against a real public repository, on a scratch copy.

```bash
pip install -e .
git clone https://github.com/HKUDS/Vibe-Trading /tmp/Vibe-Trading
./demo/run_demo.sh /tmp/Vibe-Trading
```

It never writes to the repository you point it at — it rsyncs a copy to
`/tmp/constraintflow-demo` first.

## What the seven steps show

| Step | Command | What you should see |
|---|---|---|
| 1 | `init` | 89 skills found, 1,019 candidate constraints (896 operative, 123 advisory), 16 unresolved cases. **Nothing in the store yet** — extraction is heuristic and never promotes on its own. |
| 2 | `import --confirm-all` | 1,017 confirmed — two candidates repeat text byte-identically within the same source file, so they share a `candidate_id` and collapse into one record each. The store is now authoritative; the skill files are no longer an independent copy. |
| 3 | `skills` | 87 of 89 skills carry constraints. The 2 that do not are still listed, so a skill is never hidden by having found nothing in it. |
| 4 | `constraints --skill social-media-intelligence` | 13 constraints with full provenance — file, char span, line range, source hash. |
| 5 | `active` | **the v0.1 delivery artifact**: the capsule the model receives, 4,084 chars / ~1,021 est. tokens for two active skills (26 constraints), and 2,352 chars / ~588 tokens after one is released. Rebuilt from the store on every request, so it does not depend on surviving compaction. |
| 6 | `deliver` | the load-time proof through the **real** Vibe-Trading loader and its 10,000-character tool-result cut: 13/13 constraints present in the model input. |
| 7 | `diff` | `UNCHANGED 1017` — nothing drifted. |

**Step 4 is where you should be sceptical.** Of those 13, roughly two or three
are not really agent constraints — `C002` (*"Potential alpha, but requires
filtering"*) and `C011` (*"Low latency, no GPU required"*) are descriptive table
rows, and `C009`/`C010` are `**Characteristics**:` glosses. That is the measured
~80% precision showing up in a single skill, and it is why `import` exists as a
confirmation step rather than running automatically. Equally, 13 is not all the
rules in that document; see [LIMITATIONS.md §1](LIMITATIONS.md).

## The one number worth pausing on

`social-media-intelligence-C013` is a scope restriction —

> *Version: v1.0 | Created: 2026-03-29 | Scope: quantitative research / factor
> mining (not for direct live-trading signals)*

— at characters **[40552, 40674)** of a 40,675-character skill document. The
harness truncates every tool result at 10,000 characters, so in the unmodified
system that sentence **never reaches the model**. Step 6 shows it arriving whole;
step 5 shows it arriving on every subsequent request too.

Confirm it yourself:

```bash
constraintrelay constraint /tmp/constraintflow-demo/repo social-media-intelligence-C013
```

## Any repository with skills

Steps 1–5 and 7 work on any repository with `SKILL.md` directories:

```bash
constraintrelay init ./my-repo
constraintrelay import ./my-repo --confirm-all --tier operative
constraintrelay skills ./my-repo
constraintrelay active ./my-repo --skill <one-of-them> --show
```

If your skills live in a subdirectory, add `--skills-dir path/to/skills`.
Step 6 is Vibe-Trading-specific because it runs that harness's loader.

## Embedding it

See the Python section of [README.md](README.md). The delivery integration is
three lines and changes no file in the agent checkout.
