# Harbor-native track — versioned DuckDB query pack (v1.0.0, 2026-09-08)

Read-only analysis queries over existing evidence. No query writes, projects,
or modifies anything: every file is a single `SELECT` over `read_json`,
`glob`, or `read_parquet`, executed in an in-memory database.

## Corpus scope (all four queries)

- Runs: `runs/canary-*/**/result.json` + `runs/funcdag-codex-canary/**/result.json`,
  trial-level rows only (`trial_name IS NOT NULL`, which drops job-aggregate
  result.json files and nested verifier/artifact payloads).
- Store: `derived/parquet/traj_features` with `job_name LIKE 'canary-%'` or
  `= 'funcdag-codex-canary'`. The `canary-` PREFIX is load-bearing: bare
  `%canary%` would match `tau_canary*` / `zai-wave2-*` rows outside this pack.
- Coverage export: `derived/harbor-traces/atif-coverage.json`, same job filter.
- To re-scope: extend the glob lists (01/02/04) and the `LIKE` predicates
  (03/04) together, or gap reasons become scope artifacts.

## Store read rule (applies to 03; background for 04)

`trial-statistics-diff.md` finding: naive `sum(cost_usd)` over the store
overcounts **+87%** on the 27-trial corpus (duplicate `featured` rows from
reprojection history + job-dir junk + stale checkout paths). Rule: filter
`status='featured'`, dedupe to the row whose `source_sha256` matches current
file bytes. Pure SQL cannot hash live bytes, so 03 keeps one deterministic
row per `(job_name, trial_name)` and exposes `n_featured_rows` /
`n_distinct_sha`; byte-exact work means `sha256sum` the live trajectory file
and keeping the matching row.

## Queries

### 01_trial_stats_per_job.sql
- Purpose: per-job trial statistics with explicit scored/unscored split and
  Wilson 95% bounds.
- Denominator: `n_scored` = reward `IS NOT NULL`. `success_rate_scored` =
  `n_success / NULLIF(n_scored, 0)` → `NULL` (not 0) when nothing scored;
  Wilson bounds are over `n_scored`, `NULL` on zero scored. `n_scored`
  includes reward-0-with-crash rows (reward present); the termination filter
  that excludes them lives in 03 — use `n_with_exception` to reconcile.
- Run: `uv run --with duckdb python -c "import duckdb; cur=duckdb.connect().execute(open('research/analysis/harbor-native-track/queries/01_trial_stats_per_job.sql').read()); print([d[0] for d in cur.description]); [print(r) for r in cur.fetchall()]"` (from repo root)

### 02_cost_tokens.sql
- Purpose: cost/token accounting per job plus an `ALL_JOBS` rollup
  (`GROUPING SETS`), from trial-level `agent_result` fields.
- Denominator: `n_costed` = `cost_usd IS NOT NULL` (crashed trials record
  `NULL` cost/tokens and are ignored by sums, not zero-filled). All means and
  `cost_usd_per_success` use `NULLIF` → `NULL` on zero denominator. Rows for
  fully-crashed jobs (e.g. `canary-*-20260814`) are all-`NULL` measures, which
  is the truthful empty, not a gap.
- Run: same pattern with `02_cost_tokens.sql`.

### 03_failure_cohorts.sql
- Purpose: failure cohorts by exception class + termination-filter columns
  (`max_exit_code_cascade_screening`, `unrecovered_at_terminal`,
  `is_expected_negative`), with the capability denominator enforced
  mechanically: `capability_failure` requires `exception_class IS NULL`, so
  reward-0-with-crash rows (`NonZeroAgentExitCodeError`) can never enter it.
- Denominator: capability set = `success` + `capability_failure` cohorts
  (scored AND exception-free). `unscored_crash` (reward `NULL`) is in no
  capability denominator. `n_hidden_duplicate_rows` shows how many extra
  featured rows the read-rule dedupe removed per cohort.
- Run: same pattern with `03_failure_cohorts.sql`.

### 04_coverage_reconciliation.sql
- Purpose: reconcile four stages per `(job, trial)` — runs_present (trial
  result.json) vs traj_present (`glob` file presence, contents never parsed)
  vs ingested (atif-coverage.json paths) vs projected (store keys, any
  status) — with one exact mechanical `gap_reason` per off-diagonal row.
- Denominator: each stage count is over the union of keys; reasons are
  priority-ordered in the `CASE` (`accounted_unavailable_no_trajectory`
  first, so correctly-zeroed crash rows never read as gaps).
- Run: same pattern with `04_coverage_reconciliation.sql`.

## Validation (executed 2026-09-08, DuckDB v1.5.5, read-only, repo root)

| Query | Result on real evidence |
|---|---|
| 01 | 12 rows (11 canary jobs + funcdag). Fully-crashed 0814 jobs: 0 scored, rate `NULL`. `canary-transaction-reconciliation-codex-20260816`: 2/3 scored (1 unscored crash). funcdag: 5 trials, 3 scored, rate 0.667. |
| 02 | 13 rows (12 jobs + `ALL_JOBS`). `ALL_JOBS` prompt sum 2,795,536 reproduces `evidence/s1_out.json` overall_prompt exactly (crash trials contribute `NULL`, ignored). Fully-crashed jobs truthfully all-`NULL`; zero-success jobs show `cost_usd_per_success = NULL`. |
| 03 | 4 rows over 27 deduped trials: 13 success, 7 capability_failure, 6 crash_with_reward (`NonZeroAgentExitCodeError`, reward 0, cost 0.0), 1 unscored_crash (`ValueError`). 18 hidden duplicate featured rows exposed (12 success + 6 capability). |
| 04 | 41 keys: 24 `complete`, 14 `accounted_unavailable_no_trajectory` (9 canary-0814 crashes + 1 tb-recon-0816 trial + 4 funcdag junk/unavailable names incl. bare `adapted-*` names with no disk trial), 3 `not_in_atif_coverage` (the `trials*/`-shape funcdag trials: present, trajectoried, projected, but absent from the coverage export). Zero `store_only_featured_orphan`, zero `missing_trajectory_file`. |

Note vs `trial-statistics-diff.md`: the 3 `trials*/` trials it recorded as
absent from the store are now projected (store re-ran since 2026-09-06);
04 reports the live state, including that their remaining gap is coverage-
export membership, not projection.

## Version log

- v1.0.0 (2026-09-08): initial pack. One single-`SELECT` statement per file;
  corpus-prefix scoping (`canary-`, not `%canary%`); `NULLIF`/`NULL` (never 0)
  on every zero denominator; run commands verified without pandas.
