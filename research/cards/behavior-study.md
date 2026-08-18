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
