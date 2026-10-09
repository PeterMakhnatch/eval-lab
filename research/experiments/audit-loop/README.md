# Audit loop: static lint -> controls -> deterministic ladder

Closed integrity loop for lab-authored tasks, built from the published
attacker-agent pattern (static screen, then proof through the official
entrypoint with the benchmark's own verifier as judge, gated on
oracle/nop controls).

## Why this exists

The lab's lanes were separate: `evallab tasks lint` (static findings) and
`evallab cheat run` (deterministic ladder, proven on MiMo derived packages)
had never been run together against the lab's own authored tasks in
`library/tasks/`. A static finding alone never counts; only verifier reward
through the entrypoint does, and only when oracle passes (solvable) and nop
fails (grader not vacuous).

## What it runs per task ($0, local Docker, no model calls)

1. `lint_task` — read-only static screen.
2. `evallab run --agent oracle` / `--agent nop` — controls.
3. `evallab cheat run` — full 12-attack deterministic ladder.
4. One JSONL row: lint findings, oracle/nop/cheat-max rewards, conclusion
   (`clean` | `cracked` | `broken-task` | `vacuous-grader`).

## Run it

```bash
uv sync --frozen --extra laminar  # Harbor 0.24 required for cheat run
uv run --extra laminar --no-sync python research/experiments/audit-loop/audit_loop.py \
  --task library/tasks/event-summary \
  --jobs-dir runs/audit-loop \
  --out research/experiments/audit-loop/ledger.jsonl
```

## Results

| Task | Lint | Oracle | Nop | Cheat max | Conclusion |
|---|---|---|---|---|---|
| `local-lab/event-summary` | 0 findings | 1.0 | 0.0 | 0.0, full ladder | clean |
| `transaction-reconciliation` | verifier-isolation | 1.0 | 0.0 | 1.0 | **cracked via `tamper_reward_writer` only** (4 other plants clean) |
| `query-optimize` | verifier-isolation | 1.0 | 0.0 | 1.0 | **cracked via `tamper_reward_writer` only** (3 other plants clean) |

Jobs: `runs/audit-loop/` (gitignored worktree runtime).
Ledger: `ledger.jsonl` (this dir, committed).
