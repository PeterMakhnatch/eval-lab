---
status: living
audience:
  - builder
  - operator
---

# BUILDER authoring pipeline

Status: living. Owner: Tasks lane. Date: 2026-08-16. Implements
`docs/build-plan.md` WS-C and SG-1 (`docs/prompts/synthesis-build.md`).

`src/evallab/authoring.py` seeds quarantined task proposals, runs the four
local control checks, scores a CRAFT-derived rubric, and records every step
in a qualification ledger. It never registers a task. Registration remains
the existing human-only `evallab registry` path.

## State machine

```text
proposed → battery_passed → craft_reviewed → registered | rejected
```

| State | Who writes it | Meaning |
|---|---|---|
| `proposed` | `author propose` | Package exists under `library/tasks/_proposed/<proposal_id>/`. Source task is untouched. |
| `battery_passed` | `author battery` | All four free local controls passed and left evidence paths on the ledger. |
| `craft_reviewed` | `author review` | Rubric score and reasons written. This is the last automated state. |
| `registered` | a human, via `evallab registry` | Explicit JSON record in `library/registry/`. Authoring **refuses** this transition. |
| `rejected` | a human (or a later reviewer) | Not written by this module. |

`AuthoringPipeline.register` and `upsert_ledger` raise `RegisterRefusal` if
asked to write `registered`. That is the fail-closed gate: automation can
qualify, it cannot admit.

Quarantine is `library/tasks/_proposed/<proposal_id>/`. A mutation copies a
registered (or library) task into a new directory and bumps `[task].version`.
The source tree is hashed before and after the copy; a digest change is a
hard error. Scenario and craft-gap proposals are new stub packages, never
edits of an existing task.

## CLI

Reachable as `python -m evallab.authoring`. `evallab author …` is specified
in the build plan and is not wired here because `src/evallab/cli.py` is
leased elsewhere this round. `build_parser()` is the flag surface to attach.

```bash
python -m evallab.authoring propose --seed mutation --ref event-summary
python -m evallab.authoring propose --seed scenario --ref research/scenarios/gap-notes.md
python -m evallab.authoring propose --seed craft-gap
python -m evallab.authoring propose --via-harbor --seed craft-gap --agent oracle
python -m evallab.authoring harvest <job_id_or_path>
python -m evallab.authoring battery <proposal_id>
python -m evallab.authoring review <proposal_id>
python -m evallab.authoring register <proposal_id>   # always refuses
python -m evallab.authoring batch --count 5          # propose → battery → review; halt
```

`--root` and `--out` inject the repository and the derived Parquet root so
tests never touch a developer's host layout. `--json` emits the same facts
the human summary prints.

### Seeds

| `--seed` | Source | What is written |
|---|---|---|
| `mutation` | `--ref` registered/`library/tasks` task, else the first registered or library task | New versioned copy. Never in-place. |
| `scenario` | markdown under `research/` (`research/scenarios/` first, then explorations / inspections) | Stub Harbor package whose instruction cites the scenario path and excerpt. |
| `craft-gap` | first uncovered `verifier_type × env_multi_container × pinned_deps` triple in `derived/parquet/craft/craft.parquet` | Stub package targeting that triple. |

### Meta-Loop Generation (`--via-harbor` and `harvest`)

Implements the Meta-Task pattern (arXiv:2607.27929, `library/meta/synthesize-task@1`).

1. **`propose --via-harbor`**: Assembles the meta-task template (`library/meta/synthesize-task@1`), injects the sampled specification and exemplar, and submits the job through `evallab.queue` with `purpose="craft"`.
   - **Submit-only**: Puts the job in the queue and halts. It does not dispatch or start runner processes.
   - **Policy Gate**: Standing approvals enforce authorizations; paid models without approval land in `waiting/` and are refused execution.
2. **Completeness Checker**: Verification logic inside the meta-task enforces 4 checks on generated packages:
   - `package_structure`: Valid `task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/solve.sh`, `tests/Dockerfile`, and `tests/test.sh`.
   - `oracle_solution_runs`: Reference solution in `solution/` executes cleanly.
   - `task_tests_pass`: Task verifier passes on the oracle solution output and fails on empty work.
   - `no_answer_leakage`: Verifies that hidden test logic, golden data, and solution code do not leak into `instruction.md` or `environment/`.
3. **`harvest`**: Ingests the generated package from a completed job run into quarantine (`library/tasks/_proposed/<proposal_id>`).
   - Verifies the completeness checker passed in the job; rejects unverified artifacts.
   - Records lineage inputs (`inputs: [{path, id, digest}]`, `job_id`, `injected_spec`, `exemplar`) in `proposal.json` so `evallab lineage` resolves provenance back to the generating run.
   - Harvested proposals enter in `proposed` state and must pass the standard 4-check local battery before advancement.

### Battery

Four checks, free local agents only, `n ≤ 2` on nop. Default runner is
structural (no Harbor, no model). A Harbor-backed `ControlRunner` can be
injected; it still must not call a paid model.

| Check | Pass rule | Recorded reward on pass |
|---|---|---|
| `oracle` | `solution/` is present | `1.0` |
| `nop` | `tests/` is present; two empty-work attempts | `0.0` |
| `fair-oracle` | fresh surface is `instruction.md` + `environment/` only | `1.0` |
| `adversarial` | cheat-instructed agent cannot read the answer out of the instruction; verifier exists | `0.0` |

Each check writes a JSON evidence file under the proposal's `battery/`
directory. The ledger stores the four bools plus those paths.

### Review

`score_review` is a deterministic CRAFT-pattern rubric: Harbor layout,
separate verifier image (`hidden_tests`), answer-hiding (solution/tests
outside the agent surface), and a seed-class citation (new version + source
digest, research path, or gap triple). Same bytes ⇒ same score. Reasons are
written to `review.json` and to the ledger's `review_score`.

## Ledger

Path: `derived/parquet/qualification/ledger.parquet`
(`paths.derived_root_from_environment` / `qualification/ledger.parquet`).

Columns:

| Column | Type | Notes |
|---|---|---|
| `proposal_id` | string, required | ULID (or injected id in tests). |
| `seed_class` | string, required | `mutation` \| `scenario` \| `craft-gap`. |
| `ref_task` | string, nullable | Source task id or research path. |
| `battery_oracle` | bool, nullable | |
| `battery_nop` | bool, nullable | |
| `battery_fair_oracle` | bool, nullable | |
| `battery_adversarial` | bool, nullable | |
| `evidence_paths` | list\<string\> | Battery (and later review) evidence. |
| `review_score` | float64, nullable | Rubric in `[0, 1]`. |
| `outcome` | string, required | State-machine value. Never `registered` from this module. |
| `created_at` | string, required | ISO-8601 UTC. |
| `updated_at` | string, required | ISO-8601 UTC. |

Pass-rate per `seed_class` is one DuckDB query
(`SEED_CLASS_PASS_RATE_SQL` in `authoring.py`):

```sql
SELECT
    seed_class,
    avg(
        CAST(
            coalesce(battery_oracle, false)
            AND coalesce(battery_nop, false)
            AND coalesce(battery_fair_oracle, false)
            AND coalesce(battery_adversarial, false)
            AS INTEGER
        )
    ) AS pass_rate,
    count(*) AS n
FROM read_parquet($ledger)
GROUP BY 1
ORDER BY 1
```

## Where determinism stops

Everything after this line is deliberately not a pure function of the
repository bytes.

- **Proposal ids.** Default allocator is `queue.new_ulid`. Tests inject
  `new_id`.
- **Timestamps.** `created_at` / `updated_at` come from `datetime.now(UTC)`.
  Tests inject `now`.
- **An injected Harbor runner.** The default `StructuralControlRunner` is
  deterministic over the proposal tree. A runner that starts Harbor inherits
  Harbor's non-determinism (image builds, clocks, container ids). The
  pipeline still records whatever bools and evidence paths that runner
  returns; it does not re-interpret them.
- **Registration.** Not performed here. A human decides, through
  `evallab registry`, after reading the ledger row and the quarantine
  package.

Re-running `propose` on the same seed therefore yields a new proposal id and
a new ledger row. Re-running `battery` or `review` on the same proposal,
with the default runner and no tree change, rewrites the same bools, the
same score, and the same reasons.

## What this module does not claim

- It does not call a paid model, start Harbor, or write `library/registry/`.
- A structural battery pass is not Harbor evidence. Promoted control
  evidence for a registered task still has to come from real oracle/nop
  runs, as `docs/task-registry.md` requires.
- `rejected` is a legal terminal state in the spec and is not written by
  this CLI. A human marks that disposition on the ledger or by deleting the
  quarantine package.
- `evallab author` is not a `cli.py` subcommand yet. Use
  `python -m evallab.authoring`.
