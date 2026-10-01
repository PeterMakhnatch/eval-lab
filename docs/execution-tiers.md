---
status: living
audience:
  - runner
  - operator
---

# Execution tiers: what runs where, and what it costs

Operator workflow updated 2026-09-24. Machine inventory entries retain their
observation dates; they are not universal backend qualifications. The spend
rules below remain binding.

## The one-line summary

Harbor supplies the execution backends; Eval Lab prepares bounded specs and
preserves their evidence. Compatibility belongs to a task/harness/model/backend
combination, not just an `--env` name. Terminus 2 uses a host-side metered or
explicitly local model transport with native Harbor task backends. GLM mini-SWE
has a live-proven single-container Daytona path; its container-side Modal proxy is not integrated.
Cloud runs still require explicit approval under `policy/standing-approvals.yaml`.

## Machine state (verified, not aspirational)

| Component | State | Verified how |
|---|---|---|
| Docker Desktop VM | **24 GiB RAM / 16 CPUs** (23.4 GiB usable; raised from 7.7 GiB on 2026-08-14) | `docker info` after restart; compose services self-healed via `restart: unless-stopped` |
| Host | 64 GB RAM, Apple Silicon (arm64) | `sysctl hw.memsize` |
| Harbor CLI | `harbor[modal]==0.21.0` (global uv tool; version pinned during reinstall) | `harbor --version`; `modal 1.5.4` imports in the tool env |
| Modal CLI | present at `~/.local/share/uv/tools/harbor/bin/modal` | — |
| Modal account | `p-makhnatch` authenticated, verified 2026-09-22 | `modal profile list`, `modal token info`; read-only billing API reports $0 usage for September, not a remaining credit balance |

Docker settings backup (pre-change): `~/Library/Group Containers/group.com.docker/settings-store.json.bak-claude`.
Operational note: quitting Docker Desktop from a script requires
`osascript -e 'quit app "Docker Desktop"'` — quitting app "Docker" silently
does nothing and a subsequent relaunch collides with the still-running VM
("no route to host" from the backend for 10+ minutes).

## How to classify a task (mechanically, from the package)

Read `task.toml` and `environment/Dockerfile`; no execution needed:

1. `[environment] gpus >= 1` → **cloud-only**. No CUDA on Apple Silicon.
2. `memory_mb > 24576` → **cloud-only** at current VM size.
3. `FROM --platform=linux/amd64` in the Dockerfile → **local-emulated**:
   runs under QEMU (slow, occasionally flaky); prefer cloud for results that
   will be cited.
4. `memory_mb > 8192` or `[agent] timeout_sec >= 10800` (3 h) → **local-heavy**:
   runs fine locally, unsuitable for canaries/smoke suites.
5. Otherwise → **local-ok**.

## TB3 classification (frontier-bench @ `3d694e91`, 74 tasks)

| Tier | Count | Meaning |
|---|---|---|
| local-ok | 45 | fair game for local automation |
| local-heavy | 24 | local, but hours-long and/or ≥8 GB — run deliberately, never as canaries |
| local-emulated | 1 | `memcached-backdoor` (amd64 pin, 12 GB) |
| cloud-only (GPU) | 4 | `exam-pdf-eval`, `fp8-rmsnorm-gemm`, `jax-speedrun-gpu` (32 GB!), `math-eval-grader` |

The heaviest non-GPU tasks now inside local capacity: `takens-embedding-lean`
(16 GB, 8 h), `live-database-cutover` (16 GB), `medical-claims-processing`
(10 GB). The library's curated‑19 (`library/curated/README.md`) remains the
canary set; this table is about *capability*, not what should run nightly.

Harbor-Index (82 tasks distilled from 29 benchmarks) is published on Harbor
Hub with all 1,476 baseline trials; its tasks follow the same classification
rules. The exact dataset slug should be read off hub.harborframework.com
before first use — do not guess it.

## Running locally (free, unchanged)

Oracle/nop controls and verifier runs stay exactly as `AGENTS.md` prescribes:
through `evallab` wrappers, jobs under `runs/`, ≤2 concurrent. The larger VM
just means `local-heavy` tasks no longer fail on memory.

## Offline LEGO capture checks (CPU only)

Inspect saved `proxy_capture.json` files without starting Harbor, a proxy,
a model, or a trainer:

```bash
uv run python -m evallab.lego_capture \
  --record-origin cpu_proxy_session_synthetic \
  /path/to/proxy_capture.json
```

Pass multiple files to receive per-file assessments and summary counts. Exit
status is `0` when all requested local checks pass, `1` when any input is
rejected, and `2` for invalid command options. Source files are read-only;
reports include their byte digests, not copied token/probability arrays.

Checks cover prompt/response alignment, nonempty trained-token masks, token
types, finite nonpositive trained-slot log-probabilities (including `0.0`),
finite numeric excluded-slot placeholders, disabled/error/diagnostic captures,
and optional routing alignment. Missing or empty routing remains absent.
Context overflow is retained as termination metadata: it does not by itself
invalidate correctly captured earlier tokens.

Weight-span completeness is reported separately. To check an explicitly
chosen learner-step/maximum-lag policy, supply both options:

```bash
uv run python -m evallab.lego_capture \
  --learner-step 5 --max-weight-lag 0 \
  /path/to/proxy_capture.json
```

This rejects missing, malformed, reversed, future, or stale declared spans;
lag uses the oldest sampled step. Without those options, no lag policy is
applied. A dispatch/default step never fills a missing sampled-weight identity.

**Passing is not live-model qualification or training authorization.**
`--record-origin live_trial` remains an unverified declaration and cannot
override conflicting synthetic or diagnostic provenance. Model/checkpoint,
tokenizer, actor and original sampling identities, training rights, task/data
admission, runtime compatibility, and execution approval remain separate
requirements. Text/ATIF trajectories cannot recover missing original
behavior-policy probabilities.

## Running a paid agent locally (authorised per spec, since 2026-08-16)

Local Docker execution is free only for `oracle` and `nop`. Any other agent —
`codex`, `claude-code`, and `zai-opencode` — consumes the selected provider's
subscription or API quota, which a local dollar ceiling does not itself measure. Such
a spec is refused by the policy gate with `paid_run_unauthorized` and parked in
`queue/waiting/` until a human records an authorisation for that exact spec:

```bash
uv run evallab submit <spec.json>              # -> waiting, prints why
uv run evallab approve <spec-id> --actor peter # the recorded authorisation
uv run evallab tick --spec-id <spec-id>        # only this approved spec
```

`policy/standing-approvals.yaml` cannot grant this. `auto_run` is not consulted
for a billable spec at all, so listing a paid agent there changes nothing. Full
semantics, including the fail-closed cases, are in `docs/operations.md`,
"Paid execution requires a recorded authorisation".

### Z.ai OpenCode on Docker Desktop

The default `zai-opencode` profile selects `zai-coding-plan/glm-5.3-flash`.
The queue checks the owner-only OpenCode auth store for its `zai-coding-plan`
entry; a missing credential defers dispatch rather than becoming a task failure.
Credential presence is not proof of a successful provider request.

Execution requires Docker Desktop on Apple Silicon with a kernel that supports
Harbor's broker network allowlist; on unsupported kernels, the run fails
closed during container network configuration before model requests can issue.

### Terminus 2 with native Harbor tasks

Use `--agent terminus-2`. Eval Lab subclasses Harbor's upstream **Terminus 2**
only to bind its host-side model client. Its terminal loop, JSON command parser,
context summarization, terminal recordings, and ATIF writer remain upstream
implementations. Metered routes, all host-side through the same loopback proxy:

- `zai/glm-5.3-flash` and `zai/glm-5.3` — Z.ai standard API via
  `ZAI_OPENAPI_API_KEY` ($0.15/$0.50 and $1.40/$4.40 per 1M tokens). Never
  Coding Plan credentials.
- `tinker/<base>` for `Qwen/Qwen3.6-35B-A3B` ($0.54/$1.335),
  `Qwen/Qwen3.8-27B` ($1.86/$5.595), or `Qwen/Qwen3.5-9B` ($0.66/$1.995)
  — Thinking Machines Tinker's OpenAI-compatible endpoint via `TINKER_API_KEY`,
  pinned to `tinker.thinkingmachines.dev` +
  `/services/tinker-prod/oai/api/v1/chat/completions`. A fine-tuned checkpoint
  replay uses `tinker/<base>@tinker://<run-id>:train:<i>/sampler_weights/<step>`;
  it pins the same base profile, prices, and credential. Tinker's 64K context
  window is bound at the adapter, so native summarization triggers before
  overflow. Tinker reads `reasoning_effort` (`"none"` … `"xhigh"` or a float
  in [0, 0.99]; 0.9 when omitted) only from the request body, and Harbor's
  LiteLLM drops the top-level `reasoning_effort` knob for this unregistered
  model. To disable thinking, send it in the body through the harness
  `llm_call_kwargs.extra_body.reasoning_effort: "none"`; `top_p` is forwarded
  too, `top_k` is not.
- `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` — a self-hosted SGLang
  server on Modal serving that id (`--served-model-name` equal to it, MIT
  qwen3_5 hybrid, 64K context as served). The per-trial proxy runs with
  `--provider mimo_selfhosted` and pins the chat-completions base URL from
  `EVALLAB_MIMO_SELFHOSTED_UPSTREAM` (required, no default) to one Modal
  serving label or routing region: `*.modal.run` (Web Functions) or a
  documented routing region under `*.modal.direct`. The provider key comes
  from `MIMO_SELFHOSTED_API_KEY` (the server's `--api-key`) and travels
  upstream only as `Authorization: Bearer`. Before forwarding, the proxy
  forces the model's generation_config over caller values —
  `chat_template_kwargs.enable_thinking = true`, `temperature = 0.6`,
  `top_p = 0.95`, `top_k = 20` — and strips `reasoning_effort` in both
  places: SGLang's `mimo` reasoning parser only splits `<think>` when
  `enable_thinking=True`, and without it reasoning lands in content and
  breaks Terminus JSON. Self-hosted tokens have no per-token price (the
  ledger pins `(0, 0)`). Modal bills the server container per second. The
  rate is $2.8149/h: A100-80GB $2.4984/h, 4 cores $0.1886/h and 16 GiB
  $0.1279/h (modal.com/pricing, 2026-09-28).
  `mimo_selfhosted_trial_cost_usd` estimates
  `2.8149 x trial_hours / concurrency + sandbox_usd`. With zero rates the
  proxy's cost ceiling cannot trip; its request and token ceilings still
  bound the run.
  `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:har129` (HAR-129) is the
  same route on the LoRA-enabled twin server
  (`tools/modal-mimo-serve/serve_lora.py`, pointed at by the same upstream
  variable). It selects the `har129` SFT adapter through SGLang's
  `base:adapter` model name and gets the same forced generation_config and
  `(0, 0)` prices. Only the adapter names in
  `execution_contracts.MIMO_SELFHOSTED_ADAPTERS` are admitted, and a reply
  echoing the base id fails the run's model-identity check.

- `openrouter-metered/xiaomi/mimo-v2.6-flash` (HAR-104) — MiMo-V2.6-Flash via
  OpenRouter's OpenAI-compatible endpoint, pinned to
  `https://openrouter.ai:443/api/v1/chat/completions` (`EVALLAB_OPENROUTER_UPSTREAM`,
  default `https://openrouter.ai`; loopback http only for tests). The
  provider key comes from `OPENROUTER_API_KEY` and travels upstream only as
  `Authorization: Bearer`. The prefix is `openrouter-metered/` — not
  `openrouter/` — so litellm's provider lookup stays on the openai-compatible
  path (with `litellm_provider openai` the selector resolves to provider
  `openai`; verified 2026-09-29). The proxy forces
  `provider {"order": ["xiaomi"], "allow_fallbacks": false}` (endpoint tag
  `xiaomi/fp8`; pins upstream serving and price, refuses the fallback pool)
  and `reasoning {"enabled": true}` (MiMo thinking on, matching the
  self-hosted treatment), stripping any caller-supplied
  `provider`/`reasoning`/`reasoning_effort`; ledger calls carry
  `shaping_applied`. Prices: $0.14 per 1M input, $0.28 per 1M output
  (OpenRouter list price verified 2026-09-29; cache read $0.0028/M is never
  credited — the pinned endpoint reports `supports_implicit_caching=false`,
  so uncached input pricing is exact). Context is runtime-bound at 1 048 576
  input tokens with at most 131 072 completion tokens. OpenRouter's usage
  (`prompt_tokens`/`completion_tokens`; completion includes reasoning tokens)
  reconciles through the standard ledger, and its keep-alive padding (leading
  whitespace before the non-stream JSON body) is accepted. The parser is the
  stock Terminus-2 JSON parser — no `MimoToolCallParser` wrapper on this
  route; HAR-104 decides from the proof run's raw outputs whether a
  normalizer is needed. A trial's proxy admits only that trial's model
  (`EVALLAB_OPENROUTER_EXPECTED_MODEL`), even though the route table admits
  several.

- `openrouter-metered/openai/gpt-oss-120b` (HAR-104) — gpt-oss-120b through
  the same OpenRouter route, pinned to endpoint `deepinfra/bf16`
  (`provider {"order": ["deepinfra/bf16"], "allow_fallbacks": false}`; the
  full endpoint slug is required because bare `deepinfra` also matches its
  turbo and fp8 endpoints) with `reasoning {"effort": "medium"}`, the model's
  documented default. Prices: $0.037 per 1M input, $0.17 per 1M output
  (verified 2026-09-30; `supports_implicit_caching=false`). Context is
  131 072 tokens with at most 117 964 completion tokens. Both OpenRouter
  models live in one table, `OPENROUTER_ROUTES` in
  `src/evallab/execution_contracts.py`, mirrored literally in the proxy.

The MiMo server lives in `tools/modal-mimo-serve/` (see its README for the
deploy, smoke and stop commands). Daytona Tier 1/2 sandboxes cannot reach
Modal endpoints, so the model is reachable only from this controller-side
harness; never point a task container at the server. The server scales to
zero after 5 idle minutes, and a cold start took 208 s on 2026-09-29, during
which Modal answers 503. Warm the server before a trial.

The model keeps the tool-call wrappers of its training harnesses,
`<tool_call><function=NAME>…</function></tool_call>`, and almost never ends
keystrokes with a newline. The arguments come either as JSON
(`exec_command {"keystrokes": …}`) or as Qwen3-Coder XML
(`bash <parameter=command>…</parameter>`). It also writes raw newlines inside
JSON strings, which strict JSON rejects.

On this route only, `SecretSafeTerminus2` wraps the Terminus JSON parser with
`evallab.mimo_tool_calls.MimoToolCallParser`. It decodes JSON with raw control
characters allowed and does four things:
- It passes a Terminus object behind an `exec`/`exec_command` wrapper to the
  stock parser.
- It turns a turn made only of `exec`/`exec_command`/`bash` command calls
  (`keystrokes` or `command`, plus an optional `duration`) into Terminus
  commands, in order. A `description` parameter, as in Claude Code's Bash
  tool, only labels its call and is dropped; any other parameter leaves the
  turn to the stock parser.
- It strips the native closing markup (`</parameter>…</tool_call>`) from a
  bare Terminus object that ends with it.
- It appends Enter to every executed command, except empty waits and lone
  tmux key names such as `C-c`.

MiMo also ends an episode natively with a prose answer and no tool call; after
solving its task, trial 0036-e repeated one summary 50 times as parse errors
until the timeout. A prose-only turn therefore counts as `task_complete: true`
with no commands, only when all of these hold: the completion's
`finish_reason` was `stop`, the text after any `<think>` block is not blank,
it contains no tool-call markup and no JSON object, and it is not Harbor's own
"Technical difficulties" fallback. Terminus's usual confirmation turn still
applies, so the episode ends only on a second completion. Each mapped agent
step carries `extra.prose_completion: true`; every trajectory file's
`final_metrics.extra.prose_completions` and the agent metadata's
`prose_completions` count them.

Valid Terminus JSON and any other shape reach the stock parser unchanged and
get the usual parse-error feedback. Only the executed commands and the
completion flag change: the chat history, the ATIF trajectory and the rollout
details keep the raw model output.

A separate local route, `ollama_chat/qwen2.5:7b`, uses an explicitly selected
local Ollama service.

Prepare any local Harbor task package; no task-ID allowlist or new registry
admission is needed:

```bash
uv run evallab tasks prepare /absolute/path/to/harbor-task \
  --name terminus-example \
  --agent terminus-2 --model zai/glm-5.3-flash \
  --environment docker --cost-limit-usd 0.40
uv run evallab submit derived/prepared/terminus-example.json
# Review the spec and authorize its printed ID:
uv run evallab approve <spec-id> --actor peter
uv run evallab tick --spec-id <spec-id>
```

Preparation freezes the package and verifier digests. Task-declared CPU, memory,
storage, GPU, network, and separate-verifier requirements remain task inputs.
The existing eight-hour Lab deadline limit still applies; larger declared
deadlines refuse rather than being silently shortened. An explicit shorter
`--timeout-seconds` denotes a diagnostic run, not the original benchmark protocol.

Select a backend that supports those requirements. Preparation accepts `docker`,
`daytona`, `modal`, and `beam`; remote specs also require
`--estimated-cost-usd` for model plus infrastructure. GPU tasks require a
compatible remote backend, and Terminus requires a Linux terminal with tmux.
Task-provided Compose uses the backend's native multi-container support, not the
mini-SWE proxy overlay. Upstream network/phase capability checks still apply:
for example, Daytona DinD cannot change network policy dynamically. Unsupported
task/backend combinations are not a promise of universal portability.

The model client stays on the controller, so the task receives neither the
provider key nor the proxy capability. On the metered route, main, summarizer,
and retried model calls share one trial's request/token/cost ceilings. When a
ceiling is spent, the proxy answers 429 "trial budget exhausted" and the
adapter ends the agent phase with `TrialBudgetExhaustedError`. Harbor records
it like an agent timeout and still runs the verifier; the agent metadata
records `stop_reason: trial_budget_exhausted`, and cohort comparisons treat it
as budget exhaustion. An upstream error status proves no model output was
produced, so the proxy settles it as a zero-token call instead of leaving it
unresolved: a JSON usage-less 400 (SGLang's context overflow) keeps
`error: provider_http_400_no_usage` with the provider's message; any other
usage-less error records `error: upstream_error_<status>` and the caller sees
the upstream status with the provider's redacted JSON error when it parses,
otherwise a fixed JSON body (upstream error pages are never forwarded). A 2xx
with an unparseable body may still have been billed, so it stays unresolved
and fails the trial's accounting, as does an error response whose body cannot
be read. The default per-response output limit is 8,192 tokens, separate from the
cumulative
output allowance. The proxy binds an ephemeral loopback port and stops before
final accounting is collected. Stopping drains it: a call still in flight, such
as one the agent abandoned at its timeout, runs on for up to 120 s and settles
with its real usage. A call still in flight after that is marked unresolved
(`in_flight_at_shutdown`) and fails the trial's accounting.
For Daytona, the reused lifecycle wrapper sets a provider TTL of execution
timeout plus ten minutes, five-minute inactivity stop, and deletion on stop.
Remote credentials, capacity, and explicit spending approval remain prerequisites.

Completed runs are ingested into the existing PostgreSQL catalog and Parquet
projections by the queue. Inspect them without calling another model:

```bash
uv run evallab summarize runs/terminus-example
uv run evallab trajectories runs/terminus-example
uv run evallab traj outline runs/terminus-example/<trial-directory>
# Rebuild derived records from retained native evidence when needed:
uv run evallab ingest runs/terminus-example
```

Two Terminus-specific knobs:

- Harness config `trajectory_config` (exactly `raw_content` and
  `linear_history`, both booleans, both false unless set): passed through to
  the native Terminus 2 kwarg and covered by the harness-tree digest. Set
  them only for SFT export (raw LLM responses, linear segments), as
  `research/experiments/har81-mimo-sft/harness/` does; runs meant for
  evaluation or trace analysis keep the defaults. `raw_content` writes no
  `tool_calls` and `linear_history` splits a summarized run into
  `trajectory.cont-N.json` files. For trials already recorded that way, the
  ATIF projection, `traj outline`/`card` and `report run` read the calls
  the harness accepted from `extra.step_layers`
  (`step_layers.effective_tool_calls`), in stock Terminus-2 shape
  (`bash_command`, `mark_task_complete`).
- `evallab tasks replay <retained-spec> --name <n> --model <selector>` swaps
  only the model (e.g. base to checkpoint), keeping the retained task,
  harness, and ceilings; the printed cost estimate must be re-checked before
  submitting. Prior approval is never inherited.

Independent model-call capture (same placement as the z.ai lane) chains the
metered proxy into `evallab capture serve`, so Tinker trials get the same
completeness verdicts; see [the capture recipe](model-capture.md):

```bash
uv run evallab capture serve --upstream https://tinker.thinkingmachines.dev \
  --out derived/captures/<name> --port 8471
EVALLAB_TINKER_UPSTREAM=http://127.0.0.1:8471 uv run evallab tick ...
uv run evallab capture link derived/captures/<name> runs/<job>
```

The trial's `agent/` directory retains `trajectory.json`, `recording.cast`,
`terminus_2.pane`, and any summarization/continuation trajectories. Native
results, verifier logs/rewards, declared task artifacts, and the physical-call
proxy ledger in `lab-metadata.json` remain in the ordinary job directory.
Unfinished jobs are not completed-job summaries; inspect their retained trial
evidence directly rather than inventing a grade.

Native aggregate usage and physical proxy accounting are distinct. The proxy
uses conservative uncached pricing, not a provider invoice. Some upstream
length-interrupted responses can be absent from native usage totals; do not
equate ATIF step count with physical requests. See
[the analysis loop](analysis-loop.md) for evidence validity and interpretation.

Verification on September 24 used the real native Terminus/Docker/queue path
with a **scripted loopback provider and isolated catalog**: terminal execution,
parse-error recovery, native grading, ATIF/recording capture, and ingestion.
This is CPU protocol proof, not a live GLM capability result or remote/GPU
qualification. All 66 pinned TB4 task selections also compile without execution.

#### Pinned settings, rules, and skills

`--harness-tree` freezes a directory with this layout:

```text
harness/
  terminus/config.json
  terminus/AGENTS.md
  terminus/skills/<skill>/SKILL.md
  terminus-commands/<command>/SKILL.md
```

The optional config object accepts `enable_summarize`, `interleaved_thinking`,
`llm_call_kwargs`, `max_thinking_tokens`, `max_turns`, `parser_name`,
`proactive_summarization_threshold`, `reasoning_effort`, and `temperature`.
An absent config uses stock settings; nonblank rules become a native extra
instruction, and populated skill roots become native `--skill` arguments.
`llm_call_kwargs` merges over the Lab's per-call defaults, without exceeding
an enforced cumulative output cap. Model/transport/credential overrides,
unknown knobs, `terminus/context` code extensions, symlinks, special files,
and digest drift refuse before execution. Tree bytes are not rewritten.

Preparation records `harness_tree_path` and `harness_tree_sha256`, using the
existing evidence-tree digest over sorted relative paths and file bytes.
It freezes an independent copy under `runs/.prepared-harnesses/`. Execution
rechecks the pin and retains `harness-tree/`, exact rendered arguments, base
settings, and `experiment-spec.json` in the ordinary job directory. A retained
run can therefore be inspected or replayed after temporary staging is removed.
This mirrors Reef's tree layout without importing Reef.

For local execution, first start your own **cloud-disabled** Ollama service
with an already installed `qwen2.5:7b` GGUF. The Lab does not download weights
or start a server. Point it at a literal loopback endpoint:

```bash
export EVALLAB_TERMINUS_OLLAMA_URL=http://127.0.0.1:11461
uv run evallab tasks prepare library/tasks/event-summary \
  --name local-baseline --agent terminus-2 --model ollama_chat/qwen2.5:7b \
  --environment docker --harness-tree /absolute/path/to/baseline-harness
uv run evallab submit derived/prepared/local-baseline.json
uv run evallab approve <baseline-spec-id> --actor peter
uv run evallab tick --spec-id <baseline-spec-id>

# Change only the candidate harness; reuse the executed task/model/limits.
uv run evallab tasks replay runs/local-baseline/experiment-spec.json \
  --name local-candidate --harness-tree /absolute/path/to/candidate-harness
uv run evallab submit derived/prepared/local-candidate.json
uv run evallab approve <candidate-spec-id> --actor peter
uv run evallab tick --spec-id <candidate-spec-id>
```

Local qualification reads Ollama's installed-model inventory, rejects cloud
models, and records the weight digest and endpoint. The controller uses an
8,192-token context budget. Native token counts remain observed telemetry;
zero provider API charge is recorded explicitly, not used to fill missing
usage. No provider-proxy request/token/cost ceilings are claimed for this
route. Task deadlines and native harness settings still apply, and every
non-control spec still requires recorded approval. Replay never inherits an
old approval or changes the retained task; task drift refuses.

Set `declared_variable` to `harness_tree_sha256` in a normal comparison spec,
then run `evallab compare <comparison-spec.json>`. Comparison verifies retained
tree bytes against native kwargs, rules, and skill locks before treating them
as one treatment. Non-tree model/tool settings remain consequential. Full
base execution settings must match within each paired task, even when
different tasks have different limits. Cost per solved task uses all selected
attempt costs and reports missing-cost counts; no solved tasks or incomplete
cost evidence yields **unavailable**, never a fabricated zero. Native provider
estimates are not invoices, and zero local API charge is not a hardware-cost
estimate.


### Reef-process publication gate and local calibration

`library/adapters/reef_gate/` is a separate Python package with its own
`pyproject.toml` and lock. It is **not** an `evallab` runtime dependency.
`evallab_reef_gate.plugin:Factory` imports Reef only inside the Reef process;
the Lab application never imports Reef or mixes its Harbor dependency into
the Lab environment.

Select the factory with Reef's `evolution.selection` and configure it through
`EVALLAB_REEF_GATE_CONFIG`, a JSON file with these fields:

```json
{
  "alpha": 0.05,
  "min_valid_pairs": 5,
  "pass_threshold": 1.0,
  "regression_failure_threshold": 1,
  "record_dir": "/absolute/owned/run/gate-decisions",
  "reef_commit": "818997d76412f0eead7d0b4b343da701d6ce2c20"
}
```

The gate delegates episode evaluation to Reef's `BackendEvaluateMixin`.
It drops and counts a positional pair if either score is missing or invalid.
Ties remain valid evidence but leave the sign-test sample. Selection requires
the exact one-sided sign-test probability to be **strictly below** `alpha`,
at least `min_valid_pairs`, and no protected-task regression. A task is
protected only when the current harness passed every repeat; its valid
candidate failures must stay below `regression_failure_threshold` **within
that task**, not pooled across tasks. Missing scores are never fabricated
failures or wins.

Every candidate gets an exclusive JSON decision record with pairs, counts,
probability, per-task vetoes, configuration, Reef revision and timings.
Evaluation exceptions become rejections with no observed evaluation sides;
they cannot clear the current failure history by inventing successful
observations. If recording the decision fails, publication is refused.

The calibration command copies the existing `04_gate_aa.py` experiment into
the owned package; it does not edit the original script or its work directories.
Use the already-installed Reef interpreter read-only, an already-running
cloud-disabled local Ollama service, and a fresh owned output directory:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/library/adapters/reef_gate/src" \
~/Developer/reef/.venv/bin/python -m evallab_reef_gate.calibrate \
  --reef-root ~/Developer/reef --work-dir "$PWD/runs/reef-gate-aa" \
  --ollama-url http://127.0.0.1:11461 --model qwen2.5:7b \
  --condition aa --trials 30 --repeats 5 --port 18972
```

The driver refuses nonempty output directories and output inside the Reef
checkout. It checks a unique installed GGUF's digest/size, rejects cloud
model routes, URL credentials and redirects, and neither downloads weights
nor forwards ambient provider credentials/proxies into episodes. All Reef
storage, recipes, step records and decision records stay under `--work-dir`.

For the known-effect control, use a separate fresh directory with
`--condition known-effect --trials 5 --repeats 5`. All trial scenarios are
created before the first proposal: each forks the deliberately degraded
answer-style skill before any publication can advance Reef's shared head.
Each trial then proposes the original tutorial skill. This measures publication
power; it is not A/A and does not inject reference answers into the harness.

Use `--analyze-only --work-dir <existing-owned-run>` to recompute the report
without starting a service. Task identities come from the retained served
recipe; contradictory metadata is refused. Reports retain attempted/settled/
invalid counts, the publish denominator, Wilson 95% uncertainty, the original
gate-table prediction and missing episodes. Evaluator errors, insufficient-
evidence holds and rows with no usable pair do not become resolved negative
trials. Valid partial-pair comparisons retain their missing episode counts.
With no publications, published-tree identity is unavailable, not vacuously
true. Prediction assumptions and the additional regression veto are explicit.
`decision_seconds` measures decision computation, excluding record persistence;
evaluation time and full trial wall time are reported separately. Missing
timings remain unavailable and have separate counts, never imputed zeros.

An explicitly authorized API campaign can instead use
`--api-proxy-url http://127.0.0.1:<port> --model <provider-model-id>`.
This is mutually exclusive with `--ollama-url`; the proxy model is required,
and no Ollama inventory request is made. `EVALLAB_REEF_PROXY_TOKEN` supplies
only the proxy capability (rename the variable with `--api-proxy-token-env`),
not the provider key. Use an existing supervised credential proxy with explicit
request/token/cost ceilings and current conservative pricing. API provenance
records the route and model, with no fabricated local-weight digest. Retain
proxy usage separately and never pool calibration cohorts across model changes.
The native Reef process is not an OS sandbox: an owner-only provider-key file
outside its work directory does not isolate that key from same-user tool code.
Reef may retain the resolved proxy capability in its own runtime configuration;
keep that configuration private and terminate the proxy when the campaign ends.

### GLM mini-SWE on Daytona

Prepare an ordinary spec without a Python launcher, credentials, or cloud calls:

```bash
uv run evallab tasks prepare \
  ~/Developer/agent-evals/terminal-bench-4/tasks/fin-saccr-rwa \
  --name tb4-finance-daytona \
  --agent mini-swe-agent --model zai/glm-5.3-flash \
  --environment daytona --timeout-seconds 3600 \
  --cost-limit-usd 1 --estimated-cost-usd 1.25
```

This copies exactly one local Harbor task into `runs/.prepared-tasks/`, pins its
package/verifier digests, and writes `derived/prepared/tb4-finance-daytona.json`.
It neither admits a task to the registry nor submits or approves a run. The task
can come from any local Harbor package; there is no TB4-only launcher.
`--json` emits the spec, resources, warnings and next command.
Repeated identical preparation reuses the snapshot/spec; source edits cannot
mutate the snapshot, and collisions or drift refuse rather than overwrite.

Without `--timeout-seconds`, preparation preserves the task's declared **agent**
deadline. Build and verifier deadlines are not added to that agent deadline.
Missing deadlines require an explicit value; deadlines above the Lab's eight-hour
limit are never silently truncated. This example's one-hour override is a
diagnostic run, **not the full eight-hour TB4 protocol**.

The printed model ceilings default to 200 requests, 5,000,000 input tokens,
131,072 output tokens and their sum; each is overridable. Model cost is explicit.
`--estimated-cost-usd` includes model plus sandbox/build/verifier overhead, but
is an estimate, not an infrastructure dollar meter. Unmetered harnesses are not
given fictitious enforced token/cost limits.

After reviewing the generated spec and obtaining the exact run's approval:

```bash
uv run evallab submit derived/prepared/tb4-finance-daytona.json
# Copy the spec_id printed by submit into the next two commands:
uv run evallab approve <spec-id> --actor peter
uv run evallab tick --spec-id <spec-id>
uv run evallab summarize runs/tb4-finance-daytona
```

`tick --spec-id` retains all existing health, credential, policy and quota gates.
It leaves other approved work untouched, prints the selected spec's final state
and failure/defer reason, and returns nonzero if the selected work did not finish.
Do not use an unfiltered `tick` merely to launch one trial. Direct `evallab run`
remains control-only; paid agents use the queue above.

Export `DAYTONA_API_KEY` and `ZAI_OPENAPI_API_KEY` into the invoking shell before
dispatch (for example, start a new shell after configuring your existing exports).
Preparation does not need them; no credential values are written into the spec.

The launcher requires `DAYTONA_API_KEY` and the standard-API
`ZAI_OPENAPI_API_KEY`. The scoped Daytona environment stages the existing proxy
script and its private key mount on the remote VM, not in the task container.
Harness installation uses the public network; model execution switches the task
onto an internal network reaching only the metered proxy. Accounting is recovered
before sandbox deletion. This transport currently admits single-container task
packages; separate verifiers use Harbor's independent verifier environment.

Task CPU/RAM/disk requests still come from `task.toml`. Each sandbox has a
provider-side destruction deadline of the explicit execution timeout plus ten
minutes (rounded up), five-minute inactivity stop, and immediate deletion after
stop. These lifecycle limits are not an infrastructure dollar meter: budget the
agent sandbox, separate verifier, and builds independently of model tokens.
All five provider request/token/cost ceilings remain mandatory. Shorter pilot
timeouts and budget terminations are diagnostic limits, not full TB4 results.

### Teacher SFT export and Tinker training launcher (HAR-81)

Free and local: sealing a split, exporting teacher trajectories, and the
offline dry-run never spend. Only `sft_tinker train --confirm-spend` calls
the Tinker service (paid; requires `TINKER_API_KEY` in the environment).

```bash
# 1. Freeze the sealed split from the shared MiMo task catalog
#    (docs/mimo-task-catalog.md; task_versions.parquet under the primary
#    checkout's derived root, never inside a worktree). Whole split_groups
#    go to held-out in hash-rank order until each domain's requested task
#    count is reached; the sealed manifest also carries the top-level
#    {task_version_digest: train|heldout} splits map that
#    `evallab tasks catalog export-eligible --split` consumes.
uv run python -m evallab.sft_split freeze \
  --salt "$SALT" --heldout-count code=270 --heldout-count cyber=100 \
  --out split.json

# 2. Export Terminus-2 teacher trials to chat_sl conversations. Any trial on
#    a held-out task (matched by sealed task_version_digest when the pinned
#    snapshot task directory is available, else by task name/id) REFUSES the
#    export; exceptions and reward < threshold are excluded and counted by
#    reason in manifest.json. An optional `--selection FILE` picks specific
#    trials and can truncate at a given `cut_step_id`. Optional
#    `--per-turn-stride N` expands conversations into per-turn trainer rows.
uv run python -m evallab.sft_terminus export \
  --root teacher=runs/mimo-teacher --split-manifest split.json --out export/
# 3. Offline render + cost report (free; downloads tokenizer files only).
#    The renderer runs inside the isolated, locked project tools/tinker-sft
#    (tinker==0.30.4, tinker-cookbook==0.5.7, own uv.lock; never part of the
#    root environment) via `uv run --project tools/tinker-sft --locked`,
#    invoked internally — no root dependency group is needed.
uv run python -m evallab.sft_tinker dry-run \
  --data export/ --model Qwen/Qwen3.6-35B-A3B

# 4. Real training (paid; refuses without --confirm-spend). Same isolated
#    project hosts the chat_sl trainer. Writes <log-dir>/training-manifest.json
#    linking data digest -> tinker run -> final sampler_path.
uv run python -m evallab.sft_tinker train \
  --data export/ --model Qwen/Qwen3.6-35B-A3B --log-dir logs/run1 --confirm-spend
```

Known deviation, reported machine-readably in every dry-run: the pinned
`train_on_what=all_assistant_messages` renderers lack tinker-cookbook's
sequence-extension property, so earlier assistant turns train on prefixes
that differ from their generation-time prompts; the protocol keeps one
conversation per linear segment and no per-turn export.

The exporter accepts trials recorded in raw-content mode or with `step_layers`
retaining the raw proposed message (trajectories with parsed `tool_calls`
lacking raw step layers are excluded), and each Terminus continuation segment
(`trajectory.cont-N.json`) becomes its own flagged conversation. Teacher
reasoning is dropped by default (`--keep-reasoning` opts in). Optional
`--selection` admits explicit trial choices and step truncation (`cut_step_id`).
Optional `--per-turn-stride N` expands conversations into one row per kept
assistant turn with loss only on the target turn. chat_sl 0.5.7 takes
`key=value` arguments, not `--flags`; the launcher emits the verified form.
## Running on Modal (binding rules)

**Any cloud/remote execution is `escalate_to_human` per
`policy/standing-approvals.yaml`. This includes oracle-only sweeps** — they
skip model APIs but still bill Modal compute (CPU/GPU-hours). Concretely:

- Credential setup requires Peter's authorization, but authenticated clients do
  not themselves authorize compute. Peter authorized account setup on September21.
- Use the official `modal token new` browser flow for account configuration;
  keep `~/.modal.toml` owner-only. Actual cloud work still requires the exact
  queued spec's recorded authorization. Account setup does not itself qualify
  a task/harness/backend combination.
- First run of any new suite is `-n 1` to price it before `-n 5`.

Account authentication is working; it is not proof of sandbox capacity, a remaining
credit balance, or a qualified model transport. Check account metadata without
allocating compute using `modal profile list` and `modal token info`.

`tasks prepare --environment modal` refuses unimplemented container-side
metered-proxy combinations such as GLM mini-SWE before launch. Adding credits
cannot fix that transport gap. Terminus 2's host-side model transport does not
need that container-side bridge; it uses the native Modal task backend with its
actual capability and credential requirements. All remote controls and model
runs still use an explicitly prepared/submitted/approved spec and selected
`tick`; do not bypass the queue with a direct Harbor sweep.

## What agents should take from this

- Before running any task, classify it with the five rules above; never start
  a `local-heavy`/`cloud-only` task as if it were a canary.
- `--env modal` (or any non-docker `--env`) without an explicit, current
  approval from Peter is a policy violation even at $0.01.
- If a task fails locally with an OOM or platform error, check its tier before
  filing it as a task bug.
