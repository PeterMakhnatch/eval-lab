# Observability

Where a human looks when asking "what happened?" — and what each surface owns.

| Surface | Owns | Does not own |
|---|---|---|
| **Phoenix** (`http://127.0.0.1:6006`) | Span trees: ATIF agent steps, tool calls, later LiteLLM/DSPy/researcher calls | Job pass/fail, spend vs ceiling, queue state |
| **`harbor view <jobs-dir>`** | Single-trial drill-down of Harbor artifacts (instruction, logs, reward) | Cross-trial trends |
| **`digests/YYYY-MM-DD.md`** | Morning one-pager: dispatches, canaries, spend, quarantine | Span timings |
| **Streamlit** (brief 11) | Research overview over the catalog | Writes, approvals, traces |
| **PostgreSQL catalog** | Searchable job/trial index (rebuildable) | Canonical evidence |

Harbor job directories under `runs/` remain the immutable source of truth.
Phoenix is a derived view.

## Phoenix

Added as the `phoenix` service in `compose.yaml`:

- Image: `arizephoenix/phoenix:20.2.0@sha256:db93e6fa…` (multi-arch index).
- Ports, from [Phoenix configuration](https://arize.com/docs/phoenix/self-hosting/configuration)
  (checked 2026-08-13): `6006` UI + OTLP/HTTP `/v1/traces` (protobuf);
  `4317` OTLP/gRPC.
- Data: volume `evallab-phoenix` mounted at `/mnt/data`
  (`PHOENIX_WORKING_DIR`).
- Bound to `127.0.0.1` only.

**Integrator starts it from the main checkout** — role worktrees do not run
`docker compose`:

```bash
cd ~/Developer/eval-lab
docker compose up -d phoenix
```

Retention: `PHOENIX_DEFAULT_RETENTION_POLICY_DAYS` is unset (Phoenix default
0 = keep forever). This is a local single-user volume; prune the volume if
disk grows. Do not point Phoenix at the lab Postgres — traces stay in the
Phoenix volume so a catalog rebuild cannot delete them.

## How to read a trace

1. Convert + ship a trial or job:

   ```bash
   uv run evallab trace runs/<job>/<trial> --include-controls
   uv run evallab trace runs/<job>
   uv run evallab trace research/explorations/harbor-021/fixtures/trajectory.json --dry-run
   ```

2. Open `http://127.0.0.1:6006`. The root span is `openinference.span.kind=AGENT`
   (the agent name, e.g. `codex`). Children are LLM steps and TOOL calls, with
   timestamps from the ATIF.

3. `--dry-run` validates and converts only. Use it when Phoenix is down, and
   in CI.

Missing `agent/trajectory.json` prints a one-line message (oracle/nop write
`oracle.txt` instead). Invalid ATIF lists validator issues. Neither dumps a
stack trace.

## Auto-trace

`evallab nightly` ships completed **billable** trials under `runs/` after
the digest. oracle/nop controls are skipped unless you pass
`--include-controls` on the manual `trace` command.

## OpenInference

`instrument_openinference()` is invoked at CLI startup. It attaches
OpenInference instrumentors for LiteLLM and DSPy when those packages are
importable; otherwise it is a no-op. Researcher/judge calls that go through
those libraries then land in the same Phoenix project as agent trajectories.

## Dependencies

Group `observability` in `pyproject.toml`: `harbor-atif2otel`, OTel SDK /
OTLP / proto, OpenInference LiteLLM + DSPy. Included in `uv` default-groups
so `uv sync --frozen` is enough for fixture tests.
