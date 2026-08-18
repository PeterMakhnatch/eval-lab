Status: complete
Last: Implemented, proven, and hardened LanceDB `analyses` vector table indexing analyst conclusions with joinable identity, deterministic hashing embeddings, overwrite semantics, and CLI build/search wiring.
Next: Wire `evallab.lance` search into researcher/analyst loops for failure memory retrieval.
Blockers: None.

# M022: MEMORY-ANALYSES — Index Analyst Conclusions into LanceDB

## Summary
Implements the `analyses` table in LanceDB alongside existing `tasks`, `trials`, and `steps` vector tables. Analyst conclusions are read from durable Parquet projections (`derived/analyses/analyses.parquet` or `derived/parquet/analyses/analyses.parquet`) with fallback to JSON sidecars, carrying full joinable identity (`analysis_id`, `trial_id`, `job_id`, `model`, `category`, `created_at`, `conclusion`, `vector`).

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

### Restoration
Restored `mode="overwrite"` and strict 6-field identity check. All 22 tests in `tests/test_lance.py` pass.

## Premerge Validation
```
Resolved 75 packages in 3ms
Audited 51 packages in 1ms
All checks passed!
1324 passed, 2 skipped, 1 xfailed in 86.48s (0:01:26)
PASS doctor mode=docker-free
PASS submit->tick job=smoke-oracle-v0fnd3rhgxeg trials=1
PASS catalog job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS parquet job_id=886e92a2-0de4-4384-b7ad-aa8c623e96b1
PASS digest path=runs/_smoke/smoke-oracle-v0fnd3rhgxeg/digests/2026-08-18.md
PASS analysis sidecar=runs/_smoke/smoke-oracle-v0fnd3rhgxeg/analyses/54fad702-b0dc-4bb4-a847-571c2faf5138/analysis.json validation=valid
PASS status snapshot sections=Recent,Now,Next,Tasks,Health,Analysis analysis=draft
SMOKE PASS both-stores-agree
Found 28 diagnostics
premerge green: Python 3.12; ty 28 <= 28
```
