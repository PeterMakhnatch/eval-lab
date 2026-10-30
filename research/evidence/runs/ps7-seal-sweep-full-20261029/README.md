# Full-population sealed-name sweep

Question: do the admitted DEV `qa-data` replies expose a sealed identity, rather
than an admitted identity which merely contains a seal as a substring?

## Corpus and method

The only store input is the read-only SQLite backup
`/private/tmp/ps2/store.sqlite` (4,207,423,488 bytes). Admission and seals come
from the same retriever as the dispatcher; neither answer-key assignments nor
withheld task/trace bodies are read directly. The population is 3,474 admitted
tasks, 4,544 admitted runs, 68 stored clusters across levels 1–3, and 1,425 seals.
Exactly **one admitted task** contains a seal as a substring.

`tools/sweep.py` extends the PR #1606 sweep (the original sample is retained
unchanged). It replaces the task/run sample with the entire admitted population,
exports all run ids in batches of at most 200 in all three supported formats,
checks step maps for every run, checks every cluster's member listing, run search
and export scope, and tries every supported facet value. Each process reuses one
open read-only database connection and one retriever. It checkpoints aggregate
results after each invocation and resumes using per-command offsets.

Run from the repository root with the installed Python environment:

```sh
EVALLAB_DISABLE_LOCAL_DOCKER=1 .venv/bin/python research/evidence/runs/ps7-seal-sweep-full-20261029/tools/sweep.py --seconds 175
```

Repeat until complete. `--command NAME` restricts a chunk to one command without
changing its population. `--parity` separately resumes six evenly spaced cases
for each of the eleven read-only command types (66 actual CLI comparisons).
Exports and cluster manifests use only `/private/tmp/ps7/full-sweep`; each is
deleted after its reply is inspected. The private collision ordinal checkpoint
contains no identities and is not committed. Delete that checkpoint only when
starting with a fresh results JSON.

## Classification and costs

Every JSON string leaf and key, including refusal messages, is matched against
the registry seal set, case folded. Class b means the match is wholly inside an
admitted canonical identity or its bare name. All other string hits are class c
candidates; none are silently treated as harmless. Class a is count-only or
uniform-refusal metadata: the seal encoding audit excludes numeric and JSON
literal fragments, so counts cannot be mistaken for identities in this corpus.
The checkpoint records class-b occurrence counts, hit-reply counts and distinct
admitted tasks per command and per field. Private sets use collision ordinals;
only their cardinalities leave scratch.

Per-command `cpu_seconds_per_1000` measures dispatcher CPU, including its export
or step-map setup when relevant, but excludes seal classification, checkpoint
serialization, process startup and initial population discovery. `wall_seconds`
is the matching dispatcher wall time, including store I/O. These are local
measurements, not isolated benchmark predictions; other local work can affect
wall time. No model/API calls, store writes or Docker are involved.

## Scope limits

This exhausts the stated identities and command types, **not** the Cartesian
product of optional CLI arguments. Default budgets, limits and first windows
are retained; it does not assert every byte of every admitted trace is public or
that future store snapshots are safe. `trace-card` uses `--no-ledgers`, as in
#1606. Export replies and cluster-scope manifests are scanned; blinded export
payloads are not treated as `qa-data` JSON replies. CLI parity covers read-only
commands, not filesystem exports. Production behavior is unchanged, so no new
regression test is appropriate unless the sweep finds a class-c defect.

## Results (store copy of 2026-10-28; `results.json`)

| | |
|---|---:|
| invocations over the whole admitted population (3,474 tasks, 4,544 runs, 68 clusters, 22 facet values, 15 command types) | 43,966 |
| replies / uniform refusals | 37,110 / 6,856 |
| seals | 1,425 |
| admitted tasks that contain a seal as a substring | 1 |
| **sealed identity itself or withheld text in a reply (class c) after review** | **0** |
| class b (admitted identity that contains a seal): occurrences, all on that one task | 22 |
| CLI vs in-process parity, 66 cases | 60 of 60 exact for ten command types; `recount` equal after a JSON round trip |

Class b by command and field: `traces` 4 (`task_id`), `history` 3 (`citation` 1, `task_id` 2), `task` 4 (`how_to_read_a_file` 2, `task_id` 2), `trace-card` 2 (`task_id`), `export-cluster-scope` 9 (`canonical_task_id` 3, `task_id` 6). Every other command (`split`, `runs`, `findings`, `window`, `inventory`, `clusters`, `recount`, `export`, `scan`) 0. The same task shows up everywhere: this is one identity, not 22 leaks.

The sweep's raw class c count was 2, both in `task.instruction`. Reviewed without printing text: two different admitted tasks whose instruction prose contains, as a whole word, a 7-letter seal that is an ordinary English word (present in the system word list) and not the name of any admitted task. A sealed task that is named with a common word is not exposed by that word appearing in prose. It is not a leak; it is a false positive of whole-blob substring matching (and the facet screen shares that property: it would over-withhold facet text containing that word, see `README` of #1606 for the policy question). Reported as class d.

Costs: dispatcher CPU per 1,000 invocations: `split` 17, `steps` 23, `window` 42, `task` 53, `trace-card` 55, `runs` 83, `traces` 84, `history` 80, `findings` 104, `clusters` 113, `export-cluster-scope` 120 s; `export` about 3,350 s per 1,000 invocations (up to 200 runs each, about 17 CPU-ms per exported run).

## Limits

`steps` produced 1,000 replies and 3,544 uniform refusals, so step content of 3,544 of the 4,544 runs was not scanned. [INFERENCE] The step-map seed is written by the ATIF export batches, which ran in a parallel process; it held joins for only part of the population when `steps` read it. `export` covers all admitted runs in the three formats in batches of 200 plus one run per facet value and format. Optional CLI arguments are not enumerated (see above). The word-list judgement for class d relies on the system dictionary only. One host sleep (about 8 h) interrupted the first attempt; the numbers above come from the later complete run.
