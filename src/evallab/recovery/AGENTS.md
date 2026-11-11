# Recovery

## Purpose
Reserved directory. The cleanup removes the recovery bundle, certification, and wrapper implementation.

## What lives here / entry points
- No current implementation or entry point.
- Restore removed source from `pre-deslop-2026-11-10`; see `research/ARCHIVE.md`.

## Invariants or rules
- Redact sensitive content before serialization and hashing.
- Preserve archive hashes, manifest/inventory identity, and process-state provenance.
- Missing or mismatched required evidence prevents certification.
- Never serialize raw API keys, credential state, or secrets into a recovery bundle.

## Tests or checks
Use the tests of the retained caller when changing its recovery behavior.

## What not to add here
Not a credential dump or a substitute authority for immutable source evidence.
