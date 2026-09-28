# HAR-85 budget: formula with measured counts, $ as parent-filled placeholders

Status: STAGED ONLY. No paid call made; no approval recorded. Policy ceilings:
per_job_cost_ceiling_usd 3, daily_cost_ceiling_usd 20
(policy/standing-approvals.yaml). Every per-trial ceiling below is < $3, so no
cap raise is needed for the staged plan.

## Formula

```
total_target_trials  = min(upstream eval calls, max_target_attempts) + selection re-evals
                     <= max_target_attempts                                   (= 32, enforced by AggregateBudget)
total_proposer_calls <= max_proposer_requests                                 (= 4 option A, 1 request each)
total_heldout        = heldout_tasks x arms x attempts                        (= 16 x 2 x 1 = 32)

$search  = total_target_trials x C_STUDENT_TRIAL + total_proposer_calls x C_PROPOSER_CALL
$heldout = 32 x C_STUDENT_TRIAL
```

## Measured counts (filled by Har85Gepa 2026-09-28)

| Input | Value | Source |
|---|---|---|
| Train search examples | 8 (train ids only, held-out: none) | campaign-train.json (validated by load_campaign) |
| Baseline gate evals | 8 (seed x 8 examples) | workflow.py: seed evaluated once per example before search |
| max_evals (upstream budget) | 24 | campaign-train.json |
| max_target_attempts (hard target-trial cap) | 32 (= 24 + 8 best-candidate selection re-evals) | campaign-train.json; AggregateBudget refuses beyond |
| Attempts per trial | 1 (Terminus-2 binds exactly one trial) | execution_contracts.py terminus-2 validation |
| Trials per GEPA iteration | engine max_concurrency=1; each evaluator call = 1 trial | workflow.py make_config |
| Reflection calls (option A default) | <= 4, each exactly 1 physical request | proposer-options.json A; opencode transport enforces max_requests=1 |
| Proposer token input per call | in <= 22,000, out <= 4,096, total <= 26,096 | proposer_ceilings (har59 precedent) |
| Held-out eval trials | 32 (16 tasks x seed/gepa arms x 1) | make_paired_specs.py |
| Nop dry-run trial wall time | ~7-8 s each (setup ~1 s, agent ~0 s, verifier ~1-2 s) | runs/gepa-nop-candidate-{0036,0109,0260}-* |
| Nop dry-run rewards | 0.0 x 3, verifier completed, no exceptions | campaign attempt-26a22eed result.json |
| Deterministic proposer calls in dry run | 1, $0.00, review-gate stop | same report (candidate_review_required) |

## $ placeholders (parent fills from live prices)

- `C_STUDENT_TRIAL`: one Terminus-2 + zai/glm-5.3-flash trial (per-trial
  cost_limit_usd 2.00, est_cost_usd 0.25 → search est 32 x $0.25 = $8.00).
- `C_PROPOSER_CALL`: one OpenCode Flash reflection call (per-call ceiling
  $0.05, max_proposer_cost_usd $0.20 for 4 calls).
- Held-out est: 32 x $0.25 = $8.00 (ceilings $2.00/job).
- Program est total ≈ $16.20 at estimates, under the $20 daily ceiling if both
  phases dispatch the same day; actual Flash spend is expected in cents and is
  settled from trial receipts, not estimates.

## Options B/C deltas (proposer-options.json)

- B: 8 reflection calls, proposer cap $0.40 → +4 x C_PROPOSER_CALL.
- C: 2 reflection calls, proposer cap $0.10 → -2 x C_PROPOSER_CALL.
- Student-route swap (HAR-81 Qwen-on-Tinker later): replace
  base-specs/student-terminus2-provisional.json, keep the same formula with the
  new route's C_STUDENT_TRIAL.
