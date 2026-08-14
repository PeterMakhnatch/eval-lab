# Evidence-to-experiment analysis loop

## Purpose

This document specifies how the lab turns completed Harbor trials into audited
findings and follow-up experiment proposals. The goal is useful automation with
an evidence trail, not an autonomous system that repeatedly spends tokens until
it finds a desired result.

## State machine

```text
completed trial
      |
      v
evidence validated ----invalid----> quarantined / task-or-harness investigation
      |
      v
facts extracted
      |
      v
cohort assembled
      |
      +--------> deterministic comparison
      |
      v
model-assisted analysis
      |
      v
human reviewed ----rejected-------> retained with rejection rationale
      |
      v
experiment proposed
      |
      v
policy checked ----approval needed----> waiting
      |
      v
new Harbor run
```

Each transition creates a new record. No stage edits the source job directory or
silently replaces an earlier finding.

## Stage 1: validate evidence

Before analyzing model behavior, establish that the experiment itself produced
usable evidence:

- job and trial results are complete;
- configuration and lock files are present;
- task, agent, model, environment, and Harbor versions are identifiable;
- verifier output and reward dimensions agree;
- declared artifacts exist and match their recorded digests;
- ATIF trajectories, when expected, validate against their declared schema;
- Oracle and no-op controls satisfy the task's stated expectations;
- infrastructure, authentication, and timeout failures are separated from agent
  capability failures.

A failed validity check is not a model failure. Quarantine the trial or classify
it as harness evidence and fix the experiment before drawing capability claims.

## Stage 2: deterministic extraction

Extract facts before asking a model to interpret them. At minimum:

- primary and component rewards;
- exception type and phase;
- wall time, token counts, and cost;
- ATIF step count and LLM-call count;
- tool-call counts by function;
- command exit codes and repeated failing commands when represented structurally;
- context-compression or continuation boundaries;
- final artifact inventory and digests;
- verifier check outcomes;
- task, config, prompt, rubric, and source-revision digests.

Derived facts should be reproducible by rerunning the extractor. They belong in
PostgreSQL for catalog queries and in Parquet for step-level analytical queries;
the original ATIF remains canonical.

## Stage 3: define a cohort

Never aggregate “all runs” without saying why they are comparable. A cohort
definition names:

- task and task digest;
- verifier digest;
- environment image/provider and relevant resource limits;
- agent adapter and version;
- model and model settings;
- prompt or instruction digest;
- attempts and selection rules;
- the one variable intentionally allowed to differ.

If more than one consequential variable differs, report the comparison as
exploratory rather than causal.

## Stage 4: deterministic comparison

Produce a machine-readable result before prose. Useful initial summaries are:

- pass count and pass rate with the denominator;
- per-reward distributions;
- exception and failure-category counts;
- duration, token, and cost distributions;
- tool-use and retry patterns;
- paired differences when the same task instance appears in both cohorts;
- links to representative successes and failures.

Small samples are shown as small samples. A single pass or failure is a trajectory
to inspect, not an estimate of general capability.

## Stage 5: model-assisted trial analysis

Use Harbor's existing analysis mechanism where possible. Give the analysis agent
a bounded, read-only bundle containing the task definition and source trial.
Require structured output rather than unconstrained prose.

The first rubric should answer:

1. Was this a valid agent attempt or an infrastructure/harness failure?
2. Did the verifier appear to accept an invalid shortcut or reject a valid result?
3. What is the earliest evidence-supported failure point?
4. Which capability category best describes that failure?
5. What direct evidence supports the classification?
6. What alternative explanations remain plausible?
7. What smallest follow-up experiment would distinguish them?

### Initial failure taxonomy

Use a small taxonomy and allow `unknown`:

| Category | Meaning |
|---|---|
| `task_invalid` | instructions, solvability, or hidden assumptions are defective |
| `environment_failure` | environment did not provide the intended world or tools |
| `harness_failure` | adapter, orchestration, auth, transfer, or logging failed |
| `verifier_false_positive` | invalid work received success or excessive reward |
| `verifier_false_negative` | valid work was rejected or under-rewarded |
| `planning` | agent selected or maintained an unsuitable plan |
| `evidence_use` | agent ignored, misread, or failed to reconcile available evidence |
| `tool_use` | tool choice, arguments, ordering, or recovery caused the failure |
| `implementation` | intended approach was reasonable but execution was incorrect |
| `verification_behavior` | agent did not adequately test or inspect its own result |
| `context_management` | compression, loss, or misuse of prior state was causal |
| `policy_or_refusal` | refusal or policy behavior prevented completion |
| `unknown` | evidence cannot support a narrower classification |

Categories describe the observed failure mechanism, not a permanent property of
the model.

### Structured analysis artifact

An analysis sidecar should contain fields equivalent to:

```json
{
  "schema_version": 1,
  "analysis_id": "uuid",
  "source_trial_id": "uuid",
  "source_digests": {
    "result": "sha256:...",
    "trajectory": "sha256:...",
    "task": "sha256:..."
  },
  "analysis_provenance": {
    "agent": "...",
    "agent_version": "...",
    "model": "...",
    "prompt_digest": "sha256:...",
    "rubric_digest": "sha256:...",
    "created_at": "...",
    "cost_usd": 0.0
  },
  "validity": "valid_agent_attempt",
  "primary_category": "tool_use",
  "summary": "...",
  "evidence": [
    {
      "path": "agent/trajectory.json",
      "step_id": 17,
      "tool_call_id": "call_...",
      "supports": "..."
    }
  ],
  "alternative_explanations": ["..."],
  "proposed_discriminator": "...",
  "confidence": "low"
}
```

`confidence` is a calibration label, not a calculated probability unless the
project later defines and validates one.

## Stage 6: cross-trial synthesis

Aggregate structured findings only after retaining access to the underlying
trials. The synthesis agent may identify recurring patterns, but it must report:

- cohort definition and number of trials;
- number of analyses that failed or were unavailable;
- category counts and representative trial links;
- counterexamples;
- whether the pattern appears across tasks, models, or attempts;
- which claims are observations versus interpretations;
- a proposed experiment that changes one variable.

Text similarity or clustering may help find candidates. It does not establish
that two failures have the same cause.

## Stage 7: review and proposal

A human review records one of:

- `accepted`: evidence supports using the finding for the next experiment;
- `needs_revision`: analysis schema is valid but the claim needs correction;
- `rejected`: evidence does not support the claim;
- `superseded`: a later analysis with a new rubric or better evidence replaces
  it for decision-making without deleting the original.

An experiment proposal must include:

- source finding IDs;
- explicit hypothesis;
- one primary variable to change;
- fixed variables and cohort selection;
- task and verifier validity controls;
- expected observations under competing explanations;
- attempts, concurrency, timeout, and cost ceiling;
- stop conditions;
- whether execution requires human approval.

The proposal is written first. The run is a separate action.

## Automation policy

The system may automatically:

- validate completed local evidence;
- extract deterministic facts;
- regenerate a derived index;
- compare already-completed cohorts;
- run free local Oracle and no-op controls when paths and limits are explicit;
- draft analyses and experiment proposals after the relevant model call has been
  approved.

The system may not automatically:

- invoke a paid model or model judge without acknowledgement;
- start a cloud sandbox or large sweep;
- alter a task or verifier and reuse old results as if they were comparable;
- promote evidence, publish a task, deploy infrastructure, or expose data;
- execute a generated proposal merely because another agent recommended it.

## First useful questions

The initial implementation should make these questions answerable:

1. Which trials failed because of infrastructure rather than agent behavior?
2. What was the earliest failed tool interaction in each valid attempt?
3. Which failure categories recur across attempts on the same task version?
4. Do passing trajectories use different tools or verification steps than
   failing trajectories?
5. Which analyses cite insufficient or missing evidence?
6. What single-variable follow-up experiment would discriminate the leading two
   explanations?

These are more valuable initially than a general-purpose “AI scientist” agent.
