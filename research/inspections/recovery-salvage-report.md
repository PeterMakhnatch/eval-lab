# Recovery-Bench Salvage: Certified State Materialization & Evaluation Protocol

**Status:** Complete  
**Branch:** `feature/recovery-bench-salvage`  
**Owner:** ScaffoldWriter & PilotExecutor  
**Date:** 2026-08-23  

---

## 1. Executive Verdict & Core Decisions

The upstream Recovery-Bench strategy of *best-effort command replay* is fundamentally fragile and scientifically non-reproducible (as proven by the `/tmp/recovery-replay-audit/` findings where 11/20 commands failed and manifest generation timed out).

We have replaced command re-execution with a **Certified Inherited-State Materialization Architecture**:

1. **File-Only Tasks (Strategy B):** Certified via deterministic rootfs layer archives + canonical SHA-256 manifest hashing. 100% reproducible and fast.
2. **Package & Configuration Tasks (Strategy D):** Certified via rootfs layer archive + exact package/dependency inventories (Python, OS, npm) + environment variable allowlist/redaction.
3. **Service & Background Process Tasks (Strategy D + Honest Blocker):** When in-memory process or socket state cannot be portably captured across architectures without CRIU kernel checkpointing, the system emits an explicit **`UNKNOWN`** certificate. It **never claims equivalence without evidence**.

---

## 2. Implemented Architecture & Contracts

### `src/evallab/recovery/` Modules:
- **`bundle.py` (`RecoveryStateBundle`):**
  - Task, base image, and verifier digests.
  - Initial failed ATIF path, hash, and exact step cutoff.
  - Full structured command ledger (commands, exit codes, output hashes).
  - Canonical filesystem manifest sorted deterministically by path.
  - Secret-redacted environment configuration.
  - Content-addressed `bundle_digest`.
- **`certify.py` (`StateCertificate`):**
  - Emits `PASS`, `FAIL`, or `UNKNOWN` across four independent criteria: `archive_integrity`, `filesystem_equivalence`, `package_environment`, and `process_and_service_state`.
  - Enforces double-materialization **idempotency verification**.
- **`wrapper.py` (`PairedTrajectoryOutcome`):**
  - Executes recovery agents starting from certified inherited states.
  - Separates initial trial costs/tokens from recovery costs/tokens.
  - Evaluates message modes as an experimental factor: `full`, `summary`, `none`.
  - Native final task verifier remains outcome truth.
- **`pilot.py` (`BoundedRecoveryPilotReport`):**
  - Executes the 3-class bounded pilot.
  - Certifies `file-only` and `package-config` lanes.
  - Issues formal blocker for `service-process`.
- **`facts.py` (`project_recovery_facts`):**
  - Projects paired recovery records directly to columnar Parquet for analytical querying with DuckDB.

---

## 3. Test & Verification Evidence

All 14 unit, integration, and adversarial tests pass under `pytest` with zero Ruff warnings:

```text
tests/test_recovery_adversarial.py .....                                 [ 35%]
tests/test_recovery_bundle.py ..                                         [ 50%]
tests/test_recovery_certify.py ....                                      [ 78%]
tests/test_recovery_wrapper.py ...                                       [100%]

============================== 14 passed in 0.24s ===============================
```

### Verified Adversarial Cases:
- `test_adversarial_missing_dependency_fails`: Flags missing Python and OS packages.
- `test_adversarial_undeclared_or_unrestorable_process_is_unknown`: Guarantees unverified process states are marked `UNKNOWN` rather than `PASS`.
- `test_adversarial_failed_service_restart`: Flags failed daemon rehydration.
- `test_adversarial_idempotency_divergence_fails_certification`: Catches non-deterministic filesystem divergence on repeated restore.
- `test_adversarial_wrapper_refuses_uncertified_bundle`: Strictly refuses to run recovery agents on uncertified (`FAIL`) states.

---

## 4. Upstream Patch Proposal for Recovery-Bench

To make upstream Recovery-Bench reproducible across heterogeneous platforms:
1. **Deprecate Bash Replay:** Replace `replay_commands.py` with an OCI layer or tarball state artifact emitted at the exact failure checkpoint.
2. **Standardize State Manifests:** Require each task failure checkpoint to export a canonical SHA-256 manifest and package inventory.
3. **Isolate Process Bounds:** Restrict recovery benchmarks to file and configuration restoration unless the harness provides a verified kernel checkpoint runtime (e.g. Linux x86 CRIU).
