# on-a3-rounds-20261102

Gold-certifier feedback rounds (A3 of the 2026-11-02 overnight run): `evallab.gold_certifier.run_rounds` feeds round r+1 the fresh probe mutants that survived round r's synthesized test, never an eval mutant. Preregistered in [`prereg.md`](prereg.md) before any synthesis call or Modal cell.

- `run_rounds.py`: stages `prepare` (local), `base`, `rounds`, `gates`, `status`; synthesizer `google-antigravity/gemini-3.8-flash` through `omp -p`, cells through `modal_cell_runner`.
- `analyze_rounds.py`: primary and secondary comparisons with `stats.paired` (exact McNemar from the discordant counts); writes aggregates only to `results/summary.json` and `results/per-task.md`, plus `results/manifest.json` (sha256 of the local row-level files). Per-cell and per-mutant rows (`cells.csv`, `summary-rows.json`) stay under `~/Developer/eval-lab-results/2026-11-02/A3/` because the tasks are third-party packages.
- `results/selection.json`: the fixed mutant sets (operator, line, hash only; no diffs).

Task prompts, synthesized tests and cell logs stay under `~/Developer/eval-lab-results/2026-11-02/A3/` (third-party task content); the report is `research-context/bridge/2026-11-02-overnight/lanes/A3-report.md`.

Needs the lab venv first on PATH (`uv run --extra laminar`) and `EVALLAB_DISABLE_LOCAL_DOCKER=1`.

These scripts are Peter-machine-only: they hard-code `/Users/petermakhnatch/...` paths (results home, the overnight bridge for `stats.py`, `worker.yml` copy) and need Modal and the subscription model route.
