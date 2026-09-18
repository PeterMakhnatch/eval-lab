# System Architect final review: external lineage B1-B4

## Review target

- Branch: `fix/external-lineage-b1-b4-repair`
- Worktree: `/private/tmp/eval-lab-external-lineage-b1-b4-repair`
- Base: `721afd5245c92b5b7761a64dc704a9a258ee6279`
- Head: `9aa63c31` (full hash must be resolved by reviewer)
- Review exact range `721afd52..9aa63c31`, containing production commits `53e0e981` and `9aa63c31`.
- Prior review and blockers: `/tmp/system-architect-external-lineage-repair-final-review-721afd52.md`.
- Do not edit any worktree. This is an independent fail-closed authority review.

## Required independent checks

### B1 — packet-backed v1 is legacy read-only

- Every newly packet-backed promotion path, regardless of relabeling as local/synthetic/native/non-external, must reject `m049-v1` and require `m049-v2`.
- Legacy registered v1 packet reopen/audit must remain possible only through an explicit read-only path; it must not become a general bypass flag or reach new promotion/registration.
- Inspect every callsite of `certification_envelope_from_packet`, not only tests.

### B2 — candidate packet is the single source authority

- For packet-backed promotion, `candidate.json.source` must be a strict mapping and sole authority for non-empty typed `source_uri`, `source_ref`, `license`, allowed `provenance_zone`, and typed lineage/absence.
- Malformed or missing candidate fields must fail closed. No broad exception swallow, task/path/TOML fallback, local default, or alternate explicit argument may supply packet-owned authority.
- CLI/API inputs may assert exact equality only. Explicit mismatches or an explicit lineage against packet absence must refuse before registry write.
- Confirm `run_cli` exercises the real production boundary and that a failed promotion leaves no registry record.

### B3 — two parsed, distinct build attestations

- Both workbench inspection and registry reopen must parse each referenced build evidence file as the typed `ExternalImportBuildAttestationV1` contract.
- Exact record/artifact equality is required for build ID, canonical UTC timestamp, environment digest, separate toolchain digest, and final output package digest.
- Exactly two build IDs, paths, and evidence digests must be distinct. Build artifacts must not alias the transformation record, semantic-equivalence artifact, or one another by path or digest.
- Each artifact digest must match bytes, and each attested/record output digest must equal the exact registered package digest.
- Ensure the contract rejects unknown/malformed fields under the repository ContractModel policy.

### B4 — canonical UTC time authority

- `created_at` and every record/attestation `built_at` must reject date-only, naive, offset, lowercase/space/alias, and other noncanonical spellings; accepted values must be exact canonical `...Z` strings.
- Record and attestation build timestamps must compare exactly, not only as equivalent datetimes.
- Adversarially consider fractional-second spellings and parser normalization: approve only if the accepted grammar constitutes one defensible canonical representation rather than multiple equivalent spellings.

## Cross-cutting refusal checks

- Unsafe/symlink/nested-ignore protections and prior F1-F3 behavior remain intact.
- No generic external contract, fixture, or source packet is represented as LoCoMo certification, registry admission, canary authorization, or measurement readiness.
- No unrelated branch/root changes are part of the range.
- Tests must defend production behavior rather than only source text or helper plumbing.

## Evidence reported by implementer

- `261 passed` across `tests/test_task_workbench.py` and `tests/test_registry.py`.
- Ruff check and Ruff format-check clean across the five changed files.

Independently rerun the focused suites/negative controls and touched Ruff/format checks. Write a concise report to `/tmp/system-architect-external-lineage-b1-b4-final-review-9aa63c31.md` with exact verdict `APPROVE` or `BLOCK`, evidence, and concrete blockers. Page the parent with the verdict and report path. Do not commit or integrate.
