# pG-SELF-dd3185-2: does an uncovered cheat cluster remain to turn into a tripwire?

**Result: no. After the measured input-shape repair there is no uncovered DEV cluster, so no tripwire was derived, implemented or cross-tested.**
No model calls, no paid calls, no Docker, no local inference. Every identity passed native admission (`CorpusRegistry` +
`admitted_cloud_task_ids`, strict `assignments.csv` digest `7f4703f6…`) before any label or body was read. Selection rule, unchanged
from TW-12: strict L1 (at least 5 labelled, at least 50% positive, current Stage-1 recall at most 20%) and relaxed L2 (5, 40%, 40%).
Aggregates and cluster rows are in `results/result.json`; member IDs, labels, facet text and actor text stay in the private evidence root.

## Current production Atlas build (`b4bb6537…`)

The stored RA-4 report named two newly qualifying L2 groups. Both leaned on CheatBench cross-test labels: `L1-03.2` had 9 labelled members,
all 9 CheatBench; `L1-01.1` had 6, 4 of them CheatBench. In DEV tuning scope they have 0 and 2 labelled members. With CheatBench removed,
the build has 1,145 admitted members (12/21/35 clusters). A fresh replay of the current Stage-1 rules on its 272 labelled runs
(200 positive, 72 clean) hits 181/200 positives and 34/72 clean. **No cluster qualifies.**

## The larger frozen facet store

The facet store holds 2,704 unique DEV facets, far more than that build clustered. I clustered the 2,554 tuning-source facets
(Goodfire 1,731, MiMo 732, TB4 91; CheatBench's 150 excluded) with the native, label-free clusterer (hashing embedder, seed 0,
min size 5): 12/20/33 clusters. Labelled 1,681 (1,609 positive, 72 clean, 50 considered-only left out). Current Stage-1 hits
1,541/1,609 positives (95.8%) and 34/72 clean.

**One L2 group qualified: `pg2:L1-11.2`, 5 labelled positives, 0 clean, 129 unjudged MiMo/TB4 members, Stage-1 recall 2/5 = 40%, exactly at
the ceiling.** It is the only group at any level that comes near the criteria; before the repair below, the next-lowest recall among groups
with at least 5 labelled members and 40% positives is 50% (its parent `pg2:L1-11`, 6 labelled), then 81.8%.

## What the five positives showed

Three read-only workers read the five admitted positives (all observed `openai-codex/gpt-6.1-terra`; one worker reported truncated large
single-line reads). They found no shared code-level mechanism. Case 2 (builtins injection) was already a Stage-1 hit. Case 5 swapped an
exact algorithm for a test-fitted loop that returns a partial count when stuck: one candidate predicate, from one case. Cases 3 and 4
are final-report claims ("6 independent implementations", "cross-checked all expected values"), which need a semantic check, not a
cheap code predicate. An adversarial worker advised retaining nothing.

## The input-shape confound decides it

Case 5's code was written through a shell `cmd` argument, a shape main does not read. Applying only the two measured extractor files of the
open PR #1371 (`edit_signals.py` b425fe99…, `tripwires.py` 714969c5…) to a measurement tree and replaying the same 1,681 labelled runs:
positive hits 1,541 → 1,547 (+6), clean hits 34 → 47 (+13), no run lost. Case 5 is now covered and `pg2:L1-11.2` recall rises
40% → 60%, so **no group qualifies at the predeclared rule, at native strict thresholds on any level, or even at 40%/40% on any level**.

## Where the remaining misses are

62 of 1,609 labelled positives (3.9%) are still uncovered, spread inside well-covered groups: `L1-03.2` 21 of 171, `L1-09.1` 19 of 213,
`L1-05.1` 9 of 58, `L1-10.1` 4 of 123, 9 more across other groups. That is a residual-positive question, not an uncovered-cluster one;
no threshold was relaxed to chase it.

## Not run, and limits

No rule was implemented, so there is no precision/recall/clean-FAR/marginal-TP/latency table, and the unchanged CheatBench/HackBench
cross-test was not run (the three measurement frames were freshly admitted: 3,762 / 150 / 169, with no scan). Limits: only 72 runs are
label-clean in the Atlas join (MiMo/TB4 are unjudged, not clean); "covered" means any Stage-1 hit, not one named rule; one deterministic
clustering (seed 0), so a different embedder could form other groups; the repair comparison uses PR #1371 bytes that are not on main; a
worker reported printing two cluster records (names, centroid text, samples) in a malformed-JSONL error before admission was refreshed,
which the parent did not use, and this is not an exhaustive read audit. Tool copies differ from the executed scripts only by two lint-only
edits (an unused import; one `%` format); executed hashes are in `results/result.json`.
