Status: review-wanted
Last: PR #2 opened — Phoenix compose + evallab trace; fixture tests green
Next: integrator starts Phoenix from main checkout and ships the RECON fixture
Blockers: none — do not self-merge (tests/test_tracing.py + uv.lock sit outside the strict owned set)

## Goal

Brief 08: Phoenix compose service + `evallab trace` (ATIF → OTel via
harbor-atif2otel, OTLP to Phoenix) + nightly auto-trace of billable trials +
OpenInference stub wiring.

## What landed

- `src/evallab/tracing.py` — validate/convert/ship; `TraceError` for
  missing/invalid ATIF (no traceback).
- `evallab trace PATH [--dry-run] [--include-controls] [--endpoint]`
- Nightly calls `trace_completed_jobs(runs/, include_controls=False)` after
  the digest (errors there do not fail the cycle).
- `instrument_openinference()` at CLI startup; proven with stub instrumentors
  (real LiteLLM/DSPy stay dormant until those packages exist).
- Phoenix service in `compose.yaml`: `20.2.0@sha256:db93e6fa…`, 127.0.0.1
  6006/4317, volume `evallab-phoenix`. Verified against Phoenix docs
  2026-08-13 (UI + OTLP/HTTP `/v1/traces` on 6006, gRPC on 4317).
- `docs/observability.md`, `docs/prompts/08-phoenix-trace-shipping.md`.
- Fixture tests in `tests/test_tracing.py` using RECON's
  `research/explorations/harbor-021/fixtures/trajectory.json`.
- `pyproject.toml` group `observability` + `default-groups` so CI `uv sync
  --frozen` installs the converter. `uv.lock` updated. `environments` limited
  to Python >=3.12 because harbor-atif2otel requires it (repo is 3.13).

## Verification

- `uv run pytest` — 44 passed (7 new tracing tests).
- `uv run ruff check src/evallab/tracing.py src/evallab/cli.py tests/test_tracing.py` clean.
- `uv run evallab trace research/explorations/harbor-021/fixtures/trajectory.json --dry-run`
  → `spans=10 root=codex kinds=AGENT,LLM,TOOL`.
- Missing path and bad JSON print `error: …` / `failed … not valid JSON`, no traceback.
- Phoenix is **not** running (`127.0.0.1:6006` refused). Did not start compose.

## Integrator

From the **main checkout**:

```bash
docker compose up -d phoenix
cd .worktrees/observer   # or after merge, from main
uv run evallab trace research/explorations/harbor-021/fixtures/trajectory.json
# open http://127.0.0.1:6006 — root AGENT span "codex"
```

## Paths outside the strict OBSERVER set

`tests/test_tracing.py` and `uv.lock` are required for acceptance / CI.
Do **not** squash-self-merge if that is too wide; leave the PR for the
integrator.

`uv run ruff check .` is already red on origin/main
(`research/explorations/harbor-021/demos/*`, `library/curated/_emit_card.py`).
Not ours; not fixed.
