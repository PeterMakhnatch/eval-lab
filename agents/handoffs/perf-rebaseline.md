Status: review-wanted
Last: re-baselined the `ingest` perf budget to 115.0 ms from 14 CI artifact samples, rewrote the budgets provenance note, and recorded the distribution in `docs/engineering.md`
Next: integrator reviews PR, waits for exact-head green on `perf`/`quality`/`typecheck`, then merges
Blockers: none

## Scope

Worktree `.worktrees/perf-rebaseline`, branch `role/perf-rebaseline` off
`origin/main` (`1471f41`). Wrote exactly three paths:

- `scripts/profile/budgets.json` — the `"ingest"` value and `"notes"` only.
- `docs/engineering.md` — one new dated subsection appended to §5.
- `agents/handoffs/perf-rebaseline.md` — this file.

Did not touch `src/`, `tests/`, `.github/workflows/`, `policy/`, or any other
path. `tolerance_pct` is unchanged at 50. The other five budgets are unchanged.

**`agents/STRUCTURE.md` needs no edit.** Confirmed: no new top-level entry is
created. `agents/handoffs/<role>.md` is already the declared location for a
role handoff (STRUCTURE.md line 25), and `scripts/` and `docs/` are existing
root entries. The root is untouched.

## The defect

The `profile` check failed on a docs-only PR (#52):

```
perf budget exceeded:
  ingest: median 125.239 ms exceeds budget 80.000 ms + 50% (ceiling 120.000 ms)
```

A rerun of the identical commit passed. The budgets were calibrated at ~3x an
Apple Silicon laptop capture with a local PostgreSQL (old note, line 11) and
are enforced on `ubuntu-latest` against a `postgres:18.4-alpine3.24` service
container. That premise, not the code, was the defect.

## Arithmetic

Evidence: `ingest` medians from the `speed-profile-report` artifact of the last
14 **successful** perf runs on `ubuntu-latest`.

| n | min | median | mean | stdev | max |
|---:|---:|---:|---:|---:|---:|
| 14 | 51.4 ms | 72.25 ms | 75.93 ms | 13.12 ms | **96.1 ms** |

Samples: 51.4, 66.9, 67.2, 67.4, 67.9, 68.9, 72.0, 72.5, 72.6, 85.2, 88.5,
91.3, 95.1, 96.1 ms. One further run measured **125.2 ms** and failed; the
rerun of that same commit passed, so 125.2 ms is the only direct observation of
the runner-variance tail (3.8 sigma against the successful sample).

Chosen budget: **`ingest` = 115.0 ms**.

- **Observed max (successful runs): 96.1 ms.** 115.0 / 96.1 = **1.20x**, so the
  budget itself sits above every successful sample — the metric is inside
  budget, not merely inside tolerance.
- **Resulting ceiling under the unchanged 50% tolerance: 115.0 x 1.5 =
  172.5 ms.** That is 1.79x the successful max and **1.38x the 125.2 ms
  variance spike**, so a repeat of the worst event ever observed passes.
- **Regression still caught:** the gate fails when the median exceeds 172.5 ms,
  i.e. **> 2.39x the current CI median of 72.25 ms**. A 2.4x regression fails.
  A 2x regression (~145 ms) passes — that is the explicit, deliberate cost of
  making the gate trustworthy, and it is recorded in the PR body.

Why not lower: 96–100 ms would leave the ceiling only ~1.15x above the one
observed spike, which re-arms the same flake on a single-sample tail estimate.
Why not higher: 130–140 ms pushes the detection threshold to 2.7–2.9x the CI
median for no evidential gain — nothing observed needs more than 172.5 ms.

Side effect, non-fatal: `check_budgets.maybe_rebaseline_notice` prints a
"consider re-baselining" line when a median falls under 50% of budget (57.5 ms
here). Of the 14 samples only the 51.4 ms run would trip it. It is a printed
notice, not a failure.

## Verification

- `uv run pytest tests/test_profile_harness.py` — **6 passed** (13.1 s).
  Confirmed rather than assumed: the tests build synthetic budget files under
  `tmp_path` (`tight`/`loose` in `test_check_budgets_fails_when_ceiling_exceeded`)
  and never read the committed `scripts/profile/budgets.json`.
- `scripts/profile/budgets.json` re-parsed with `json.load`; six paths present,
  `tolerance_pct` 50, ceiling 172.5 ms.
- **Gate behaviour proven live** by running the real
  `scripts/profile/check_budgets.py` against the committed budgets file with
  synthetic reports:

  | injected `ingest` median | expected | exit code |
  |---|---|---:|
  | 96.1 ms (observed successful max) | pass | 0 |
  | 125.2 ms (the variance spike that broke #52) | pass | 0 |
  | 145.0 ms (2x CI median) | pass | 0 |
  | 172.6 ms (just over ceiling) | fail | 1 |
  | 180.0 ms (2.5x CI median) | fail | 1 |

  The 172.6 ms case emits
  `ingest: median 172.600 ms exceeds budget 115.000 ms + 50% (ceiling 172.500 ms)`.

Capability label: **proven live** for the budget-checker behaviour above;
**pending in PR** for the `perf` workflow itself, which only runs on GitHub.
The 14-sample CI distribution is supplied evidence I did not re-collect from
the artifacts myself.

## Follow-up candidate — NOT part of this change

**`ingest` does not measure ingest.** `scripts/profile/harness.py:176-184`
(`_time_ingest`) calls `initialize(database_url)` *inside* the timed region:

```python
assert_not_shared_catalog(database_url)
initialize(database_url)          # full sql/schema.sql DDL replay, own connection
ingest(database_url, jobs, root=root)   # second fresh connection
```

`database.initialize` reads all of `sql/schema.sql` and executes it through its
own `psycopg.connect`; `database.ingest` opens a second one. So every one of
the 5 reps pays two TCP+auth handshakes plus a complete DDL replay against the
PostgreSQL catalog, and the corpus is only 2 job directories. The metric is
therefore dominated by connection setup and server-side catalog work — exactly
the quantity most sensitive to machine class — and the actual ingest logic is a
minority of the number. That is the mechanism behind both the 13 ms stdev and
the 125 ms spike.

**Recommendation: yes, a future mission should move `initialize()` (and ideally
the connection) outside the timed region**, leaving `ingest()` measured against
an already-initialized scratch database. That would make the metric mean what
its name says and should cut both its level and its variance substantially.

Two consequences that mission must own:
1. It **requires another re-baseline** — the number will drop sharply, and the
   115.0 ms budget would then be far too loose (it would immediately trip the
   `< 50% of budget` re-baseline notice). Per the rule added to
   `docs/engineering.md`, the new value must come from CI artifact samples.
2. It touches `scripts/profile/harness.py`, which is outside this mission's
   lease, and it changes what the number means, so the old and new `ingest`
   series are not comparable and the §5 tables must say so.

Not done here: this mission was scoped to the miscalibrated budget, and fixing
the harness would have conflated a gate re-calibration with a metric-definition
change in the same PR.

## Conflicts

None observed. No other live mission leases `scripts/profile/budgets.json` or
`docs/engineering.md`.
