---
status: living
audience:
  - builder
  - operator
---

# Retired production factory

The autonomous production factory, supervisor, feeder, relay, and factory UI are retired.
Their source installers and service templates are removed. Installed services are not changed.

The native watcher core remains `live_watch`, `auto_watch`, and the causal monitor.
Finished-run Line scheduling still appends to the native `judge_station` queue.
The queue keeps its schema, duplicate checks, priority fields, and caller-owned lock.
The held Seeker route, native receipt checks, and source exports remain available.
The native certification backend and current certification workflow remain unchanged.
Strict G1–G7 consolidation is not part of this partial retirement.

Use `evallab prod --help` for the remaining queue and source operations.
Use [cached DEV scoring](score.md) instead of the retired QC report export.
Existing queues, WAL databases, pinned releases, receipts, labels, and evidence remain unchanged.
The source before retirement is in the `pre-deslop-2026-11-10` Git tag.
