# `evallab/syn-funcdag-medium`

This task measures deterministic function-DAG evaluation and exact dependency tracing for target node `n_4_0`.
The agent must report the integer result and only the target's required dependencies.
See [instruction.md](instruction.md).

## Environment

The agent image uses the pinned `python:3.13-slim` base in `environment/Dockerfile`.
It starts in `/app` with an empty `/app/output` directory.
The image provides these fixtures:

- `/app/src/inputs.json`: root input values.
- `/app/src/dag_spec.json`: graph operations, parameters, and dependencies.
- `/app/src/dag_code.py`: generated Python functions for the graph.

| Setting | Value |
|---|---|
| CPUs | `1` |
| Memory | `512` MB (`memory_mb`) |
| Storage | `1024` MB (`storage_mb`) |
| Agent timeout | `60` seconds |
| Image build timeout | `120` seconds |
| Agent network mode | `public` |
| Metadata `construct_name` | `function_dag` |
| Graph depth | `4` |
| Graph width | `3` |
| Connected distractors | `2` |
| Disconnected distractors | `0` |
| Target node | `n_4_0` |

`task.toml` records `network_isolation_enforced = false` and the reason `darwin-docker-cannot-enforce-no-network`.
The task has no network dependency, but Docker Desktop on macOS cannot enforce Harbor's agent `no-network` policy.
Do not treat the `public` agent environment as network-isolated.

The agent writes `/app/output/result.json`, the only declared artifact.
Its exact schema contains `target`, `value`, and `dependency_trace`.
The trace must include each required node once, in topological order, with ordered inputs and exact integer values.
It must exclude unrelated nodes and extra fields.

## Verifier

`task.toml` sets `environment_mode = "separate"`, a `60`-second verifier timeout, and verifier `network_mode = "no-network"`.
`tests/Dockerfile` builds a separate image from the same pinned `python:3.13-slim` base.
It copies `golden.json`, `verify.py`, and `test.sh` into `/tests`.
The verifier receives the declared result artifact, and `/tests/test.sh` runs `python /tests/verify.py` in that image.
The verifier compares the artifact with the trusted `GOLDEN` object embedded in `verify.py`.

| Check | Requirement |
|---|---|
| Result file | `/app/output/result.json` exists, is nonempty, and contains valid JSON |
| Top-level schema | A JSON object with exactly `target`, `value`, and `dependency_trace` |
| Exact result | Target, value, and trace match the trusted result |
| Exact trace | Node order, ordered inputs, values, field sets, and JSON types match exactly |

The verifier writes `/logs/verifier/reward.txt`: `1` when all checks pass, otherwise `0`.
It also writes `/logs/verifier/result.json` with `passed`, `reason`, and details for the relevant check.
Expected rewards are `1` for Oracle and `0` for nop.

## Layout

```text
README.md                              This guide
instruction.md                         Agent-visible output contract
instruction.txt                        Packaged instruction text
task.toml                              Task name, limits, networks, and artifact boundary
spec.json                              Synthetic-task parameters and provenance
certificate.json                       Packaged experimental certificate
environment/                           Agent image and graph fixtures
  Dockerfile                           Pinned Python base and fixture installation
  inputs.json                          Root input values
  dag_spec.json                        Graph operations and dependencies
  dag_code.py                          Generated graph functions
solution/                              Oracle reference solver files
  solve.sh                             Solver entry point with embedded Python
  solve.py                             Python solver file
oracle/                                Additional packaged Oracle files
tests/                                 Separate verifier image and trusted checks
  Dockerfile                           Verifier image definition
  test.sh                              Verifier entry point
  verify.py                            Exact result comparison and reward logging
  golden.json                          Packaged trusted result fixture
verifier/                              Additional packaged verifier files
workbench/                             Local control scripts, not agent fixtures
  fair-alternative.sh                  Graph solver control with a complete dependency trace
  please-hack.sh                       Fixed target and value without a dependency trace
  adversarial/                         Invalid submission scripts
    empty-output.sh                    Writes an empty result file
    wrong-target.sh                    Writes a corrupted target without a trace
    wrong-value.sh                     Writes an incorrect value without a trace
```

## Run

```bash
uv run evallab run --task library/tasks/experimental/syn-funcdag-medium --agent oracle --name syn-funcdag-medium-oracle
uv run evallab run --task library/tasks/experimental/syn-funcdag-medium --agent nop --name syn-funcdag-medium-nop
```

Expected rewards are `1` for Oracle and `0` for nop.
