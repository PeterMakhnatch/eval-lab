# ps5 screened facet read memo (2026-10-29)

Counts and CPU seconds only; no run content. Store: read-only copy `/private/tmp/ps2/store.sqlite`
(4,207,423,488 bytes). Baseline: detached `origin/main` (5a1a7bd4a). Scripts in this directory.

## Question
`CloudTraceDatabase._admitted_facets` (behind `qa-data runs/clusters/recount/traces/task/trace-card`) and
`CloudTraceRetriever._retrieval_facets` (every Atlas `retrieve_similar_examples`) re-ran the facet SQL and
the sealed-text screen on every call. Can the repeat be cut without changing a served byte?

## Change
Both reads go through `CloudTraceDatabase._read_memo`. Admission and the returned seal set are still
decided afresh on every call; only the facet SQL plus screening is reused, keyed by
`(store change token, allowed task set, sealed identity set, typed filters, limit)` for
`_admitted_facets` and `(change token, admitted ids, seal pattern)` for `_retrieval_facets`.
PostgreSQL has no change token, so it always rebuilds. Rows are copied out of the memo.

## Numbers
* Identity (`cli_identity_and_cpu_old_first.json`): 18 distinct real `qa-data` calls (traces x6 incl. `--ids-only`/`--sort alarms`,
  task x2 incl. the sealed-name-contained `layout-config-recreation2`, clusters, runs x3 incl. `--like`, recount,
  inventory, history, trace-card x2 incl. the withheld run `retained-trace:b493cd8d...`), old vs new, `meta` dropped:
  `data` equal in 18/18 (one refused call returns the same error).
* Identity in process (`identity_old.json` vs `identity_new.json`): 40 `_admitted_facets` shapes
  (5 filter sets x 4 scopes x 2 limits) and 40 Atlas `retrieve_similar_examples` queries (facets, `like_run_id`,
  `include_provisional`): digests of all rows/answers identical.
* Repeat inside one process (`inprocess_repeat_cpu.json`, n=7 fresh processes, old/new interleaved, CPU s medians):
  two screened reads (unfiltered + one facet filter) repeat 0.1075 -> 0.0393 (-63%);
  Atlas `retrieve_similar_examples(facets=...)` repeat 0.744 -> 0.642 (-14%; the rest of an Atlas call is not facet screening).
  The "first" columns are not cold: the child warms the unfiltered read before timing, so ignore them.
* One-shot CLI (`cli_cpu_alternating_order.json`, n=9, old/new alternating who goes first): traces --limit 200 0.446 -> 0.438,
  task cad-model 0.485 -> 0.507, clusters 0.403 -> 0.427, runs --facet 0.503 -> 0.460: **no change** beyond noise
  (a process screens each facet set once, so a per-process memo cannot help). The first run
  (`cli_identity_and_cpu_old_first.json`, old always first, n=7) showed 0-17% "gains"; that was order bias, not the change.

## Meaning
The win is for long-lived workers and loops that screen repeatedly (Seeker/Task QA retrieval, services); a single CLI
invocation is unchanged. Admission, sealing and screening rules are untouched.
