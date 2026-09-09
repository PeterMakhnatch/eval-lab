# HAR-13 — independent paired harness analysis

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
Missing/unreadable paths remain diagnostics; missing arms and partial trials
remain visible. Conflicting duplicate UUIDs at different source paths are
refused instead of choosing a favorable record.

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
- Model names must agree; known conflicting model revisions reject a pair.
  Missing revisions allow a **configured-only descriptive** comparison labeled
  `unknown_revision`. Revision fields come from native model metadata, never the
  analysis checkout's Git revision. Equal recorded revision labels are not
  independently verified weight bytes.
- Missing task/verifier/harness identities are not equal identities. Explicit
  verifier digests and task/configuration-derived verifier identities have
  different reported strengths. Environment equality is configuration equality,
  not proof of identical container image bytes.
- Positive, neutral, negative and fractional deltas survive. There are no
  significance tests, causal improvement claims, or shortest-trace objective.
  This is a whole-agent configuration comparison; matched total budgets are not
  presumed. Native approval or task admission is never established by this tool.
- A fixture record cannot be relabeled `model-run` or historical. `model-run` is
  a caller declaration checked against known fixture/control records, not proof
  of authorized execution. Live model interpretation still requires HAR-11's
  actual approved native receipts and correct task/runtime bindings.

## Compute and failures

Every retained attempt contributes available time and compute, including
exceptions. Each aggregate has `total`, `observed_subtotal`, `covered_count`, and
`n_total`; an incomplete total stays null. Empty coverage is not zero usage.

`native_aggregate_usage` preserves `agent_result` token/cost fields without
assuming they are root-only or inclusive of all workers. For supported validated
ATIF, `root_usage`, `worker_usage`, and `total_usage` use disjoint retained
**generation-step metrics**, excluding copied context and user messages. They do
not add inclusive `final_metrics` to worker totals. Missing components remain
null; unresolved worker references or ambiguous document roles prevent a total.
No observed worker document is not evidence of zero workers.

These role amounts describe the retained capture, **not independently verified
full-episode physical requests or cash billing**. Input cached tokens are a
subset, not extra tokens to add again. The report does not equate ATIF steps
with physical attempts or requests.

The published HAR-12 source format `authors-rlm-root-messages` (unversioned
`schema_version: null`) is consumed from `agent/rlm/root-messages.json`.
`agent_result.metadata` supplies the reported root/worker model identities and
root-only `root_input_tokens` / `root_output_tokens`. These amounts may be
incomplete, so they never establish full-episode compute. Root/worker logical
call counts are not physical request attempts. `worker_usage` remains unknown;
full total tokens/cost remain null, even when the reported worker-call count is
zero. The collector exports `source_native_accounting` with the source path and
retains its hash in `metadata.source_inputs_sha256`, never the messages. A
conflicting root identity suppresses qualification. Unknown future format
versions remain explicit rather than being guessed.

The JSON report includes per-task raw/effective reward, success threshold,
source paths, model/harness/verifier qualification, wall/agent time,
root/worker/retained-total/unpartitioned usage, and exception category/phase.
Markdown exposes the same diagnostics; SVG plots reward and retained-compute
deltas, including unavailable/unqualified rows. Actual inputs remain required
for any next harness intervention; fixture results cannot justify one.
