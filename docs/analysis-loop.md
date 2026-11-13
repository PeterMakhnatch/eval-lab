---
status: living
audience:
  - analyst
---

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

Trial-level step, tool, failed-command, and declared LLM-call counts exclude
ATIF steps marked `is_copied_context`: copied history is not another execution.
The raw per-document projection retains those rows and their provenance for
inspection. Rebuild older derived facts from the retained jobs when comparing
counts across this convention change.

Native usage totals prefer the trial result, or the terminal continuation's
declared aggregate when the result does not supply one. Outlines use native
aggregate fields where available, not an incomplete sum of attributed steps.
Do not add parent and summarization-child totals: the parent can already include
that usage. Continuation views retain earlier non-copied steps with their
original document, hash, and step coordinates. Missing, escaping, or cyclic
continuations are unavailable/degraded evidence, not a complete partial head.

The raw ATIF projection follows file references from every embedded subagent
document as well as the outer document. Relative references use the enclosing
source file's directory; targets must remain inside the same trial directory.
Reachable files outside `agent/` are included, and repeated or cyclic links do
not duplicate documents or steps. Missing and escaping targets remain invalid
evidence rather than fabricated documents. This traversal does not authorize
summing parent and child usage totals.

Native usage estimates, physical-call proxy accounting, and provider invoices
are different records. Some producers omit `llm_call_count`, and some upstream
length-interrupted calls are absent from native token totals. Neither zero
declared calls nor missing usage proves zero actual consumption. The proxy's
conservative uncached accounting is not an invoice.

### Archived CLI reference: deterministic trajectory analysis

This analyzer is archived in `archive/pre-round3-2026-11-12`.
Restore it before using the historical command below:

```bash
git checkout archive/pre-round3-2026-11-12 -- research/analysis/harness-mechanics
```
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

Preserve a finite verifier grade after a recognized agent stop
(`AgentTimeoutError`, `TrialBudgetExhaustedError`, or `LoopBreakStop`): the agent
stopped, but the verifier still measured its output. This applies to zero,
fractional and passing grades under both values of
`budget_exhaustion_is_failure`; the raw exception remains in member provenance.
That option imputes a failure only for an **ungraded** timeout/budget stop.
Unrelated infrastructure exceptions remain excluded even when a grade exists.
Cohort denominators, task pairing, cost-per-solved and curve exclusions use the
same effective-reward rule. Missing or non-finite grades are not valid outcomes.

Paired binary `pass_any_first_k` rankings additionally require an **exact
two-sided paired sign/McNemar p-value below .05**, computed from task wins and
losses with ties excluded. JSON exposes `paired_exact_p_value` and
`paired_exact_method`; Markdown includes the same statistic. The task-bootstrap
interval remains descriptive and must still exclude zero, but it is not enough
on its own: two wins can produce `[1, 1]` while exact `p = .5`, so they must not
be ranked. Five unanimous discordant tasks give `p = .0625`; six give
`p = .03125`. Additional tied tasks do not manufacture decisive evidence.

This is a per-contrast decision rule for independent paired task units, not
proof that an observational cohort is causal and not a multiple-comparison or
repeated-search correction. All identity, pairing, ordering and comparability
refusals still apply. Existing preregistered one-sided publication gates are
unchanged.

For a pinned Terminus harness experiment, declare
`harness_tree_sha256` as the treatment. The comparator verifies the retained
tree and the native frozen kwargs/rules/skills rather than trusting a supplied
digest label. It permits only the changes induced by that tree; unrelated
model/tool settings and within-task execution-control changes still invalidate
the comparison. Different task blocks may retain their own fixed settings.

Every arm also reports **cost per solved task**: recorded cost across all
selected trials (including failed or unscored attempts), divided by distinct
task instances with at least one valid pass. Repeated successes do not increase
the denominator. Missing, negative, or non-finite cost is counted and makes the
ratio unavailable; zero solved tasks also makes it unavailable. An explicit
local no-API-charge zero is valid cost evidence. Provider estimates remain
estimates, not invoices. This accounting does not turn a small or invalid
comparison into evidence of an improvement.


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

The Insights engine is retired. Retain its existing state and historical reports.
Use source-cited native reports and cached scoring for current measurements.
Proposals remain separate from execution approval.

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

## Trial features

`evallab.interpretation.features` (`trial_features`, `write_features`) extracts deterministic
per-trial tables (`<stem>.parquet` and `<stem>.csv`) during `evallab process-job` and `evallab trials`,
combining existing deterministic detectors without new detection logic (see trust notes).
## Column dictionary

| column | source | meaning |
| --- | --- | --- |
| `trial`, `job` | dir names | trial / job directory names |
| `task_id` | `result.json:task_name` | last `/`-segment (QUIRKS Q23; never the `task_id` dict) |
| `arm` | job/trial name | first `-`-segment in `KNOWN_ARMS` (`gepa/stock/tuned/direct/wrapper/…`); best effort, may be null |
| `n_steps`, `n_agent_calls` | trajectory | all steps / agent-source steps |
| `wall_time_s` | `result.json` | `agent_execution` finish−start, else trial finish−start |
| `input_tokens`, `output_tokens` | step metrics | sums over agent steps (`live_watch`) |
| `mean_input_per_call`, `mean_output_per_call` | derived | totals / `n_agent_calls` (null when 0) |
| `tools_used` | tool calls | sorted unique verbs: bash verbs via `interpretation/features.py` quote-aware `_segments`/`_program` (lowercased); non-bash tools as `@name` |
| `tool_counts_json` | derived | verb → count JSON |
| `first_edit_step`, `first_edit_paths` | `live_watch.first_repo_edit` | first repo edit via the shared `token_flow` detectors (quoted reads excluded) |
| `n_test_runs`, `test_runs_passed/failed`, `last_test_step` | observations | agent-side pytest only: a step counts when its command invokes `pytest` or its observation has a pytest `===` summary bar; counts from `N passed/failed` tokens (nulls when no runs) |
| `n_errors`, `n_exceptions` | observations | steps matching tool-error markers / containing a `Traceback` marker (may overlap) |
| `loop_onset_step` | `token_flow.analyze_token_flow` | earliest repetition-without-progress onset (null when none) |
| `max_identical_run` | `live_watch` | longest trailing-identical-command run |
| `n_fetch_attempts`, `first_fetch_step` | `upstream_fetch` via `live_watch` | every classified attempt counts, failed or not; first step id |
| `n_fetch_confirmed` | same | attempts with confirmed acquisition evidence |
| `copy_verdict` | `counts.classify_counts` | `counted_pass/counted_fail/excluded`; includes copy findings and declared red-team controls |
| `stop_reason` | `step_layers.classify_stop_reason` | `trial_budget_exhausted/agent_timeout/error/task_complete/…`; final-claim flag comes from the last agent step (prose-completion mapping needs step layers, so those stay `unknown`) |
| `limit_hit` | `probe03.ceiling_which` | binding ceiling for budget stops (`input_tokens/…`), else `agent_timeout`/`loop_break`/null |
| `reward` | `verifier_result.rewards.reward` | null when unscored |
| `final_diff_lines`, `files_touched`, `diff_source` | `verifier/agent.diff`, else edit steps | `+/−` line count + `+++` files when the harness diff exists (`verifier/agent.diff`); otherwise the union of edit-step touched paths with null line count (`edit_steps`) |

## Trust notes

- Read-only: never touches `runner.py`, the proxy, or trial files.
- One bad trial degrades to a null row, never kills the table.
- `copy_verdict` includes declared red-team controls but does not incorporate
  fetch taint or task usability. Fetch evidence is in `n_fetch_confirmed`.
- Test/error columns cover **agent-side** runs only; the harness verifier
  (`verifier/test-stdout.txt`, `ctrf.json`) is out of scope.
- `arm` is a naming-convention parse, not ground truth.
- Validation: `tests/test_features.py` (quoted-awk read is not an edit;
  failed fetch is an attempt; ceiling mapping) plus a 3-trial hand
  spot-check recorded in the HAR-159 receipt.

## Behavioral queries

`sql/behavior.sql` owns the summary, effort, efficiency, struggle, step-shape, and token-economics views.
Every metric reports its population and populated counts.
Do not replace those views with broad direct Parquet globs.

Outcomes separate reward at least 1, exact zero, partial reward, and never measured.
Absent, non-finite, and sentinel -1 rewards remain unmeasured.
Agent exits do not erase a measured verifier outcome.
Undefined ratios remain null, including reward efficiency at zero reward and seconds per step without steps.
An all-zero instrument column does not prove absence of the behavior.
Sparse usage requires its own denominator rather than silently averaging only populated rows.
The historical 17-of-92 token census refers to August 15–16, 2026, not the current corpus.

Definitions and calibration remain in `research/behavior/catalog-v1.yaml`.
Human ground-truth absence never becomes a negative label.
The Phoenix publisher is retired. ATIF and the evidence store remain canonical.

### Descriptive trajectory metrics

For N tool calls and D distinct `(function_name, arguments_sha256)` signatures,
Loop Index is `(N - D) / N` for N > 0, otherwise null.
A repeated safe read can be reasonable. The metric ranks review candidates, not failures.
Tool Efficiency Ratio divides linked observations with null/zero command exit by tool calls.
Report unlinked calls separately. No calls means null.
Context Bloat Velocity is `regr_slope(prompt_tokens, llm_step_ordinal)` on measured points.
Fewer than 2 points means null.
Its unit is prompt tokens per LLM-bearing step, not context-window occupancy.
Confirm adapter metric semantics before comparisons.

Flaky-verifier, missing-tool-observation, timeout, and early-stop rules generate review candidates.
They do not overwrite reviewed labels or rewards.
Match task, verifier, environment, scaffold, prompt, and model before interpreting differences.
Review the earliest implicated ATIF action and publish missingness and reviewed counts.

## Failure atlas and source authorities

The failure atlas is an inspection report, not an exhaustive causal taxonomy.
It retains denominators, raw step links, source hashes, and intervention hypotheses.
HAR-119 blind raters, HAR-109 hand labels, and HAR-128 SFT cleanliness are different authorities.
None silently becomes another task's loop-kind ground truth.

The frozen G5 table retains stock, tuned, and historical GEPA arms.
Missing/unbound cells remain null. G5 identities stay outside the historical pattern denominator.
The cohort binds all 60 spec files in their frozen order and verifies native identity and setup.
A results-home card prefix or shared model is not an experiment identity.
The [partial consumer proof](../research/experiments/har117-results-home/har131-g5-atlas-consumer-proof.json)
remains a 249-trial observation: 152 eligible non-G5 trials and 13 bound G5 trials.
It is not the final paired G5 result.
The full atlas can contain held-out evidence. Do not send it to a proposer.
Reflection needs a separately authorized training payload with task/package and exclusion bindings.

## Research scope

Trust the instrument before ranking a model.
Ask first about verifier discrimination, alternative valid solutions, calibration, and contamination.
Then describe behavior, compare matched cohorts, and test 1-variable interventions.
Trial evidence includes trajectory, environment/verifier outputs, judgment provenance, and lab telemetry.
Subscription CLI agents do not expose model internals for these analyses.
Old generator blueprints and historical ecosystem reviews do not create supported runtime routes.

