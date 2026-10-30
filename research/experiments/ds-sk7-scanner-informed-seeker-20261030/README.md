# ds-sk7-scanner-informed-seeker-20261030

SK-7: does a **static grader-surface summary** (TQ-1's scanner fields) target a text-only seeker
better than SK-5's hand-written mechanism cards alone, and what does it propose outside the hook
family? Nothing is executed: text proposals only, no grader, no Modal.

Arms, 44 DEV tasks, 2 samples per task per arm, all fresh calls:

- `R1C` — SK-5's cards arm, byte-identical (the builder fails unless its prompt sha256 equals
  `SK-5/receipts/prompts.json`'s `R1C` digest).
- `R1CS` — `R1C` + a "Static grader-surface summary" block built only from
  `TQ-1/receipts/scan-all.json`: scalar features verbatim, list features as item counts, and the
  names of the flagged classes. No evidence lines, no task/dataset identifiers, no defect/witness
  information. Template in the SK-7 `prereg.md`.

Route: `deepseek/deepseek-flash`, thinking `low`, tool-less, max 4 concurrent processes.
Answers in SK-5's `=== FILE: ... ===` / `=== END ===` format, parsed with SK-5's `parser.py`.

## Files

| file | role |
|---|---|
| `build_prompts.py` | builds both arms from SK-5's staged prompts + scan-all summary; verifies R1C parity |
| `runner.py` | one headless `omp` call per attempt, attests served route, writes `attempts.jsonl` |
| `stats_run.py` | parse rate, witness agreement (S1/S2), per half, paired McNemar, non-hook proposals |
| `attest.py` | route attestation summary |

Receipts (prompts manifest, attempts, stats, attestation) are written to
`bridge/2026-10-29-ds-squad/raw/SK-7/receipts/`, next to the report.

## Reproduce

```bash
# 1. prompts (requires SK-5's /tmp/sk2-stage stage and SK-5/receipts)
python3 build_prompts.py
# 2. calls (writes receipts/attempts.jsonl; resumable, skips attested records)
SK7_JOBS=4 SK7_CAP=0.90 python3 runner.py
# 3. numbers
python3 stats_run.py && python3 attest.py
```
