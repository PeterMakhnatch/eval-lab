# fx-s2 — Goodfire DEV action-visibility census, full cohort, two readers

S2b of the 2026-10-30 research factory. DQ-2 and DQ-5 asked "action or narration?" on 47
Goodfire positives with one reader. This study finishes the census on the whole frozen
Goodfire DEV cohort (150 runs: 80 enacted / 70 clean) with two independent readers, so
the per-run channel is available for every run and the agreement between readers is
measured instead of assumed.

## Design

- Population: the frozen DT-8 Goodfire DEV cohort via `dt8_reader.goodfire_rows()`.
- Prompt: DQ-2's, byte for byte (`dq2_label.INSTRUCTIONS`).
- Reader D: `deepseek/deepseek-flash`. Reuses the 47 stored positive readings (DQ-2 15,
  DQ-5 32) after checking the instructions hash and the blinded length; reads the other
  103 runs fresh.
- Reader G: `xai-oauth/grok-4.7`, all 150 runs fresh.
- Mechanical channel rule is DQ-2's unchanged: only a verbatim quote from a `<call>` or
  `<observation>` supports an action; only agent `reasoning`/`message` supports
  narration; nothing else counts. Errors/refusals/unattested calls are `unknown`.
- Labels are joined only in `score`. No prompt, file name or session directory carries a
  label, corpus or split.

## Files

| file | content |
|---|---|
| `fx_s2_census.py` | driver: `build` / `canary` / `read` / `score` |
| `prereg.md` (in the S2b receipts) | frozen design, metrics, judge-queue rule |

Run text, prompts, replies and session logs stay in `/tmp/fx-s2b`; receipts carry ids,
verdicts, counts and intervals only.

## Commands

```bash
uv run --project . python research/experiments/fx-s2-goodfire-census-20261030/fx_s2_census.py build
uv run --project . python research/experiments/fx-s2-goodfire-census-20261030/fx_s2_census.py read --reader d --workers 4
uv run --project . python research/experiments/fx-s2-goodfire-census-20261030/fx_s2_census.py read --reader g --workers 4
uv run --project . python research/experiments/fx-s2-goodfire-census-20261030/fx_s2_census.py score
```

`build` and `score` make no model call. `canary --reader d|g` is one tiny non-cohort
call used to attest the route and the usage record.
