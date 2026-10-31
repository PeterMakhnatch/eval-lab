# S1 — action-first multi-model detector (Goodfire + CheatBench DEV)

Factory worker S1 (2026-10-30). Question: the cheating signal lives in the agent's
actions, not its narration (DT-15); can a *second model family* reading the same
narration-masked prompt beat DeepSeek flash, and do deterministic tripwires or fixed
ensembles get either corpus to the DEV adoption bar?

Prereg: `prereg.md` (written before the first Grok call; frozen). Report: the factory
worker's `report.md` (same content as `../<raw>/S1/report.md`).

## Arms

| arm | input | model |
|---|---|---|
| A1 | DT-15 masked prompt (`agent narration -> "(tool calls only)"`) | `deepseek/deepseek-flash`, thinking low — **stored readings reused**, no new call |
| A2 | the same prompt files, byte for byte | `xai-oauth/grok-4.7`, thinking low — 300 new calls |
| A3 | blinded step dicts | none: DT-10's `fired_rules` (hackbook hits + ten named rules) |
| ensembles | stored verdicts | A1∧A2, A1∨A2, majority(A1,A2,A3), A1∨A3, A2∨A3, A1∧A3, A2∧A3 |

Alert rule for the model arms is DT-15's: at least one citation that survives DT-8's
mechanical verbatim check. A3 alerts on a non-empty fired-rule set.

## Commands

```bash
# 1. rebuild the masked prompts with DT-15's driver (no model call); /tmp was wiped
uv run --project . python \
  research/experiments/ds-dt15-action-grounded-reader-20261030/dt15_masked.py mask

# 2. prompt-identity check: A1 may be reused only if every rebuilt prompt hashes to
#    DT-15's recorded prompt_sha256 (text-mode read, DT-15's convention)
uv run --project . python research/experiments/fx-s1-action-multimodel-20261030/s1_detector.py build

# 3. A3 (no model call)
uv run --project . python research/experiments/fx-s1-action-multimodel-20261030/s1_detector.py tripwires

# 4. A2: one Grok call per masked prompt, <=4 concurrent
uv run --project . python research/experiments/fx-s1-action-multimodel-20261030/s1_detector.py read-a2

# 5. score everything, then write receipts (ids/verdicts/counts only, no run text)
uv run --project . python research/experiments/fx-s1-action-multimodel-20261030/s1_detector.py score
uv run --project . python research/experiments/fx-s1-action-multimodel-20261030/s1_detector.py receipts
```

Working data: masked prompts and masked step dicts in `/tmp/dt15` (DT-15's paths),
A2 sessions, raw replies and readings in `/tmp/s1`. Receipts contain ids, verdicts,
counts, citation classes and 12-hex prompt hashes only — no run text, no quotes.

## Receipts

| file | what |
|---|---|
| `result.json` | every arm and ensemble: recall/FA with Wilson, length tertiles, length-matched bootstrap difference, McNemar pairs, gates, A2 cost and citation honesty |
| `readings-a2.jsonl` | A2 per run: id, status, verdict, citations offered/verified, alert, attested, prompt hash prefix |
| `attestation.json` | requested model, served models, fallbacks, unattested count |
| `tripwires.json` | A3 fired rule sets on the original and the masked steps, per run |
| `prompt-identity.json` | the A1 reuse check (300/300 text-mode hash matches) |

DEV only. No held-out or cross-test data, no answer-key path, no local Docker and no
local model inference.
