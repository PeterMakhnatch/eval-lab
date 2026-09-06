# Analyst — read-only inventory for strict historical contract regeneration

## Exact target

Read-only analysis of the current canonical spine:

- Worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Branch/head: `analyst/synth-data-promotion-hardening @ 1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee`
- Isolation/trial admissibility integrated at `770488cf`
- Context operation ordering integrated at `1ecfc4a5`

Do not edit files, regenerate outputs, commit, integrate, or run a model/calibration/control.

## Goal

Produce an exact implementation map for the pending strict historical regeneration. Existing historical engineering counts (170 / 130 / 128) are not authority claims. The future generator may derive only from authoritative task registry/runtime identity, exact raw result/lock/provenance, network isolation, analysis sidecar, trial-admissibility, and outcome-authority evidence. Unknown history must remain typed unavailable. No labels, task paths, opportunity counts, ordering, isolation, or outcome eligibility may be guessed from names/directories/defaults.

## Required inventory

1. Locate every checked-in historical derived artifact, manifest, sidecar, fixture, report, or projection participating in the 170 / 130 / 128 counts. Report exact paths, schemas, row counts, digests, generators, and consumers.
2. Locate all existing generator/audit commands and functions. Identify which are production-supported versus hand-authored/one-off.
3. Build a field-level authority table for every output column:
   - exact authoritative source path/schema/field;
   - required digest/binding chain;
   - deterministic transformation;
   - typed unavailable value/reason when source or binding is absent;
   - currently guessed/defaulted/inferred behavior that must be deleted.
4. Classify every historical input cohort by whether the integrated strict authority is actually present:
   - exact admissible authority available;
   - descriptive/calibration-only authority available;
   - legacy raw evidence present but strict authority absent;
   - missing/ambiguous/corrupt.
5. Reproduce the existing 170 / 130 / 128 counts using the current audit command only to explain their meaning. Do not relabel them as valid. State which counts are files, rows, engineering candidates, validated bindings, or unavailable.
6. Define a deterministic non-mutating dry-run inventory and an idempotent regeneration command contract. Regeneration must refuse unexpected existing extras, avoid overwriting unrelated artifacts, use atomic writes, emit a manifest with exact input/output digests and code/schema version, and be repeatable byte-for-byte where timestamps are not authority.
7. Define focused tests that fail on plausible historical inference bugs: path/name label inference, fallback task identity, guessed operation order/opportunities, legacy controls treated as admissible, missing runtime/isolation binding, stale result/analysis snapshots, duplicate/ambiguous trials, and representation-order drift.
8. Identify exact files a Platform Builder would need to change/create. Prefer repairing existing generator code; do not propose a second convention.

## Output

Write `/tmp/analyst-strict-historical-regeneration-inventory.md` with:

- exact path/count/digest inventory;
- field authority matrix;
- current unsafe inference catalog;
- deterministic generator CLI/API specification;
- focused test plan;
- recommended implementation scope and dependency order;
- explicit blockers that cannot be resolved from checked-in authority.

Page `wH:p9` with the report path and a concise statement of what can be regenerated versus what must remain unavailable. Confirm no files changed and no model/control run.
