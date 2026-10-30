# DQ-6 — strict (substantiation) rubric on DQ-2's CheatBench negatives

Research-only experiment for the 2026-10-29 DeepSeek squad. It finishes the one stratum
DQ-5 could not afford: DQ-2's ten CheatBench clean items under a rubric that forces every
action claim to name the checker part, name the grade effect, and quote a `<call …>` or
`<observation>`.

## What it does

- `dq6.py build` rebuilds the strict prompts for 16 rows (DQ-2's 10 CheatBench negatives +
  the 6 DQ-2 positives with a cited action claim that have no strict reading yet), asserts
  `sha256(STRICT_INSTRUCTIONS) == d5b421a13f76…` (the value DQ-5 recorded) and asserts each
  built prompt is byte-identical to the one DQ-5 already built for the same blind id.
- `dq6.py read` issues one tool-less `deepseek/deepseek-flash` call per row (`--thinking
  low`, `--no-tools`, at most 4 concurrent), budget-guarded at $0.50 measured spend.
- `dq6.py score` applies DQ-2's channel rule plus the strict field rule (plus a
  generic-phrase rejection), reads `run-8a405f225777a079` from DQ-5's strict output
  instead of re-calling it, joins DQ-2's loose verdicts per run, and writes
  `verdicts.jsonl`, `strict-negatives.json`, `attestation.json` and `sample.json`.

Reused by import, not by copy: DQ-5's strict instructions (`dq5.STRICT_INSTRUCTIONS`),
DQ-2's run-body builder/parser/quote classifier (`dq2_label`, whose DT-8/DT-10 imports
carry the blinded step projection).

## Files

| file | contents |
|---|---|
| `dq6.py` | driver (`build` / `read` / `score`) |
| `prereg.md` | preregistration, frozen before the first call |
| `sample.json` | the 16 rows, prompt hashes, byte-identity check vs DQ-5 |
| `verdicts.jsonl` | one line per run: ids, flags, cited-step classes, cost |
| `strict-negatives.json` | per-label rates with Wilson intervals, the gap, Fisher, McNemar |
| `attestation.json` | served model per call, fallbacks, instruction hash |

No run text, no prompt text and no quotable passage is committed; the run bodies and
replies stay in `/tmp/dq6`. DEV cohort (CheatBench DEV) only.

## Spending

16 calls, cap $0.55 / 20 calls. Measured cost is in `strict-negatives.json`
(`usage_new_calls`) and in each `verdicts.jsonl` line's `usage`.
