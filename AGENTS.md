# Eval Lab instructions

Eval Lab preserves immutable task versions and raw execution evidence.
Use this path for a normal code change. Read only the selected domain manual.

## Change path

Keep the primary checkout on `main`. Never edit or run checks from a dirty primary.
Keep the controlling shell outside the topic worktree so ship can retire it.
Replace the angle-bracket values before running these commands.

1. Fetch and create an owned topic worktree and a detached delivery worktree:
   ```sh
   git -C ~/Developer/eval-lab fetch origin
   git -C ~/Developer/eval-lab worktree add -b <topic> ~/Developer/eval-lab/.worktrees/<topic> origin/main
   git -C ~/Developer/eval-lab worktree add --detach ~/Developer/eval-lab/.worktrees/<topic>-delivery origin/main
   ```
2. Install an isolated locked environment in each worktree:
   ```sh
   cd <worktree> && uv sync --locked --no-group observability --group benchmarks --group inspect
   ```
   Run repository commands inside the owned worktree, not the primary checkout.
3. Read the affected package instructions, then exactly 1 manual from the table below.
   Core changes also read `src/evallab/AGENTS.md`. Read placement rules only when adding or moving files.
4. Edit the scoped files. Inspect their direct consumers. Run the focused checkpoint before every push:
   ```sh
   make prepush TESTS='<affected test selectors>'
   ```
   Without an explicit selection, `make prepush` selects affected tests. It does not prove CI green.
5. Commit only your paths and push 1 head per fix:
   ```sh
   git add -- <owned-paths>
   git commit -m '<complete change summary>' -- <owned-paths>
   git push -u origin <topic>
   ```
6. Create the PR with its complete title and body once:
   ```sh
   gh pr create --base main --title '<title>' --body-file <body-file>
   ```
   Add later evidence with `gh pr comment <PR> --body-file <evidence-file>`.
   Title or body edits repeat CI. Do not rebase because `main` moves; resolve a real conflict only.
7. Read the exact-head CI result before another push:
   ```sh
   gh pr checks <PR> --watch
   ```
   Every reported check must succeed. `agents/CHECKS.md` owns the gate contract and deterministic-test rules.
8. Refresh the detached delivery worktree to `origin/main`, then run there:
   ```sh
   git fetch origin
   git checkout --detach origin/main
   uv sync --locked --no-group observability --group benchmarks --group inspect
   uv run --no-sync evallab ship <PR> --no-rerun-failed-once --smoke 'uv run --locked evallab --version'
   ```
   Ship proves the merged revision and retires the topic. Remove the detached delivery worktree with plain Git.
   A failed check or smoke remains author-owned work. See the delivery manual for recovery.

## Domain router

Implementation paths in the second column start at `src/evallab/`.
The table selects 1 manual, not every linked manual.

| Domain | Implementation entry | Read |
|---|---|---|
| CLI and execution | `entry.py`, `cli.py`, `execution_contracts.py`, `runner.py`, `queue.py` | `docs/execution-tiers.md` |
| Tasks and certification | `registry.py`, `task_lint.py`, `gold_certifier.py`, `qa/task.py` | `docs/task-quality.md` |
| Evidence and storage | `evidence/atif.py`, `database.py`, `process_job.py`, `storage/paths.py` | `docs/data-architecture.md` |
| Offline detection | `line/cli.py`, `line/core.py`, `hack_drop.py`, `trajectory_ir.py` | `docs/line.md` |
| Online monitoring | `live_watch.py`, `production/monitor_worker.py`, `production/window_cascade.py` | `docs/live-watch.md` |
| Gold and scoring | `gold/`, `failure_library/bank/`, `score/inputs.py`, `score/metrics.py` | `docs/score.md` |
| Local checks and delivery | `devloop.py`, `ship/`, `scripts/premerge.sh` | `docs/ship.md` |

Placement: `agents/STRUCTURE.md` owns repository buckets; `docs/SYSTEM.md` owns code and evidence boundaries.
Coordination: `agents/WORKFLOW.md` owns handoffs; `agents/OWNERS.md` routes responsibility, not contact permission.
Product orientation: `docs/NOW.md` is optional background, not another required code-change router.
Pass the relevant rules explicitly to task workers. They do not inherit instruction bodies.

## Never

- Never reset, stash, overwrite, or delete another writer's changes or unique ignored evidence.
  `docs/GENERATED-CACHE-POLICY.md` owns retention, including raw jobs, evidence CAS, queue events, backups, and drafts.
- Never bypass CI or branch protection with `--admin`, a direct merge, or fabricated evidence.
  Ship selects the base-owned Line DEV gate from `agents/ship-gates.toml`; missing replay data blocks delivery.
- Never change frozen prompts, thresholds, Gold rows, score contracts, DEV decisions, or held-out exclusions without the assigned authority.
  Do not open sealed held-out payloads or expose hidden tests and solutions to the evaluated agent.
- Never treat credentials, an old cap, a merge, or an oracle/nop result as paid-run authority or model-capability proof.
  Before a trial, read the execution manual's policy and selected backend/agent sections.
  Peter controls paid calls, remote compute, shared-service changes, task registration, and external publication.
  Do not loosen `policy/standing-approvals.yaml` or run local generative-model inference on Peter's laptop.
- Never edit registered task bytes in place. Create a new version. Preserve imported benchmark contracts.
- Never commit secrets, runtime credential databases, volumes, unredacted private prompts, or arbitrary large run directories.
- Never add an incidental language or package migration. Application code, adapters, verifiers, and first-party task authoring use Python.
  Shell, SQL, Dockerfiles, and configuration are allowed. Keep deterministic SQL ingestion and immutable raw source authority.
