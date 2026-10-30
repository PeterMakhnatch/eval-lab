# ds-tq2-hook-hardening — hardening recipe for the pytest-hook grader family

A short shell recipe for the MiMo grader shape (tests run in the agent's container, reward = the
test command's exit status, `tests/test.sh` resets only an enumerated list of test files), tested
against a local toy, plus the static re-scan of the 32 DEV packages TQ-1 flags FCI/RES.

## Contents

| path | what |
|---|---|
| `hardened-test.sh-template.sh` | the recipe. Cumulative layers L1–L4, `HARDEN_LEVEL=0..4`; the same file serves as the L0 control. Package-specific inputs: `M`, `CWD`, `V`, `TESTS`, the reset heredoc, `expected_tests.txt`, `JUNIT_REQUIRED`. |
| `toy/base/` | toy package: flat `toycalc/` with stubs, one visible test, a git base commit is created by the runner |
| `toy/tests/` | hidden test patch (3 tests), hidden command, reference test count (`expected_tests.txt` = 4) |
| `toy/reference/`, `toy/wrong/` | correct and wrong implementations |
| `toy/attacks/<id>/` | attack payloads A, B, C, C_pp, D, E1, E2, F1, F2, F3 |
| `matrix.py` | runs every (scenario, layer) cell in a fresh copy under `/tmp/tq2`; writes `results/attack-matrix.json` |
| `harden.py` | generates the hardened `tests/test.sh` for copies of the FCI/RES packages under `/tmp/tq2/copies`, re-runs TQ-1's scanner there; writes `results/patched-copy-scan.json` |
| `results/` | both receipts |

## Run

```bash
/Users/petermakhnatch/Developer/eval-lab/.venv/bin/python matrix.py   # ~20 s, local toy only
/Users/petermakhnatch/Developer/eval-lab/.venv/bin/python harden.py   # ~3 s, copies + static scan
```

Neither executes anything from a DEV package: `harden.py` copies a package, replaces its
`tests/test.sh`, and runs the scanner (which decodes tarballs in memory and executes nothing).

## Layers

- **L1** restore/delete every `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `pytest.ini`,
  `tox.ini`, `setup.cfg`, `pyproject.toml`, `*.pth` in the tree from the base commit.
- **L2** `PYTHONNOUSERSITE=1`, `PYTHONSAFEPATH=1`, unset `PYTHONPATH`/`PYTHONSTARTUP`/`PYTHONHOME`;
  direct `python -m pytest` runs through a grader-owned launcher that puts the workdir back on
  `sys.path` after start-up.
- **L3** `--junitxml` to a grade-time path; reward 1 only if the report exists, has at least
  `expected_tests.txt` tests, and 0 failures/errors/skips. When the runner is an opaque wrapper,
  `PYTEST_ADDOPTS` requests the report from any child pytest.
- **L4** grade in a tree rebuilt from `git archive <base>` plus the agent's changed non-test,
  non-config source paths.

Results and limits: see the item report
(`research-context/bridge/2026-10-29-ds-squad/raw/TQ-2/report.md`).
