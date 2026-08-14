Status: review-wanted
Last: Renamed the package, CLI, runtime identity, and prose; premerge is green with 54 tests and ty 33 <= 33.
Next: Rebase onto origin/main, rerun premerge, open the REFRAME PR, and require every GitHub check green before merge.
Blockers: none.

# REFRAME handoff

## Entry gate

- Closed the two finished Cursor agent terminal panes (`ttys018` and `ttys020`)
  after confirming they had no child processes; the base lab terminal remained.
- `gh pr list --state open` returned an empty list.
- `scripts/fleet-status.sh` showed no running queue jobs. One spec remained in
  `waiting` under the existing `quiet_failure_rule`; it was not executing.
- Integrated role branches were at main. The only ahead branch was the known
  spent FORGE branch, whose PR #6 was already merged.

## Migration boundary

Eval Lab owns evaluation intent, evidence, analysis, and guarded iteration.
Harbor remains the execution engine and keeps its genuine CLI, ATIF,
`harbor-atif2otel`, and adapter references. Immutable run evidence under
`research/evidence/runs/` is not rewritten to disguise its historical paths.

## Verification ledger

| Step | Evidence |
|---|---|
| Quiet-fleet gate | No open PRs; no running queue jobs; base `5a9b834` |
| Code/prose PR | Local premerge green: Ruff clean, 54 tests, ty `33 <= 33` |
| GitHub repository rename | Pending |
| Local directory/worktree repair | Pending |
| launchd regeneration and fired tick | Pending |
| Compose restart | Pending |
| `evallab doctor --headless` | Pending |
| Post-move premerge | Pending |
| Free oracle queue control | Pending |
