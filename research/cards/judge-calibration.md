# Eval card: judge-calibration-checkout-pool-exhaustion

Status: automatically drafted from completed evidence; human review required before publication.

## Question

Does the LLM judge backend (`harbor-codex-agent` with model `gpt-5.6-sol`) satisfy the mandatory policy floor of `mean_agreement >= 0.90` against the sealed ground-truth corpus for postmortem evaluation on family `checkout-pool-exhaustion`?

## Configuration and evidence

- Task: `registered/judge-calibration/checkout-pool-exhaustion`
- Completed spec: `research/calibration/records/queue-specs/checkout-codex-gpt-5-6-sol-authjson-20260814.json`
- Config digest: `sha256:86890290c3f2b615b474911f95d73d2bc2ff1bbbc0f7e9659191e916b6b1dc83`
- Harbor job: `runs/judge-checkout-codex-gpt-5-6-sol-authjson-20260814` (`checkout-pool-exhaustion-20260814-gpt-5-6-sol-bbfcdacbd3`)
- Harbor lock digest: `sha256:bbfcdacbd3746a5ec1554a7e8481777bebfc37b749da6a8f69901d3559991224`

## Result

- Task evidence units ($n_{\text{criteria}}$): **14**
- Labeled corpus documents ($n_{\text{docs}}$): **22**
- Evaluated judgment pairs ($n_{\text{judgments}}$): **308**
- Observed mean agreement: **0.763** (Bootstrap 95% interval: **[0.617, 0.896]** over 14 criteria; pairwise total: 235/308 = 0.763 [0.712, 0.807])
- Policy floor: **0.900**
- Gate disposition: **FAILED / BLOCKED** (`meets_floor = false`)
- Execution/harness exceptions: **0** (all 22 documents and 14 criteria evaluated without harness failures)

### Criterion-level breakdowns
- Perfect agreement (1.000, n=22, interval [0.851, 1.000]): `action_quality.fixes_the_capacity_coupling`, `causal_reasoning.identifies_the_mechanism`, `evidence_fidelity.blames_payments_vendor`
- High agreement (>=0.900, n=22): `action_quality.actions_trace_to_findings` (0.955 [0.782, 0.992]), `action_quality.closes_the_detection_gap` (0.955 [0.782, 0.992]), `causal_reasoning.rules_out_the_decoy` (0.955 [0.782, 0.992]), `action_quality.actions_are_actionable` (0.909 [0.722, 0.975])
- Sub-floor agreement (<0.900, n=22): `evidence_fidelity.misstates_a_fact` (0.864 [0.667, 0.953]), `causal_reasoning.grounded_in_evidence` (0.773 [0.566, 0.899]), `causal_reasoning.separates_contributing_factors` (0.773 [0.566, 0.899]), `causal_reasoning.uncertainty_is_genuine` (0.636 [0.430, 0.803])
- Severe breakdown (<0.500, n=22): `action_quality.proposes_unsupported_work` (0.364 [0.197, 0.570]), `evidence_fidelity.asserts_unsupported_cause` (0.318 [0.164, 0.527]), `evidence_fidelity.invents_evidence` (0.182 [0.073, 0.385])

### What this finding blocks
This below-0.90 finding (0.763 < 0.900) **BLOCKS** `gpt-5.6-sol` / Codex from acting as an autonomous evaluation judge on incident postmortems in the production evaluation pipeline. Specifically, the judge fails to detect fabricated evidence and unsupported claims, preventing unsupervised admission of judged capability metrics until prompt optimization (e.g., DSPy MIPROv2) or rubric recalibration achieves verified agreement >= 0.900.

## Elicitation tuple and caveats

```json
{
  "judge_backend": "harbor-codex-agent",
  "judge_model": "gpt-5.6-sol",
  "rubric_digest": "sha256:86890290c3f2b615b474911f95d73d2bc2ff1bbbc0f7e9659191e916b6b1dc83",
  "corpus_digest": "sha256:bbfcdacbd3746a5ec1554a7e8481777bebfc37b749da6a8f69901d3559991224",
  "document_count": 22,
  "k": 1
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: Elicitation provided raw candidate postmortem texts and rubric criteria definitions to the judge model. Prompt instructions did not include sealed answer keys or ground truth labels.

## Contamination note

- Contamination caveat: The 22 postmortem documents in `checkout-pool-exhaustion` constitute a sealed local calibration corpus with hidden answer keys (`research/calibration/checkout-pool-exhaustion/answer-keys/`). No postmortems or keys were exposed to model training sets or external search.

## Threats to validity

- Sensitivity on negative criteria: The model heavily underperforms on negative criteria (`invents_evidence` at 0.182, `asserts_unsupported_cause` at 0.318), suggesting a strong affirmative bias (lenient grading) when evaluating hallucinated details.
- Single incident scenario: Calibration was measured exclusively on the `checkout-pool-exhaustion` family (22 documents); transfer to other failure families (such as `retry-storm-backlog`) cannot be assumed without independent calibration.
- Sample size of corpus: With $n=22$ documents per criterion, per-criterion 95% intervals are moderately wide (+-15-20 percentage points).

## Regeneration query / command

```sql
SELECT
  record_id,
  family,
  judge_backend,
  judge_model,
  mean_agreement,
  agreement_floor,
  meets_floor,
  reportable,
  document_count
FROM judge_calibrations
WHERE record_id = 'checkout-pool-exhaustion-20260814-gpt-5-6-sol-bbfcdacbd3';
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
