# Failure Library

Verifiably broken tasks and failed or reward-hacked runs, kept with their provenance, so the QA Task agent, the QA Trace agent and the Reward Seeker can learn from real failures.

- **Contract:** `failure-library/v1` in `src/evallab/failure_library/records.py`.
- **CLI:** `uv run evallab failures --help`.
- **Coordination:** `~/Developer/research-context/bridge/2026-10-27-failure-library/CONTRACT.md`.

## What a record says

- **Finding:** one pinned task version is broken.
  - It names the task's upstream repository and revision as observed, so a later upstream fix never erases the evidence.
  - It records the defect class (taxonomy TS01–TS11 in `research/hackbook/taxonomy.json`), the evidence, who found it, where it was published, and whether it is still broken.
- **RunRecord:** one agent attempt.
  - Holds the source-native labels and who produced them, plus a reference to the transcript.
  - A task that was fixed later does not make its runs useless: they still show model behaviour.

## Verification tiers

The tier is derived from the evidence and never asserted.

| Evidence kind | Tier | Example |
|---|---|---|
| `executed` | V3_executed | our receipt, or a published reproducer that was run |
| `upstream_fix` | V2_upstream_fixed | the maintainers' fix commit, pinned |
| `human_label`, `construction` | V1_external_label | a human audit; ImpossibleBench's impossible-by-construction variants |
| `judge_label`, `static_finding` | V0_unverified | an LLM judge vote; a static scan |

## Where things live

| What | Where |
|---|---|
| Findings (small, reviewed) | `findings/<collection>.jsonl`, sorted by `finding_id` |
| Run manifests (count, sha256, source revision) | `runs/<collection>.manifest.json` |
| Run records (large) | `$EVALLAB_FAILURE_LIBRARY_STATE/runs/<collection>.jsonl` (default `~/.local/state/evallab-failure-library`) |
| Raw source bytes | `<state>/raw/<source>/<revision>/`, read-only after download |

## Leakage rules

- Only `split = DEV` items may feed knowledge packs or agent retrieval.
- Cross-test sets (CheatBench, HackBench) and any key's TEST or heldout items are recorded as `cross_test`, `TEST` or `heldout`. They are never compiled into knowledge.
