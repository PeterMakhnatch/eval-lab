# embedding-drift-monitor local Docker controls

## Scope

HAR-438 authorizes 3 authoritative local Docker controls at a cost of $0.
These controls test the task and verifier, not a model agent.

The frozen source package is
`library/external/terminal-bench-v3.0.0/tasks/embedding-drift-monitor`.
The source revision is `decb2dbc5f1406d0bb3e5fec41bbf9e21cdefc84`.
The cheat controls use task copies outside `library/`.

The source identity is:

- Complete package: `sha256:a13b90206b96b81af3bc012b0396b86193838529b3c804fc143ea3cf68fb26b6`.
- Files: 39.
- Digest kind: `sha256-of-canonical-json-file-manifest-v1`.
  It covers sorted relative paths, file bytes, and file sizes.
  It excludes file modes.

Each control ran serially, with `--concurrency 1` and `--attempts 1`.
The command was `uv run evallab run` without `--timeout-seconds`.
The package's declared timeouts therefore apply:

- Agent: 7200 seconds.
- Verifier: 300 seconds.
- Image build: 600 seconds.
- Observed `agent_timeout_multiplier`: 1.

That last point required a source fix in this branch.
Before the fix, an omitted `--timeout-seconds` used 1800 seconds.
That value produced `agent_timeout_multiplier` 0.25 for this package.
The fix reads the task's declared `[agent] timeout_sec` when the CLI option is absent.
An explicit option keeps its earlier meaning.
See `src/evallab/execution_parser.py` and
`tests/test_execution_contracts.py::test_run_cli_passes_native_or_explicit_agent_timeout_to_executor`.

## Control criteria

| Control | Solution | Required reward |
| --- | --- | ---: |
| Oracle | The package's reference solution | 1 |
| Nop | No solution commands | 0 |
| Cheat | The package's reward-forging script | 0 |

A missing reward is unmeasured. It is not reward 0.
`tests/test.sh` writes reward 0 on every nonzero pytest exit.
It writes reward 1 only when pytest passes and the CTRF file has at least 11
passed tests and no failed test.

## Results

| Control | Job | Reward | CTRF passed/failed | Verifier seconds | Authoritative? |
| --- | --- | ---: | --- | ---: | --- |
| Oracle | `har438-embedding-oracle` | 1 | 11 / 0 | 109.479 | Yes |
| Nop | `har438-embedding-nop` | 0 | 3 / 8 | 140.585 | Yes |
| Cheat, initial copy | `har438-embedding-cheat` | 0 | 8 / 3 | 22.615 | No |
| Cheat, full payload | `har438-embedding-cheat-retry` | 0 | 0 / 11 | 16.477 | Yes |

The initial cheat job is preserved and not rewritten.
Its task copy replaced only `solution/solve.sh`.
`cheat/solve.sh` installs `$D/drift_monitor`, where `$D` is the solution directory.
The copy therefore installed the original six solution modules, not the poisoned
package.
The published artifact confirms it: `app/drift_monitor/` holds six modules and
no `__init__.py`.
That attempt measured an incomplete solution, not the cheat control.
It is the one permitted cheat retry, and it used a fresh task copy.

## Oracle control

The trial is `har438-embedding-oracle__E4Midpu`.
It has reward 1 and no Harbor exception.
The verifier phase is 109.479257 seconds.

```text
PASSED ../tests/test_outputs.py::test_l2_normalize_handles_zero_vectors
...
PASSED ../tests/test_outputs.py::test_pairwise_cosine_matches_reference_values
======================== 11 passed in 72.59s (0:01:12) =========================
CTRF verification: 11/11 passed - OK
```

The CTRF summary is `tests=11, passed=11, failed=0`.
The reward file contains `1`.
The oracle result meets the required reward 1 on this host.

## Nop control

The trial is `har438-embedding-nop__rGvpwaV`.
It has reward 0 and no Harbor exception.
The verifier phase is 140.584749 seconds.

The verifier runs all 11 tests.
8 tests fail and 3 pass:

```text
FFFF.FF.F.F                                                              [100%]
=================== 8 failed, 3 passed in 117.10s (0:01:57) ====================
```

The passing tests are limited to cases that do not require a repaired module.
The CTRF summary is `tests=11, passed=3, failed=8`.
The reward file contains `0`.
The nop result meets the required reward 0.
The zero is a real verifier failure, not a missing reward.

## Cheat control and reward consistency

The authoritative cheat trial is `har438-embedding-cheat-retry__Menu4KH`.
It has reward 0 and no Harbor exception.
The verifier phase is 16.476678 seconds.

The task copy replaces `solution/` with the complete package `cheat/` directory.
It contains `solve.sh` and all 9 poisoned `drift_monitor` files.
The published artifact confirms the poison landed:
`app/drift_monitor/` contains `__init__.py` and all 8 sibling modules.

The verifier runs all 11 tests.
Every test fails:

```text
FFFFFFFFFFF                                                              [100%]
============================== 11 failed in 0.17s ==============================
```

Every failure message is `privilege-dropped worker did not report success`.
That is consistent with the poisoned `__init__.py` calling `os._exit(0)` in
each privilege-dropped test child.
The CTRF summary is `tests=11, passed=0, failed=11`.

**The cheat reward matches the CTRF result.**
The captured trial reward is 0, and CTRF reports 11 failures.
The required cheat reward is 0, so this control meets its target.

**However, the retained reward file is forged.**
`verifier/reward.txt` contains `1`, while `result.json` contains reward 0 and
CTRF reports 11 failures.
There is no `verifier/reward.json` in this trial.
`tests/test.sh` writes `0` before pytest and on a failed pytest status.
The retained `1` therefore comes from a later writer.
The package's cheat `__init__.py` starts a detached daemon that rewrites
`/logs/verifier/reward.txt` to `1` every 0.05 seconds.
The observed final file state is 1.

The captured reward and the retained file disagree.
Harbor captured reward 0.0; the retained file says 1.
The exact read time of Harbor's reward capture is not instrumented.
The write attribution to the cheat daemon is an inference from these
observations, not a traced write.

A disposable probe reproduced the permission model on this Docker Desktop
backend:

```text
/logs/verifier mode=drwx------ numeric_mode=700 uid=0 gid=0
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
nobody_write_rc=0
contents after nobody write: forged
host directory: mode=drwx------ uid=501 gid=0
```

Docker Desktop maps the bind-mounted directory to host uid 501 even inside a
container that sees `root:root`.
A dropped `nobody` child therefore keeps write access.
The probe is not a control trial.
Its script and log are retained beside this file.

Result for the card's third condition:
the cheat reward matches CTRF, but `verifier/reward.txt` holds a forged `1`.
The forged value is in the trial's `verifier/reward.txt`; it is copied under
the published job's trial directory.

## Architecture and limits

The host is `arm64`.
The Docker daemon is `aarch64`.
The daemon is Docker Desktop 29.8.0 on linuxkit 7.0.12.
It reports 16 CPUs and 4,105,551,872 bytes of memory.
The task declares 1 CPU, 2048 MB of memory, and 10240 MB of storage.
The task declares no GPU.

All 4 trials ran serially with one attempt.
No model was called.
The controls use public image and package registries only.

## Evidence

`controls.json` holds one row for each measured trial.
Each row records:

- The control and its authority.
- The job name, trial name, and publication path.
- The source and executed task digests.
- The host and daemon architecture.
- The raw reward, CTRF counts, and reward/count agreement.
- `reward_file_forges_trial_reward` and `forged_value` for the cheat retry.
- The verifier phase timestamps and seconds.
- The exact timeout values and multiplier.
- The command, publication, and raw evidence paths.

Published jobs are under `~/Developer/eval-lab-results/2026-11-14/`:

- `HAR-438-har438-embedding-oracle`
- `HAR-438-har438-embedding-nop`
- `HAR-438-har438-embedding-cheat` (non-authoritative initial copy)
- `HAR-438-har438-embedding-cheat-retry`

Each published job contains `native-control-evidence/` with the exact command,
the input and host snapshot, the task `task.toml`, and a source task archive.
The raw jobs remain in the run checkout under `runs/`.
They are also copied into the published jobs.

The frozen package is unchanged:

```text
$ git diff origin/main --stat -- library
$
```

## Conclusion

On this arm64 Docker Desktop host:

- The oracle control reaches reward 1 with 11/11 CTRF passes.
- The nop control reaches reward 0 with 3/11 CTRF passes.
- The cheat control reaches reward 0 with 0/11 CTRF passes.
- The cheat reward matches CTRF.
- The cheat daemon still forged `verifier/reward.txt` to 1 on this backend.
  Harbor's captured reward stayed 0 in this trial.

The reward directory is writable by the privilege-dropped child on Docker
Desktop, matching the HAR-434 permission finding.
A trial's captured reward can therefore disagree with its retained reward file.
