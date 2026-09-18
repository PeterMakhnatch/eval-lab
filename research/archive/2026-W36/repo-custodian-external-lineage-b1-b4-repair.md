# Repo Custodian — External Lineage B1–B4 Repair

## Target and workspace

Create a fresh isolated repair branch/worktree from exact blocked head `721afd5245c92b5b7761a64dc704a9a258ee6279`. Do not edit the dirty primary checkout, staged spine, old synth worktree, or prior external repair worktree in place. Return one focused forward commit for final Architect review.

Authoritative review: `/tmp/system-architect-external-lineage-repair-final-review-721afd52.md`.

Preserve all verified strengths: F3 nested-ignore parity and symlink refusal; canonical durable-reference checks; parsed v2 packet promotion; typed lineage; candidate/certification/registry/reopen equality; artifact reopening; and no waiver/model/LoCoMo activation. No real task registration.

## B1 — Close every recomputed v1 admission path

1. At the packet-backed/new-promotion boundary, reject **all newly promoted `m049-v1` certifications**, independent of source URI/ref, provenance zone, task family, license, caller flags, or any content-controlled classification.
2. Preserve v1 only for read/reopen/audit of already committed legacy records. Do not expose a new-registration compatibility flag or waiver.
3. Reproduce the full adversarial attack: start from valid external v2; remove lineage; relabel source/provenance/URI/ref/task-family as local/synthetic/native; change candidate/certification versions to v1; recompute every digest/ID; provide otherwise valid controls; assert promotion refuses before persistence.
4. Verify the attack cannot create/reopen/audit a new record, while a checked-in genuine legacy/native v1 record still loads/audits read-only.

## B2 — Candidate packet is the single source/license authority

1. Parse candidate source metadata before applying promotion defaults.
2. Derive registry source URI, source ref, license, provenance zone, and external lineage from the candidate packet.
3. Treat every explicit caller/CLI source URI/ref/license/provenance/lineage value only as an exact equality assertion. Reject mismatches before persistence.
4. Exercise the public parsed `run_cli(..., workspace=repo)` path for each mismatch; verify no registry record is created. Positive v2 promotion must survive reopen, certification verification, and audit with exact candidate authority.
5. Do not retain a second CLI construction/default path or allow an explicit license override.

## B3 — Typed, independently parsed two-build attestations

Define one typed canonical build-attestation artifact using existing schema/canonical-JSON conventions. Each referenced artifact must independently contain and bind at least:

- schema/version identity;
- exact build ID;
- canonical UTC build timestamp;
- environment identity digest;
- separately typed toolchain identity digest;
- exact final output package digest.

Then:

1. Extend the transformation-record build entry only as needed to bind the canonical evidence ref/digest and the same typed fields.
2. On workbench inspection, promotion, certification verification, registry reopen/reload, and audit, reopen each evidence artifact, verify its file digest, parse the typed attestation, and require exact field equality to the transformation record.
3. Require exactly two distinct build IDs, canonical resolved paths, and evidence digests. Different paths containing identical bytes/digests do not count as two builds.
4. Require both independently parsed attestations to name the exact final candidate/registry package digest. Reject output, build ID, environment, toolchain, timestamp, ref, digest, alias, parse, and reopening mismatches.
5. Replace opaque positive fixtures such as `{"build": 1, "status": "clean"}` with real typed attestations. Do not accept self-asserted record tuples without parsed evidence.

## B4 — Canonical UTC timestamps

1. Reuse or add one narrow canonical UTC timestamp validator/serializer at the typed schema boundary.
2. Accept only the repository-standard timezone-aware UTC `...Z` representation that is byte-equal to canonical serialization.
3. Reject date-only, naive, numeric-offset, alternate fractional/alias spellings, malformed values, and noncanonical equivalents.
4. Apply it to transformation `created_at`, record `built_at`, and parsed attestation timestamps; require exact record/artifact equality.
5. Add negative schema plus full workbench/promotion tests for date-only/naive/offset/alias and evidence timestamp mismatch.

## Public-path and adversarial closure

Tests must lock:

- parsed `run_cli` valid v2 candidate → certification → registry → reopen/reload → audit exact equality;
- all explicit URI/ref/license/provenance/lineage mismatches through parsed CLI;
- relabeled/recomputed v1 admission refusal across local/synthetic/native/task-family variants;
- genuine legacy v1 read/reopen/audit only;
- two distinct typed attestations positive path;
- identical evidence bytes/digests under different paths refusal;
- per-field build ID/output/environment/toolchain/timestamp mismatch refusal;
- canonical ref, alias, parse, digest, and reopen failures;
- prior F3 nested-ignore/included-file/symlink matrix.

Run focused `tests/test_task_workbench.py` and `tests/test_registry.py`, touched-file Ruff and format check, and independent relabeled-v1/license/identical-attestation probes. Do not run project-wide suites, register a real task, or run a model. Return branch/worktree/commit, exact diff, command counts, independent observations, and confirmation all other worktrees/spines were untouched.
