# pS-SELF-e2fa4f-3: path-mode `CloudTraceRetriever` open cost

`CloudTraceRetriever(path)` (the Seeker's `--cloud-trace-db` route) copied every DEV task, issue, trace
and step into a private SQLite snapshot, re-tokenising all step text through FTS5 triggers. `retrieve()`
only reaches traces whose receipt status is `confirmed` or `verified`, so only those traces and their steps
are copied now.

Store copy: a SQLite backup of the live Cloud Trace DB, 4,207,423,488 bytes, opened read-only, never written
(`source_unchanged: true`). No model call, no Docker.

| | before | after (3 runs) |
|---|---|---|
| open CPU s | 118.67 | 1.21, 2.30, 2.03 |
| open wall s | 202.36 | 4.95, 28.53 (loaded machine), 2.76 |
| snapshot bytes | 3,737,165,824 | 38,498,304 |
| 120 queries, CPU s | 8.39 | 1.97, 2.30, 3.06 |
| answers sha256 | `ea0c0d3f...caef1b8` | identical in all 3 |

120 answers = 30 queries x 4 task ids (796 hits, 106 answers with hits), compared as hits plus receipt
counts, without the source path and snapshot digest (which differ by design). The before row is a single
4-minute run; `summaries.json` has every field. `path_open.py` is the harness; `diag_cluster.py` is the
check that cluster + anchor retrieval answers on the store (8 of 8 level-2 clusters, five argument shapes).
