# Engineer Data — repair final projection attach blockers

## Exact state

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/projection-settlement-ledger`
- Branch/head: `feat/projection-settlement-ledger @ bdb33e5810e789a1aceb2d4a27c67e4f368ee205`
- Approved CAS base: `078cf287b05d80bb88bd20d87b112b33b250f688`
- Architect report: `/tmp/system-architect-projection-settlement-review-bdb33e58.md`
- Verdict: BLOCK on P1/P2 plus static cleanliness. Authority split, ledger, canonical root, atomic publication, reconciliation, test preservation otherwise pass.

Do not rebase/integrate/backfill, call a model, mutate live PostgreSQL, or broaden scope.

## P1 — non-ready manifests must type, never crash

- Remove nonexistent `manifest.failure_reason` access.
- Derive a deterministic diagnostic from actual authority: settlement state plus the final canonical event reason and/or exact table settlement reasons where applicable.
- Every valid active non-ready state (`discovered`, `source_validated`, `cas_committed`, `cataloged`, `projecting`) must attach as typed per-table partial/unavailable without exception.
- Terminal `projection_failed` and `quarantined` must likewise remain typed unavailable/partial with an authority-derived reason.
- Do not invent success or reuse another table’s status.
- Add public `attach()` tests covering every active non-ready and terminal failure state, not helper-only tests.

## P2 — eliminate mutable pathname authority at query time

The current digest check followed by lazy `read_parquet(path)` view is prohibited. It allows atomic replacement after attach to return forged bytes.

- Capture each admitted Parquet file’s bytes exactly once, compute/verify the manifest digest over those captured bytes, parse those same captured bytes, and bind the result into DuckDB session-owned immutable state.
- Prefer a DuckDB temporary table created from the authenticated captured Arrow table(s), then expose the established public view/table name over that session-owned table. This closes both the verify→read race and post-attach lazy pathname reopening without managing fragile temporary-file lifetime.
- Do not hash a path and then independently reopen the path for parsing; that retains a race.
- Preserve manifest schema/row-count checks on the authenticated captured bytes and exact per-table readiness behavior.
- Add public adversaries for:
  1. file replacement between initial discovery/readiness and session binding;
  2. file replacement immediately after `attach()` and before first query;
  3. replacement between repeated queries on the same connection.
  The attached session must continue returning only the exact admitted rows or fail closed; it must never return replacement bytes.
- Keep pre-attach digest mismatch refusal.

## Static closure

Resolve all six reported `ty` diagnostics:

- two `ProjectionState` literal arguments;
- two removed manifest `failure_reason` accesses;
- two possible-None `current_row` subscriptions through explicit invariant narrowing.

Run Ruff format on the one drifted production file using the repository formatter, then touched Ruff check/format check. No broad restyling.

## Preserve

- `EvidenceLocator` CAS-only source authority and final CAS base contracts;
- append-only/monotonic settlement identity and transaction behavior;
- canonical derived/parquet root and atomic publication;
- deterministic read-only reconciliation and quarantine of unbound extras;
- per-table ready/partial/unavailable and explicit not-applicable versus zero-row-ready distinction;
- all restored pre-checkpoint attach/property tests and new settlement state-machine properties;
- exact no-backfill behavior.

## Verification/handoff

Run:

- projection settlement + pipeline;
- full attach + pre-existing properties + settlement properties including P1/P2 adversaries;
- trajectory runtime + data quality;
- touched `ty`, Ruff check/format, compile, `git diff --check`;
- clean status.

Commit cleanly and page `wH:p9` with exact head, commit/stat, captured-byte/session binding design, test counts, static results, and no-backfill/no-model confirmation. Do not claim approval; exact repaired head returns to System Architect.
