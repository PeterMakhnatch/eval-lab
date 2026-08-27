# Analysis engine

This directory holds the declared inputs, bounded rubrics, query examples, and
fixture tests for the raw-evidence-to-analysis path. Harbor job and trial
directories remain canonical and read-only. Every generated table, comparison,
and analysis sidecar lives under ignored `derived/` and can be rebuilt or
re-indexed.

## Association path

Queued runs write `lab-metadata.json → experiment.spec_id`. Ingestion stores
that value as `jobs.experiment_id`; Harbor supplies job and trial UUIDs; each
trajectory document and analysis invocation references the source trial UUID.
The catalog view `experiment_trial_analysis_path` therefore exposes one path:

```text
experiments.id
  -> jobs.experiment_id
  -> trials.job_id
  -> trajectory_documents.trial_id
  -> analysis_invocations.source_trial_id
```

Legacy direct controls predate queued provenance. Their `experiment_id` remains
null rather than being guessed, and the tracked comparison spec supplies a
report-level experiment label. The reviewed event-summary controls also use
Oracle/no-op adapters, which do not emit ATIF; their zero-trajectory state is a
fact, not an ingestion error.

## Raw facts and Parquet

Inspect without PostgreSQL or writing derived data:

```bash
uv run evallab trajectories runs research/evidence/runs evidence/runs
```

Rebuild partitioned Parquet from raw jobs:

```bash
uv run evallab trajectories --export \
  --output-dir derived/parquet \
  runs research/evidence/runs evidence/runs
```

The stable partitions are
`derived/parquet/job_id=<uuid>/trial_id=<uuid>/`. Each partition contains:

- `trajectories.parquet`: document identity, source path/digest, validation,
  schema/session IDs, aggregate steps/tokens/cost;
- `steps.parquet`: copied-context flag, source/timestamp, LLM calls,
  per-step token/cost counts, tool/observation counts;
- `tool_calls.parquet`: call ID, function name, and arguments digest;
- `observations.parquet`: source call ID, content byte count/digest, subagent
  reference digest, and structured command exit code when present;
- `trial_facts.parquet`: rewards/exception class, phase durations, token/cost,
  trajectory/tool/failure counts, and artifact-set digest;
- normalized `reward_facts.parquet`, `artifact_facts.parquet`, and
  `tool_usage.parquet`.

Prompts, reasoning, tool arguments, and observation content are never copied
into Parquet or PostgreSQL. Full values remain only in canonical raw ATIF.
Re-running the exporter replaces only the matching derived table file and
produces identical rows and file digests for identical raw bytes.

Harbor 0.21.0 is installed as an isolated `uv tool`, not as a project import.
The parser uses `harbor.models.trajectories.Trajectory` when importable;
otherwise it applies the recorded strict ATIF-v1.0–v1.7 structural fallback and
records `validator=internal-atif-v1`. Unsupported versions, malformed JSON,
broken external references, and paths escaping a trial are status rows rather
than agent failures. External URL/object-store subagent references are not
resolved by the local projector.

Example DuckDB queries are in [queries.sql](queries.sql).

## Cohort comparison

The checked-in control declaration is
[control-oracle-vs-nop.json](control-oracle-vs-nop.json):

```bash
uv run evallab compare research/analysis/control-oracle-vs-nop.json
```

Add `--index` only when the declared spec should become the durable association
for legacy jobs that lack `lab-metadata.experiment`; an existing different job
association is refused rather than overwritten.

JSON is the sole calculation path; Markdown renders that JSON. Reports land in
`derived/comparisons/`. A causal comparison always holds task and verifier
digests fixed and refuses every consequential difference except
`declared_variable`. Exploratory mode emits the same mismatches as machine-
readable validity warnings.

Exceptions remain in `n_total` and the exception breakdown but are excluded
from `capability_denominator`. Missing rewards are separate exclusions.
Realized `pass_any_first_k` and `pass_all_first_k` group attempts by task and
select the first `k` eligible trials by timezone-aware Harbor `started_at`, never
by trial UUID chronology. A missing/invalid timestamp or a timestamp tie across
the selection boundary makes that task unavailable for realized first-k. Attempts
from one task are one evidence unit. Percentile 95% intervals resample tasks,
never attempts. Groups with fewer than `k` eligible attempts are listed rather
than silently changing the denominator. `pass_at_k_unbiased` (Chen) and
`pass_power_k_unbiased` (Yao/tau) use all eligible attempts per task with no
attempt order, then average task-level estimates rather than pooling attempts.

Every two-cohort decision is paired by task. A report prints a ranking only
when it can name `n_tasks`, `k`, the paired task-bootstrap interval, and a
uniform elicitation tuple for each cohort (agent version, model pin, preamble
hash, configured toolset, and `k`), and the interval excludes zero. Otherwise
it prints `not distinguishable / not comparable` with the reasons. Exploratory
summaries remain available, but validity warnings prohibit a ranking.

Plan an experiment before spending:

```bash
# Minimum detectable per-attempt difference for a fixed paired-task design.
uv run evallab power --n-tasks 100 --k 3 --baseline 0.30

# Required paired tasks and total attempts across candidate k values.
uv run evallab power --target 0.15 --baseline 0.30 --max-k 8
```

The planner transforms per-attempt rates to pass@k under an explicit independent-
attempt planning assumption, then uses a documented normal approximation for
paired task outcomes. Its between-cohort task-pair correlation assumption is
explicit and has a neutral default of zero. Empirical comparisons still
cluster attempts within each task; the planning assumption never changes the
analysis evidence unit.

## Trajectory family reports and eval cards

After rebuilding Parquet, render one plain-language task-family report:

```bash
uv run evallab report family event-summary
```

The report joins task/cost/step facts from Parquet to canonical raw ATIF. It
shows the first structured command-failure step distribution, repeated identical
tool-call loop heuristic, recognizable verification before completion, and
cost/step summaries. The heuristic boundaries are printed alongside the result.

Draft an eval card only after its spec has a completed Harbor job:

```bash
uv run evallab report card queue/done/<completed-spec>.json
```

The command fills `research/cards/TEMPLATE.md`, refuses incomplete jobs and
overwrites, and records config/lock digests, task-bootstrap numbers, elicitation,
an unresolved contamination note, and observed threats. Drafts require human
review before publication.

## Stage-5 sidecars

Dry-run planning never calls a model:

```bash
uv run evallab analyze plan \
  research/evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq
```

Fixture/stub validation writes a new immutable sidecar and can index it:

```bash
uv run evallab analyze stub \
  research/evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq \
  --response research/analysis/stub-oracle-analysis.json \
  --index
```

Each invocation gets a fresh UUID under `derived/analyses/<uuid>/analysis.json`.
It records raw source digests, the exact rendered prompt/rubric/schema digests,
agent/model/token/cost provenance, structured output, and evidence-validation
status. ATIF citations require a resolvable step ID; cited tool-call IDs must
exist at that step. Schema output gets one validation retry. A human review is
appended as `reviews/<uuid>.json`; it never edits the model output.

### Committed saved responses

Two hand-authored saved responses are tracked here. Neither is a model output;
both exist so the stage-5 path can be exercised without a model call. Each
produces `agent: stub`, `model: saved-response`, `cost_usd: 0.0` in the sidecar
provenance, and its findings are **not** reviewed research ground truth.

| File | Source trial | Cites |
|---|---|---|
| `stub-oracle-analysis.json` | `event-summary-oracle-evidence/event-summary__FZg7pvq` | `verifier/reward.json` only — that control has no ATIF trajectory |
| `stub-codex-html-js-filter-analysis.json` | `canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt` | five ATIF citations into real Codex agent behaviour plus `verifier/ctrf.redacted.json` |

`stub-codex-html-js-filter-analysis.json`
(`sha256:c7d00390f0a5b08a6dc8b1c1b33873e07ed0b2d6a81bc66fded3046722a3dbca`) was
written by the TrajectoryProof mission on 2026-08-16 to prove that citations into
a real trajectory resolve end to end. Its `step_id` and `tool_call_id` values were
read from the promoted ATIF
(`sha256:f112fc244724a9b9ac6e1d7658bceff259670a14564f1f5c948d1cfd2d3075a2`).
Citation `[4]` deliberately targets step 1, a system-source step whose text is
redacted under promotion rule R1; it resolves on every surface and yields no
readable text, which is the point. Full measurement, per-hop verdicts, and the
exit status of every command are in
`docs/checkpoints/2026-08-16-trajectory-hop-proof.md`.

Reproduce its sidecar (fresh `analysis_id` and `created_at`; every other field is
a function of the committed bytes):

```bash
uv run evallab analyze stub \
  research/evidence/runs/canary-terminal-bench-html-js-filter-codex-20260815/terminal-bench-html-js-filter__5rgjEEt \
  --response research/analysis/stub-codex-html-js-filter-analysis.json \
  --output-dir derived/analyses
```

From a linked worktree, pass `--derived-dir` / `--output-dir` explicitly to
`evallab ingest` and `evallab trajectories --export`. Their default Parquet root
is the **primary** checkout (`paths.py`, `shared_checkout_root`) so worktrees
share one derived store with the shared catalog; a worktree writer that omits the
flag writes outside its own tree.

Harbor 0.21.0's `harbor analyze` assembles a useful read-only copy for its
evaluator, but then writes `analysis.json` back into the analyzed source trial.
That conflicts with this lab's immutable evidence contract. The lab therefore
uses its own Pydantic sidecar contract and a headless `codex exec` adapter. The
adapter has no direct CLI execution route: it revalidates a matching
`queue/running` authorization with `policy_rule=researcher-followups` at every
call and caps schema retry plus initial call at two. No live model was invoked
for this implementation.

Compare completed valid sidecars with the fixed calibration labels without
altering either source:

```bash
uv run evallab analyze agreement derived/analyses \
  --labels research/calibration/trajectory-labels
```

The deterministic JSON report records source-file digests, exact category
agreement, valid-sidecar label coverage, unmatched analyses, and labels lacking
a valid analysis. Sidecars marked invalid are reported but excluded from both
the agreement and coverage denominators.

## Verification

```bash
uv run pytest -q
uv run pytest -q research/analysis/tests
uv run ruff check src/evallab/evidence/atif.py src/evallab/evidence/facts.py \
  src/evallab/cohort.py src/evallab/schemas/__init__.py src/evallab/cli.py \
  research/analysis/tests
```

The focused suite rebuilds from synthetic raw evidence, queries Parquet through
DuckDB, confirms source bytes are unchanged, exercises causal refusals and
exception denominators, and validates/indexes a saved analysis response.
