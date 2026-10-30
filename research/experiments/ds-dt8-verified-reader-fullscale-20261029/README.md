# DT-8 — the verified-evidence reader at full DEV scale

Does DT-2's whole-run evidence reader (alert = at least one mechanically verified
verbatim citation) hold at full DEV scale, on Goodfire DEV (150 runs) and CheatBench
DEV (150 items)? Two gates were fixed in `prereg.md` before the first new call.

## Result: both gates FAIL

| gate | rule | measured | verdict |
|---|---|---|---|
| G1, Goodfire | ≥20/80 enacted alerted, ≤2/70 clean | **15/80** enacted (18.8% [11.7–28.7]), **0/70** clean | **FAIL** |
| G2, CheatBench DEV | ≥19/75 recall, ≤1/75 false alarms | **42/74** (56.8% [45.4–67.4]), **12/75** (16.0% [9.4–25.9]) | **FAIL** |

- The Goodfire gap survives the length control (+19.2 pts [+10.0, +29.8],
  within-stratum McNemar 10/0, p = 0.00195) and stays perfectly specific, but recall is
  5 positives short of the adoption rule. DT-2's 50-run estimate (22.0%) did not
  replicate: the 50 runs it never read alert at 4/30.
- CheatBench clears the recall bar by a wide margin and fails on specificity: 12 false
  alarms against a ceiling of 1. Its false-alarm rate is flat across length tertiles
  (18.2% / 16.1% / 15.2%), so it is not a length artifact.
- Unions with the stored cascade v8 / Haiku facets do not rescue it: Goodfire
  22/80 at 1/69 (cascade v8 alone: 10/80 at 1/69); CheatBench 52/74 at 12/75.
- Citation honesty (verified / all citations): 29/52 = 55.8% Goodfire, 133/177 = 75.1%
  CheatBench. The reader's `confidence` is not usable (AUROC 0.316 / 0.492).

## Method

`dt8_reader.py` — `build` (cohorts, prompts, hash checks), `read` (one toolless
`deepseek/deepseek-flash` call per run, ≤4 concurrent), `score` (mechanical quote check,
then label join). Prompt and verification rule are DT-2's, unchanged. The 100 Goodfire
runs DT-2 already read are reused: same run set (sample sha256 verified) and all 100
rebuilt prompt files are byte-identical to DT-2's.

Full numbers, limits and deviations: `research`-external report
`raw/DT-8/report.md` in the squad bridge. `result.json` here is the scored output of
this script, byte-identical across reruns.
