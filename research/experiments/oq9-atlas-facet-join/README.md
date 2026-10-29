# OQ-9: zero-model facet ranking vs the 44 keyed OQ-6 DEV tasks

Preregistered in the bridge `opus-vs-swarm-qa.md` before scoring. `join_rank.py` ranks strict-DEV tasks by the number of
faceted runs with (a) tests modified/deleted/replaced, (b) answer material accessed, (c) grader/harness touched,
(d) suspicion >= 2, against a task-hash baseline, and reports precision@10/20/44 with Wilson 95% intervals,
facet coverage of the keyed tasks, and an exploratory Fisher/BH cluster enrichment. Controls: pass-only counts,
share of faceted runs, and "most faceted runs first".

Run: `PYTHONPATH=src python research/experiments/oq9-atlas-facet-join/join_rank.py --output results.json`
(needs the cloud trace store, the run-atlas `facets-v1.jsonl`, and the OQ-7 `labels.jsonl` for the strict population).
Unkeyed tasks are not certified negatives; precision is a lower bound. Result summary: see the bridge file.
