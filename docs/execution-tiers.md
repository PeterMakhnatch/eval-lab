---
status: living
audience:
  - runner
  - operator
---

# Execution tiers: what runs where, and what it costs

Operator workflow updated 2026-09-22. Machine inventory entries retain their
observation dates; they are not universal backend qualifications. The spend
rules below remain binding.

## The one-line summary

Harbor supplies the execution backends; Eval Lab prepares bounded specs and
preserves their evidence. Compatibility belongs to a task/harness/model/backend
combination, not just an `--env` name. GLM mini-SWE has a live-proven
single-container Daytona path; its Modal proxy transport is not integrated.
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

## Running on Modal (binding rules)

**Any cloud/remote execution is `escalate_to_human` per
`policy/standing-approvals.yaml`. This includes oracle-only sweeps** — they
skip model APIs but still bill Modal compute (CPU/GPU-hours). Concretely:

- Credential setup requires Peter's authorization, but authenticated clients do
  not themselves authorize compute. Peter authorized account setup on September21.
- Use the official `modal token new` browser flow for account configuration;
  keep `~/.modal.toml` owner-only. Actual cloud work still requires the exact
  queued spec's recorded authorization. Modal account setup does not qualify
  the GLM proxy transport on Modal.
- First run of any new suite is `-n 1` to price it before `-n 5`.

Account authentication is working; it is not proof of sandbox capacity, a remaining
credit balance, or a qualified model transport. Check account metadata without
allocating compute using `modal profile list` and `modal token info`.

`tasks prepare --environment modal` refuses the unimplemented Lab metered-proxy
combinations before launch and identifies the integration gap. Adding credits
cannot fix that transport gap. Upstream-supported control runs still use an
explicitly prepared/submitted/approved spec and selected `tick`; do not bypass
the queue with a direct Harbor sweep.

## What agents should take from this

- Before running any task, classify it with the five rules above; never start
  a `local-heavy`/`cloud-only` task as if it were a canary.
- `--env modal` (or any non-docker `--env`) without an explicit, current
  approval from Peter is a policy violation even at $0.01.
- If a task fails locally with an OOM or platform error, check its tier before
  filing it as a task bug.
