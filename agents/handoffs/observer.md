Status: review-wanted
Last: brief 08 implemented; fixture tests + dry-run CLI green; Phoenix not started from this worktree
Next: rebase onto origin/main, push role/observer, open PR
Blockers: none — integrator must `docker compose up -d phoenix` from the main checkout to render a live span tree

## Goal

Brief 08: Phoenix compose service + `harbor-lab trace` (ATIF → OTel via
harbor-atif2otel, OTLP to Phoenix) + nightly auto-trace of billable trials +
OpenInference stub wiring.

## What landed

- `src/harbor_lab/tracing.py` — validate/convert/ship; `TraceError` for
  missing/invalid ATIF (no traceback).
- `harbor-lab trace PATH [--dry-run] [--include-controls] [--endpoint]`
- Nightly calls `trace_completed_jobs(runs/, include_controls=False)` after
  the digest (errors there do not fail the cycle).
- `instrument_openinference()` at CLI startup; proven with stub instrumentors
  (real LiteLLM/DSPy stay dormant until those packages exist).
- Phoenix service in `compose.yaml`: `20.2.0@sha256:db93e6fa…`, 127.0.0.1
  6006/4317, volume `harbor-lab-phoenix`. Verified against Phoenix docs
  2026-08-13 (UI + OTLP/HTTP `/v1/traces` on 6006, gRPC on 4317).
- `docs/observability.md`, `docs/prompts/08-phoenix-trace-shipping.md`.
- Fixture tests in `tests/test_tracing.py` using RECON's
  `research/explorations/harbor-021/fixtures/trajectory.json`.
- `pyproject.toml` group `observability` + `default-groups` so CI `uv sync
  --frozen` installs the converter. `uv.lock` updated. `environments` limited
  to Python >=3.12 because harbor-atif2otel requires it (repo is 3.13).

## Verification

- `uv run pytest` — 44 passed (7 new tracing tests).
- `uv run ruff check src/harbor_lab/tracing.py src/harbor_lab/cli.py tests/test_tracing.py` clean.
- `uv run harbor-lab trace research/explorations/harbor-021/fixtures/trajectory.json --dry-run`
  → `spans=10 root=codex kinds=AGENT,LLM,TOOL`.
- Missing path and bad JSON print `error: …` / `failed … not valid JSON`, no traceback.
- Phoenix is **not** running (`127.0.0.1:6006` refused). Did not start compose.

## Integrator

From the **main checkout**:

```bash
docker compose up -d phoenix
cd .worktrees/observer   # or after merge, from main
uv run harbor-lab trace research/explorations/harbor-021/fixtures/trajectory.json
# open http://127.0.0.1:6006 — root AGENT span "codex"
```

## Paths outside the strict OBSERVER set

`tests/test_tracing.py` and `uv.lock` are required for acceptance / CI.
Do **not** squash-self-merge if that is too wide; leave the PR for the
integrator.

`uv run ruff check .` is already red on origin/main
(`research/explorations/harbor-021/demos/*`, `library/curated/_emit_card.py`).
Not ours; not fixed.
