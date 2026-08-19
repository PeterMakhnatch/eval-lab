# M033 GYM-DATA — cycle 1: Harbor-Index corpus

Status: complete — corpus PENDING, acquisition path investigated and documented
Last: probed every public route to the 1,476-trial harbor-index corpus; none of them
yields the trials. Landed the investigation, the binding contamination note, and
`tests/test_external_corpus.py` enforcing external-corpus discipline before any
corpus arrives.
Next: queue item 2, the llm-as-a-verifier **TB 2.1 trajectory corpus**
(`data/terminal_bench_2.1_trajs/`). Unlike harbor-index its repo is publicly
reachable (GitHub web + API both `200`), so it is the better next target with the
same discipline and the same contamination class.
Blockers: harbor-index trials need Hub credentials or an official export — a Peter
question (does the lab want a Harbor Hub account?), not a build task.

## The honest outcome: zero trials acquired

The brief expected "fetch via the pinned-acquisition path (fetch.py), verify
digests, land under `research/external/harbor-index/` … multiplies the lab's
trajectory holdings ~15× for free." The 82 **tasks** and the 1,476 **trials** turn
out to be different artifacts, and only the tasks are publicly distributable.

Every probe and its result is recorded in
`research/external/harbor-index/README.md`. Summary:

| Route | Result |
|---|---|
| `evallab fetch --list` | harbor-index absent from all 83 pins |
| `harbor-index.org/data/v1/{trials,trials/index.json,manifest.json}` | **404** each |
| per-trial page `…/trials/<slug>/` | 200, but client-rendered HTML: zero UUIDs, zero `/api/` or `_next/data` refs in 54 KB |
| `harbor hub leaderboard list --json` | works unauthenticated; 5 harbor-index leaderboards (1.0–1.4) |
| `harbor hub leaderboard show <uuid> --json` | **rows: 0** for 1.0, 1.3 and 1.4 |
| `harbor hub job list` | empty — no publicly visible jobs |
| `harbor hub trial download <slug>` | `Error: '…' is not a valid UUID` |

`harbor hub trial download` is the right transport; it needs trial **UUIDs** and no
public endpoint enumerates them.

Scraping 1,476 HTML pages was not attempted: it is not pinned acquisition, yields no
verifiable digests, and is not what `fetch ≠ register` means.

## Two things deliberately not built

- **No `fetch.py` pin** for a target that cannot fetch.
- **No `sql/external_views.sql`** view for rows that do not exist.

Both would be scaffolding that reads as capability — the exact pattern this repo
has spent the week removing. The lease covered them; the honest cycle did not need
them.

## What did land, and why it is worth more than an empty directory

`tests/test_external_corpus.py` encodes the discipline **before** the first corpus
arrives, so the first import cannot skip it:

1. every corpus directory has a README;
2. it states its contamination class in the README (with the data, not in a doc
   nobody opens);
3. it spells out `fetch ≠ register`;
4. a directory with no data files must declare itself `pending` — otherwise an
   unacquired corpus is indistinguishable from one whose files were lost;
5. unpinned refs (`@latest/@head/@main/@master`) are refused, and a version-less ref
   is refused.

Mutation evidence:

```
MUT 1 — remove the contamination wording from the README
FAILED test_corpus_states_its_contamination_class[harbor-index]

MUT 2 — reword "pending" to "acquired" on an empty corpus
FAILED test_unacquired_corpus_says_so_instead_of_looking_empty[harbor-index]
  - no data files present, so the README must mark the corpus pending

MUT 3 — make parse_pin accept @latest (if version.lower() in UNPINNED_VERSIONS -> if False)
4 tests failed

restored -> 11 passed
```

## Contamination note (binding, recorded with the data)

Public models' rollouts on public tasks: **behaviour-study material only, never
capability claims.** Rewards are their verifier's verdicts under their harness and
their timeout policy (1.2× fastest-model runtime, or 3h for tasks all models fail) —
elicitation and instrument choices, not neutral facts. **No reward recompute.**
Acquiring it would never register a task.
