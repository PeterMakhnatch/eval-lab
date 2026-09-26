# CEO-Bench adapter

Bridge (`bridge.py`) converts one CEO-Bench harness run
(`bash_agent_runs/run_<id>/`: `config.json`, `checkpoint.json`, `world.nmdb`,
`logs/raw_responses_*.jsonl`, `logs/tool_results_*.jsonl`,
`logs/timing_*.jsonl`) into a Harbor-shaped trial directory that
`evallab report run` reads.

## Operator path

```bash
python -m library.adapters.ceo_bench <run_dir> --out <trial_dir>
uv run evallab report run <trial_dir> --output-dir <out_dir>
```

The first command prints the written trial directory (exit 1 with an
`error:` message when the run cannot be bridged); the second renders the
`evallab.run_report/v1` JSON and Markdown, including `## Domain: ceo_bench`.

## Outputs (trial dir)

- `result.json` — trial identity, agent token totals, agent cost only when the
- `agent/trajectory.json` — one step per tool call with per-turn tokens.
- `ceo_bench/*.json` — `meta`, `cash_daily`, `spend`, `forecasts`, `weeks`
  sidecars for the `ceo_bench` domain plugin (it never reads `world.nmdb`).

## Invariants

- Simulator-LLM spend is metered separately from agent spend, never merged.
- Bankruptcy is cash below $0 (upstream engine rule).
- No-op week: no state-changing tool call between completed week advances.
- Encrypted `world.nmdb` without `NMDB_KEY`/sqlcipher3 degrades to timing
  fallbacks with reasons, never fabricated zeros.

## Upstream

https://github.com/zlab-princeton/ceobench-src at the commit pinned in
`bridge.py` (`UPSTREAM_COMMIT`).
