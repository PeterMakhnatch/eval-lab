# mimo-clean-v6 (separate-verifier@10 fleet build)

Date: 2026-10-15. Spend: **$0** (local CPU only; no Docker trials, no
paid APIs). Chain (Python):
`strip-future-history@1>purge-installed-copies@2>purge-build-caches@4>mtime-normalize@2>separate-verifier@10>agent-network-none@1`;
non-Python omits the purge slot; `NODE_FOLD_TASKS` keep
`purge-build-caches@5` per row. Manifest: `manifest.csv` (2,698 rows:
2,666 built, 32 skipped — the ledger discards). Every built row's
`verify` is `unverified` except 23 exclusions carried verbatim from
mimo-clean-v5 (`fail:open-leak:*` x6, `fail:unfair-task:*` x5,
`fail:vacuous-grader:compile-only` x1, `env-broken` x11) so excluded
tasks stay marked. Selection: `selection.json` (flagged traces +
flagged envcheck + seeded `sample_50`, seed `20261015`).

## Measured build wall-clock (local CPU, `--workers 8`, $0)

| build | tasks | wall |
|---|---|---|
| `mimo-clean-v6` selection subset (flagged union 49 + sample 50) | 99 built | 38.1 s |
| `mimo-clean-v6` Python ledger pool | 1,148 built / 32 skipped of 1,180 | 166.9 s |
| `mimo-clean-v6` full fleet | 2,666 built / 32 skipped of 2,698 | 269.9 s |
| (context) `mimo-clean-v5` #848 digest refresh, same host | 2,666 built of 2,698 | 652.8 s |

Builds are incremental and content-addressed: later builds reuse the
chain steps materialized by earlier ones in
`derived/task-store/variants/` (first-build numbers above are in run
order: subset, Python pool, full fleet).

## Cost/time to verify the full 1,148-task Python fleet on v6

Rates are the measured v5 census Modal LIMIT actuals: oracle
$0.0022–$0.0071/task, nop $0.0053/task, ladder $0.00674/task
(`research/experiments/mimo-clean-v5-census/`). Reference-fix tasks
(428) get oracle+nop+ladder; reference-less tasks (720) get
nop+ladder. The 3x option runs the repeat verifier (`repeat_n=3`) for
every cell. Local-Docker walls project from the measured ladder
median of 15.5 min/task (n=50 local sample) plus ~5 min each for the
single-trial oracle/nop cells; Modal walls assume the census's
24-wide remote fan-out at ~3 min/cell. Projections, not measurements.

| option | cells | est. wall | cost |
|---|---|---|---|
| Local Docker, 1 concurrent | 2,724 trials | ~480 h (~20 days) | $0 |
| Local Docker, 2 concurrent | 2,724 trials | ~240 h (~10 days) | $0 |
| Modal LIMIT, 1x verifier | 2,724 cells | ~5.7 h at 24-wide | $14.76–$16.86 |
| Modal LIMIT, 3x repeat verifier | 2,724 cells (3 gradings each) | ~17 h at 24-wide | $44.29–$50.58 |

Modal 1x split: 428 reference-fix tasks $6.10–$8.19
(oracle range drives the spread) + 720 reference-less tasks $8.67.
Verification itself has not run yet; the repeat-verifier lane grades
the flagged/sample packages first.
