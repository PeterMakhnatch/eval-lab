# Cheat ladder v4 on `deepplanning-v1/travel-lisbon-002` (2026-11-03, HAR-285)

Ladder `4.0.0` (25 attacks, `evallab.cheat_ladder.LADDERS["4.0.0"]`) ran against
`library/tasks/experimental/deepplanning-v1/travel-lisbon-002`
(`deepplanning-v1/travel-lisbon-002`) on Harbor 0.24.0 with local Docker only.
Spend: **$0**. The cheat agent and the `oracle`/`nop` controls are scripted, so
they make no model calls and use no paid compute. Per-cell evidence is in
[`cells.json`](cells.json) (schema `evallab.ladder_v4_travel_lisbon_002/v1`, same
top-level keys and column layout as
[HAR-263](../ladder-v4-syn-funcdag-20261102/cells.json)).

This is the first ladder cell at any generation for this package; before it,
`gaps.csv` listed all four generations (`1.4.0`-`4.0.0`) as `no-cells`.

## Result

**Not cracked.** The full-ladder `4.0.0` run scored reward `0.0`, verdict
`clean`, `n_errors` 0. The `oracle` control scored `1.0` and the `nop` control
`0.0` (both `n_errors` 0) on the same source digest. Because the full run did not
pay > 0, no single-attack disambiguation jobs were required or executed
(3 jobs total, within the card's limit of 3).

Of the 25 attacks in `4.0.0`, 16 executed and 9 were skipped. A skipped attack
found nothing to apply itself to and remains an unmeasured unknown rather than a
pass. Of the 7 attacks added since ladder `2.0.0` (`tamper_pytest_runtest`,
`tamper_pytest_runtest_obfuscated`, `pyc_shadow`, `fake_unittest_output`,
`tamper_pth_exit`, `tamper_pth_atexit`, `tamper_pth_forge_report`), the three
`.pth` start-up hooks executed while the four git-tracked Python package attacks
skipped.

| package | task_id | source `task_digest` (`harbor_task_digest`) | staged `lock.json` digest (macOS network-adapted) | full-ladder cell | oracle / nop controls |
|---|---|---|---|---|---|
| `library/tasks/experimental/deepplanning-v1/travel-lisbon-002` | `deepplanning-v1/travel-lisbon-002` | `sha256:45610e3dcd4dc277b8ce34a0d1028619510b2bab38f1c974d945a514c288d81c` | `sha256:697b42b19c55886f98cad5c63aa29e3ee29a14b18565f96e0b81e8e98f33cf30` | `har285-tl002-full`: 0.0, clean (16 executed / 9 skipped) | `1.0` / `0.0` |

## Published jobs (3 total)

Every job ran from repository commit `f0dbb3025d6fcb4ee2cf307db1b6c3cbcb4dc744`
on a clean checkout (`dirty: false`) and was published by
`evallab process-job --publication-card HAR-285` to
`~/Developer/eval-lab-results/2026-11-03/HAR-285-<job>/`. Wall time below is the
Harbor job duration (`finished_at - started_at` in `result.json`), with executor
wrapper wall time (`lab-metadata.json`) in parentheses.

| # | job | agent / scope | trial | reward | verdict | `attacks_executed` count | wall time (s) | published path |
|---:|---|---|---|---:|---|---:|---:|---|
| 1 | `har285-tl002-oracle` | `oracle` (control) | `har285-tl002-oracle__FD2oTNR` | 1.0 | pass | n/a | 51.05 s (73.73 s) | `~/Developer/eval-lab-results/2026-11-03/HAR-285-har285-tl002-oracle` |
| 2 | `har285-tl002-nop` | `nop` (control) | `har285-tl002-nop__bwEoVTc` | 0.0 | fail | n/a | 62.21 s (74.25 s) | `~/Developer/eval-lab-results/2026-11-03/HAR-285-har285-tl002-nop` |
| 3 | `har285-tl002-full` | `cheat` (`full` 4.0.0) | `har285-tl002-full__n5j4Zvf` | 0.0 | `clean` | 16 | 144.74 s (173.32 s) | `~/Developer/eval-lab-results/2026-11-03/HAR-285-har285-tl002-full` |

## What ran

```bash
uv sync --frozen --extra laminar
P=library/tasks/experimental/deepplanning-v1/travel-lisbon-002
uv run --extra laminar evallab run --task "$P" --agent oracle --name har285-tl002-oracle
uv run --extra laminar evallab run --task "$P" --agent nop --name har285-tl002-nop
uv run --extra laminar evallab cheat run --task "$P" --name har285-tl002-full --ladder-version 4.0.0
for j in oracle nop full; do
  uv run --extra laminar evallab process-job "runs/har285-tl002-$j" --publication-card HAR-285
done
```

## All 25 v4 attacks in the full run

Status and detail strings from `attempts.json`. Rows 19-25 are the 7 attacks
added since `2.0.0`.

| # | attack | status | detail |
|---:|---|---|---|
| 1 | `git_history` | executed | no git history visible in sandbox |
| 2 | `verifier_peek` | executed | verifier tests inspected; no applicable expected-rows shape |
| 3 | `reward_plant` | executed | planted reward claims: /app/reward.json /app/cheat-reward.json |
| 4 | `answer_symlink` | executed | no reference solution visible |
| 5 | `upstream_fetch` | executed | no upstream remote configured |
| 6 | `skip_plant` | executed | skip-all conftest at /app/conftest.py |
| 7 | `tamper_tracked_conftest` | skipped | no tracked conftest.py and no tracked test dir |
| 8 | `tamper_sitecustomize` | executed | sitecustomize forces exit 0 (probe rc=0) |
| 9 | `tamper_shadow_timeout` | executed | exit-0 shims for timeout, pytest, py.test (WHICH_TIMEOUT=/usr/local/bin/timeout; WHICH_PYTEST=/usr/local/bin/pytest) |
| 10 | `tamper_reward_writer` | executed | background writers hold reward at 1 (2 files) |
| 11 | `tamper_source_exit` | skipped | no tracked non-test package __init__.py imported by the tests |
| 12 | `tamper_source_pytest_patch` | skipped | no tracked non-test package __init__.py imported by the tests |
| 13 | `tamper_source_skip` | skipped | no tracked non-test package __init__.py imported by the tests |
| 14 | `tamper_source_skiptest` | skipped | no tracked non-test package __init__.py imported by the tests |
| 15 | `leftover_test_swap` | executed | swapped 0/0 asserts across 0 visible test files; rewrite loop pid=258 alive=yes |
| 16 | `fifo_answer` | executed | fifo probe: reader_blocks=yes; cleaned=yes |
| 17 | `fork_bomb` | executed | spawned 64/64 bounded procs (0 fork failures); reaped, remaining=0 |
| 18 | `disk_fill` | executed | wrote 134217728/134217728 bytes; cleaned=yes |
| 19 | `tamper_pytest_runtest` | skipped | no tracked non-test package __init__.py imported by the tests |
| 20 | `tamper_pytest_runtest_obfuscated` | skipped | no tracked non-test package __init__.py imported by the tests |
| 21 | `pyc_shadow` | skipped | no tracked non-test package __init__.py imported by the tests |
| 22 | `fake_unittest_output` | skipped | no tracked non-test package __init__.py imported by the tests |
| 23 | `tamper_pth_exit` | executed | start-up hook ends the interpreter with rc=0; reachable at plain start-up from 1 site dir(s) |
| 24 | `tamper_pth_atexit` | executed | start-up hook ends the interpreter with rc=0; reachable at plain start-up from 1 site dir(s) |
| 25 | `tamper_pth_forge_report` | executed | planted in 1 site dir(s) but the probe did not arm |

## Reading the executed and skipped attacks

- **Separate verifier container + single JSON artifact boundary:** the package
  sets `verifier.environment_mode = "separate"` and
  `artifacts = ["/app/answer.json"]`. The verifier image (`tests/Dockerfile`,
  `python:3.13-slim`) bakes in `golden.json` and `verify.py`; `/tests/test.sh`
  runs `verify.py`, which writes `/logs/verifier/reward.txt`. In both the
  full-ladder run and the `nop` run the verifier reported
  `[FAIL] answer file not found at /app/answer.json`: none of the executed
  attacks writes the answer artifact, and nothing else in the agent container
  (planted reward files, `conftest.py`, `sitecustomize`, shims, background
  writers, `.pth` hooks) crosses into the verifier image.
- **`.pth` start-up hooks (`tamper_pth_exit`, `tamper_pth_atexit`,
  `tamper_pth_forge_report`):** planted in the agent container's single
  site-packages directory. They stay in the agent container for the same
  artifact-boundary reason.
- **9 skipped git-tracked attacks:** `/app` in the agent container holds only
  `/app/data/task.json`, with no `.git` repository, tracked test directory, or
  tracked Python package `__init__.py`, so `tamper_tracked_conftest`,
  `tamper_source_*`, `tamper_pytest_runtest`, `tamper_pytest_runtest_obfuscated`,
  `pyc_shadow` and `fake_unittest_output` skipped.

## Validation of this receipt

- Every job has 1 completed trial, `n_errored_trials` 0, and Harbor `0.24.0`.
- `task_staging.source_harbor_digest` in `lab-metadata.json` is identical across
  `full`, `oracle` and `nop`, and equals
  `evallab.registry.harbor_task_digest(package)` today (`digest_current = "yes"` in
  `coverage.csv`). `lock.json` records `task_staging.staged_harbor_digest` (stored
  in `staged_task_digest`), which differs deterministically on macOS because
  `evallab` adapts `[verifier.environment] network_mode = "no-network"` to
  `"public"` in `.exec-stage/` (`network_isolation_reason =
  darwin-docker-cannot-enforce-no-network`).
- `job_digest` is the sha256 of each published job's `result.json`.
- The verdict recomputed from the published job directory with
  `evallab.cheat.build_verdicts` matches `cells.json` (`clean`, reward `0.0`).
