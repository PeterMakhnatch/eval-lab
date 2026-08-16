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

## The `session.id` bridge

Converted spans carry no `spec_id`, `job_id` or `trial_id` — `harbor-atif2otel`
converts the ATIF document and nothing else. One identifier does cross:
`session.id` on the root span is the ATIF `session_id` verbatim
(`harbor_atif2otel/convert.py:212`), and that is stored as
`trajectory_documents.session_id`, from which `trial_id -> job_id ->
experiment_id` follow. `src/evallab/tracing.py` makes the hop usable in both
directions.

**Trial -> trace.** `trace_identity_for_trial(trial_dir)` returns the
`trace_id`, `root_span_id`, `session_id` and `base_session_id` of a trial
without converting or shipping anything. The ids come from `atif2otel`'s own
seed functions, so they cannot drift from what Phoenix receives;
`tests/test_trace_join.py` pins them against the converted payload.

**Trace -> research graph.** `session_lookup_sql(placeholder)` is the join and
`resolve_session(session_id, fetch=...)` runs it through a caller-supplied
cursor. It returns a `SessionResolution`, not a row. `.trial` yields the single
trial or raises `TraceError`; it never guesses.

Two identifiers, not one:

| Value | What it keys | Note |
|---|---|---|
| `session_id` | the span, and the catalog row | raw ATIF value |
| `base_session_id` | the Phoenix trace | `-cont-N` stripped, so a resumed session's documents share one trace |

### `session_id` is not unique, and must not be constrained

All 23 `trajectory_documents` rows in the catalog have a distinct `session_id`,
but that is a property of today's data, not of the model. Two mechanisms break
it:

- **Embedded subagents.** ATIF v1.7 subagent trajectories share their parent's
  `session_id` and are disambiguated by `trajectory_id`
  (`harbor_atif2otel/ids.py:45-55`). `evallab.atif._flatten_payloads` writes one
  `trajectory_documents` row per embedded payload, so one multi-agent trial
  legitimately yields several rows with the same `session_id`. A
  `UNIQUE (session_id)` constraint does not deduplicate that — it **aborts the
  ingest**.
- **Continuations.** `-cont-N` sessions are textually distinct, so they satisfy
  a unique constraint while still collapsing to one `trace_id`. Uniqueness on
  the raw column would therefore imply a one-trace-one-trial guarantee it does
  not provide.

`sql/schema.sql` indexes the column instead
(`trajectory_documents_session_idx`) and records why the constraint is absent.
Ambiguity is refused at the resolver: several documents for one trial is a
normal answer, several *trials* raises.

### Redaction propagates into spans

`input.value` on the root span of a promoted trajectory is the
`<<evallab-redacted: N bytes, sha256:...>>` marker verbatim, so shipping
promoted evidence to Phoenix cannot leak what promotion withheld — and equally,
Phoenix shows the marker, not the prompt. This is inherited from
`scripts/promote_codex_bundle.py`, not enforced by the converter, so
`tests/test_trace_join.py` pins it over every committed Codex bundle: every
marker reaching a span must be a whole, unaltered marker from the source
document, which also rules out one sliced short by `atif2otel`'s attribute
truncation. The guard is mutation-controlled — the same documents with the
markers replaced by plaintext put the plaintext on the span and fail the guard.

## Dependencies

Group `observability` in `pyproject.toml`: `harbor-atif2otel`, OTel SDK /
OTLP / proto, OpenInference LiteLLM + DSPy. Included in `uv` default-groups
so `uv sync --frozen` is enough for fixture tests.
