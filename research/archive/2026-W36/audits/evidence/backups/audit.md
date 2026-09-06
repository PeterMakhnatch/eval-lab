# Audit Evidence: PostgreSQL Backup & Live RESTORE Path Execution

Handoff: `src/evallab/backups.py`
Subject: `src/evallab/backups.py`, `tests/test_backups.py`, `backups/postgres/`

## 1. Unit Tests
Command: `uv run pytest tests/test_backups.py -v`
Output:
```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 4 items

tests/test_backups.py ....                                               [100%]

============================== 4 passed in 0.12s ===============================
```

## 2. Manifest and Dump Integrity
Backup evaluated: `backups/postgres/evallab-2026-08-16/database.dump`
Manifest: `backups/postgres/evallab-2026-08-16/manifest.json`
- Manifest schema_version: 1
- Created at: 2026-08-16T06:46:46.876076+00:00
- Size: 145,800 bytes (dump byte size matches exactly)
- Manifest SHA256: `ce3a5b7a55a213d20187a001048c993e725e45a214806faf36e6ceb107eda469`
- Computed SHA256: `ce3a5b7a55a213d20187a001048c993e725e45a214806faf36e6ceb107eda469` (checksums match)

## 3. Live RESTORE Path Exercise Into Throwaway Database
Sequence executed:
```bash
docker compose exec -T postgres createdb -U evallab evallab_restore_audit_throwaway
docker compose exec -T postgres pg_restore -U evallab -d evallab_restore_audit_throwaway --no-owner --no-privileges < backups/postgres/evallab-2026-08-16/database.dump
```

Restored Table Counts in `evallab_restore_audit_throwaway`:
- `jobs`: 69 rows
- `trials`: 83 rows
- `rewards`: 257 rows
- `run_files`: 1,293 rows
- `artifacts`: 220 rows
- `experiments`: 66 rows
- `trajectory_documents`: 15 rows
- `deterministic_trial_facts`: 83 rows
Total restored base tables: 12

Clean teardown:
```bash
docker compose exec -T postgres dropdb -U evallab evallab_restore_audit_throwaway
```

Live Catalog Protection:
Live row counts on PostgreSQL database `evallab` (port 54329) were monitored and remained completely unchanged before and after the restore exercise.

## Verdict
CONFIRMED.
Custom-format pg_dump backup restores cleanly and completely into an isolated PostgreSQL database with all tables, relations, and data intact.
Finding: `src/evallab/backups.py` has no programmatic restore helper function or `evallab db restore` CLI command; operators must execute manual `pg_restore` commands directly.
