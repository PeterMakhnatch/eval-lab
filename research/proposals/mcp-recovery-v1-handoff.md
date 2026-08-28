# MCP Recovery v1 Benchmark Vertical Handoff

## Overview

`mcp-recovery-v1` is the Campaign 0 Family C (certified MCP error recovery)
source-only Harbor family. It uses the shared FastMCP substrate, program
`CellFactorsC` / `FaultClass` contracts, StateCertificate-style before/after
digests, and paired clean/fault twins.

## Persistence semantics

Persistence N is the number of target-tool `fault_injected` events that occur
before the fault can clear.

- Transient timeout/5xx: after N injections the same write succeeds (auto-clear).
  C3 reward still requires `fallback_query` mutation. Blind identical retry that
  rides auto-clear scores 0 and sets `auto_clear=true`.
- Persistent signature/schema: the first N writes always fault. After N, success
  still requires `refresh_auth` / `fallback_query`. Early adaptation does not
  skip remaining injections.
- Silent-wrong: first N `read_record` calls return `corrupted_silent_val`.
  Diagnostic read is the required mutation.

Oracle parses FastMCP HTTP 200 `CallToolResult.isError` SSE frames, not JSON-RPC
error objects or HTTP 4xx.

## Layout

- `library/benchmarks/mcp-recovery-v1/` source family including `client.py`.
- `scripts/mcp_recovery/` thin generate/verify wrappers.
- `tests/test_mcp_recovery_v1.py` focused tests plus live FastMCP class/p1/p2
  ledger regressions.
- `.github/workflows/mcp-recovery.yml` 10-cell workbench certification.

## Evidence for later feature producers

Each materialized task emits:

- `/app/output/benchmark-events.jsonl` — monotone `event_index`, no timestamps
- `/app/output/final-state.json` — records + digest
- `tests/fixtures/fault_record.json` — verifier-only fault reference
- `tests/fixtures/family_spec.json` — Family C identity

Raw fields: injected-fault denominator, error class, detection, strategy
mutation, blind retry, auto-clear vs adaptation-required, invariant restoration,
numeric Harbor `reward`.

Feature computation is a later owner.

## Commands

```bash
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/ci_contract.py
PYTHONPATH=src pytest tests/test_mcp_recovery_v1.py
PYTHONPATH=src python3 scripts/mcp_recovery/generate.py --seed 42
```

## Controls

| Control | Expected reward |
| --- | --- |
| repair oracle | 1.0 |
| NOP | 0.0 |
| blind retry / fixed-policy retry | 0.0 (auto-clear is not C3) |
| wrong repair | 0.0 |

## Recovery-Bench

Ecological replay fallback only. Replay-drift limitation is preserved. Not vendored.
