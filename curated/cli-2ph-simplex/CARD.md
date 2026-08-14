# cli-2ph-simplex

## Provenance

- **Source repo:** `harbor-framework/frontier-bench` (clone `~/Developer/agent-evals/frontier-bench`)
- **Commit:** `3d694e919871dbf21ea5ff618782c99a3cb3663f`
- **Path:** `tasks/cli-2ph-simplex`
- **Also present at:** terminal-bench `4e77c91dc523107eedd9440b659159d470209188`
- **License:** Apache License 2.0
- **Package name:** `terminal-bench/cli-2ph-simplex`

## Pinned digest

- **Harbor `task_checksum`:** `60b2150c58190978991251ad6b8773dd078710aeadbcb522800fab8526f3fccf`

## Difficulty / domain / runtime

- **Domain:** Software / Algorithms
- **Difficulty:** expert_time_estimate_hours = 2.0; agent timeout 2500s
- **Resources:** 1 CPU, 2048 MB, 0 GPU
- **Verifier:** separate; pickle/text artifacts from `lp_solve`

## Local verification (free oracle / nop)

| Run | Agent | k | -n | jobs_dir | Result |
| --- | --- | --- | --- | --- | --- |
| `runs/oracle-cli-2ph-simplex` | oracle | 3 | 2 | `~/Developer/helab-curator/runs` | 3/3 reward **1.0** |
| `runs/nop-cli-2ph-simplex` | nop | 1 | 1 | same | reward **0.0** (job dir present; nop mean 0 after completion) |

Trials: `cli-2ph-simplex__7tXJvmf`, `__h7ke3jB`, `__teuwVYZ`.

## Verifier read-through

- Starter package copied into `/app` (not the oracle). Tests run the `lp_solve` CLI on fixtures and compare tableaus / reports.
- Instruction is unusually process-heavy (tableau column order, pivot-log schema). Included because merged TB3 task verifies **CLI outputs**, not `grep import`.
- **Leakage / hack smells:** no gold in agent image. Tight format coupling is a product-spec smell, not env leakage.

## Canary note

Fast CPU algorithm task; instruction is long (not a first-line canary).
