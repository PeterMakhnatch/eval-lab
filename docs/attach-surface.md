---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Unified Attach Surface (E04)

The unified entry point for cross-zone consumers, with local trial and trace query modes.

## Zones

- **z2**: PostgreSQL catalog via `postgres_scanner`. Tables mirror the canonical entities (§2.1). Unavailable when `DATABASE_URL` cannot be reached; reason includes the DSN identity examined.
- **z3**: Parquet analytics. Views `trial_facts`, `reward_facts`, `artifact_facts`, `trajectories`, `steps`, `tool_calls`, `tool_usage`, `observations`, `jobs`. Hot layout `job_id=*/trial_id=*/<table>.parquet`; cold `compact/<table>/dt=YYYY-MM-DD/part*.parquet`. Uses `union_by_name=true`. Unavailable when derived root does not exist.
- **z4**: Knowledge front-matter. Table `front_matter` with columns `path`, `title`, `status`, `audience`, `generated_by`. Populated via `contextpack.parse_doc`. Unavailable when `docs/` missing.

## Python usage

```python
from evallab.storage.attach import attach
result = attach()
print(result.zones)
rows = result.connection.execute("SELECT * FROM z3.trial_facts LIMIT 5").fetchall()
result.connection.close()
```

## Bare duckdb shell

```sql
INSTALL postgres_scanner;
LOAD postgres_scanner;
ATTACH 'postgresql://...' AS z2 (TYPE postgres);
CREATE SCHEMA IF NOT EXISTS z3;
CREATE SCHEMA IF NOT EXISTS z4;
CREATE OR REPLACE VIEW z3.trial_facts AS SELECT * FROM read_parquet([...], union_by_name=true);
...
```

## Unavailable vs empty

A zone reports `attached=False` with explicit `reason` and the path/DSN examined. A silent empty view is never produced.

## Cross-zone example (Z2 + Z3)

```sql
SELECT j.job_name, COUNT(*) AS trials
FROM z2.jobs j
JOIN z3.trial_facts t ON j.id = t.job_id
GROUP BY j.job_name;
```

When Postgres is unavailable the surface still returns a usable connection carrying Z3 and Z4; the join is skipped with explicit reason naming the DSN.
## CLI

Cross-zone access uses `evallab db attach`. The local `evallab trials` census below needs no catalog.

```sh
$ uv run evallab db attach --zones
z2: attached (localhost:54329/evallab)
z3: attached (/Users/.../derived/parquet (9/9 tables))
z4: attached (/Users/.../docs)

$ uv run evallab db attach --print-sql | head -20
INSTALL postgres_scanner;
LOAD postgres_scanner;
ATTACH 'postgresql://...' AS z2 (TYPE postgres);
CREATE SCHEMA IF NOT EXISTS z3;
CREATE SCHEMA IF NOT EXISTS z4;
CREATE OR REPLACE VIEW trial_facts AS SELECT * FROM read_parquet([...], union_by_name=true);
...

$ uv run evallab db attach --query "select count(*) from trial_facts"
[(92,)]
```

Cross-zone query (Z2 + Z3) demonstrating the surface:

```sh
$ uv run evallab db attach --query "SELECT j.job_name, COUNT(*) AS trials FROM z2.jobs j JOIN z3.trial_facts t ON j.id = t.job_id GROUP BY j.job_name;"
[('smoke-oracle-8ya566yyqwms', 1), ...]
```


### Trials Census (HAR-178)

```sh
uv run evallab trials
uv run evallab trials --sql "SELECT campaign, legit, count(*) AS trials FROM trials GROUP BY campaign, legit ORDER BY campaign, legit"
uv run evallab trials --sql "SELECT job, trial, reward, integrity, reward_gated, copy_verdict FROM trials WHERE campaign = 'HAR-168' ORDER BY job, trial"
# Explicitly restrict discovery, instead of scanning all local run roots:
uv run evallab trials --runs-dir .worktrees/har164/runs
```

`trials` is one transient DuckDB view, not another database or stored census.
Default discovery covers the primary checkout and every `.worktrees/*` checkout:
`runs/`, `jobs/`, and the existing reviewed evidence run roots. A configured
`EVALLAB_RUNS_ROOT` adds a source; it does not silently hide other lanes.
Dot-prefixed executor/staging/cache directories are not separate evaluations.
Physical copies and retained viewer aggregates do not multiply a native
`(job_id, trial_id)`. A trial's recorded `config.job_id` takes precedence over a
viewer aggregate's parent ID. Unfinished and infrastructure trials remain rows;
unavailable native identities stay null rather than becoming invented UUIDs.

The view joins existing `trial_facts`, `reward_facts`, and HAR-159 `features`
Parquet by native identity. Historical worktree-local projections can be read;
backfills write only to the selected existing Parquet store. Missing projections
run the local `process-job` producer with catalog ingest and publication disabled,
using temporary report output so existing processed reports and post-session spend
allocations are not replaced. The native raw files are not rewritten. No provider,
queue, sandbox, or Laminar API call is made. Projection failures remain visible in
`projection_error`, never disappear from the census.

For an explicit manual projection:

```sh
uv run evallab process-job runs/JOB --no-ingest --no-publish --parquet-root derived/parquet
```

This writes canonical `job_id=*/trial_id=*/` fact/trace Parquet and the existing
HAR-159 feature projection (`features.parquet` plus its CSV sibling). The manual
command retains ordinary `process-job` report-output behavior; use `--output-dir`
when preserving an existing report.

Columns include native IDs and source paths, `campaign`, `job`, `trial`, `task`,
recorded `date`, `model`, `harness` (agent import path), observed `egress_lock`,
`reference_profile`, `reference_profile_match`, `reference_profile_diffs`,
`stop_reason`, `cut_short_by_our_limits`, `infra`, `infra_exception`, `reward`,
`integrity`, `reward_gated`, `copy_verdict`, `laminar_trace_id`, and `legit`.
Reward dimensions are projected as recorded, not recomputed. `copy_verdict` is
the existing HAR-159 copy-check/counts verdict, not a fresh detector label.

`legit` requires exactly the native MiMo-V2.6-Distill-Qwen-9B model, native
`mimoagent`, an applied egress lock, a HAR-149 reference-profile match, no canonical
counts infrastructure exclusion, and no HAR-156 `our_limit` stop. Unknown
required evidence does not establish legitimacy. HAR-156 distinguishes our
ceilings/loop breaks from the native harness step cap and Harbor task timeout.
Reward positivity, integrity, and copy verdict **do not enter this predicate**.
An approved deviation is still a reference difference, not an exact match;
in particular, HAR-168's declared context/parser/harness deviations must not be
relabeled as a matching training setup. Its 12-scored/1-infrastructure receipt
is an exact native-ID cohort, not every later trial carrying the HAR-168 card.

### Trace Query Mode (HAR-131)

Trace mode queries existing Harbor cohorts in memory without PostgreSQL or
network dependencies. The commands below are invocation examples, not receipts
for an executed cohort or its coverage:

```sh
uv run evallab db attach --trace --zones
uv run evallab db attach --trace --query "SELECT card, arm, n_total, n_scored, n_unscored, raw_pass_rate_all_attempts, raw_pass_rate_scored, n_counts_unknown FROM v_trace_cohort_raw"

# Restrict the attachment to a specific existing job directory:
uv run evallab db attach --trace-job-dir ~/Developer/eval-lab-results/2026-10-01/HAR-116-har116-b-002308-original --zones
```

Trace mode exposes `v_trace_trials`, real ATIF-only `v_trace_steps`, and ten
canonical views defined once in `sql/trace_queries.sql`; files under
`sql/trace-queries/` select those views. Native identity is `(job_id, trial_id)`.
Counts alone decides countability: absent or invalid verdicts are unknown.
Scored raw pass rates exclude unscored attempts; all-attempt pass yields do not
silently recode unscored attempts as failures.

Coverage separates trajectory-file existence, actual observed steps,
processed/counts availability and frozen-label availability. First-edit
detector measurements reuse source-hash-matched `traj_features` Parquet. When
no stored feature exists, the existing read-only `traj.outline_trajectory`
producer computes it from the explicitly scoped trial directory without writing
a store. Stale or conflicting stored values remain unknown. A missing metric
is not a no-edit result. Post-edit shares use a
complete ordinal-matched native step-metric population for both numerator and
denominator, independently for input/output, never settled proxy totals or
mixed-source producer shares. Edit signals do not prove persisted useful work.

Shape classification, acceptance counts/provenance, recorded rejection
agreement and rejection-cause classification are separate evidence. Acceptance
and provenance marginals cannot reveal the full recorded-rejection count.
Decision loop predictions and first-failure anchors are opinions, not recorded
actions or countability. Frozen-loop accuracy requires at least two distinct
valid agreeing HAR-119 raters, one unambiguous cohort, and a recognized
prediction; disagreement and prediction abstention remain separate.
HAR-109 hand labels and HAR-128 pass-cleanliness labels provide coverage only,
not compatible multi-rater loop ground truth. Exemplar paths/refs/hashes link
only uniquely resolved actual ATIF steps and retain opinion provenance.
See `sql/README.md` for the detailed query contracts and focused check commands.
`--zones` exits non-zero only when zero zones attach.
