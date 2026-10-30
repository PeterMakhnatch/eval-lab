# h1-grader-hardening-20261030

Executed validation of `grader-hardening@1` (`src/evallab/grader_hardening.py`, registered in `evallab.hardening`) on real
MiMo DEV packages, on Modal. Preregistered in [`prereg.md`](prereg.md) before any matrix cell.

- `attacks.py`: agent-side scripts for the correct solution, an empty submission and the TQ-2 attack families (A, B, C, D, E2, F1, F2, F3).
- `run_matrix.py`: selection (strict DEV, reference vendored, pytest runner whose wrapper keeps `PYTEST_ADDOPTS`), calibration of the
  reference test count, then L0 (original grader), L1 (auto-load reset only) and H (L1 + L3) cells through `ModalBackend`'s shortcut channel
  and each package's native `tests/test.sh`. Needs the lab Harbor: `uv sync --extra laminar`, `PATH=$PWD/.venv/bin:$PATH`.
- `analyze.py`: layer matrix from the per-cell results; writes `results/matrix-summary.json`.

Raw cell evidence (logs, reports) lives outside the repository under `eval-lab-results/2026-10-30/h1-grader-hardening/`. The report is
`research-context/bridge/2026-10-30-factory/lanes/H1-report.md`.
