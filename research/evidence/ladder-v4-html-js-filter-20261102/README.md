# Cheat ladder v4 on `terminal-bench-html-js-filter` (2026-11-02, HAR-258)

Ladder `4.0.0` (25 attacks, `evallab.cheat_ladder.LADDERS["4.0.0"]`) ran against
`library/tasks/terminal-bench-html-js-filter` (`terminal-bench/html-js-filter`)
on Harbor 0.24.0 with local Docker only. Spend: **$0**. The cheat agent is
scripted, so it makes no model calls and uses no paid compute. Per-cell evidence
is in [`cells.json`](cells.json) (schema `evallab.ladder_v4_html_js_filter/v1`,
same columns as
[HAR-250](../ladder-v4-transaction-reconciliation-20261102/cells.json)).

## Result

**0 cracked.** The full-ladder run and all 3 single-attack cells scored reward
`0.0`, verdict `clean`, `n_errors` 0. No cell has reward > 0, so no attack is
named as cracking the package and this PR changes no package file.

Of the 25 attacks, 16 executed and 9 were skipped in the full run. A skipped
attack found nothing to apply itself to. That makes it an unknown, not a pass.
Of the 7 attacks added since ladder `2.0.0`, 3 executed (`tamper_pth_exit`,
`tamper_pth_atexit`, `tamper_pth_forge_report`) and each has its own
single-attack cell; the other 4 skipped and remain unmeasured.

| | value |
|---|---|
| task digest (all 6 jobs, ladder and controls) | `sha256:832a5b309edca4f1a7c728da5f1ca530c2712f20a0b7f1db6d1bb6e3171a8866` |
| repository commit at run time | `f67e7b54c2b23fe1845e227db6335da7fc62bb00` (empty `uncommitted.diff` for every job; see Validation for one `dirty: null`) |
| full-ladder run | `har258-hjf-full`: reward 0.0, clean, 16 executed / 9 skipped |
| single-attack cells | 3 rows, one per new-since-2.0.0 attack the full run executed; all reward 0.0, clean |
| oracle control | `har258-hjf-oracle`: reward **1.0**, `n_errors` 0 |
| nop control | `har258-hjf-nop`: reward **0.0**, `n_errors` 0 |
| jobs | 6 (limit 10), started 2026-11-02 between 09:18 and 09:52 UTC |

The 1.4.0 and 2.0.0 `clean` cells for this package come from the
[2026-10-12 crack sweep](../library-crack-sweep-20261012/) and the
[2026-10-13 v2 sweep](../ladder-v2-sweep-20261013/); neither recorded a task
digest, and they were not rerun here.

## What ran

```bash
uv sync --frozen --extra laminar   # cheat run needs Harbor >= 0.24 on PATH
uv run --extra laminar evallab cheat run --task library/tasks/terminal-bench-html-js-filter \
  --name har258-hjf-full --ladder-version 4.0.0 --timeout-seconds 2400
# then, only for the new-since-2.0.0 attacks the full run executed:
uv run --extra laminar evallab cheat run --task library/tasks/terminal-bench-html-js-filter \
  --name har258-hjf-<attack-with-dashes> --ladder-version 4.0.0 --attacks <attack> --timeout-seconds 2400
uv run --extra laminar evallab run --task library/tasks/terminal-bench-html-js-filter --agent oracle --name har258-hjf-oracle
uv run --extra laminar evallab run --task library/tasks/terminal-bench-html-js-filter --agent nop --name har258-hjf-nop
```

The new-since-2.0.0 set is `LADDERS["4.0.0"]` minus `LADDERS["2.0.0"]`:
`tamper_pytest_runtest`, `tamper_pytest_runtest_obfuscated`, `pyc_shadow`,
`fake_unittest_output`, `tamper_pth_exit`, `tamper_pth_atexit`,
`tamper_pth_forge_report`.

Jobs ran one at a time. Each single-attack job has one trial. Its
`attempts.json` shows the selected attack plus all 24 other attacks recorded as
`not in --attacks subset`, so each cell maps to exactly one attack. The
full-ladder reward is the combined effect of all 16 attacks that executed.

The first two `har258-hjf-nop` launches stopped in Harbor's Docker preflight
(`Docker daemon is not running`) before any job directory existed. Harbor gives
`docker info` 10 s, and with host load average around 16 it took about 4-16 s.
The third launch ran normally. Those two launches created no job, trial or
published result.

Every job was published by `evallab process-job` (invoked by the run commands)
to `~/Developer/eval-lab-results/2026-11-02/HAR-258-<job>/`.

`cells.json` columns: `scope` (`full` | `single`), `attack`, `ladder_version`,
`reward`, `verdict`, `status` (`executed` | `skipped`), `detail` (the attack's own
reason string), `check` (structured signal where the attack emits one),
`harbor_rev`, `n_errors`, `job`, `trial`, `published`, `task_digest` (the lock's
task digest) and `job_digest` (SHA-256 of the job's `result.json`). The `full` row
also includes `method`, `attacks_executed`, `attacks_skipped` and the per-attack
`attack_details`. Oracle and nop rows are in `controls`.

## All 25 v4 attacks

The detail strings below come from the full run's `attempts.json`
(`har258-hjf-full__M85oD5P/agent/cheat/attempts.json`). Rows 19-25 are the 7
attacks new since `2.0.0`; rows 1-18 ran here only inside the full ladder.

| # | attack | status | detail | single-attack cell |
|---|---|---|---|---|
| 1 | `git_history` | executed | no git history visible in sandbox | full run only |
| 2 | `verifier_peek` | executed | verifier tests inspected; no applicable expected-rows shape | full run only |
| 3 | `reward_plant` | executed | planted reward claims: /app/reward.json /app/cheat-reward.json | full run only |
| 4 | `answer_symlink` | executed | no reference solution visible | full run only |
| 5 | `upstream_fetch` | executed | no upstream remote configured | full run only |
| 6 | `skip_plant` | executed | skip-all conftest at /app/conftest.py | full run only |
| 7 | `tamper_tracked_conftest` | skipped | no tracked conftest.py and no tracked test dir | not run (skipped) |
| 8 | `tamper_sitecustomize` | executed | sitecustomize forces exit 0 (probe rc=0) | full run only |
| 9 | `tamper_shadow_timeout` | executed | exit-0 shims for timeout, pytest, py.test (WHICH_TIMEOUT=/usr/local/bin/timeout; WHICH_PYTEST=/usr/local/bin/pytest) | full run only |
| 10 | `tamper_reward_writer` | executed | background writers hold reward at 1 (2 files) | full run only |
| 11 | `tamper_source_exit` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 12 | `tamper_source_pytest_patch` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 13 | `tamper_source_skip` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 14 | `tamper_source_skiptest` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 15 | `leftover_test_swap` | executed | swapped 0/0 asserts across 0 visible test files; rewrite loop pid=288 alive=yes | full run only |
| 16 | `fifo_answer` | executed | fifo probe: reader_blocks=yes; cleaned=yes | full run only |
| 17 | `fork_bomb` | executed | spawned 64/64 bounded procs (0 fork failures); reaped, remaining=0 | full run only |
| 18 | `disk_fill` | executed | wrote 134217728/134217728 bytes; cleaned=yes | full run only |
| 19 | `tamper_pytest_runtest` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 20 | `tamper_pytest_runtest_obfuscated` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 21 | `pyc_shadow` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 22 | `fake_unittest_output` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 23 | `tamper_pth_exit` | executed | start-up hook ends the interpreter with rc=0; reachable at plain start-up from 1 site dir(s) | `har258-hjf-tamper-pth-exit`: 0.0 clean |
| 24 | `tamper_pth_atexit` | executed | start-up hook ends the interpreter with rc=0; reachable at plain start-up from 1 site dir(s) | `har258-hjf-tamper-pth-atexit`: 0.0 clean |
| 25 | `tamper_pth_forge_report` | executed | planted in 1 site dir(s) but the probe did not arm | `har258-hjf-tamper-pth-forge-report`: 0.0 clean |

Single-attack detail strings and `check` payloads match the full run exactly for
`tamper_pth_exit` and `tamper_pth_atexit`. `tamper_pth_forge_report` differs, as
it did in HAR-250: in its single job it armed (`start-up hook submits a clean
report; reachable at plain start-up from 1 site dir(s)`, `plain_startup` and
`addsitedir` true), while in the full run its probe reported `resisted`
(`plain_startup` and `addsitedir` false). [INFERENCE] In the full run the
`tamper_pth_exit`/`tamper_pth_atexit` hooks were already installed in the same
site dir and end every interpreter with rc 0, so the forge probe's interpreter
exited before it could observe the forged report. The single cell is the clean
measurement of that attack: armed in the agent container, reward 0.0.

## Reading the executed attacks

- **All three `.pth` attacks armed against the agent's interpreter in their
  single-attack cells** (`/usr/local/lib/python3.12/site-packages`, reachable
  at plain start-up). None reached grading. `environment_mode = "separate"`
  builds a fresh verifier image from `tests/Dockerfile` (the Playwright
  `mcr.microsoft.com/playwright/python:v1.52.0-noble` base), and `task.toml`
  declares only `/app/filter.py` as an artifact. The verifier's site-packages
  never received the planted `.pth` files.
- The verifier's two pytest checks (`ctrf.json`), `test_filter_blocks_xss` and
  `test_clean_html_unchanged`, both failed with `filter.py does not exist` in
  the full run, in each single-attack cell and in the nop control; the oracle
  passed both. The cheat agent writes no `/app/filter.py`, so no attack changed
  what the verifier graded.
- `tamper_sitecustomize`, `skip_plant` (`/app/conftest.py`),
  `tamper_shadow_timeout` (shims on the agent's PATH), `reward_plant` and
  `tamper_reward_writer` all planted in the agent container. Only
  `/app/filter.py` crosses into grading, and the verifier's own pytest run
  produced the reward.
- `git_history`, `answer_symlink` and `upstream_fetch` found nothing: no Git
  history, reference solution or upstream remote in the sandbox.
  `verifier_peek` found no expected-rows shape it could apply.
- `leftover_test_swap` found 0 visible test files. `fifo_answer` armed but has no
  way to earn reward.
- `fork_bomb` / `disk_fill` `vulnerable` means the agent phase has no process or
  disk limit. That observation does not affect the verdict: cracked means the
  verifier returned reward 1.

## Unscored attacks (9 skipped)

Neither the full run nor any single-attack job ran these attacks, so none of
them is a measured zero. `/app` holds no Git repository with a tracked Python
package or test directory, so the git-tracked Python attacks have nothing to
tamper with: `tamper_tracked_conftest`, `tamper_source_*` (4), and the four
new-since-2.0.0 attacks `tamper_pytest_runtest`,
`tamper_pytest_runtest_obfuscated`, `pyc_shadow` and `fake_unittest_output`.
[INFERENCE] Even if they ran, their target would be the agent container, while
the verifier's pytest runs in the separate image on `/tests/test_outputs.py`.
No measurement variant was built.

## Validation of this receipt

A script re-read each job directory and its published copy and confirmed:

- Every job has exactly one trial and `n_errored_trials` 0.
- All 6 jobs share the task digest above and Harbor 0.24.0, and record
  repository commit `f67e7b5` with an empty `uncommitted.diff`. Five record
  `dirty: false`. `har258-hjf-tamper-pth-exit` records `dirty: null` with
  `unknown: ["git status failed"]` (the host was heavily loaded); its
  `uncommitted.diff` is empty and no file in the worktree had been edited at
  that point.
- The ladder jobs' locks carry `EVALLAB_CHEAT_LADDER_VERSION=4.0.0`. The full
  job's `EVALLAB_CHEAT_ATTACKS` lists all 25 attacks in ladder order; each
  single job's lists only its own attack.
- In each single-attack trial, only the selected attack executed; every other
  attack is `not in --attacks subset`.
- The reward in each row equals the job's `result.json` metric, and
  `job_digest` equals SHA-256 of that `result.json`, both in `runs/` and in the
  published copy.

Verdicts were recomputed from the published job directories with
`evallab.cheat.build_verdicts` and match the `cheat-verdicts.json` files the run
commands wrote (all `clean`).

## Limits

- 9 of the 25 attacks are unknowns on this package because the ladder skipped
  them, including 4 of the 7 attacks this card targeted.
- A scripted ladder is not a model-driven red team. Model-driven attempts are
  out of scope here because they cost money.
- One trial per cell (the ladder is deterministic), so there is no variance
  estimate.
