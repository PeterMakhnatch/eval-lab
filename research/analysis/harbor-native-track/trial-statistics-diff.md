# Task 1 — deepagents-equivalent stats vs traj.py on the same 27 bytes

Date: 2026-09-06. Corpus: `runs/canary-*/**/agent/trajectory.json` (23) +
`runs/funcdag-codex-canary/**/agent/trajectory.json` (4). All 27 validate as
ATIF-v1.7 with full per-step metrics. Evidence: `evidence/s1_out.json`.

## Premise correction (recorded, not hidden)

`libs/harbor/scripts/analyze.py` is **404 on deepagents main**; the vendor PIN
already records that path stale. The live equivalents (`stats.py` + `failure.py`,
MIT, same pin `07d2952`) are vendored at
`research/external/harbor-ecosystem/vendor/deepagents-harbor/`. This task's
"deepagents side" = `wilson_ci` + `classify_failure(exception_text, exit_codes)`
called exactly per their signatures (keyword-only; a positional call raises
TypeError — found during this work). `traj.py` side = `outline_trajectory` +
`extract_features` in-process.

Reproduce: `uv run python /tmp/hn-track/s1_diff.py` (throwaway; logic below is
self-contained). Note: with **kwargs misuse `classify_failure` raises; the
corrected call passes `exception_text` from `exception.txt` (preferred) or
`result.json:exception_info`, and `exit_codes` from `extract_exit_codes(raw)`.

## Agreement table (n=27: 13×reward 1.0, 13×reward 0.0, 1×reward null)

| Quantity | deepagents-side | traj.py side | Delta | Cause |
|---|---|---:|---|---|
| Successes | 13/27 | 13/27 (primary_reward) | 0 | same `result.json` reward read |
| Wilson 95% overall | 13/27 = 48.1% [0.306, 0.660] | identical function | 0 | same code path |
| Prompt tokens sum | 2,795,536 | 2,795,536 | 0 | exact |
| Cost sum | $2.013526 | $2.013527 | −6e-07 | **rounding only**: `TrajectoryFeatures.cost_usd` rounds per-trial to 6dp; `final_metrics` keeps full float. 14/27 trials differ at the 7th decimal. Immaterial at $2 scale. |
| Completion tokens | — (not emitted) | 81,340 | n/a | deepagents stats has no token-sum helper; not a divergence |
| Failure classes (14 failed) | 7×UNKNOWN, 7×CAPABILITY | n/a (traj has no failure taxonomy) | n/a | complementary, not comparable. UNKNOWN = exception present but no OOM/timeout/sandbox pattern (all 6 r2 harness crashes + Az2rApj verifier crash). CAPABILITY = no exception, reward 0 (6× html-js-filter + funcdag-hard). |
| Min detectable effect (n=27) | 0.267 (vendored `min_detectable_effect`) | — | — | corpus cannot resolve effects below ~27 points. Interviewer-facing bound. |

Per-job Wilson table is in `evidence/s1_out.json:per_job` (9 jobs; e.g. html-js-filter
0815: 0/3 = 0.0% [0.0, 0.562]; event-summary 0815: 3/3 = 100% [0.438, 1.0]).

## DuckDB store vs same bytes (the real finding)

`derived/parquet/traj_features` (hive store) vs the 27 files on disk:

| Check | Value |
|---|---:|
| Store rows for these jobs | 59 |
| Distinct trial names in store | 41 |
| On-disk trajectories | 27 |
| Naive `sum(cost_usd)` over the store | **$3.77 (+87% over true $2.01)** |
| Cause 1: duplicate `featured` rows per trial (reprojection history; 9 trials have 2–3 rows with different `source_sha256`) | — |
| Cause 2: 17 store names not on disk (job-dir junk rows `trials`, `trials-hard`, `trials-medium`; `accounted_unavailable` rows for no-trajectory trials — correctly zeroed) | — |
| Cause 3: **3 on-disk trials absent from the store** (`adapted-syn-funcdag-hard__kziNARo`, `adapted-syn-funcdag-medium__NoqKuag`, `adapted-task__PmTen2E` — the `trials*/` subdir shape; stale projection) | — |
| Deduped-to-current-bytes featured cost | $1.88 (under-counts: missing the 3 absent trials) |

Rule for anyone querying the store: filter `status='featured'`, dedupe to the row
whose `source_sha256` matches current file bytes, and know 3 trials are absent.
Reproduce: `uv run python -c` snippets in task log 2026-09-06 (DuckDB
`read_parquet('derived/parquet/traj_features', union_by_name=true)`).

## Unestablished

- Whether `analyze.py` exists at a different deepagents path (searched main only).
- Store staleness policy: no `projected_at` column was checked; dedupe rule above is
  byte-matching, not timestamp-based.
