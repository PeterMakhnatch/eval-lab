# Executable behavioral contracts and tests

## Purpose
`tests/` defines the executable contracts and verification suites for eval-lab.
It ensures that platform components, data pipelines, policy enforcement, and
task admissions function correctly and deterministically.

## What lives here / entry points
- `tests/conftest.py`: selects shard files before import and records collected or shard-skipped modules for the coverage contract.
- `tests/test_test_hygiene.py`: Checks bare module names, shard partitions and collection, and slow-test budgets.
- `tests/fixtures/`: Synthetic fixtures, mock data, and test environments.
- `tests/test_smoke.py`: Fast smoke verification suite for essential platform paths.
- `tests/test_governance.py`: Repository governance, naming, and structural checks.
- `tests/test_preflight.py`: Preflight assertions and safety boundary tests.

## Invariants or rules
- Strict determinism: Tests must be deterministic with injected seams: no host state, network calls, live clock, Docker dependencies, or sleeps (cited in `agents/CHECKS.md` and `AGENTS.md`).
- Fixture isolation: Tests use pytest temporary paths for temporary I/O; tests never write directly into repo directories (cited in `agents/CHECKS.md`).
- Default collection covers the `testpaths` in `pyproject.toml` (`tests/`, `research/analysis/tests/`, `research/experiments/tests/`, `research/experiments/ov1104-detbench/test_length_metrics.py`); use `-n0` and a module path for focused runs during development (cited in `agents/CHECKS.md`).
- Non-package directories from pytest `testpaths` and `pythonpath` must have distinct bare Python module names.
  The check excludes directories with `__init__.py` and ignores `conftest.py`.

## Tests or checks
- `uv run pytest tests/test_smoke.py`
- `uv run pytest tests/test_governance.py`
- `uv run pytest tests/test_preflight.py`
- `uv run pytest tests/test_test_hygiene.py`

## What not to add here
- Do not store actual production data, large corpora, or unredacted credentials here.
- Do not add tests that depend on network access, wall-clock sleeps, live Docker daemons, or host state (cited in `agents/CHECKS.md`).
- Do not place benchmark task evaluation suites here; task verifiers belong under `library/`.
