# `local-lab/event-summary`

This task measures whether an agent can follow an exact data-transformation
contract while preserving its input. See [instruction.md](instruction.md).

## Environment

The agent receives a Python 3.13 Debian container with one JSONL fixture at
`/app/input/events.jsonl`, an empty `/app/output`, one CPU, 512 MB RAM, and a
120-second agent timeout. The task has no network dependency, but its baseline
is intentionally `public`: Harbor's Docker provider cannot enforce
`no-network` on Docker Desktop for macOS and correctly rejects that setting.
Use a provider with enforced network policy before treating this task as an
offline-agent evaluation. The reference solution uses only the Python standard
library and is uploaded only for Oracle trials.

## Verifier

Verification runs in a separate Python image built from `tests/`. It receives
only Harbor's declared artifacts. Its own trusted fixture is baked into the
verifier image, so changing `/app/input/events.jsonl` cannot make a fabricated
answer pass.

| Reward | Check |
|---|---|
| `reward` | All checks pass |
| `correctness` | Exact schema and independently computed values |
| `input_preservation` | Input bytes equal the hidden trusted fixture |
| `output_hygiene` | `summary.json` is the only output file |

The verifier writes `checks.json`, a small CTRF report, and `reward.json` under
`/logs/verifier`.

## Layout

```text
instruction.md             Agent-visible contract
task.toml                   Harbor metadata, limits, artifact boundary
environment/               Agent image and initial JSONL fixture
solution/                   Oracle-only reference implementation
tests/                      Separate verifier image and trusted fixture
```

## Run

```bash
uv run evallab run --task tasks/event-summary --agent oracle \
  --name event-summary-oracle-local
uv run evallab run --task tasks/event-summary --agent nop \
  --name event-summary-nop-local
```

Expected rewards are `1` for Oracle and `0` for no-op.