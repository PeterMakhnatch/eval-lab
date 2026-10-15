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
nop+ladder: 428 oracle + 1,148 nop + 1,148 ladder = 2,724 cells.
The 3x option runs the repeat verifier (`repeat_n=3`) for every cell,
not three independent agents: 2,724 trials / 8,172 verifier invocations.

**All v6 verification times/costs below are projections, not measurements
or approval to launch.** The local scenario uses the measured v5 ladder
median of 15.5 min/task (n=50 local sample), plus an explicitly assumed
5 min per oracle/nop cell. Thus local 1x trial-minutes =
`1,148 * 15.5 + (428 + 1,148) * 5 = 25,674` (427.9 h).
Modal time is only a planning scenario: an assumed 3 min/cell at the
census's 24-wide fan-out. The 3x columns conservatively multiply
whole-cell time and cost by three; real repeat-verifier overhead may be
lower because image/setup/agent work is shared. Cold pulls, retries,
flakiness, and the @10 integrity gate can change both estimates.

| option | verifier repeats | estimated wall | provider cost |
|---|---|---|---|
| Local Docker, 1 concurrent trial | 1x | 427.9 h (17.8 days) | $0 |
| Local Docker, 2 concurrent trials | 1x | 214.0 h (8.9 days) | $0 |
| Local Docker, 1 concurrent trial | 3x | 1,283.7 h (53.5 days) | $0 |
| Local Docker, 2 concurrent trials | 3x | 641.9 h (26.7 days) | $0 |
| Modal LIMIT, 24-wide | 1x | 5.7 h | $14.76–$16.86 |
| Modal LIMIT, 24-wide | 3x | 17.0 h | $44.29–$50.58 |

| Python group | checks | Modal 1x | Modal 3x | local 1x trial-hours | local 3x trial-hours |
|---|---|---|---|---|---|
| 428 reference-fix tasks | oracle+nop+ladder | $6.09–$8.19 | $18.28–$24.58 | 181.9 | 545.7 |
| 720 reference-less tasks | nop+ladder (no solvability claim) | $8.67 | $26.01 | 246.0 | 738.0 |
| all 1,148 | 2,724 cells | $14.76–$16.86 | $44.29–$50.58 | 427.9 | 1,283.7 |

Concurrency 2 is a hypothetical throughput comparison, **not today's
run configuration**: with the egress sidecar, this tab's two-container
cap requires at most one active Harbor trial. No v6 verification was
run by this CPU build slice; the independent repeat-verifier lane
grades the flagged/sample packages first.
