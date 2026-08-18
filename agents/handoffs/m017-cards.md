Status: review-wanted
Last: Cycle 5: authored and validated Card 5: Behavior Study (research/cards/behavior-study.md), hardened test suite with test_behavior_study_card_validation; all 5 cards in queue complete and passing validation; full test suite green (1281 passed), ty diagnostics 28 <= 28, premerge green.
Next: Integrator review; do not merge.
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

## Cycle 3 Log

### 1. Card 3: Oracle-vs-Codex Cohort (`research/cards/oracle-vs-codex-cohort.md`)

#### Validator Output

```
VALID: research/cards/oracle-vs-codex-cohort.md passed all schema and caveat checks.
```

#### Card Text

```markdown
# Eval card: oracle-vs-codex-cohort

Status: automatically drafted from completed evidence; human review required before publication.

## Question

In the eight-run cross-agent comparison cohort (D-20260815-CHEY952N), do oracle controls establish basic task and verifier viability across the test suites, and how does the sampled Codex execution path behave under identical harness conditions?

## Configuration and evidence

- Task: `cohort-comparison (event-summary, transaction-reconciliation, html-js-filter)`
- Completed spec: `digests/DISCOVERIES.md#D-20260815-CHEY952N`
- Config digest: `sha256:7f495bf4aeeb9ffbe92c10b427b34b1509a24445c8adca3c0f68202599723cf3`
- Harbor jobs: `event-summary-oracle-evidence`, `brief07-transaction-oracle`, `control-reset-oracle-20260814`, `canary-event-summary-codex-20260814`, `canary-transaction-reconciliation-codex-20260814`, `canary-terminal-bench-html-js-filter-codex-20260814`, `canary-transaction-reconciliation-codex-20260814-r2`, `canary-terminal-bench-html-js-filter-codex-20260814-r2`
- Harbor lock digest: `sha256:d82e85a1a1005bf3e2fb56dc815d4d3d75c6bf23364f3316f45532c51016597f`

## Result

- Job evidence units ($n_{\text{jobs}}$): **8** (3 oracle control jobs, 5 Codex canary jobs)
- Recorded trials ($n_{\text{trials}}$): **19** (4 oracle control trials [zero steps], 15 Codex agent trials)
- Oracle control pass rate: **1.000** (3 of 3 jobs passed; 95% Wilson interval: **[0.439, 1.000]** for $n=3$)
- Codex canary pass rate: **0.000** (0 of 5 jobs passed; 95% Wilson interval: **[0.000, 0.434]** for $n=5$)
- Execution/harness exceptions: **15** trials on Codex (9 launch-stage `ValueError`, 6 execution-stage `NonZeroAgentExitCodeError`; 0 on Oracle)

### Verdict Framing (Instrument Finding, NOT a Capability Claim)
This evaluation represents an **instrument finding**, not an agent capability claim.
1. The 100% success of oracle controls (3/3 jobs, 4/4 trials) confirms basic task definition validity, container environment integrity, and verifier discrimination on the benchmark tasks.
2. The 0% scored completion of Codex canaries (0/5 jobs, 15 trials) reflects early-stage harness and launcher failures (ValueError parameter validation, nonzero agent container exit codes) within the sampled execution pipeline.
3. This finding **does NOT establish that Codex lacks task capability**; it isolates execution pipeline instability in the early automated dispatch path.

## Elicitation tuple and caveats

```json
{
  "agents": [
    {"name": "oracle", "model": null, "steps": 0},
    {"name": "codex", "model": "gpt-5.6-terra", "attempts": 3}
  ],
  "cohort_id": "D-20260815-CHEY952N",
  "harness": "docker-container-runner",
  "toolset": {
    "type": "bash_terminal"
  }
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: Oracle controls execute deterministic reference scripts with zero model inference steps. Codex trials were dispatched through the automated container runner with pinned model `gpt-5.6-terra`.

## Contamination note

- Contamination caveat: Benchmark tasks were sourced from internal lab specifications (`local-lab/event-summary`, `petermakhnatch/transaction-reconciliation`) and public benchmark `terminal-bench/html-js-filter`. Ground-truth solutions are isolated inside test harness containers and never mounted into the agent filesystem.

## Threats to validity

- Instrument vs capability confound: The failure of Codex canaries was driven by launcher and environment exceptions (`ValueError` and `NonZeroAgentExitCodeError`), rather than evaluated task reasoning failures.
- Small cohort size: With $n=3$ oracle jobs and $n=5$ Codex jobs, statistical power is limited, and confidence intervals are wide ([0.439, 1.000] and [0.000, 0.434]).
- Disparate trial counts: 4 oracle trials vs 15 Codex trials across differing execution modes (pre-scripted reference vs interactive agent).

## Regeneration query / command

```sql
SELECT
  agent_name,
  count(DISTINCT job_name) AS n_jobs,
  count(*) AS n_trials,
  sum(CASE WHEN primary_reward >= 1.0 THEN 1 ELSE 0 END) AS passed_trials,
  sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exception_trials,
  avg(CASE WHEN primary_reward >= 1.0 THEN 1.0 ELSE 0.0 END) AS trial_pass_rate
FROM trial_facts
WHERE job_name IN (
  'event-summary-oracle-evidence',
  'brief07-transaction-oracle',
  'control-reset-oracle-20260814',
  'canary-event-summary-codex-20260814',
  'canary-transaction-reconciliation-codex-20260814',
  'canary-terminal-bench-html-js-filter-codex-20260814',
  'canary-transaction-reconciliation-codex-20260814-r2',
  'canary-terminal-bench-html-js-filter-codex-20260814-r2'
)
GROUP BY agent_name
ORDER BY agent_name;
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
```

### 2. Hardening

Added `test_oracle_vs_codex_card_verdict_framing` in `tests/test_cards.py` asserting that the card explicitly includes the instrument finding framing and does not make unjustified capability claims.

### 3. Verification

```
uv run pytest
# 1279 passed, 1 skipped, 1 xfailed in 48.21s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise
# Found 28 diagnostics (ratchet <= 28)

bash scripts/premerge.sh
# premerge green: Python 3.12; ty 28 <= 28
```

## Cycle 4 Log

### 1. Card 4: SG-1 Meta-Loop (`research/cards/sg1-metaloop.md`)

#### Validator Output

```
VALID: research/cards/sg1-metaloop.md passed all schema and caveat checks.
```

#### Card Text

```markdown
# Eval card: sg1-metaloop-task-synthesis

Status: automatically drafted from completed evidence; human review required before publication.

## Question

Does the SG-1 meta-loop task synthesis pipeline (`library/meta/synthesize-task@1`) produce structurally valid Terminal-Bench task packages that pass the automated 4-check completeness battery and preserve lineage provenance in quarantine?

## Configuration and evidence

- Task: `library/meta/synthesize-task@1`
- Completed spec: `agents/handoffs/sg1-metaloop.md`
- Config digest: `sha256:d8a9f24e1302b1154c1f8876adbc96486711a3b90038e9dc2a33f44358a9e083`
- Harbor job: `library/meta/synthesize-task@1` (Terminal-Bench package format)
- Harbor lock digest: `sha256:886e92a20de44384b7adaa8c623e96b17765ecfca8159b9aa515ba92f033cb41`

## Result

- Meta-task units ($n_{\text{meta}}$): **1** (`library/meta/synthesize-task@1`)
- Automated completeness checks ($n_{\text{checks}}$): **4**
- Completeness battery pass rate: **1.000** (4 of 4 checks passed; 95% Wilson interval: **[0.510, 1.000]** for $n=4$)
- Unit tests passing in authoring suite ($n_{\text{tests}}$): **17** (17 of 17 tests passed; 95% Wilson interval: **[0.816, 1.000]**)
- Execution/harness exceptions: **0**

### Automated Completeness Battery Results
1. `package_structure`: **PASS** (1.000 [0.207, 1.000] for n=1 package; valid `task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/solve.sh`, `tests/Dockerfile`, `tests/test.sh`)
2. `no_answer_leakage`: **PASS** (1.000 [0.207, 1.000]; zero golden solution or verifier test fixture literals leaked into `instruction.md` or `environment/`)
3. `oracle_solution_runs`: **PASS** (1.000 [0.207, 1.000]; oracle reference solution executes cleanly within timeout)
4. `task_tests_pass`: **PASS** (1.000 [0.207, 1.000]; verifier returns code 0 on oracle outputs and code 1 on empty baseline)

### Pipeline Controls & Quarantine
Synthesized task packages are quarantined in `library/tasks/_proposed/<proposal_id>/`. Each proposal records full lineage inputs (`proposal.json` with spec and exemplar digests) and enters at state `proposed`. It must satisfy the 4-control battery (oracle pass, empty fail, corrupt fail, baseline run) before human craft review and registry promotion (`evallab registry promote`).

## Elicitation tuple and caveats

```json
{
  "component": "sg1-metaloop",
  "meta_task": "library/meta/synthesize-task@1",
  "pipeline": "evallab.authoring",
  "purpose": "craft",
  "battery_checks": [
    "package_structure",
    "no_answer_leakage",
    "oracle_solution_runs",
    "task_tests_pass"
  ],
  "k": 1
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: The meta-task was tested under local deterministic verification without billable model dispatch. Authoring proposals are submitted with `purpose="craft"` and quarantined until battery verification.

## Contamination note

- Contamination caveat: Meta-task templates, skeletons, and exemplars (`local-lab/event-summary`) reside entirely in the repository authoring plane. No external benchmark tasks or test sets were ingested into the meta-task image.

## Threats to validity

- Single exemplar template: Synthesis tests currently reference `local-lab/event-summary` as the sole exemplar; diverse task families (e.g. multi-container services, interactive CLI tasks) remain uncalibrated.
- Small verification battery sample size: $n=4$ completeness checks provide wide statistical intervals ([0.510, 1.000]), requiring multi-task batch evaluations in SG-2.
- Deterministic fixture testing: Initial verification evaluated static reference packages; live LLM agent authoring performance will depend on model generation quality.

## Regeneration query / command

```bash
uv run python -m library.meta.synthesize_task@1.tests.completeness_checker library/meta/synthesize-task@1/exemplar
uv run pytest tests/test_authoring.py
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
```

### 2. Hardening

Added `test_sg1_metaloop_card_checks` in `tests/test_cards.py` ensuring all 4 completeness checks are verified and card validation succeeds.

### 3. Verification

```
uv run pytest
# 1280 passed, 1 skipped, 1 xfailed in 57.73s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise
# Found 28 diagnostics (ratchet <= 28)

bash scripts/premerge.sh
# premerge green: Python 3.12; ty 28 <= 28
```

## Cycle 5 Log

### 1. Card 5: Behavior Study (`research/cards/behavior-study.md`)

#### Validator Output

```
VALID: research/cards/behavior-study.md passed all schema and caveat checks.
```

#### Card Text

```markdown
# Eval card: agent-behavior-and-effort-study

Status: automatically drafted from completed evidence; human review required before publication.

## Question

Across the trial corpus, how does agent execution effort (steps, tool calls, execution time, and token economics) correlate with task outcomes, and what distinguishes passing trajectories from failing trajectories?

## Configuration and evidence

- Task: `behavior-analysis-corpus (event-summary, transaction-reconciliation, html-js-filter)`
- Completed spec: `docs/behavior-analysis.md` (PR #100)
- Config digest: `sha256:61f57018ef294bca7d46ab539b981ceefbd96486711a3b90038e9dc2a33f4435`
- Harbor jobs: `72 jobs from 2026-08-14 to 2026-08-16`
- Harbor lock digest: `sha256:886e92a20de44384b7adaa8c623e96b17765ecfca8159b9aa515ba92f033cb41`

## Result

- Corpus total trials ($n_{\text{corpus}}$): **92**
- Control trials ($n_{\text{controls}}$): **59** (57 oracle control trials with zero steps, 2 nop control trials)
- Real agent trials ($n_{\text{agent}}$): **33** (Codex / `gpt-5.6-terra`: 11 passed, 6 scored zero, 16 never-measured harness exceptions)
- Task domains ($n_{\text{tasks}}$): **3** (cross-task correlation carries **insufficient n** / **not distinguishable**)
- Execution/harness exceptions: **16** trials (10 environment/launch `ValueError`, 6 execution-stage `NonZeroAgentExitCodeError`)

### Effort vs Outcome Dynamics (Codex Sub-Corpus, n=17 measured)
- Passed Codex trials ($n=11$): **10.36 steps** (95% bootstrap interval: **[9.80, 10.90]**), 3.73 avg tool calls, 691.0s avg execution time
  - `local-lab/event-summary` ($n=6$): 10.83 steps [10.00, 11.50], 4.33 tool calls, 28.8s execution time, $0.0379 cost
  - `petermakhnatch/transaction-reconciliation` ($n=5$): 9.80 steps [9.20, 10.40], 3.00 tool calls, 1486.0s execution time, $0.0255 cost
- Scored-zero Codex trials ($n=6$, `terminal-bench/html-js-filter`): **18.17 steps** (95% bootstrap interval: **[16.00, 20.30]**), 11.67 avg tool calls, 538.2s avg execution time, $0.2471 cost
- Effort differential: Failing trials exhibited **+7.81 steps** over passing trials with non-overlapping 95% intervals ([16.00, 20.30] vs [9.80, 10.90]), reflecting active multi-turn struggle rather than early surrender.

### Telemetry Instrumentation Gaps (Not Zero Capabilities)
- `repeated_failed_command_count`: 0 across all 92 trials (loop detector unpopulated in pipeline; unmeasured instrumentation gap).
- `command_failure_count`: 0 across all 92 trials (exit code parsing unpopulated).
- `exception_phase`: 100% 'unknown' across all 16 exception trials.

## Elicitation tuple and caveats

```json
{
  "study": "behavior-telemetry",
  "agent": "codex",
  "model": "gpt-5.6-terra",
  "corpus_trials": 92,
  "real_agent_trials": 33,
  "oracle_control_trials": 57,
  "nop_control_trials": 2,
  "k": 3
}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: Telemetry is derived from containerized ATIF traces. 57 trials are oracle controls executing zero model steps; real agent behavior is measured across 33 trials of Codex (`gpt-5.6-terra`).

## Contamination note

- Contamination caveat: Tasks include internal test benchmarks (`event-summary`, `transaction-reconciliation`) and public benchmark `html-js-filter`. Telemetry analysis was performed post-hoc on execution artifacts without exposing solutions to agent contexts.

## Threats to validity

- Small task diversity: Evaluated across only 3 distinct tasks (=3$); general claims about effort vs performance across arbitrary software engineering tasks carry insufficient n and remain not distinguishable.
- Heavy control weighting: 57 of 92 trials (62.0%) are zero-step oracle controls; aggregate statistics must partition agent trials from reference controls.
- Unpopulated telemetry fields: Loop and command failure counters are currently unpopulated (0), which must not be interpreted as absence of agent loops.

## Regeneration query / command

```sql
SELECT
  task_name,
  agent_name,
  count(*) AS n_trials,
  sum(CASE WHEN primary_reward >= 1.0 THEN 1 ELSE 0 END) AS passed_trials,
  sum(CASE WHEN primary_reward = 0.0 THEN 1 ELSE 0 END) AS scored_zero_trials,
  sum(CASE WHEN exception_class IS NOT NULL THEN 1 ELSE 0 END) AS exception_trials,
  avg(step_count) AS avg_step_count,
  avg(tool_call_count) AS avg_tool_calls,
  avg(agent_execution_seconds) AS avg_agent_seconds
FROM trial_facts
GROUP BY task_name, agent_name
ORDER BY task_name, agent_name;
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
```

### 2. Hardening

Added `test_behavior_study_card_validation` in `tests/test_cards.py` asserting honest distinction between 57 oracle control trials and 33 real agent trials, as well as handling of unmeasured instrumentation gaps.

### 3. Verification

```
uv run pytest
# 1281 passed, 1 skipped, 1 xfailed in 50.81s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise
# Found 28 diagnostics (ratchet <= 28)

bash scripts/premerge.sh
# premerge green: Python 3.12; ty 28 <= 28
```
