Status: complete
Last: Implemented append-only human verdict record for discoveries (schema, views, engine, CLI, tests, docs)
Next: Downstream researcher prompts and report generators query v_current_verdicts to filter accepted findings
Blockers: none

## Summary

Implemented the human verdict decision record (§2.1, §2.2, §6) to capture authoritative dispositions on findings in `digests/DISCOVERIES.md`:

1. **Zone 2 Catalog Table (`sql/schema.sql`)**:
   - Added idempotent `verdicts` table with columns: `id`, `discovery_id`, `status`, `"by"`, `"at"`, `note`, `ingested_at`.
   - Indexed `discovery_id`, `"at" DESC`, and `status`.

2. **SQL Views and Fallbacks (`sql/verdicts.sql` & `sql/schema.sql`)**:
   - `v_current_verdicts`: Exposes the latest verdict per discovery (partitioned by `discovery_id`, ordered by `"at" DESC`).
   - `v_verdicts_history`: Exposes full append-only decision history per discovery, oldest first.
   - DuckDB fallback schema (`verdicts_schema_fallback`) allowing zero-table standalone DuckDB resolution.

3. **Validation and Storage Engine (`src/evallab/verdicts.py`)**:
   - Reuses `Verdict` contract model from `src/evallab/schemas.py`.
   - Strictly append-only: changing a verdict adds a new timestamped row, never overwriting or mutating prior rows.
   - Mandatory human actor (`--by`): refuses empty or automated actors (`autopilot`, `agent`, `bot`, `ci`, `harbor`, `codex`, etc.).
   - Discovery existence validation: resolves against `digests/DISCOVERIES.md` and refuses unknown IDs to prevent typos.
   - Status validation: enforces §2.1 literal set (`accepted`, `rejected`, `needs_evidence`, `pending`).
   - Supports both PostgreSQL catalog and DuckDB execution surfaces.

4. **CLI Integration (`src/evallab/cli.py`)**:
   - Wired `evallab verdict <discovery_id> <status> --by <who> [--note ...]`
   - Wired `evallab verdict list [--status S] [--json]`
   - Wired `evallab verdict history <discovery_id> [--json]`
   - Registered `verdict` in `tests/test_cli_audit.py` in parser-registration order.

5. **Documentation & Tests**:
   - Authored `docs/verdicts.md` documenting operational status meanings, append-only rules, views, and refusal cases.
   - Regenerated `docs/repo-map.md` and `docs/INDEX.md`.
   - Authored 17 unit tests in `tests/test_verdicts.py` covering roundtrips, history invariants, refusals, CLI, and standalone DuckDB.

## Verification
- `uv run pytest tests/test_verdicts.py` (17 passed, 1 skipped)
- `uv run pytest tests/test_cli_audit.py` (55 passed)
- `uv run pytest` (1143 passed, 3 skipped, 1 xfailed)
- `uv run ruff check .` clean
- `uvx ty@0.0.71 check src/` clean (28 diagnostics, unchanged from baseline)
- `uv run python -m evallab.repomap check` clean
- `uv run python -m evallab.docindex check` clean
- `uv run evallab verdict list` clean
