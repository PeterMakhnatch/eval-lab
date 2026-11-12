# `deepplanning-v1/travel-lisbon-002`

This offline, deterministic task measures whether an agent identifies an infeasible Lisbon itinerary and reports the required sources and refusal reason.
The imported Qwen DeepPlanning case records Apache-2.0 provenance from `https://github.com/QwenLM/Qwen-Agent` at ref `31a4d36d123688581a9e9744427272b33ce940e0`.
See [instruction.md](instruction.md).

## Environment

The agent image builds from `environment/Dockerfile` using `python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251`.
The working directory is `/app`.
The image contains the task specification and local flight, hotel, and museum-pass sources at `/app/data/task.json`.
The agent submits `/app/answer.json`, the only declared artifact.

`task.toml` sets `cpus = 1`, `memory_mb = 512`, `storage_mb = 1024`, and an agent `timeout_sec` of `120` seconds.
The environment build timeout is `120` seconds, and the agent network mode is `public`.
The case uses local data and requires no network access; `public` does not enforce network isolation.
No MCP servers are configured.

The specification requires all listed sources, a Lisbon hotel, a budget of `900`, and a time limit of `300`.
The required prices total `1130`, so the expected answer reports that the itinerary is infeasible.

## Verifier

The verifier uses `environment_mode = "separate"`, network mode `no-network`, and a `60`-second timeout.
`tests/Dockerfile` builds a separate image from the same pinned Python base as the agent image.
It copies `golden.json`, `verify.py`, and `test.sh` into `/tests`.
`tests/test.sh` runs the Python verifier against `/app/answer.json` and the verifier's own `/tests/golden.json`.
The artifact boundary declares only `/app/answer.json`; `collect` is empty.

| Check | Requirement |
|---|---|
| Answer format | The answer exists, is nonempty, and parses as a JSON object. |
| Source declarations | `acquired_sources` is a list containing `flight-lis`, `hotel-baixa`, and `museum-pass`. |
| Status | `status` equals `infeasible`. |
| Refusal reason | The trimmed reason equals `minimum sourced cost is 1130, exceeding budget 900`. |

The verifier checks declarations against the golden specification; it does not independently calculate the itinerary cost or validate source acquisition.
It writes `/logs/verifier/reward.txt` with `1.0` or `0.0` and `/logs/verifier/result.json` with the status, reason, and reward.
Expected rewards are `1` for Oracle and `0` for nop.

## Layout

```text
README.md                              This guide
instruction.md                         Agent-visible task and answer path
task.toml                              Task metadata, provenance, limits, and artifact boundary
environment/                           Agent image build context
  Dockerfile                           Pinned Python image and task-data copy
  data/task.json                       Sources and planning constraints
solution/                              Oracle reference submission
  solve.py                             Price calculation and answer writer
  solve.sh                             Oracle entry point
tests/                                 Separate verifier build context
  Dockerfile                           Pinned Python image and trusted verifier files
  golden.json                          Expected sources, status, and refusal reason
  test.sh                              Verifier entry point
  verify.py                            Answer checks and verifier log writer
workbench/                             Alternative and adversarial submissions
  fair-alternative.sh                  Independent price-based answer writer
  please-hack.sh                       Submission that exits without writing an answer
  adversarial/empty-output.sh          Writes an empty answer
  adversarial/missing-sources.sh       Omits required source declarations
  adversarial/wrong-refusal.sh         Writes a nonmatching refusal reason
  adversarial/wrong-status.sh          Writes a success status
```

## Run

```bash
uv run evallab run --task library/tasks/experimental/deepplanning-v1/travel-lisbon-002 --agent oracle --name travel-lisbon-002-oracle
uv run evallab run --task library/tasks/experimental/deepplanning-v1/travel-lisbon-002 --agent nop --name travel-lisbon-002-nop
```

Expected rewards are `1` for Oracle and `0` for nop.
