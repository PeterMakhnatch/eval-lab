---
source_type: internal
---

# PR #359 shared artifact-authority migration review

Exact head: `38b9a2c51bc1e7a9a1271eba3e024dfa1cb6489c`
Base: `origin/integrate/spine-batch1@b53e48cd78a43688157c1b2a658a78dce780d9aa`
PR: https://github.com/PeterMakhnatch/eval-lab/pull/359

## Review request

Please review this exact head and page back explicit PASS/BLOCK with evidence. The prior `3f085b5a` reviews remain valuable, but this review is specifically for the approved shared `evallab.artifact_authority` migration.

## Migration

- Training export now calls shared `verify_artifact(..., minimum_level="bytes-verified")` for trajectory bytes, lineage bytes, and the canonical `TrialAdmissibilityV1` record inside the authenticated evidence archive.
- Trajectory verification binds the shared authority to the causal admissibility receipt with `artifact_kind="trajectory"`.
- The admissibility record is re-opened via `reverify_authority`, parsed, canonical-byte checked, and compared with the normalized authority object.
- Every accepted example source binding and manifest source ref embeds all three complete `ArtifactAuthority` receipts, not caller-supplied booleans or opaque claims.
- Contract validators bind every embedded authority to exact artifact path/digest, archive record/content anchors, verifier implementation digest, bytes-verified level, and trajectory admissibility digest/trial identity.
- Authority verifier identity and authority digests therefore participate in example IDs and manifest identity.
- Output still rejects all pre-existing destinations and publishes via fsynced staging plus one atomic directory rename.

## Diff scope

Net PR diff against base contains only:
- `src/evallab/training_export.py`
- `tests/test_training_export.py`

## Verification

- `PYTHONPATH=src uv run pytest -q tests/test_artifact_authority.py tests/test_training_export.py` -> 20 passed
- `uv run ruff check src/evallab/training_export.py tests/test_training_export.py` -> passed
- `git diff --check origin/integrate/spine-batch1...HEAD` -> passed
- Tests explicitly rehydrate all three emitted authorities with `reverify_authority` and reject a recomputed-but-contradictory embedded authority.

Schema bundle: `/private/tmp/pr359-training-export-schema-38b9a2c5.json`
Schema SHA-256: `d243c675fe20d4fd5eedfef6d54524dc3890872547bf74c115672313bb7615df`
