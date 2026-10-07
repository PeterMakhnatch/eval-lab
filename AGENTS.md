# Eval Lab instructions

Private, durable Python lab for Harbor tasks, verifier controls, execution evidence, and analysis. Raw Harbor job directories and promoted task versions are immutable source evidence; catalogs and projections are derived.

## Boundaries

- Application, adapters, verifiers, and first-party task authoring use Python; shell, SQL, Dockerfiles, and configuration are allowed. Preserve imported benchmark contracts. A language or module-layout migration must be part of the assigned change, not incidental cleanup.
- Free local preparation, code work, inspection, and bounded oracle/nop controls need no second approval. Paid model calls, remote/cloud compute, disruptive shared-service deployment, task registration, and publication outside authorized repository delivery require current applicable authorization. Credentials are not spend permission; old run caps do not authorize new specs. Do not loosen `policy/standing-approvals.yaml`.
- Before executing a trial, read the policy section and selected backend/agent section of `docs/execution-tiers.md`. No local generative-model inference on Peter's laptop.
- Oracle/nop results establish task or harness validity, not model capability. Pin task version, agent/model, Harbor revision, and the changed variable for comparisons; do not expose hidden tests or solutions to the agent.
- Do not commit secrets, credential/runtime databases, volumes, private unredacted prompts, or arbitrary large run directories. Run artifacts normally live under `runs/`; reviewed small immutable evidence belongs under `research/evidence/`.
- SQL/catalog ingestion is deterministic and idempotent. `sql/schema.sql` and tested parsers own derived contracts; immutable job bytes remain source authority.
- Ignored is not disposable: preserve unique run evidence, `derived/evidence-cas/`, queue events/leases, backups, and drafts. Follow `docs/GENERATED-CACHE-POLICY.md` for retention or worktree retirement.

## Placement and delivery

Keep the primary checkout on `main` and preserve another writer's work. A sync timer fast-forwards it to `origin/main` only when clean; it never resets, stashes, or switches branches. Use an owned topic branch and isolated native Git worktree for substantive changes; launch its chat with `omp --cwd <worktree>`. Do not use `/tmp` as a persistent repository chat root.

`agents/STRUCTURE.md` owns placement; register a new top-level entry there in the same change. Existing roots: `src/`, `tests/`, `library/`, `research/`, `docs/`, `agents/`, `policy/`, `scripts/`, `sql/`; `runs/`, `derived/`, `queue/`, and `backups/` have retention contracts. Finished jobs are published to `~/Developer/eval-lab-results/<date>/<card>-<job/>` by `evallab process-job`, outside every worktree.

The author delivers through PR, exact-head CI, protected merge, and bounded merged-revision verification under `agents/CHECKS.md`. No independent reviewer or integration steward is a routine prerequisite. `agents/WORKFLOW.md` applies to ownership/integration; Linear is current work intake, not the historical board.

Read only the route needed: `docs/NOW.md` is the short router; core work also reads `src/evallab/AGENTS.md` and the affected package rules. Plain documentation edits need the affected source and direct references, not the full architecture stack. Pass relevant repository requirements explicitly to native task workers: task spawning does not inherit AGENTS.md bodies.

## Safe run pattern

Use the wrapper so run provenance is recorded and billable adapters require an
explicit acknowledgement:

```bash
uv run evallab run \
  --task library/tasks/event-summary \
  --agent oracle \
  --name event-summary-oracle
```
