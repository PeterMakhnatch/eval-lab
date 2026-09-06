# Platform service container definitions

## Purpose
`containers/` defines committed container entrypoints, compose overlays, and proxies
used by Platform services. It isolates network access, secret handling, and event
streaming for evaluable workloads.

## What lives here / entry points
- `containers/state-journal/`: Event producer and watch runtime for state journaling (`containers/state-journal/Dockerfile`, `containers/state-journal/producer.py`, `containers/state-journal/watch.py`).
- `containers/continuous-operator/`: Runtime definitions for continuous operator loops (`containers/continuous-operator/Dockerfile`, `containers/continuous-operator/compose.yaml`).
- `containers/deepseek-v4-flash-secret.compose.yaml`: Secret proxy overlay for DeepSeek workloads.
- `containers/deepseek_secret_proxy.py`: Credential proxy runtime for DeepSeek workloads.
- `containers/zai-secret.compose.yaml`: Secret proxy overlay for ZAI workloads.
- `containers/zai_secret_proxy.py`: Credential proxy runtime for ZAI workloads.

## Invariants or rules
- Committed runtime definitions: Service container sources are committed, reviewed Platform definitions, not generated state (cited in `agents/STRUCTURE.md`).
- Hidden verifier inputs: Do not place secret credentials, test suites, or solutions in an evaluated agent's environment image (cited in `AGENTS.md`).
- Network isolation: Secret proxies isolate credentials across workbench internal networks (cited in `docs/architecture.md`).

## Tests or checks
- `uv run pytest tests/test_continuous_operator.py`
- `uv run pytest tests/test_state_events.py`
- `uv run pytest tests/test_harbor_deepseek.py`

## What not to add here
- Do not store task-specific sandbox images or task definitions here; use `library/tasks/`.
- Do not commit secrets, API keys, credentials, or environment files here (cited in `AGENTS.md`).
- Do not store ephemeral runtime containers, volumes, or execution outputs here.
