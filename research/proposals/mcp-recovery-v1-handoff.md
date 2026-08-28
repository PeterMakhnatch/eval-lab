# MCP Recovery v1 Benchmark Vertical Handoff

## Overview
`mcp-recovery-v1` implements a source-only, zero-vendoring benchmark vertical measuring autonomous LLM agent error detection, strategy mutation, and invariant recovery over Model Context Protocol (MCP) tool executions.

## Components & File Layout
- `library/benchmarks/mcp-recovery-v1/`:
  - `sources.json`: Source provenance, MIT license attribution, calibration seed matrix, and benchmark parameters.
  - `source.py`: Manifest loading and canonical SHA-256 source digest calculation.
  - `state.py`: `DatabaseState` model, StateCertificate representation, and cryptographic digest verification.
  - `faults.py`: `FaultClass` enum (5 fault modes: `permission_denied`, `not_found`, `timeout`, `malformed_output`, `silent_wrong_result`), `FaultSpec`, and `FaultController` with persistence tracking.
  - `runtime.py`: Streamable-HTTP MCP JSON-RPC server runtime supporting tool registration, fault injection, clean/fault twin execution, and canonical event logging (`/app/evidence/benchmark-events.jsonl`).
  - `verifier.py`: Deterministic verifier checking monotonic event indexing, fault injection evidence, strategy adaptation, and state invariant restoration against expected twin digests.
  - `templates.py`: Oracle solutions, NOP baselines, blind retry controls, and wrong-repair mutants.
  - `materializer.py` / `materialize.py`: Materializes complete Harbor task packages into digest-addressed paths (`derived/harbor-tasks/mcp-recovery/<digest>/...`).
  - `ci_contract.py`: Deterministic regeneration check, untracked corpus check, oracle scoring (1.0), NOP scoring (0.0), and mutant scoring (0.0).
  - `contract.py`: Programmatic contract exporter for trajectory and campaign orchestration.
- `scripts/mcp_recovery/`:
  - `generate.py`: Materialization CLI wrapper.
  - `verify.py`: Verification CLI wrapper.
- `.github/workflows/mcp-recovery.yml`: PR and push workflow for CI contract and focused pytest checks.
- `tests/test_mcp_recovery_v1.py`: Unit tests for contracts, database state certificates, fault controllers, MCP server runtime, and verifiers.

## Calibration Matrix (Campaign 0)
- **Fault Modes**: `permission_denied`, `not_found`, `timeout`, `malformed_output`, `silent_wrong_result`
- **Persistence**: `1` (transient auto-clear / single-hit), `2` (recurrent, requires parameter mutation or multi-step recovery)
- **Seeds**: `42`, `101`, `2024`

## Verification Commands (To run during integration / CI)
```bash
# Run CI contract
python3 library/benchmarks/mcp-recovery-v1/ci_contract.py

# Run focused tests
pytest tests/test_mcp_recovery_v1.py

# Materialize sample canary task
python3 scripts/mcp_recovery/generate.py --seed 42

# Verify task
python3 scripts/mcp_recovery/verify.py derived/harbor-tasks/mcp-recovery/*/mcp-recovery-seed42
```
