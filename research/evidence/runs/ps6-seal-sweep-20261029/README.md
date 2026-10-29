# QA data sealed-identity sweep

Question: do sampled `qa-data` replies name identities sealed by the admission registry?
The input is the 4,207,423,488-byte read-only SQLite backup at
`/private/tmp/ps2/store.sqlite`, measured against source revision
`2dea8b9d99b9a312886193cca3989295e36391fe`. It is a snapshot, not a census of
future store contents. No model calls or writes to the store are involved.

Run `EVALLAB_DISABLE_LOCAL_DOCKER=1 .venv/bin/python
research/evidence/runs/ps6-seal-sweep-20261029/sweep.py --seconds 180` from
the repository root; repeat to resume the count-only checkpoint. `--parity`
compares representative direct-dispatch replies with the real CLI. The script
uses the retriever's admission and seal construction. Sampling was reduced for
runtime: 30 dataset-stratified tasks plus the one other admitted ID containing
a seal, and 30 admitted runs including the known withheld-facet run. It tries
every facet value and every one of 68 stored clusters at levels 1–3. It covers
all 14 `qa-data` command types, all three export formats, step mapping and the
deterministic scanners; it also checks the receipt and manifest of the separate
`prod export-cluster-scope` command for each stored cluster. Exports use scratch.

Results: 1,425 seals; 545 invocations, 516 replies and 29 refusals. The 30
real-CLI comparisons matched direct dispatch. There were 22 class-b occurrences
and no unclassified occurrences: history 3 (`citation` 1, `task_id` 2), task 4
(`how_to_read_a_file` 2, `task_id` 2), traces 4 (`task_id`), trace-card 2
(`task_id`), and cluster-scope 9 (`canonical_task_id` 3, `task_id` 6).
Other commands had zero occurrences. No class-c defect was identified; no code
was changed and no regression test was added.

The checkpoint holds counts and field names only, not identities, hit context or
response bodies. Class b counts each seal occurrence within a longer admitted
identity or its bare name, including overlaps. The seal set contained no escaped,
JSON-delimiter, numeric-fragment or JSON-literal-fragment identities, making
string-leaf/key scanning suitable for this snapshot; it is not a claim about
arbitrary future seal sets. This is a sample, not proof for all possible
arguments, stored rows, or unsampled windows.
