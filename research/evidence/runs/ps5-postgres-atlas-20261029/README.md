# Run Atlas reads on a real PostgreSQL (pS-SELF-e2fa4f-5, shard D)

Question: do the Run Atlas store reads and the facets/findings imports that changed this week behave on
PostgreSQL the way they do on SQLite? Four earlier receipts said "PostgreSQL: not run".

Setup: throwaway PostgreSQL 16.15 (Homebrew), `initdb --no-locale` (C collation), Unix socket only, port 55432,
deleted afterwards. Synthetic content only: no live store, no store copy, no answer-key row, no model call.

## 1. Same reads, same synthetic store, three backends (`compare_pg_sqlite.py`, `results.json`)

One synthetic store (7 tasks, 12 hand-made runs, 40 bulk tasks x 30 runs = 1,200 runs with 3 steps each, facet records naming a sealed task,
an admitted task whose id contains a sealed name, findings with valid/invalid/oversized evidence-step pairs) is built through the
public `CloudTraceDatabase` API on PostgreSQL, on SQLite+FTS5 and on SQLite+LIKE; 144 read/write calls run on each
(write receipts and table dumps included) and the PostgreSQL result is compared with both SQLite results: 288 comparisons.

| status | count | meaning |
|---|---|---|
| equal | 198 | same value |
| equal_exception | 78 | both sides raise the same type and message (refusals such as `not available: ...`, `unknown cluster_id`) |
| order_only | 4 | same rows, different order (2 calls x 2 stores) |
| differs | 4 | different value (2 calls x 2 stores), see finding C |
| differs_exception | 4 | different exception (2 calls x 2 stores), see finding D |
| PostgreSQL-only exception | 0 | after the fix; on origin/main 19 calls (38 comparisons) raised `TypeError` on PostgreSQL only (finding A) |

Calls covered: `search_runs` (13 filter/limit/allowed-scope forms, incl. an allowed list of 5,004 ids), `_admitted_facets`, `QaContext.served_facet_run_ids`
(tuple, set, frozenset, empty, denied, 5,004 ids, 40 tasks), `qa_data_inventory.traces` (14 forms: limit, dataset filter,
task, outcome, ids_only, alarms sort), `inventory`, `clusters`, `qa_data_task.task` / `_task_rows`, `qa_data_card.build_card` / `_stored`
(13 runs), `retrieve_similar_examples` (lexical, widened, facets, cluster, anchor `like_run_id`, cluster+anchor, +facets, refusals),
`failed_run_summary`, `qa_data_findings`, and the facets/findings imports (`import_run_facets`, `import_trace_findings`: 1,200 batched facet
records, 2,400 findings with exact `(trace_id, step_index)` evidence-pair probes, re-import, older/newer supersession).
Import receipts and the resulting `run_facets`, `trace_findings`, `run_clusters` rows are equal on all three backends.

## 2. Existing tests re-run on PostgreSQL (`pg_atlas_plugin.py`)

The plugin redirects every `CloudTraceDatabase("<tmp>/x.sqlite")` to a PostgreSQL schema, so the suites written against SQLite
run unchanged on PostgreSQL. 14 files (`test_run_atlas_store`, `test_qa_data*`, `test_trace_findings`, `test_cloud_trace_retriever*`,
`test_cloud_trace_snapshot_retriever`, `test_cloud_trace_trust_tiers`, `test_run_atlas_score`): 1,039 tests selected.

| | passed | failed |
|---|---|---|
| origin/main | 698 | 333 |
| this PR | 936 | 95 |

The 95 left (`pg_suite_failures_after_fix.txt`): 77 poke SQLite itself (`set_authorizer` 43, `PRAGMA` 9, `set_trace_callback` 2, raw `?` SQL on the
connection 8, path snapshots/`sqlite3` 7, legacy-DDL migration 1, SQLite-only `export` 3, raw `flagged_passage_steps` text 2, the
SQLite-only step-index bound 1, one token-size sweep that includes `export` 1); 18 are finding C. The native PostgreSQL variants of
`tests/test_cloud_trace_db.py` pass on both origin/main (148) and this PR (154: the 6 new parametrisations).

## 3. Findings

A. **Defect, fixed.** On PostgreSQL `retrieve_similar_examples` raised `TypeError: the JSON object must be str, bytes or bytearray, not dict`
whenever the returned example carried trace steps, and `expand_pointer` for any trace pointer did too: step `flags_json` is JSONB there, the
driver decodes it, and the retrieval hydrator `json.loads` it again. 262 of the 333 baseline test failures and 19 of the 143 calls above were this.
Fix: the raw-flag step columns cast `flags_json::text` on PostgreSQL (keeps the "decode only what is returned" design).
Regression test `test_retrieval_and_pointer_expansion_return_stored_step_flags_on_every_backend[postgres]` fails on origin/main.

B. **Defect, fixed.** `CloudTraceDatabase(url, read_only=True)` on PostgreSQL passed `options=-c default_transaction_read_only=on` to
`psycopg.connect`, which replaces the DSN's own `options`. A schema-scoped DSN (`...?options=-csearch_path%3Dx`, what the test
fixture uses) therefore lost its `search_path` and the read-only handle read the wrong schema (`UndefinedTable`; with a populated `public`
schema it would read that silently). The DSN's options are now kept. Regression test
`test_read_only_open_reads_the_same_store_and_refuses_writes[postgres]` fails on origin/main.

Follow-up: set the session default after connecting instead of replacing libpq's `options`, preserving
DSN, `PGOPTIONS`, and service-file options. A PostgreSQL 16 probe showed that `BEGIN; SET TRANSACTION
READ WRITE` overrides `default_transaction_read_only`; it is a safety default, not an authorization
boundary. Current library transaction paths do not issue that override.

C. **Recorded, not fixed.** PostgreSQL's `english` parser keeps `grader_tamperable.log` as one `host` token, so `search_traces("tamperable")` and
`retrieve_similar_examples("tamperable")` miss a step that only contains that identifier; FTS5 and LIKE find it
(`to_tsvector('english','cat grader_tamperable.log') @@ plainto_tsquery('english','grader tamperable')` is `false`). Snake_case and dotted
file names are common in traces, so lexical recall on a PostgreSQL store is lower than on SQLite (18 re-run tests encode the SQLite behaviour).
A fix changes the stored `search_vector`s (migration/rebuild) and is a decision for the owner.

D. **Recorded, not fixed.** A run id containing NUL reaches PostgreSQL as `psycopg.errors.DataError` from `task_of_run` / `build_card`, where SQLite
(like unencodable surrogates on both) gives the uniform `not available` refusal. Not reachable from the CLI (argv cannot carry NUL) and
it discloses nothing about admission; the library-level difference remains.

E. **Benign.** `qa.card_stored` lists a run's findings without `ORDER BY`; PostgreSQL and SQLite order equal `(produced_at)` rows differently
(the card sorts them afterwards, `build_card` is equal). `search_runs`/`_admitted_facets`/`served_facet_run_ids` with `= ANY(?)` arrays (empty, 5,004 ids,
quotes, sets) behave exactly as the SQLite `json_each` form.

F. **Recorded, not fixed.** PostgreSQL JSONB normalizes numeric representations: values such as `1e20`
and `-0.0` can appear as `100000000000000000000` and `0.0` in flags / `get_trace`, unlike SQLite.
In JSONL import probes, NUL in a facet run ID or an admitted run's goal raised PostgreSQL `DataError`;
findings refused a NUL run ID and NUL summary with explicit validation reasons.

Limits: synthetic content; C collation (a UTF-8 locale could order `ORDER BY run_id` differently from SQLite binary order); one PostgreSQL version.
