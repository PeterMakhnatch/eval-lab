# Harness-first paired analysis — HAR-13 / HAR-17

A read-only analysis CLI over the existing `CohortComparisonSpec`, native
`JobRecord`/`TrialRecord`, `extract_trial_fact`, and `project_trial` interfaces.
It does not submit experiments, contact models, ingest fixtures, or create a
new evidence store. HAR-11 owns experimental dispatch.

## Reproduce the CPU software checks

From the Eval Lab checkout with its locked environment installed:

```bash
uv run python research/analysis/harness-first/fixtures.py \
  --output-dir derived/harness-first/fixtures
uv run python research/analysis/harness-first/analyze.py \
  derived/harness-first/fixtures/spec.json \
  --repo-root derived/harness-first/fixtures \
  --evidence-kind fixture \
  --output derived/harness-first/report
uv run pytest -n 0 research/analysis/tests/test_harness_first_analysis.py
```

Use a fresh fixture destination; the generator refuses nonempty directories.
Generated native-shaped records are explicitly tagged CPU fixtures, never model
results or actual attempts. Their deterministic UUIDs are fixture identifiers.
The boundary matrix is software verification, not the authorized live task grid.

The analyzer writes `report.json`, `report.md`, and `plot.svg` atomically as a
new output directory. Identical rerenders are idempotent. Differing existing
output and overlap with input evidence are refused; use a new report directory
for a changed analysis. Input files are never modified. Reports retain spec,
analysis-code and input-file digests; analysis Git revision is not model revision.

The selected file evidence envelope includes nested trajectories, JSONL request
records, linked sources regardless of extension, and native
parent metadata even when directly selected trials have no parent `result.json`.
Files under missing/malformed trial records remain in the envelope on partial
jobs. It is hashed before and after collection; observed additions,
deletions, changed bytes, or escaping source links refuse the analysis.
This is an evidence envelope, not a list of exclusively consumed fields.
Equal pre/post manifests do not establish an atomic filesystem snapshot:
use frozen inputs to rule out concurrent modification and reversion.

## Integration contract

```text
uv run python research/analysis/harness-first/analyze.py SPEC.json \
  --repo-root ROOT --evidence-kind fixture|historical|model-run --output DIR
```

`SPEC.json` is the existing `evallab.schemas.CohortComparisonSpec`, with exactly
two ordered cohorts: baseline then candidate. Each uses existing relative
`paths` and optional `trial_names`. Only `pass_k=[1]` is supported here; multiple
attempts for a pairing key are displayed as ambiguous rather than selected or
zipped. `pairing_key` may be `task_digest`, `task_block_id`, `task_name`, or
`trial_name`; task/verifier equality remains necessary regardless of the key.
Constraints and the configured pass threshold are respected.

An explicitly selected unfinished native job can expose its existing typed
trial records without being relabeled complete. Directory discovery retains the
existing completed-job semantics, with a notice: select a partial job explicitly.
Direct trial selection does not parse unrelated siblings. Explicit job selection
quarantines malformed children with path diagnostics while retaining valid ones.
Missing arms and repeat ambiguity are counted independently. UUID spelling
aliases cannot evade duplicate-source or physical self-comparison checks.

Cohort summaries retain `trial_population.jobs`: source path, native job ID,
selection scope, declared and observed trial counts, missing declared count and
coverage reasons. A finished job with missing records is incomplete too; valid
siblings remain available for descriptive pairs. No missing trial identity or
outcome is synthesized. `n_total_attempts` counts observed unique records, while
`observed_pass_rate` is explicitly conditional on those records. With incomplete
population coverage, cohort `pass_rate`, complete compute/time totals and metric
`n_total` remain unknown; declared-versus-observed counts remain visible in the
population receipt. Direct trial selection does not claim full parent-job coverage.

For local Python use, `from harness_first import analyze` exposes
`analyze(spec, *, repo_root, evidence_kind)`. It accepts the existing schema
instance, a JSON path, or a dict and returns the same JSON-compatible report.
The analysis directory must be on the local Python module path. The CLI is the
preferred cross-worktree/version-isolated interface; no global package install
or modification of Eval Lab's root dependency configuration is required.

PR391's `research/experiments/harness-first/compile.py::build_comparison_spec`
returns this exact existing schema. Supply its actual baseline/candidate job
paths; no adapter or alternate comparison DTO is needed. The report's
`metadata.spec_id` preserves `comparison_id`; `metadata.experiment_id` preserves
the producer's experiment identifier. An integration view should verify that
association rather than attach an unrelated report.

## Validity and interpretation

- Raw rewards remain visible. Exceptions (including verifier and agent failures)
  and unfinished trials suppress effective reward. Only an explicitly configured
  completed `AgentTimeoutError` may map to zero as a budget-exhaustion failure;
  infrastructure faults do not become task reward zero.
- Model names must agree. Contradictory nonempty identity sources within a trial
  reject qualification instead of silently choosing a favorable value.
  Root ATIF document and non-copied generation-step models are reconciled against
  result/lock identity; worker models need not equal the root. Contradictory
  result/lock harness versions are reported as `conflicting_sources`.
  Missing revisions allow a **configured-only descriptive** comparison labeled
  `unknown_revision`; the same normalization applies to native and source-native
  revision fields. Equal recorded labels are not independently verified weights.
- Missing task/verifier/harness identities are not equal identities. Explicit
  verifier digests and task/configuration-derived verifier identities have
  different reported strengths. Environment equality is configuration equality,
  not proof of identical container image bytes.
- Positive, neutral, negative and fractional deltas survive, including genuine
  sub-micro cost differences without calculation-time rounding. There are no
  significance tests, causal improvement claims, or shortest-trace objective.
  This is a whole-agent configuration comparison; matched total budgets are not
  presumed. Native approval or task admission is never established by this tool.
- Recognized explicit evidence-origin tags must agree with the requested kind.
  A fixture cannot become historical/model evidence, and oracle/nop remain
  controls. Missing origin metadata is not reconstructed from arbitrary names.
  Present but unreadable or non-object parent provenance is not absence: its path
  and failure reason remain visible, affected pairs are unqualified, and a
  caller-declared `model-run` report is refused rather than promoting lost evidence.
  `model-run` remains a caller declaration, not execution authorization; actual
  model interpretation requires HAR-11's approved native receipts and bindings.

## Compute and failures

Every retained attempt contributes available time and compute, including
exceptions. Each aggregate has `total`, `observed_subtotal`, `covered_count`, and
`n_total`; an incomplete total stays null. Empty coverage is not zero usage.

`native_aggregate_usage` preserves `agent_result` token/cost fields without
assuming they are root-only or inclusive of all workers. For supported validated
ATIF, `root_usage`, `worker_usage`, and `total_usage` use disjoint retained
**generation-step metrics**, excluding copied context and user messages. They do
not add inclusive `final_metrics` to worker totals. Missing components remain
null. An unresolved or unsupported worker reference prevents a total even when
another worker is observed; repeated references do not double-count that worker.
No observed worker document is not evidence of zero workers.

These role amounts describe the retained capture, **not independently verified
full-episode physical requests or cash billing**. Input cached tokens are a
subset, not extra tokens to add again. The report does not equate ATIF steps
with physical attempts or requests.

The published HAR-12 source format `authors-rlm-root-messages` (unversioned
`schema_version: null`) is consumed from `agent/rlm/root-messages.json`.
Its payload and `agent_result.metadata` supply reported root/worker identities,
root-only token amounts, and logical call counts. Conflicting amounts stay
unknown; contradictory identity or scope declarations are not silently promoted.
Source-native fields follow the published HAR-12 agent at PR393
`ddf1a0112c4873e38d165942216429d24c62cccb`, not an invented future schema.
These amounts may be incomplete and never establish full-episode compute.
Worker usage and full total tokens/cost remain null even when worker-call count
is zero. `source_native_accounting` retains source and backend provenance, never
the messages or physical-request claims. Unknown future formats remain explicit.

The JSON report includes per-task raw/effective reward, success threshold,
source paths, model/harness/verifier qualification, wall/agent time,
root/worker/retained-total/unpartitioned usage, and exception category/phase.
Markdown exposes the same diagnostics; SVG plots reward and retained-compute
deltas, including unavailable/unqualified rows. Actual inputs remain required
for any next harness intervention; fixture results cannot justify one.

## Opt-in observation masking — HAR-50

`evallab.observation_masking.LastNObservations(keep_last=N)` provides a
deterministic model-visible context view. It preserves every instruction,
assistant/reasoning item, call ID, recent observation and non-text content block.
Only text bodies of older explicitly linked tool results are replaced. It never
classifies an ordinary user message as an observation or rewrites retained runs.
Positive integer `N` is required; `keep_output` tags protect additional old results.

This deliberately adapts the MIT-licensed
[Complexity Trap / SWE-agent implementation](https://github.com/JetBrains-Research/the-complexity-trap/blob/bf15b5fb7d279679035a007ac9a81084d6b9a89a/sweagent/agent/history_processors.py):
polling is fixed at one; instructions are identified by native role rather than
counted as the first observation; always-remove tags cannot override preservation
of recent results. Attribution and the upstream license are retained in the module.

### Offline retained-history demonstration

```bash
mkdir -p derived/observation-masking
uv run python -m evallab.observation_masking \
  --input /path/to/retained/rollout.jsonl --input-format codex-rollout \
  --keep-last 10 --output derived/observation-masking/context.json
```

The default `--input-format messages` accepts a native message list or JSON object
containing `messages`. `codex-rollout` takes original `response_item.payload`
objects without renaming calls or reconstructing messages from ATIF. Original
`session_meta.payload` objects, including base instructions, are retained separately.
This is an offline context view, not an exact historical provider request or replay.
Chat Completions tool messages and flattened Responses function/custom-tool items
are supported; ambiguous legacy user-role observations and unresolved tool calls
are rejected or left untouched rather than guessed.

Output is a new mode-0600 JSON artifact; existing paths, including the source, are
refused. The console receipt excludes message contents and contains input,
implementation, policy and before/after context hashes. Footprints are canonical
UTF-8 JSON bytes (including retained session metadata), **not tokens or costs**.
Fewer bytes need not mean lower billed cost because caching and provider encoding
differ. Short observations can even grow when replaced by a marker.

### Existing manageable-harness boundary

`evallab.mini_observation_masking.LastNObservationModel` is an explicit subclass
of mini-swe-agent **2.4.6**'s Chat Completions `LitellmModel`. It applies the policy
inside `_prepare_messages_for_api`, after upstream preparation, without changing
the default class, loop, tool execution or stored native history. Serialized model
metadata records the policy identity. The offline CLI needs only the standard
library; this optional consumer requires mini-swe-agent in its runtime environment.

The opt-in configuration is [`observation-masking.yaml`](observation-masking.yaml).
Load it after mini's built-in configuration. **Harbor's stock package installation
does not install a host-side policy file.** Before activation, the integration owner
must deliver the two `evallab` modules into the agent's actual Python environment
(a pinned Lab wheel, or an explicitly staged package on its Python import path),
pin mini-swe-agent to 2.4.6, and pass the YAML through the existing mini configuration
surface. Verify class resolution in that environment. No runner/adoption changes
or live model trials are included here; HAR-46 owns that integration.

The native Codex evidence validates the deterministic transformation on real
retained histories. The separate no-network Mini-SWE preparation smoke validates
the actual consumer boundary using linked synthetic chat messages. Neither is a
model-backed success comparison, a live Codex modification, or verified support
for mini's Responses-API model class.
