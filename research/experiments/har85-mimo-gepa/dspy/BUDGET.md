# HAR-85 DSPy arm: budget formula + measured inputs (2026-09-28; sealed rebind 2026-09-29)

## Spend type (read first)

The staged student route (`zai-coding-plan/glm-5.3-flash`) and reflection
model (`zai-coding-plan/glm-5.3`) bill against the **Z.ai coding-plan
SUBSCRIPTION window quota** (see `research/experiments/rlm-harness-20260916/README.md`:
a Pro 5-hour window holds 12 000 credits; the post-reset reference batch of
16 trials + 108 bench rollouts used ~7% ≈ 840 credits ≈ ~47 credits/trial).
They are NOT metered API spend.

Dollar figures below are **API-list-price equivalents** in the units the
harness already meters (`cost_usd`: $1.40/$4.40 per M tokens,
`src/evallab/rlm/harness.py:85-86`, mirroring
`src/evallab/execution_contracts.py:188-189`). They exist so a pilot can be
checked against the standing per-job ceiling; the binding constraint at run
time is the subscription window, not a card charge.

The metered OpenAPI route (`zai/glm-5.3-flash`, $0.15/$0.50 per M) is a
DIFFERENT route through a different endpoint with a different credential
transport. The RLM lane cannot use it without a lane change (agent kwarg +
lane validation + key transport); the staged path keeps the coding-plan
route. See README.md "Student route verdict".

Task containers run in **Daytona** (`--harbor-env daytona`, bound into the
approval): that part is **METERED** sandbox spend, billed per second at
$0.0504/vCPU-h + $0.0162/GiB-h (https://www.daytona.io/pricing, read
2026-09-28; storage free below 5 GiB). MiMo terminal tasks request 1 vCPU +
2 GiB, so a sandbox costs $0.0834/h. It is reported as its own line, never
folded into the model API-equivalent figure. The TTL (`ttl_minutes=40`)
bounds a sandbox orphaned by a dead controller at ≈ $0.056.

## Formula (enforced by `verify_approval` via `expected_cost_usd`)

Per phase binding:

- phase gepa: `max_metric_calls × 2.5 × ($0.06 + $0.007) + ceil(trials/10) × $0.10`
  (model API-equiv) `+ trials × $0.03` (Daytona)
- phase heldout: `16 × 2 × attempts × $0.06` (model API-equiv) `+ trials × $0.03` (Daytona)

`verify_approval` checks the SUM of both parts against the bound cap;
`--print-binding` prints each part separately.

Inputs and where they were measured ($0 work, this branch):

| input | value | source |
|---|---|---|
| trial API-equiv $ | $0.06 ($0.053 LM + margin) | feasibility trial: 14 309 in + 7 851 out tokens at $1.40/$4.40 (`runs/har85-feasibility/jobs/har85-dspy-feasibility-0036/.../agent/rlm/usage.json`) |
| trace-seed call $ | $0.007 | 1 799 in + 1 003 out tokens, same trial |
| metric-call overshoot | 2.5× | $0 dry run: 19 metric calls for `max_metric_calls=8` (2.4×); budget rounds up |
| reflection calls | ~1 per 10 rollouts | $0 dry run: 2 proposals / 19 metric calls (reflection calls ≥ proposals) |
| reflection call $ | $0.10 | glm-5.3 (non-flash) planning assumption at list prices; recheck from real usage after the pilot |
| per-trial ceiling | $1.00 (`cost_limit_usd`) | enforced in-harness by `LabRlm` (`src/evallab/rlm/harness.py`); worst case, never the plan |
| held-out trials | 13 scorable tasks × 2 arms × attempts (16 sealed held-out minus 0260/0674/2376) | sealed manifest `c3df70a5…52dab` via `sealed_split.heldout_ids`; paired winner-vs-base |
| Daytona sandbox $ per trial | $0.03 (≈ 22 min at $0.0834/h) | list price above × task.toml resources (1 vCPU / 2 GiB); planning estimate, no paid Daytona RLM trial yet |

## Costed scopes (staged coding-plan student)

| scope | model API-equiv (subscription) | Daytona (metered) | total vs phase cap | ceiling change |
|---|---|---|---|---|
| pilot phase 1 (4 train + 2 val, 10 metric calls, 25 trials) | 25×$0.067 + 3×$0.10 = $1.98 | 25×$0.03 = $0.75 | **$2.73** / $3 | none (standing `per_job_cost_ceiling_usd` 3 covers it) |
| full phase 1 (48-task sealed train pool, 36 metric calls, 90 trials) | 90×$0.067 + 9×$0.10 = $6.93 | $2.70 | **$9.63** / $10 | raise `per_job_cost_ceiling_usd` 3 → 10 for that job |
| phase 2 (13 scorable heldout × 2 arms × 3 attempts, 78 trials) | 78×$0.06 = $4.68 | $2.34 | **$7.02** / $9 | raise `per_job_cost_ceiling_usd` 3 → 9 for that job |

## Time-based student (self-hosted distill): planning numbers, BLOCKED

The experiment student is the self-hosted distill, but this lane cannot dial
it without new transport code (see README.md "Student route verdict (sealed
rebind)": wrong endpoint/key, phantom ceiling accounting). No paid DSPy trial
runs on that route until a lane change lands, so the staged binding above
keeps the coding-plan student and these figures are planning-only. Per-trial
price on the distill route is $0; cost is time-based per
`evallab.execution_contracts.mimo_selfhosted_trial_cost_usd`
(= 2.8149 × trial_hours ÷ concurrency + sandbox_usd; plus ≈ $0.40 per warm
period). Model cost and sandbox/GPU cost are separate lines; missing data is
`None` with a reason, never 0:

- Model/GPU line per trial: 2.8149 × trial_h / c. Trial length for an RLM
  rollout on this route is `None` (no RLM trial has run on the distill; the
  539 s HAR-90 mean is Terminus-2, not RLM). Worst case at c=1 (direct
  `harbor run`, sequential, `num_threads=1`): full 900 s agent timeout =
  **$0.70**.
- Sandbox line per trial: terminal $0.0834/h. Worst case (900 + 240 + 900 s
  TTL): **$0.047**. At the HAR-81 expected 839 s: **$0.019**.
- Warm line: **~$0.40 per warm period**; count per phase `None` (depends on
  dispatch gaps vs the 300 s scale-to-zero).
- Pilot scope (25 trials) worst case: 25 × ($0.70 + $0.047) = **$18.7** +
  warms; phase 2 (78 trials) worst case: 78 × $0.747 = **$58.3** + warms --
  both far above the staged $3/$9 caps, because direct sequential trials
  share the server with nobody (c=1). Sharing (concurrent trials on one warm
  server) divides the GPU line by c; the cap for a future self-hosted pilot
  must be derived from measured RLM trial lengths, not from these bounds.
- RLM-specific unknowns stay `None`: rollout length, parse-loop behaviour and
  ceiling trips of the RLM loop on the distill (the HAR-90 loops are
  Terminus-2 evidence and do not transfer).

The pilot was 12 metric calls before trials moved to Daytona; 12 calls now
expect $3.21 and `verify_approval` refuses them at a $3 cap (exercised), so
the pilot is 10 calls. The daily $20 ceiling covers any single phase. The cap
is part of the binding: raising it invalidates the approval sha and forces a
re-derive + re-sign.

Subscription quota, two bounds from the same reference batch (840 credits):
at ~7 credits per rollout-ish unit (840 / 124 units) the pilot (~28 units)
uses ≈ 2% of one 5-hour window, full phase 1 (~99) ≈ 6%, phase 2 (96) ≈ 6%;
if trials dominate (~47 credits/trial, above) the pilot's 25 trials use
≈ 10%, full phase 1 ≈ 35%, phase 2 ≈ 38%. No phase needs more than one
window. If the window is already partially consumed, run the phase in a
fresh one; the launcher does not check this — the approver does, before
signing.
