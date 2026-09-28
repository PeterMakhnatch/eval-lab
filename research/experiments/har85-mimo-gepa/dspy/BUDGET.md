# HAR-85 DSPy arm: budget formula + measured inputs (2026-09-28)

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

## Formula (enforced by `verify_approval` via `expected_cost_usd`)

Per phase binding:

- phase gepa: `max_metric_calls × 2.5 × ($0.06 + $0.007) + ceil(trials/10) × $0.10`
- phase heldout: `16 × 2 × attempts × $0.06`

Inputs and where they were measured ($0 work, this branch):

| input | value | source |
|---|---|---|
| trial API-equiv $ | $0.06 ($0.053 LM + margin) | feasibility trial: 14 309 in + 7 851 out tokens at $1.40/$4.40 (`runs/har85-feasibility/jobs/har85-dspy-feasibility-0036/.../agent/rlm/usage.json`) |
| trace-seed call $ | $0.007 | 1 799 in + 1 003 out tokens, same trial |
| metric-call overshoot | 2.5× | $0 dry run: 19 metric calls for `max_metric_calls=8` (2.4×); budget rounds up |
| reflection calls | ~1 per 10 rollouts | $0 dry run: 2 proposals / 19 metric calls (reflection calls ≥ proposals) |
| reflection call $ | $0.10 | glm-5.3 (non-flash) planning assumption at list prices; recheck from real usage after the pilot |
| per-trial ceiling | $1.00 (`cost_limit_usd`) | enforced in-harness by `LabRlm` (`src/evallab/rlm/harness.py`); worst case, never the plan |
| held-out trials | 16 tasks × 2 arms × attempts | split manifest `fb645fed…52dab` (16 heldout ids); paired winner-vs-base |

## Costed scopes

| scope | expected API-equiv | phase cap | ceiling change |
|---|---|---|---|
| pilot phase 1 (4 train + 2 val, 12 metric calls) | 30×$0.067 + 3×$0.10 = **$2.31** | $3 | none (standing `per_job_cost_ceiling_usd` 3 covers it) |
| full phase 1 (48 train, 36 metric calls) | 90×$0.067 + 9×$0.10 = **$6.93** | $10 | raise `per_job_cost_ceiling_usd` 3 → 10 for that job |
| phase 2 (16 heldout × 2 arms × 3 attempts) | 96×$0.06 = **$5.76** | $8 | raise `per_job_cost_ceiling_usd` 3 → 8 for that job |

The daily $20 ceiling covers any single phase. `verify_approval` refuses when
`expected_cost_usd(binding) > cap_usd` (exercised: $0.50 cap against a $2.31
binding refuses). The cap is part of the binding: raising it invalidates the
approval sha and forces a re-derive + re-sign.

At ~7 credits per rollout-ish unit (840 credits / 124 units in the reference
batch), the pilot (~33 units) burns roughly 230 credits ≈ 2% of one 5-hour
window; full phase 1 (~99 units) ≈ 6%; phase 2 (96 short trials) ≈ 6%. No
phase comes close to one window on credits. If the window is already
partially consumed, run the phase in a fresh one; the launcher does not
check this — the approver does, before signing.
