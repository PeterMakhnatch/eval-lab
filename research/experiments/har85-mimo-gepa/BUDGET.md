# HAR-85 budget: formula with measured counts and per-trial estimates

Status: STAGED ONLY. No paid call made; no approval recorded. Policy ceilings:
per_job_cost_ceiling_usd 3, daily_cost_ceiling_usd 20
(policy/standing-approvals.yaml). Every per-trial ceiling below is < $3, so no
cap raise is needed for the staged plan. Trials run in Daytona (base spec
`environment: daytona`), so each trial has two spend types, reported
separately: model tokens (metered Z.ai OpenAPI balance) and sandbox time
(metered Daytona).

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

## Per-trial estimates (the $0.25 queue estimate covers both spend types)

- `C_STUDENT_TRIAL` = model + sandbox, est_cost_usd 0.25 in the base spec
  (the queue's policy estimate; per-trial model ceiling cost_limit_usd 2.00).
  - Model: glm-5.3-flash Terminus-2 at list price ≈ $0.01–0.03/trial (proxy
    from research/evidence/runs/zai-wave2-flash-matrix; LOW-MED confidence,
    no Terminus-2 MiMo trial exists yet).
  - Daytona sandbox: MiMo terminal tasks request 1 vCPU + 2 GiB →
    $0.0504 + 2 × $0.0162 = $0.0834/h (https://www.daytona.io/pricing, read
    2026-09-28; storage free below 5 GiB) ≈ $0.02–0.03 for a 15–20 min
    trial; TTL-bounded worst case 70 min ≈ $0.10. Harness `cost_usd` does
    NOT include sandbox time; settle it from the Daytona bill, never mixed
    into model cost.
- `C_PROPOSER_CALL`: one OpenCode Flash reflection call (per-call ceiling
  $0.05, max_proposer_cost_usd $0.20 for 4 calls).
- Search est: 32 x $0.25 = $8.00; held-out est: 32 x $0.25 = $8.00.
- Program est total ≈ $16.20 at estimates, under the $20 daily ceiling if both
  phases dispatch the same day. Expected actual: 64 trials × ($0.01–0.03 model
  + $0.02–0.03 sandbox) + ≤ $0.20 proposer ≈ $2.12–4.04, settled from trial
  receipts and the Daytona bill, not estimates.

## Options B/C deltas (proposer-options.json)

- B: 8 reflection calls, proposer cap $0.40 → +4 x C_PROPOSER_CALL.
- C: 2 reflection calls, proposer cap $0.10 → -2 x C_PROPOSER_CALL.
- Student-route swap (HAR-81 Qwen-on-Tinker later): replace
  base-specs/student-terminus2-provisional.json, keep the same formula with the
  new route's C_STUDENT_TRIAL.

## Approval rounds (after the baseline/selection batching fix)

- Baseline gate and final selection loop park EVERY example before halting
  (`src/evallab/gepa_optimizer/workflow.py` `_evaluate_allowing_pending`):
  baseline = 1 round (8 specs), selection = 1 round.
- In-engine search still halts per novel batch: worst case 24 rounds, typical
  a handful. Worst case ≈ 26 rounds total (was ~33); typical ≈ 8-12.
- Trial/proposer counts above are unchanged; only the round count moved.

## In-engine concurrency verdict: NO (evidence, 2026-09-28)

- Upstream (pinned gepa @0632cdb) DOES fan minibatch examples over threads
  when the adapter runs parallel batches: `OptimizeAnythingAdapter.evaluate`
  takes the `_evaluate_parallel` path for `len(batch) > 1`
  (`adapters/optimize_anything_adapter/optimize_anything_adapter.py:386-389`),
  all futures submit upfront and worker exceptions propagate via
  `future.result()` (`:628-635`); our engine gets legacy `GEPAConfig`
  defaults (`parallel=True`) through `engine_config={}`
  (`oa/engines/gepa.py:48-52`, `gepa_launcher.py:1363-1364`); actual execution
  is gated by our eval-server semaphore
  (`oa/eval_server.py:197`, singular `evaluate` propagates at `:233-264`).
  Every started spec would submit before propagation — answer 1 is yes.
- But our evaluator is NOT thread-safe: `_ensure_candidate_stored` checks
  `exists()` then `open("x")` (`src/evallab/gepa_optimizer/evaluator.py:603-611`),
  so concurrent same-candidate stores (e.g. the seed on 8 examples) fail all
  but one worker with `FileExistsError` → spurious `evaluation_failed`.
  `AggregateBudget.reserve` IS safe (fcntl `LOCK_EX` read-modify-write +
  duplicate detection, `src/evallab/gepa_optimizer/budget.py:71-103,153-184`)
  and queue submit uses `O_EXCL` unique names plus a locked event log
  (`src/evallab/queue.py:1226-1231,824-838`) — answer 2 is no.
- So no concurrency campaign field was added and the race was left untouched:
  widening the semaphore today would turn the batched seed eval into
  evaluation failures. Revisit only with a store-level fix plus a concurrent
  same-candidate test.
