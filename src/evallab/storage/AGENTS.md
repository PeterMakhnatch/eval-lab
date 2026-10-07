# Storage

## Purpose
Storage paths/zones, attachments and disposition, Parquet compaction, data backfill, trial census, and storage inspection.

## What lives here / entry points
`paths.py`, `attach.py`, `fs.py`, `parquet_compaction.py`, `data_backfill.py`, `trials.py`, and `inspect_storage.py`.

## Invariants or rules
- `paths.py` owns zone/path classification; do not duplicate zone policy.
- Attachment and disposition results carry explicit reasons.
- Immutable raw Harbor evidence remains source authority; catalogs and projections do not replace it.
- Follow the retention policy before deleting/rebuilding ignored storage.

## Tests or checks
The assigned verifier uses existing paths, attach/attach-properties, compaction, and backfill-command tests for the touched behavior.

## What not to add here
No alternate SQL-dialect abstraction or model-based evaluation in storage helpers.
