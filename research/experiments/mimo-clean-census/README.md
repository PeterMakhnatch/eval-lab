# mimo-clean-census: fleet-wide clean-set verification receipt

Date: 2026-10-09. Lane: FleetCensus. Slice cap: **$15.00** (of Peter's $25
day approval, 2026-10-09 chat). Method: `docs/mimo/verification.md`. Runner:
`src/evallab/mimo_census.py` (`evallab mimo-census run|report|record-spend`).

Raw run output (out of git):
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-09/mimo-clean-census/`.

## Spend ledger (provider actuals only)

Day baseline before this slice (2026-10-09, `evallab spend day --date
2026-10-09 --json`): day total $8.92 of the $20 standing-policy cap
($7.04 model + $1.88 Daytona; Modal billing rows partial/$0 posted at
snapshot). Slice spend below is this lane's own batches only.

| batch | backend | tasks | provider actual | evidence | running slice total |
|---|---|---|---|---|---|
| _parity-6 (docker) | docker | 6 | $0.00 (local) | jobs dir | $0.00 |
| _pilot-20 (modal)_ | modal | 20 | _pending_ | _pending_ | _pending_ |

## 1. Runner parity (v1 packages, docker backend, $0)

Target: ≥5 tasks, census docker-backend == `verify-local` Harbor results.

| task | fix? | verify-local (oracle/nop/cheat) | census (oracle/nop/ladder) | match |
|---|---|---|---|---|
| format-code-task-002552 | yes | 1 / 0 / 0 cracked | _running_ | _ |
| format-code-task-001809 | yes | 1 / 0 / 0 cracked | _running_ | _ |
| format-code-task-002391 | yes | 1 / 0 / 0 cracked | _running_ | _ |
| format-code-task-000666 | yes | 1 / 0 / 0 cracked | _running_ | _ |
| format-code-task-000085 | yes | 1 / 0 / 0 cracked | _running_ | _ |
| format-code-task-001269 | no | n/a / 0 / 0 cracked | _running_ | _ |

(All six images verified cached locally before launch — zero Docker Hub pulls.)

## 2. Paid pilot (20 tasks, modal backend)

_pending — launches after parity passes. Measures real $/task per check._

## 3. Fleet census (v2 manifest, pending CleanSetV2 merge)

_pending — Python fleet first (a+c all, b where fix exists, d where fix
recoverable), then a stratified non-Python sample sized to the remaining
budget. Gated on v2 because the Python chain changes in substance
(purge-build-caches@1→@2, separate-verifier@2→@3)._

## 4. Failure taxonomy + open issues

_pending — every failure classified (env broken / oracle wrong / real leak /
real grader hole / infra flake) with evidence. Real leaks or grader holes
are written up here as open issues (transforms NOT fixed by this lane)._
