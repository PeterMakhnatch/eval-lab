# MCP Recovery v1 Benchmark Vertical Handoff

## Overview

`mcp-recovery-v1` implements Campaign 0 Family C (certified MCP error recovery)
as a source-only Harbor benchmark family. It features real streamable-HTTP FastMCP
sidecars, matched clean/fault twin pairs (20 tasks total), authenticated AES-256-GCM
sealed evidence envelopes, and strict verifier-only truth isolation.

## Architectural Properties

1. **Authenticated AES-256-GCM Evidence Envelope**:
   - The sidecar maintains private runtime state in `/app/.recovery-runtime-state.json`.
   - After each tool call, the sidecar seals the state journal into an AES-256-GCM
     ciphertext envelope written atomically to `/app/output/sealed-evidence.json`.
   - AAD binds `schema_version`, `task_id`, `fault_id`, `persistence`, and `sequence`.
   - The 32-byte secret key is provisioned via `RuntimeAsset("secret_key.txt", ...)`
     into the sidecar and stored in verifier-only `tests/fixtures/secret_key.txt`.
   - The main (agent) container mounts `evidence-volume:/app/output:ro` read-only,
     observing only opaque base64 ciphertext and nonce with zero access to keys or plaintext.
   - Harbor copies `/app/output/sealed-evidence.json` directly from the declared artifact path;
     no inert `[[verifier.collect]]` hooks are used.

2. **Matched Clean Twins**:
   - Each of the 5 fault classes × 2 persistence levels (10 fault cells) has a matched
     clean twin holding seed, task definition, tools, Dockerfile, base image, and wheelhouse
     identical except fault injection (`persistence = 0`).
   - Causal contrast pairs are linked via `base_task_pair_id` and `twin_task_id`.

3. **Causal Recovery Scoring & Exact Opportunity Ledgers**:
   - Fault cells require exactly N injected opportunities (`len(injections) == expected_persistence`).
   - Clean twin cells require exactly 0 injected opportunities (`len(injections) == 0`).
   - Causal mutation: at least one mutation tool (`refresh_auth` or `fallback_query`) must be
     executed *after* the first fault and *before* the first successful recovery write.
   - Confirmation: a successful `write_record` followed by a confirmed matching `read_record`.
   - Injected silent-wrong reads return stale data and are classified as `silent_corruption`
     (invalid confirmation).
   - Blind retries without causal mutation on transient faults result in `auto_clear = True`
     and `reward = 0.0`.

## File Layout

- `library/benchmarks/mcp-recovery-v1/`:
  - `envelope.py`: AES-256-GCM encryption/decryption with pure-Python zero-dependency fallback.
  - `materializer.py`: Task generator emitting FastMCP sidecars, RuntimeAssets, and verifier containers.
  - `verifier.py`: Verifier-only envelope decryptor and causal recovery evaluator.
  - `templates.py`: In-process controls (oracle, NOP, blind retry, wrong repair) across all 20 cells.
  - `ci_contract.py`: Automated 20-cell verification suite (10 fault + 10 clean twins).
  - `contract.py`: Benchmark program contract and Campaign 0 cell matrix.
  - `client.py`: Streamable-HTTP client session with FastMCP `isError` parsing.
  - `sources.json`: Manifest and provenance pins.
- `scripts/mcp_recovery/`:
  - `generate.py`: Materialization CLI wrapper.
  - `verify.py`: Verification CLI wrapper.
- `tests/test_mcp_recovery_v1.py`:
  - AES-GCM encryption/decryption and AAD tamper rejection.
  - One-delta clean twin matching across all cells.
  - Security boundary and zero truth leakage on agent-visible surfaces.
  - Live FastMCP causal recovery, blind-retry gate, and multi-repair strategy verification.
  - Full workbench static inspection for all 20 cells.
- `.github/workflows/mcp-recovery.yml`: PR and push certification workflow.

## Verification Commands

```bash
# Run CI contract across all 20 cells
PYTHONPATH=src python3 library/benchmarks/mcp-recovery-v1/ci_contract.py

# Run comprehensive test suite
PYTHONPATH=src pytest tests/test_mcp_recovery_v1.py

# Run linters and typecheckers
uv run --no-sync ruff check library/benchmarks/mcp-recovery-v1 scripts/mcp_recovery tests/test_mcp_recovery_v1.py
uvx ty@0.0.71 check library/benchmarks/mcp-recovery-v1 scripts/mcp_recovery --extra-search-path library/benchmarks/mcp-recovery-v1 --extra-search-path src --output-format=concise
```
