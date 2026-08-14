# Multi-step tasks

## What it is

A multi-step task replaces the single root `instruction.md` / `tests/` /
`solution/` with `[[steps]]` in `task.toml` and a `steps/<name>/` directory
per step. One environment is shared; each step has its own instruction,
oracle, and verifier. `min_reward` aborts later steps. Per-step artifacts
land under `steps/<name>/`. `harbor init --task --steps N` scaffolds this.

The lab's only task (`event-summary`) is single-step.

## Demo

```bash
bash explorations/harbor-021/demos/run-multistep.sh
```

Tiny task `demos/tasks/two-step-echo`:

1. `write-name` — write `/app/name.txt` = `Harbor` (`min_reward = 1.0`)
2. `greet` — read that name, write `/app/greeting.txt` = `Hello, Harbor!`

```bash
harbor run --path explorations/harbor-021/demos/tasks/two-step-echo \
  --agent oracle --jobs-dir runs --job-name multistep-oracle-demo -n 1
```

Observed (2026-08-13): Mean **1.000**, 6s, no exceptions. Trial
`two-step-echo__GYAUsRT` `step_results`:

| step | verifier reward |
|---|---|
| write-name | 1.0 |
| greet | 1.0 |

Rolled-up `verifier_result.rewards.reward = 1.0`. Per-step artifacts:
`steps/write-name/artifacts/app/name.txt`,
`steps/greet/artifacts/app/greeting.txt`. Transcript:
`captures/multistep/demo.log`.

## Verdict

**Adopt into brief 11 (migration) when a migrated task is actually
sequential** (belief-revision / judged-output style). Not required for
event-summary or the first 07 canary pin. Do not invent multi-step canaries
just to use the feature. After 11, the executor (05) already runs whatever
`task.toml` declares — no extra executor work.
