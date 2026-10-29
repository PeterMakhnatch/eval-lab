---
status: accepted
audience:
  - analyst
  - operator
---

# What still lacks cheat-ladder evidence, and what to run next (HAR-224)

Date: 2026-10-29. Owner: omp-audit. Spend: **$0** — this slice reads tracked
receipts only. No task was built, run, graded or pulled; no package byte changed.

Deliverables: this report, [`unverified-ranking.csv`](unverified-ranking.csv)
(all 2,666 built rows, ranked), and [`rank.py`](rank.py), which regenerates the
CSV deterministically (`--check` fails on drift).

## The card's target moved, twice

HAR-224 was written against `mimo-clean-v5` and its 1,729
`unverified:ladder-missing` rows. Neither number is current:

1. **The v5 grades were rebound.** #924 (`ced544c5c`) tied manifest grades to
   exact package digests after the #848 rebuild; every v5 row that was not
   re-graded on the new bytes became `unverified:package-changed` — 2,664 of
   2,666. That label is a statement about bookkeeping, not about the task.
2. **v6 is the current corpus.** `docs/mimo/README.md` names
   `research/experiments/mimo-clean-v6` (and `mimo-v6-full-fleet` for the 567
   reference-bearing tasks) as the current build. Its manifest ships **zero
   ladder cells**: 2,652 `unverified`, 10 `unverified:ladder-probe-pending`,
   4 `unverified:ladder-missing`, 0 with a ladder verdict.

So the ranking is computed over the v6 manifest, and the surviving v5 statements
are carried in `prior_v5_label` as **prior-generation context only** — the same
standing the v6 receipt gives them: a rebuild does not inherit a verdict.

`docs/mimo/clean-set.md` still calls v5 canonical. That is doc drift against
`docs/mimo/README.md`; it is reported here, not silently resolved.

## What the ranking may use, and what it may not

The card asked for ranking by *static findings, domain and repo group*. In this
corpus two of the three are degenerate and the third is not joinable:

- **Domain** — 2,666/2,666 built rows are `code`.
- **Repo group** — every `task_id` is one template family (`format-code-task-*`).
- **Static findings** — `evallab hack scan` writes a per-invocation JSON ledger
  (`src/evallab/reward_hack.py`) and `task_admission.AdmissionFinding` carries
  `flaw_class`/`severity`, but no persisted per-task findings file exists to
  join a manifest row to. `derived/task-store/` is not tracked. Nothing in this
  slice may launder a scan that never ran into a ranking column.

What *is* recorded per row, and therefore what the ranking uses:

| Signal | Source | Rows |
|---|---|---|
| Reference fix present (an answer exists in the image) | manifest `reference_fix` | 567 |
| Prior-generation proven defect | `mimo-clean-v5-census/verify-overrides.csv`, `pending-controls.json` `prior_generation_context`, `fill_verify.py` `NODE_BUILD_LEAK`, manifest `excluded-from-training` | 15 |
| Frozen v6 selection still owes runnable control cells | `mimo-clean-v6/pending-controls.json` | 85 tasks, 168 cells |
| Language | manifest | 18 values, 1,148 Python / 1,518 non-Python |

## The ranking

Phases are ordered by the strength of the prior that a row still hides a hole,
not by how many cells it is missing.

| Phase | Rows | What the row already tells us | Why it is here |
|---|---:|---|---|
| `P0-proven-leak-prior` | 9 | a leak or grader defect was demonstrated on an earlier digest (5 `env-dependency`, 2 node-build `@4` close-out, 1 pnpm, 1 vacuous grader) | mechanism known; only the package changed |
| `P1-proven-defect-prior` | 6 | task fairness or environment failure already proven | a pass is not yet interpretable |
| `P2-frozen-selection-controls` | 85 | already chosen as the v6 acceptance set, 168 runnable cells outstanding | finish the acceptance before opening new work |
| `P3-reference-bearing` | 469 | an answer exists in the image, no ladder cell on this digest | the E1 class, the corpus's dominant leak vector |
| `P4-reference-less` | 2,097 | no reference fix, so no answer to copy and no oracle cell | E1 inapplicable; ladder applies at full cost |

`P0` and `P1` are deliberately small. They are the rows where a real defect was
already proven once; a 15-task ladder run answers them directly.

## Why a second uniform sample buys little

Earlier generations did sample this fleet, on v5 digests:

- 322 distinct tasks carry a v5 ladder cell (`sampling.json`: `ladder20` ∪
  `ladder250` ∪ `ladder50_local` ∪ `foldgates`; `ladder50_local ∩ ladder250 = ∅`,
  verified here, not assumed).
- **Zero cracked.** 258 clean + 7 unscored in the 250-task batch, 50/50 clean in
  the local sample, 5 persistent timeouts.

Zero cracks in 322 trials bounds the v5 residual leak rate at **0.93%** (exact
one-sided 95%). That is a prior about a *previous* chain build, not evidence
about v6 — but it is the reason a further 300-task uniform draw is the wrong
next move: it would spend ~78 h of local wall clock to move a bound that already
says "rare".

## The plan

Throughput is the binding constraint, not dollars. `evallab mimo-census run`
caps Docker at `--workers 2`, and the v6 receipt records the current
configuration as **one active trial** (the egress sidecar's two-container cap
admits at most one Harbor trial). Measured ladder wall is **15.5 min/task**
(v6 README, from the 50-task local sample); measured controls are 4.19 min per
oracle-x3 cell and 2.81 min per nop-x3 cell.

| Step | Scope | Local wall @1 trial | Cost |
|---|---|---|---|
| 1 | ladder the 15 `P0`+`P1` rows | 3.9 h | $0 |
| 2 | the 168 outstanding `P2` control cells | ~11.7 h | $0 |
| 3 | ladder the 150-row pre-drawn `P3` sample | 38.8 h | $0 |
| — | ladder all 2,666 built rows | 688.7 h (28.7 d) | $0 |
| — | full Python fleet (428 oracle + 1,148 nop + 1,148 ladder) | 427.9 h @1, 214.0 h @2 | $14.76–$16.86 on Modal LIMIT, 5.7 h |

Step 3 is pre-drawn in the CSV (`sample_draw=yes`, seed `20261029`, stratified by
language: 110 python / 21 javascript / 12 typescript / 7 go). It is a plan, not
an executed sample; it is also disjoint from the earlier local sample by
construction, because it draws only from rows that sample never touched on v6.

If step 3 returns clean, `0/150` bounds the reference-bearing leak rate at
**1.98%**. The alternative — bounding the whole Python fleet at 0.26% — is
`0/1,148`, i.e. the Modal option in the table above. That is a spend decision
for Peter, not a default.

## How to run a step

```bash
# ladder the P0+P1 rows (ids from unverified-ranking.csv, phase P0/P1)
uv run --extra laminar evallab mimo-census run \
  --manifest research/experiments/mimo-clean-v6/manifest.csv \
  --manifest-version mimo-clean-v6 \
  --tasks format-code-task-001478,format-code-task-001671,... \
  --backend docker --checks ladder --workers 1 --timeout-seconds 2400

# the outstanding frozen-selection control cells
uv run --extra laminar evallab mimo-census run ... --checks nop,oracle --workers 1

# any paid backend needs a recorded authorisation, never a standing approval
```

`--timeout-seconds 2400` follows the 50-task local sample: 900 s does not fit
emulated-amd64 local Docker with cold image pulls (~13 GB per task image).

## Limits

- Nothing was executed. Every phase is a **prior about where a hole is likely**,
  not evidence that one exists; a phase assignment is not a finding.
- The prior-defect set is only as complete as the receipts. A defect recorded
  nowhere machine-readable cannot be ranked; `000928` was recoverable from the
  manifest's own `excluded-from-training` reason, the two node-build rows only
  from `fill_verify.py`'s `NODE_BUILD_LEAK`, which is read rather than copied.
- Static findings are absent from the ranking because no per-task ledger exists
  to join. If one is persisted, it should become a phase input, not a new column
  nobody recomputes.
- The 4 `unverified:ladder-missing` and 10 `ladder-probe-pending` v6 rows are not
  special-cased; they fall into `P3`/`P4` by rule.
- Wall-clock figures are the receipts' measured means applied to this plan. They
  exclude queue wait, and a cold pull on a 13 GB image can dominate a single
  task.
