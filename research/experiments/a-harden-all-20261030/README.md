# a-harden-all-20261030

Phase 2 stream A: `grader-hardening@1` (L1 auto-load reset + L3 JUnit gate) classified and executed on **every** `format-code-task-*` package of the MiMo DEV set, not the 12-task H1 sample. Preregistered in [`prereg.md`](prereg.md) before any A2/A3 cell.

- `classify.py` (A1, local): `results/classification.{json,md}`: strict DEV, reference patch, `tests/test.sh` shape, runner, `PYTEST_ADDOPTS` handling, class full / l1-only / not-hardenable.
- A2: `../h1-grader-hardening-20261030/run_matrix.py --run --all --out-name a-harden-all` (27 full-eligible tasks x L0/L1/H x 10 arms); `analyze_all.py --mode all` writes `results/all/{summary.json,per-task.md,cells.csv}`.
- A3: `run_matrix.py --run --l1-only --out-name a-harden-l1` (8 L1-only tasks x L0/L1 x correct, empty, A-D); `analyze_all.py --mode l1` writes `results/l1/`.

Raw cell evidence lives outside the repository under `eval-lab-results/2026-10-30/a-harden-{all,l1}/`. Report: `research-context/bridge/2026-10-30-factory/lanes/A-report.md`.
