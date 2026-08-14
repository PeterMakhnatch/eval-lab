Status: building
Last: created role/runner worktree from origin/main; read protocol + policy + submit schema
Next: write 4-6 experiment specs; submit admissible ones; run free oracle/nop baselines
Blockers: none yet — headless doctor likely quarantines tick if Claude keychain is absent

Worktree: `.worktrees/runner` on `role/runner` @ origin/main (`ddb4b03`).
Owned paths only: `research/experiments/`, `agents/handoffs/runner.md`.

Policy facts (not stretched):
- `local-controls` admits any oracle/nop spec.
- `canary` admits `task=canary/*` + codex|claude-code + attempts≤3.
- `researcher-followups` admits `task=registered/*` + attempts≤5 + requires {schema_valid, dedup_pass, calibrated_judges_only}. Nothing is registered; will not invent that namespace.
- Tick/nightly fail closed if ANY headless-doctor check fails (including the absent Claude keychain). Free baselines will use `harbor-lab run`/`matrix` (oracle/nop only).

Canary members actually pinned: event-summary, transaction-reconciliation, terminal-bench-html-js-filter. Curated nominees are cards only (no task.toml here).
