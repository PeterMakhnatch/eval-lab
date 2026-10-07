# Recovery

## Purpose
Recovery bundles, inventory/manifests, deterministic certification, and their wrapper.

## What lives here / entry points
- `bundle.py`: State bundle creation and snapshotting.
- `certify.py`: State restoration certification (`StateCertificate`).
- `wrapper.py`: Execution wrapping with automated recovery safeguards.

## Invariants or rules
- Redact sensitive content before serialization and hashing.
- Preserve archive hashes, manifest/inventory identity, and process-state provenance.
- Missing or mismatched required evidence prevents certification.
- Never serialize raw API keys, credential state, or secrets into a recovery bundle.

## Tests or checks
The assigned verifier uses existing bundle, inventory, certification, and wrapper tests for the touched contract.

## What not to add here
Not a credential dump or a substitute authority for immutable source evidence.
