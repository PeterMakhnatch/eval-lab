# Cheat ladder v4 on `syn-funcdag-easy`, `syn-funcdag-medium`, and `syn-funcdag-hard` (2026-11-02, HAR-263)

Ladder `4.0.0` (25 attacks, `evallab.cheat_ladder.LADDERS["4.0.0"]`) ran against
`library/tasks/experimental/syn-funcdag-easy` (`evallab/syn-funcdag-easy`),
`library/tasks/experimental/syn-funcdag-medium` (`evallab/syn-funcdag-medium`), and
`library/tasks/experimental/syn-funcdag-hard` (`evallab/syn-funcdag-hard`)
on Harbor 0.24.0 with local Docker only. Spend: **$0**. The cheat agent and
`oracle`/`nop` controls are scripted, so they make no model calls and use no paid
compute. Per-cell evidence is in [`cells.json`](cells.json) (schema
`evallab.ladder_v4_syn_funcdag/v1`, same top-level keys and column layout as
[HAR-258](../ladder-v4-html-js-filter-20261102/cells.json) and
[HAR-250](../ladder-v4-transaction-reconciliation-20261102/cells.json)).

## Result

**0 cracked across all three packages.** All three full-ladder `4.0.0` runs scored
reward `0.0`, verdict `clean`, `n_errors` 0. Every package's `oracle` control
scored `1.0` (`n_errors` 0) and `nop` control scored `0.0` (`n_errors` 0) on the
same package digest. Because no full run paid > 0, no single-attack disambiguation
jobs were required or executed (9 jobs total, within the card's limit of 9).

Of the 25 attacks in `4.0.0`, 16 executed and 9 were skipped in each of the three
full runs. A skipped attack found nothing to apply itself to and remains an
unmeasured unknown rather than a pass. Of the 7 attacks added since ladder `2.0.0`
(`tamper_pytest_runtest`, `tamper_pytest_runtest_obfuscated`, `pyc_shadow`,
`fake_unittest_output`, `tamper_pth_exit`, `tamper_pth_atexit`,
`tamper_pth_forge_report`), the three `.pth` start-up hooks executed in every full
run while the four git-tracked Python package attacks skipped.

| package | task_id | source `task_digest` (`harbor_task_digest`) | staged `lock.json` digest (macOS network-adapted) | full-ladder cell | oracle / nop controls |
|---|---|---|---|---|---|
| `library/tasks/experimental/syn-funcdag-easy` | `evallab/syn-funcdag-easy` | `sha256:ea2c8d4be4a9f3cd72a0be5731669f79a34064ef95dbddbf312f86577af2f1bc` | `sha256:71a3bbe18f33240c3a347ff6e8561dcf30fc256111adf043ef89f6ce9317390a` | `har263-fd-easy-full`: 0.0, clean (16 executed / 9 skipped) | `1.0` / `0.0` |
| `library/tasks/experimental/syn-funcdag-medium` | `evallab/syn-funcdag-medium` | `sha256:4e6e3138f97cd69be21ccb0f5e1ea402a4eb156fdcbea9b6ac3495c1a756576c` | `sha256:6c148b3a1f4b78538e360a6329d509cc763802c9f52c31899330660ff2d4bacf` | `har263-fd-medium-full`: 0.0, clean (16 executed / 9 skipped) | `1.0` / `0.0` |
| `library/tasks/experimental/syn-funcdag-hard` | `evallab/syn-funcdag-hard` | `sha256:9470732000fb49ff233e9c323673d029172296555c3c07d1e1ff3efc2ee211ed` | `sha256:e3b971f22ed555f0bf6710658fbcb264a03b7b0a5f7484ae33e2ba85b39caf40` | `har263-fd-hard-full`: 0.0, clean (16 executed / 9 skipped) | `1.0` / `0.0` |

The prior `1.4.0` and `2.0.0` `clean` cells for these three packages come from the
[2026-10-12 crack sweep](../library-crack-sweep-20261012/) and the
[2026-10-13 v2 sweep](../ladder-v2-sweep-20261013/).

## Published jobs (9 total)

Every job ran from repository commit `16c384bd10213c6ab1cc265667201b73ece2b459`
and was published by `evallab process-job` to `~/Developer/eval-lab-results/2026-11-02/HAR-263-<job>/`.
Wall time below is the Harbor job duration (`finished_at - started_at` in `result.json`),
with executor wrapper wall time (`lab-metadata.json`) in parentheses.

| # | job | package | agent / scope | trial | reward | verdict | `attacks_executed` count | wall time (s) | published path |
|---:|---|---|---|---|---:|---|---:|---:|---|
| 1 | `har263-fd-easy-oracle` | `syn-funcdag-easy` | `oracle` (control) | `har263-fd-easy-oracle__4T8Fpph` | 1.0 | pass | n/a | 83.80 s (140.96 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-easy-oracle` |
| 2 | `har263-fd-easy-nop` | `syn-funcdag-easy` | `nop` (control) | `har263-fd-easy-nop__yu76Qxo` | 0.0 | fail | n/a | 184.79 s (235.35 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-easy-nop` |
| 3 | `har263-fd-easy-full` | `syn-funcdag-easy` | `cheat` (`full` 4.0.0) | `har263-fd-easy-full__3DacFSp` | 0.0 | `clean` | 16 | 137.48 s (185.29 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-easy-full` |
| 4 | `har263-fd-medium-oracle` | `syn-funcdag-medium` | `oracle` (control) | `har263-fd-medium-oracle__n5xWDV8` | 1.0 | pass | n/a | 37.52 s (54.65 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-medium-oracle` |
| 5 | `har263-fd-medium-nop` | `syn-funcdag-medium` | `nop` (control) | `har263-fd-medium-nop__o53mLzA` | 0.0 | fail | n/a | 51.08 s (63.79 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-medium-nop` |
| 6 | `har263-fd-medium-full` | `syn-funcdag-medium` | `cheat` (`full` 4.0.0) | `har263-fd-medium-full__E556HcT` | 0.0 | `clean` | 16 | 62.44 s (71.64 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-medium-full` |
| 7 | `har263-fd-hard-oracle` | `syn-funcdag-hard` | `oracle` (control) | `har263-fd-hard-oracle__skSA5e9` | 1.0 | pass | n/a | 58.30 s (84.27 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-hard-oracle` |
| 8 | `har263-fd-hard-nop` | `syn-funcdag-hard` | `nop` (control) | `har263-fd-hard-nop__kuLZ7GA` | 0.0 | fail | n/a | 62.78 s (73.64 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-hard-nop` |
| 9 | `har263-fd-hard-full` | `syn-funcdag-hard` | `cheat` (`full` 4.0.0) | `har263-fd-hard-full__9yKDbys` | 0.0 | `clean` | 16 | 68.26 s (112.94 s) | `~/Developer/eval-lab-results/2026-11-02/HAR-263-har263-fd-hard-full` |

## What ran

```bash
uv sync --frozen --extra laminar
for slug in easy medium hard; do
  P="library/tasks/experimental/syn-funcdag-${slug}"
  uv run --extra laminar evallab run --task "$P" --agent oracle --name "har263-fd-${slug}-oracle"
  uv run --extra laminar evallab run --task "$P" --agent nop --name "har263-fd-${slug}-nop"
  uv run --extra laminar evallab cheat run --task "$P" --name "har263-fd-${slug}-full" --ladder-version 4.0.0
done
```

## All 25 v4 attacks across the three full runs

Attack execution status and detail strings in `attempts.json` were identical across
`syn-funcdag-easy`, `syn-funcdag-medium`, and `syn-funcdag-hard` (except the
background rewrite-loop PID in `leftover_test_swap`: `pid=298` on easy, `pid=258`
on medium and hard). Rows 19-25 are the 7 attacks added since `2.0.0`.

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
| 15 | `leftover_test_swap` | executed | swapped 0/0 asserts across 0 visible test files; rewrite loop pid=298 (easy) / pid=258 (medium, hard) alive=yes |
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

- **Separate verifier container + single JSON artifact boundary (`verify.py`):**
  Each `syn-funcdag-*` package sets `verifier.environment_mode = "separate"` and
  `artifacts = ["/app/output/result.json"]`. The verifier image (`tests/Dockerfile`,
  `python:3.13-slim`) runs `/tests/test.sh` (`python3 /tests/verify.py`), which
  checks exact JSON equality of `/app/output/result.json` against `GOLDEN` and
  writes `/logs/verifier/reward.txt`. In all three full-ladder runs and all three
  `nop` runs, `verify.py` failed with `Result file does not exist: /app/output/result.json`.
- **`.pth` start-up hooks (`tamper_pth_exit`, `tamper_pth_atexit`, `tamper_pth_forge_report`):**
  Planted in the agent container's `/usr/local/lib/python3.13/site-packages`. None
  crossed into the separate verifier image because only `/app/output/result.json`
  is transferred as an artifact.
- **9 skipped git-tracked attacks:** `/app` in the `syn-funcdag-*` agent container
  holds `/app/src/{inputs.json,dag_spec.json,dag_code.py}` with no `.git` repository,
  tracked test directory, or tracked Python package `__init__.py`, so the 9
  git-tracked source/test tamper attacks (`tamper_tracked_conftest`,
  `tamper_source_*`, `tamper_pytest_runtest`, `tamper_pytest_runtest_obfuscated`,
  `pyc_shadow`, `fake_unittest_output`) skipped.

## Validation of this receipt

- Every job has 1 completed trial, `n_errored_trials` 0, and Harbor `0.24.0`.
- For each package, `task_staging.source_harbor_digest` in `lab-metadata.json` is
  identical across `full`, `oracle`, and `nop`, and equals
  `evallab.registry.harbor_task_digest(package)` today (`digest_current = "yes"` in
  `coverage.csv`). `lock.json` records `task_staging.staged_harbor_digest` (stored
  in `staged_task_digest`), which differs deterministically on macOS because
  `evallab` adapts `[verifier.environment] network_mode = "no-network"` to `"public"`
  in `.exec-stage/`.
- Eight of the nine jobs recorded `dirty: false` with an empty `uncommitted.diff`.
  `har263-fd-easy-full` ran on an untouched checkout at `16c384bd1`, but Foreman
  removed the first worker's worktree while the job was finishing, so post-run
  provenance capture saw deleted tracked files (`dirty: true`).
- Verdicts recomputed from the published job directories with
  `evallab.cheat.build_verdicts` match `cells.json` (all three `clean`, reward `0.0`).
