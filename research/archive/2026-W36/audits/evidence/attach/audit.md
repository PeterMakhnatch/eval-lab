# Audit Evidence: Attach Surface (E04)

Handoff: `agents/handoffs/e04-attach.md`
Subject: `src/evallab/attach.py`

## 1. Test Suite Verification
Command: `uv run pytest tests/test_attach.py -v`
Output:
```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 12 items

tests/test_attach.py ............                                        [100%]

============================== 12 passed in 1.17s ==============================
```

## 2. CLI Zone Discovery Verification
Command: `uv run evallab db attach --zones`
Output:
```
evallab: derived root /Users/petermakhnatch/Developer/eval-lab/derived/parquet belongs to /Users/petermakhnatch/Developer/eval-lab, not to this checkout /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit; set EVALLAB_DERIVED_ROOT to an absolute path to choose another.
z2: attached (localhost:54329/evallab)
z3: attached (/Users/petermakhnatch/Developer/eval-lab/derived/parquet (8/9 tables); missing: jobs (intentionally shaped differently))
z4: attached (/Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit/docs)
```

## 3. SQL Query Execution Across Zones
Command 1 (Z4 Doc Front-Matter): `uv run evallab db attach --query "SELECT count(*) FROM z4.front_matter"`
Output:
```
(76,)
```

Command 2 (Z3 Parquet Trial Facts): `uv run evallab db attach --query "SELECT count(*) FROM trial_facts"`
Output:
```
(92,)
```

## Verdict
CONFIRMED.
DuckDB attach surface mounts Z2 (PostgreSQL catalog), Z3 (DuckDB Parquet views over hot + cold tables), and Z4 (front_matter parser over docs/) cleanly. Degrades honestly when services/directories are missing.
