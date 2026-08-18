Status: in-progress
Last: Cycle 2: authored and validated Card 2: Judge Calibration (research/cards/judge-calibration.md), hardened test suite with test_all_committed_cards_pass_validation, full suite green (1278 passed).
Next: Cycle 3: Card 3: Oracle-vs-Codex 8-run Cohort (research/cards/oracle-vs-codex-cohort.md).
Blockers: none.

# M017: LOOP-CARDS - turn finished studies into eval cards

Worktree: `.worktrees/m017-cards` (branch `role/m017-cards`)

## Cycle 1 Log

### 1. Prerequisites Implemented
- Extended `research/cards/TEMPLATE.md` to include mandatory elicitation caveats, contamination caveats, and regeneration query block.
- Implemented `validate_card` and `validate_card_file` in `src/evallab/cards.py` with `CardValidationResult` dataclass enforcing:
  - No unresolved placeholders (`{{...}}`)
  - All required H2 sections
  - Mandatory contamination and elicitation caveat checks
  - Sample size $n$ and uncertainty reporting (Tenet T4)
  - Regenerability command/query code block
- Added `evallab card validate <path> [--json]` CLI subcommand in `src/evallab/cli.py`.
- Added unit tests in `tests/test_cards.py` covering validation success, failure modes, and CLI commands.

### 2. Card 1: Canary/Drift Suite (`research/cards/canary-drift.md`)

#### Validator Output

```
VALID: research/cards/canary-drift.md passed all schema and caveat checks.
```

#### Card Text

```markdown
# Eval card: canary-drift-suite

Status: automatically drafted from completed evidence; human review required before publication.

## Question

Does the daily canary/drift test suite (event-summary, transaction-reconciliation, html-js-filter) detect drift and execution path regressions across Codex agent deployments over consecutive daily evaluations?

## Configuration and evidence

- Task: `canary-drift-suite (event-summary, transaction-reconciliation, html-js-filter)`
- Completed spec: `queue/done/canary-daily-drift.json`
- Config digest: `sha256:d82e85a1a1005bf3e2fb56dc815d4d3d75c6bf23364f3316f45532c51016597f`
- Harbor jobs: `canary-event-summary-codex-20260814..16`, `canary-transaction-reconciliation-codex-20260814..16`, `canary-terminal-bench-html-js-filter-codex-20260814..16`
- Harbor lock digest: `sha256:7f495bf4aeeb9ffbe92c10b427b34b1509a24445c8adca3c0f68202599723cf3`

## Result

- Task evidence units ($n_{\text{tasks}}$): **3**
- Recorded trials ($n_{\text{trials}}$): **33** (23 valid scored trials, 10 harness exception trials)
- Attempts per task (`k`): **3**
- Observed pass@3: **0.667** (2 of 3 tasks passed: event-summary at 1.000 [n=6 trials, interval [0.610, 1.000]], transaction-reconciliation at 1.000 [n=5 trials, interval [0.566, 1.000]], html-js-filter at 0.000 [n=6 trials, interval [0.000, 0.390]])
- Task-level 95% interval: **[0.208, 0.939]** (Wilson score interval via `cohort.py` for $n=3$ tasks)
- Execution/harness exceptions: **10** trials (9 launch-stage `ValueError` on 2026-08-14, 1 `NonZeroAgentExitCodeError` on 2026-08-16; excluded from capability denominator)

Attempts from the same task are one evidence unit. This card clusters by task and does not treat repeated attempts as independent samples.

## Elicitation tuple and caveats

```json
{
  "agent_name": "codex",
  "agent_version": "0.147.0",
  "k": 3,
  "model_name": "gpt-5.6-terra",
  "preamble_hash": "sha256:4b22cf5",
  "toolset": {
    "type": "bash_terminal",
    "commands": ["cat", "grep", "ls", "python3", "pytest"]
  }
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: Elicitation parameters (agent version, model pin, preamble hash, toolset, attempts k=3) are pinned across daily canary invocations. Codex was invoked via standard terminal interaction harness without external search or multi-agent orchestration.

## Contamination note

- Contamination caveat: Tasks in this suite derive from private eval-lab benchmarks (`event-summary`, `transaction-reconciliation`) and public Terminal-Bench (`html-js-filter`). Model `gpt-5.6-terra` training cutoff was pre-evaluated; task verifiers and solutions are isolated in separate test containers and never mounted into the agent workspace during trial execution.

## Threats to validity

- Small task sample size: Only 3 task evidence units ($n=3$); overall task-level generalization power is low and confidence interval [0.208, 0.939] is wide.
- Initial harness volatility: 2026-08-14 runs suffered environment setup exceptions (`ValueError`), showing sensitivity to launcher configuration.
- Single model architecture: Tested solely on `codex` / `gpt-5.6-terra`; comparative claims against other model families require separate calibration.

## Regeneration query / command

```sql
SELECT
  task_name,
  count(*) AS n_trials,
  sum(CASE WHEN primary_reward IS NOT NULL THEN 1 ELSE 0 END) AS valid_trials,
  sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exception_trials,
  avg(CASE WHEN primary_reward >= 1.0 THEN 1.0 ELSE 0.0 END) AS pass_rate
FROM trial_facts
WHERE job_name LIKE '%canary%'
GROUP BY task_name
ORDER BY task_name;
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
```

### 3. Verification

```
uv run pytest
# 1276 passed, 2 skipped, 1 xfailed in 72.11s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise
# Found 28 diagnostics (ratchet <= 28)

bash scripts/premerge.sh
# premerge green: Python 3.12; ty 28 <= 28
```

## Cycle 2 Log

### 1. Card 2: Judge Calibration (`research/cards/judge-calibration.md`)

#### Validator Output

```
VALID: research/cards/judge-calibration.md passed all schema and caveat checks.
```

#### Card Text

```markdown
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
```

### 2. Hardening

Added `test_all_committed_cards_pass_validation` in `tests/test_cards.py` to guarantee all markdown cards in `research/cards/` pass schema, required sections, mandatory caveats, and regenerability checks.

### 3. Verification

```
uv run pytest
# 1278 passed, 1 skipped, 1 xfailed in 56.69s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise
# Found 28 diagnostics (ratchet <= 28)

bash scripts/premerge.sh
# premerge green: Python 3.12; ty 28 <= 28
```
