# Handoff: MCP Function-DAG Benchmark (`mcp-funcdag-v1`)

## Overview & Scope
`mcp-funcdag-v1` implements a Harbor-native benchmark for assessing agent performance on **MCP tool selection, composition, and value propagation** over typed dependency graphs. DAG nodes and distractors are served as genuine streamable-HTTP MCP tools (`streamable-http` transport).

## Delivered Components
1. **Benchmark Package**: `library/benchmarks/mcp-funcdag-v1/`
   - `dag_generator.py`: Deterministic typed DAG generator with configurable depth, width, distractors, lexical similarity, doc volume, and schema-drift twins.
   - `contract.py`: Versioned benchmark contract, opportunity count formulas, and Campaign 0 calibration cells.
   - `runtime.py`: Streamable-HTTP JSON-RPC MCP server runtime logging deterministic event records to `/app/evidence/benchmark-events.jsonl` and `/app/evidence/final-state.json`.
   - `materializer.py` & `materialize.py`: Materializes source-digest addressed Harbor tasks under `derived/harbor-tasks/mcp-funcdag/` with complete `task.toml`, `instruction.md`, Dockerfile, docker-compose.yaml, solution solver, and test verifiers.
   - `templates.py`: Oracle solver, NOP control, and 3 adversarial mutants (wrong-order, wrong-value, distractor-trace).
   - `verifier.py`: Ground-truth verifier enforcing DAG topological ordering, 100% intermediate value propagation accuracy, schema conformance, and the in-container oracle-leak exclusion gate.
   - `ci_contract.py`: Deterministic regeneration assertion, corpus tracking guard, and oracle/NOP/mutants evaluation.
2. **CLI Helpers**: `scripts/mcp_funcdag/`
   - `materialize.py`: Materializes calibration cells or custom parameter configurations.
   - `run_calibration.py`: Executes oracle and NOP controls across the 10 Campaign 0 calibration cells.
3. **Automated Tests & CI**:
   - `tests/test_mcp_funcdag_v1.py`: Unit and integration test suite covering generator, contract, runtime, events, materializer, and verifiers.
   - `.github/workflows/mcp-funcdag.yml`: Dedicated GitHub Actions workflow executing corpus guard, CI contract, calibration grid, and pytest suite.

## Campaign 0 Calibration Matrix
Varies one factor at a time against the baseline (`depth=3, width=2, distractor_count=2, name_similarity=low, schema_token_volume=concise, schema_drift=False, seed=42`):
- `depth_2` / `depth_4` (depth ladder)
- `width_3` / `width_4` (width ladder)
- `distractors_0` / `distractors_5` (distractor density ladder)
- `name_similarity_high` (adversarial lexical distractor naming)
- `schema_tokens_verbose` (schema doc pressure)
- `schema_drift_twin` (clean twin with drifted schema signatures)

## Future Feature-Producer Inputs
The canonical `/app/evidence/benchmark-events.jsonl` provides raw facts for downstream analysis producers:
- `tool_selection_entropy`: distribution over chosen vs unchosen tools across layers.
- `schema_conformance_rate`: valid invocations / total tool call events.
- `value_propagation_accuracy`: valid intermediate outputs supplied to downstream tools / required DAG nodes.
- `dag_conformance`: Boolean topological order satisfaction.
- `redundant_call_rate`: redundant calls / total tool calls.
