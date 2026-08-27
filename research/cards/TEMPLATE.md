# Eval card: {{TITLE}}

Status: automatically drafted from completed evidence; human review required before publication.

## Question

{{HYPOTHESIS}}

## Configuration and evidence

- Task: `{{TASK}}`
- Completed spec: `{{SPEC_PATH}}`
- Config digest: `{{SPEC_DIGEST}}`
- Harbor job: `{{JOB_PATH}}` (`{{JOB_ID}}`)
- Harbor lock digest: `{{JOB_LOCK_DIGEST}}`

## Result

- Task evidence units: **{{N_TASKS}}**
- Recorded trials: **{{N_TRIALS}}**
- Attempts per task (`k`): **{{K}}**
- {{PRIMARY_METRIC_LABEL}}: **{{PRIMARY_METRIC_VALUE}}**
- Task-bootstrap 95% interval: **{{INTERVAL}}**
- Execution/harness exceptions: **{{EXCEPTIONS}}**

Attempts from the same task are one evidence unit. This card does not treat repeated attempts as
independent samples.

## Elicitation tuple and caveats

```json
{{ELICITATION}}
```

The tuple must name the agent version, model pin, preamble hash, configured toolset, and `k`. An
unavailable tuple makes cross-cohort ranking non-reportable.

- Elicitation caveat: Pinned model and toolset parameters; unmeasured configurations are non-reportable.

## Contamination note

- Contamination caveat: Benchmark exposure and training data contamination status.

{{CONTAMINATION}}

## Threats to validity

{{THREATS}}

## Regeneration query / command

```sql
SELECT task_name, avg(primary_reward) FROM trial_facts GROUP BY task_name;
```

## Human review

- [ ] Confirm task and verifier identity.
- [ ] Confirm the elicitation tuple describes the actual run.
- [ ] Resolve the contamination note with evidence.
- [ ] Decide whether the interval supports the intended claim.
- [ ] Record reviewer, date, and publication disposition.
