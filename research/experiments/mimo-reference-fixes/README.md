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
  `oracle:patch-conflict`, `nop:pass`).

## Wave 1 (MEASURED)

- Evidence: `~/Developer/eval-lab-results/2026-10-09/mimo-ref-fixes-sweep-w1/`
  (manifest copy at `inputs-cohort.json`, receipts, `budget.json`,
  `coverage.json`, `summary.json`).
- Command: `uv run --with modal==1.6.1 python
  research/experiments/leak-oracle/sweep.py --manifest .../inputs-cohort.json
  --evidence .../mimo-ref-fixes-sweep-w1 --output .../observations.csv
  --task <801 ids> [--batch-size 100] --execute` (Modal SDK 1.6.1, env `main`;
  batch size raised 50 -> 100 from the third invocation on after two
  single-cancellation fail-closed stops; max-open default 20).
- Outcome (MEASURED, state `budget-stopped`, 25 billed Apps): admitted 524 of
  801; classified 65 — 33 `oracle:pass+nop:fail`, 15 `oracle:fail`, 14
  `oracle:none`, 3 `oracle:patch-conflict`. 15 open-egress confirmations ran,
  0 passed (no `oracle:fail-network`). Operational unknowns: 227 image-build
  failures (`ImageBuildError`, receipt base unbound — the large-image tail),
  93 extraction `git-error` (sub-bucket: 9 confirmed `unsupported-tree`
  masked by the runtime base-binding check, 3 genuine, rest un-sub-bucketed),
  67 extraction `git-timeout`, 63 `nop:` infra (mostly `setup-clean-base`
  exit 1), 3 `nop: base differs`, 2 oracle timeouts, 1 `needs-tip-decision`
  (ambiguous). 3 receipts carry transient `cancelled` flags (1 per early
  50-batch; the study fail-closed each time and was resumed; 100-batches ran
  clean). 277 selected tasks left `budget-stopped`.
- Spend (MEASURED): actual provider **$1.16925127**, conservative exposure
  **$1.98927715** vs the $2.00 code fence. Batches: 50 + 50 + 97 + 100s to the
  fence. Per-batch actuals in `budget.json` snapshots.

## Wave 2 (MEASURED)

- Selection: 280 tasks — the 277 wave-1 `budget-stopped` plus the 3
  transient-`cancelled` receipts retried under the fresh journal. Fence check:
  wave-1 actual $1.16925127 + wave-2 worst case $2.00 = $3.17 ≤ $4.00 slice cap.
- Difference from wave 1: `--max-open 0` (no open-egress confirmations).
  Rationale: the slice goal is reference fixes; wave 1 + HAR-191 measured the
  fail-network rate at 1/53 and 0/15, so new `oracle:fail` rows stay `fail`
  with that caveat, conserving the fence for locked coverage. Batch size 100.
- Evidence: `~/Developer/eval-lab-results/2026-10-09/mimo-ref-fixes-sweep-w2/`.
- Outcome (MEASURED, state `selected-slice-complete`, 24 billed Apps):
  admitted all 280; classified 56 — 31 `oracle:pass+nop:fail`, 8
  `oracle:fail`, 14 `oracle:none`, 3 `oracle:patch-conflict`. Operational
  unknowns: 85 extraction `git-error`, 71 extraction `git-timeout`, 53 `nop:`
  infra, 9 nop timeouts, 4 oracle timeouts, 2 `needs-tip-decision`
  (ambiguous). No image-build failures (unlike wave 1's middle band) and no
  `cancelled` flags; the 3 wave-1 cancelled retries completed as honest
  unknowns (2 git-timeout, 1 nop infra).
- Spend (MEASURED): actual provider **$0.81527352**, conservative exposure
  **$1.98646500** vs the $2.00 code fence.

## Slice totals (MEASURED)

- Attempted: all **801** resume tasks (524 wave 1 + 280 wave 2, incl. 3
  retries). Newly classified: **121** — 64 `oracle:pass+nop:fail`, 23
  `oracle:fail`, 28 `oracle:none`, 6 `oracle:patch-conflict`. New pass
  strategies: 59 S1, 2 S2a, 3 S2b.
- Operational unknowns remaining: 683 observations over **680 unique tasks**
  (3 retried after transient cancellation): image-build 227, git-error 178,
  git-timeout 138, nop infra/timeout 128, oracle timeout 6, source-mismatch 3,
  ambiguous 3. A third study could retry them, but image-build failures are
  systematic for those images under this replay path, not transient flakes.
- Spend: wave-1 actual $1.16925127 + wave-2 actual $0.81527352 = **$1.98452479
  total ≤ $4.00 cap** (headroom $2.02). Provider billing rows lag; journal
  actuals are the binding record. `evallab spend day --date 2026-10-10`
  shows no settled Modal rows for the day (partial data) plus unrelated
  slices' spend — nothing attributable contradicts the journals.

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
- Current build: **313 rows (256 oracle:pass+nop:fail)**, sha256
  `06af630dabed151f3e0f2cf7639c8de84b0cc764b0cce09f75cb8cc138f77cdf` —
  192 `har191` + 121 `sweep-2026-10-09` (64 pass, 23 fail, 28 none,
  6 conflict). All 256 pass rows carry verified patch bytes; all 57 other
  rows leave patch fields empty (builder-enforced).

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
- Non-Python code tasks (1,519, see `code-harden-night/census.json`): **not
  covered — residual**. Costed out: 1,519 tasks at the measured worst-case
  reservation (~$0.0038/task) need ~$5.8 exposure, exceeding the $4.00 slice
  cap on their own; they also need a new cohort manifest (that census is not
  ledger-shaped) plus extractor validation per language. Proposed as its own
  funded slice, not smuggled into this one.
