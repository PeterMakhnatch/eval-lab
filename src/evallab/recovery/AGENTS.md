# Recovery Subsystem (src/evallab/recovery/)

## Purpose
State recovery bundles, filesystem/environment redacting, restoration
certification, and paired-trajectory recovery evaluation pilots.

## What lives here / entry points
- `bundle.py`: State bundle creation and snapshotting.
- `certify.py`: State restoration certification (`StateCertificate`).
- `wrapper.py`: Execution wrapping with automated recovery safeguards.

## Invariants or rules
1. Strict Redaction: Sensitive environment variables, keys, and tokens are sanitized
   prior to bundle hashing or serialization.
2. Verifiable State Certificates: Certification criteria evaluate exact archive hashes,
   manifest equivalence, package inventories, and process state.
3. Fail-Closed Restoration: Missing or mismatched state artifacts prevent certification.

## Tests or checks
- Targeted unit tests: `pytest tests/test_recovery_bundle.py tests/test_recovery_certify.py tests/test_recovery_wrapper.py tests/test_recovery_adversarial.py`

## What not to add here
Do not place unredacted environment states, raw trial dumps, or credential material here.
