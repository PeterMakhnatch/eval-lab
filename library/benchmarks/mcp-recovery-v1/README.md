# MCP Error Recovery Benchmark Family (v1)

Source-only certified error-recovery family for Campaign 0 calibration. Tasks are
Harbor packages with a real FastMCP 3.4.7 streamable-HTTP sidecar, paired
clean/fault twins, and verifier-only oracle truth.

## Principles

- Source-only. No generated Harbor corpus, no Recovery-Bench vendoring.
- Deterministic verifier truth. No LLM judges. Oracle/NOP/blind-retry/wrong-repair
  controls are first-class.
- Evidence is canonical JSONL at `/app/output/benchmark-events.jsonl` plus
  `/app/output/final-state.json`. Event indexes are monotone. No timestamps.
- Silent-wrong and malformed reference truth lives in `tests/fixtures/` only.
  The main agent image does not COPY tests or solution trees.
- A transient auto-clear never earns autonomous recovery from blind retry.
  Paired fixed-policy/NOP-retry controls at the same persistence emit
  adaptation-required versus auto-clear outcomes separately.

## Campaign 0 cells

Program `FaultClass` values, with ecological Recovery-Bench aliases:

| Alias | FaultClass | Persistence |
| --- | --- | --- |
| permission-denied | persistent_signature_error | 1, 2 |
| not-found | persistent_schema_mismatch | 1, 2 |
| timeout | transient_network_timeout | 1, 2 |
| malformed-output | transient_http_5xx | 1, 2 |
| silent-wrong-result | silent_wrong_payload | 1, 2 |

Seed 42 is the calibration canary. Campaign 0 is calibration only.

## Ecological replay fallback

Cite MIT Recovery-Bench (`https://github.com/mcp-bench/recovery-bench`) as an
ecological replay fallback. Live-API replay drift is an explicit limitation; this
family does not vendor or replay that corpus.

## Materialization

```bash
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/materialize.py
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/ci_contract.py
```

Outputs land under gitignored `derived/harbor-tasks/mcp-recovery/<digest>/`.
Set `MCP_RECOVERY_WHEELHOUSE` to a hash-locked FastMCP wheel directory for
offline sidecar builds. Absent wheels, materialize is plan-only.
