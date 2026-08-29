# MCP Error Recovery Benchmark Family (v1)

Source-only certified error-recovery family for Campaign 0 calibration. Tasks are
Harbor packages with a real FastMCP 3.4.7 streamable-HTTP sidecar, matched
clean/fault twins, authenticated AES-256-GCM sealed evidence envelopes, and
verifier-only truth.

## Principles

- **Source-Only & Zero-Vendoring**: No generated task corpus in Git, no vendored
  Recovery-Bench repo. Tasks are materialized into digest-addressed paths under
  `derived/harbor-tasks/mcp-recovery/<digest>/`.
- **Verifier-Only Truth & Opaque Agent Surface**: Agent container sees zero
  fault taxonomy tokens, secret keys, or gold states in instruction or environment.
  Main container mounts evidence volume read-only (`:ro`) and sees only the
  sealed ciphertext envelope (`/app/output/sealed-evidence.json`).
- **Authenticated AES-256-GCM Envelope**: Sidecar records private event journal
  and atomically writes AES-256-GCM ciphertext bound with AAD (`schema_version`,
  `task_id`, `fault_id`, `persistence`, `sequence`). Verifier holds the per-cell
  key in private fixtures, decrypts, and verifies exact N injection counts.
- **Causal Recovery vs. Blind Retries**: Transient auto-clear requires causal
  strategy mutation (`refresh_auth` / `fallback_query`) prior to the first valid
  post-fault write and confirmed read; blind retry yields `auto_clear=True, reward=0.0`.
- **Matched Clean Twins**: Every fault cell has a paired clean twin holding seed,
  task, tools, compose, Dockerfile, and requirements constant with zero faults.

## Campaign 0 cells (20 Tasks: 10 Fault + 10 Clean Twins)

| Alias | FaultClass | Persistence | Matched Twin Arm |
| --- | --- | --- | --- |
| permission-denied | persistent_signature_error | 1, 2 | clean_twin (p=0), fault (p=1,2) |
| not-found | persistent_schema_mismatch | 1, 2 | clean_twin (p=0), fault (p=1,2) |
| timeout | transient_network_timeout | 1, 2 | clean_twin (p=0), fault (p=1,2) |
| malformed-output | transient_http_5xx | 1, 2 | clean_twin (p=0), fault (p=1,2) |
| silent-wrong-result | silent_wrong_payload | 1, 2 | clean_twin (p=0), fault (p=1,2) |

Seed 42 is the calibration canary. Campaign 0 is calibration only.

## Ecological replay fallback

Cite MIT Recovery-Bench (`https://github.com/mcp-bench/recovery-bench`) as an
ecological replay fallback. Live-API replay drift is an explicit limitation; this
family does not vendor or replay that corpus.

## Materialization & CI

```bash
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/materialize.py
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/ci_contract.py
```

Outputs land under gitignored `derived/harbor-tasks/mcp-recovery/<digest>/`.
Production materialization uses `MCP_RECOVERY_WHEELHOUSE` and `MCP_RECOVERY_RESOLVER_PROVENANCE`.
Absent wheels, materialize operates in plan-only mode.
