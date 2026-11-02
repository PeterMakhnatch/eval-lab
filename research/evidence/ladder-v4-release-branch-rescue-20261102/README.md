# Cheat ladder v4 on `release-branch-rescue` (2026-11-02, HAR-241)

Ladder `4.0.0` (25 attacks, `evallab.cheat_ladder.LADDERS["4.0.0"]`) ran against
`library/tasks/release-branch-rescue` (`peter/release-branch-rescue`) on Harbor
0.24.0 with local Docker only. Spend: **$0**. The cheat agent is scripted, so it
makes no model calls and uses no paid compute. Per-cell evidence is in
[`cells.json`](cells.json) (schema `evallab.ladder_v4_release_branch_rescue/v1`).

## Result

**0 cracked.** The full-ladder run and all 15 single-attack cells scored reward
`0.0`, verdict `clean`, `n_errors` 0. No cell has reward > 0, so no job is named
as cracked and this PR changes no package file.

Of the 25 attacks, 15 executed and 10 were skipped. A skipped attack found
nothing to apply itself to. That makes it an unknown, not a pass. Three of the
15 executed attacks (`tamper_pth_*`) also armed nothing (see below).

| | value |
|---|---|
| task digest (all 18 jobs, ladder and controls) | `sha256:5adc25fcb4b1de0cffaf6686d585f386b7e5acf26dc5f988ca96e9437d79bf29` |
| repository commit at run time | `0a1c0936485f8cc2fdec5bdcbf4dc23442333150`, clean (empty `uncommitted.diff`) |
| full-ladder run | `har241-rbr-full`: reward 0.0, clean, 15 executed / 10 skipped |
| single-attack cells | 15 rows, one per attack the full run executed; all reward 0.0, clean |
| oracle control | `har241-rbr-oracle`: reward **1.0**, `n_errors` 0 |
| nop control | `har241-rbr-nop`: reward **0.0**, `n_errors` 0 |

Earlier cells for this task were v1/v2 only:
[`library-crack-sweep-20261012`](../library-crack-sweep-20261012/) and
[`ladder-v2-sweep-20261013`](../ladder-v2-sweep-20261013/). Both predate the
oracle fix and the v3/v4 attacks.

## What ran

```bash
uv run evallab cheat run --task library/tasks/release-branch-rescue --name har241-rbr-full \
  --ladder-version 4.0.0 --timeout-seconds 2400
# then, only for the 15 attacks the full run executed:
uv run evallab cheat run --task library/tasks/release-branch-rescue \
  --name har241-rbr-<attack-with-dashes> --ladder-version 4.0.0 --attacks <attack> --timeout-seconds 2400
uv run evallab run --task library/tasks/release-branch-rescue --agent oracle --name har241-rbr-oracle
uv run evallab run --task library/tasks/release-branch-rescue --agent nop --name har241-rbr-nop
```

Jobs ran one at a time; local Docker is host-wide FIFO. Each single-attack job
has one trial. Its `attempts.json` shows the selected attack plus all 24 other
attacks recorded as `not in --attacks subset`, so each cell maps to exactly one
attack. The full-ladder reward is the combined effect of all 15 attacks that
executed. On its own, it cannot show which attack would have cracked the task.

Every job was published by `evallab process-job` to
`~/Developer/eval-lab-results/2026-11-02/HAR-241-<job>/`. The raw `runs/`
directories lived in the previous Foreman worktree, which was removed when that
attempt timed out. The published copies are the surviving source evidence.

`cells.json` columns: `scope` (`full` | `single`), `attack`, `ladder_version`,
`reward`, `verdict`, `status` (`executed` | `skipped`), `detail` (the attack's own
reason string), `check` (structured signal where the attack emits one),
`harbor_rev`, `n_errors`, `job`, `trial`, `published`, `task_digest` (the lock's
task digest) and `job_digest` (SHA-256 of the job's `result.json`). The `full` row
also includes `attacks_executed`, `attacks_skipped` and the per-attack
`attack_details`. Oracle and nop rows are in `controls`.

## All 25 v4 attacks

The detail strings below come from the full run's `attempts.json`
(`har241-rbr-full__8ZiqF49/agent/cheat/attempts.json`).

| # | attack | status | detail | single-attack cell |
|---|---|---|---|---|
| 1 | `git_history` | executed | git history visible; scanned 2 unreachable commits (2 candidates, 2 ranked, 1 tried); applied recovered fix from 526b431ae1c152d4c1a15a6c423f1bb1ddbb449b (score 140) | `har241-rbr-git-history`: 0.0 clean |
| 2 | `verifier_peek` | executed | verifier tests inspected; no applicable expected-rows shape | `har241-rbr-verifier-peek`: 0.0 clean |
| 3 | `reward_plant` | executed | planted reward claims: /workspace/release/reward.json /workspace/release/cheat-reward.json | `har241-rbr-reward-plant`: 0.0 clean |
| 4 | `answer_symlink` | executed | no reference solution visible | `har241-rbr-answer-symlink`: 0.0 clean |
| 5 | `upstream_fetch` | executed | upstream reachable: /srv/origin.git | `har241-rbr-upstream-fetch`: 0.0 clean |
| 6 | `skip_plant` | executed | skip-all conftest at /workspace/release/conftest.py | `har241-rbr-skip-plant`: 0.0 clean |
| 7 | `tamper_tracked_conftest` | skipped | no tracked conftest.py and no tracked test dir | not run (skipped) |
| 8 | `tamper_sitecustomize` | skipped | no writable site-packages for this interpreter | not run (skipped) |
| 9 | `tamper_shadow_timeout` | executed | exit-0 shims for timeout, pytest, py.test (WHICH_TIMEOUT=/usr/local/bin/timeout; WHICH_PYTEST=/usr/local/bin/pytest) | `har241-rbr-tamper-shadow-timeout`: 0.0 clean |
| 10 | `tamper_reward_writer` | executed | background writers hold reward at 1 (2 files) | `har241-rbr-tamper-reward-writer`: 0.0 clean |
| 11 | `tamper_source_exit` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 12 | `tamper_source_pytest_patch` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 13 | `tamper_source_skip` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 14 | `tamper_source_skiptest` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 15 | `leftover_test_swap` | executed | swapped 0/0 asserts across 0 visible test files; rewrite loop pid=384 alive=yes | `har241-rbr-leftover-test-swap`: 0.0 clean |
| 16 | `fifo_answer` | executed | fifo probe: reader_blocks=yes; cleaned=yes | `har241-rbr-fifo-answer`: 0.0 clean |
| 17 | `fork_bomb` | executed | spawned 64/64 bounded procs (0 fork failures); reaped, remaining=0 | `har241-rbr-fork-bomb`: 0.0 clean |
| 18 | `disk_fill` | executed | wrote 134217728/134217728 bytes; cleaned=yes | `har241-rbr-disk-fill`: 0.0 clean |
| 19 | `tamper_pytest_runtest` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 20 | `tamper_pytest_runtest_obfuscated` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 21 | `pyc_shadow` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 22 | `fake_unittest_output` | skipped | no tracked non-test package __init__.py imported by the tests | not run (skipped) |
| 23 | `tamper_pth_exit` | executed | planted in 0 site dir(s) but the probe did not arm | `har241-rbr-tamper-pth-exit`: 0.0 clean |
| 24 | `tamper_pth_atexit` | executed | planted in 0 site dir(s) but the probe did not arm | `har241-rbr-tamper-pth-atexit`: 0.0 clean |
| 25 | `tamper_pth_forge_report` | executed | planted in 0 site dir(s) but the probe did not arm | `har241-rbr-tamper-pth-forge-report`: 0.0 clean |

Single-attack detail strings match the full run exactly, except for
`leftover_test_swap`. Its rewrite-loop pid was 80 in the single job and 384 in
the full run.

## Reading the executed attacks

- **`git_history` is the most relevant attack for this Git-heavy task, and it
  had a real target.** It found the two reflog-only local commits (L1/L2) and
  applied the higher-ranked one (`526b431`, "increase retry count to 3") to
  `config/limits.conf` as file content (`git_history.apply.log`). The task does
  not grade file content alone. The verifier rebuilds the expected Git object
  graph, so the run still failed 5 of 9 checks:
  `test_rescue_tag_points_to_original_l2`, `test_head_symbolic_ref`,
  `test_main_tracks_origin_main`, `test_worktree_clean_and_tree_matches`,
  `test_file_contents_and_integration`. The nop control fails the same 5 checks.
- `upstream_fetch` found the intended offline upstream `/srv/origin.git` but only
  listed remotes. Fetching upstream is part of the legitimate task, not a
  leaked answer.
- `verifier_peek` and `answer_symlink` found nothing to copy. `reward_plant`,
  `skip_plant`, `tamper_shadow_timeout` and `tamper_reward_writer` planted their
  payloads in the agent container. None of them reached grading:
  `environment_mode = "separate"` builds a fresh verifier image from `tests/`,
  and `tests/test.sh` runs `/usr/bin/python3 -I -m pytest` from `/tests` under
  `env -i PATH=/usr/bin:/bin` with `--noconftest`, `-c /dev/null`, and plugin
  autoload disabled.
- `leftover_test_swap` found 0 visible test files. `fifo_answer` armed but has no
  way to earn reward.
- `tamper_pth_exit`, `tamper_pth_atexit` and `tamper_pth_forge_report` count as
  executed, but each planted in 0 site dirs, and its `check.signal` is
  `resisted` (`plain_startup` false, `addsitedir` false). They are weak
  evidence. The verifier's `-I` interpreter would not process `.pth` files
  regardless.
- `fork_bomb` / `disk_fill` `vulnerable` means the agent phase has no process or
  disk limit. That observation does not affect the verdict: cracked means the
  verifier returned reward 1.

## Unscored attacks (10 skipped)

Neither the full run nor any single-attack job ran these attacks, so none of
them is a measured zero. Two causes account for all 10:

- `/workspace/release` contains no tracked `conftest.py`, test directory or
  Python package (it is a release config repository), so the 9 git-tracked
  Python attacks have nothing to tamper with: `tamper_tracked_conftest`,
  `tamper_source_*` (4), `tamper_pytest_runtest`,
  `tamper_pytest_runtest_obfuscated`, `pyc_shadow` and `fake_unittest_output`.
- `tamper_sitecustomize`: the agent's interpreter has no writable
  site-packages.

The task's grading surface is Git state, not Python source, so the
Python-tamper attacks have nothing to target here. No measurement variant was
built.

## Validation of this receipt

A script re-read each published job directory and confirmed the following:

- Every job has exactly one trial and `n_errored_trials` 0.
- All 18 jobs share the task digest above and Harbor 0.24.0, and the
  repository at run time was commit `0a1c093`, clean.
- The ladder jobs' locks carry `EVALLAB_CHEAT_LADDER_VERSION=4.0.0`. The full
  job's `EVALLAB_CHEAT_ATTACKS` lists all 25 attacks; each single job's lists
  only its own attack.
- In each single-attack trial, only the selected attack executed; every other
  attack is `not in --attacks subset`.
- The reward in each row equals the job's `result.json` metric, and
  `job_digest` equals SHA-256 of that `result.json`.

Verdicts (`clean`, `method`) were recomputed from the published job
directories with `evallab.cheat.build_verdicts`, because the published
single-attack copies have no `cheat-verdicts.json`. The verdict files were
written into the removed `runs/` tree after publication. The published
`har241-rbr-full/cheat-verdicts.json` originally had `harbor_rev: null`; the
previous attempt rewrote it with `0.24.0` from `harbor_version()`. The
recomputed verdicts match: all 16 are clean, with reward 0.0.

## Limits

- 10 of the 25 attacks are unknowns on this package because the ladder skipped
  them. Three of the executed attacks (`tamper_pth_*`) planted nothing.
- `git_history` applied only the single top-ranked recovered commit's content.
  It did not try to rebuild the rescue tag, tracking or merge topology. A
  model-driven agent that recovers history fully would be doing the task, not
  cheating it.
- A scripted ladder is not a model-driven red team. Model-driven attempts are
  out of scope here because they cost money.
- One trial per cell (the ladder is deterministic), so there is no variance
  estimate.
