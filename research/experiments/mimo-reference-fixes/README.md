# MiMo reference fixes (resume sweep, 2026-10-09)

Resume of the HAR-191 history-oracle sweep for the tasks it left unclassified,
plus publication of a reference-fix index for the clean chain's
`separate-verifier@2 (+ solution/solve.sh ... when one exists)` step.

## Spend approval and fences (MEASURED)

- Slice approval: actor `omp-mimo-clean (delegate for Peter; 2026-10-09 chat:
  "feel free to spend up to $25 on stuff")`. Slice cap: **$4.00 total**.
- Pre-launch gate: `uv run --extra laminar evallab spend day --date 2026-10-09`
  in this worktree returned **grand total $0.0000** (standing policy cap $20.00;
  Modal billing rows for the day absent; current-day data partial).
- Replay code: reused **unmodified** from `research/experiments/leak-oracle/`
  at `origin/main` (`f3829ce9f`, PR #765; sha16: sweep 204c57485884e4b4,
  runtime e57c7e3dc38a2bd8, extract 6625285082fca074, budget c03c45f52a175ea0,
  results 759392224f3d569b, prepare c10286e54571589f). Its built-in
  batch-enforced **$2.00-per-study fence** (≤100-task batches, durable
  pre-allocation reservations covering both locked sandboxes per task) is
  retained as-is; at most two studies run, so slice exposure stays ≤ $4.00.
  Per-batch actuals come from each study's `budget.json` snapshots.
- The sweep's internal `AUTHORITY` label still names the HAR-191 disposition:
  that is provenance of the reused mechanism, not this slice's spend authority
  (recorded above).

## Cohort (MEASURED)

- Manifest: frozen HAR-191 `cohort.json` (sha256
  `33e183b0d5107c7cdf3a2bb316c41f39b085386da1049fc41b887814663c0109`),
  built from `inputs/ledger.csv` (sha256
  `aca1a997144da6e7a1220322dba368719308bad4b6dc92bef5387f4006621147`).
  Current `ledger.csv` differs in exactly 2 run digests (001269, 002308 —
  known package moves); neither is in the resume set (002308 is already
  classified pass; 001269 is not in the study), so the frozen manifest binds
  the correct package bytes for every resumed task.
- Resume set: **801 tasks** from HAR-191 `coverage.json` —
  657 `budget-stopped` (never attempted) + 144 attempted-unknown
  (51 extraction `git-timeout`, 49 extraction `git-error`, 44 `nop:
  infrastructure-error`). Selected with the sweep's `--task` filter;
  plan-only admission prints `selected_tasks: 801`.
- Run shape per task (unchanged HAR-191 semantics): extract a source-only patch
  from the image's own leaked git history, replay oracle (fix applied) and nop
  on the task's published setup + hidden tests in fresh locked sandboxes
  (1 CPU core = 2 vCPU, 4 GiB, 180 s), then classify
  (`oracle:pass+nop:fail`, `oracle:fail[-network]`, `oracle:none`,
  `oracle:patch-conflict`, `nop:pass`). Open-egress confirmations keep the
  default `max-open 20` per study; batch size default 50 (≤ 100).

## Wave 1 (RUNNING)

- Evidence: `~/Developer/eval-lab-results/2026-10-09/mimo-ref-fixes-sweep-w1/`
  (manifest copy at `inputs-cohort.json`, receipts, `budget.json`,
  `coverage.json`, `summary.json`).
- Command: `uv run --with modal==1.6.1 python
  research/experiments/leak-oracle/sweep.py --manifest .../inputs-cohort.json
  --evidence .../mimo-ref-fixes-sweep-w1 --output .../observations.csv
  --task <801 ids> --execute` (Modal SDK 1.6.1, env `main`).
- Outcome: TBD (per-batch $ records, counts by label/state, final billing
  snapshot). Whatever the $2 fence leaves as `budget-stopped` goes to wave 2
  (fresh evidence dir, fresh $2 fence) if slice actual + exposure stays ≤ $4.00.

## Wave 2 (PENDING)

TBD: task list (wave-1 leftovers), per-batch $ records, counts, billing.

## `index.csv` (contract: task_id,label,fix_commit,patch_path,patch_sha256,source)

- Built by [`build_index.py`](build_index.py) (deterministic; fails closed on
  missing/empty patch bytes or reclassified seed tasks).
- Seed: **192 HAR-191 rows** (`source=har191`) — the `oracle:pass+nop:fail`
  rows of the committed `python-task-ledger/oracle_sweep.csv` projection
  (347 observations minus moved-package 002308). All 192 retained patches
  (`.../2026-10-07/HAR-191-oracle-sweep/tasks/<task>/oracle/solution-patch.stdout.log`,
  empty stderr) verified present and non-empty at build time.
- New rows: every wave-classified task (`source=sweep-2026-10-09`); only
  `oracle:pass+nop:fail` rows carry a patch. Per-row selection rationale
  (`how_chosen`, S1/S2a/S2b) lives in the wave `observations-*.csv` files
  tracked alongside; see caveats.
- Patch bytes: `~/Developer/eval-lab-results/2026-10-09/mimo-reference-fixes/<task>/solution.patch`
  — hidden-solution material outside the repo, never mounted into an agent
  container; the repo carries paths + hashes only.
- Current build: 192 rows (192 oracle:pass+nop:fail), sha256
  `ca6b768d3e5480932cc1c1ad8f1b403483fbdbd16259e76d7307fe3d91bbda7b`.
  Final counts after waves: TBD.

## Failure taxonomy (from HAR-191 coverage; slice waves append their own)

| state | reason | n |
|---|---|---:|
| budget-stopped | no batch fits actual-plus-retained-exposure and both fresh reservations within $2 | 657 |
| classified | locked oracle passed + locked nop failed | 193 (192 in projection) |
| classified | applied source patch failed the completed locked verifier | 52 |
| timeout | extraction: git-timeout | 51 |
| infrastructure-error | extraction: git-error | 49 |
| infrastructure-error | nop: infrastructure-error | 44 |
| classified | extractor: no-identifiable-fix / empty-diff / test-only-fix | 78 (all `oracle:none`) |
| classified | patch-no-apply | 23 (`oracle:patch-conflict`) |
| classified | same patch failed locked, passed fresh open | 1 (`oracle:fail-network`) |

## Caveats

- **S2b multi-commit patches** (`how_chosen` starting `S2b:`): the patch is the
  full base..tip diff of the feature files, so it can carry unrelated
  refactors alongside the fix. Usable as a solvability witness and a
  verifier-test seed, not as a minimal fix. Consumers must read `how_chosen`
  in the observations files before treating a patch as ground truth.
- Extraction-side unknowns (`git-timeout`, `git-error`) are operational, not
  evidence of unsolvability; the resume re-attempts all of them.
- `oracle:none` means the extractor found no usable source fix, not proof that
  no solution exists (candidate misses, docs-only conflicts, API drift).
- Billing is observed, never settled: provider rows can lag; retained runtime
  bounds are exposure, not invoices. Cold image pulls are the accepted
  residual risk.
- Non-Python code tasks (1,519, see `code-harden-night/census.json`): not
  covered unless the Python resume closes with slice budget to spare. Status: TBD.
