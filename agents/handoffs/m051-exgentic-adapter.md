Status: ready
Last: registered the typed upstream-source and file-only Exgentic adapter contract
Next: freeze one minimal raw Exgentic file and define UpstreamSource and AdapterManifest before conversion logic
Blockers: none

# M051 (E) — File-only Exgentic adapter

## Contract

- **Outcome:** define `UpstreamSource` and `AdapterManifest`, then prove a file-only Exgentic adapter preserving nulls, raw bytes, and source revision.
- **Lane / owner:** Tasks / Tasks lane owner.
- **Exclusive lease:** `src/evallab/upstream_adapter.py` (new), `library/adapters/exgentic/**`, `tests/test_upstream_adapter.py` (new), and `tests/fixtures/exgentic/**`.
- **Status:** ready; partial generic import surface exists, but this typed adapter contract does not.
- **Acceptance:** an offline fixture converts without network or provider calls; source revision and manifest digest are recorded; absent values remain null rather than empty/default; raw source bytes are retained and digest-verifiable; replay is byte-identical.
- **Next executable step:** freeze one minimal raw Exgentic file and write the source/manifest schemas before conversion logic.

## Source evidence and dependencies

PR #146 added `task_import.py` and a pinned external cohort, but no typed upstream-source/adapter-manifest record. This is an independent follow-on PR with no dependency on M047–M050.
