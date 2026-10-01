---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Unified Attach Surface (E04)

The single mandated entry point for all consumers of the three storage zones.

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

Access is exclusively via the unified attach surface: `evallab db attach`.

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
detector measurements require existing, unambiguously matched trial-feature
Parquet; a missing metric is not a no-edit result. Post-edit shares use a
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
