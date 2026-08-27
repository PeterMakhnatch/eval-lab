# Recovery Subsystem (src/evallab/recovery/)

## Responsibilities
Owns state recovery bundles, filesystem and environment redacting, state restoration
certification (`StateCertificate`), and paired-trajectory recovery evaluation pilots.

## Core Invariants
1. Strict Redaction: Sensitive environment variables, keys, and tokens are sanitized
   prior to bundle hashing or serialization.
2. Verifiable State Certificates: Certification criteria evaluate exact archive hashes,
   manifest equivalence, package inventories, and process state.
3. Fail-Closed Restoration: Missing or mismatched state artifacts prevent certification.

## Testing & Verification
- Targeted unit tests: `pytest tests/test_recovery.py tests/test_recovery_certify.py`
