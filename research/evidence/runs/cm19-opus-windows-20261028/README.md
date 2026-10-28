# CM-19: CM-13's facet-cited windows re-judged by Opus 5.5

The CM-13 plan and its 418 unique Stage-2 prompts (arms A production windows, B facet windows, C facet-first; same system prompt, `build_window`
radius 5 / 9,000 characters, 3-window cap) are re-judged unchanged by `anthropic/claude-opus-5-5` (thinking low, subscription route, no tools,
fallback off, served model attested per call). The only change is the judge. DEV only: Goodfire 80 enacted / 70 clean, strict `assignments.csv`
cross-key before any facet, step or label is read. Preregistered in research-context `ed33906`.

- Adopt only if arm C reaches **recall >= 20/80 with <= 2/70 clean false alarms** (A and B reported alongside; adoption is read from C only).
- `tools/cm19_opus.py`: `prepare` (copies the CM-13 plan and prompts into a private state directory and re-checks the CM-13 sha256 pins), `run`, `score`
  (the unchanged CM-13 scorer on the Opus cache). `tools/compare_terra.py`: window- and run-level agreement with CM-13's terra judgments.
- `results/result.json` (CM-13 aggregate format; per-run rows keyed by a hash of the run id), `results/compare-terra.json`, `results/freeze.json` (the CM-13 freeze).
- Prompts and replies stay in `~/.local/state/evallab-prod-live/run-atlas/comparisons/cm19/` (outside git).
