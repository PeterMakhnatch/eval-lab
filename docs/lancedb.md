---
status: living
audience:
  - analyst
  - builder
---

# LanceDB vector store

LanceDB provides nearest-neighbour search over task instructions and trial text beside the DuckDB projections in `derived/`.

Store location: `<derived_root>/lance/` (gitignored, rebuildable).

## Table schemas

- `tasks`
  - `task_ref` (string): e.g. "eval-lab/library:foo/bar"
  - `instruction` (string): full instruction.md text
  - `vector` (list[float], 256-dim, L2-normalised)

- `trials`
  - `job_id`, `trial_id` (string)
  - `task_name`, `agent_version` (string)
  - `primary_reward` (float | null)
  - `exception_class`, `exception_phase` (string)
  - `text` (string): bounded concatenation of step messages from the trial's ATIF trajectory (first 8 + last 4 messages, truncated to 2048 chars total; falls back to task+agent when no trajectory)
  - `vector` (list[float], 256-dim, L2-normalised)

- `steps`
  - `job_id`, `trial_id`, `task_name` (string)
  - `step_id` (int | null), `source` (string)
  - `primary_reward` (float | null)
  - `message` (string): raw step message text from ATIF trajectory
  - `vector` (list[float], 256-dim, L2-normalised)

## Default embedder

`HashingEmbedder` (implements `Embedder` protocol): deterministic lexical only.

- Tokenises on `\w+`, lowercased.
- Stable MD5 hash into 256 buckets, count occurrences.
- L2-normalise.
- Same text → identical vector in-process and across processes (asserted in tests).
- **Lexical overlap only, not semantics.** A query matches on shared words/tokens, not meaning. Do not treat distances as semantic similarity.

Real neural embedder can be swapped by implementing the protocol; callers unchanged.

## DuckDB vs LanceDB

DuckDB (structured aggregates):
- "Pass rate by verifier_type across all tasks?"
- "Count of trials with exception_class = 'TimeoutError' per agent?"
- Exact column filters, GROUP BY, joins on ids.

LanceDB (lexical similarity):
- "Which tasks read like this one?" (query on instruction text)
- "Find trajectories where the agent produced similar step reasoning or tool output to X"
- Nearest-neighbour on embedded text; returns distances + identifying columns (including reward and task for immediate interpretability).

Example DuckDB question belongs in `evallab.database` or direct SQL.
Example LanceDB question uses `python -m evallab.lance search "..."`

## Rebuild

```
python -m evallab.lance build --table all
python -m evallab.lance search "quick brown" --table tasks --k 5
python -m evallab.lance search "remove javascript from html" --table steps --k 3
```

Idempotent: re-running produces identical row counts, no duplicates.
Skips cleanly (with reason naming exact missing path) when source data absent or trajectory missing on disk (non-fatal; count reported).
`table_names()` deprecation fixed to `list_tables()` for clean output.

Run after changes to library/tasks or after new evidence promotion.

## Vector index

`create_index` uses the recommended form `create_index("vector", config=IvfPq(distance_type="cosine"))` (avoids deprecation warning on `vector_column_name` + `metric`).
Cosine is the correct metric because the embedder L2-normalises every vector to unit length; cosine distance then exactly captures angular similarity (equivalent to dot product on the unit sphere).

When a table has <256 rows LanceDB raises on index creation ("Not enough rows to train PQ"); this is caught specifically and reported as "index: skipped (too few rows for ANN index (exact brute-force search))". Search then falls back to exact brute-force scan over the table, which is the right behaviour at the current corpus size. The skip reason is printed by `build` so the user always knows whether ANN or exact search applies.

Skip reasons for trials/steps now include the exact path examined (e.g. "no derived/parquet directory (/path/to/derived/parquet)"), making miscomputed paths visible immediately. Missing trajectories are counted and reported without aborting the build.
## Vector index

`create_index` uses the recommended form `create_index("vector", config=IvfPq(distance_type="cosine"))` (avoids deprecation warning on `vector_column_name` + `metric`).
Cosine is the correct metric because the embedder L2-normalises every vector to unit length; cosine distance then exactly captures angular similarity (equivalent to dot product on the unit sphere).

ANN index is built only when a table has >=1000 rows. Below this threshold (chosen because exact brute-force is correct and fast for corpus sizes of hundreds of rows, and to avoid LanceDB's "dataset too small (<65536)" and empty KMeans cluster warnings seen at 477 rows) the build skips `create_index` entirely and reports "index: skipped (too few rows for ANN index (exact brute-force search))". The same row-count decision is applied to tasks, trials, and steps.

## Runs root resolution

`runs/` (and `research/evidence/runs/` for promoted bundles) is resolved via `shared_checkout_root` from `paths.py` so linked worktrees find the primary checkout's machine-local `runs/`. Explicit override supported via `--runs-root` CLI flag on `build` or `EVALLAB_RUNS_ROOT` environment variable. Per-root trajectory counts are printed on success so the source of each bundle is visible. Missing trajectories are still counted and reported with their exact concrete path (non-fatal).

Skip reasons for trials/steps now include the exact path examined (e.g. "no derived/parquet directory (/path/to/derived/parquet)"), making miscomputed paths visible immediately. Missing trajectories are counted and reported without aborting the build.

