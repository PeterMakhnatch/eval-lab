# System Architect — review strict historical generator dry-run

## Exact candidate

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ ba2ef5c8fb837499cfe05de9c17630109a564a5f`
- Exact base: `8fa4d4998b298ee4475eb55e30729f3ed8ef60d7`
- Canonical spine has since advanced only through unrelated five-path memory landing `9768ad60`.
- Analyst authority inventory: `/tmp/analyst-strict-historical-regeneration-inventory.md`
- Candidate dry-run manifest: `/tmp/platform-builder-strict-historical-regeneration-dry-run.json`

Read-only. Do not edit/rebase/integrate/apply regeneration, run models/controls, or spawn subagents.

## Candidate surface

Three paths, +977/-2:

1. `src/evallab/storage/data_backfill.py`
2. `src/evallab/cli.py`
3. `tests/test_contract_derivation_strict.py`

Public dry-run reported 170 promoted, 152 events, 130 truth/descriptive, 128 final-state complete, 2 final-state missing, 40 truth missing, 18 truth+events missing, and exactly zero analysis-ready/admissible records. Historical evidence was not mutated.

## Required review

### Authority and semantics

- Every semantic field is bound to exact authoritative bytes; no task/trial/path/label substring, regex, template, host path, filename, iteration order, or default supplies family/version/construct/seed/cell/arm/dose/representation/opportunities/platform/isolation/admissibility.
- A task content digest is named only as content identity; it never becomes a registry revision or task admission identity.
- Artifact paths are a sorted descriptive inventory only and cannot grant semantics.
- Missing truth, events, final state, registry/design binding, runtime/platform/isolation authority produce complete deterministic typed reason sets; no positive subset hides additional holds.
- The 130 outputs are explicitly descriptive/non-admissible; the 128/2 split is represented without claiming the two incomplete records loadable; `ANALYSIS_READY`/admissible remains exactly zero.
- No `not_enforced`, opportunity `0/1`, guessed Darwin/Linux, or inferred benchmark family can be constructed through public CLI/API.

### Integration with existing backfill convention

- Extension genuinely reuses existing `data_backfill` disposition/ledger/fail-closed patterns rather than creating a shadow authority.
- Existing `ANALYSIS_READY iff no hold reasons` invariant is preserved. If historical descriptive records use a distinct schema/disposition, its relationship to the existing ledger is explicit and cannot be confused with ready backfill.
- CLI is unambiguous: default dry-run, explicit apply, incompatible modes rejected, expected-count preflight before writes.
- `/tmp --manifest-out` during dry-run is an explicit report artifact, not mutation of historical evidence; absent `--manifest-out` behavior is clear.

### Determinism and transaction safety

- Manifest/self digests are domain-separated and bind schema/code version plus exact input/output bytes without circular/self-inconsistent identity.
- Repeated dry-run bytes are representation-order stable and wall-clock independent.
- Apply implementation, though not run on historical evidence yet, is tested in isolated fixtures for atomic create-or-verify, exact output allowlist, symlink/path traversal refusal, expected-count zero-write preflight, conflict preservation, second-apply verify-only idempotence, and dry-run/apply disposition equality.
- Unexpected extras/conflicting bytes never overwrite or delete; partial failure cannot leave a misleading successful manifest.

### Regression/scope

- No existing backfill/C0/trial-admissibility/isolation/CLI test or public behavior is weakened.
- The new 15 tests bind observable mechanisms rather than names/source text.
- +977 lines are justified and not a duplicated general framework; flag weightless abstractions or schema vocabulary drift.

## Verification/output

Independently run the 15 new tests plus existing data-backfill/C0/trial-admissibility/isolation/CLI focused matrix, touched `ty`, Ruff/format, compile, diff check, and two dry-runs with byte comparison. Inspect exact manifest counts/reasons/digests and confirm zero historical outputs.

Write `/tmp/system-architect-strict-historical-generator-review-ba2ef5c8.md` beginning `APPROVE` or `BLOCK`, with exact blockers/evidence. Page `wH:p9`; confirm no edit/apply/model/control/subagent.
