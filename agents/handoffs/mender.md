Status: building
Last: Verified all six merged missions, closed superseded PR #16, and sunset every spent branch/worktree
Next: Run premerge, open the MENDER PR, require green current-head checks, merge, and verify zero open PRs
Blockers: none

## What the wave actually added

- DASHBOARD — a read-only seven-pane research overview; run `uv run evallab dashboard` and
  inspect leaderboard, canary, spend, queue, calibration, ATIF, and DISCOVERIES panes.
- FETCH — pinned benchmark acquisition plus integrity audit; run `uv run evallab fetch --audit`
  to see all five manifests pass.
- RETENTION — evidence-aware compression/pruning plans; run `uv run evallab gc` for the default
  dry run against `runs/`.
- PIPELINE — one catalog-first completion path that also writes Parquet; run a free queue control
  with `evallab submit`/`tick`, then check `evallab doctor`.
- SPEED — a reproducible six-path profile and budget gate; run `uv run python
  scripts/profile/harness.py` followed by `scripts/profile/check_budgets.py`.
- INSPECTOR — three evidence-quality reports; read `research/inspections/`.

MENDER closed duplicate PR #16 as superseded by merged PR #17, deleted its branch last after the
other spent branches, removed 17 finished worktrees, and left only `main` plus the active MENDER
branch/worktree. No source patch was needed. One zero-cost oracle rerun restored raw evidence that
had lived only in the spent PIPELINE worktree; the rebuilt invariant is 17 catalog jobs = 17
projected jobs. The cross-worktree storage topology is flagged below for a larger follow-up.

## Task 1 — superseded PIPELINE PR

PR #16 and merged PR #17 had the same title and the same 7-file, 860-addition/36-deletion payload.
The owned implementation files were byte-identical at the two branch tips:

```text
$ git diff --exit-code role/pipeline integrate/pipeline -- \
    src/evallab/atif.py src/evallab/automation.py src/evallab/queue.py tests/test_pipeline.py
[exit 0; no output]
```

The remaining branch diff was only later union additions already on `main` plus the obsolete
conflict blocker in the old handoff. Case (a) therefore held. I ran:

```text
$ gh pr close 16 --delete-branch --comment "PR #17 ... is the merged integration ..."
✓ Closed pull request PeterMakhnatch/eval-lab#16
$ git push origin --delete role/pipeline
- [deleted] role/pipeline
```

No rebase or conflict resolution was attempted on the spent branch.

## Task 2 — fresh merged-wave verification

### DASHBOARD — cold browser start renders every pane

```text
$ uv run evallab dashboard --port 8517
Uvicorn server started on 127.0.0.1:8517
URL: http://127.0.0.1:8517
```

The bundled Playwright wrapper currently resolves an upstream package without its documented
`playwright-cli` binary, so I used the installed Playwright command with local Chrome:

```text
$ npx playwright screenshot --channel chrome --wait-for-selector 'text=DISCOVERIES' \
    --wait-for-timeout 1000 --viewport-size '1440,6000' \
    http://127.0.0.1:8517 output/playwright/dashboard-all-panes.png
Navigating to http://127.0.0.1:8517
Waiting for selector text=DISCOVERIES...
Capturing screenshot into output/playwright/dashboard-all-panes.png
```

Visual inspection showed all seven headers and their contents in one cold render: `Leaderboard by
cohort`, `Canary trend vs 7-day baseline`, `Spend vs daily ceiling`, `Queue funnel`, `Calibration
history`, `ATIF-derived activity`, and `DISCOVERIES`. The ATIF pane rendered its explicit
unavailable-data state in this initially fresh worktree; no pane crashed.

### FETCH — all benchmark pins audit cleanly

```text
$ uv run evallab fetch --audit
pass  aime: ... on-disk sha256:845637e564faa9d5f5ee0ab84d2ed079aea85fe219372d60656c6fa0ee6a9d00
pass  gpqa-diamond: ... on-disk sha256:628800b427094998e6cc3395b154bbbe334ba999a46fc1e7a16143778aeb91d0
pass  hello-world: ... sha256:cff230d09ea952d092daf99796d0c52ec5bfb92d86f13021af902ce7b6b36720
pass  humanevalfix: ... on-disk sha256:5926e73ec3b26f04cfe3f4f733ac4ac6da8bb9092f0a41e8b7336ca450b1ea1c
pass  terminal-bench-sample: ... on-disk sha256:5130a7b73439f05daa7c055e774da0da65c85c0589b7fdf775bb5666eea61758
5 benches, 0 fail
```

### RETENTION — default dry-run plan is safe against the real corpus

`gc` is dry-run by default; there is no `--dry-run` flag.

```text
$ uv run evallab gc --runs-dir /Users/petermakhnatch/Developer/eval-lab/runs
gc plan: 0 action(s), 9 skipped, reclaim=0 bytes
empty: no completed+ingested+unpromoted candidates
  skip canary-event-summary-codex-20260814  referenced by digest or DISCOVERIES
  skip canary-terminal-bench-html-js-filter-codex-20260814  referenced by digest or DISCOVERIES
  skip canary-terminal-bench-html-js-filter-codex-20260814-r2  referenced by digest or DISCOVERIES
  skip canary-transaction-reconciliation-codex-20260814  referenced by digest or DISCOVERIES
  skip canary-transaction-reconciliation-codex-20260814-r2  referenced by digest or DISCOVERIES
  skip checkpoint-oracle-20260814  referenced by digest or DISCOVERIES
  skip control-reset-oracle-20260814  referenced by digest or DISCOVERIES
  skip failed-network-policy-oracle  missing finished_at
  skip reframe-post-move-oracle-20260814-1756  referenced by digest or DISCOVERIES
```

The plan proposed no mutation and explained every skip.

### PIPELINE — automatic catalog, Parquet, and digest landing

I submitted a unique, free, one-attempt oracle control through the real queue:

```text
$ uv run evallab submit runs/mender-pipeline-oracle-20260814.json
approved: .../queue/approved/oracle-01M013FTDMTZQRZVH883TKKVXA.json
admitted by standing policy rule local-controls
$ uv run evallab tick
Trials 1  Exceptions 0  Correctness 1.000  Reward 1.000
Results written to .../runs/mender-pipeline-oracle-20260814/result.json
dispatched 1 experiment(s)
quarantined: no
```

No manual ingest or trajectories command ran before these three observations:

```text
$ uv run evallab db list
| mender-pipeline-oracle-20260814 | event-summary__4RAr69N |
| local-lab/event-summary | oracle | | 1.0 | | 8.745281 |

$ find derived/parquet/job_id=eb652a14-8728-4cf4-828e-3a163b7301e4 -type f
jobs.parquet
trial_id=4f7aa00c-007c-4250-9487-5f00b1f81a2b/{artifact_facts,observations,
reward_facts,steps,tool_calls,tool_usage,trajectories,trial_facts}.parquet

$ rg -n -C 2 mender-pipeline-oracle-20260814 digests/2026-08-15.md
37:| mender-pipeline-oracle-20260814 | local-lab/event-summary | oracle | 1 | | local-controls |
69:| ... | submitted | mender-pipeline-oracle-20260814 | |
70:| ... | policy_admitted | mender-pipeline-oracle-20260814 | local-controls |
71:| ... | dispatch_started | mender-pipeline-oracle-20260814 | local-controls |
72:| ... | dispatch_completed | mender-pipeline-oracle-20260814 | local-controls |
```

The fresh worktree initially reported `catalog=17 projected=1`: PostgreSQL is shared, but each
worktree has its own ignored `derived/parquet`. Removing the spent PIPELINE worktree had also
removed the only raw copy for its acceptance job. I restored that raw source with another free
oracle queue run under the same evidence path, then used the shared rebuild wrapper over retained
main evidence plus the two MENDER controls:

```text
$ uv run evallab trajectories /Users/petermakhnatch/Developer/eval-lab/runs \
    /Users/petermakhnatch/Developer/eval-lab/research/evidence/runs runs
totals:
artifact_facts: 55 row(s)
jobs: 17 row(s)
reward_facts: 43 row(s)
steps: 30 row(s)
trajectories: 6 row(s)
trial_facts: 25 row(s)
$ uv run evallab doctor | rg 'catalog-parquet|overall'
ok    catalog-parquet catalog=17 projected=17 exceptions=0 missing=0 extra=0
$ find derived/parquet -name jobs.parquet | wc -l
17
```

### SPEED — report and budgets reproduce

```text
$ uv run python scripts/profile/harness.py
fixture: 2 jobs, 4 result.json files, 31716 bytes
backend: scratch-postgres; Harbor runner stubbed
method: median of 5 reps after 1 warmup
ingest                 34.485 ms
projection              2.924 ms
facts                    3.886 ms
digest                   0.908 ms
queue-tick-100          49.308 ms
fleet-status          1807.444 ms
ingest+projection       49.866 ms
$ uv run python scripts/profile/check_budgets.py runs/_speed/profile-report.json
perf budgets ok
```

All six measured paths were below 50% of their committed budgets.

### INSPECTOR — all three reports are present

```text
$ ls -l research/inspections/{discoveries-first-pass,judge-floor,transaction-reconciliation}.md
9158 ... research/inspections/discoveries-first-pass.md
8158 ... research/inspections/judge-floor.md
9478 ... research/inspections/transaction-reconciliation.md
$ wc -l research/inspections/{discoveries-first-pass,judge-floor,transaction-reconciliation}.md
33 discoveries-first-pass.md
40 judge-floor.md
33 transaction-reconciliation.md
106 total
```

## Task 3 — patch-up result and follow-up proposal

No source defect small enough and within a finished mission's owned paths surfaced, so MENDER made
no code patch. The invalid `gc --dry-run` placeholder was corrected in this handoff to the real
default-dry-run command.

### Follow-up proposal — make catalog/Parquet storage topology explicit across worktrees

The doctor invariant compares a repository-local Parquet root with the process-wide PostgreSQL
catalog. A newly created worktree therefore fails the global invariant until it rebuilds all raw
evidence, even though new completions project correctly. Solving this is larger than a 30-line
mission-owned patch: choose and document either one shared derived root, isolated per-worktree
catalogs, or an invariant scoped by evidence root; then add lifecycle tests covering worktree
creation and removal. No implementation was started.

## Task 4 — fleet sunset

Finished worktrees removed: `adapter`, `analyst`, `autopilot`, `curator`, `evidence`, `fetch`,
`forge`, `ingest`, `inspector`, `judge`, `observer`, `pipeline-integrate`, `recon`, `retention`,
`runner`, `speed`, and `speed-2`. All spent local/remote role, `-closeout`, `-2`, and `-audit`
branches were deleted; `role/pipeline` was deleted last. `agents/ROLES.md` now records the actual
merged/done state for every role.

```text
$ git fetch --prune origin && git worktree list && git branch -a
/Users/petermakhnatch/Developer/eval-lab                    5be6c1c [main]
/Users/petermakhnatch/Developer/eval-lab/.worktrees/mender  28a27fb [role/mender]
* main
+ role/mender
  remotes/origin/HEAD -> origin/main
  remotes/origin/main
```

The MENDER worktree was then fast-forwarded to `5be6c1c`; it remains only until this mission's PR
merges. Final PR, current-head checks, merge, open-PR count, and post-merge branch inventory are
recorded below before `Status` changes to `done`.

## Task 5 — MENDER PR closeout

Pending.
