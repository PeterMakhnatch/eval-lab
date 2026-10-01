# Schema and analytical SQL views

## Purpose
`sql/` defines the relational schema for PostgreSQL and portable SQL views for DuckDB.
It powers the derived search and indexing layer over evaluated runs, trials,
and analytical features.

## What lives here / entry points
- `sql/schema.sql`: Primary PostgreSQL schema definition (tables, indexes, constraints).
- `sql/views.sql`: Core join spine (`v_spine`) and quota observation views.
- `sql/evidence_queries.sql`: Analytical queries over evidence facts and failure classifications.
- `sql/traj_benchmark_views.sql`: Analytical views for benchmark trajectory programs.
- `sql/craft_views.sql`: Analytical views for CRAFT benchmark facets.
- `sql/trace_queries.sql`: Ten canonical analytical views over the transient in-memory trace surface (`v_trace_trials` and `v_trace_steps`), plus private interpretation views and typed JSON macros.
- `sql/trace-queries/`: Runnable saved-query handles (`SELECT * FROM` the canonical view), not duplicate definitions.
## Invariants or rules
- Idempotent schema additions: Add schema changes idempotently to `sql/schema.sql` (cited in `AGENTS.md`).
- Derived search/index layer: PostgreSQL is a derived search/index layer; Harbor job directories are the immutable source of truth and must remain interpretable without the database (cited in `AGENTS.md`).
- Core DuckDB view files use empty-session fallback definitions where their existing contract requires them. Trace views instead require the declared Arrow-backed `v_trace_trials` and `v_trace_steps` attachment; never invent empty traces to hide a missing attachment.
- Trace identity is the native `(job_id, trial_id)` pair. Real steps are aggregated before joining trial populations; absent steps do not remove a trial from coverage denominators.
- Counts is the sole countability authority. Missing or invalid counts verdicts remain unknown. Raw scored pass rates and all-attempt pass yields have different explicit denominators; unscored attempts are not failures.
- JSON signals use actual producer field paths and types, not substring mentions. Detector findings, classifier opinions, frozen labels, recorded actions and counts verdicts are distinct evidence layers.
- First-edit measurements reuse unambiguously matched existing `traj_features` Parquet or the same read-only outline producer used by `process-job` when no feature is stored. Native identities and current ATIF hashes bind the result; stale/conflicting stored features remain unknown. An absent measurement is unknown; a measured no-signal is not proof that no edit occurred. Neither first nor last edit signals prove persistence or useful work.
- Post-edit shares recompute numerator and denominator from complete, ordinal-matched native `token_flow.prompt_series` metrics, separately for input and output. Proxy totals remain separate; no mixed native/proxy or step/agent-result ratio is used.
- Parse-shape counts are not harness-rejection counts. Acceptance/provenance marginals do not reveal all recorded rejected steps; `agreement.agree_reject` is the directly observed recorded-rejection/agreeing-observation intersection, not a complete rejection count. Missing measurements stay unknown.
- Loop alignment requires valid, unanimous top-level HAR-119 loop-kind labels from at least two distinct raters in one unambiguous cohort. Disagreement, insufficient/invalid labels and prediction abstentions are separate; `eligibleN` is the accuracy denominator. HAR-109 hand labels and HAR-128 pass-cleanliness adjudications are coverage only, not loop truth.
- Exemplar selection is deterministic, not representative sampling or causal attribution. Opinion anchors get source paths and hashes only when their exact ref resolves uniquely to a real native-pair ATIF step; ordinary passes are not failures, and task usability is not pass taint.

## Tests or checks
- `uv run pytest tests/test_z2_tables.py`
- `uv run pytest tests/test_evidence_queries.py`
- `uv run pytest tests/test_canary.py -k test_schema`
- `uv run pytest tests/test_trace_query.py`
- `uv run pytest tests/test_trace_query_semantics.py` (synthetic behavioral boundaries; not evidence of actual-run query results)
## What not to add here
- Do not store raw database dumps, snapshots, or Parquet binary files here; use `derived/`.
- Do not add destructive DDL without explicit migration policies.
- Do not store application queries or business logic that belongs in Python modules under `src/evallab/`.
