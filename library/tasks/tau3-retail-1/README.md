# `sierra-research/tau3-bench__tau3-retail-1`

This imported Sierra Research tau3-bench task measures customer service through tool use in a simulated retail conversation.
The agent must resolve the user's exchange request under the supplied retail policy.
See [instruction.md](instruction.md).

## Environment

The task uses schema version `1.1`.
The agent image builds from `python:3.12-slim` in `environment/Dockerfile` and uses `/app` as its working directory.
The image clones Sierra Research's `tau2-bench` into `/opt/tau2-bench` and installs its `knowledge` dependencies.

`task.toml` specifies `cpus = 4`, `memory_mb = 8192`, and `storage_mb = 10240`.
The agent timeout is `3600` seconds, and the environment build timeout is `600` seconds.

The package does not declare a network mode or an offline restriction.
Image builds require network access to package repositories and GitHub.
Compose connects the agent container to a separate `tau3-runtime` service and shares the agent log directory with that service.
The MCP server uses `streamable-http` at `http://tau3-runtime:8000/mcp`.
Its image also builds from `python:3.12-slim` and adds FastMCP.
The simulated user defaults to `gpt-5.2`; Compose passes `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and user-model settings to the runtime service.

The agent receives the retail policy and uses MCP tools to converse with the simulated user and inspect or change retail data.
It must call `start_conversation` first and use `send_message_to_user` for user-facing messages.
Each step must contain either a message or a domain tool call, not both.
The policy requires identity authentication and explicit user confirmation before database updates.
The agent ends the conversation with `###STOP###` or `end_conversation`.

## Verifier

`environment_mode` is not set in `task.toml`; the verifier uses the shared agent container.
There is no `tests/Dockerfile` and no separate verifier image build.
The agent image supplies the installed `tau2-bench` runtime.
The verifier timeout is `300` seconds.

`tests/test.sh` runs `/tests/evaluate.py` with `/tests/config.json` in that container.
It reads `/logs/agent/tau3_runtime_state.json`, which the MCP runtime writes into the shared agent log directory.
The evaluator converts the recorded messages into a tau2 simulation and calls the upstream evaluator with `EvaluationType.ALL`.

| Check | Behavior |
|---|---|
| Runtime log | A missing or invalid runtime log produces reward `0`. |
| Stop signal | The runtime must end with `agent_stop` or `user_stop`; other reasons produce reward `0`. |
| Retail outcome | The upstream evaluator checks the configured task, including the expected database outcome for the exchange. |
| Reward basis | The configuration declares `DB` and `NL_ASSERTION`, with no explicit natural-language assertions. |
| Oracle handling | The evaluator removes `NL_ASSERTION` from the reward basis when `/solution/solve.sh` exists. |
| Evaluation errors | Runtime evaluation errors produce reward `0` and diagnostic information. |

The verifier writes the numeric reward to `/logs/verifier/reward.txt`.
It writes status, reward, evaluator usage, and reward details to `/logs/verifier/result.json`.
The Oracle script executes reference retail actions and writes the runtime state expected by this evaluator.
Expected rewards are `1` for Oracle and `0` for nop; these are expectations, not measured results.

## Layout

```text
README.md                              This guide
instruction.md                         Agent instructions and retail policy
task.toml                              Imported task identity, limits, and MCP endpoint
environment/                           Agent image and MCP service definitions
├── Dockerfile                         Agent image with the upstream tau2 runtime
├── docker-compose.yaml                Runtime service, model settings, and shared logs
└── runtime-server/                    Separate MCP runtime service
    ├── Dockerfile                     Runtime image with tau2 and FastMCP
    ├── server.py                      Conversation tools and runtime state logging
    └── task_config.json               Runtime task configuration
solution/                              Oracle reference solution
└── solve.sh                           Reference actions and runtime state generation
tests/                                 Shared-container verifier; no Dockerfile
├── config.json                        Retail scenario and evaluation criteria
├── evaluate.py                        Upstream evaluation and reward output
└── test.sh                            Verifier entry point and output paths
```

## Run

```bash
uv run evallab run --task library/tasks/tau3-retail-1 --agent oracle --name tau3-retail-1-oracle
uv run evallab run --task library/tasks/tau3-retail-1 --agent nop --name tau3-retail-1-nop
```

Expected rewards are `1` for Oracle and `0` for nop.
