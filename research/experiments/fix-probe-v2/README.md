# Fix-content probe v2 (fleet census over 256 reference tasks)

Owner: ProbeV2. $0 local Docker (`--network none`, cached registry images).
No model calls. Paid spend: **$0.00** (cap $1.50, untouched).

## Why v2

The v1 census check (d) over reference tasks (prior run `probes-full-228` +
`probe-pilot-3`, 231 tasks, Modal $0.70) reported 65 clean-chain signature
hits and 164 probe-blind tasks: observed line-pattern matches, not
established leaks. Hand triage (`~/Developer/eval-lab-results/2026-10-10/leaktriage/`,
LeakTriage verdicts-q1/q2/q3.md) showed every sampled hit was a false
positive:

- Q1: generic lines (`except (OSError, AttributeError):`, `from copy import
  deepcopy`), Modal-only `/__modal` paths, base-subtraction shadowing
  (000122: batched `git grep -ohF` let a shadowed shorter line through).
- Q3: patternless fixes (deletion-only 001955, single 18-char line 002378)
  need a blob-SHA method, with `reconstruct_blob.py` proof.

## What v2 changes (`src/evallab/fix_content_census.py`)

- Post-fix blob SHA + whitespace-normalized variant per changed non-test
  file, derived container-side (`git archive` base + `git apply` reference
  patch in scratch `/tmp/probe-pfx-*`, excluded from all scans); searched
  in same-basename carriers (incl. `*.pyc` stems) and `.git` objects.
- Per-pattern base subtraction; imports + short lines excluded (host +
  staged awk agree); generic idioms (one pattern in >= 3 unrelated dirs)
  dropped; a location counts with >= 2 co-located fix-specific lines or a
  blob hash.
- Harness paths excluded (`/__modal`, `/proc`, `/sys`) + own scratch.
- Published fast control (`no_scan`): fix commit or post-fix blob pre-setup
  counts, else `blind` with reason.

## Validation (8 hand-triaged tasks)

`validation/` raw root: `~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/validation/`
(+ `smoke/` for the 000161 pilot).

RESULTS-TBD per task (expect: 0 real hits on clean; published controls positive).

## Fleet run (256 reference tasks, published + clean)

Command (run twice, disjoint `--tasks` shards, <= 2 concurrent local trials):

```sh
uv run --extra laminar python -m evallab.fix_content_census batch \
  --primary ~/Developer/eval-lab \
  --manifest research/experiments/mimo-clean-v3/manifest.csv \
  --ledger research/experiments/python-task-ledger/ledger.csv \
  --index research/experiments/mimo-reference-fixes/index.csv \
  --results ~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/fleet \
  --tasks <shard>
```

Raw root: `~/Developer/eval-lab-results/2026-10-10/fix-probe-v2/fleet/`
(`<task>.meta.json` rows + `out-published/`/`out-clean/` probe outputs).

RESULTS-TBD (table: probe-blind before/after, hits before/after, real leaks
with route + proof; triage notes for every remaining clean real hit).

## Spend actuals

- Local Docker pulls/runs: $0.00 (cached images; `docker images` count recorded at run time).
- Paid (Modal/Daytona/model): $0.00. Cap $1.50 untouched.
- Approval: Peter 2026-10-09 chat ("feel free to spend up to $25 on stuff");
  actor `omp-mimo-clean (delegate for Peter)`.
