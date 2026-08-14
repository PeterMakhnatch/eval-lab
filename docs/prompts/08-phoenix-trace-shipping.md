# 08 — Phoenix + trace shipping

Add the Phoenix compose service. `harbor-lab trace <trial>` converts the
trial's ATIF via `harbor-atif2otel` and ships it OTLP → Phoenix; a job path
ships all trials of that job; nightly ships completed billable trials
automatically (free oracle/nop controls only with `--include-controls`).
Wire OpenInference instrumentation into researcher-agent invocations and any
DSPy / LiteLLM calls so judge and optimizer traffic land in the same UI.

Acceptance: open Phoenix, see a Codex trajectory as a span tree with step
timings, and a researcher-analyst call beside it. Converter path is covered
by fixture tests that need no live Phoenix.

## Implementation notes (OBSERVER, 2026-08-14)

- Phoenix image: `arizephoenix/phoenix:20.2.0` digest-pinned. Ports from
  current docs (2026-08-13): `6006` UI + OTLP/HTTP `/v1/traces` (protobuf);
  `4317` OTLP/gRPC. Persistence via `PHOENIX_WORKING_DIR=/mnt/data`.
- Do not start compose from a role worktree. Integrator starts Phoenix from
  the main checkout: `docker compose up -d phoenix`.
- Converter + ship live in `src/harbor_lab/tracing.py`. Missing or invalid
  ATIF is a `TraceError` (clear message); the CLI does not dump a traceback.
- Starting material: `research/explorations/harbor-021/` (RECON atif2otel
  demo + `fixtures/trajectory.json`).
- Dependency group `observability` holds `harbor-atif2otel` and OTel /
  OpenInference packages. `uv sync` default-groups include it so CI can
  run the fixture tests.

## Repository-wide constraints

- Preserve immutable `runs/` and rebuildable PostgreSQL.
- No live Phoenix required in pytest.
- No billable runs in tests.
- Additive-only edits to `compose.yaml`, `cli.py`, `pyproject.toml`.
