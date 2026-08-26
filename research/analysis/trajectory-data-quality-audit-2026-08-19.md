# Trajectory data-quality audit — 2026-08-19

## Scope and provenance

This report closes the two bounded work items requested for this session:

1. field parity across durable campaigns;
2. citation-reopen digest/join/orphan integrity.

Implementation commit: `75b03de` on `feature/trajectory-data-trust-v1`, rebased onto `origin/main` `149169d` and including the jobs-Parquet attach fix `f36ec8a`. No raw CAS record, source trajectory, or append-only interpretation generation was deleted or rewritten. Automatic acceptance remains disabled.

## Confirmed defects and disposition

| Area | Confirmed defect | Disposition |
|---|---|---|
| Jobs Parquet | Job-level `job_id=*/jobs.parquet` was previously classified as missing/stray; unreadable job-level files could abort reporting. | Uses merged `f36ec8a` layout, treats trial-nested files as legacy stray without double counting, and reports unreadable files as `invalid` with `row_count=null`. |
| Trial/projection joins | Trial facts could match on trial ID without job ID; interpretation, judgment, and decision projections used partial identities. | Exact current joins use job ID, trial ID, pack digest, judgment ID, decision ID, and exact decision judgment list where available; missing and duplicate identities force HOLD. |
| Unknown versus zero | Missing PostgreSQL, Parquet, source fields, and citations could be confused with empty results. | Unavailable/missing states retain `row_count=null`; zero is reserved for an observed empty source. |
| Sidecar authority | Mutable local sidecars could be accepted without proving equality to the immutable interpretation CAS generation. | Complete sidecars must match canonical digests, decision-directory identity, archive digest, restored archive content, and byte-for-byte sidecar files. |
| Generation ambiguity | A valid generation could be selected while invalid/partial siblings were silently ignored. | More than one candidate generation is `multiple`; no generation is selected and the campaign remains on HOLD. |
| Source CAS integrity | Path existence was treated as CAS availability; malformed tar/gzip content could escape fail-closed paths. | Source and interpretation archives are restored and digest-verified; malformed archives become invalid/unavailable rather than usable evidence. Quarantined rows are not restored. |
| Citation identities | Report reconciliation used the handle's display `citation_id`, while runtime judgments use the canonical digest of the full handle. | Reconciliation now uses the same canonical full-handle digest as runtime gates. |
| Citation source binding | Handle and IR metadata could agree on a claimed source SHA without hashing the restored CAS member. | The restored source member is jailed to the archive root and byte-hashed against the authoritative source digest. |
| Selected/omitted pack parity | Selected payloads, selected hydrated text, omitted IDs, bounds, anchors, overlaps, and coverage were incompletely checked. | Serialized pack validation enforces canonical IR payload equality, exact selected/omitted partitioning, counts, bounds, reopening locators, hydrated-content equality during citation reopen, and per-member C10 hydration. Projection rebuild applies the same structural checks. |
| Durable report archive | Archiving an existing report directory could include stale files and change CAS identity independently of report identity. | Each report is archived from an isolated one-file staging directory, then restore-verified. |
| CLI surface | Operators lacked a concise deterministic projection of the full report. | `evallab analyze quality ... --fields '{name:.path,...}'` selects display fields only; the full immutable report is still written and archived. |

## Final durable campaign observations

All four reruns used the rebased implementation and the shared durable roots under `/Users/petermakhnatch/Developer/eval-lab/derived`.

| Campaign | Readiness | Jobs Parquet | Report ID | Report CAS URI |
|---|---|---|---|---|
| `terminal-bench-v3-k1-gemini-low-screen` | HOLD | present, 135 rows, no legacy strays | `sha256:2fe42476019d0f7a63bdbcf659a734ce170f92efa0f565d07f046142c23f64ea` | `cas://sha256/956476bda09c865187334a2f317abecc6e0d07b77cd55242c34163c0a1e313fd` |
| `canary-event-summary-codex-20260815` | HOLD | present | `sha256:f1f44d35a9058255dad388a2aa0d27bbb0f2970a958ebe276550a40158a2c307` | `cas://sha256/03c0e78c84de133336356f83bcfc4c34d92116a990ac75ed61db38747276ab37` |
| `canary-terminal-bench-html-js-filter-codex-20260815` | HOLD | present | `sha256:b3c0d86d54acb4c1cd8bb79840283aabddf69759ec32458025df7380ae67b006` | `cas://sha256/569000169e7aef23d363de30b6c9d99dbc84ce0ea99116bf98567fef969a3bca` |
| `canary-transaction-reconciliation-codex-20260815` | HOLD | present | `sha256:fc2f82c3d8558ff6c095014a256df37d9df4583343ff601cebb3242a077725e8` | `cas://sha256/5d2f471337ff8aa0743522b09b24af88ce31ed1477fa94230bc5bbe0bf467748` |

Current HOLD causes are evidence states, not suppressed errors: automatic acceptance disabled, PostgreSQL unavailable in this environment, coverage gaps, and append-only sidecar generation ambiguity; the TB3 campaign also retains its quarantined input. The jobs-Parquet missing hold is cleared after rebasing onto `f36ec8a`.

## Verification

- Focused parity/integrity/CLI/attach suite: passed (`tests/test_trajectory_data_quality.py`, `tests/test_trajectory_runtime.py`, `tests/test_cli_registry.py`, `tests/test_attach.py`; two expected skips).
- Full repository pytest suite: passed after the final fixes (one expected xfail and existing expected skips).
- `ruff check .`: passed.
- Documentation index, repository map, and governance checks: passed.
- Four real campaign `evallab analyze quality` reruns: completed and archived to the CAS URIs above.
