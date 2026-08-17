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
  - `text` (string): constructed from task+agent+exception+phase for embedding
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
- "Find trajectories where the agent produced similar exception text to X"
- Nearest-neighbour on embedded text; returns distances + identifying columns.

Example DuckDB question belongs in `evallab.database` or direct SQL.
Example LanceDB question uses `python -m evallab.lance search "..."`

## Rebuild

```
python -m evallab.lance build --table all
python -m evallab.lance search "quick brown" --table tasks --k 5
```

Idempotent: re-running produces identical row counts, no duplicates.
Skips cleanly (with reason) when source data absent (no library tasks, no derived/parquet).

Run after changes to library/tasks or after new evidence promotion.

## Vector index

`create_index` uses the recommended form `create_index("vector", config=IvfPq(distance_type="cosine"))` (avoids deprecation warning on `vector_column_name` + `metric`).
Cosine is the correct metric because the embedder L2-normalises every vector to unit length; cosine distance then exactly captures angular similarity (equivalent to dot product on the unit sphere).

When a table has <256 rows LanceDB raises on index creation ("Not enough rows to train PQ"); this is caught specifically and reported as "index: skipped (too few rows for ANN index (exact brute-force search))". Search then falls back to exact brute-force scan over the table, which is the right behaviour at the current corpus size. The skip reason is printed by `build` so the user always knows whether ANN or exact search applies.

Skip reasons for trials now include the exact path examined (e.g. "no derived/parquet directory (/path/to/derived/parquet)"), making miscomputed paths visible immediately.
