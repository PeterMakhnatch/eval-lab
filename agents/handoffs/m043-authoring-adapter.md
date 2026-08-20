# M043 — Authoring model adapter

Status: complete — ready for review
Last: Replaced the production novel-spec path with an injected `AnalyzerCallable`/`ModelAdapter` design seam. `model-propose` validates strict `spec/1` JSON before generating a quarantined package, records hashed prompt/output plus model, transport, and schema provenance, deduplicates coordinates, and never registers. CLI model paths require explicit pinned `--model` and `--transport` and advertise subscription-quota spend. No paid model was invoked.
Next: Parent live-smokes after merge with Gemini Low; do not merge this PR from the authoring branch.
Blockers: none.

## What landed

| Component | Detail |
|---|---|
| `src/evallab/authoring.py` | Added `ModelBackedDesigner`, strict prompt/schema validation, `propose_model`, adapter injection on `AuthoringPipeline`, atomic cleanup on model/validation failures, coordinate duplicate refusal, and explicit CLI `model-propose` / model-backed sample configuration. Renamed the deterministic fallback to `local_test_designer`. |
| `src/evallab/schemas.py` | Constrained `ProposalSpec.seed_class` to the registered authoring seed literal. |
| `tests/test_authoring.py` | Fake adapter end-to-end quarantine/provenance coverage, malformed JSON/schema/timeout/nonzero-exit fail-closed cases, duplicate-coordinate control, and prompt/schema assertions. |
| `docs/authoring.md` | Documented explicit model/transport selection, quota warning, and model-propose command. |
| `docs/platform-architecture.md` | Updated the authoring seam's default/fallback description. |

## Invariants

1. Provider SDKs are not imported. The designer receives an injected analyzer-compatible callable; merged `ModelAdapter` supplies the production subprocess transport.
2. `ProposalSpec` is validated before `generate_stub_task` runs. Raw model text is never written; provenance stores only SHA-256 digests, model, transport, and schema versions.
3. A failed model call, malformed JSON, schema failure, or duplicate coordinate creates no quarantine directory or ledger row.
4. Generated proposals enter `proposed`; the existing battery, review, and human-only registration refusal paths are unchanged.
5. Production CLI model paths cannot use an implicit model or transport. `ModelAdapter` separately rejects `auto`, `default`, `latest`, and other unpinned selectors.

## Verification

- `uv run pytest tests/test_authoring.py tests/test_modeladapter.py` — 73 passed.
- `uv run ruff check src/evallab/authoring.py src/evallab/schemas.py tests/test_authoring.py` — passed.
- `uv run python -m evallab.authoring model-propose --help` and `sample --help` — show explicit model/transport and subscription-quota wording.
- `bash scripts/premerge.sh` — exit code 0; 1539 passed, 2 skipped, 1 xfailed; smoke checks passed; ty reported 27 diagnostics against the repository baseline of 28.
- No paid model or external provider call was run.
