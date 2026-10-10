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
- Current build: **973 rows (564 oracle:pass+nop:fail)**, sha256
  `d701351fe18b6036cf2cb3ec983f0caf561e805cc8a6e0129e49c060bb0b8962` —
  192 `har191` + 121 `sweep-2026-10-09` (64 pass, 23 fail, 28 none,
  6 conflict) + 361 `sweep-2026-10-10` (172 pass, 82 fail, 1 fail-network,
  95 none, 11 conflict) + 52 `sweep-2026-10-10-go` (22 pass, 19 fail,
  4 fail-network, 6 none, 1 conflict) + 247 `sweep-2026-10-10-js`
  (114 pass, 64 fail, 53 none, 15 conflict, 1 nop:pass). All 564 pass rows
  carry verified patch bytes (builder fail-closed on missing/empty patch
  bytes or non-empty patch stderr); all other rows leave patch fields empty
  (builder-enforced). Pass rows by task language (code-harden-night census):
  Python 172+64+192, JavaScript 69, TypeScript 45, Go 22 (plus Go-census
  002361 resolved as patch-conflict in a Python-ledger wave, see 2026-10-10
  section). Patch bytes for 2026-10-10 rows live in
  `~/Developer/eval-lab-results/2026-10-10/mimo-reference-fixes/<task>/solution.patch`
  (same hidden-solution rule as the 2026-10-09 dir).

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
- Non-Python code tasks: partially covered by the 2026-10-10 slice (Go 90/722,
  JS/TS 330/554 classified; see section above). Remaining residuals: 632 Go,
  224 JS/TS, plus Ruby/PHP/Java/C++/C/Rust/etc. never attempted.

## 2026-10-10 recovery and language pilots (MEASURED, slice cap $5.00)

Slice approval: actor `omp-mimo-clean (delegate for Peter; 2026-10-09 chat:
"feel free to spend up to $25 on stuff")`. Slice cap: **$5.00 total**
including the crashed first attempt's study below. Replay code: the
fixed `research/experiments/leak-oracle/` from this branch's first two
commits (operational-failure cures + batched S2 git fan-out/Go-JS test-file
recognition, with regression tests). Each study keeps its own batch-enforced
**$2.00 code fence** (`budget.json`); batch-size 30 (20 for w5) after the
study-A app death. Slice accounting (program precedent w1->w2):
actuals-so-far + worst-case-new ≤ cap. No paid launches after w5.

### Study A: w3 Python recovery — DIED, terminally pending-cleanup

- Evidence: `~/Developer/eval-lab-results/2026-10-10/mimo-ref-fixes-sweep-w3/`
  (84-task head of the 680 operationally-unknown Python tasks).
- What happened: batch 1 (84 tasks) ran 07:12–07:38 UTC; the Modal app
  `ap-s3F2vOXTLL7Rfwe2hTxuMl` stopped mid-batch with the driver context still
  open (cause not established — no app-stop path exists in the sweep code).
  48 tasks failed both arms with `ConflictError: app is stopped or disabled`
  (zero signal); 24 classified (10 `oracle:pass+nop:fail` — the fixed
  extractor works). The driver finished the batch orderly and published
  `pending-cleanup`; the worker crashed later during analysis.
- Recovery check: `modal app list` no longer shows the app; `Sandbox.list`
  for it returns 0 sandboxes (no orphans). Billing actual for the app settled
  at **$0.0557** with no growth 6h later, but the journal conservatively
  retains **$1.306** exposure for the 107 unconfirmed allocation slots, and
  `budget.py` forbids reclassifying attempted allocations or relaunching over
  a pending journal (verified: resume attempt raises `BudgetError: Existing
  batch has unresolved allocation/cleanup`; `controller-error.json` retained
  as evidence). w3 therefore stays `pending-cleanup` permanently by design;
  the 10 fixes are salvaged, the 48 app victims rerun in w4.
- Lesson: batch-size 30 (20 for the large-image tail) bounds the blast radius
  of a recurrence. All later studies completed with zero app deaths (39 batch
  apps total across w4/w5/go/js).

### Study B: w4 Python recovery — 368/653, fence-filled

- Evidence: `.../mimo-ref-fixes-sweep-w4/` (653 = 596 w3 not-run + 57 w3
  infra-error, w3 order). 26 batches (20 locked shrinking 30→1 as the fence
  filled, 6 open-egress confirmations), state `budget-stopped`, exposure
  **$1.979** vs $2 fence, actual **$1.1205**.
- Outcome: 253 classified — 121 `oracle:pass+nop:fail`, 64 `oracle:fail`
  (20 open-confirmed, 1 reclassified `oracle:fail-network`), 63
  `oracle:none`, 4 `oracle:patch-conflict`. Unknowns: 59 git-timeout,
  31 unsupported-tree, 9 needs-tip-decision, 7 nop infra, 4 git-error,
  3 oracle timeout. 285 budget-stopped → w5.
- Procedural note (own error, recovered): w4 launched without `--output`, so
  publishes overwrote the worktree `python-task-ledger/oracle_sweep.csv`
  (reverted; worktree clean). The study CSV was recovered byte-identically
  via `results.write_sweep` over the retained receipts
  (`w4/observations.csv`, sha256 matches `summary.json:csv_sha256`) and
  `coverage.json` restored byte-exact (sha verified against
  `summary.json:coverage_sha256`).

### Study E: w5 large-image tail — 194/285, fence-filled

- Evidence: `.../mimo-ref-fixes-sweep-w5/` (285 w4 budget-stopped, batch-size
  20). State `budget-stopped`, exposure **$1.978**, actual **$1.0465**.
- Outcome: 84 classified — 41 `oracle:pass+nop:fail`, 13 `oracle:fail`,
  23 `oracle:none`, 7 `oracle:patch-conflict`. Unknowns dominated by 68
  extraction git-timeouts + 18 arm timeouts (systematic for the
  large-image tail: 91/285 tasks have image_mib>4000), 11 unsupported-tree,
  6 nop infra, 4 ambiguous, 4 git-error. 91 budget-stopped remain (residual).
- Crossover: `format-code-task-002361` is census-Go but ledger-run here
  (`oracle:patch-conflict`; mixed Python/Go repo). Resolved in this index;
  exclude from future Go slices (the builder fails closed on duplicates).

### Study C: Go pilot + slice — 90/722, expensive and timeout-heavy

- Local validation first (free): 2 Go tasks — 000001 `unsupported-tree`
  (submodules, honest), 000011 S1 `ok` (`Solution_test.go`, 1400B patch).
- Evidence: `.../mimo-ref-fixes-sweep-go/` (30 pilot + 60 slice, batch-size
  30). 6 batches, `selected-slice-complete`, exposure **$0.442**, actual
  **$0.6776** ($0.0075/task — 10x Python).
- Outcome: 52 classified — 22 `oracle:pass+nop:fail`, 19 `oracle:fail`
  (+4 `oracle:fail-network` — open confirmations caught network-dependent
  Go graders, E6 evidence), 6 `oracle:none`, 1 conflict. Unknowns: 29 arm
  timeouts + 2 git-timeouts (the 180 s sandbox bound binds Go verifiers),
  6 unsupported-tree (submodules), 1 ambiguous, 1 nop infra. 632 not-run
  (residual — unaffordable at this $/task under the slice cap).

### Study D: JS/TS pilot + slices — 330/554, highest yield

- Local validation first (free): 2 JS (000004 S1 `ok`, 1589B; 000007 S1 `ok`,
  725B) + 2 TS (000025 S1 `ok`, 34KB; 000026 `patch-no-apply`, honest).
- Evidence: `.../mimo-ref-fixes-sweep-javascript-typescript/` (30 + 150 +
  150). 22 batches, `selected-slice-complete`, exposure **$1.934**, actual
  **$1.0542** ($0.0032/task).
- Outcome: 247 classified — 114 `oracle:pass+nop:fail` (69 JS + 45 TS),
  64 `oracle:fail`, 53 `oracle:none`, 15 conflict, **1 `nop:pass`**
  (base passes without the fix — trivially-passing task, kept as a labeled
  row with empty patch fields). Unknowns: 42 git-timeout, 16 arm timeouts,
  7 unsupported-path + 5 unsupported-tree (JS-heavy extraction classes), 5
  ambiguous, 5 infra, 3 base-unavailable. 224 not-run (residual).

### Slice totals (MEASURED)

- Index: **313 → 973 rows (+660)**; fixes (`oracle:pass+nop:fail`):
  **256 → 564 (+308)** — Python +172 (w3 10, w4 121, w5 41), JS +69, TS +45,
  Go +22. Go/JS/TS extractor rules validated locally (≥2 tasks each) then
  measured at Modal scale.
- Spend: w3 $0.0557 + w4 $1.1205 + w5 $1.0465 + go $0.6776 + js $1.0542 =
  **$3.9545 total ≤ $5.00 cap** (headroom $1.05). Per-batch actuals in each
  study's `budget.json` (`actual_after_usd` per batch; open-egress batches
  listed separately). Journal actuals are the binding record; provider rows
  lag. Pre-launch `evallab spend day` showed $7.60 of the $20 standing cap
  (unrelated slices); no settled Modal rows for the day at check time.
- Python operational unknowns: 680 → 91 budget-stopped + systematic
  git-timeout/unsupported-tree residuals above (w3's 57 infra-error
  superseded by w4 reruns). The image-build class is gone (fixed replay
  binds the base without it); remaining timeouts concentrate on the
  large-image tail.
- Residuals (unfunded under this ticket): 91 Python (largest images), 224
  JS/TS, 632 Go (+ Ruby/PHP/Java/C++/C/Rust/etc. never attempted). Go needs
  longer sandboxes or cheaper images before a full run makes sense.
