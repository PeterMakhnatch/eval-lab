# query-optimize runtime-check margin (HAR-423)

Date: 2026-11-13. Cost: **$0**. **No trial was executed**: `build.py` is a
read-only walk over published results under `~/Developer/eval-lab-results`
(no Docker, no network, no model calls).

`test_compare_golden_vs_solution_runtime` in
[`library/tasks/query-optimize/tests/test_outputs.py`](../../../library/tasks/query-optimize/tests/test_outputs.py)
line 142 asserts `median(solution_s) <= 1.05 * median(golden_s)`. The oracle
writes `sol.sql` from the same query bytes as `tests/golden.sql`, so every
oracle ratio measures timing noise, not speed. This table records that noise.

## The answer

Of 73 published query-optimize trials, 16 print the timing dict. All 16 are
oracle trials and all scored reward 1.0.

| Statistic | Value |
|---|---|
| Trials walked | 73 |
| Trials with a ratio | 16 |
| Minimum ratio | 0.9719 |
| Median ratio | 0.9932 |
| Maximum ratio | 1.0048 |
| Ratios at or above 1.05 | **0** |
| Golden median range | 0.74 s to 1.61 s |

The largest ratio is 1.0048. The threshold sits 0.0452 above it. That gap is
4.3% of the threshold (4.52 percentage points of ratio). Four trials measured
above 1.0, so the noise is not one-sided; none came within 0.045 of the
threshold. The check passed on every one of the 16 runs. The evidence does not
show that the check is too tight. It shows that the worst observed run sat about
4.3% of the threshold below it.

Decision input for Peter: the current 1.05 line is not the cause of any known
failure on these bytes. A host probe that failed the check is not in this
results root, so this table cannot explain it. Widen the line only for a
measured reason, for example a failing oracle trial with a recorded ratio above
1.05.

## `ratios.csv`

One row per query-optimize trial. Columns:

| Column | Meaning |
|---|---|
| `job` | job directory name under the results root |
| `trial` | trial directory name |
| `role` | cell role: an explicit `oracle`/`nop`/`cheat` token in the job name, else `ladder` when the job recorded `EVALLAB_CHEAT_LADDER_VERSION`, else the agent name |
| `reward` | `verifier_result.rewards.reward`, else `verifier/reward.txt` |
| `golden_median_s` | `median_s` of the golden query, from `verifier/test-stdout.txt` |
| `solution_median_s` | `median_s` of the submitted query, same line |
| `ratio` | `solution_median_s / golden_median_s` |

Empty `golden_median_s`, `solution_median_s` and `ratio` mean the trial never
printed the timing dict. A missing measurement is not a zero. Empty `reward`
means the trial recorded no reward (12 trials ended without a reward file).

The committed CSV is the results root as of 2026-11-13. A newly published
trial changes the rebuilt table, so `--check` fails until the CSV is
regenerated and committed with it.

Task identity comes from the job's `lab-metadata.json` (`declared_task_name`,
`source_task_basename`), the trial's `result.json` `task_name`, and the staged
task path. The job name is not used for identity: ladder and hardening cells
stage the task under names such as `har257-qo-harden-oracle`, and two HAR-262
oracle cells stage it as `qo`.

## Regenerate and check

```bash
# rewrite ratios.csv from the default results root
uv run python research/experiments/query-optimize-runtime-margin/build.py

# exit 0 when ratios.csv reproduces; exit 1 on drift; exit 2 when the root is absent
uv run python research/experiments/query-optimize-runtime-margin/build.py --check

# point at a copy of the results root
uv run python research/experiments/query-optimize-runtime-margin/build.py \
  --root /path/to/eval-lab-results --out /tmp/ratios.csv --check
```

`--check` compares the rebuilt CSV byte-for-byte and prints a unified diff on
drift, so a hand edit fails until the builder regenerates the file. The
environment variable `EVALLAB_RESULTS_ROOT` sets the default root.

`tests/test_query_optimize_runtime_margin.py` covers the parser and the drift
check with fixture stdout under `tmp_path`; it does not read the real results
root.
