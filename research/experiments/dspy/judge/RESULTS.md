# DSPy calibration judge — results (2026-09-16)

Model: GLM 5.3 Flash (`zai/glm-5.3-flash`, hidden thinking off, temperature 0)
via the Lab's Z.ai Coding Plan route. Metric: exact per-criterion agreement with
the sealed keys (`evallab.calibrate.dspy_metric`). Every number below has a
`artifacts/<run>/metrics-<family>-<split>.json`; full-family runs also have a
`bundle-<family>.json` and, when clean, a record under
`research/calibration/records/<family>/`.

**Clean** = the optimizer never saw any document of the scored set.
**Contaminated** = 16 of 22 scored documents were optimizer-visible; reported
for completeness only, never written as a record.

## Headline

| Program | Trained on | checkout heldout (6) | checkout all (22) | retry-storm heldout (6) | retry-storm all (22) |
|---|---|---|---|---|---|
| Prior record: Codex gpt-5.6-sol, plain prompt (2026-08-14) | — | — | 0.763 | — | — |
| DSPy CoT, unoptimized | — | 0.881 (subset of all) | **0.851** clean | — | **0.818** clean |
| GEPA light (120 metric calls) | checkout train 12 / val 4 | **0.988** clean | 0.971 *contaminated* | 0.948 clean | **0.912** clean — meets 0.90 |
| GEPA light | retry-storm train 12 / val 4 | pending (`after-reset.sh`) | pending | pending | *contaminated* |

Records written (all `status: measured`, `--skip-catalog`; catalog rows can be
added with `evallab calibrate ... --predictions` against the shared database):

- `checkout-pool-exhaustion-20260916-glm-5-3-flash-bbfcdacbd3.json` — unoptimized, 0.8506, below floor
- `retry-storm-backlog-20260916-glm-5-3-flash-e05b4fda9f.json` — unoptimized, 0.8182, below floor
- `retry-storm-backlog-20260916-dspy-gepa-checkout-glm-5-3-flash-e05b4fda9f.json` — GEPA (checkout), **0.9119, meets floor** (321/352 cells; four-cell margin)

The daily digest line is now: "1 of 4 measured record(s) reach their agreement
floor; best retry-storm-backlog / dspy-gepa-checkout glm-5.3-flash, mean
agreement 0.912 against a 0.90 floor over 22 documents (2026-09-16)."

## Per-criterion movement on the unseen family (retry-storm, all 22)

| Criterion | unoptimized | GEPA (checkout) | Δ |
|---|---|---|---|
| `evidence_fidelity.invents_evidence` | 8/22 | 17/22 | +9 |
| `causal_reasoning.uncertainty_is_genuine` | 17/22 | 22/22 | +5 |
| `causal_reasoning.grounded_in_evidence` | 16/22 | 21/22 | +5 |
| `action_quality.actions_are_actionable` | 17/22 | 21/22 | +4 |
| `action_quality.actions_trace_to_findings` | 17/22 | 20/22 | +3 |
| `action_quality.proposes_unsupported_work` | 19/22 | 21/22 | +2 |
| `causal_reasoning.separates_contributing_factors` | 15/22 | 17/22 | +2 |
| `action_quality.bounds_the_amplification` | 21/22 | 22/22 | +1 |
| `causal_reasoning.rules_out_the_decoys` | 14/22 | 15/22 | +1 |
| `evidence_fidelity.asserts_unsupported_cause` | 19/22 | 20/22 | +1 |
| `evidence_fidelity.treats_db_cpu_as_cause` | 21/22 | 22/22 | +1 |
| `action_quality.closes_the_detection_gap` | 20/22 | 20/22 | 0 |
| `causal_reasoning.separates_trigger_from_cause` | 20/22 | 20/22 | 0 |
| `evidence_fidelity.blames_the_deploy` | 22/22 | 22/22 | 0 |
| `evidence_fidelity.misstates_a_fact` | 20/22 | 20/22 | 0 |
| `causal_reasoning.identifies_the_mechanism` | 22/22 | 21/22 | −1 |

Remaining weak spots on the unseen family: `rules_out_the_decoys` (15/22) and
`separates_contributing_factors` (17/22) — both are family-specific judgement
calls the checkout-trained instruction says nothing about.

## GEPA run facts (`artifacts/gepa-checkout/optimize-summary.json`)

- 12 candidates; validation aggregate by candidate: 0.821 (seed), 0.893, **0.982**, 0.982, 0.964, 0.946, 0.964, 0.946, 0.982, 0.964, 0.982, 0.964; best index 2.
- Task LM: 137 calls, 429k prompt / 129k completion tokens, ≈$0.14. Reflection LM (same model, temperature 1.0, 16k max tokens): 12 calls, 110k / 26k tokens, ≈$0.03. Wall time 29 min at 3 threads.
- No demos were added (GEPA edits instructions only); the compiled instruction grew from ~400 to 6,966 characters (117 lines).

### What the compiled instruction contains

General rules that transferred (paraphrased from `compiled_instructions`):
absent or "TBD" sections make every positive criterion in that dimension `no`;
generic filler contributing factors are `no` even when a section exists;
open questions about the postmortem *template* are not genuine uncertainty;
`invents_evidence` is `yes` only for a *named* fabricated artifact, and
fabricated support for a correct mechanism goes under `invents_evidence`, not
`asserts_unsupported_cause`; work premised on an unsupported cause is
`proposes_unsupported_work` even when actions elsewhere are sound.

Family-specific content that did **not** transfer and is a memorised shortcut:
a "Domain context (checkout-pool-exhaustion family)" block restating the seven
reference facts, and — verbatim — *"Per reviewer feedback, artifacts like
`deploys.csv`, `metrics.csv`, `service-config.yaml`, and `alerts.log` DO appear
in the evidence pack and are NOT invented — do not flag them."* This is the
missing-evidence input-contract confound (`research/inspections/judge-floor.md`)
learned as a fact about one family's fixtures. It explains the +15 on
`invents_evidence` on checkout (contaminated) versus +9 on retry-storm (clean).

## Cost and rate limits

Whole night through 03:12 EDT: ≈330 GLM calls, ≈$0.35 at LiteLLM list prices
(subscription transport, so no dollars moved). The Coding Plan's 5-hour usage
window was exhausted at 03:11 EDT ("Usage limit reached for 5 hour", reset
18:39 provider time ≈ 06:39 EDT), which killed the reverse-transfer run at
iteration 2; `after-reset.sh` resumes it from GEPA's checkpoint. Eight concurrent
requests trip the per-request limit; three are tolerated.

## Not run tonight (queued as next steps, each ≈150 calls)

- MIPROv2 light on checkout (instructions + 2 bootstrapped demos) for an
  optimizer comparison at equal budget.
- `--train-family both` GEPA: does seeing two rubrics remove the family-specific
  shortcut from the instruction, and what does it score on both held-out sets?
- `--thinking` ablation of the unoptimized baseline (hidden reasoning on, 16k
  budget) as the one-variable check that turning thinking off did not cost accuracy.
