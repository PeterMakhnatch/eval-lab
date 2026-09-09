Status: review-wanted
Last: PR394 published; 33 focused boundaries and fixture/historical CLI consumers exercised with no experiments dispatched.
Next: HAR-11 consumes the report fields; analyze actual paired model receipts only after recorded authorization and execution.
Blockers: HAR-11 model receipts await approval/runtime readiness; HAR-10 provider block stays in human review; 19 known baseline premerge failures remain outside this diff.

# HAR-13 — independent harness comparison analysis

- Branch: `research/harness-first-analysis`.
- Explicit base: `edd3cbc1c44aa55764ebafab24639fd758d38f72`, published PR389.
- PR: https://github.com/PeterMakhnatch/eval-lab/pull/394 (draft; no merge/full-green claim).
- Ownership: `research/analysis/harness-first/` and its CI-collected test under
  `research/analysis/tests/`. No shared runtime, queue, profile, schema, registry,
  producer, or primary-checkout edits.
- Return: HAR-13/related dependency tickets on Linear and the canonical Quality
  handoff only. No completion pages or background monitor revival.

## Consumer

`uv run python research/analysis/harness-first/analyze.py SPEC --repo-root ROOT
--evidence-kind fixture|historical|model-run --output DIR`

SPEC is existing `CohortComparisonSpec`, including actual PR391
`build_comparison_spec` output. JSON/Markdown/SVG retain incomplete/unqualified
pairs, partial and neutral deltas, raw/effective reward, failed-attempt compute,
model/task/verifier/harness qualifications, and usage missingness.

Actual published HAR-12 `agent/rlm/root-messages.json` is decoded as source-native
root-only reporting, never ATIF or a complete request ledger. Worker/total usage
remain unknown in that format. No fixture result is a model improvement.

## Proof

- Focused boundary suite: `uv run pytest -n 0 research/analysis/tests/test_harness_first_analysis.py`.
- Fixture CLI: generate a fresh directory with `fixtures.py`, then run the analyzer;
  output is explicitly CPU-fixture software verification.
- PR391 composition: `examples/pr391-historical-controls.json` is actual output of
  `build_comparison_spec` at `477dcdd21939d9009332db9dd6ed4af2ae432dbb`.
  Existing event-summary oracle/nop evidence yields one historical pair with
  unknown model identity and no qualified model delta. No old control was rerun.
- Final outputs: `derived/harness-first/publication-fixture-report/` and
  `derived/harness-first/publication-historical-report/`; explicitly different evidence kinds.
  Earlier rendered proof remains in `derived/harness-first/final-fixture-report/`.
- Independent native reviewer challenged the implementation; identified
  root-revision, missing identity, exception, compute-overlap and output-safety
  defects were corrected. Model qualification uses recorded model metadata, not Git.
- Required `make premerge` attempted: 19 failed, 3732 passed, 34 skipped, one xfailed.
  Failures are existing MCP wheelhouse/supply-chain and ingest_verify cases, also
  reported by HAR-14. No shared fix or repeated confirmation run undertaken.
  Final focused checks cover this diff; GitHub is the merge authority.

## Independent runtime/product review

Published HAR-11 head `a3619233dfc9e36d60af46aad74a6f6af5cee2c4` was audited with
an isolated CPU `compile_pair` call. The requested manifest root remained in the
label while the emitted ExperimentSpec used the profile's different root.
Exact source/probe/output: `derived/harness-first/har11-source-audit/receipt.json`.
The counterexample was posted directly on HAR-11; no submit, queue creation or
model request occurred. Updated Integration source now passes the requested
root id to `validate_model_pin`; the old counterexample was not rerun.

Actual model interpretation requires HAR-11 native baseline/candidate job paths,
correct root/profile/task bindings, current runtime artifacts and genuine per-spec
approval. Code publication is not blocked by that missing live input; no merge,
dataset/model publication, or performance acceptance is claimed.

HAR-11 currently reports the event-summary canary pair waiting with
`paid_run_unauthorized`, not executed model trials. Its current inspector exports
`comparison_spec` once both arms have jobs and accepts the report JSON/directory.
The HAR-10 provider safety block is not retried, rerouted or bypassed here.
