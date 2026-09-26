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

Native usage estimates, physical-call proxy accounting, and provider invoices
are different records. Some producers omit `llm_call_count`, and some upstream
length-interrupted calls are absent from native token totals. Neither zero
declared calls nor missing usage proves zero actual consumption. The proxy's
conservative uncached accounting is not an invoice.

### Per-run report

`uv run evallab report run <trial_dir | job_dir> [--json] [--output-dir DIR]`
renders one deterministic report per trial (schema `evallab.run_report/v1`) and,
for a job directory, a rollup (`evallab.job_report/v1`). The Markdown is rendered
from the JSON, so people and models read the same facts. It never writes into
the run directory; `--output-dir` writes `<trial>.run_report.{json,md}` and
`job.run_report.{json,md}`. Sections: outcome, Harbor phase timing and gaps
between agent steps, tokens and cost, tools, revisits, subagents, context
events, errors, an optional benchmark domain section (`## Domain: <name>`,
absent as `domain: null` when no plugin detects the trial), a bounded
timeline (`--full-timeline` lists every step), and data-quality notes.

`evallab.run_report/v1` is additive: a `domain` section appears when a
`src/evallab/interpretation/domains/` plugin detects the trial, else `domain`
is `None`. The CEO-Bench plugin (`ceo_bench`) reads the `ceo_bench/*.json`
sidecars written by the `ceo_bench` adapter package
(`library/adapters/ceo_bench/ceo_bench/bridge.py`), which converts one
CEO-Bench harness run (`bash_agent_runs/run_<id>/`) into a Harbor-shaped trial
dir (`result.json` with honest nulls, one trajectory step per tool call, plus
the sidecars). Operator path (the adapter is an independent uv project so
`sqlcipher3` never becomes an evallab core dependency):
`uv run --project library/adapters/ceo_bench python -m ceo_bench <run_dir>`
(prints the trial dir; `--out` overrides the `<run_dir>.trial` default),
then `uv run evallab report run <trial_dir>`. An encrypted `world.nmdb`
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
- Domain sections are benchmark plug-ins (`src/evallab/interpretation/domains/`,
  explicit `PLUGINS` registry, deterministic order) reading verifier outputs
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

### CLI reference: deterministic trajectory analysis

The deterministic analyzer runs directly on retained raw native ATIF trajectories without invoking models, sandboxes, or tools:

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
