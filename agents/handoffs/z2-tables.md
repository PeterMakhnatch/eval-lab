Status: review-wanted
Last: all Z2 catalog tables and views implemented, tested, and verified; documentation and indexes generated
Next: integrator review; do not merge
Blockers: none (v_quota_today is backed by the real live catalog source `trials`, which records runs, agent/provider, started_at, input_tokens, and output_tokens; subscription headroom percentages remain observable via quota sidecars / quota.py)

## Z2 Catalog Tables & Views: suites, suite_members, and v_quota_today

Worktree: `.worktrees/z2-tables` (branch `role/z2-tables`)
Edited ONLY allowed paths:
- `sql/schema.sql`
- `sql/views.sql`
- `src/evallab/database.py`
- `tests/test_z2_tables.py`
- `docs/catalog-tables.md`
- `docs/INDEX.md`
- `docs/repo-map.md`
- `agents/handoffs/z2-tables.md`

No Harbor runs, no paid models, no network beyond local PostgreSQL.

### Changes

1. **`suites` and `suite_members` catalog tables (`sql/schema.sql`)**:
   - `suites` table keyed on `(name, version)` with `frozen_at` and `created_at`.
   - `suite_members` table keyed on `(suite_name, suite_version, task_ref, task_version)` referencing `suites(name, version)` with ON DELETE CASCADE and indexes on `(task_ref, task_version)` and `(suite_name, suite_version)`.
   - Idempotent DDL applied by `evallab db init` via `database.initialize()`.

2. **Database-level frozen suite immutability (`sql/schema.sql`)**:
   - Enforced in PostgreSQL via `BEFORE` triggers:
     - `trg_suite_members_immutability` on `suite_members`: aborts any `INSERT`, `UPDATE`, or `DELETE` on members of a frozen suite (`frozen_at IS NOT NULL`).
     - `trg_suite_immutability` on `suites`: aborts any `UPDATE` modifying `name`, `version`, or unfreezing `frozen_at`, and prevents `DELETE` of frozen suites.
   - Enforced in the database rather than only Python so direct SQL queries, external ingests, and migrations cannot corrupt historical comparisons citing frozen suites (§2.1, §4).

3. **`v_quota_today` view (`sql/views.sql` and `sql/schema.sql`)**:
   - Aggregates provider consumption for the current UTC calendar day: `provider`, `runs`, and `tokens` (`sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0))`).
   - Normalizes timestamps to UTC before date grouping: `(started_at::timestamptz AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date`.
   - `sql/views.sql` provides schema fallbacks (`trials`, `trial_usage`) enabling execution in clean DuckDB sessions with zero pre-created tables.

4. **Database helpers (`src/evallab/database.py`)**:
   - Added `views_path() -> Path` resolving `sql/views.sql`.
   - Added `quota_today(database_url: str) -> list[tuple[str, int, int]]` helper.

5. **Comprehensive tests (`tests/test_z2_tables.py`)**:
   - DDL idempotence: applying schema twice succeeds.
   - Frozen suite rejection: direct INSERT, UPDATE, DELETE on frozen suite members and unfreezing/deleting frozen suites fail with expected `psycopg.errors.RaiseException`.
   - Unfrozen suite mutability: inserting, updating, and deleting members on draft suites succeed.
   - UTC day bucketing: planting trials across UTC midnight verifies boundary correctness and timezone independence in both DuckDB and live PostgreSQL.
   - Clean DuckDB view script execution with zero pre-created tables.
   - CI-safe with `skipif` naming `_DSN_FOR_TEST` for PostgreSQL tests.

6. **Documentation & Repo Maps**:
   - `docs/catalog-tables.md` created with required YAML front-matter, table/view specs, immutability rationale, UTC convention, and empty vs unavailable source handling.
   - `docs/INDEX.md` and `docs/repo-map.md` regenerated and checked.

### Verification

- `uv run pytest tests/test_z2_tables.py`: 6 passed
- `uv run pytest`: 1218 passed, 2 skipped, 1 xfailed (100% passing)
- `uv run ruff check .`: All checks passed
- `uvx ty@0.0.71 check src/ --output-format=concise 2>&1 | tail -2`: Found 28 diagnostics (at/below 28 threshold)
- `uv run python -m evallab.repomap check`: passed
- `uv run python -m evallab.docindex check`: passed
- `uv run evallab db init`: database schema is current (idempotent)

### Frozen Suite Rejection Output

```
psycopg.errors.RaiseException: Cannot modify membership of frozen suite test-frozen-1a2b3c4d@v1 (frozen at 2026-08-18 01:25:01+00)
CONTEXT: PL/pgSQL function check_suite_members_immutability() line 20 at RAISE
```
