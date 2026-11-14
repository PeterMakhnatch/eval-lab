# Library verifier runtime headroom (HAR-428 extended by HAR-429)

Date: 2026-11-13. Cost: **$0**. **No trial was executed**: `build.py` is a
read-only walk over published results under `~/Developer/eval-lab-results`
(no Docker, no network, no model calls).

A verifier that runs close to its `timeout_sec` scores a correct answer 0 on a
slower host. This table measures the published verifier phase and pytest
runtime of every library package against that package's verifier timeout.

## The answer

The results root holds **388 published trials** of the 12 library package
names. **365** record a verifier phase. **213** print a pytest summary.
**No trial raised `VerifierTimeoutError`.**

Five packages reach more than 50% of their verifier timeout on the recorded
phase:

| Package | Modes seen | Phase max | Timeout | Max / timeout | Pytest max / timeout |
|---|---|---|---|---|---|
| `local-lab/event-summary` | separate | 130.673 s | 60 s | **217.8%** | no pytest line |
| `peter/release-branch-rescue` | separate | 155.505 s | 120 s | **129.6%** | 0.3% |
| `evallab/syn-funcdag-easy` | separate | 76.405 s | 60 s | **127.3%** | no pytest line |
| `deepplanning-v1/travel-lisbon-002` | separate | 60.515 s | 60 s | **100.9%** | no pytest line |
| `terminal-bench/query-optimize` | separate 21, shared 19 | 1437.816 s | 1800 s | **79.9%** | 79.3% |

Only one package reaches more than 50% of its timeout on pytest alone:
`terminal-bench/query-optimize` at 1427.48 s of 1800 s. That trial is
`2026-10-16/unknown-rollout-base-4-query-optimize` in shared mode, so the phase
and the pytest time measure the same window there.

The other four rows are not timed-verifier overruns. Four packages record a
phase above 100% of `timeout_sec` and still record no verifier timeout, because
the phase includes the verifier image build: see the Harbor citation below.
For `environment_mode = "separate"`, `phase_s` is an upper bound on the timed
verifier, and `pytest_s` is the direct measurement of the timed work.

Packages below 50% on the phase maximum: `peter/sheet-recalc` 30.5%,
`terminal-bench/html-js-filter` 28.9%, `peter/sheet-recalc-cutoff` 28.5%,
`evallab/syn-funcdag-hard` 45.1%, `evallab/syn-funcdag-medium` 44.0%,
`petermakhnatch/transaction-reconciliation` 43.2%. `sierra-research/tau3-bench__tau3-retail-1`
has two published trials; both record `ValueError` and no verifier phase, so it
has no phase sample.

## Where Harbor applies `timeout_sec`

Harbor 0.24.0, installed under `.venv/lib/python3.12/site-packages/harbor`:

| Location | What it does |
|---|---|
| `trial/trial.py:859` | `return await asyncio.wait_for(verifier.verify(), timeout=timeout_sec)` — this line caps the verifier work |
| `trial/trial.py:855` | `_verify_with_timeout` converts the resulting `TimeoutError` to `VerifierTimeoutError` (line 863) |
| `trial/trial.py:1559` | `_compute_verifier_timeout_sec` applies `override_timeout_sec`, `max_timeout_sec` and `verifier_timeout_multiplier` to the task's base value |
| `trial/single_step.py:93` | `self.result.verifier = TimingInfo(started_at=self._now())` — the measured phase starts **before** the verifier is prepared |
| `trial/trial.py:922` | `_separate_verifier_env(...)` builds and starts the separate verifier environment before the `wait_for` at line 966 |

So `timeout_sec` in a task file caps `verifier.verify()`, but
`verifier.started_at` in `result.json` also covers the separate verifier
environment start. A separate-mode phase can exceed `timeout_sec` without a
`VerifierTimeoutError`; a shared-mode phase cannot.

## `trials.csv`

One row per published trial whose `task_name` is one of the 12 exact package
names. Columns:

| Column | Meaning |
|---|---|
| `date` | results date directory, `<date>/<job>/<trial>` |
| `job` | job directory name |
| `trial` | trial directory name |
| `package` | `result.json` `task_name` (exact match) |
| `reward` | `verifier_result.rewards.reward`, else `verifier/reward.txt` |
| `phase_s` | `verifier.finished_at - verifier.started_at` in seconds |
| `pytest_s` | last `=+ ... in <seconds>s` line in `verifier/test-stdout.txt` |
| `timeout_sec` | `[verifier] timeout_sec` in the package `task.toml` on the checked-out revision |
| `exception_type` | `exception_info.exception_type` |

Empty `phase_s` means the trial wrote no verifier timing. Empty `pytest_s`
means the verifier printed no pytest summary (for example `event-summary`
prints JSON), not that pytest took 0 s. `timeout_sec` comes from the current
checkout; an older trial may have run an older version of its task file, and
Harbor can also adjust it by override, maximum and multiplier.

## `headroom.csv`

One row per package, in package-name order:

| Column | Meaning |
|---|---|
| `package` | one of the 12 package names |
| `modes_seen` | recorded `verifier_environment_mode` counts, e.g. `separate:21;shared:19` |
| `trials` | matched published trials |
| `n` | trials with a recorded phase (the percentile sample) |
| `phase_p50_s`, `phase_p95_s`, `phase_max_s` | phase seconds; percentiles use linear interpolation |
| `pytest_max_s` | largest recorded pytest duration |
| `pytest_n` | trials that printed a pytest summary |
| `max_pytest_ratio` | `pytest_max_s / timeout_sec` |
| `timeout_sec` | `[verifier] timeout_sec` on the checked-out revision |
| `timeout_exceptions` | trials with `exception_type = VerifierTimeoutError` |
| `exception_types` | other recorded exception types and counts |
| `task_path` | `task.toml` path that supplied `timeout_sec` |

The results root as of 2026-11-13 records these exception types:
`RuntimeError` 11, `AttributeError` 10, `RewardFileNotFoundError` 6,
`EnvironmentStartTimeoutError` 2, `ValueError` 2. No verifier timeout.

## `tests.csv`

One row per test in `verifier/ctrf.json` at a matched trial directory.
The snapshot contains **2921 rows from 322 trials**.
The builder reads only `<date>/<job>/<trial>` directories with an exact package match in `result.json`.
It excludes shallower and deeper layouts, including `tb4-hub`.

| Column | Meaning |
|---|---|
| `date` | results date directory |
| `job` | job directory name |
| `trial` | trial directory name |
| `package` | `result.json` `task_name` (exact match) |
| `test` | test `name` from `results.tests` |
| `status` | test `status` from the CTRF report |
| `duration_s` | recorded test duration, normalized to seconds |
| `duration_source` | `start_stop`, `duration`, or empty when timing is absent |

The builder prefers numeric `stop - start` when `stop >= start` (`start_stop`).
Otherwise, it uses numeric `duration` (`duration`).
It uses a report-wide scale of 1000 if any summary or test `start` or `stop` has absolute value above `1e11`.
It divides both timing sources by that scale.
Otherwise, the scale is 1.
Thus, reports with epoch-millisecond timestamps are divided by 1000.
`pytest-json-ctrf` 0.3.5 writes seconds; the CTRF report written under pytest 9.1.1 uses epoch milliseconds.

A missing or malformed CTRF report produces no test rows.
A valid test with no recorded timing retains its row with empty `duration_s` and `duration_source`.
Missing timing is not a zero duration.

## Slowest verifier test per package

For each package, select the test with the largest maximum `duration_s`.
Break ties by lexicographic test name.
For each trial with positive total recorded test duration, divide the selected test's duration by that total.
The median of those trial shares is the median share below.
Trials that lack the selected test, or have a zero total, do not enter the median.

| Package | Slowest test | Max duration_s | Median share |
|---|---|---|---|
| `local-lab/event-summary` | `correctness` | 0.0 s | undefined |
| `peter/release-branch-rescue` | `test_rescue.py::test_repo_security_invariants` | 0.089 s | 43.4% |
| `peter/sheet-recalc` | `test_sheet.py::test_deep_chain_of_ten_thousand_cells` | 60.101 s | 0.8% |
| `peter/sheet-recalc-cutoff` | `test_sheet.py::test_deep_chain_of_ten_thousand_cells` | 60.134 s | 0.9% |
| `petermakhnatch/transaction-reconciliation` | `test_outputs.py::test_ledger_entries_are_reconciled_without_collateral_changes` | 0.021 s | 87.9% |
| `terminal-bench/html-js-filter` | `test_outputs.py::test_filter_blocks_xss` | 125.899 s | 76.4% |
| `terminal-bench/query-optimize` | `test_outputs.py::test_compare_golden_vs_my_sql_query_correctness` | 1412.152 s | 99.97% |

Five packages have no published CTRF report:
`deepplanning-v1/travel-lisbon-002`, `evallab/syn-funcdag-easy`,
`evallab/syn-funcdag-hard`, `evallab/syn-funcdag-medium`, and
`sierra-research/tau3-bench__tau3-retail-1`.
They have no measured slowest test.

The `event-summary` custom verifier records 0.0 s for all 4 tests.
The name tie selects `correctness`, not `schema`; its median share is undefined because every trial total is 0.
For transaction reconciliation, only **23 of 41** CTRF trials have positive totals.
The other 18 trials do not enter its median share.

For query-optimize, the correctness test peaks at 1412.152 s against a 1800 s verifier timeout, so 1800 / 1412.152 = 1.27; a host about 1.27x slower than the slowest published host would time out a correct answer.
This is a speed-scaling estimate, not an observed timeout.
This test runs the golden query and the task's original slow query and reads no agent output.
See `library/tasks/query-optimize/tests/test_outputs.py:46-66`.

## Regenerate and check

```bash
# rewrite trials.csv, headroom.csv and tests.csv from the default results root
uv run --no-sync python research/experiments/library-verifier-headroom/build.py

# exit 0 when all three CSVs reproduce; exit 1 on drift; exit 2 when inputs are unusable
uv run --no-sync python research/experiments/library-verifier-headroom/build.py --check

# point at a copy of the results root and a fixture task tree
uv run --no-sync python research/experiments/library-verifier-headroom/build.py \
  --root /path/to/eval-lab-results --tasks-root /path/to/tasks --out-dir /tmp/out
```

`--check` compares all three rebuilt CSVs byte-for-byte and prints a unified diff on drift.
A one-byte change to any CSV fails the check until the builder regenerates the files.
The environment variable `EVALLAB_RESULTS_ROOT` sets the default root.

`tests/test_library_verifier_headroom.py` uses `tmp_path` fixtures and never reads the real results root.
It covers phase and pytest parsers, the timeout-exception count, and the exclusion of `terminal-bench/break-filter-js-from-html`.
It also covers the three-CSV drift check and unusable input paths.
The CTRF cases cover missing, malformed, and duration-less reports, both timestamp units, and duration-source precedence.

## Limits

- The committed CSVs are a snapshot of one results root.
  Newly published trials change the tables, so `--check` fails until all three files are regenerated.
- `phase_s` is a wall-clock span around the verifier phase, not a measure of
  timed test work for separate-mode packages.
- The only evidence for a near-timeout failure is a trial that raises
  `VerifierTimeoutError`. These 388 trials contain none, so this table does not
  show a timeout failure. It shows how close published runs came.
