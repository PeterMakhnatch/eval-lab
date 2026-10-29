## Reverted (2026-10-29)

The review measured screened reads at ~0.090 s miss / 0.015 s hit, deep-copy
at 6.3 ms/call and ~0.7 MB/entry; Atlas retrieval was 0.469 → 0.461 s,
within variation, and one-shot CLI was unchanged. The memo adds a second
sealing-boundary cache without a real repeated-read caller; scoped keys churn
the shared LRU; the pre-existing memo assumes an unchecked lock/transaction;
and PostgreSQL paid for copies without caching. The parent chose removal:
no sealing or served byte changed at any time.
Against main, 19/19 CLI replies, 40/40 facet shapes and 40/40 retrieval
queries matched; see the removal evidence for the CPU comparisons.

# Screened facet memo: rebased measurement (2026-10-29)

Question: can repeated screened facet and Atlas retrieval reads be reused without changing served results?
The store was the 4,207,423,488-byte read-only copy `/private/tmp/ps2/store.sqlite`; the baseline was a detached checkout of main `79c5ade11e815214c941f337634c3cf6d4c08e7e`. Only aggregate counts and CPU timings are recorded.

`_admitted_facets` keys its screened rows by allowed tasks, narrowed run IDs, the complete freshly determined seal set, typed filters and limit; `_retrieval_facets` keys by admitted tasks and the seal pattern and flags. SQLite's change token covers the store snapshot. PostgreSQL rebuilds. Returned rows are deep copies, and rollback discards memo entries. Admission and returned seals are determined on each call. Sealing rules are unchanged.

## Results

* `rebased-results.json`: all 18 real CLI cases agreed after dropping `meta`, including one identical refusal. The calls are listed in `cli_compare.py`. Five CLI timing cases use n=7 interleaved processes alternating old/new first. CPU-second medians old → new: traces 0.314 → 0.326; task 0.416 → 0.406; clusters 0.279 → 0.284; filtered runs 0.368 → 0.357; recount 0.800 → 0.789. These one-shot timings do not establish a memo benefit and are susceptible to system noise.
* `rebased-results.json`: n=7 interleaved fresh processes alternating old/new first. CPU-second medians of within-process repeated calls: two screened reads 0.0807 → 0.0345; Atlas retrieval 0.469 → 0.461. Atlas's small difference is within observed variation. Each sample is the median of five repeat calls in a fresh process. Other local tests and CLI measurements ran concurrently, so timings should not be treated as isolated-machine measurements. Samples are rounded to six decimal places.

The other copied JSON files record earlier PR #1561 or pre-final-rebase measurements against older baselines; they are historical, not evidence for this revision. The scripts describe their corresponding protocols.
