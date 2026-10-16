---
status: living
audience:
  - builder
  - operator
---

# Rollout engine (`evallab rollout`)

Batch checkpoint evaluation: many Harbor trials at once in Modal
sandboxes for one model selector, reporting tasks/hour and cost per
task. Built for the training loop — the Trainer hands over a checkpoint
selector and held-out task packages; this runs the same runner with the
same settings for every arm, changing only the selector.

## Usage

```bash
keys run -- uv run --no-sync evallab rollout run \
  --tasks-file heldout-tasks.txt \
  --model 'tinker/Qwen/Qwen3.5-9B@tinker://<run>:train:0/sampler_weights/40' \
  --concurrency 16 \
  --cost-limit-usd 0.50 \
  --actor "research-harbor as Peter's delegate (rollout cap \$5, steer 2026-10-16)" \
  --out runs/rollout/arm-b-step40
```

- `--task DIR` (repeatable), `--tasks-file`, or `--tasks-dir` select the
  task packages. Tasks must live under the repo root you run from (the
  queue resolves repo-relative paths); trial jobs land in `runs/<name>`.
- `--model` is a Tinker base selector (`tinker/<base>`) or a trained
  checkpoint (`tinker/<base>@tinker://<run>:train:<i>/sampler_weights/<step>`).
- `--actor` is required and never defaulted: billable agents run only
  through the standing-policy queue, so the runner submits one
  ExperimentSpec per task, records this approval in `queue/events.jsonl`,
  and drains the queue with `tick`. The approval is the spend consent;
  `--cost-limit-usd` caps each trial and `--est-cost-usd` is the
  per-trial estimate the queue admits against.

## Outputs (`--out` dir)

- `results.jsonl`: one row per task — reward, pass, wall and sandbox
  seconds, input/output tokens, settled model cost, Modal sandbox cost
  estimate, job dir, error.
- `summary.json`: trials, completed, errors, pass rate, batch wall,
  tasks/hour, total cost, cost per task.
- `spend.jsonl`: one row tagged `2026-10-16` for the day ledger.

## Cost model

Model tokens settle through the proxy ledger at the pinned Tinker
prices recorded in `lab-metadata.json`. The Modal sandbox is an
estimate: Harbor trial seconds at Modal list prices (CPU
$0.0000131/core/s, memory $0.00000222/GiB/s) with the task's declared
`[environment]` resources. Rows keep the two apart; the summary adds
them. Modal's billed total also lands in the day ledger under the
default Harbor app.

## Throughput

Sandbox startup plus grading for a small task is ~30 s on Modal
(measured 2026-10-16, oracle baseline: 6/6 trials at reward 1.0 across
three first-party tasks, all concurrent). Throughput is task-bound: at
concurrency C, tasks/hour ≈ C / mean trial hours, so long agent trials
want C in the teens. Local Docker is not used.
