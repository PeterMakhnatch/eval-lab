# MCP Function-DAG Benchmark (`mcp-funcdag-v1`)

## Overview
`mcp-funcdag-v1` is a Harbor-native benchmark evaluating agent capabilities in **MCP tool selection, composition, and value propagation** over typed dependency graphs.

Unlike older benchmarks that expose whole Python scripts or command-line wrappers, `mcp-funcdag-v1` exposes discrete compute nodes and distractors as genuine **streamable-HTTP MCP tools** conforming to Harbor workbench v2 specifications (`streamable-http` transport).

## Key Characteristics
1. **Discrete Streamable-HTTP MCP Runtime**: Each DAG node computation and distractor is served as an independent MCP tool endpoint (`http://mcp-server:8000/mcp`).
2. **Canonical Event Ledger**: Deterministic raw events are recorded at `/app/evidence/benchmark-events.jsonl` with monotone event indexes, argument validation, tool results, and execution traces without wall-clock timestamps.
3. **Verifier-Only Truth**: The required topological execution order, ground-truth dependency edges, and expected intermediate node values are strictly isolated in `tests/verifier_truth.json` and verifier environments, with zero leakage to the main agent image or workspace.
4. **Campaign 0 Calibration Cells (One Factor At A Time)**:
   - **Depth Ladder**: 2, 3, 4
   - **Width Ladder**: 2, 3, 4
   - **Distractor Ladder**: 0, 2, 5 distractors
   - **Distractor Lexical Similarity**: low vs high
   - **Schema Token Volume**: concise vs verbose descriptions
   - **Schema Drift Twin**: clean vs drifted schema signatures
5. **Deterministic Verification & Non-Saturated Bands**:
   - Oracle = 1.0 (requires full schema conformance, DAG topological adherence, and 100% intermediate value propagation accuracy).
   - NOP = 0.0
   - Mutants (wrong-order, wrong-value, distractor-trace) = 0.0

## Directory Structure
- `dag_generator.py`: Seeded deterministic typed DAG and distractor synthesis.
- `contract.py`: Benchmark contract definitions, opportunity counts, and Campaign 0 calibration grid.
- `runtime.py`: Streamable-HTTP JSON-RPC MCP server runtime and canonical event logger.
- `templates.py`: Oracle solver, NOP control, and adversarial mutant implementations.
- `verifier.py`: Ground-truth verification, metric extraction, and oracle-leak exclusion gate.
- `materializer.py` & `materialize.py`: Materializes source-digest addressed Harbor tasks under `derived/harbor-tasks/mcp-funcdag/`.
- `ci_contract.py`: Deterministic regeneration and control assertions.
