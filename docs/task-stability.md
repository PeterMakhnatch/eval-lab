---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Verifier stability (HAR-83 §4.2.1)

Repeat-verification audit for MiMo-V2.6 Harbor tasks: after the agent
finishes, the task's own verifier runs **k times on the same final container
state**, and every run is recorded. The trial reward is always the FIRST
run's reward, so the audit never changes the result it measures.

## Opt-in command (HAR-81 rollouts)

One nop control with 3-fold repeat verification per task (all $0, local
Docker). Sources stay read-only: each task is staged under
`<jobs-dir>/.stage/` before Harbor sees it.

```bash
uv run evallab tasks stability-run \
  --tasks /private/tmp/mimo-data/terminal/tasks/<task-id> [...] \
  --job-prefix har83-stab
uv run evallab tasks stability-collect \
  --job-name har83-stab-<task-id> [...] \
  --output derived/parquet/external/task_catalog/task_stability.parquet
```

Raw Harbor equivalent (what `stability-run` builds per task):

```bash
harbor run --path <staged-task> --agent nop \
  --job-name <job> --jobs-dir runs/ \
  --n-concurrent 1 --n-attempts 1 \
  --verifier evallab.harbor_repeat_verifier:RepeatVerifier \
  --verifier-kwarg repeat_n=3 -y
```

`PYTHONPATH` must include Eval Lab's `src/` so the Harbor controller can
import the verifier (`stability-run` sets this automatically).

## Cost impact

k× verifier wall-time per trial. All script-graded MiMo domains
(code/cyber/terminal/music) grade for **$0**, so repeat verification is free
apart from local Docker time. The judge-graded domains (general = LLM judge,
webdev = VLM judge) would cost k× judge calls per trial and are excluded
from $0 sampling.

## What each run records (`verifier/stability.json`)

Per run: `reward` (or null when no reward file was written — a testbed
failure, never a 0), `error`, `duration_sec`, `exit_code` (test script's
return code, observed from the verifier's exec calls), and
`stdout_tail_sha256` (sha256 of the last 8 KiB of `test-stdout.txt`).

## Verdicts

- `stable` — all runs produced a reward and all rewards are equal.
- `flipped` — all runs produced a reward but rewards differ.
- `errored` — any run lacks a reward (missing evidence counts as errored).

## Per-domain rerun validity (Harbor 0.21, MiMo-V2.6-RL)

Reruns execute in the same container with no reset between them:

- **code** — `tests/test.sh` resets the listed test files to the base commit
  and re-applies `/tests/test.patch` on every run, and re-saves
  `verifier/agent.diff`. Reruns are self-resetting: valid.
- **terminal** — `tests/test.sh` does NOT reset `/app`; it re-runs pytest on
  the current state. Reruns are valid as an audit, but a flip can mean the
  verifier itself mutates state (e.g. a first run's replay creates files the
  second run then sees) rather than nondeterministic grading.
- **cyber** — `tests/verify.py` rebuilds a fresh `verify/run/` dir per run
  (rmtree + copy of the binary and PoC) and cleans up after itself. Reruns
  are self-cleaning: valid.
- **music** — `tests/grade.py` scores the composed file read-only (abcmidi is
  installed once). Reruns are valid.

## First sample (2026-09-28, Harbor 0.21.0, local Docker, nop agent)

`task_stability.parquet` holds 11 rows from `har83-stab*` jobs:

| task | method | rewards | verdict |
|---|---|---|---|
| candidate-0036-software-data-engineering | repeat_verifier | [0,0,0] | stable |
| candidate-0109-science-robotics | repeat_verifier | [0,0,0] | stable |
| candidate-0260-security-appsec | repeat_verifier | [0,0,0] | stable |
| candidate-0308-security-forensics | repeat_verifier | [0,0,0] | stable |
| arvo_10055 | repeat_verifier | [0,0,0] | stable |
| arvo_10096 | repeat_verifier | [0,0,0] | stable |
| music-gk-0000 (x2 attempts) | nop_repeat | [null] | errored |
| music-gk-0001 (x2 attempts) | nop_repeat | [null] | errored |
| candidate-0036 (har83-stab3 re-run, fixed verifier) | repeat_verifier | [0,0,0] | stable |

Notes: terminal stdout tails differ across runs while rewards agree
(timing lines); cyber stdout is byte-identical. The `har83-stab3` re-run
uses the fixed verifier: the trial's `verifier/` top level holds run 0's
`reward.txt`/`test-stdout.txt`/`ctrf.json` plus `repeat/{0,1,2}/` and
`stability.json` (top reward == run 0 reward, top stdout byte-identical
to `repeat/0`). Music never reached the verifier: Harbor's healthcheck
fails with rc=127 (missing `/app` workdir vs `exec -w`; see Known issues),
so both music attempts are infra `errored`, not signal. Code was skipped
(2.7 GB image, long test commands) per the $0 sampling plan.

## `diff_replay` recipe (offline reruns, no container retained)

Implemented only as a documented recipe (see `task_stability.py`
`STATE_PRESERVATION["diff_replay"]` for the recorded string):

1. `docker run` the task's pinned image (amd64).
2. Replay the one-time setup from the task's `[environment.healthcheck]`
   command (unpacks `/var/lib/mimo`, writes `/var/lib/mimo/base`).
3. For code tasks: restore the agent's end state with the trial's saved
   `verifier/agent.diff` (`git apply` in the workdir), then run
   `tests/test.sh` k times and record each `reward.txt`.
4. Collect with `stability-collect --method diff_replay`.

## Known issues

### Music tasks fail Harbor setup with rc=127 (all music tasks likely affected)

Music tasks declare `workdir = "/app"` but their base image
(`docker.io/library/python@…`, multi-arch) ships **without** `/app`.
Harbor 0.21's Docker backend runs `[environment.healthcheck].command` as
`docker compose exec -w /app … bash -c '…'`, and `exec -w` into a missing
directory fails with **rc=127** — before `setup.sh` (which would create
`/app`) ever runs. Chicken-and-egg: terminal/cyber MiMo images bake their
workdirs in, so only music is affected, but every music task shares this
shape, so presumably all ~1,000 music tasks are unrunnable under Harbor
0.21 local Docker until the adapter creates `/app` (e.g. `Dockerfile`
`WORKDIR`/`mkdir`) or Harbor tolerates a missing exec workdir.

Evidence (2026-09-28, Harbor 0.21.0, arm64 host): `music-gk-0000` and
`music-gk-0001` healthchecks failed with rc=127 twice each, while the byte-
identical healthcheck command run manually in the same image (`docker exec
-w /app … bash -c '…'`) succeeds with rc=0, and `docker exec -w` into a
missing directory reproduces rc=127 exactly. The task bytes and image are
exonerated; the failure is in Harbor's exec invocation vs the image layout.
