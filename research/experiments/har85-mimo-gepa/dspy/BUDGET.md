# HAR-85 DSPy arm: budget formula + measured inputs

Prices: Z.ai list-price equivalents from `src/evallab/rlm/harness.py`
($1.40/M input tokens, $4.40/M output tokens). Z.ai coding-plan billing is
a subscription window, so these are COMPARABILITY units; the binding
host-side controls are the approval caps + per-trial `cost_limit_usd=1.0`
(enforced in-loop by `LabRlm`: `rlm_budget_stopped` in trial metadata).

## Measured ($0, this branch, 2026-09-28)

Feasibility trial (1 real MiMo terminal task, DummyLM probe, local Docker):

| Stage | Wall | Notes |
|---|---|---|
| container start + `[environment.healthcheck]` | ~1.0 s | image cached; `/var/lib/mimo/ready` baked in |
| agent execution (3 RLM steps, real tool outputs) | ~1.4 s | DummyLM; paid = task work, up to 900 s task timeout |
| verifier (pytest `FFFFF`, reward 0.0) | ~1.3 s | of the 240 s verifier timeout |
| Harbor overhead (trial dirs, logs, teardown) | ~4.5 s | |
| total per trial | ~8.2 s | `harbor_trials_run` counts these |

GEPA dry run 2 (2 train + 1 val tasks, `--max-metric-calls 8`,
`--dry-run`, real containers + verifier per call):

| Counter | Value |
|---|---|
| `harbor_trials_run` (real trials) | 13 |
| `metric_calls_made` (GEPA count) | 19 |
| `proposer_calls_made` (scripted) | 2 |
| `elapsed_seconds` | 152.7 (~11.7 s/trial all-in) |
| outcome | `changed=false`: challengers evaluated on real 3-trial subsamples, ties at 0.0 correctly rejected |

Planning factors (small-sample, flagged as such):
- **Trial overshoot ≈ 2.4×**: GEPA made 19 metric calls / 13 trials against
  a cap of 8 (val tracking + proposal subsample evals are extra). Budget
  trials as `max_metric_calls × 2.5`.
- **Reflection invocations ≈ trials ÷ (train + val)**: 2 per 13 trials here.
  Paid reflection input ≈ 3 examples × ~5 KB feedback ≈ 15–20 KB; output cap
  16 K tokens ⇒ worst ≈ (20 K × $1.40 + 16 K × $4.40)/1 M ≈ **$0.10/call**.
- **Trace-seed call** (1 extra student-route call per rollout, output unused
  for scoring): ~4.5 KB in + ~0.2 KB out ⇒ ≈ **$0.007/call**.
- **Rollout agent**: hard ceiling `cost_limit_usd=1.0` per trial (enforced);
  expected from the rlm-harness lane on the same model family: $0.024–0.150
  per trial, plan at **$0.06**.

## Formulae

```
phase1_trials  = max_metric_calls × 2.5
phase1_cost    = phase1_trials × (0.06 + 0.007) + reflections × 0.10
                 where reflections ≈ phase1_trials ÷ (n_train + n_val)
phase2_trials  = 16 heldout × 2 arms × attempts
phase2_cost    = phase2_trials × 0.06        (no reflection, no trace seed)
worst_case     = trials × cost_limit_usd     (per-trial enforcement bound)
```

## Worked examples (what the approval `<CAP>` should say)

Pilot phase 1 (recommended first spend): 4 train + 2 val, `--max-metric-calls 12`
⇒ trials ≈ 30, reflections ≈ 5:
`30 × 0.067 + 5 × 0.10 ≈ $2.5` ⇒ **cap $3, fits the standing
per-job ceiling; no raise needed.**

Full phase 1: 6 train + 3 val, `--max-metric-calls 36`
⇒ trials ≈ 90, reflections ≈ 10:
`90 × 0.067 + 10 × 0.10 ≈ $7.0` ⇒ **cap $10; Peter must raise
`per_job_cost_ceiling_usd` (standing: $3) first.**

Phase 2 (final paired held-out, once): 16 × 2 × 3 = **96 trials**,
`96 × 0.06 ≈ $5.8` ⇒ **cap $12; same per-job raise required.**
Worst-case bound 96 × $1.0 = $96 (only if every trial burns its full
ceiling; expected is the $5.8 above). Daily standing cap $20 covers one
phase at a time; do not run both phases the same day without a daily raise.

## Limits

- Overshoot/proposer ratios come from one 13-trial dry run; re-estimate
  from phase-1 logs before approving phase 2.
- Dry-run scores are all 0.0 (DummyLM probe never solves); acceptance of a
  challenger requires score variance, which needs a solving (paid) agent.
- Verifier is deterministic pytest (no judge cost); terminal images ~0.29 GB
  (already local for the tried tasks; further tasks pull on first use).
