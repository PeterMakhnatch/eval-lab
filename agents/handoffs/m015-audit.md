Status: complete
Last: completed full audit queue across 12 subjects including backup RESTORE path and corrected earlier verdicts
Next: Integrator review and PR merge into main
Blockers: none

# M015: LOOP-AUDIT Handoff

Auditing handoff claims across invisible-surface modules against origin/main.

## Ledger

| date | subject | handoff | verdict | evidence path | risk note |
|---|---|---|---|---|---|
| 2026-08-18 | preflight | `agents/handoffs/preflight.md` | CONFIRMED | `research/audits/evidence/preflight/` | Core runtime and contract tests pass (31 tests); operator docs in `docs/operations.md` omit manual command reference |
| 2026-08-18 | storm | `agents/handoffs/storm-status.md` | CONFIRMED | `research/audits/evidence/storm/` | Storm engine, tests (11 tests), models, and banner utilities verified; engine lacks a direct CLI subcommand |
| 2026-08-18 | parquet-compaction | `agents/handoffs/parquet-compaction.md` | CONFIRMED | `research/audits/evidence/parquet-compaction/` | Compaction engine, tests (15 tests), and dry-run across 72 jobs on 9 tables pass; runs via module invocation `python -m evallab.parquet_compaction` |
| 2026-08-18 | status/status_generator | `agents/handoffs/storm-status.md` | CONFIRMED | `research/audits/evidence/status_generator/` | Markdown generator and tests (9 tests) verified; defaults to `research/experiments/STATUS.md` rather than `docs/STATUS.md`, no direct CLI subcommand |
| 2026-08-18 | preflight (correction) | `agents/handoffs/preflight.md` | CONFIRMED | `research/audits/evidence/preflight/` | Sharpened standard: CONFIRMED on execution surface (`uv run evallab preflight` works live as claimed); documentation in `docs/operations.md` remains missing |
| 2026-08-18 | storm (correction) | `agents/handoffs/storm-status.md` | DRIFTED | `research/audits/evidence/storm/` | Supersedes row 2: `storm.py` has no CLI entrypoint and is not imported or called anywhere in `src/` (unwired in production); unit tests pass but module is unreachable |
| 2026-08-18 | parquet-compaction (correction) | `agents/handoffs/parquet-compaction.md` | CONFIRMED | `research/audits/evidence/parquet-compaction/` | Sharpened standard: CONFIRMED on documented module entrypoint (`python -m evallab.parquet_compaction compact --dry-run` works); lacks root `evallab` CLI subcommand |
| 2026-08-18 | status/status_generator (correction) | `agents/handoffs/storm-status.md` | DRIFTED | `research/audits/evidence/status_generator/` | Supersedes row 4: generator shipped without a CLI entrypoint or automated caller in `src/`; target `docs/STATUS.md` did not exist on main |
| 2026-08-18 | spine purpose gate | `agents/handoffs/spine-purpose.md` | CONFIRMED | `research/audits/evidence/spine_purpose_gate/` | `ExperimentSpec.purpose` required across 7 values; `PolicyGate` and `evallab submit` reject purposeless specs with reason_code `purposeless_spec`; 30 tests pass |
| 2026-08-18 | quota accounting vs events.jsonl truth | `agents/handoffs/quota-accounting.md` | CONFIRMED | `research/audits/evidence/quota_accounting/` | Headroom (70.0% used, 67 snapshots) and artifact tokens (1.17M cached, 113K uncached) verified; `events.jsonl` reservation ($2.50) confirmed as static queue bound, not spend; 79 tests pass |
| 2026-08-18 | storm (correction 2) | `agents/handoffs/storm-status.md` | CONFIRMED | `research/audits/evidence/storm/` | Supersedes row 6: earlier correction was wrong (`grep -rn 'from evallab.storm import' src/` reveals callers in `digest.py` and `status_generator.py`); module is wired in production |
| 2026-08-18 | status/status_generator (correction 2) | `agents/handoffs/storm-status.md` | UNPROVEN | `research/audits/evidence/status_generator/` | Supersedes row 8: wired in `automation.py` as a nightly step, but 'wired but never run' (`launchctl list | grep evallab` shows 0 loaded jobs); pipeline has never executed under scheduler |
| 2026-08-18 | attach surface | `agents/handoffs/e04-attach.md` | CONFIRMED | `research/audits/evidence/attach/` | DuckDB attach surface across Z2 (PostgreSQL scanner), Z3 (9 Parquet tables), Z4 (doc front-matter) verified; CLI `evallab db attach` `--zones`/`--query` functional; 12 tests pass |
| 2026-08-18 | contextpack determinism | `agents/handoffs/context-pack.md` | CONFIRMED | `research/audits/evidence/contextpack/` | Deterministic priority truncation under 12k token budget verified; two consecutive compilations produce byte-identical output; 42 tests pass |
| 2026-08-18 | canary suite paths vs library/tasks/ | `policy/canary-suite.yaml` | CONFIRMED | `research/audits/evidence/canary_suite/` | All 3 suite members (transaction-reconciliation, terminal-bench-html-js-filter, event-summary) resolve to `library/tasks/` with exact matching directory sha256 digests; 9 tests pass |
| 2026-08-18 | behavior | `agents/handoffs/behavior.md` | CONFIRMED | `research/audits/evidence/behavior/` | Behavioral telemetry analysis across 92 trials in DuckDB/Parquet; effort vs outcome, struggle signals, token economics, and confidence intervals verified; 6 tests pass |
| 2026-08-18 | provenance | `agents/handoffs/provenance.md` | CONFIRMED | `research/audits/evidence/provenance/` | Task classification and reporting over 74 external and 4 local tasks verified; runs via `python -m evallab.provenance`; no top-level `evallab provenance` subcommand; 11 tests pass |
| 2026-08-18 | backups | `src/evallab/backups.py` | CONFIRMED | `research/audits/evidence/backups/` | Manifest sha256 verified; custom-format dump restored cleanly via `pg_restore` into throwaway database (reconstructing 69 jobs, 83 trials, 257 rewards) with 0 live DB pollution; 4 tests pass; no CLI restore wrapper exists |

## Cycle Log
- Cycle 1 (2026-08-18): Audited `preflight` (`agents/handoffs/preflight.md`). Initial verdict: CONFIRMED. Runtime command `evallab preflight` works without network/billable calls, `tests/test_preflight.py` passes 31 tests, digest section is integrated. Board note filed for missing documentation in `docs/operations.md`.
- Cycle 2 (2026-08-18): Audited `storm` (`agents/handoffs/storm-status.md`). Initial verdict: CONFIRMED. Engine sliding window detection, structured `StormAlarm` models, reason code catalog, rendering functions, and 11 tests in `tests/test_storm.py` verified.
- Cycle 3 (2026-08-18): Audited `parquet-compaction` (`agents/handoffs/parquet-compaction.md`). Initial verdict: CONFIRMED. Compaction across 9 tables, date resolution hierarchy, retention window, and 15 tests in `tests/test_parquet_compaction.py` verified; live dry-run planned 72 jobs across 3 dates with 0 row loss.
- Cycle 4 (2026-08-18): Audited `status/status_generator` (`src/evallab/status_generator.py`). Initial verdict: CONFIRMED. Generator correctly projects catalog, queue, and `PROGRAM.json` state into markdown; 9 tests pass in `tests/test_status_generator.py`.
- Cycle 5 (2026-08-18): Applied Integrator corrections for Cycles 1-4. Audited `spine purpose gate` (`agents/handoffs/spine-purpose.md`). Verdict: CONFIRMED. `ExperimentSpec.purpose` strictly required across 7-value taxonomy; `PolicyGate.decide` refuses purposeless specs at dispatch time (`reason_code="purposeless_spec"`); `evallab submit` rejects missing/invalid purpose; `scripts/backfill_spec_purpose.py` verified; 30 tests pass in `tests/test_spine_purpose.py` and `tests/test_queue.py`.
- Cycle 6 (2026-08-18): Audited `quota accounting vs events.jsonl truth` (`agents/handoffs/quota-accounting.md`, `agents/handoffs/quota-gate.md`, `agents/handoffs/quota-fallback.md`). Verdict: CONFIRMED. Verified `quota.py` sidecar fallback across 67 snapshots in `research/evidence/runs/` reporting 70.0% `[observed]` used, 30.0% remaining, weekly rolling window (`10080` min), and `hard_stop=True`. Verified artifact token aggregation: 1,176,832 cached input tokens (91.2%), 113,176 uncached input tokens, 40,375 output tokens across 9 trials ($0.9462 list-price equivalent). Reconciled against `queue/events.jsonl` truth: `events.jsonl` tracks lifecycle state-machine events and gate reason codes, while `dispatch_attempt_reserved` carries a static estimate heuristic ($2.50) bounding the queue slot rather than measuring provider spend. 79 tests pass.
- Cycle 7 (2026-08-18): Applied Second Integrator Corrections on `storm` and `status_generator`: refuted earlier unwired claims via caller grep evidence (`src/evallab/digest.py`, `src/evallab/status_generator.py`, `src/evallab/automation.py`) and established the systemic "wired but never run" finding via `launchctl list` evidence. Audited remaining queue subjects:
  - `attach surface`: CONFIRMED (DuckDB attaches Z2, Z3, Z4; 12 tests pass; live queries return 76 docs, 92 trials).
  - `contextpack determinism`: CONFIRMED (Priority truncation under 12k budget verified byte-identical across runs; 42 tests pass).
  - `canary suite paths vs library/tasks/`: CONFIRMED (All 3 suite members resolve to `library/tasks/` with identical sha256 directory digests; 9 tests pass).
  - `behavior`: CONFIRMED (Behavioral telemetry across 92 trials in DuckDB/Parquet verified with confidence intervals; 6 tests pass).
  - `provenance`: CONFIRMED (Multi-corpus task origin classifier and report verified over 74 external and 4 local tasks; 11 tests pass; CLI gap noted).
  - `backups`: CONFIRMED (Atomic dump generation verified with SHA-256 manifest; custom-format dump restored cleanly into throwaway database restoring 69 jobs, 83 trials, 257 rewards without live DB pollution; 4 tests pass; no CLI restore wrapper).

## Evidence Transcript: PostgreSQL Backup & RESTORE Path Audit (`backups`)

### Command 1: `uv run pytest tests/test_backups.py -v`
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

### Command 2: Integrity Verification on Live Backup Generation
```python
import hashlib, json
from pathlib import Path

backup_dir = Path("backups/postgres/evallab-2026-08-16")
dump_file = backup_dir / "database.dump"
manifest_file = backup_dir / "manifest.json"

manifest = json.loads(manifest_file.read_text())
dump_bytes = dump_file.read_bytes()
calc_sha = hashlib.sha256(dump_bytes).hexdigest()

print("Manifest schema_version:", manifest.get("schema_version"))
print("Manifest created_at:    ", manifest.get("created_at"))
print("Manifest size_bytes:    ", manifest.get("size_bytes"), f"(actual: {len(dump_bytes)})")
print("Manifest sha256:        ", manifest.get("sha256"))
print("Calculated sha256:      ", calc_sha)
print("Integrity check:        ", calc_sha == manifest.get("sha256"))
```
Output:
```
Manifest schema_version: 1
Manifest created_at:     2026-08-16T06:46:46.876076+00:00
Manifest size_bytes:     145800 (actual: 145800)
Manifest sha256:         ce3a5b7a55a213d20187a001048c993e725e45a214806faf36e6ceb107eda469
Calculated sha256:       ce3a5b7a55a213d20187a001048c993e725e45a214806faf36e6ceb107eda469
Integrity check:         True
```

### Command 3: Real RESTORE Execution Into Throwaway Database
```bash
docker compose exec -T postgres createdb -U evallab evallab_restore_audit_throwaway
docker compose exec -T postgres pg_restore -U evallab -d evallab_restore_audit_throwaway --no-owner --no-privileges < backups/postgres/evallab-2026-08-16/database.dump
docker compose exec -T postgres psql -U evallab -d evallab_restore_audit_throwaway -c "
SELECT table_name, (xpath('/row/cnt/text()', xml_count))[1]::text::int as row_count
FROM (
  SELECT table_name, table_schema, 
         query_to_xml(format('select count(*) as cnt from %I.%I', table_schema, table_name), false, true, '') as xml_count
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
) t ORDER BY table_name;"
docker compose exec -T postgres dropdb -U evallab evallab_restore_audit_throwaway
```
Output:
```
         table_name          | row_count 
-----------------------------+-----------
 analysis_evidence_citations |         0
 analysis_findings           |         0
 analysis_invocations        |         0
 analysis_reviews            |         0
 artifacts                   |       220
 deterministic_trial_facts   |        83
 experiments                 |        66
 jobs                        |        69
 rewards                     |       257
 run_files                   |      1293
 trajectory_documents        |        15
 trials                      |        83
(12 rows)
```
