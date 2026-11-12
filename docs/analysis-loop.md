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

### Per-run report

renders one deterministic report per trial (schema `evallab.run_report/v1`) and,
for a job directory, a rollup (`evallab.job_report/v1`). The Markdown is rendered
from the JSON, so people and models read the same facts. It never writes into
the run directory; `--output-dir` writes `<trial>.run_report.{json,md}` and
`job.run_report.{json,md}`. Sections: outcome, Harbor phase timing and gaps
between agent steps, tokens and cost, tools, revisits, subagents, context
events, errors, an optional benchmark domain section (`## Domain: <name>`,
absent as `domain: null` when no plugin detects the trial), a bounded
timeline (`--full-timeline` lists every step), and data-quality notes.

The native report owns domain readers, baseline facts, evidence coverage, and custom feature eligibility.
`interpretation/features.py` owns the shared quote-aware command parser. Semantic IR and settlement compliance remain separate.

`evallab.run_report/v1` is additive: a `domain` section appears when a
`src/evallab/interpretation/run_report.py` reader detects the trial, else `domain`
is `None`. The CEO-Bench plugin (`ceo_bench`) reads the `ceo_bench/*.json`
sidecars written by the `ceo_bench` adapter package
(`library/adapters/ceo_bench/ceo_bench/bridge.py`), which converts one
CEO-Bench harness run (`bash_agent_runs/run_<id>/`) into a Harbor-shaped trial
dir (`result.json` with honest nulls, one trajectory step per tool call, plus
the sidecars). Operator path (the adapter is an independent uv project so
`sqlcipher3` never becomes an evallab core dependency):
`uv run --project library/adapters/ceo_bench python -m ceo_bench <run_dir>`
(prints the trial dir; `--out` overrides the `<run_dir>.trial` default),
needs the published SQLCipher key (`NMDB_KEY`, else `--ceobench-src`
`<checkout>`); without either it degrades to timing fallbacks with a reason.
The section reports cash-by-sim-day, bankruptcy (cash below $0),
forecast error of the agent's cash predictions, no-op weeks, and
simulator-LLM spend metered separately from agent spend (never merged
into agent cost).

Definitions the report applies:

- Step numbers are 1-based positions in the stitched trajectory (continuations
  included, copied context excluded), not native ATIF `step_id`s.
- Tokens and cost follow the precedence above, field by field, then step sums;
  each total names its source. Cost from partial step sums is flagged as a lower
  bound, and missing cost is `null` with a data-quality note, never zero.
- When the harness recorded no cost anywhere, the report may still show a cost
  figure labelled `(estimate)` with source `price_table_estimate`: token counts
  priced at the pinned Standard-tier list rates in
  `src/evallab/interpretation/price_table.py` (per-model source URL and
  retrieval date recorded in `cost.price_table`). Matching is exact after
  stripping a known provider prefix; unknown models and missing token counts
  stay `null` with a reason. Estimates never enter the job rollup's harness
  totals (`total_cost_usd`, `cost_per_pass_usd`); they roll up separately as
  `estimated_cost_usd` / `trials_with_estimated_cost` and are marked `(est)`.
- Tool status is `ok`, `error`, or `unknown`. `evidence` names the channel:
  exit code, mini-swe-agent envelope, Codex code-mode script status, harness
  error flag, structured payload status, leading harness rejection text,
  strong output-text patterns (`output_text`, inferred), or — for Reef native
  tools (`execute`/`run_bash`/`write_file`/`read_file`) only — a leading
  `timed out after 60s` (`timeout`) or `refused: …` line (same `output_text`
  channel; the exact strings those tools return with no exit code or flag).
  Failure words deeper in such an output are data the agent read, not a
  rejection, so successful runs that merely mention errors stay `unknown`.
  Codex code-mode `exec` calls are reported as the inner tool
  (`exec_command`, `apply_patch`). OpenCode non-MCP tools (bash/read/write)
  carry no per-call channel, so their failures surface only through the
  strong patterns (for example shell `command not found`) and their successes
  stay `unknown`; MCP-backed calls are still judged by structured payload
  status (`isError`/`status`/`ok`).
- An action's signature is its tool plus normalized command or arguments.
  A repeated action reuses an earlier signature; it is a *return* when other
  actions came between and an *immediate repeat* otherwise. An *exact revisit*
  also got an identical normalized result (volatile wall-time lines removed):
  the agent was back at the same spot and nothing had changed. Empty-input
  polls are excluded.
- Subagents come from ATIF subagent references (embedded or file), Claude Code
  sidechain steps, and delegation tool calls. For codex trials the report also
  re-reads the retained native rollouts under `agent/sessions/**/rollout-*.jsonl`:
  Harbor's codex converter keeps only the newest rollout and never links child
  threads, so sibling rollouts surface as `evidence: "codex_native_rollout"` items
  (per-child tokens from cumulative `token_count` snapshots, cost left `null`
  because rollouts carry none). A sibling counts as a child only when its
  `session_meta` source is a `thread_spawn` record naming this trial's parent
  thread (`parent_thread_id` edge); the `spawn_agent`-call link is kept only
  for V1 `{"agent_id": ...}` outputs, so V2 children keep no spawn step rather
  than a guess. Any other extra rollout (retries, review/compact threads) is
  listed under `other_threads`, never as delegated spend. Child tokens are
  never merged into run totals. Rollouts present with no children stay
  `none_observed`; absent or unreadable rollouts are `unavailable` with a
  reason. `none_observed` is not proof of absence for harnesses whose converters
  drop child threads. No retained codex trial has yet spawned a subagent (27
  retained trials hold single `exec`-source rollouts), so multi-child behavior is
  pinned by format-faithful fixtures verified against the codex source, not
  production evidence.
  Sidechain steps group by per-step agent id when present; anonymous steps
  rejoin to the retained native session (`<trial>/agent/sessions/…`) through
  two exact keys the converter preserves: the raw `tool_use` block id (ATIF
  `tool_call_id`) first, then the step timestamp (attributed only when it
  names exactly one agent; normalized to ms, UTC). Steps with no joined key
  (text-only turns on a timestamp two agents share, or with no session
  retained) fall back to labeled contiguous runs, which may conflate
  interleaved parallel subagents. Proven on a real local session converted
  with the installed Harbor 0.21 converter (2 subagents, 46 sidechain steps
  → 46 keyed by real `agentId`, `count` 2, 0 fallback runs) and pinned by
  `tests/fixtures/claude-sidechain/` (real converter output over a synthetic
  raw session with the real key layout: `agent-a` × 3 incl. one timestamp
  join, `agent-b` × 1, plus one 2-step fallback run for a shared timestamp).
  No real Claude Code trial exists in retained evidence; the producing spec
  is `research/experiments/specs/04-claude-code-canary/event-summary.json`
  (billable — needs approval before running).
- The trajectory is parsed once: windows, revisits, loop suspicion, and the
  context-growth curve all derive from that single pass, so build cost stays
  roughly flat per step on long runs (measured ~2k–10k steps). Loop suspicion
  is the outline's own heuristic (`traj._analyze_loop_suspicion`), called on
  per-step facts from the shared `traj.extract_loop_step` source instead of a
  second outline parse; it is `null` when the continuation chain is
  incomplete or steps are malformed.
- The timeline has two windowings of runs with at least 20 steps: tenths of
  the run by step ordinal, and equal-duration wall-clock windows over the
  span between the first and last timestamped step (steps without timestamps
  are counted, not placed). Each window row carries the context growth
  curve: `peak_prompt_tokens` and `compactions` — inferred compactions reuse
  the context-drop heuristic (input tokens falling below 60% of the previous
  agent step once past 8,000). Time windows are an explicit
  `{"status": "unavailable", "reason": ...}` when timestamps are missing or
  degenerate, never a fabricated bucketing.
- `revisits.revisits_started` names when looping began: the first
  step-window whose repeat rate (repeated non-poll actions over counted
  non-poll actions in that window, rounded to four decimals) strictly
  exceeds the median of the window rates (windows without actions have no
  rate and cannot be the onset). `no_repeats` when nothing repeats, `flat`
  when no window exceeds the median, omitted entirely below 20 steps.
- Domain sections use the native readers in `src/evallab/interpretation/run_report.py`.
  The explicit `PLUGINS` registry reads verifier outputs in a deterministic order
  from the trial directory. `synthetic_hospital` reports reward/steps/submitted
  from `verifier/reward.json` plus redundant chart-section re-reads from the
  trajectory's tool calls (native EHR tools and `sh-agent call` wrappers, keyed
  by the finest chart address named). `atlas_finance` reports the gandalf
  rubric from `verifier/grader/info.json`: per-section pass/fail with section
  gates (criterion-level `failed_gate_indices` never fail a section), penalties,
  and downstream required-criteria loss attributed to the first failed gate.
  Malformed verifier output yields `status: unreadable` with a reason, never a
  fabricated zero or an exception. Counts over an unavailable input are `null`
  with a reason (`Chart reads: unavailable (...)`), never zero.

### Archived CLI reference: deterministic trajectory analysis

This analyzer is archived in `archive/pre-round3-2026-11-12`.
Restore it before using the historical command below:

```bash
git checkout archive/pre-round3-2026-11-12 -- research/analysis/harness-mechanics
```

```bash
uv run python research/analysis/harness-mechanics/analyze.py \
  --trajectory <raw ATIF path> \
  --repo-root . \
  --output <new derived directory> \
  --evidence-kind historical
```

Parameters and options:

- `--trajectory <path>`: Repeatable argument pointing to one or more raw native ATIF trajectory JSON files.
- `--manifest <path>`: Mutually exclusive alternative to `--trajectory`. The manifest JSON object must contain `"evidence_kind"` (`"historical"`, `"fixture"`, or `"model-run"`) and `"trajectories": ["<path>", ...]`.
- `--repo-root <path>`: Path to repository root used for relative trajectory path resolution and safety checks (relative paths must resolve inside the repo root).
- `--output <path>`: Path to a non-existent target directory. The tool refuses to overwrite existing directories or symlinks, publishing all outputs atomically.
- `--evidence-kind`: User-declared provenance (`"historical"`, `"fixture"`, or `"model-run"`). Defaults to `"historical"` when omitting `--manifest`.

Inputs and supported formats:

- Input trajectories must be raw native ATIF JSON structures. Normalized flat IR files (such as converted step tables or intermediate schemas) are not valid raw native ATIF input.
- Controls (e.g. `oracle` or `nop` runs) and non-trajectory records are classified as `unsupported`.

Produced artifacts:

- `report.json`: Machine-readable diagnostics containing source hashes, raw-source `locator` JSON pointers, recorded `step_id` and `tool_call_id`, identity metadata, and summaries across stopping, clipping, and shell behavior.
- `report.txt`: Human-readable summary table of evaluated records, observed facts, and diagnostic counts.
- `ablation-plan.json`: Single-variable proposals linked to substantive evidence, or an empty proposal list when evidence is insufficient. `execution_authorized` remains false; this artifact is neither an approval nor an executable queue specification.

Execution and interpretation semantics:

- Exit codes: Returns `0` when all provided trajectories are supported and valid; returns `2` on invalid CLI usage, bad shapes, errors, or when any trajectory record is classified as `unsupported`.
- Atomic writing: Writes outputs to a temporary sibling directory and moves it into place. Refuses to overwrite an existing directory or symlink.
- Observational discipline: Outputs record observed facts, hypotheses, and unknowns. They distinguish factual runtime observations from diagnostic hypotheses and record `unknown` when evidence is incomplete.
- No model/tool execution: Analysis is entirely deterministic and static over the provided files.
- Descriptive, not causal: Diagnostic facts characterize mechanics in the observed sample. They do not infer causal capability claims, benchmark rankings, or harness superiority.

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

The separate Reef publication gate (`library/adapters/reef_gate`) is a
per-candidate selection rule, not a replacement for task-paired capability
analysis. Its exact sign test drops ties and invalid pairs, applies a minimum
valid-pair requirement, and vetoes regressions on tasks the current harness
passed on every repeat. A/A calibration reports its measured false-publish
rate with Wilson uncertainty against the gate-table prediction; the prediction
assumes independent binary episode outcomes and is not itself a measurement.
A known-effect control reports power separately. Repeated candidate selection
does not acquire a family-wise error guarantee from the per-candidate alpha.
Missing scores, failed evaluations and missing decision costs/timings remain
explicit unknowns or refusals, never silent zero-valued successes.



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

directories (same discovery rule as `evallab watch`: a path is either a job
dir whose children are `<task>__<id>` trials, or a runs root whose children
are job dirs) and writes `<stem>.parquet` plus a `<stem>.csv` sibling for
humans. The module (`evallab.interpretation.features`) implements no new
detection logic; every signal comes from an existing deterministic detector
(see trust notes).

```bash
```

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
| `tools_used` | tool calls | sorted unique verbs: bash verbs via `outside_fetch` quote-aware `_segments`/`_program` (lowercased); non-bash tools as `@name` |
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

## Durable analyst records

An explicit `--model` selects a model-backed call and requires current approval.
No model selector or credentials means no provider execution.
`analyst list` and `analyst show <analysis-id> --json` inspect stored results.

The conclusion is an `AnalysisRecord` with trial/rubric/model identity,
category, cited evidence, and confidence with sample and provenance.
Empty evidence fails before publication.
Records under `research/analysis/<analysis-id>.json` retain explicit input digests.
Their companion `.trajectory.json` records the analyst's reasoning steps.
Repeated analyses create new IDs. Disagreement never overwrites earlier conclusions.
`sql/analyst.sql` exposes conclusions, trajectories, and their joined view.
Use [the attach and lineage contract](data-architecture.md) for queries and source verification.

### Frozen-window trajectory judging

`run_trajectory_judge(analyzer, windows, rubric=..., repeats=3)` runs triage,
inspection, then final synthesis over one frozen, post-redaction window pool.
This API uses transient `EvidenceCitation.path` values `W1`, `W2`, ... in full
input order, not file paths or copied SHA-256 strings. Handles remain stable
across stages and repeats; inspection can cite only its selected visible subset,
while final synthesis can revisit the full pool for counterevidence.

The host resolves an exact visible handle to its authoritative window digest,
trial ID and step range. Returned stage/run records retain canonical citations,
hash rules and deterministic-context bindings; the handles are not substituted
for persisted source identity. Unknown handles, full-digest paths, brackets and
whitespace variants are rejected rather than guessed. Existing bounded citation
repair remains in effect.

Final synthesis requires reported supporting and contradicting citations plus an
alternative explanation. That structural requirement does not verify semantic
support or turn disagreement into a decision: returned records remain
`decision_eligible=False`.

## Guarded post-trial analysis worker

`analysis_worker.py` stages 1 request per trial using frozen `sha256(job_id:trial_id)[:16]` identity.
It freezes result, trajectory, task, verifier, rubric, prompt, and profile digests.
Changed evidence quarantines the request. Changed profile identity defers it.
Transitions and invocation attempts remain append-only and durable.

Admission checks STOP, evidence integrity, profile identity, policy, qualification,
cost ceilings, service health, and an actual wired adapter before invocation.
The calibration check fails closed until its required floor is met.
Default composition with no adapter defers `adapter_not_wired` without arming an invocation.
An exception without usable reward remains a harness deferral.
A measured reward remains eligible while its agent exception stays separate.

An existing durable sidecar is adopted without another call.
An invocation marker without a sidecar becomes
`ambiguous_invocation_requires_operator_resolution`.
No automatic retry can repeat a possibly paid call.
Per-request kernel locks serialize execution and preserve another owner's lease path.

```sh
```

Resolution records an attributed disposition. `--action retry` acknowledges possible prior spend.
It still grants no new spend and bypasses no admission check.
Nightly staging can discover requests but cannot invoke a model.
Sidecars retain citation validation errors, source/model/schema digests, and measured usage.
Source trial bytes remain unchanged.

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

`behavior_episodes` stores multi-label step-bounded interpretations without changing ATIF.
The v1 dimensions remain `tool_error`, `unchanged_retry`, `recovered_progress`, and `verification_gap`.
`unresolved_error` is a right-censored recovery state, not another calibrated dimension.
`effect_loop_candidate` remains experimental and requires complete no-state-change evidence.
Missing observation, relevance, or state coverage remains unknown.
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

## Receipt-bound program reports

Start with `--index-only --output-dir <new-directory>`.
`facts.json` preserves every number-bearing physical line with content-bound IDs and source location.
`checks.json` records missing receipts, unmatched numbers, arithmetic, and receipt hashes.
The checker cannot establish semantic support. Its status is `receipt_bound_unchecked`.
Receipts above 256 KiB remain unchecked; provide a bounded extract.
Arithmetic disagreement blocks a draft even when a receipt repeats the same wrong percentage.

The default draft uses every fact. Missing support retains a rejection record, not a successful report.
`--select ID` creates an editorial selection while preserving the complete index.
`--citations bindings.json` binds an exact quoted line to explicit receipt paths.
Selection does not make a refuted number true.
Use a new output directory; existing output is never overwritten.
The optional `--bridge` publishes to `research-context`, commits, and pushes.
It requires current publication authority and a new destination.
Google Docs publication is not part of this command.

## Research scope

Trust the instrument before ranking a model.
Ask first about verifier discrimination, alternative valid solutions, calibration, and contamination.
Then describe behavior, compare matched cohorts, and test 1-variable interventions.
Trial evidence includes trajectory, environment/verifier outputs, judgment provenance, and lab telemetry.
Subscription CLI agents do not expose model internals for these analyses.
Old generator blueprints and historical ecosystem reviews do not create supported runtime routes.

## Explorer data contracts

`explorer.py` retains read-only evidence indexing and citation checks.
The former Streamlit explorer frontend is retired; use `evallab view` and native reports.
Index construction does not modify evidence.
Fields distinguish `observed`, `derived`, `draft`, `withheld`, and `unavailable`.
A withheld citation can resolve while its content remains unreadable.
Retain its byte count and source digest instead of showing it as present text.

Citation checks require a trial-relative existing artifact, valid step, and exact tool-call membership.
Duplicate source IDs or ambiguous publication leave the analysis unlinked with a reason.
Path traversal, absolute artifact paths, and task `tests/` or `solution/` access fail.
Key-shaped mapping fields redact before rendering.
Missing/malformed roots and sidecars remain named unavailable states.
A scored timeout retains its verifier outcome and separate agent exit.
The explorer does not rank incomparable cohorts or execute the next action it displays.

## Historical literature and source corrections

The following dated research notes retain their source claims and corrections.
They do not set attempts, thresholds, or approval policy.
Use the current experiment spec and frozen protocol for each measurement.

## 0. Citation correction (read first)

The mission brief cited "The Scaffold Effect in Coding Agents (arXiv:2502.12151)".
**That ID resolves to "VoLUT: Efficient Volumetric streaming enhanced by
LUT-based super-resolution" — a video-compression paper.** No paper titled "The
Scaffold Effect in Coding Agents" exists on arXiv under that ID; the citation
appears to be a confabulated reference. The *phenomenon* it names is real and
well studied under other titles. This survey covers the actual literature:

| Paper | ID | What it contributes |
|---|---|---|
| On Randomness in Agentic Evals | arXiv:2602.07150 | Quantifies run-to-run variance; the strongest "scaffold effect" numbers |
| Building Effective AI Coding Agents for the Terminal (OpenDev) | arXiv:2603.05344 | Engineering catalog of harness variables |
| Inside the Scaffold: A Source-Code Taxonomy of Coding Agent Architectures | arXiv:2604.03515 | Taxonomy of scaffold architectures from source |
| Can Generalist Agents Automate Data Curation? | arXiv:2606.04261 | Light-vs-heavy scaffold effect on outcome variance |
| From QA to Task Completion: Survey on Agent System and Harness Design | arXiv:2606.20683 | Minimal-scaffold-with-good-primitives finding |
| Beyond the Leaderboard: Synthesis of Tool-Use, Planning, Reasoning Failures | arXiv:2607.05775 | Six-cluster failure taxonomy |
| Agentic Harness Engineering | arXiv:2604.25850 | Automated harness evolution from observability data |

## 1. The scaffold effect, quantified

**Headline numbers (arXiv:2602.07150; 60,000 trajectories, SWE-bench-Verified,
3 open models × 2 scaffolds × 10 runs, 25.58B tokens, 1.88M tool calls):**

- Single-run pass@1 ranges span **2.2–6.0 percentage points** across identical
  configurations; σ > **1.5pp even at temperature 0**.
- Detecting a 2pp improvement needs **~9 independent runs**; a 1pp improvement
  needs **~36 runs** (σ=1.5pp, p<0.05, 80% power).
- Gap between optimistic pass@k and pessimistic pass^k bounds reaches
  **24.9pp**.
- The same model shows *different variance profiles under different scaffolds*
  (nano-agent vs R2E-Gym) — scaffold choice changes not just the mean but the
  distribution.

Corroboration: arXiv:2606.04261 finds light scaffolds **reduce outcome
variance without improving the best outcome**, while heavy scaffolds shift
outcomes substantially in either direction. arXiv:2606.20683 concludes scaffold
effectiveness depends on **interface design more than feature count** — minimal
scaffolds with well-chosen primitives approach full-framework performance.

**Harness variables that move results** (arXiv:2603.05344, engineering report —
directional, no effect sizes published):

1. **Prompt composition** — modular, priority-ordered system-prompt sections;
   provider-conditional sections for cache efficiency.
2. **Error feedback timing** — event-driven reminders injected at decision
   points to counteract "instruction fade-out" in long sessions (vs
   front-loading everything in the system prompt).
3. **Edit/diff format** — a 9-pass fuzzy-match pipeline for file edits;
   robustness of the edit tool is itself a capability variable.
4. **Context management** — five-stage progressive compaction; episodic vs
   working memory separation.
5. **Tool surface** — lazy tool discovery (MCP) to keep tool descriptions from
   consuming the prompt budget.

**Takeaways for eval-lab:**

- **T1.** Single-attempt cohort comparisons cannot detect <3pp differences.
  The lab's experiment specs (`schemas.ExperimentSpec.attempts`) should default
  to ≥5 attempts for any claim of harness improvement, and cohort reports must
  carry σ, not just means. (Directly implementable in `cohort.py` reporting.)
- **T2.** "Agent" must be recorded as (adapter, version, model, prompt digest) —
  the scaffold is a first-class experimental variable, never a constant. The
  reproducibility contract in `docs/architecture.md` already lists this; the
  Parquet projection must carry it per-trajectory so cross-scaffold queries are
  possible (see P4 queries).
- **T3.** Temperature 0 does not buy determinism. Never treat two runs as
  A/A-identical; store per-run variance explicitly.

## 2. Benchmark contracts: Terminal-Bench 2/3 and SWE-bench

**Version correction (2026-08-28):** the 2026-08-15 note below was correct
*then* — there was no Terminal-Bench 4.0 — but Terminal-Bench 4.0 **was
released 2026-08-26/28** (release tag `v4.0.0`, commit `452bf30`). The lineage
is TB1 → TB2 (89 tasks) → TB2.1 → **TB3** (74 tasks) → **TB4** (66 tasks), all
under the same `terminal-bench/terminal-bench` dataset with semantic
versioning. TB4 is a maintenance release (not a re-built sequel): it removes 8
saturated/contaminated tasks, fixes 19, and sets a flat 8-hour agent timeout on
every task. **TB3 and TB4 scores are not comparable** — TB4 is a semver
x-increment requiring a full re-run, so a TB3 cohort must never be mixed with
or compared against a TB4 cohort.

The machine-readable migration record (8 removed, 19 fixed, pin/license/schema
status, expected 66-task inventory) lives at
`src/evallab/data/terminal-bench-4-migration.json`; `python -m evallab.craft
plan --tb4` reports the live inventory and 74→66 delta against a pinned v4
checkout.

### TB2 → TB3: what changed and why (verified against local clones)

This lab holds first-hand evidence: `~/Developer/lab-extras/data/agent-evals/frontier-bench@3d694e91`
(audited in the fin-saccr-rwa study) and the curated library's 19 re-verified
tasks (`library/curated/README.md`).

| Contract element | TB2 | TB3 / Frontier-Bench |
|---|---|---|
| Verifier location | agent's container (shared) | **separate container** built from `tests/`, artifacts explicitly declared (`environment_mode = "separate"`) |
| Verifier network | runtime `curl`/`uv` downloads | **forbidden at trial time**; deps baked into tests/Dockerfile (`checks/check-trial-network-fetch.sh`) |
| Anti-gaming | none systematic | cheating-agent CI runs, canary GUIDs in every text file, 23 static checks |
| Timeouts | shorter | agent 1.5–8h (`timeout_sec` 5400–28800 across 74 tasks), verifier ≤600s typical |
| Resources | modest | 45/74 tasks ≤8GB; 24 heavier; 4 GPU-only (this lab's `docs/execution-tiers.md`) |
| Reward | binary `reward.txt` | binary, plus per-test CTRF sidecar |

The forcing event: Berkeley RDI showed a fake `curl` wrapper scored **89/89 on
TB2 with zero tasks solved** (82/89 tasks fetched `uv` at verification time).
Terminal Wrench (arXiv:2604.17596) generalized this: 331 demonstrably hackable
environments, 3,632 exploit trajectories; >15% of surveyed verifiers bypassable.

### SWE-bench family contract

Task = repo snapshot + issue text; verification = FAIL_TO_PASS + PASS_TO_PASS
test sets executed post-patch; reward binary. Weaknesses (motivating Pro/Live
variants): training-set contamination of popular repos, saturation (~75% on
Verified), and solution leakage in issue threads. SWE-bench Pro answers with
copyleft/commercial held-out repos; SWE-bench-Live/LiveCodeBench answer with
post-cutoff continuous refresh — the only *structural* contamination defense.

**Takeaways for eval-lab:**

- **T4.** The lab's synthetic-task gate (P5) must adopt TB3's full contract:
  separate verifier, no trial-time network in tests, declared artifacts,
  cheating-agent pass, canary strings. This is now the documented floor, not
  best practice.
- **T5.** Timeout is a *task-schema field with analytical value*: TB3 timeouts
  span 1.5–8h and timeout-vs-solve interacts with model strength (Harbor-Index
  found open-weight models time out 3–4× more often than frontier models).
  Timeouts must be first-class in the Parquet projection.
- **T6.** Binary rewards hide diagnosis. TB3's CTRF per-test sidecar is the
  model: keep the headline binary, store the full check vector (the lab's
  Postgres `rewards` table + verifier stdout already do this — preserve it in
  the Parquet layer too).

## 3. Context degradation and long-horizon failure

**Established results:**

- **Context rot** (Chroma, 18 frontier models): every model tested degrades as
  input length grows, on tasks whose difficulty is otherwise constant.
- **Long-horizon cliff** (METR time-horizon work): near-100% success on tasks
  taking skilled humans <4 minutes; <10% on tasks taking >4 hours.
- **Embedding effect**: web-agent tasks solvable at 40–50% in short-horizon
  form drop **below 10%** when embedded in a longer interaction history — even
  with all relevant information still in context.
- **Coding agents maximize context rot** by construction: accumulative tool
  output, high distractor density from code search, 15–60min+ horizons.
- **Mitigation magnitudes** (Anthropic evals): context editing alone +29%;
  editing + memory tool +39%.
- **Failure synthesis** (arXiv:2607.05775, six clusters): tool
  invocation/parameter errors; planning & constraint-satisfaction failures;
  long-horizon degradation from context accumulation; multi-agent coordination
  failures; safety/security under adversarial or underspecified conditions;
  measurement-validity problems. Failures **compound nonlinearly with task
  length**; sub-task competence does not compose into end-to-end success.

**Takeaways for eval-lab:**

- **T7.** Context growth is a measurable trajectory property, not an anecdote.
  The Parquet projection must support computing **tokens-in-context per step**
  so Context Bloat Velocity (P4) is a queryable fact.
- **T8.** The six-cluster taxonomy from arXiv:2607.05775 is the peer-reviewed
  anchor for this lab's failure buckets; P4's operational buckets (Flaky
  Verifier, Tool Hallucination, Timeout, Surrender) map into clusters 1, 3, and
  6 and should be documented as such rather than invented ad hoc.
- **T9.** Because failures compound nonlinearly, *where* in a trajectory a
  failure occurs matters as much as whether one occurs. Step index and
  cumulative-token position belong on every extracted failure fact.

## 4. What this survey changes about lab priorities (judgment)

1. Variance-aware reporting (T1/T3) is cheap and immediately raises the
   evidential standard of every cohort comparison the lab produces.
2. The scaffold is the lab's most promising *independent variable*: the
   literature says harness deltas of several pp are common, and the lab is
   uniquely instrumented (one harness, many adapters, ATIF everywhere) to
   measure them honestly.
3. Long-horizon/context metrics are underserved publicly (no benchmark measures
   multi-session work; TB3 tasks are single-goal). The lab's trajectory
   intelligence layer (P4) can produce novel, publishable measurements from
   data it already generates.
