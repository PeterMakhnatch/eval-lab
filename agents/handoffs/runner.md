Status: blocked
Last: PR #5 open (not self-merged); six studies + control interpretation landed on role/runner
Next: dispatch the five approved canary jobs once tick is unblocked; then interpret Codex trajectories
Blockers: headless doctor keychain_readable=false quarantines all tick dispatch; CI ruff fails on CURATOR/RECON files already on main (not ours)

## Done tonight

- 6 studies in `research/experiments/specs/` (one variable each).
- Every admissible spec submitted via `harbor-lab submit`.
- Refusals recorded: k5 `per_job_cost_ceiling`; curated + query-optimize `out_of_policy`; preamble A/B not submitted (ExperimentSpec cannot carry `--extra-instruction-path`).
- Free oracle/nop on event-summary, transaction-reconciliation, html-js-filter, query-optimize. All four families valid (oracle 5/5, nop 0). Journal has n, Wilson intervals, trajectory attribution.
- Tick attempted: `tick_quarantined` / `headless_doctor_failed:keychain_readable`.

## Not done (acceptance gap)

No Codex study completed. Standing policy + "never harbor run a paid agent" + fail-closed tick means Study 01 cannot produce a model result until Peter stores the Claude keychain item **or** the doctor stops requiring it for Codex dispatch.

## For Peter

- `registered/*` is untouched. Studies 05 and 06 are the registration questions.
- n=5 at the published $2.50/3-attempt rate is $4.17 > $3/job.
- Shared Postgres: this worktree's `database.initialize` raises `cannot drop columns from view`. Jobs are on disk under `.worktrees/runner/runs/`; they did not ingest.
- query-optimize is valid and a bad canary (amd64 image, ~10 min/trial, verifier does not fail-fast on nop).

## PR

https://github.com/PeterMakhnatch/harbor-experiment-lab/pull/5
Diff is only owned paths. Local `uv run pytest -q` is 36 passed. Did not squash-merge: quality `ruff check .` fails on `library/curated/_emit_card.py` and `research/explorations/harbor-021/demos/*` (CURATOR / RECON), already on `origin/main`.

## Queue spec_ids (this worktree)

Approved: `01KZZB36PPBKM863RB5R2MQDZG` `01KZZB36VPXEQ6E8D0QZ13SRNZ` `01KZZB370NKG312T8ZB17Y371H` `01KZZB375FWNZERK8V09RJYNGN` `01KZZB37F6S9NHAYHR7W5KAR5S`
Waiting: `01KZZB37AF6HKA21A57NP5D0N8` (cost) `01KZZB37M99GPF788SXF5CJ1EF` `01KZZB37SFMQJ9JEAHC41HVXDG` (out_of_policy)
