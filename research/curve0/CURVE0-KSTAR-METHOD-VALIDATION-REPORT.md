---
type: study-report
topic: curve0-kstar-method-validation
author: analyst
date: 2026-08-27
status: complete
epistemic: method validation (no capability claims)
collection: trajectory-analysis
reviewed: 2026-08-27
authorized_by: Peter (execution), Research-Eval Capabilities (clearance conditions)
artifact: research/curve0/results/kstar_validation.json
code: research/curve0/kstar_validation.py
fetch: research/curve0/fetch_corpora.sh
---

# Curve 0 — Derailment-Point ($k^*$) Method Validation

**Claim type: METHOD VALIDATION ONLY.** No capability claim about any model is
made or implied. No effects are pooled across corpora. No LLM-judged labels. No
re-runs, no model calls. Everything read-only.

## Headline

| Predicate | Verdict |
|---|---|
| `aci_state_stall` | **REJECTED.** Fires on 80% of failures but **63.3% of successes** (FPR $= 0.633$). Not measuring irreversibility. |
| `no_progress_lock` | **Surviving, underpowered.** Where the proxy is valid (tau-bench): fires 2.6% of failures, **FPR $= 0.0$**. High precision, near-zero recall. |
| `blind_retry_lock` | **Surviving, underpowered.** tau-bench: 1.3% of failures, **FPR $= 0.0$**. |
| `error_cascade_to_end` | **UNTESTABLE.** No corpus obtained carries structured exit codes. |

**The most valuable result is the rejection.** `aci_state_stall` is the
intuitively appealing predicate — "the agent stopped moving, so it's stuck." It
fires on nearly two-thirds of *successful* runs, because successful agents also
settle into one file and finish. Had we built the engine on it we would have
shipped a coordinate that measures task shape, not capability. This is exactly
what Curve 0 existed to catch, and it cost zero compute.

## Corpora

Per-corpus provenance, as required by the clearance conditions. Two corpora that
both "publish traces" are not interchangeable.

| Corpus | Disposition | Public | Pin | License | Per-step state | Structured exit codes |
|---|---|---|---|---|---|---|
| `swebench-verified-sweagent-gpt4` | available | yes | `SWE-bench/experiments@1faa91ca…`; trajs from `s3://swe-bench-submissions/verified/20240402_sweagent_gpt4/trajs` | MIT | **yes** (interface state) | no |
| `taubench-airline-gpt4o` | available | yes | `sierra-research/tau-bench@59a200c6…` | MIT | no | no |
| `local-atif-harbor` | available | **no** | local working tree | internal | no | no |
| `vending-bench-1-2` | **unavailable** | — | — | closed | — | — |
| `agentlab-browsergym-tmlr` | **unavailable** | — | — | Apache-2.0 | — | — |
| `osworld-2.0-trajectory` | **unavailable** | — | — | gated | — | — |
| `swe-rebench-openhands` | **unavailable** | — | — | CC-BY-4.0 | — | — |

Unavailable reasons are recorded per source in the artifact. No dummy rows were
emitted for any of them.

**Correction to the Curve 0 spec.** The spec named Vending-Bench as the ideal
substrate because its per-step ledger state makes state-divergence $k^*$
directly measurable. **That was wrong**: Vending-Bench (Andon Labs,
arXiv:2502.15840) is closed — no per-step trace dump exists on GitHub or
HuggingFace; community projects publish runners, not traces. The spec is
corrected by this artifact. AgentLab was excluded on practicality (~42.9 GB per
split part, no granular fetch API), OSWorld on gating (HTTP 401), SWE-rebench on
size (~2.08 GB, deferred).

## Results

Rates are conditioned on **eligible** trials only (≥ 4 actions and the
predicate's required fields present). Null where an arm is below $\text{MIN\_N}=5$.

### `swebench-verified-sweagent-gpt4` — 60 trials (30 resolved / 30 unresolved)

| Predicate | Eligible fail / succ | Fire on failures | **FPR on successes** | $k^*$ position (median) |
|---|---|---|---|---|
| `aci_state_stall` | 30 / 30 | 0.800 (24) | **0.633 (19)** | 0.429 |
| `no_progress_lock` | 30 / 30 | 0.0 | 0.0 | — |
| `blind_retry_lock` | 30 / 30 | 0.0 | 0.0 | — |
| `error_cascade_to_end` | — | PREDICATE_UNAVAILABLE | — | — |

Observation-digest stability: **0.58 instability → `PROXY_INVALID`.** 58 of 100
repeated-action groups produced *differing* observation digests. So the two
zero-fire results here are **inconclusive, not negative** — the information-intake
proxy itself is broken on this corpus.

Length confound flagged: mean actions 12.03 (success) vs 21.67 (failure). Any
fire-rate difference on this corpus is confounded with trajectory length.

### `taubench-airline-gpt4o` — 200 trials (84 success / 116 fail)

| Predicate | Eligible fail / succ | Fire on failures | **FPR on successes** | $k^*$ position (median) |
|---|---|---|---|---|
| `no_progress_lock` | 78 / 36 | 0.0256 (2) | **0.0 (0)** | 0.783 |
| `blind_retry_lock` | 78 / 36 | 0.0128 (1) | **0.0 (0)** | 0.826 |
| `aci_state_stall` | — | PREDICATE_UNAVAILABLE | — | — |
| `error_cascade_to_end` | — | PREDICATE_UNAVAILABLE | — | — |

Observation-digest stability: **0.045 instability → `PROXY_USABLE`** (21 of 22
repeated-action groups gave identical digests). **This is the only corpus where
P1/P2 results are interpretable**, and there both predicates show zero false
positives with very low recall. Both fires sit late in the trajectory
(normalized position 0.78–0.83), which is directionally consistent with an
irreversibility boundary — on $n = 3$ total fires, which is far too few to claim
anything.

Eligibility mattered: **86 of 200 trials excluded** (18 with zero actions, 68
with 1–3). Without eligibility conditioning these rates would have been computed
over 200 and understated by roughly 43%.

Length confound: 4.13 vs 7.04 mean actions — flagged "comparable" by the
threshold, but the 2.91 gap is *borderline* and should be treated as suspect.

### `local-atif-harbor` — 26 trials (11 success / 15 fail), NOT public

All predicates either unavailable or zero-fire on 9 eligible failures / 5
eligible successes, both at or below $\text{MIN\_N}$. Observation-digest
instability **1.0** (3/3) → `PROXY_INVALID`. This corpus is short-horizon
(max 35 steps) and contains almost no repeated actions (2/26 trials), so it
cannot exhibit derailment and cannot validate a long-horizon claim. It served as
a smoke-test substrate only.

## Method defects found

1. **`aci_state_stall` is invalid** (FPR 0.633). Interface-state stall is not
   irreversibility. Do not promote it. If a state-based predicate is wanted it
   must use *environment* state, not interface state — and no public corpus we
   obtained carries per-step environment state.
2. **The information-intake proxy is corpus-dependent.** Raw observation-digest
   equality is defeated by nondeterministic content (wall-time strings,
   timestamps, PIDs). Instability ranged 0.045 → 1.0 across three corpora. Fix:
   compute digests over **normalized** observations (strip known nondeterministic
   spans), and report the instability rate alongside every P1/P2 result so nulls
   are never misread as negatives.
3. **Exit codes are flattened into text across the entire public ecosystem.**
   Three corpora, zero with structured exit codes. `error_cascade_to_end` is
   untestable on public data. This independently justifies the P2 error-semantics
   work in our own pipeline as a real differentiator rather than housekeeping.
4. **A bug in the first implementation of this very study**: rates were initially
   conditioned on all trials rather than eligible trials. Caught by the diagnostic
   pass and fixed before any result was reported. Noted because it is precisely
   the denominator error the program doctrine warns about, committed by its own
   author.

## What this does and does not license

**Licensed:** the $k^*$ *method* is partially validated. Two predicates survive
with zero observed false positives on the one corpus where the proxy is valid;
one predicate is decisively rejected; one is untestable on public data. The
engine may carry $k^*$ as a coordinate type **only** with the instability rate
reported alongside, and **only** on corpora where the proxy is valid.

**Not licensed:** any statement about model capability; any pooled cross-corpus
statistic; any claim that $k^*$ is usable as an SFT/DPO pruning gate yet — recall
is far too low ($n = 3$ fires total) to gate anything. The amplifier thesis
remains untested: no corpus obtained had both long horizons and a valid proxy.

## Next steps

1. **Normalized-observation predicate**, then re-run. Highest value, cheap: it
   converts SWE-bench from inconclusive to interpretable and is the single change
   most likely to raise recall.
2. **Recall is the binding problem, not precision.** Both surviving predicates
   are near-zero recall. Either the predicates are too strict, or genuine
   irreversibility is rarer than assumed — which would itself be a finding worth
   publishing.
3. **Environment-state substrate.** No public corpus carries per-step environment
   state. Our own state journal does. That is a concrete argument for running our
   own long-horizon campaign rather than mining public traces further.
4. **SWE-rebench** (~2.08 GB, 50–250 turns/trial, resolved label in-row) is the
   best next public corpus for horizon length, once a normalized proxy exists.

## Reproduce

```bash
bash research/curve0/fetch_corpora.sh
python3 research/curve0/kstar_validation.py \
  --local-runs-root runs \
  --swebench-dir research/curve0/.cache/swebench \
  --taubench-file research/curve0/.cache/taubench/gpt-4o-airline.json \
  --out research/curve0/results
```

Corpora are fetched, never committed. Integrity: per-file sha256 for all 62
fetched artifacts is recorded in `results/kstar_validation.json`.

Preregistration digest
`5b762f9da633edc380b1768bc0e824335bfdb6ec4003eaffa0cacbdab3065d85` — predicates
and thresholds were fixed before outcomes were computed; transcribed from the
Curve 0 spec, not third-party timestamped. The digest covers the four predicate
ids, `TAIL_MIN`, `MIN_N`, the eligibility rule, the primary metric, and the
no-pooling constraint, so any later alteration to the preregistered design is
detectable by recomputing it.
