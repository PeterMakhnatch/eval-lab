# MCP Recovery v1 Benchmark Vertical Handoff

## Overview

`mcp-recovery-v1` is the Campaign 0 Family C (certified MCP error recovery)
source-only Harbor family. It uses the shared FastMCP substrate, program
`CellFactorsC` / `FaultClass` contracts, StateCertificate-style before/after
digests, and paired clean/fault twins.

## Layout

- `library/benchmarks/mcp-recovery-v1/` source family (materializer, runtime,
  verifier, templates, contract, CI contract).
- `scripts/mcp_recovery/` thin generate/verify wrappers.
- `tests/test_mcp_recovery_v1.py` focused tests.
- `.github/workflows/mcp-recovery.yml` 10-cell workbench certification.

## Evidence for later feature producers

Each materialized task emits:

- `/app/output/benchmark-events.jsonl` — monotone `event_index`, no timestamps
- `/app/output/final-state.json` — records + digest
- `tests/fixtures/fault_record.json` — verifier-only fault reference
- `tests/fixtures/family_spec.json` — Family C identity

Raw fields available without prose interpretation: injected-fault denominator,
error class, detection evidence, strategy mutation (`refresh_auth` /
`fallback_query` / `read_record`), blind retry, invariant restoration, numeric
Harbor `reward`.

Feature computation is a later owner. Do not add derived rates here.

## Commands (integration / CI; not run in this PR by policy except focused tests)

```bash
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/ci_contract.py
PYTHONPATH=src pytest tests/test_mcp_recovery_v1.py
PYTHONPATH=src python3 scripts/mcp_recovery/generate.py --seed 42
```

Populate `/tmp/fastmcp3_wheelhouse` from `FASTMCP_SIDECAR_REQUIREMENTS_TXT`
before production materialize.

## Controls

| Control | Expected reward |
| --- | --- |
| repair oracle | 1.0 |
| NOP | 0.0 |
| blind retry (permanent / no adaptation) | 0.0 |
| wrong repair | 0.0 |

## Recovery-Bench

Ecological replay fallback only. Replay-drift limitation is preserved. Not vendored.
