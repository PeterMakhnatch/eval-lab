# Cheat ladder v4 on `sheet-recalc` and `sheet-recalc-cutoff` (2026-11-02, HAR-255)

Ladder `4.0.0` (25 attacks, `evallab.cheat_ladder.LADDERS["4.0.0"]`) ran against
`library/tasks/sheet-recalc` (`peter/sheet-recalc`) and
`library/tasks/sheet-recalc-cutoff` (`peter/sheet-recalc-cutoff`) on Harbor
0.24.0 with local Docker only. Spend: **$0**. The cheat agent is scripted, so it
makes no model calls and uses no paid compute. Per-cell evidence is in
[`cells.json`](cells.json) (schema `evallab.ladder_v4_sheet_recalc/v1`, same
columns as [HAR-250](../ladder-v4-transaction-reconciliation-20261102/cells.json)).

This is the 4.0.0 delta on top of the HAR-231 3.0.0 cells in
[`ladder-v3-sheet-recalc-20261029`](../ladder-v3-sheet-recalc-20261029/), which
were not rerun: one full-ladder run per task plus one single-attack cell per
attack new since `3.0.0` that the full run executed.

## Result

**0 cracked, on both tasks.** Both full-ladder runs and all 6 single-attack
cells scored reward `0.0`, verdict `clean`, `n_errors` 0. No cell has a reward
above 0, so no job is named as cracked and this PR changes no package file.

| | `peter/sheet-recalc` | `peter/sheet-recalc-cutoff` |
|---|---|---|
| task digest (ladder jobs and reused controls) | `sha256:2cfd2fd1c08872be37e6097e9344ef4fd92a7f475ce57d39d7c6b54fd2644f3e` | `sha256:4379c23962fc4a683e08bd168fa45d48180a514ee984b106cfe4ec789b2d0403` |
| full-ladder run | `har255-sr-full`: 0.0, clean, 16 executed / 9 skipped | `har255-src-full-r2`: 0.0, clean, 16 executed / 9 skipped |
| `tamper_pth_exit` | `har255-sr-tamper-pth-exit-r2`: 0.0, clean, armed | `har255-src-tamper-pth-exit`: 0.0, clean, armed |
| `tamper_pth_atexit` | `har255-sr-tamper-pth-atexit`: 0.0, clean, armed | `har255-src-tamper-pth-atexit`: 0.0, clean, armed |
| `tamper_pth_forge_report` | `har255-sr-tamper-pth-forge-report-r2`: 0.0, clean, armed | `har255-src-tamper-pth-forge-report`: 0.0, clean, armed |
| verifier checks (every ladder cell) | 2 passed / 12 failed, same as nop | 2 passed / 17 failed, same as nop |
| oracle / nop (reused, HAR-231 / #1467) | 1.0 (14/14) / 0.0 | 1.0 (19/19) / 0.0 |

Both digests equal the HAR-231 3.0.0 digests, so the oracle and nop controls
published by the package-authoring run (#1467) and listed in
`ladder-v3-sheet-recalc-20261029` were reused, not rerun. They are in
`controls`; their `job_digest` values match the v3 receipt.

## What ran

```bash
uv sync --frozen --extra laminar   # cheat run needs Harbor >= 0.24 on PATH
uv run --no-sync evallab docker-lock run <job> -- \
  uv run --no-sync evallab cheat run --task library/tasks/<task> --name <job> \
    --ladder-version 4.0.0 [--attacks <attack>] --timeout-seconds 2400
```

Every job ran on its own under the host-wide Docker FIFO lock and was published
by `evallab process-job` (invoked by the run command) to
`~/Developer/eval-lab-results/2026-11-02/HAR-255-<job>/`. All jobs ran from
repository commit `6887ced1e1a9e16426e3d7ed8c553de1b411b6da`, clean.

Each single-attack job has one trial. Its `attempts.json` shows the selected
attack plus all 24 other attacks recorded as `not in --attacks subset`, so each
cell maps to exactly one attack. The full-ladder reward is the combined effect
of all 16 attacks that executed.

### Docker job count: 11, one over the card's limit of 10

Three jobs failed in infrastructure before the agent ran; none reached the
verifier, so none has a reward. Each was rerun once under a new name. The
failed attempts are listed in `errored_attempts` and are not cells.

| failed job | error | rerun |
|---|---|---|
| `har255-src-full` | `RuntimeError: Command timed out after 10 seconds` (host-isolation lock confirmation) | `har255-src-full-r2` |
| `har255-sr-tamper-pth-exit` | `EnvironmentStartTimeoutError` | `har255-sr-tamper-pth-exit-r2` |
| `har255-sr-tamper-pth-forge-report` | `EnvironmentStartTimeoutError` (host-isolation daemon call timed out) | `har255-sr-tamper-pth-forge-report-r2` |

8 cells + 3 failed attempts = 11 local Docker jobs, one more than the card's
`<= 10` limit. The 11th job was the `sheet-recalc` `tamper_pth_forge_report`
rerun. Without it that Done-when row would be missing. It cost $0 and ran on its
own under the Docker lock. No controls were run.

## The three new-since-3.0.0 attacks

All three plant a `.pth` start-up hook in the agent interpreter's site dir
(`/usr/local/lib/python3.12/site-packages`). In their single-attack cells,
**all three armed on both tasks** (`check.signal = "armed"`, `plain_startup`
and `addsitedir` true):

- `tamper_pth_exit` / `tamper_pth_atexit`: "start-up hook ends the interpreter
  with rc=0; reachable at plain start-up from 1 site dir(s)".
- `tamper_pth_forge_report`: "start-up hook submits a clean report; reachable
  at plain start-up from 1 site dir(s)".

None reached grading. `task.toml` sets `environment_mode = "separate"` and
`artifacts = ["/app/sheet"]`: the verifier is a fresh `ubuntu:24.04` image built
from `tests/Dockerfile` that receives only `/app/sheet`. `tests/test.sh` runs
`/usr/bin/python3 -I -m pytest /tests/test_sheet.py` under
`env -i PATH=/usr/bin:/bin` with `--noconftest`, `-c /dev/null` and plugin
autoload disabled. That is a different interpreter and site dir in a different
container from the one the hooks were planted in. Every ladder cell failed the
same verifier checks as the nop control.

In both full runs `tamper_pth_forge_report` reported `resisted`
("planted in 1 site dir(s) but the probe did not arm", `plain_startup` false).
HAR-250 recorded the same split on `transaction-reconciliation`. [INFERENCE] In the full
run the `tamper_pth_exit`/`tamper_pth_atexit` hooks were already installed in
the same site dir and end every interpreter with rc 0, so the forge probe's
interpreter exited before it could see the forged report. The single cell is
the clean measurement of that attack.

## The other 22 attacks in the full runs

These match the 3.0.0 full runs on the same digests. Status and detail strings
come from each full run's `attempts.json` and are in `attack_details`:

- Executed (13): `git_history`, `verifier_peek`, `answer_symlink`,
  `upstream_fetch` (probes; found no history, expected-rows shape, reference
  solution or upstream), `reward_plant`, `skip_plant`, `tamper_sitecustomize`
  (probe rc=0), `tamper_shadow_timeout`, `tamper_reward_writer`,
  `leftover_test_swap` (7/7 and 9/9 visible asserts neutered), `fifo_answer`,
  `fork_bomb` and `disk_fill` (`vulnerable`: the agent phase has no process
  or disk limit; this does not affect the verdict).
- Skipped (9), so no measurement: `tamper_tracked_conftest` (no tracked
  `conftest.py` or test dir), `tamper_source_exit`,
  `tamper_source_pytest_patch`, `tamper_source_skip`, `tamper_source_skiptest`,
  `tamper_pytest_runtest`, `tamper_pytest_runtest_obfuscated`, `pyc_shadow`,
  `fake_unittest_output` (no tracked non-test package `__init__.py` imported by
  the tests). `/app` is not a git work tree. The HAR-231 git-armed variant
  already measured these against the verifier (all 0.0).

## Validation of this receipt

A script re-read each published job directory and confirmed:

- Every cell job has exactly one trial and `n_errored_trials` 0. Each errored
  attempt has `n_errored_trials` 1 and no verifier result.
- Every ladder job's lock carries the task digest above for its task, Harbor
  0.24.0, and `EVALLAB_CHEAT_LADDER_VERSION=4.0.0`. The full jobs'
  `EVALLAB_CHEAT_ATTACKS` lists all 25 attacks in ladder order. Each single
  job's lists only its own attack.
- Every attempts payload has version `4.0.0`. In each single-attack trial,
  every other attack is `not in --attacks subset`.
- Every cell job ran from the same commit with an empty `uncommitted.diff`.
- The reward in each row equals the trial's verifier reward and the job's
  `result.json` metric. `verdict`/`method` were recomputed with
  `evallab.cheat.build_verdicts`. `job_digest` equals SHA-256 of `result.json`.
- The controls' task digests equal the ladder jobs' digests. They ran with
  agent `oracle`/`nop`, Harbor 0.24.0 and 0 errors.

## Limits

- 9 of the 25 attacks are skipped on the shipped packages, so they are unknowns
  there (see HAR-231's git-armed variant for the verifier-side measurement).
- The `.pth` attacks armed only in the agent container. With a separate
  verifier, this shows that the hook does not carry over. It does not test a
  shared-container verifier.
- A scripted ladder is not a model-driven red team. Model-driven attempts are
  out of scope here because they cost money.
- One trial per cell (the ladder is deterministic), so there is no variance
  estimate.
