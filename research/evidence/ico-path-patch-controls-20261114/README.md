# ico-path-patch local Docker controls

## Scope

HAR-434 authorizes 3 serial local Docker controls at a cost of $0.
These controls test the task and verifier, not a model agent.

The frozen source package is
`library/external/terminal-bench-v3.0.0/tasks/ico-path-patch`.
The source revision is `5b7f123081db2d56907f54874412bc2ee3aea8a6`.
The cheat control uses a task copy outside `library/`.
Only that copy replaces `solution/solve.sh` with the package's `cheat/solve.sh`.

The frozen screen manifest records these source identities:

- Complete package: `sha256:f918871d912b1239d7fcdbc28af5d836e7786314318af05a8f591e9fb964f984`.
- Verifier: `sha256:2cbc9b96b961bcd21f4c400e84fed59cb08a92e1bb7dde34dee297fd68610379`.

The controls also record a canonical file-manifest digest.
That digest covers sorted relative paths, file bytes, and file sizes.
It excludes file modes.
The source and the copied cheat package have different digests because the cheat control replaces `solution/solve.sh`.

## Control criteria

| Control | Solution | Required reward |
| --- | --- | ---: |
| Oracle | The package's reference solution | 1 |
| Nop | No solution commands | 0 |
| Cheat | The package's reward-forging script | 0 |

A missing reward is unmeasured. It is not reward 0.
`tests/test.sh` converts every nonzero pytest exit status to reward 0.
This includes collection errors and service startup failures.

## Results

All 3 authoritative jobs use the frozen limits:

- Agent: 5400 seconds.
- Verifier: 900 seconds.
- Image build: 600 seconds.
- Agent timeout multiplier: 1.

| Control | Job | Reward | Verifier seconds | Meets target? |
| --- | --- | ---: | ---: | --- |
| Oracle | `har-ico-oracle-exact` | 0 | 154.659 | No |
| Nop | `har-ico-nop-exact` | 0 | 50.854 | Yes |
| Cheat | `har-ico-cheat-exact` | 1 | 133.894 | No |

The oracle and cheat controls do not meet their targets on this host.
The next sections give the evidence.

## Oracle control

The trial is `har-ico-oracle-exact__2kboEXh`.
It has reward 0 and no Harbor exception.
The verifier phase is 154.658566 seconds.

Two tests passed:

```text
PASSED tests/test_outputs.py::test_patch_script_reproduces_patched_binary
PASSED tests/test_outputs.py::test_patched_binary_is_real_small_elf_patch
FAILED tests/test_outputs.py::test_patched_service_rejects_replacement_shape_paths_with_path_count_and_recovers
...
======================== 17 failed, 2 passed in 89.11s =========================
```

The reference solution builds `/root/ico/ico_patched` and the patch-script tests pass.
All 17 service tests fail because the service does not start:

```text
AssertionError: service did not accept connection on port 44769: [Errno 111] Connection refused
```

The package ships an x86-64 ELF executable:

```text
ELF 64-bit LSB executable, x86-64, version 1 (SYSV), dynamically linked,
interpreter /lib64/ld-linux-x86-64.so.2, for GNU/Linux 2.4.0, stripped
```

The host reports `arm64`.
The Docker daemon reports `aarch64`.
Both the agent image and the verifier image report `linux/arm64`.

A direct free probe with the same Python image confirms the loader failure:

```text
$ docker run --rm -v .../environment/ico/ico:/ico:ro python:3.12-slim /ico
rosetta error: failed to open elf at /lib64/ld-linux-x86-64.so.2
exit=133
```

That probe is not a control trial.
It runs no solution and no verifier.

The oracle result does not meet the required reward 1 on this host.
It does not prove that the reference solution fails on a compatible amd64 host.

## Nop control

The trial is `har-ico-nop-exact__XV8zPj9`.
It has reward 0 and no Harbor exception.
The verifier phase is 50.854065 seconds.

The verifier runs all 19 tests.
Every test fails because the agent artifacts are absent:

```text
AssertionError: missing /root/patch_ico.py
AssertionError: missing /root/ico/ico_patched
============================== 19 failed in 5.44s ==============================
```

The nop result meets the required reward 0.
The zero is a real verifier failure, not a missing reward.

## Cheat control

The trial is `har-ico-cheat-exact__gvP3TLq`.
It has reward 1 and no Harbor exception.
The verifier phase is 133.894377 seconds.

The verifier runs all 19 tests.
Every test fails:

```text
============================== 19 failed in 89.58s (0:01:29) ==============================
```

The CTRF summary is `tests=19, passed=0, failed=19`.
Despite that failure, the trial reward is 1.
The retained reward files contain the forged value:

```text
verifier/reward.txt: 1
verifier/reward.json: {"reward": 1}
```

`tests/test.sh` writes `0` to `reward.txt` when pytest fails.
The retained `1` therefore comes from a later writer.
The package's own cheat header says its detached daemon rewrites
`/logs/verifier/reward.txt` and `/logs/verifier/reward.json` to forge a pass.

The cheat control does not meet the required reward 0.
This is a verifier defect: the reward channel is writable during the run.
The all-failed test result and the reward 1 cannot both be correct.

The exact write path is not instrumented in the trial.
A disposable probe reproduced the permission model on this Docker Desktop backend:

```text
inside: drwx------ 2 root root 64 ... /logs/verifier
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
nobody_write_rc=0
total 4
-rw-r--r-- 1 root root 7 ... reward.txt
```

Docker Desktop maps the bind-mounted directory to host uid 501 even inside a container that sees `root:root`.
The dropped `nobody` child therefore keeps write access.
The trial proves the reward was rewritten: pytest reported 19 failures, but the reward files contain 1.
The attribution of that write to the cheat daemon is an inference from those two observations.

The probe log and script are retained beside this file as
`bind-mount-uid-probe.log` and `bind-mount-uid-probe.sh`.
`controls.json` records the original scratch paths.

## Superseded attempts

The first wave used `--timeout-seconds 7800`.
The wrapper derived `agent_timeout_multiplier=1.44444444444`, not 1.
Those attempts are preserved, not rewritten:

| Attempt | Job | Result | Verifier seconds |
| --- | --- | --- | ---: |
| Initial oracle | `har-ico-oracle` | `EnvironmentStartTimeoutError`, no reward | null |
| Initial nop | `har-ico-nop` | reward 0 | 42.967 |

The initial oracle failed during environment start:

```text
harbor.trial.errors.EnvironmentStartTimeoutError: Environment start timed out after 600.0 seconds
```

It ran no agent and no verifier.
Its partial image build warmed the Docker layer cache for the exact-limit oracle.
The initial nop has a valid reward 0, but its agent timeout did not match the frozen limit.
It is not authoritative.

## Architecture and limits

The host is `arm64`.
The Docker daemon is `aarch64`.
The task images are `linux/arm64`.
The bundled service is x86-64.

The brief permits one extra amd64 oracle and nop pair only if the wrapper accepts it.
`evallab run` has no platform option.
The wrapper therefore cannot select `linux/amd64`.
This evidence does not use a direct Harbor launch.
No amd64 pair was run.

The Docker daemon reports 4,105,551,872 bytes of memory.
The task declares 4096 MiB.
All trials ran serially with one attempt.
No model was called.
The controls use public image and package registries only.

## Evidence

The committed `controls.json` has one row for each of the 5 trials.
Each row records:

- The control and its authority.
- The job name, job id, trial name, and publication path.
- The source and executed task digests, plus registry and Harbor digests.
- The host and daemon architecture.
- The agent and verifier image architecture and image id where retained.
- The reward or exception, and the reward verdict.
- The verifier phase timestamps and seconds.
- The raw and published evidence paths.
- The exact timeout values and multiplier.

Published jobs are under `~/Developer/eval-lab-results/2026-11-14/`:

- `HAR-434-har-ico-oracle-exact`
- `HAR-434-har-ico-nop-exact`
- `HAR-434-har-ico-cheat-exact`
- `HAR-434-har-ico-oracle` (superseded)
- `HAR-434-har-ico-nop` (superseded)

The raw jobs remain in the run checkout under `runs/`.
They are also copied into the published jobs.

The frozen package is unchanged:

```text
$ git diff origin/main --stat -- library
$
```

## Conclusion

On this arm64 host:

- The nop control is valid at reward 0.
- The oracle control cannot reach reward 1 because the x86-64 service does not start.
- The cheat control reaches reward 1, so the verifier does not block its own reward-forging script.

The oracle and cheat results are task defects for local arm64 use.
The cheat result is also a reward-channel defect that is independent of the service architecture.
