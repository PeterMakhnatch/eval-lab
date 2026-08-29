# MCP Function-DAG Benchmark (`mcp-funcdag-v1`)

Harbor-native Campaign 0 family for **MCP tool selection, composition, and value propagation**.

Sidecar packaging is delegated to `evallab.mcp_substrate` (PR #268): real FastMCP 3.4.7, hash-locked wheels, task-local `workbench-internal` network, and `evidence-volume` (`main` RO / `mcp-service` RW at `/app/output`).

This tree keeps the DAG generator, Campaign 0 cells, verifier, mutants, and event/value-propagation contract.

## Materialize

```
uv run python scripts/mcp_funcdag/ensure_wheelhouse.py
uv run python library/benchmarks/mcp-funcdag-v1/materialize.py --cell baseline
```

Outputs are source-digest addressed under `derived/harbor-tasks/mcp-funcdag/` (gitignored).

## Controls

Oracle = 1, NOP = 0, wrong-order / wrong-value / distractor-trace / answer-only = 0.
Saturation (`CEILING_SATURATION` / `FLOOR_SATURATION`) is a later analysis-owner output, not a per-task contract field.
