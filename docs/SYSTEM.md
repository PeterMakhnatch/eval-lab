---
status: living
audience: [builder, runner, operator, analyst]
---

# Eval Lab system and contracts

Eval Lab keeps 5 functions and 1 measurement: run, gold, detect, watch, certify, and score.
The minimum delivery tooling supports those functions.
[The router](NOW.md) selects the manual for each task.
This map describes code and evidence boundaries. It does not report live service health.

## Execution and evidence flow

| Stage | Owner | Artifacts and boundary |
|---|---|---|
| Launch | `cli.py`, `queue.py`, `runner.py` | The experiment spec and append-only authorization ledger control admission. A merge does not authorize a run. |
| Harness | `execution_contracts.py`, native Harbor adapters | Pin task, verifier, environment, scaffold, model, and execution settings. Keep hidden verifier material outside the agent environment. |
| Capture | `model_capture.py`, provider sidecars | Preserve actual requests, responses, usage, capture manifests, and job links when enabled. Credential isolation does not imply complete capture. |
| Trial | Harbor, `evidence/atif.py` | Keep the original job, trajectory, verifier output, errors, and `result.json`. A catalog row does not replace raw evidence. |
| Processing | `process_job.py`, `evidence/`, `storage/` | Ingest and project completed evidence. Processing does not rerun a trial or replace its reward. |
| Counts and reports | `counts.py`, `trial_decision.py`, `interpretation/run_report.py` | Counts owns `counted_pass`, `counted_fail`, and `excluded`. Keep detector opinions separate from native rewards. |
| Publication | `results_home.py` | Completed reports live under `~/Developer/eval-lab-results/<date>/<card>-<job>/`. Retain source revision, dirty provenance, and collision identity. |
| Gold and measurement | `gold/`, `score/` | Preserve registry labels, action coordinates, split/exposure records, frozen cohorts, thresholds, and receipt hashes. |

Offline Line judges finished runs. Live watch observes running jobs.
Certification checks tasks and graders. Cached scoring measures existing outputs.
These functions do not share an interchangeable acceptance result.
The strict and historical certifiers retain distinct contracts.
See [Line](line.md), [watch](live-watch.md), [task quality](task-quality.md), and [score](score.md).

## Placement

`agents/STRUCTURE.md` owns repository placement. Package instructions define local invariants.

| Material | Location |
|---|---|
| Shared Pydantic contracts | `src/evallab/schemas/` |
| Storage paths, attach, compaction, backfill | `src/evallab/storage/` |
| Deterministic ATIF and fact extraction | `src/evallab/evidence/` |
| Interpretation, evidence packs, judgments, readiness | `src/evallab/interpretation/` |
| Recovery bundles and state recovery | `src/evallab/recovery/` |
| Runner, queue, quota, CLI | Existing top-level `src/evallab/*.py` modules |
| First-party tasks | `library/tasks/` |
| Pinned external benchmarks | `library/benchmarks/` |
| Experiment specs and analysis | `research/experiments/`, `research/analysis/`, `sql/` |
| Raw execution and durable results | Ignored `runs/`, results home, reviewed `research/evidence/` |
| Durable evidence CAS | `derived/evidence-cas/`; never a disposable cache |
| Projections | Configured Parquet root; resolve it with `storage.paths` |
| Tests and fixtures | `tests/`, `tests/fixtures/` |
| Maintained manuals | The 14 routes in [NOW](NOW.md) |

Do not introduce runtime database side effects into contract modules.
Do not move the CLI into the reserved `cli/` namespace without an assigned migration.
The top-level and interpretation TrajectoryIR modules have different persisted consumers.
Do not replace either with a facade.

## Terminology

| Term | Meaning |
|---|---|
| Experiment spec | A treatment plan, not an executed job directory. |
| Job | A Harbor execution unit containing 1 or more trials. |
| Trial | 1 task/agent attempt within a job; use native job/trial identity. |
| Run | An execution invocation or evidence directory. Qualify it when job and trial differ. |
| Trajectory IR | A deterministic structured representation, not raw `trajectory.json` or a model prompt. |
| Machine judgment | A diagnostic model result or deterministic abstention, not a governance decision. |
| Acceptance decision | The immutable policy result: `accepted`, `rejected`, or `abstained`. |
| Finding | A source-cited issue or anomaly, not a task reward. |
| Counts verdict | The canonical countability result, distinct from raw reward and agent exit. |
| Manifest | A qualified index: campaign analysis, evidence, compaction, task, export, or dependency manifest. |

Use qualified status names: execution state, quality status, readiness disposition, or contract status.
`TrajectoryQualityReport.is_analysis_ready`, `TrialReadinessRecord.is_analysis_ready`, and projected coverage are different carriers.
Do not use `TrialBundle` as a Python type. Use `TrialRecord` or “durable evidence directory”.
The `analyze batch` inventory argument takes a campaign analysis manifest.
Qualify “frozen”: immutable model, locked schema, blocked execution, pinned protocol, or frozen calibration labels.
None of these states implies another.

## Shared contracts

`schemas/` owns strict Pydantic contracts. Unknown fields fail validation.
`Suite` uses `(name, version)` identity. Once frozen, it rejects mutation.
The database also protects frozen suite membership against direct SQL and concurrent edits.

`AnalysisRecord` binds a trial, rubric digest, model, category, evidence citations, and confidence claim.
`ObservationRecord` follows `research/observations/TEMPLATE.md`.
`CalibrationRecord` records agreements, totals, rates, and corpus/rubric identity.
Human verdicts require an attributed actor and append history rather than replace it.
See [the judge guide](judge-guide.md).

`TaskRegistryRecord` carries task path, version/digests, controls, allowed uses, and contamination provenance.
`CapabilityCurveSpec` and its report require task-block pairing and a preregistered primary contrast.
A byte-identical comparison can still fail scientific compatibility.
Missing metrics, incompatible treatment identity, or incomplete pairs prevent a rankable primary contrast.
Descriptive outcomes remain available with the refusal.

### Experiment purpose

`ExperimentSpec` records purpose, hypothesis, question reference, elicitation, preregistration, power, task, agent, and model.
The purpose taxonomy belongs to Peter. Do not add a value to make a caller fit.
A mismatch is a finding, not permission to change the taxonomy.
A spec file cannot authorize itself. The queue ledger owns authorization.

Behavior tests protect validation, serialization, retained-spec replay, and authorization invalidation.
Do not replace those checks with copied schema or argparse dumps.
Legacy contract fixtures under `tests/fixtures/contracts/` remain versioned artifacts.
A deliberate contract change updates the specific affected contract and its rejection behavior.

## CRAFT task metadata

`craft.py` performs a deterministic, read-only scan of task directories.
It writes `CraftRecord` rows under the configured `craft/` Parquet root.
`sql/craft_views.sql` exposes the resulting facets.
The standalone entry is supported; there is no `evallab craft` root command.

```sh
uv run python -m evallab.craft scan --tb3
uv run python -m evallab.craft scan --tb4
uv run python -m evallab.craft scan path/to/corpus --json
uv run python -m evallab.craft plan --tb4 --tb4-root /absolute/path/to/v4
uv run python -m evallab.craft compile --tb4-root /absolute/path/to/v4
```

Use `--tb3-root` / `EVALLAB_TB3_ROOT`, `--tb4-root` / `EVALLAB_TB4_ROOT`, and `--out` for explicit roots.
The scanner refuses an output root inside a scanned corpus.
TB3 retains its `terminal-bench/terminal-bench` identity.
TB4 uses `terminal-bench/terminal-bench@4.0.0`, pinned to `v4.0.0`, commit `452bf30`.
The 74-task TB3 and 66-task TB4 inventories are not comparable score cohorts.
The migration record is `src/evallab/data/terminal-bench-4-migration.json`.

Compilation retains a flat 8-hour timeout, per-task resumable identity, and upstream drift guards.
`--include-task` selects tasks without weakening the full inventory digest check.
An older subset plan without full inventory digests fails rather than becoming a complete baseline.
Offline compilation does not qualify a runtime or authorize execution.

### Facet boundary

| Facet | Source and limit |
|---|---|
| `task_ref`, `source_repo` | Manifest/dataset identity, with the documented path fallback. |
| `version` | `[task].version`, never the manifest's `schema_version`. Missing stays null. |
| `task_digest` | Sorted inventory of paths, sizes, content digests, directories, and symlink targets. |
| `instruction_chars` | Character count, not bytes or difficulty. |
| `instruction_style`, `difficulty_mechanism` | Require semantic judgment; the deterministic scanner leaves them unresolved. |
| `env_n_files`, `env_languages` | Files and recognized extensions. An ambiguous `.v` needs a distinguishing sibling. |
| `env_services_n`, `env_multi_container` | Compose services, a lone Dockerfile, or null when unknown. |
| `verifier_signals`, `verifier_type` | Structural mechanism evidence; an unsupported mechanism does not receive an invented enum label. |
| `hidden_tests` | Observed separate-verifier declaration. |
| `answer_outside_image` | Entailed by the separate build context, not an independent leak measurement. |
| `digest_check`, `process_check` | Narrow imports, strings, and executable commands; `subprocess` alone proves neither. |
| `answer_hiding` | Observed mechanism, not a semantic description of the withheld answer. |
| `human_minutes` | Explicit expert hours × 60. Difficulty labels are not time estimates. |
| `pinned_deps` | True for all exact declarations, false for an unpinned declaration, null when no declaration exists. |
| `base_image_pin` | `digest`, `tag`, or `bare`; a tag is not an immutable pin. |
| `unresolved_facets` | Distinguishes undeterminable facets from absent mechanisms. |

Python verifiers use AST parsing. Shell verifiers use tokenized commands, not prose substring matches.
JS/TS mechanisms use parsed package metadata. Golden references must resolve to actual non-code verifier files.
Byte equality between environment and verifier trees can identify legitimate duplicated inputs, not necessarily an answer leak.

Dependency sites include lockfiles, exact requirements, and parsed installer commands.
A requirements-file argument delegates to that file. It is not an unpinned package name.
The base image has a separate facet.
The record schema is `craft/1`. Change its version when a facet meaning changes.

Unchanged digests produce no row churn. Rows key on `(source_repo, task_ref)`.
`records_digest` hashes canonical sorted records, independent of Parquet encoding.
The writer avoids replacing identical Parquet bytes.
The scanner neither executes a task nor certifies a verifier.
Historical corpus counts require their original digest; rescan before describing current coverage.

## Engineering and scaling boundaries

Use [the exact-head checks](../agents/CHECKS.md) for lint, types, tests, and delivery.
Measure performance against a named, fixed corpus. Do not loosen a limit because the live corpus grows.
Keep catalog/network time distinct from CPU projection time and task execution.
A passing synthetic smoke does not prove model capability, private Cloud ingestion, or scientific acceptance.

PostgreSQL owns the catalog. DuckDB reads local analytical projections.
Object storage, Kubernetes, another workflow engine, and ClickHouse require a named measured bottleneck.
Do not add them as speculative infrastructure.
A shared service also requires an owner, authorization, recovery procedure, and a demonstrated user.

## Source and receipt verification

An import audit establishes reachability, not runtime execution or scientific validity.
Include relative imports, function-local imports, re-exports, package initializers, and CLI handlers.
Distinguish runtime edges from `TYPE_CHECKING` edges and narrative references.
Inspect external configuration and computed imports before declaring a module unused.
Pin every inventory claim to its source revision and date.

A receipt supports a claim only when its source identity, parameters, denominator, and result can be checked.
Recompute the measured value from the named evidence rather than copying a report sentence.
Record discordant cases, missing coverage, dirty provenance, and served-model uncertainty.
The retired `verify-lane` frontend is not a current proof command.
Use the owning package's native checks and the base-owned exact-head Line gate.

The retired flight recorder does not remove native Harbor journals or stored event evidence.
A missing sensor record means unknown coverage, not evidence that no event occurred.
No documentation change authorizes service installation, removal, or an execution decision.

## Historical boundaries

Flight capture, the builder context compiler, optimizer engines, SFT producer commands, and retired report frontends are not supported routes.
Historical run identities, manifests, labels, releases, and receipts remain evidence.
Git history preserves removed design reviews and builder work orders.
Those documents do not grant current authority or establish current runtime health.

The architecture merges the old system tour, placement guide, glossary, contract guide, and CRAFT manual.
[Operations](operations.md) owns runtime recovery and quotas.
[Data architecture](data-architecture.md) owns storage, attach, lineage, and query contracts.
[The cache policy](GENERATED-CACHE-POLICY.md) owns retention and worktree retirement.
