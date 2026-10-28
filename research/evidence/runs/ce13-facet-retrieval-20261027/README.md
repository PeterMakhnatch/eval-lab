# CE-13: facet- and cluster-aware retrieval vs the lexical baseline

Measured 2026-10-27 (ET evening) on a read-only backup copy of the live cloud trace store. No model calls, Docker or spend.

## Protocol

`measure.py prepare` copies the live SQLite store with the online-backup API, imports only facet records whose model input was fully source-blinded (1,128 of 1,242; the 114 still awaiting the blinding refresh are excluded), and installs the latest Run Atlas cluster build trimmed to stored runs. 150 CheatBench action IDs have no stored trace and are refused by the importer. Receipt: `prepare.json`.

`measure.py measure` repeats the CE-4 leave-one-out protocol on `goodfire-reward-hack-data` (registry admission narrowed to that dataset, as the CE-4 single-source bundle was):

- one query per task: the same 8 instruction words as CE-4, top 5, query task excluded;
- precision = the returned trace shares a publisher category with the query task's non-provisional labels;
- baseline = a seeded random draw from the arm's own returnable pool;
- the query population is tasks with a faceted, clustered run and a labelled non-provisional trace: **104 queries, 520 judged results per arm**.

Facets are extracted blind (no labels, split or source name), so the publisher categories used for scoring were never visible to the facet extractor.

## Result (`results.json`; `reward_seeker` and `misalignment` are identical)

| arm | pool | precision | random in pool | paired Δ vs lexical (95% CI) | paired Δ vs lexical on faceted pool (95% CI) |
|---|---:|---:|---:|---|---|
| lexical (CE-4 baseline arm) | 1,609 | 0.798 | 0.652 | | |
| lexical, faceted pool only (`facets={}`) | 100 | 0.852 | 0.537 | +0.054 (+0.008, +0.102) | |
| `like_run_id`, empty query | 100 | 0.858 | 0.537 | +0.060 (+0.013, +0.104) | +0.006 (-0.040, +0.048) |
| lexical + `like_run_id` | 100 | **0.902** | 0.537 | **+0.104 (+0.062, +0.148)** | **+0.050 (+0.013, +0.088)** |
| `cluster_id` of the run's deepest cluster | 100 | 0.775 | 0.768 | -0.027 (-0.096, +0.041) | -0.075 (-0.143, -0.005) |
| filter on the run's own `tests` + `eval_awareness` values | 100 | 0.508 | 0.614 | -0.290 (-0.354, -0.227) | -0.344 (-0.416, -0.273) |

## Reading it

- Ranking by facet similarity to a known run, combined with lexical match, beats lexical alone by 10 points. 5 points of that survive when lexical runs on the same 100-run faceted pool, so it is not only the smaller pool.
- Part of the gain is pool selection: simply restricting lexical retrieval to faceted runs gains +5.4 points (CI excludes 0). Facet similarity adds a further +5.0 on top of that, but only together with the lexical match; similarity alone (empty query) is indistinguishable from lexical on the same pool.
- Cluster membership alone is chance level (0.775 vs 0.768 random in the cluster), and hard-filtering on two behavioral facets alone is *worse* than random. Those facets describe behavior, not the publisher's category, so they are for filtering ("pass with modified tests"), not for category-similarity ranking.
- This measures category agreement with publisher labels on 104 tasks of one dataset. It does not show the retrieved examples help a worker, and the faceted pool is 100 returnable runs, not the full 1,609 or the 4,544 admitted traces.
