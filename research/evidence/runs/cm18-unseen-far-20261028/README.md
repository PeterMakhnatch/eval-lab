# CM-18: first false-alarm rates on unseen staged cross-test sets

Cascade v8, Run Atlas facets (Haiku 5.5 reader, `suspicion >= 2`) and their union, each run once and unchanged on 946 runs drawn from seven LB-13 run-level
cheating sets with real negatives (KernelBench-Hard, Reflex-Sentinel, Agent Cheat Index, EvilGenie, SDF shortcut-taking, ResearchArena, Beam tripwire).
Design: research-context `causal-prefix-monitor.md`, CM-18 (preregistered in `2bfff9f`). Results: `RESULTS.md`.

- `tools/cm18_unseen.py`: `sample`, `freeze`, `cascade`, `facets`, `score`. Adapters are the pq1/pq2 corpus adapters; no adapter was changed.
- `results/sample.json` (pool and drawn counts per set, hash of the sampled keys), `results/freeze.json`, `results/score.json` (aggregates only).
- Runs, per-run alarms, facet prose and judge replies are kept in `~/.local/state/evallab-cm18/` (local, outside git, never uploaded). Receipts here hold counts and rule names only.
