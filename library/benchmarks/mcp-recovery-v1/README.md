# MCP Error Recovery Benchmark Family (v1)

Source-only certified error recovery benchmark for Model Context Protocol (MCP) agents under streamable-HTTP transport, fault injection, and StateCertificate invariant proofs.

## Architectural Principles
1. **Source-Only & Zero-Vendoring**: No vendored copies of third-party benchmark repos or bulky task corpora in Git. Tasks are materialized on-demand into gitignored, digest-addressed paths (`derived/harbor-tasks/mcp-recovery/<digest>/...`).
2. **Deterministic Primary Truth & Verifier Twins**: Uses un-intervened clean twins and fault twins to verify state recovery without relying on probabilistic LLM judges.
3. **StateCertificate Guarantees**: Mutations, pre/post database states, and cryptographic digests are recorded canonically in `/app/evidence/benchmark-events.jsonl` with monotonic indexes and no wall-clock timestamps.
4. **Adaptive Recovery vs. Blind Retries**: Distinguishes between transient auto-clearing faults (where blind retry passes) and adaptive recoveries (where agent mutates authorization scopes, routes around errors, or performs fallback queries).

## Campaign 0 Calibration Matrix
- **Fault Modes**:
  - `permission_denied` (HTTP 403 / token scope expiry)
  - `not_found` (HTTP 404 / entity not found)
  - `timeout` (HTTP 408 / gateway timeout)
  - `malformed_output` (Transport corruption / malformed stream)
  - `silent_wrong_result` (Corrupted semantic payload requiring secondary verification)
- **Persistence Levels**:
  - `1`: Transient (clears after single failure)
  - `2`: Recurrent / Persistent (requires strategy mutation or multi-step mitigation)
- **Seeds**: `42`, `101`, `2024`

## Ecological Replay & Provenance
Inspired by the concepts in MIT Recovery-Bench (https://github.com/mcp-bench/recovery-bench), but rebuilt ground-up to eliminate live-API replay drift and provide closed-loop Harbor test containers.
