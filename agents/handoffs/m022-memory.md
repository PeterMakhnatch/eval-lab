Status: complete
Last: Implemented, proven, and hardened LanceDB `analyses` vector table indexing analyst conclusions with joinable identity, deterministic hashing embeddings, overwrite semantics, full skip reporting, corrupt sidecar surfacing, and CLI build/search wiring.
Next: Wire `evallab.lance` search into researcher/analyst loops for failure memory retrieval.
Blockers: None.

# M022: MEMORY-ANALYSES — Index Analyst Conclusions into LanceDB

## Summary
Implements the `analyses` table in LanceDB alongside existing `tasks`, `trials`, and `steps` vector tables. Analyst conclusions are read from durable Parquet projections (`derived/analyses/analyses.parquet` or `derived/parquet/analyses/analyses.parquet`) with fallback to JSON sidecars, carrying full joinable identity (`analysis_id`, `trial_id`, `job_id`, `model`, `category`, `created_at`, `conclusion`, `vector`). Rows can be completed from `research/analysis`, `research/evidence/analyses`, or `derived/analyses` JSON when parquet fields are blank, which is a second data path and readers deserve to know which source a row came from.

## Honest Corpus Reality
1. The repository currently contains zero analysis sidecars under `research/evidence/analyses/` and no production `analyses.parquet` under `derived/` (only 3 unit-test fixtures under `tests/fixtures/explorer/analyses/`).
2. `lance.py` currently has no callers anywhere in `src/`, `scripts/`, `dashboard/`, or `cli.py`, so this capability is reachable only via `python -m evallab.lance` until someone wires it into the researcher loop.

## Verification & Real CLI Execution

### 1. Build against Real Repo Data
```
$ uv run python -m evallab.lance build --table analyses
evallab: derived root /Users/petermakhnatch/Developer/eval-lab/derived/parquet belongs to /Users/petermakhnatch/Developer/eval-lab, not to this checkout /Users/petermakhnatch/Developer/eval-lab/.worktrees/m022-memory; set EVALLAB_DERIVED_ROOT to an absolute path to choose another.
analyses: skipped (analyses.parquet not found (/Users/petermakhnatch/Developer/eval-lab/derived/parquet/analyses/analyses.parquet))
```
Row count: 0 rows (source Parquet does not exist in live environment; handled gracefully per existing builder conventions).

### 2. Search against Real Repo Data
```
$ uv run python -m evallab.lance search --table analyses "agent error"
evallab: derived root /Users/petermakhnatch/Developer/eval-lab/derived/parquet belongs to /Users/petermakhnatch/Developer/eval-lab, not to this checkout /Users/petermakhnatch/Developer/eval-lab/.worktrees/m022-memory; set EVALLAB_DERIVED_ROOT to an absolute path to choose another.
table analyses not found
```

## Mutation Evidence

### Mutation 1: Break Idempotence
Mutating `db.create_table("analyses", mode="overwrite")` to accumulate rows across rebuilds:
```python
# Mutated:
tbl = db.open_table("analyses")
tbl.add(rows)
```
Test failure output (`uv run pytest tests/test_lance.py -k test_build_analyses_idempotent`):
```
FAILED tests/test_lance.py::test_build_analyses_idempotent - assert 2 == 1
 +  where 2 = len(pyarrow.Table
    analysis_id: string
    trial_id: string
    job_id: string
    model: string
    category: string
    created_at: string
    conclusion: string
    vector: fixed_size_list<item: float>[256]
    ...
```

### Mutation 2: Break Identity Requirements
Mutating `_build_analyses` to skip validation of required identity fields (`trial_id`, `job_id`, `model`, `created_at`, `conclusion`):
```python
# Mutated:
if not analysis_id:
    skipped.append(analysis_id or "unknown")
    continue
```
Test failure output (`uv run pytest tests/test_lance.py -k test_build_analyses_skips_missing_required_identity_fields`):
```
FAILED tests/test_lance.py::test_build_analyses_skips_missing_required_identity_fields - AssertionError: assert 'analyses: 1 rows' in 'analyses: 6 rows\nanalyses index: skipped (too few rows for ANN index (exact brute-force search))\n'
```

### Mutation 3: Silence on Partial Skips
Mutating `_build_analyses` to suppress surfacing skipped items in stdout when rows are indexed:
```python
# Mutated:
return n_rows, None, index_reason
```
Test failure output (`uv run pytest tests/test_lance.py -k test_build_analyses_skips_missing_required_identity_fields`):
```
FAILED tests/test_lance.py::test_build_analyses_skips_missing_required_identity_fields - AssertionError: assert 'analyses: 1 rows (6 skipped: unknown, A_NOTRIAL, A_NOJOB, ...)' in 'analyses: 1 rows\nanalyses index: skipped (too few rows for ANN index (exact brute-force search))\n'
```

### Restoration
Restored `mode="overwrite"`, strict 6-field identity check, and full skip surfacing. All 23 tests in `tests/test_lance.py` pass.

## Integrator verification (independent of the authoring agent)

### Review finding that was sent back and fixed

The first version reported skipped rows **only when every row was skipped**. Its own
test seeded 1 valid and 6 invalid rows and asserted only `analyses: 1 rows`, so six
rows vanished with no output and the test locked that silence in. This is the same
defect class the lab fixed last night in `status_generator.py`, where an empty result
was silently replaced by a different dataset. Now fixed and tested:

```
$ uv run pytest tests/test_lance.py -q      # skip reporting mutated to None
FAILED tests/test_lance.py::test_build_analyses_skips_missing_required_identity_fields
  - assert 'analyses: 1 rows (6 skipped: unknown, A_NOTRIAL, A_NOJOB, ...)' in 'analyses: 1 rows\n...'
FAILED tests/test_lance.py::test_build_analyses_surfaces_corrupt_sidecar_json
  - assert '1 skipped:' in 'analyses: skipped (no valid analyses rows (...))'
restored -> 23 passed
```

Also fixed: the `trial_id -> job_id` catalog read swallowed every exception with
`except Exception: pass`. A failed catalog read then presented as "rows missing
identity", i.e. a data problem rather than the infrastructure problem it was. It now
prints `analyses: trial->job map unavailable (<error>)`.

An independent mutation of the identity guard (disabling it entirely) turns
`analyses: 1 rows` into `analyses: 7 rows` and fails the guard test, so the guard is
real and not decorative.

### What this actually does on today's repository

```
$ uv run python -m evallab.lance build --table analyses
analyses: skipped (analyses.parquet not found (.../derived/parquet/analyses/analyses.parquet))
$ ls research/evidence/analyses/ | wc -l
0
```

**Zero analyses exist**, because no analyst has ever run with a real model
(`ModelAnalyzer.analyze()` refuses without `--model` by design). This mission closes
the structural gap; it cannot be exercised on real analyst output until that spend
decision is made.

To prove the query itself works rather than only the tests, the table was built from
**four real trial and job identities** taken from this repo's own `trial_facts`
parquet, paired with four conclusions written by hand (labelled
`model=constructed-no-analyst-has-run`, in a throwaway `EVALLAB_DERIVED_ROOT`, never
committed):

```
$ uv run python -m evallab.lance build --table analyses
analyses: 4 rows
analyses index: skipped (too few rows for ANN index (exact brute-force search))

$ uv run python -m evallab.lance search --table analyses "local time versus UTC day boundary"
dist=0.7190 ... 'Daily spend buckets were four hours off because the dashboard used local time while quota accounting normalises to UTC.'
dist=0.9063 ... 'Trial consumption bucketed timezone-aware timestamps by local date, the same defect surviving in a second location.'
dist=0.9199 ... 'The status surface fell back to an unfiltered filesystem scan when the catalog returned no trials for the reporting day.'
dist=1.0000 ... 'Worktree staleness was decided by commit ancestry, which is never true in a repository that squash-merges.'
```

Ranking is correct and the rows join back to real trials and jobs.

### Two caveats a reader must not miss

1. **The default embedder is deterministic lexical hashing, not semantic** — the module
   docstring says so itself ("no semantics"). So "find analyses similar to this one" is
   word-overlap similarity today. The ordering above is real but it is lexical; a
   genuine semantic embedder is a separate, unmade decision.
2. **`lance.py` has no callers in `src/`, `scripts/`, `dashboard/` or `cli.py`.** It is
   reachable only as `python -m evallab.lance`. Indexing analyses does not make the
   vector memory part of any automated path — wiring it is a separate mission.
