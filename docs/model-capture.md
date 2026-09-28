---
status: living
audience:
  - builder
  - runner
  - operator
---

# Independent model-call capture


Harness trajectories are agent-controlled: installed agents write them inside
the container (`/logs/agent`), a root agent can delete them, and
timeouts/cancels can skip conversion (Harbor 0.21 `trial.py`; the opencode
converter silently returns empty on failure). The lab's existing container
proxies (`containers/*_secret_proxy.py`) record usage only, never bodies.

`evallab capture` keeps an independent record of what the model saw and said,
in a host process outside the agent's reach, so a missing, incomplete, or
tampered trajectory is detectable after the run.

## Commands

```bash
# Record: forward to the real backend, append one JSON record per call.
uv run evallab capture serve --upstream <base_url> --out <dir> [--port 8471] \
  [--bind 127.0.0.1] [--upstream-key-env NAME]

# Attribute + judge: match calls to trials, write Parquet, print verdicts.
uv run evallab capture link <capture_dir> <job_dir> [--derived-root DIR]

# Surface: `report run` gains an "Independent capture" section when linked.
uv run evallab report run <trial_dir>
```

`serve` forwards OpenAI-compatible (`/v1/chat/completions`, `/v1/completions`,
`/v1/responses`) and Anthropic (`/v1/messages`) requests, including streaming
SSE, and appends one record per call to `<dir>/calls.jsonl` (append + flush per
record): sequence number, start/end timestamps, method/path, optional route
token from a `/t/<token>/` path prefix (stripped before forwarding), selected
request headers with auth redacted, full request body, response status, full
response body (raw SSE plus the reassembled message/tool calls), usage,
upstream latency, and errors (upstream down, client disconnect). A
`capture.json` manifest (upstream, start time, eval-lab commit, proxy version)
is written at start; a `ProvenanceMetadata`-compatible digest
(`provenance.json`) is written at close — stop with SIGINT/SIGTERM, not
SIGKILL, so the close path runs.

Upstream path joining is prefix-aware: `--upstream .../v1` serves both
`/v1/chat/completions` clients (raw OpenAI) and `/chat/completions` clients
(litellm posts the bare path to its `api_base`).

Keys: with `--upstream-key-env NAME` the key is injected upstream and never
recorded; `Authorization`/`x-api-key`-shaped headers are scrubbed from every
record and every log line. Without it the proxy is transparent to client auth
(what the client sent is what the upstream gets, minus hop-by-hop headers).

`link` attribution priority per call: route token → `X-Session-ID`-style
session header → conversation reconstruction (message-prefix chaining: the
first user message matches the trial's instruction and timestamps fall inside
the trial's `agent_execution` window from `result.json`). The instruction
anchor is embedding-tolerant: agents wrap `instruction.md` in a prompt
template, so containment either way anchors (64-char guard). Output, under the
resolved derived root: `derived/parquet/job_id=*/model_calls.parquet`,
`trial_capture.parquet`, and a `capture_link.json` receipt.

Per-trial completeness verdicts:

| Verdict | Meaning |
|---|---|
| `complete` | Captured turns agree with the harness trajectory |
| `trajectory_missing` | Calls captured, ATIF absent/empty — deleted or never converted |
| `trajectory_truncated` | Fewer ATIF agent steps than captured turns, or a captured turn is absent from the ATIF |
| `capture_missing` | ATIF present, no captured calls — the proxy was not in the path |
| `ambiguous` | Attribution matched more than one trial; calls left unassigned |

Turn comparison is rendering-robust: besides verbatim containment it compares
lowercased alphanumeric skeletons, because harnesses store parsed renderings
(Terminus keeps `Analysis: …` while the model emitted fenced
`{"analysis": "…"}`). The both-absent corner reads `capture_missing` with a
pointer at the `agent_execution` window: an idle control agent is
indistinguishable from a full bypass.

## Wiring recipes

Run the proxy on the host (`--bind 127.0.0.1`, fixed `--port`), then point the
agent at it. In-container agents must reach the host via
`http://host.docker.internal:<port>`.

### Terminus-2 (host loop — covers remote sandboxes too)

Verified in Harbor 0.21 source (`harbor/agents/terminus_2/terminus_2.py`):
`Terminus2.run()` builds a host-side `Chat(LiteLLM(api_base=...))` and the
episode loop (`_run_agent_loop`) calls litellm from the Harbor host process;
the sandbox is only touched for terminal I/O. So a host proxy sees every
model call even when the sandbox is remote (Daytona/Modal) — point `api_base`
at the host proxy:

```yaml
agents:
  - name: terminus-2
    model_name: openai/qwen2.5:7b
    kwargs:
      api_base: http://127.0.0.1:8471
      model_info: {max_input_tokens: 32768, max_output_tokens: 8192,
                   input_cost_per_token: 0.0, output_cost_per_token: 0.0}
```

(`model_info` registers the custom model with litellm so token counting does
not fail.) Observed: Harbor does not pass a session id to Terminus-2, so no
`X-Session-ID` arrives and attribution uses conversation chaining.

### mini-swe-agent (in-container)

The agent runs inside the container and reads its endpoint from
`OPENAI_BASE_URL`/`OPENAI_API_BASE` (passthrough env;
`harbor/agents/installed/mini_swe_agent.py`). Model names are
`provider/model`. When Harbor sets a session id the agent sends it as
`X-Session-ID` (`model.model_kwargs.extra_headers`), which the proxy records
for session attribution. Set the job env to the host proxy:

```bash
OPENAI_BASE_URL=http://host.docker.internal:8471
```

### OpenCode (in-container)

Provider endpoints come from the generated `~/.config/opencode/opencode.json`:
for `openai`/`anthropic` providers Harbor writes
`provider.options.baseURL` from the model connection's configured base URL
(`harbor/agents/installed/opencode.py`, `_build_register_config_command`),
deep-merged with job-level `opencode_config` kwargs. Point the connection (or
the override) at `http://host.docker.internal:<port>`.

### Local model server (Ollama / llama.cpp / MLX)

```bash
ollama serve  # or any OpenAI-compatible server on 127.0.0.1:11434
uv run evallab capture serve --upstream http://127.0.0.1:11434/v1 \
  --out derived/captures/<name> --port 8471
```

For litellm's `openai/` route set a dummy key (`OPENAI_API_KEY=dummy…`;
Ollama ignores auth) plus `model_info` as above. Proven with
`openai/qwen2.5:7b` through Terminus-2: 4 turns captured, verdict `complete`;
after deleting `agent/trajectory.json` in a job copy, `trajectory_missing`.

## Limits

- No request/response bodies for non-HTTP transports; Ollama's native
  `/api/*` endpoints are forwarded but recorded as `unknown` kind (use its
  OpenAI-compatible `/v1/*` surface for reassembled turns).
- Route-token and session attribution need agent cooperation (path prefix,
  header); otherwise chaining needs `instruction.md` resolvable from the trial
  config and clocks inside the execution window.
- The proxy is a diagnostic tap, not a meter: for spend accounting keep the
  container secret proxies.
