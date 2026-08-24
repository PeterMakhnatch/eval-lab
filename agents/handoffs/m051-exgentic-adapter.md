Status: review-wanted
Last: implemented one strict manifest contract and offline file adapters for Exgentic and Recovery-Bench constructed fixtures
Next: review the manifest boundary, field mapping, and refusal coverage; do not acquire or register upstream corpora
Blockers: none

# M051 (E) — File-only upstream result adapters

## Contract

- **Outcome:** bind local Exgentic trajectory JSONL and Recovery-Bench result JSON to immutable upstream revisions; emit validator-conformant ATIF only when trajectory evidence exists, alongside external evidence, without importing or executing upstream code.
- **Lane / owner:** Tasks / Tasks lane owner.
- **Exclusive lease:** `src/evallab/upstream_adapter.py`, `library/adapters/exgentic/**`, `library/adapters/recovery-bench/**`, `tests/test_upstream_adapter.py`, and `tests/fixtures/upstream_adapters/**`.
- **Status:** review-wanted. The implementation is file-only; it does not vendor, install, fetch, register, or execute either upstream project.
- **Acceptance:** repeated offline imports are byte-identical; raw input bytes, digest, canonical source URL, immutable revision, license status, manifest version, and adapter code digest remain bound; incompatible schema/revision/license and unsafe paths are refused.

## Mapping notes

- Exgentic `trajectory.jsonl`: `session_id` becomes the ATIF session; observed upstream steps determine grouping and are reindexed to ATIF's required one-based sequence. Action `name` and `arguments` become a tool call. Observation values are retained without normalization, including nulls. Required ATIF agent, message, and source-call identity fields use explicit `[unavailable]` markers because the source fixture does not contain them.
- Recovery-Bench `result.json`: no ATIF document or trajectory step is emitted. The output contract is external-evidence-only and records `trajectory: null`; the full observed result, including verifier rewards, remains external evidence.
- Fields outside each declared input vocabulary are retained under `mapping.unknown_fields_by_record` and in `observed_records`, but are explicitly excluded from ATIF mapping. Role, capabilities, isolation, and output claims must exactly match the selected versioned input contract.
- Compatibility fixtures are lab-constructed and contain no copied upstream bytes. `tests/fixtures/upstream_adapters/PROVENANCE.json` records this boundary.

## Source evidence and dependencies

- Exgentic output format: `https://github.com/Exgentic/exgentic/blob/ae8d10f7f1e29d2b08d8a5d41bafa16836004998/docs/output-format.md`; repository license file declares Apache-2.0.
- Recovery-Bench source: `https://github.com/letta-ai/recovery-bench/tree/c5f83f2ba4f882a9b544c7bf0fa9be1bc3859c78`; `pyproject.toml` declares MIT, recorded as declaration rather than verified license-file evidence.
- This is the upstream-adapter mission already registered as M051 on the board. The earlier M050 identifier belongs to the merged lessons-boundary mission and is not rewritten.
