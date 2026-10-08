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
combination, not just an `--env` name. Terminus 2 uses host-side approved hosted
model transport with native Harbor task backends. GLM mini-SWE
has a live-proven single-container Daytona path; its container-side Modal proxy is not integrated.
Cloud runs still require explicit approval under `policy/standing-approvals.yaml`.

## Local price bench (HAR-201)

`evallab price bench` is a $0, provider-free comparison of billed historical
windows and projected eval/training spend. Its [inputs and assumptions](../research/price-bench/README.md)
include cold/drain/idle time, whole-host rounding, KV feasibility and proposed
capped probes. Rankings are not deployment qualifications or spend approval.

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

## Harbor-native reuse and local regrade (HAR-171)

`evallab preflight --spec <candidate.json>` reports **reuse, regrade, or rerun
per target trial**, using Harbor 0.24's `job_diff` over the same locally staged
task bytes and command inputs as dispatch. It creates no job or sandbox and
downloads nothing. Only finished local source jobs with the same
`lock.json` → `harbor.version` are comparable; older-version jobs remain
readable evidence but are not reused.

These native paths require the pinned Harbor runtime in the Lab interpreter:
use `uv run --extra laminar evallab ...` or install it with
`uv sync --locked --extra laminar`. A separate global `harbor` executable is
not sufficient for in-process diff planning.

Select sources in the experiment spec before approval:

```json
{"diff_sources": ["runs/prior-job"]}
```

Omitting `diff_sources` discovers finished jobs in the same explicit grid
cell or exact campaign attempt identity. A card label or research question
alone is **not** a cohort; ad-hoc new runs remain fresh. Set
`"diff_sources": []` for **fresh independent stochastic attempts** within an
existing grid. Harbor consumes each source trial UUID at most once per target
plan: growing an N-attempt job requires new trials for unmatched slots.
`preflight --spec <candidate.json> --diff <prior-job>` is a preview override
only; persist the source list in the spec before approval. Changing that list
changes the approval digest, rather than bypassing it through a sidecar.

An identical completed trial or a patch-version task change is reused; a
minor-version change regrades when the task supports a separate verifier;
major-version or non-task input changes rerun. Missing versions cannot be
reused, and changed task bytes under an unchanged version fail closed.
Preflight displays Harbor's downgrade/refusal reasons instead of silently
turning them into evidence.

The setup fingerprint is carried by the lock-covered
`agent.env.EVALLAB_SETUP_FINGERPRINT`, alongside native agent options and
environment kwargs such as `egress_lock`. Sampling, harness, parser/server
pins, budgets and lock posture therefore participate in comparison without
inventing an unknown strict `AgentOptions` kwarg. The local MiMo normalizer's
implementation bytes are hashed too: keeping its name while changing its
behavior cannot reuse old trials. Task/ledger bookkeeping, spec names and
transient staging paths are excluded; Harbor locks task, instruction and skill
contents independently.

All-reuse dispatch records `dispatch_reused` and source trial IDs/lock digests
in `queue/reasons/`; it starts no child, reserves no new attempt and does not
re-ingest old trials as new observations. Partial dispatch retains the normal
approval, lease, readiness, capacity and spend gates and passes `--diff` to
the existing guarded Harbor subprocess. Regrade-only work does not warm a
model; a cloud verifier still requires the existing paid authorization.

For a stored job and a current separate-verifier task:

```bash
uv run --extra laminar evallab regrade runs/prior-job --task path/to/separate-verifier-task \
  --name verifier-replay --dry-run
uv run --extra laminar evallab regrade runs/prior-job --task path/to/separate-verifier-task \
  --name verifier-replay --json
```

The job-level command runs Harbor's verifier-only regrade on **local Docker
only**, with no model call or cloud opt-in. It requires recorded `agent/` and
`artifacts/manifest.json`; a vanished staged task path requires an explicit
`--task`. Task names and multi-task coverage must match. Source evidence is
never overwritten or resumed in place. The new `runs/<name>/<trial>` outputs
retain native locks/manifests plus `regrade-job-receipt.json` and per-trial
receipts, separating recorded rewards from new dimensions and deltas.
Partial/refused scoring exits nonzero. The Python trial-level receipt API
remains available; the CLI uses the job as its reproducible unit.

MiMo's `reward`, `integrity`, and `reward_gated` dimensions come from the
separate-verifier variant (HAR-169), not from this replay wrapper. Compose
`rewardkit-integrity@1` **before** `separate-verifier@1`. The source must have
successfully collected the declared workspace and `/logs/agent/trajectory.json`;
a failed or missing required artifact is not reconstructed from pristine task
files. The [separate-verifier evidence](../research/experiments/har169-separate-verifier/results.md)
records the supported layout and controls. A successful local replay proves
verifier operation, not task validity or model improvement.

### Additional upstream tests (`--held-out`, HAR-197)

`evallab regrade JOB --held-out BUNDLE.json --task ORIGINAL_TASK --dry-run`
checks a separate, digest-bound upstream-test suite against the recorded source.
The preview reads files only: it does not probe Docker, build an image, create
an output job, or execute task code. `runnable` means input eligibility, not
runtime availability or research approval.

Extract a suite from a **locally retained** repository with
`python -m evallab.heldout_tests extract --help`. Supply the actual base and
upstream fix commits, the task's hidden patch, pinned image, and workdir.
Extraction reads the original Git store without mutation or transport access;
hidden postimages use a temporary Git index/object store. Added or modified
Python test definitions are compared with normalized hidden/base ASTs.
Comments, docstrings, and renamed copies do not create extra tests.
Unavailable inputs are distinct from completed `no_extra_tests` subtraction.
The output includes exact test/support bytes, selected nodes, exclusions, and
provenance—not the production fix.

An example bundle, with paths relative to the bundle's directory:

```json
{
  "schema_version": "heldout-regrade/v1",
  "tasks": [{
    "task_name": "org/task",
    "suite": "suite.json",
    "framework": "pytest",
    "command": ["python", "-m", "pytest", "-q"]
  }]
}
```

Use the interpreter and environment belonging to that task. The `unittest`
adapter takes an interpreter prefix and optionally a config-relative
`bootstrap` Python file; bootstrap and test collection share one interpreter,
so initialization such as Django settings persists. Explicit `env` values and
`timeout_sec` are bound with the other inputs.

Without `--dry-run`, this starts **local, CPU-only, no-network verifier
containers** through the existing native trial-regrade API. The source task,
kernel-probe, and native egress-control images must already be cached; missing
prerequisites are refused, not downloaded. Generated Compose disables the
verifier's network and image pulls, and no source-task healthcheck or Compose
configuration is inherited. Runs are sequential, with no model invocation.
**A zero-dollar operation does not grant a card-specific sandbox approval.**

Only the original `reward == 1` selects a trial; `integrity` and `reward_gated`
remain separate observations. The pre-hidden-test `verifier/agent.diff` is
applied on its pinned base, then authoritative test files replace agent edits.
Unrecoverable binary placeholders are refused, not silently stripped. A patch
does not recover ignored files, installed dependencies, or other runtime state.

Complete observed test results produce only `holdout_pass: 1.0` or `0.0`.
Missing inputs/reports, collection or runtime errors, skipped or unexecuted
selected tests, and timeouts stay **unscored**. Neither refusal nor missing
coverage is a zero. Original trial bytes and reward dimensions remain unchanged.
The new output groups native trial directories with the existing per-trial and
`regrade-job-receipt.json` receipts; it does not fabricate a native job invocation.

“Held-out” here means additional to the task grader, **not secret from an agent
that could read the leaked history**. AST novelty is not semantic independence
or a cheating verdict. Qualify candidate suites with correct-fix/honest controls
and deliberately hollow patches before reporting recall or false-positive rates;
report unavailable corpus and runtime coverage separately.


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

### Batch launch safety (HAR-163, repaired by HAR-164)

Every self-hosted launch requires a fresh authenticated
`POST /v1/chat/completions` readiness probe; a prior success is never cached
for another launch. Cold endpoints wait with backoff inside
`--selfhosted-warmup-seconds` (default 600 seconds). A still-cold or rejected
endpoint defers the spec rather than launching a trial into that failure.

For an unqualified batch with multiple model-backed specs, the first selected
model spec runs alone even when capacity or Daytona memory headroom clamps
the batch to one launch. The remaining specs launch only after that dispatch completes
successfully and its complete, immutable Harbor evidence contains nonempty
finite verifier grades without an infrastructure or wiring exception.
A grade of zero is a valid task failure, not a broken harness. Agent timeouts,
trial-budget stops, and loop stops also require a finite grade; a stop name
without grading is not proof of a healthy run. Graded files left behind by
a failed dispatch cannot release the batch.

An undispatched, ungraded, unavailable, unreadable, or otherwise blocked smoke
sets `queue/STOP` **before** any further launch or smoke-block event recording.
All remaining specs, including free controls, stay queued across later ticks
and fresh executors. Running trials are not killed or changed. Resolve the
cause before an operator uses `evallab resume`; no automatic resume or trial
retry is implied. Controls-only and intentionally selected single-model
dispatches do not become smoke batches. `--no-smoke-gate` is an explicit,
recorded opt-out, not the default or a substitute for paid authorisation.

Campaign failure classification uses the terminal transition into `failed`,
not later smoke-fence diagnostics. A post-run compliance refusal still opens
the campaign circuit and quarantines its remaining attempts.

### Cheap campaign execution (HAR-192: source, not deployment)

The goal is **at most $0.10 per completed valid trial**, including failed
attempts' resource costs. This is a target, not measured C20 performance.
HAR-192 authorizes design/code only: no paid smoke, deployment, new task
admission, resource downsizing, or release of the held HAR-168 specs.
HAR-181 remains parked; no native telemetry collectors are required.

**Selected implementation: one existing Modal A100 server plus native Harbor
Docker containers on a dedicated Linux host.** Use the existing spec fields
`"environment": "docker", "egress_lock": true` with `mimoagent`, `nop` or
`oracle`. The flag selects `evallab.harbor_docker:LockedDockerEnvironment`;
ordinary Docker is unchanged. Run the trusted Lab/Harbor controller on the
same host as its Docker daemon so native bind paths refer to that host.
An SSH Docker context alone is not remote staging of the controller's files.

The container has `network_mode: none` **from creation**, drops `NET_RAW`,
and has `no-new-privileges`. Native daemon inspection must confirm the
namespace, privileges and exact Harbor log/artifact binds before setup exec
or copy is allowed. Docker calls the `none` network's driver `null`.
The applied-lock receipt is the existing `egress-lock.json` contract.
Compose, extra overlays, privileged/device/host namespace access, arbitrary
host mounts, in-sandbox agent installation and phase-network switches are
refused. There is no macOS-to-public fallback and no source-package rewrite.
Native host-side exec/upload/download still work. Builds/pulls are trusted
host operations; the lock does not certify pre-existing image contents or
protect against a Docker/kernel escape. Cohort/image admission remains separate.

The queue preserves declared task CPU/RAM, observes the selected Linux daemon,
subtracts bounded existing containers plus two CPU/two GiB controller reserve,
and leaves excess work approved for a later tick. Unknown capacity or an
unlimited competing container refuses admission. This is **one dedicated
host/one queue owner**, not a distributed reservation service. Do not oversubscribe
or quietly turn an 8-GiB task into a 4-GiB task.

#### HAR-175 rule: already-qualified setups do not repeat a serial smoke

An optional, approval-digest-covered `execution` object records:

- `max_concurrent_trials` (1–40; the proposed A/B comparison uses 20);
- `model_host`: `modal` or `runpod`;
- optional `qualification`: `trial_dir` and `evidence_digest`.

The digest covers the exact native trial's `config.json`, `lock.json`,
`result.json` and applied `egress-lock.json`. Compute it with
`campaign_execution.qualification_evidence_digest(Path(trial_dir))`.
Qualification requires a completed, exception-free, finitely graded trial
(zero reward is allowed), consistent native identities and a lock-covered
setup fingerprint. When **every** model-running member matches the same
approved qualification, tick defaults to the campaign's concurrency and
records `smoke_gate_campaign_qualified`; no full trial runs alone first.
Explicit `--parallel` can lower, not exceed, that campaign cap.
Mixed/unqualified batches retain the old smoke gate. Missing, modified or
drifted evidence cannot waive it; context/parser/server-source/deploy-knob,
backend/lock and budget changes require new matching evidence.
No present-day campaign is declared qualified by this code.

An absent `execution` object preserves historical approval digests. Native
readiness, task/reference/credential gates, existing Daytona quota clamps,
and HAR-175 replacements still apply. **HAR-189's physical-spend fence is a
separate required dependency before paid activation**; this change supplies
neither settled provider charges nor a replacement budget mechanism.
This does not use `--no-smoke-gate` as an approval shortcut. HAR-168's old
65,536/`mimo` runs do not qualify 262,144/`qwen3_coder` or the new backend.

Deploy once per approved campaign, not once per trial. Both serving variants
share one GPU (`min_containers=0`, `max_containers=1`, no buffer) and an
explicit SGLang active-request cap, initially 20, distinct from task lanes.
Keep automatic shared-KV sizing and the full 262,144 context. Do not claim
that 20 full-window requests fit. A configurable idle window retains the
300-second default; long tool gaps may still cause a cold start and must be
measured. Never install an unbounded `min_containers=1` hold: the pinned
Modal autoscaler has no TTL, and a controller `finally` is not crash recovery.
Drain/stop the **owned** deployment and verify zero containers at campaign
end; do not stop another queue's shared app. A window containing only Runpod
policies skips the Modal teardown hook; a mixed window still calls it, and the
hook keeps waiting until no self-hosted work remains. Runpod Pod termination/storage cleanup is part of A's
explicitly admitted operator lifecycle, not a new automatic provider launcher.

#### Two candidate designs and conditional costs

Prices below are the 2026-10-07 public snapshots, not invoices or guarantees
of available capacity: [Runpod](https://www.runpod.io/pricing),
[Modal](https://modal.com/pricing), [Daytona](https://www.daytona.io/pricing),
[Hetzner CCX63 rates](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
and [hourly rounding](https://docs.hetzner.com/cloud/billing/faq/).
CCX63 is 48 dedicated vCPU/192 GB/960 GB NVMe; verify stock and the actual
tax-inclusive quote before admission. Twenty 2-vCPU/8-GiB limits can fit its
published envelope; twenty 4-vCPU limits cannot.

For **100 tasks × 2 = 200 trials**, assume 20 minutes per trial, 149 seconds
of startup once, prompt resource cleanup and no slowdown at the stated
concurrency. Modal GPU+4 physical CPU+16 GiB is $2.814912/h. Runpod A100-80
plus 50 GB running disk is $1.596849/h. CCX63+IPv4 is $1.6148/h, rounded to
four hours including ten minutes of preparation. Image preparation,
unpriced transfers, extra storage/tails and taxes must be added when present.

| Configuration | Per trial | 200 trials | Material condition |
|---|---:|---:|---|
| Historical HAR-168 all-in upper / 24 | ~$0.654 | ~$130.80 extrapolated | Mixed startup/idle; not an invoice or C20 forecast |
| Historical fresh four-trial upper | ~$0.320 | ~$64.02 extrapolated | Measured-time upper, not C20 throughput |
| Current Modal + current Daytona envelope, C19 | $0.1293 | $25.87 | Eleven hypothetical 20-minute waves |
| **A: Runpod A100-80 + Daytona 4 GiB, C20** | **$0.0825** | **$16.50** | Requires justified resource change and C20 admission |
| A without downsizing or lifting current C19 clamp | $0.1068 | $21.35 | Uses current $0.23148/sandbox-h envelope |
| **B: Modal A100 + CCX63 Docker, C20** | **$0.0798** | **$15.96** | Preserve task limits; host fit, lock and makespan unqualified |
| Modal A100 + Modal Sandbox, true 2 vCPU/4 GiB, C20 | ~$0.1268 | ~$25.36 | Sandbox tariff is not Function tariff |

For managed sandboxes, compute
`GPU_rate × (ceil(N/C) × trial_hours + startup_hours) + N × trial_hours × sandbox_rate`.
For B, replace the per-sandbox term with rounded shared-host hours. At 30
minutes/trial, B rises to about $23.87/$0.1194: occupancy and makespan matter
more than a small hourly price difference. All denominators must include
failed work when costing completed valid trials.

Actual frozen evidence is distinct from those assumptions. The selected
12 a1 records under `har164/runs/har168-native-selected12-6da15fc1d06c` plus
the twelve original a2 result records contain 919 recorded requests,
28,075,949 cumulative input tokens and 539,495 output tokens; median native
trial wall is 765.56 seconds and nearest-rank p95 is 2,657.67 seconds.
The selected a1 set includes the `002391-a1-infra` record; filtering original
job names by `-a1` silently changes the cohort. These are selected native
records, not 24 scientifically accepted passes. The fresh four a2 jobs
(001109/000792/001985/001198) span **834.671114 seconds**, about 17.25 physical
trials/hour, with 113,562 output tokens across that wall. This is not GPU-only
decode throughput. Dividing their $1.28 upper by hourly rates does **not**
recover measured wall time: that upper already includes startup/tail.

The supplied prompt median/p95 (29k/61k) and historical ~745k KV pool remain
planning inputs; the latter is inferred from old telemetry, not a measured
current startup pool. At 32 KiB/token, 40×61k consumes 74.46 GiB of KV before
17.5 GiB weights and workspace, so C40 is not a defensible default.
**[INFERENCE]** L40S has room for the weights plus one full window, but its
actual runtime workspace, shared KV capacity and throughput remain unqualified.
A lower hourly price does not establish cheaper completed trials. A10 cannot
fit the unchanged BF16 weights plus a full 262k window. No model/context
clipping or quantization is used to reach the target.

A uses the existing host-side authenticated model transport. **Do not use
Runpod's ordinary HTTP proxy:** its [100-second timeout](https://docs.runpod.io/pods/configuration/expose-ports)
would change the native 3600-second read contract. Use an authenticated SSH
tunnel over direct TCP to a loopback endpoint, preserving the SGLang key,
checkpoint, dtype, sampling and native tool semantics. Nothing is exposed
to the task container and no egress exception is added.

#### One combined paid comparison screen: at most $2, NOT RUN

This is a short rejection/readiness screen, not two $2 budgets or a full
trial/performance qualification. It needs a separate explicit approval.
Prebuilt images/weights, quote availability, account lock capability and
HAR-189 reservations are prerequisites; their preparation is not silently free.
If current inclusive quotes cannot fit the cap, do not create resources.

| Hard allocation envelope, including startup and deletion | Nominal cost |
|---|---:|
| One CCX63+IPv4, delete both within the first billed hour | $1.61480 |
| One owned Modal A100 window, at most 4 paid minutes | $0.18766 |
| One Runpod A100-80+50 GB running disk, at most 4 paid minutes | $0.10646 |
| Daytona: at most 855 aggregate sandbox-seconds at the unchanged rate (19×45 seconds) | $0.05498 |
| **Subtotal / remaining allowance to $2** | **$1.96389 / $0.03611** |

Prepare the host before either GPU window. Keep three separate results:
(1) simultaneous sandbox occupancy and declared-resource/tool/verifier fit;
(2) root-level DNS/HTTP/TLS/direct-IP/IPv6/metadata/host/sibling/Docker-API
denial, with a reachable positive control and working host exec/file copies;
(3) makespan/TTFT/usage/queued requests for **20 concurrent authenticated
controller-side model probes** using representative 29k/61k contexts and
an explicitly probe-only small output cap. Do not reduce real trial limits.
Run each GPU burst only after readiness and only if time remains before its
allocation deadline; incomplete bursts are failed/incomplete screens, not
permission to extend time. Include stop/absence checks in the paid windows.

Docker can request C20 only when observed host headroom permits. Daytona
must keep its existing clamp (currently C19 on an idle account), so A's
sandbox result cannot certify C20 until separate capacity admission permits
it; model C20 and sandbox C20 are different observations. Stop rather than
buy a second VM hour or hide cleanup/idle time. A fresh full-context/native
tools qualification and actual completed-trial duration tails remain unproven
if they do not fit this small screen. No held campaign is released by the plan.


### Z.ai OpenCode on Docker Desktop

The default `zai-opencode` profile selects `zai-coding-plan/glm-5.3-flash`.
The queue checks the owner-only OpenCode auth store for its `zai-coding-plan`
entry; a missing credential defers dispatch rather than becoming a task failure.
Credential presence is not proof of a successful provider request.

Execution requires Docker Desktop on Apple Silicon with a kernel that supports
Harbor's broker network allowlist; on unsupported kernels, the run fails
closed during container network configuration before model requests can issue.

### Xiaomi native mimoagent on locked Daytona (HAR-148)

Use `--agent mimoagent` with the admitted MiMo self-hosted model selector.
The Harbor adapter runs Xiaomi's actual `DefaultAgent` from
`mimo-oss@467f0a19016f0ac4d63b8d17a1f0da9ba07f232c` in an isolated
Python 3.12 subprocess. Its tools, prompts, parallelism, and step limits use
the pinned SDK; ordinary successful-answer behavior is unchanged, and no
Terminus prompt is substituted. HAR-164 adapts only transport, terminal errors,
child-log paths, and the task boundary (300s known-cold retry, structured
terminal-error metadata, logical childlog refs, and single-copy issue-header
strip), declared as `harness.additions` in the setup fingerprint. Install it
with `uv sync --project tools/mimoagent-harbor --locked`; its OpenAI 3.x graph
must not be combined with Harbor/LiteLLM's OpenAI 2.x graph.

Live Laminar tracing is an independent host-side opt-in, not a new harness
treatment. Install the host with `uv sync --locked --extra laminar` and inherit
the central `LMNR_PROJECT_API_KEY` through `keys run --` for an already
authorized launch. Preserve that extra in subsequent `uv run --locked`
commands: traced native launches use this checkout's pinned Harbor `0.21.0`,
not an unrelated globally installed CLI. The host and isolated native graphs
both pin `lmnr==0.7.64`; SDK initialization precedes the native OpenAI client.
The key never enters the task image, native worker or model traffic.
See [live Laminar observability](observability.md#live-laminar-tracing-for-native-mimo-har-165)
for identity, live export, privacy and exporter-failure boundaries.


`tools/mimoagent-harbor/swe.yaml` is byte-identical to Xiaomi's pinned file:
bash, read, write, edit **and agent delegation**, the original system template
and `Fix the following issue:` prefix, 500 steps, and antihack off. Native
parallel tools and no-tool-call → `Idle` remain unchanged. The host loop's
sandbox RPC maps execution and file uploads to Harbor's environment methods;
the task image receives neither the native controller nor a model credential.
Model queries run through the existing controller-side proxy under per-spec
authorization and request/token ceilings. It requires Daytona's provider
deny-all lock or the explicit creation-time locked Docker flag described
above; an unlocked spec and in-sandbox model credentials remain refused.
Harbor's recorded outer wall timeout can still interrupt the 500-step loop;
such a short smoke is not the original benchmark protocol.

The ordinary initial query keeps the SDK's 3600s read timeout; only explicit
transient failed transports retry, inside a 300s known-cold recovery window —
not a 300s cap on ordinary generation. A real HTTP 200 is never requeried, even
with an empty or malformed body; auth/permanent 4xx and proxy-budget refusals
fail fast, and retried attempts reuse the exact prefix.

The explicit Xiaomi RL sampling profile is temperature 1.0/top_p 0.95/top_k
20, with thinking enabled. Every completed HTTP response records its sampling,
status, finish reason and available usage; the proxy also records actual
shaping. Terminus retains its separate 0.6/0.95/20 generation-config profile.
SGLang 0.5.20 is configured with `--tool-call-parser qwen3_coder` and
`--context-length 262144`, matching the Xiaomi RL reference. Native OpenAI
structured tools are not translated into Terminus commands. Native messages, raw tool arguments
and logs remain available under `agent/mimoagent/`. Harbor ATIF includes
delegated trajectories and references so copy-check inspects child actions.
Unobserved usage stays unknown — a failed call with no response contributes no
invented tokens — and a total is populated only when every relevant call
supplied that metric. Terminal typed `ModelQueryError`/`InfraError` stops are
safe structured stop metadata (`stop_reason: infra_error` with error type, last
response status, and retry-window detail), never synthetic user turns, and carry
no traceback or host paths; they surface as Harbor run errors, not ordinary
verifier failures. A child failure is reported child-specifically and does not
override a recovered root `Idle`. The trusted AgentTool `log_file` becomes
`childlog://<stem>` before model history, raw native output, and ATIF; on-disk
logs are unchanged.
Family reporting reads IDs from the Parquet columns rather than inferring
uniform Hive partitions, so compacted and recent trial facts can coexist.


Remaining differences from Xiaomi's evaluation setup are the MiMo model and
endpoint in place of the YAML's default GPT-5, Harbor Daytona exec/uploads in
place of Kubernetes, the task image's actual working directory, the explicit
RL top_p/top_k overlay, 64K rather than 262K context, Harbor's outer budgets,
and the ATIF representation. The serving README records the capture census and
memory/concurrency recommendation; context remains unchanged. The worker's
`WRAPPER_ADDITIONS` is captured as a declared `harness.additions` treatment in
the setup fingerprint and specs, so these departures are explicit.
The pinned Xiaomi path adds its header once:
[`batch.py`](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/run/extra/batch.py)
passes the raw `problem_statement` to `agent.run`, and
[`swe.yaml`](https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/example_configs/swe.yaml)
renders `Fix the following issue:\n\n{{task}}` (config SHA `a03457e6...`).
The [RL-oss source rows](https://huggingface.co/datasets/XiaomiMiMo/MiMo-V2.6-RL-oss)
at `639865fd3374018d6cb29b9fb82dd531406fcf5f` carry the raw issue body, while the
[FineEnvs Harbor conversion](https://huggingface.co/datasets/FineEnvs/MiMo-V2.6-RL-harbor-code)
at `5746e2f0c5c61` bakes the header into `instruction.md`. The parent adapter strips one
leading `Fix the following issue:` blank-line header (LF or CRLF) and preserves
every other byte. The HAR-148 double-prefix direct control remains a historical
observation, not proof of Xiaomi-pipeline equivalence. The paired smoke also
exposed a real 64K prompt-plus-completion rejection; see the serving README
for that boundary.


The no-model HAR-148 allowlist probe **did not qualify** an in-container
MiMo/mini-SWE route: Daytona's domain allowlist left unrelated raw-IP TCP
connections open even after root rewrote `/etc/hosts`; CIDR allowlisting blocked
DNS resolution of the endpoint until it was pinned and admitted shared endpoint
IPs directly. No endpoint-only production lock mode or two-task mini-SWE smoke
is enabled. The existing host-side deny-all route remains the supported path.

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
  qwen3_5 hybrid, 262,144-token context configured). The per-trial proxy runs with
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

A historical local route, `ollama_chat/qwen2.5:7b`, used an explicitly selected
local Ollama service. Local generative-model inference is not permitted on
Peter's laptop; do not start, point at, or qualify a local Ollama service.

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
Since HAR-140 every MiMo run on Daytona (MiMo-family model or MiMo-dataset
task, including nop/oracle census runs) additionally passes
`egress_lock=true` to the bounded environment, blocking sandbox egress after
agent setup through Daytona's runner-side firewall; each trial records the
outcome in `egress-lock.json`, and a lock failure ends the trial as `infra`.
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
  --out derived/captures/<name>
ENDPOINT=$(python -c "import json; print(json.load(open('derived/captures/<name>/capture.json'))['endpoint'])")
EVALLAB_TINKER_UPSTREAM="$ENDPOINT" EVALLAB_MODEL_CAPTURE=1 EVALLAB_MODEL_CAPTURE_DIR=derived/captures/<name> uv run evallab tick ...
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

Local-model execution via Ollama is not permitted on Peter's laptop: do not
start a local Ollama service, point the lab at one, or run a local-baseline
recipe. Use an explicitly authorized hosted route instead. To compare a changed
candidate harness against a retained baseline spec:

```bash
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
Use the already-installed Reef interpreter read-only, an explicitly authorized
API proxy (local Ollama inference is not permitted on Peter's laptop), and a
fresh owned output directory:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
PYTHONPATH="$PWD/library/adapters/reef_gate/src" \
~/Developer/reef/.venv/bin/python -m evallab_reef_gate.calibrate \
  --reef-root ~/Developer/reef --work-dir "$PWD/runs/reef-gate-aa" \
  --api-proxy-url http://127.0.0.1:<port> --model <provider-model-id> \
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

### Shared Daytona capacity admission (HAR-144)

Every Daytona launcher uses the bounded lifecycle wrapper. Before a child
launch, and atomically before any Harbor snapshot build or sandbox creation,
it reads the current key's organization, live resource quotas, and the complete
paginated inventory across all lanes. Unknown identity, incomplete inventory,
unsupported target/class, or unavailable quota reads refuse the launch.
Queue preflight refusals return to `waiting`, not a poisoned failed run.

The verified 2026-10-01 Tier-2 `us/container` limits are **100 CPU / 200 GiB RAM /
300 GiB disk**, with **4 CPU / 8 GiB RAM / 10 GiB disk per sandbox**.
`policy/daytona-limits.yaml` records the dated API/dashboard sources. Admission
uses the smaller of committed and live quotas, with a 20% reserve: **80 CPU /
160 GiB RAM / 240 GiB disk**. A separate concurrent-sandbox cap was not exposed;
the inventory count is reported, not tested against an invented ceiling.
Whole-GiB provider allocations round positive MiB task requests upward;
omitted dimensions reserve the verified per-sandbox maximum, not guessed SDK
defaults. Resolved snapshot allocations are checked again before creation.

Controllers on this host share an `fcntl`-locked pending ledger at
`$XDG_STATE_HOME/evallab/daytona-admission` (default
`~/.local/state/evallab/daytona-admission`). Keep that state root identical
across lanes/worktrees. The lock spans fresh reads and reservation persistence.
Retries hold their owned high-water allocation until matching live inventory
replaces it; ambiguous creation failures never immediately free capacity.
Stopped containers still consume disk. This is host-shared coordination plus
organization-wide observation, **not a distributed lock across separate hosts**.

Each trial writes private `daytona-usage.json` evidence: admission, a 15-second
monitor sample, source/time, quotas, safety margin, pending allocations, and
live inventory. Only a confirmed sandbox GET 404 records disappearance.
Current or at-most-30-second-old near-limit pressure is capacity correlation,
not a confirmed provider deletion cause; stale/unavailable reads remain
unknown. `evallab report run` and processed trial pages show these distinctions.
Old trials without records never acquire fabricated zero usage.

The billing wallet/tier API returned 401 for this key; dated dashboard wallet
balances are recorded as observations, not a live credit cap or permission to
spend. Capacity admission does not replace financial approval. Peter's
2026-10-01 dashboard evidence confirms usage-limit loss of G5 wave-3 stock
sandboxes `001241`, `001626`, and `001765`, superseding the earlier unknown/audit
403 diagnosis for those three; the frozen run record itself is not rewritten.

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
Use `--linear-card HAR-NNN` with the exact authorized Linear card to preserve
explicit spend provenance; it does not grant spending permission.
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
