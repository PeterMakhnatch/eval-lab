---
status: living
audience:
  - analyst
  - operator
---

# Observability

Where a human looks when asking "what happened?" — and what each surface owns.

| Surface | Owns | Does not own |
|---|---|---|
| **Phoenix** (`http://127.0.0.1:6006`) | Span trees: ATIF agent steps, tool calls, later LiteLLM/DSPy/researcher calls | Job pass/fail, spend vs ceiling, queue state |
| **Laminar Cloud** (opt-in native `mimoagent`) | Live automatic OpenAI LLM spans, native tools/child agents and Harbor lifecycle under one trial trace | Canonical grades, copy verdicts, spend admission, paid-run approval or packet-level lock proof |
| **`harbor view <jobs-dir>`** | Single-trial drill-down of Harbor artifacts (instruction, logs, reward) | Cross-trial trends |
| **`digests/YYYY-MM-DD.md`** | Morning one-pager: dispatches, canaries, spend, quarantine | Span timings |
| **Streamlit** (brief 11) | Research overview over the catalog | Writes, approvals, traces |
| **PostgreSQL catalog** | Searchable job/trial index (rebuildable) | Canonical evidence |

Harbor job directories under `runs/` remain the immutable source of truth.
Phoenix and Laminar are derived views; neither replaces those artifacts.

## Live Laminar tracing for native MiMo (HAR-165)

Install the checkout-pinned host runtime and the separate native runtime:

```bash
uv sync --locked --extra laminar
uv sync --project tools/mimoagent-harbor --locked
```

The host extra pins Harbor `0.21.0` and `lmnr` `0.7.64`; the native lock pins
the same SDK alongside OpenAI `3.3.1`. Keep that graph isolated from the lab's
OpenAI 2.x graph. A native launch inheriting `LMNR_PROJECT_API_KEY` selects
the checkout's Harbor executable. Load the centrally stored key with
`keys run -- <authorized-launch-command>`; retain `--extra laminar` when
using `uv run --locked`. The key is host-only, never a sandbox credential,
native-worker credential or model request. No key means no Laminar tracing.

The existing state-journal plugin opens one `harbor.trial` root on public
trial START, before setup; the session is the Harbor job name. Setup,
cached egress acknowledgment, agent execution, verifier, its actual reward
and cleanup/stop spans share that trace. The native process initializes
automatic OpenAI instrumentation before constructing its client. Native
tool spans carry actual input, output and process exit codes; an `agent`
tool nests its delegated agent below itself. A delegated agent without a
process exit code records it as unknown, not a fabricated zero.

The native SDK writes sanitized OTLP frames locally; the host tails complete
new frames and forwards them through a bounded asynchronous Cloud queue.
The SDK's 250 ms batch interval and host's 500 ms tail interval allow
completed child spans to appear while the trial root is still running.
There are no added model headers, messages, answer retries or termination
decisions. Broken/stalled exporters are best-effort and cannot become a
trial's business exception or hold its process exit.

Export copies redact known secrets, authorization bearer values and host
paths across span names, attributes, events, status, resources, scopes and
links. Original prompts, tool results and errors remain unchanged. Logical
task paths such as `/testbed/...` remain useful in the trace.

Metadata includes actual trial/task identity, card, arm, job name, intended
setup-fingerprint hash, configured model revision/source and lock state.
Configured revision is not proof of loaded weight identity; the cached
Daytona deny-all acknowledgment is not a packet probe. Unobserved values
remain unknown. `laminar-trace.json` records the real SDK trace ID and root
closure alongside the Harbor result. It does **not** attest Cloud ingestion
or manufacture a deep link: observe the authenticated private Cloud trace
and record its actual URL separately. Laminar IDs are not Phoenix's
deterministically derived ATIF IDs.

Source availability does not authorize execution. In HAR-165, the three
independently approved tracing smokes must complete and be observed live
before the unchanged frozen HAR-157 twelve; both stages share the existing
$8 Modal/bridge/Daytona cap. Local scripted-SDK fixtures are not those
smokes, model capability evidence, paid-run approval or remote cleanup
proof.


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
  (`harbor_atif2otel/ids.py:45-55`). `evallab.evidence.atif._flatten_payloads` writes one
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
OTLP / proto, OpenInference LiteLLM + DSPy. It is an opt-in dependency group;
install via `uv sync --group observability` to enable live OTLP trace export
to Phoenix and runtime instrumentation.
