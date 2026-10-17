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
- `--model` is one of the harness's admitted Terminus selectors:
  - a Tinker base (`tinker/<base>`) or trained checkpoint
    (`tinker/<base>@tinker://<run>:train:<i>/sampler_weights/<step>`);
  - a pinned OpenRouter route (`openrouter-metered/<model>`);
  - the self-hosted MiMo base or one of its LoRA adapters
    (`selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B[:rds1-a|:rds1-b|:rds1-c]`,
    harness `terminus-2`). The self-hosted route reads the LoRA server
    address from `EVALLAB_MIMO_SELFHOSTED_UPSTREAM`; the runner refuses
    before submitting anything when it is unset. The Trainer deploys one
    server for the base plus all adapters (`EVALLAB_MIMO_LORAS=...`), so
    every arm differs only in the selector suffix.
  The selector is validated before any queue submission, so a typo
  fails in seconds instead of inside a queued trial.
- `--actor` is required and never defaulted: billable agents run only
  through the standing-policy queue, so the runner submits one
  ExperimentSpec per task, records this approval in `queue/events.jsonl`,
  and drains the queue with `tick`. The approval is the spend consent;
  `--cost-limit-usd` caps each trial and `--est-cost-usd` is the
  per-trial estimate the queue admits against.

## Outputs (`--out` dir)

- `results.jsonl`: one row per task/attempt — frozen task package path,
  deterministic name and string job dir, reward, pass, wall and sandbox
  seconds, input/output tokens, settled model cost, Modal sandbox cost
  estimate, and error. Each terminal trial is written immediately after
  dispatcher settlement, even while another trial in the same wave is
  running. The file is replaced atomically; a terminated driver leaves a
  complete snapshot of measured terminal outcomes, not a truncated row.
  A non-done queue state always has an error and null reward/pass, even
  when its job contains an earlier native reward. Once dispatch completes
  cleanly, the verifier reward determines the benchmark outcome, even if
  the agent reported a native exception; this does not claim successful
  agent completion. Without a reward, the native exception explains the
  unmeasured error, or the row reports `missing verifier reward`.
  Benchmark measurement is separate from SFT admission: the latter still
  refuses non-null native exceptions. Native results retain the original
  exception evidence.
- `summary.json`: trials, measured completions, errors, pass rate, batch
  wall, tasks/hour, total cost, cost per completed task. Cost includes
  **all persisted outcomes**, including failed/unmeasured trials; only
  measured completions enter throughput and pass-rate denominators.
  Cost per completed task uses that whole-batch numerator. Both cost
  totals remain null if any outcome lacks model-cost or sandbox-duration
  evidence. Refreshed with each completion; while a batch is active its
  counts cover only persisted terminal rows.
- `spend.jsonl`: one row tagged `2026-10-16` for the day ledger, refreshed
  with the current all-outcome cost, including null when incomplete.

On normal exit the results include every submitted spec, including an
explicit error row for work still nonterminal at the driver's deadline.
After an abrupt driver death, compare the persisted rows against the
authorized spec list: missing specs are **unmeasured**, never passes or
implicitly completed. Native job evidence and queue settlement remain
authoritative; an output-filesystem error cannot reclassify a done trial.

## Cost model

Model tokens and dollars come from the shared strict proxy-ledger reader
over `lab-metadata.json#provider_usage`, not an unvalidated flat usage
summary. It reads the actual v1/v2 call lifecycle and cross-checks v2
totals: settled input/output tokens exclude unresolved reservations;
settled dollars require recorded pinned pricing. Malformed accounting
stays unknown without retroactively failing an observed trial grade.

The Modal sandbox cost is an estimate: recorded job seconds at Modal
list prices (CPU $0.0000131/core/s, memory $0.00000222/GiB/s) with the
task's declared `[environment]` resources. Missing durations are unknown,
not zero. Rows preserve each component even when the batch total cannot
be established.

Zero token prices on a self-hosted route do **not** price its serving
app: model cost and the batch total remain null. That separate time and
billing evidence must be accounted independently. The rollout estimate is neither a
provider invoice nor a financial-cap guard; serving, unresolved
exposure and other unpriced resources require their own accounting.

## Throughput

Sandbox startup plus grading for a small task is ~30 s on Modal
(measured 2026-10-16, oracle baseline: 6/6 trials at reward 1.0 across
three first-party tasks, all concurrent). Throughput is task-bound: at
concurrency C, tasks/hour ≈ C / mean trial hours, so long agent trials
want C in the teens. Local Docker is not used.
