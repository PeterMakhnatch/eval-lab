Status: complete
Last: Consolidated all contract models into schemas.py per platform-architecture v2 §2.1, deleted contracts.py, preserved golden schema freeze, updated tests and docs
Next: none (E00 consolidation complete, unblocks downstream epics)
Blockers: none

## Summary of Changes

1. **Model Consolidation**: Moved `Suite`, `AnalysisRecord`, `ObservationRecord`, `CalibrationRecord`, `Verdict`, `EvidenceCitation`, `ConfidenceClaim`, `CriterionAgreement`, along with `ULID_PATTERN`, `SHA256_DIGEST_PATTERN`, `_validate_ulid`, and `_validate_sha256_digest` into `src/evallab/schemas.py`.
2. **Reconciliation**: All models inherit from `schemas.ContractModel`. No name collisions were found with existing models.
3. **Module Deletion**: Deleted `src/evallab/contracts.py` and migrated all imports/references across `tests/test_contracts.py` and documentation to `evallab.schemas`.
4. **Golden Schema Freeze**: Byte-for-byte schema equality verified for all models against `tests/fixtures/contracts/*.json` with no diff or regeneration needed.
5. **Documentation & Repo Maps**: Updated `docs/contracts.md`, regenerated `docs/repo-map.md` and `docs/INDEX.md`.
6. **Validation**:
   - `uv run pytest tests/test_contracts.py`: 14 passed
   - `uv run pytest`: 1065 passed, 1 skipped, 1 xfailed
   - `uv run ruff check .`: clean
   - `uvx ty@0.0.71 check src/`: 28 diagnostics (<= 28)
   - `repomap check` and `docindex check`: clean
