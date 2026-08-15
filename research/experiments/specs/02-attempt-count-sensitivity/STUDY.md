# Study 02 — Attempt-count sensitivity on transaction-reconciliation

**Hypothesis.** Holding the pinned transaction-reconciliation task and
Codex fixed, changing only the attempt count changes the width of the
pass-rate interval more than it changes the point estimate. A 1-attempt
result is not a comparison.

**One variable.** `attempts` ∈ {1, 3, 5}.

**Fixed.** `task=canary/transaction-reconciliation`,
`task_path=tasks/transaction-reconciliation`, `agent=codex`, docker,
concurrency 1, no extra instruction. The k=3 cell is Study 01's
transaction-reconciliation spec — do not submit it twice.

**Policy and cost.** Canary suite publishes $2.50 per 3-attempt job
(~$0.833 / attempt).

| Arm | attempts | est_cost_usd | Expected gate |
|---|---:|---:|---|
| k1 | 1 | 0.83 | `canary` (admitted) |
| k3 | 3 | 2.50 | `canary` (Study 01 cell; do not resubmit) |
| k5 | 5 | 4.17 | `per_job_cost_ceiling` ($3). Independently, `canary` max_attempts is 3. `researcher-followups` would allow 5 attempts only on `registered/*` with three requires this lab does not yet satisfy. |

**n.** The scientific comparison wants n≥5 on at least one arm. That arm is
the one the standing policy and the $3 job ceiling both refuse. Staging k5
anyway records the stacked reason codes. Do not interpret k1 vs k3 as
"sensitivity" if k5 never runs — that pair only shows that n=1 is noisy.

**Next spec this implies.** If Peter wants n=5 on this task, either the
per-job ceiling must move or the per-attempt estimate must be measured
(from a completed k=3 job's actual `cost_usd`) and shown to be ≤ $0.60.
Do not lower `est_cost_usd` to sneak k=5 through.

## 2026-08-15 PROGRAM reconciliation

k=3 cell now has scored evidence: three trials in
`runs/canary-transaction-reconciliation-codex-20260815/*/result.json`
with `verifier_result.rewards.reward=1.0` and `exception_info=null`.
Harbor-recorded `cost_usd` sums to 0.0793556. k=1 still has no scored
Codex job on the primary checkout. k=5 remains unrun (`per_job_cost_ceiling`
plus canary `max_attempts=3`). Do not call this “sensitivity” yet.
