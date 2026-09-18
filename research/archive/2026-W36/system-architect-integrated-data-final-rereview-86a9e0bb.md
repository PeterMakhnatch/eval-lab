# System Architect brief: final integrated data-plane rereview at `86a9e0bb`

## Assignment

Perform a fresh, adversarial, read-only architectural and behavioral rereview of the exact clean head below. Do not trust the builder's delivery summary or prior passing tests; inspect the implementation and construct the strongest practical negative controls.

- Worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch: `analyst/synth-data-promotion-hardening`
- Exact head: `86a9e0bb`
- Repaired source commit: `2d54fa86`
- Documentation regeneration: `86a9e0bb`
- Previous blocked review: `/tmp/system-architect-integrated-data-repair-rereview-6843da00.md`
- Previous reviewed head: `6843da002ddebdcd2d2c450633a5f00da2554a2d`
- Original CAS approval: `/tmp/system-architect-run-cas-analyst-final-rereview-078cf287.md`
- Original projection approval: `/tmp/system-architect-projection-settlement-rereview-26509bef.md`

Write no repository files. Deliver a report to `/tmp/system-architect-integrated-data-final-rereview-86a9e0bb.md` and page `wH:p9` with an exact `APPROVE` or `BLOCK` verdict, the report path, and concise causal evidence.

## Required review surface

### B1 — frozen deterministic quality identity

Verify that `AnalysisRequest` freezes every input and decision that can affect trajectory-quality admission, including file presence/absence and byte identity for all evaluator inputs. Stage a request, mutate/add/remove a quality-relevant file, and prove admission fails before any model call. Confirm `WARN` is admitted only when the frozen report is still analysis-ready and every frozen identity still matches. Confirm no mutable Parquet/view quality island or interpretation-time `read_parquet` path was reintroduced.

### B2 — atomic CAS-to-durable bootstrap publication

Inspect the OS-specific no-replace implementation, ctypes signatures, errno handling, platform behavior, path/symlink safety, full-tree validation, durability calls, and cleanup. Exercise injected mid-materialization/validation failure, pre-existing destination, symlink adversary, and two concurrent publishers. A reader must observe either no authoritative destination or one complete locator-authenticated tree—never a partial tree; the winner must never be overwritten; staging siblings must be cleaned. There must be no fallback to overwrite-capable ordinary rename.

### B3 — typed canonical publication authority

Verify `CanonicalPublicationBinding` is derived from exact request/terminal-event/locator/spec authority and that callers cannot relabel an arbitrary path. There must be no scan, glob, trial-name fallback, CAS-record-id/job-UUID conflation, environment/default namespace, or caller-selectable arbitrary file path. The derived path must be exactly `<publication_root>/<binding.job_name>/<trial.name>`. Test unrelated same-named copies, symlinks in any path component, pre-call mutation, and mutation during the analyzer call. Binding/mismatch/drift failures must be typed and must prevent authority minting; all pre-call failures must make zero model calls. Confirm post-analyzer identity is rechecked before finalization.

### B4 — exact unique terminal-event binding

Verify production/test bootstrap code selects exactly one terminal event by event type, job name, spec id, CAS kind `job`, and every available attempt/request identity. Materialize only through the exact `EvidenceLocator`; validate loaded job UUID and embedded provenance independently. Duplicate or near-match events must fail closed, not select the last event.

### B5 — explicit CLI ingest store contract

Verify `evallab ingest` requires explicit `--store` with no environment/default fallback. Exercise the real CLI path: archive raw evidence, capture the locator, delete the raw source, then prove settlement/projection succeeds only from CAS materialization and the manifest remains locator-bound. A mocked plumbing-only test is not sufficient evidence.

### B6 — invalid interpretation separation

Verify invalid analysis sidecars are persisted and returned for diagnostics without minting admissibility authority, while an explicit strict finalization attempt fails closed with the correct typed cause. Confirm the existing invalid-citation worker path does not crash.

### Tamper regression

Verify the bootstrap tamper negative control corrupts a valid final-state payload while leaving unrelated metadata valid and asserts this exact cause chain:

- outer: `TaskControlEvidenceError`
- cause: `TrialAdmissibilityError`
- reason: `trial_admissibility_invalid:source-digest-drift`

It must not pass on an incidental missing-control or malformed-result error.

## Preservation requirements

Confirm the repaired head preserves the approved CAS/projection semantics and historical evidence:

- `storage/settlement.py`, `storage/reconciliation.py`, `storage/attach.py`, `evidence/parquet_io.py`, and approved interpretation settlement semantics remain equivalent to approved projection source `26509bef00ac0bc19666e3436c1bdc04c357f878`.
- `storage/data_backfill.py`, `historical_git_snapshot.py`, and checked historical evidence remain byte-identical to canonical parent `09b839770e53137dd09a7131776120d462a67761`.
- Pinned historical identities remain unchanged: snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`, plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`, manifest SHA `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`, counts `170/152/130/128/2/40/18`, ready/admissible `0/0`, and 1,690 explicit-null design fields.
- Historical outputs remain descriptive only, never certified controls, measurements, or model results.

## Verification expectations

Run focused adversarial tests and the relevant exact matrices at this clean head, plus Ruff lint/format, governance, repomap/docindex, and `ty` if available. Report exact pass/fail/skip counts and any unavailable tool honestly. Do not modify the repository to make checks pass.

## Verdict standard

`APPROVE` only if all B1–B6 and the tamper regression are causally closed, the integration has no competing authority path, and all preservation requirements hold. Any partial atomicity, inferred identity, mutable evidence path, weak negative control, or post-call drift gap is `BLOCK` with the exact causal defect and a minimal repair contract.
