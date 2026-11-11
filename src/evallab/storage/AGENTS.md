# Storage

## Purpose
Storage paths/zones, attachments and disposition, Parquet compaction, task audit, and trial census.

## What lives here / entry points
`paths.py`, `attach.py`, `fs.py`, `parquet_compaction.py`, `task_audit.py`, and `trials.py`.

## Invariants or rules
- `paths.py` owns zone/path classification; do not duplicate zone policy.
- Attachment and disposition results carry explicit reasons.
- Immutable raw Harbor evidence remains source authority; catalogs and projections do not replace it.
- Follow the retention policy before deleting/rebuilding ignored storage.

## Tests or checks
The assigned verifier uses existing paths, attach/attach-properties, compaction, and trial-census tests for the touched behavior.

## What not to add here
No alternate SQL-dialect abstraction or model-based evaluation in storage helpers.
