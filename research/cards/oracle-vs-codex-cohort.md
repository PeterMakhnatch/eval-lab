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
