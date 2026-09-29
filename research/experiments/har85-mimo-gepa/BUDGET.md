# HAR-85 budget: time-based formula with measured counts and per-trial estimates

Status: STAGED ONLY. No paid call made; no approval recorded. Policy ceilings:
per_job_cost_ceiling_usd 3, daily_cost_ceiling_usd 20
(policy/standing-approvals.yaml). The worst-case per-trial estimate ($0.77) is
< $3, so no per-job cap raise is needed for the staged plan. Trials run in
Daytona (base spec `environment: daytona`), and the student is the self-hosted
distill (tokens priced at $0), so each trial has two spend types, reported
separately: GPU/server time (metered Modal) and sandbox time (metered
Daytona). The per-trial model-spend ceiling (`cost_limit_usd` $0.01) is
nominal and never trips; spend is bounded by the 900 s agent timeout plus the
proxy request/token ceilings.

## Formula

```
total_target_trials  = min(upstream eval calls, max_target_attempts) + selection re-evals
                     <= max_target_attempts                                   (= 32, enforced by AggregateBudget)
total_proposer_calls <= max_proposer_requests                                 (= 4 option A, 1 request each)
total_heldout        = heldout_tasks x arms x attempts                        (= 13 x 2 x 1 = 26)

C_STUDENT_TRIAL = mimo_selfhosted_trial_cost_usd(trial_hours, concurrency, sandbox_usd)
                = 2.8149 x trial_h / c + sandbox_usd   (+ ~$0.40 per warm period, counted separately)
$search  = total_target_trials x C_STUDENT_TRIAL + total_proposer_calls x C_PROPOSER_CALL
$heldout = 26 x C_STUDENT_TRIAL
```

Model (GPU/server) and sandbox lines are always reported separately; missing
inputs are `None` with a reason, never 0 (see Unknowns).

## Measured counts (rebound 2026-09-29; spend inputs from HAR-90/HAR-81)

| Input | Value | Source |
|---|---|---|
| Train search examples | 8 (sealed-split train only; 0109/0390/0534/1682 replaced by 1990/2207/0803/2836) | campaign-train.json (validated by load_campaign + train_pool.py check-campaign) |
| Baseline gate evals | 8 (seed x 8 examples) | workflow.py: seed evaluated once per example before search |
| max_evals (upstream budget) | 24 | campaign-train.json |
| max_target_attempts (hard target-trial cap) | 32 (= 24 + 8 best-candidate selection re-evals) | campaign-train.json; AggregateBudget refuses beyond |
| Attempts per trial | 1 (Terminus-2 binds exactly one trial) | execution_contracts.py terminus-2 validation |
| Trials per GEPA iteration | engine max_concurrency=1; each evaluator call = 1 trial | workflow.py make_config |
| Server concurrency (c) | 1 (conservative: search ticks sequentially; sharing only lowers the server share) | search-round.sh; `mimo_selfhosted_trial_cost_usd` divides by c |
| Reflection calls (option A default) | <= 4, each exactly 1 physical request | proposer-options.json A; opencode transport enforces max_requests=1 |
| Proposer token input per call | in <= 22,000, out <= 4,096, total <= 26,096 | proposer_ceilings (har59 precedent) |
| Held-out eval trials | 26 (13 scorable tasks x seed/gepa arms x 1) | make_paired_specs.py + heldout-exclusions.json |
| Server rate | $2.8149/h (A100-80GB $2.4984 + 4 cores $0.1886 + 16 GiB $0.1279) | modal.com/pricing 2026-09-28; `MIMO_SELFHOSTED_SERVER_USD_PER_HOUR` |
| Warm period | ~$0.40 (208 s cold start + 300 s idle tail) x $2.8149/h | HAR-90 smoke + follow-up spend ($1.9752 covered 3 warms + pairs A-C) |
| Expected trial length | 539 s agent (mean of HAR-90's 631 s + 447 s) + 5 min sandbox setup | HAR-81 `stage.py` EXPECTED_TRIAL_HOURS (+SETUP); pre-normalizer, ceiling-truncated -- see Unknowns |
| Healthy solve (pair C 0036-e) | 84 requests / 2.40M ledger input tokens in ~470 s agent; reward 1.0 | HAR-90 follow-up |
| Looping trials | up to 779 requests / 26.2M input tokens in 900 s | HAR-90 pair B/C |
| Proxy ceilings (this route) | 2000 requests / 64M input / 1M output (pair C, nothing tripped) | HAR-90 Ceilings; base spec pins them |
| Daytona sandbox rate (terminal) | 1 vCPU + 2 GiB -> $0.0834/h | daytona.io/pricing 2026-09-28; storage free below 5 GiB |
| Nop dry-run trial wall time | ~7-8 s each (setup ~1 s, agent ~0 s, verifier ~1-2 s) | runs/gepa-nop-candidate-{0036,0109,0260}-* (pre-seal, frozen evidence) |
| Deterministic proposer calls in dry run | 1, $0.00, review-gate stop | campaign attempt-26a22eed result.json |

## Per-trial costs (base spec `student-terminus2-selfhosted.json`)

- Model/GPU line: `mimo_selfhosted_trial_cost_usd` server share only (tokens $0).
  Expected at c=1: 2.8149 x (539/3600) = **$0.42**; worst case (full 900 s
  agent timeout): 2.8149 x 0.25 = **$0.70**.
- Sandbox line: terminal $0.0834/h. Expected ((539+300) s): **$0.019**; worst
  case (agent 900 s + verifier 240 s + 900 s margin = 2040 s): **$0.047**.
- Warm line: **~$0.40 per warm period**, counted separately (count: None, see
  Unknowns).
- Spec estimate (worst case, carried as `est_cost_usd`): 0.01 + 0.047 + 0.704
  = **$0.77** (= HAR-81 `stage.py worst_usd` for a terminal profile at c=1).
- `C_PROPOSER_CALL`: one OpenCode Flash reflection call (per-call ceiling
  $0.05, max_proposer_cost_usd $0.20 for 4 calls). Unchanged (coding plan).
- Search: <= 32 trials x $0.44 expected = **~$14.1** + warms + <= $0.20
  proposer; worst-case 32 x $0.77 = $24.64.
- Held-out: 26 trials x $0.44 = **~$11.5** + warms; worst-case 26 x $0.77 =
  $20.02.
- The $20 daily ceiling covers the held-out expected but not worst-case sums,
  and neither sum includes warms: dispatch in waves (as in HAR-81), compare
  measured spend against these lines before the next wave, and stop the Modal
  app between phases. The queue's $20/day ceiling adds each spec's $0.77
  estimate to catalog spend but never sees the Modal server bill -- the
  operator's controls for it are `modal billing report` and stopping the app.

## Unknowns (`None`, with reason)

- Warm-period count per phase: `None` -- the server scales to zero after 300 s
  idle, so the count depends on dispatch gaps no staged plan can fix. Budget
  one warm per wave; HAR-90 measured ~$0.40 each.
- Post-normalizer trial length: `None` -- the 539 s mean comes from
  ceiling-truncated pre-normalizer trials; normalized trials may run longer
  (pair C hit the agent timeout). The $0.77 spec estimate covers everything up
  to the 900 s timeout; re-estimate from the `--max-specs 1` smoke before
  ticking the rest.

## Options B/C deltas (proposer-options.json)

- B: 8 reflection calls, proposer cap $0.40 → +4 x C_PROPOSER_CALL.
- C: 2 reflection calls, proposer cap $0.10 → -2 x C_PROPOSER_CALL.
- Student-route swap: done (this rebind). Any future route change replaces
  `base-specs/student-terminus2-selfhosted.json` and recomputes this file with
  the new route's C_STUDENT_TRIAL.

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
