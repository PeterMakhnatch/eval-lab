---
status: living
audience:
  - analyst
  - builder
---

# LanceDB vector store

LanceDB provides nearest-neighbour search over task instructions and trial text beside the DuckDB projections in `derived/`.

Store location: `<derived_root>/lance/` (gitignored, rebuildable).

## Storage zones and data flow

The store builds its tables by consuming data from three distinct sources according to the lab's storage zone architecture (§2.1, §2.2):

1. **Library supply (`library/tasks/`)**:
   - `tasks` table reads task definitions (`task.toml`) and instructions (`instruction.md`) directly from the task library.
2. **Zone 3 (Z3 Analytics Parquet)**:
   - `trials` and `steps` tables read structured trial metadata (`job_id`, `trial_id`, `job_name`, `trial_name`, `task_name`, `agent_version`, `primary_reward`, `exception_class`, `exception_phase`) exclusively via the **unified attach surface** (`evallab.storage.attach.attach`).
   - The attach surface resolves the hot `job_id=*/trial_id=*/` partitions, cold `compact/` partitions, and schema unification (`union_by_name=true`) without ad-hoc path globbing.
3. **Zone 1 (Z1 Evidence)**:
   - Step messages and trial trajectory texts (`steps[].message`) are loaded from raw ATIF trajectory files (`runs/<job>/<trial>/agent/trajectory.json`).
   - Raw trajectories are Z1 execution evidence, not derived Z3 analytics. The attach surface does not cover Z1 raw artifacts and should not. Trajectories are read directly from disk with candidate roots resolved via `evallab.storage.paths.shared_checkout_root`.
   - Missing trajectory files are counted and reported with concrete paths (non-fatal; trials fall back to task/agent/exception text).

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
- Same text -> identical vector in-process and across processes (asserted in tests).
- **Lexical overlap only, not semantics.** A query matches on shared words/tokens, not meaning. Do not treat distances as semantic similarity.

Real neural embedders can be swapped by implementing the `Embedder` protocol; callers remain unchanged.

## DuckDB vs LanceDB

DuckDB (structured aggregates):
- "Pass rate by verifier_type across all tasks?"
- "Count of trials with exception_class = 'TimeoutError' per agent?"
- Exact column filters, GROUP BY, joins on ids.

LanceDB (lexical similarity):
- "Which tasks read like this one?" (query on instruction text)
- "Find trajectories where the agent produced similar step reasoning or tool output to X"
- Nearest-neighbour on embedded text; returns distances + identifying columns (including reward and task for immediate interpretability).

Example DuckDB questions belong in `evallab.storage.attach` or direct SQL.
Example LanceDB questions use `python -m evallab.lance search "..."`.

## Rebuild and search

```sh
python -m evallab.lance build --table all
python -m evallab.lance search "quick brown" --table tasks --k 5
python -m evallab.lance search "remove javascript from html" --table steps --k 3
```

- Idempotent: re-running produces identical row counts, no duplicates.
- Skips cleanly (with reason naming exact examined path) when source data is absent or unavailable.
- Uses `list_tables()` on LanceDB connection for clean schema inspection.

## Vector index

`create_index` uses the recommended form `create_index("vector", config=IvfPq(distance_type="cosine"))`.
Cosine distance is used because the embedder L2-normalises every vector to unit length; cosine distance then captures angular similarity on the unit sphere.

An ANN index is built only when a table has >= 1000 rows (`MIN_ROWS_FOR_ANN = 1000`). Below this threshold (where exact brute-force search is fast and avoids LanceDB's small-dataset and empty-cluster warnings), the build skips `create_index` and reports `index: skipped (too few rows for ANN index (exact brute-force search))`. The same threshold policy is applied uniformly across tasks, trials, and steps.

## Runs root resolution

`runs/` (and `research/evidence/runs/` for promoted bundles) is resolved via `shared_checkout_root` from `paths.py` so linked worktrees find the primary checkout's machine-local `runs/`. Explicit override is supported via `--runs-root` CLI flag on `build` or `EVALLAB_RUNS_ROOT` environment variable. Per-root trajectory counts are printed on success so the source of each bundle is visible. Missing trajectories are counted and reported with their concrete path (non-fatal).
