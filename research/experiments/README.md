# Experiments

RUNNER-owned. Specs are the unit of submission; this journal is the
human-readable thread. Generated jobs stay in the worktree `runs/` directory
and are not committed.

| Path | What it is |
|---|---|
| `PROGRAM.json` | Versioned machine-readable ledger (`validate_program.py`) |
| `STATUS.md` | Human page: RECENT / RUNNING NOW / NEXT / TASK DECISIONS |
| `JOURNAL.md` | Running scientific thread: what / why / status / results / links |
| `specs/` | One study per directory; JSON files are `ExperimentSpec` documents |
| `preambles/` | Extra-instruction files for studies the runner cannot yet express |
| `baselines/` | Free oracle/nop matrices (`evallab matrix` only) |
| `local-controls.json` | Original event-summary oracle/nop matrix (kept; tests load it) |

Standing policy that admits work, copied from `policy/standing-approvals.yaml`
and not stretched:

- `local-controls` — any `oracle` / `nop` spec
- `canary` — `task` matches `canary/*`, agent `codex` or `claude-code`, attempts ≤ 3
- `researcher-followups` — `task` matches `registered/*`, attempts ≤ 5, and
  `requires` includes `schema_valid`, `dedup_pass`, `calibrated_judges_only`

Nothing in this checkout is registered. Using `registered/*` or putting a
non-member under `canary/` would be a policy stretch; those questions go to
Peter in the handoff.

Harbor 0.21 accepts `--extra-instruction-path`. `evallab.runner.build_command`
does not forward it, and `ExperimentSpec` forbids unknown fields. Preamble A/B
is designed here and is not executable through the queue until BUILDER adds
that field.
