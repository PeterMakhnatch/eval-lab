Status: done
Last: Renamed the code, repository, directory, scheduler, and services; doctor, launchd, premerge, and a queued Oracle control all passed from ~/Developer/eval-lab.
Next: none.
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
| Code/prose PR | [PR #9](https://github.com/PeterMakhnatch/eval-lab/pull/9) merged as `3e2dd7b`; [quality](https://github.com/PeterMakhnatch/eval-lab/actions/runs/31825721567) and [typecheck](https://github.com/PeterMakhnatch/eval-lab/actions/runs/31825721529) green on the PR head; [quality](https://github.com/PeterMakhnatch/eval-lab/actions/runs/31825834419) and [typecheck](https://github.com/PeterMakhnatch/eval-lab/actions/runs/31825834365) green on merged main |
| GitHub repository rename | `PeterMakhnatch/eval-lab`; origin is `git@github.com:PeterMakhnatch/eval-lab.git` |
| Local directory/worktree repair | Main moved to `~/Developer/eval-lab`; all eleven retained role worktrees resolve below `.worktrees/` after explicit `git worktree repair` |
| launchd regeneration and fired tick | New `com.petermakhnatch.evallab.tick` and `.nightly` plists point to the new path; tick run count advanced to 2 with latest exit code 0 |
| Compose restart | Legacy project stopped; `eval-lab-postgres-1` healthy and `eval-lab-phoenix-1` running on ports 54329, 6006, and 4317; derived schema initialized |
| `evallab doctor --headless` | Healthy at `2026-08-14T17:55:19.010507Z`: Codex auth, Docker, PostgreSQL, and disk true; Keychain false without blocking health |
| Post-move premerge | Ruff clean, 54 tests, ty `33 <= 33` from `~/Developer/eval-lab` |
| Free oracle queue control | Spec `01M00PQ900EXX6M6ASEVY3AX1K` admitted by `local-controls`, moved to `queue/done`, and produced reward 1.0 in 8.578 seconds at `runs/reframe-post-move-oracle-20260814-1756` |

## Relocation findings

- The moved virtual environment retained absolute console-script shebangs. The
  first post-move premerge reached Ruff but could not spawn pytest. A locked
  `uv sync --reinstall` rebuilt all 41 packages against the new path.
- Main retained an ignored `__pycache__/` under the legacy package directory
  after the tracked rename. It contained bytecode only; removing that
  regenerable cache made the repository identity contract pass from main.
- The launchd tick's RunAtLoad execution proved the new command path but exited
  1 while the old PostgreSQL identity was still running. After the prescribed
  Compose restart and schema initialization, an explicit launchd kickstart
  advanced the run count and exited 0.
- Peter concurrently added `docs/research-questions.md` as main commit
  `650a852`. REFRAME preserved it unchanged. Its
  [quality](https://github.com/PeterMakhnatch/eval-lab/actions/runs/31826163574)
  and
  [typecheck](https://github.com/PeterMakhnatch/eval-lab/actions/runs/31826163566)
  workflows are green, and the identity contract passes with the new document.

## Legacy-name audit

The repository contract rejects the old Python package, repository name, and
lab title. The only intentional legacy command strings are the one-week alias,
its warning/contract, and two alias invocations inside the digest-pinned
event-summary task. Immutable historical paths in `research/evidence/runs/`
remain untouched. All other Harbor references identify the execution engine,
ATIF integration, `harbor-atif2otel`, or adapters.
