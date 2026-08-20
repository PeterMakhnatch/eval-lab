---
status: living
audience:
  - builder
  - operator
---

# BUILDER authoring pipeline

Status: living. Owner: Tasks lane. Date: 2026-08-17. Implements
`docs/build-plan.md` WS-C, SG-1 (`docs/prompts/synthesis-build.md`), and SG-2.

`src/evallab/authoring.py` seeds quarantined task proposals, samples specifications
coverage-first from decoupled axes data files, runs the four local control checks,
scores a CRAFT-derived rubric, and records every step in a qualification ledger.
It never registers a task. Registration remains the existing human-only `evallab registry` path.

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
# Spec sampling (SG-2)
python -m evallab.authoring sample --count 20
python -m evallab.authoring --json sample --count 20

# Proposal generation
python -m evallab.authoring propose --seed mutation --ref event-summary
python -m evallab.authoring propose --seed scenario --ref research/scenarios/gap-notes.md
python -m evallab.authoring propose --seed craft-gap
python -m evallab.authoring propose --seed inversion --ref library/tasks/event-summary/environment/events.jsonl
python -m evallab.authoring propose --via-harbor --seed craft-gap --agent oracle
python -m evallab.authoring model-propose \
  --topic incident-response --style formal \
  --model gemini-3.7-flash-high --transport agy   # spends subscription quota
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
| `inversion` | real data asset inside `library/` environments (JSONL, JSON, CSV, SQL, text) | Answer-first task package whose answer key is computed by executing reference analysis code against the data asset; instruction is written backwards from the verified key. |

## Dimension-Decoupled Spec Sampling (SG-2)

Implements coverage-first spec sampling across decoupled axis data files under `authoring/templates/`.

### 1. Axes as Data Files

The axes are specified as clean, self-describing YAML files:

- **`authoring/templates/category.yaml`**: Domain categories derived directly from CRAFT facets and the scanned 551-task corpus (74 TB3 tasks + 477 library tasks). Includes:
  - `data-engineering`, `systems-programming`, `scientific-computing`, `machine-learning-infra`, `formal-methods`, `database-internals`, `operations-research`, `cad-hardware-design`, `security-incident-response`, `web-engineering`, `financial-engineering`, `multimedia-processing`, `graph-algorithms`, and `code-repair-and-synthesis`.
  - Each category records typical verifier mechanisms, languages, container tendencies, exemplar tasks from the corpus, and topic seeds for novel generation.
- **`authoring/templates/scenario.yaml`**: 10 instruction styles spanning register (terse to conversational to formal) and length (minimal to long):
  - `minimal`, `incident-emergency`, `bug-report`, `feature-specification`, `refactoring-migration`, `investigation-audit`, `dialogue-transcript`, `structured-pipeline`, `adversarial-obfuscated`, and `documentation-driven`.
  - Each scenario defines exact authoring guidelines and prompt style constraints.
- **`authoring/templates/difficulty.yaml`**: 4 difficulty levels (`introductory`, `intermediate`, `advanced`, `expert`) with explicit complexity bounds and anti-pattern lists:
  - Anti-patterns capture what makes a task *bad* rather than hard (e.g. artificial file obscurity, unseeded flaky verifiers, fragile string-matching, compile loops > 10m, oracle leakage).

### 2. Coverage-First Sampling Order

Unlike the paper (arXiv:2607.27929) which samples the axis space randomly, this lab prioritizes **coverage-first**:

1. **Primary — CRAFT Gap Queries**: Queries unexercised facet combinations (`verifier_type × env_multi_container × pinned_deps`) with zero coverage in `derived/parquet/craft/craft.parquet`. All available craft gaps are emitted first.
2. **Secondary — Random Axis Product**: Samples uniformly from the Cartesian product of `category × scenario × difficulty` to fill the remaining requested batch count once gaps are exhausted.
3. **Multi-Phase Novel-Spec Mode**: The production CLI requires an explicitly pinned `--model` and `--transport`; model calls spend subscription quota. Tests and offline controls may inject the explicitly named `local_test_designer`, which never invokes a provider.

### 3. Ledger Deduplication and Lineage

- **Deduplication against Qualification Ledger**: Every sampled spec is checked against existing entries in `derived/parquet/qualification/ledger.parquet` and quarantined proposals (`library/tasks/_proposed/`). Deduplication is keyed strictly on the spec's axis coordinates (`category`, `scenario`, `difficulty`, `target_facets`), never on display names. Matching specs are excluded.
- **Proposal Lineage**: Axis coordinates (`axes`, `category`, `scenario`, `difficulty`, `provenance`) are permanently stored on `Proposal`, serialized into `proposal.json`, and mirrored in `ProposalSpec` Pydantic models.

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


### Inversion Tasks (`seed_class=inversion`)

Implements SG-3 (`docs/prompts/synthesis-build.md` lines 56-65) answer-first task generation:

1. **Execution Ground Truth**: The answer key is correct by construction because it is computed by executing Python reference analysis code against a real data asset from `library/` environments. If the analysis code fails or does not execute cleanly, the proposal is **refused** — never filled in with a guessed or model-authored value.
2. **Reproducibility**: The reference analysis code, data asset digest, and computed value are recorded in `inversion.json` and `proposal.json` (`inversion_analysis`). The helper `verify_inversion_reproducibility` re-runs the code to confirm the key matches.
3. **Provenance & Lineage**: The proposal embeds `inputs: [{path, id, digest}]` pointing to the source data asset so `evallab lineage` traces provenance directly to the data.
4. **Gating**: Inversion proposals enter at `proposed` and pass through the unchanged four-check battery (`oracle`, `nop`, `fair-oracle`, `adversarial`) and CRAFT review rubric before halting at the human registration gate.
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
| `seed_class` | string, required | `mutation` \| `scenario` \| `craft-gap` \| `inversion`. |
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
