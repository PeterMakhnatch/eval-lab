Status: building
Last: HAR-17 integrated after 18 Gemini worker invocations; 67 focused cases and actual CLI/source-preservation checks pass.
Next: Complete repository publication gates, publish the scoped PR, and return its exact receipt on HAR-17.
Blockers: none for code publication; experimental dispatch and model interpretation remain separately gated.

# HAR-17 — adversarial successor to HAR-13

Peter explicitly requested substantial multi-agent Gemini work. Eighteen native
worker invocations covered eight independent adversarial proposals, six isolated
correction slices, and four fresh review/consolidation slices. The configured
route was `google-antigravity/gemini-3.8-flash:high`; provider billing is not
inferred from that configuration. Two first-wave result transports failed after
writing recoverable artifacts; their files were inspected, not assumed accepted.

Integration lives in `.worktrees/har17-adversarial-main`, branch
`research/har17-adversarial-validity`, based on PR394
`14745315bbe447c13021435cdd51836b008edf2d`.

## HAR-17 proof and interpretation

- Final focused suite: 67 passed. The same regression set against unchanged
  published implementation bytes had 22 failures; original 33 cases remain.
- Actual CLI scenarios: ordinary fixture, structured error, UUID alias and
  payload-only HAR-12 accounting. Source preservation, idempotence and
  fixture-to-model relabel refusal passed; generated SVG was visually checked.
- Existing historical oracle/nop input produced 0/1 qualified pairs, not a model
  result. No old control, Docker/Harbor trial or model request was rerun.
- Evidence under `derived/har17/`: `baseline-final-regressions.xml`,
  `final-regressions.xml`, `cli-smoke-receipt.json`, `baseline-adjudication.json`,
  `review-disposition.json`, and `historical-report/`.
- Original proposals remain local evidence; eight bulky proposal test modules
  were replaced by meaningful additions to the existing CI-collected test file.
- Current `make premerge`: 3799 passed, 52 skipped, one xfailed, one failed.
  The sole failure is unchanged `test_ingest_verify_cli_output`, whose CLI
  inspects existing primary-checkout Parquet state with missing partitions.
  Lint, document freshness, governance, registry and lessons gates passed before
  that failure. This supersedes the older 19-failure HAR-13 gate result below;
  no shared ingest/storage fix or full-green claim is included.
  Remaining Docker-free smoke and pinned ty 0.0.71 gates passed independently.
- Rejected review proposals included cost rounding, redundant identity aliases,
  fabricated-report fallbacks, removing the oracle/nop boundary, unsupported
  reference-key guessing and unused private compatibility parameters.
- Producer source: HAR-12 PR393 `ddf1a0112c4873e38d165942216429d24c62cccb`,
  `harbor_rlm.py` SHA256
  `609f3d2065daf438f8c2f11c83713dc0469dc8b6cf01c380cdefaf228347076c`.
- Full pre/post JSON-envelope equality is not an atomic filesystem freeze:
  concurrent mutation followed by reversion still requires frozen input evidence.
- HAR-12 now reports newer backend source at PR392 `1183f6af`; that updates
  source availability, not this task's experimental authorization or ownership.

No persistent peer recruitment, task admission, backend workaround, merge or
model-performance claim. Return through HAR-17 and the canonical Quality handoff.

## Previous HAR-13 delivery

- Branch: `research/harness-first-analysis`.
- Explicit base: `edd3cbc1c44aa55764ebafab24639fd758d38f72`, published PR389.
- PR: https://github.com/PeterMakhnatch/eval-lab/pull/394 (draft; no merge/full-green claim).
- Ownership: `research/analysis/harness-first/` and its CI-collected test under
  `research/analysis/tests/`. No shared runtime, queue, profile, schema, registry,
  producer, or primary-checkout edits.
- Return: HAR-13/related dependency tickets on Linear and the canonical Quality
  handoff only. No completion pages or background monitor revival.

### Consumer

`uv run python research/analysis/harness-first/analyze.py SPEC --repo-root ROOT
--evidence-kind fixture|historical|model-run --output DIR`

SPEC is existing `CohortComparisonSpec`, including actual PR391
`build_comparison_spec` output. JSON/Markdown/SVG retain incomplete/unqualified
pairs, partial and neutral deltas, raw/effective reward, failed-attempt compute,
model/task/verifier/harness qualifications, and usage missingness.

Actual published HAR-12 `agent/rlm/root-messages.json` is decoded as source-native
root-only reporting, never ATIF or a complete request ledger. Worker/total usage
remain unknown in that format. No fixture result is a model improvement.

### Proof

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

### Independent runtime/product review

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
