# Platform Builder — implement strict historical regeneration, dry-run only

## Exact base and workspace

Create a dedicated isolated worktree/branch from the current canonical spine:

- Source: `/private/tmp/eval-lab-staged-spine-integration`
- Exact base: `8fa4d4998b298ee4475eb55e30729f3ed8ef60d7`
- Suggested worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Suggested branch: `feat/strict-historical-regeneration`
- Analyst inventory/spec: `/tmp/analyst-strict-historical-regeneration-inventory.md`

Do not edit dirty root or canonical integration worktree. Do not copy/revive blocked prototype commits `25e15812`/`d709cf6d`. Do not spawn task subagents. Do not run a model/control, promote/register anything, apply regeneration to checked-in historical evidence, backfill projections, or touch unrelated branches.

## Ground truth to preserve

The checked-in historical cohort is evidence-determined:

- 170 promoted trials (`artifacts/manifest.json`);
- 152 with event journals;
- 130 with verifier `truth_digest` and events;
- 128 of those with final state;
- 2 truth-digest trials missing final state;
- 40 without truth digest, including 18 without events;
- 0 historically analysis-admissible trials.

Across the 130, design `seed`/`cell_id`/`arm`/`dose`/`representation`/opportunity fields occur in authoritative config+lock metadata 0 times. Sixty-one distinct task content digests have no checked-in registry/design-cell binding. No per-trial isolation probe/runtime-platform authority exists. Task names/paths are the only carrier and are prohibited as authority.

## Architecture

Reuse and extend the existing production convention:

- `src/evallab/storage/data_backfill.py` for typed dispositions, holds, deterministic ledger, fail-closed `ANALYSIS_READY iff no holds`, and apply mechanics;
- the existing `evallab data backfill` CLI family for writer/apply;
- `evallab traj c0-status` or the established read-only status surface for dry-run classification;
- existing canonical-json/domain-separated digest, atomic-create-or-verify, and atomic-write primitives;
- existing `NetworkIsolationStatus` and strict trial/admissibility types.

Do not create a second contract-derivation subsystem or independent refusal vocabulary. A small colocated type/helper is acceptable only if the existing backfill module cannot express the record cleanly.

## Required contract

### Field authority

May derive only:

- verifier truth digest from exact verifier result bytes;
- sorted trial-relative artifact inventory plus exact input digests;
- task content digest from exact lock metadata, named as content identity rather than a registered revision;
- canonical output/self digest.

Must type unavailable/hold, never infer:

- registry revision, family, version, construct;
- seed, cell, arm, dose, representation;
- every opportunity count;
- host platform, isolation enforcement, evidence class/admissibility.

Never derive any semantic field from task/trial/directory names, path substrings, labels, template defaults, opportunity defaults (`0` or `1`), fixture order, host filesystem prefix, or absence-as-negative. A bare task path is not `task_id` authority.

### Classification and output semantics

- Dry-run must deterministically classify all 170 promoted trials and write nothing.
- Emit a reason-coded disposition for every trial. The 130 truth-digest trials may be descriptive historical records only; none may be analysis-ready/admissible because registry/design/isolation authority is absent.
- Reconcile the 128/2 final-state split explicitly: missing final state is a typed completeness hold, never a downstream parser surprise. Whether the two incomplete descriptive records have a serializable non-admissible record or only a ledger disposition must follow existing backfill schema invariants; in either case they must not be silently dropped or reported loadable.
- The 40 without truth digest refuse derivation; the 18 without events carry both applicable reasons. Do not count a positive subset and hide additional holds.
- Distinguish “descriptive record emitted” from “analysis ready.” Expected ready/admissible count is exactly zero.

### CLI and determinism

Implement a public, unambiguous surface equivalent to:

```text
evallab data backfill contracts --dry-run
  --runs-root <path>
  --expect-promoted 170
  --expect-derivable 130
  --manifest-out <path>

evallab data backfill contracts --apply   # implemented but NOT run in this assignment
```

Exact spelling may follow the existing parser hierarchy, but:

- dry-run is default/non-mutating;
- apply is explicit and incompatible with dry-run;
- expected-count mismatch refuses before any write;
- unexpected/conflicting existing outputs refuse without overwrite;
- second apply is verify-only/idempotent;
- writes are atomic and limited to the exact output set;
- manifest binds code/schema version plus every authoritative input/output digest;
- canonical result bytes exclude wall-clock/non-authority ordering;
- identical inputs produce byte-identical dry-run/apply disposition bytes.

Do not claim a raw filesystem artifact path/digest is a registry identity.

## Tests

Add focused public tests that fail on plausible inference/transaction bugs:

1. family never inferred from name;
2. seed never parsed from `s2026`;
3. arm/dose/representation never substring matched;
4. opportunity counts remain unavailable, never 0/1 defaults;
5. task identity never falls back to name/path/trial ID;
6. path markers never establish platform/isolation;
7. missing final state becomes typed hold before loadability;
8. missing truth/events produce complete reason sets;
9. unexpected existing output leaves original bytes unchanged;
10. second apply is verify-only/idempotent;
11. dry-run predicts apply disposition bytes exactly;
12. representation-order drift is canonical/refuses rather than silently changes identity;
13. expected-count mismatch produces zero writes;
14. zero descriptive historical records are marked admissible/analysis-ready.

Preserve existing data-backfill, C0, trial-admissibility, isolation, CLI, and registry tests.

## Required execution/handoff

1. Implement only generator/types/CLI/tests in the isolated branch.
2. Run the focused tests, touched `ty`, Ruff check/format, compile, and `git diff --check`.
3. Run the new command in **dry-run only** against `research/evidence/runs` with exact expected counts. Capture deterministic manifest/report output under `/tmp`, not checked-in evidence.
4. Re-run dry-run and prove byte identity/checksum.
5. Commit cleanly.

Page `wH:p9` with exact branch/head/base, path/stat, public command, test/static results, observed 170/130/128/2/40/18 disposition counts, byte-identity digest, and explicit confirmation: zero writes to historical runs, zero admissible records, no model/control/subagent/root edit.
