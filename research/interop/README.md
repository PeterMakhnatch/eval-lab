# Experience Infra interop (HAR-204): Harbor <-> Inspect AI <-> Karotte <-> verifiers

Model-free, $0-only bridges. No RL training infrastructure. Every runnable cell
uses the existing local Docker capacity probe before image preparation; unknown
capacity, unbounded competing containers and insufficient CPU/RAM refuse it.
Admission, sandbox setup and detected runner failures are `error`, not task
scores. This is not a guarantee about upstream reward fallbacks (see below).
Deterministic tests prove contracts, **not** end-to-end destination execution.

## Current acceptance — 2026-10-17

**HAR-204 remains blocked on admitted local execution.** The actual command
`evallab interop matrix library/tasks/event-summary --targets harbor inspect
karotte verifiers --agents oracle nop --json` returned **2**, with all eight
cells `error` and grading `incomplete`. Harbor, Inspect and Karotte were refused
because an existing shared-daemon container has unbounded CPU or memory.
Verifiers rejected the real task's unenforced disk limit and unbuilt separate
verifier image before Docker mutation. No shared service was altered, no
container/model/cloud/price probe was started, and no resources were downsized.

The **10:38 AM ET resume on 2026-10-17** did not clear that dependency:
the normal two-sandbox admission probe again refused the daemon, before any
image preparation. The [resume inventory](../evidence/har204-admission-resume-20261017.json)
identifies three existing shared services without CPU/RAM bounds
(`eval-lab-postgres-1`, `fxtr-postgres`, and the state-journal service). The
selected daemon actually reports **7.75 GiB RAM**, not the historical 24-GiB
inventory, and two other owners' bounded task containers already reserve 7 GiB
before the 2-GiB controller reserve. An authorized shared-runtime owner must
restore normal admission and sufficient headroom; merely bounding the services
would not make that observed occupancy fit. This run changed no shared services
and did not retry task execution after refusal. The card remains **blocked**,
not complete because its dependency cards were closed.

The **11:17 AM ET recheck** also refused normal admission, with the same three
unbounded services still present. The [recheck receipt](../evidence/har204-admission-recheck-20261017.json)
retains that refusal, fresh regression/CLI proof and source-integrity results.
It also corrects two contradictory source disclosures: detected failures are
errors but verifiers' upstream reward fallback can still become a task zero;
Karotte's collected-copy grading is not Harbor shared-mode custody. Actual
Inspect/Karotte exports exercised the corrected wording, without building or
starting a sandbox. No live-parity acceptance is claimed.

The **12:05 PM ET fifth-resume probe** still refused normal admission before
image preparation. Its [source-citation errata](../evidence/har204-source-citation-errata-20261017.json)
preserve the original evidence while correcting two insufficient native-source
references: Compose/MCP declarations and separate-verifier/collected-copy
custody. All sixteen exclusions remain source-qualified support boundaries,
not live destination qualification. The runtime prerequisite remains open.

The [dated evidence receipt](../evidence/har204-interop-20261017.json) separates:

- **Actual source regression:** the old Inspect export omitted `events.jsonl`
  from the Docker `COPY` context and included `solution/` and `tests/` in that
  context. The revised context preserves the real input and excludes hidden
  inputs. Real `docker compose config` resolves 1 CPU, 512 MiB and `/app`.
- **Actual framework construction, not execution:** the emitted real task loads
  as Inspect 0.3.276 `Task` objects for oracle/nop and as Karotte 3.0.59 `Task`
  and `Step` objects. No solver, judge or sandbox ran in these construction probes.
- **Historical AgentEnv evidence:** the retained eleven-control receipt was
  read and its published SHA-256 verified, including zero model calls and the
  reachable-data-plane limitation. This is not a Harbor conversion.
- **Outstanding acceptance:** run the emitted Inspect task's oracle and nop,
  run the converted Karotte task's oracle and nop, and compare their real grades
  to same-version native Harbor controls after normal local admission succeeds.
  The historical matrix below does not discharge this requirement.

## Commands

```bash
evallab interop export-harbor library/tasks/<name> --to inspect --out DIR
evallab interop export-harbor library/tasks/<name> --to karotte --out DIR
evallab interop matrix library/tasks/<a> [library/tasks/<b>...] [--targets harbor,inspect,karotte,verifiers] [--agents oracle,nop,cheat] [--attacks a,b] [--json]
```

`--out` must be empty (exports refuse non-empty dirs). `matrix` runs every
(target, agent) cell, renders a table where control cells show pass/fail and
cheat cells show cracked/clean, and distinguishes wrong controls (`broken`)
from unscored/skipped/missing controls (`incomplete`). Exit status is 1 for
wrong controls, 2 for incomplete execution, and 0 only when every requested cell
is scored. `--json` prints the envelope; the same envelope and per-cell admission
records persist under the owned worktree's `runs/interop/`.
`--attacks` selects the cheat-ladder subset for cheat cells only.

## Pinned versions

| Component | Pin | Source |
| --- | --- | --- |
| harbor (lab lock) | 0.24.0 | uv.lock |
| harbor (historical tool runs) | 0.21.0 | older receipts; not a current-runtime claim |
| inspect-ai | 0.3.276 | uv.lock (`inspect` group) |
| inspect-harbor | 1.0.0 | uv.lock (`inspect` group) |
| karotte (generic runner) | 3.0.59 | `src/evallab/interop.py: KAROTTE_PIN` |

Envelopes record the detected Harbor command and configured adapter pins;
failed admission does not establish that a destination framework ran.
`export-harbor --to inspect` never imports `inspect_harbor`; matrix Inspect
cells provision the pinned stack in a subprocess (`uv run --no-project
--with ...`) so the lab venv stays lean.

## Harbor -> Inspect mapping

| Harbor | Inspect export |
| --- | --- |
| instruction.md | `Sample` input (`task.py harbor_task`) |
| solution/solve.sh | oracle solver only; `-T agent=nop` never stages the solution |
| tests/test.sh | scorer clears stale rewards, runs test.sh in the task workdir |
| reward.txt / reward.json | `parse_reward_bytes`; **missing file = error, never fail** |
| task.toml [task] + [metadata] | metadata.json + Sample/Task metadata |
| environment/ or docker_image | unchanged Dockerfile context; prebuilt-image overlays are not replayed |
| CPU / RAM / workdir | bounded Compose service; prebuilt-image WORKDIR can be lost |
| verifier.timeout_sec | scorer `timeout=` |
| network / user / environment overrides / MCP | explicitly reported when not replayed |
| separate verifier / GPU / storage / agent timeout | explicitly reported, not equivalent |

Exports refuse multi-service Compose tasks rather than emitting a broken single
container. Large or symlinked oracle helpers in the matrix Inspect upload path
are refused, not silently omitted. The matrix's Inspect target uses
`inspect_harbor`; loading the pure `export-harbor --to inspect` output is a
different path. Its actual sandbox execution is still outstanding. Direct
`inspect eval` is outside Eval Lab admission and must not be used after an
admission refusal.

## Harbor -> Karotte mapping

`export-harbor --to karotte` writes the same runnable env
`interop_karotte.run_cell` assembles (offline, deterministic: no karotte
import, no `uv lock`, no docker): the `environment` support package vendored
from the pinned karotte template, a generic task package, a nop fake model,
the task's own `tests/` under root-only `root_data/`, a harness
Containerfile `FROM` the Harbor student image, and a pinned project file.
Grading has different custody from Harbor shared mode: the step hook collects
submission paths root-only, kills student processes and wipes the workdir.
The judge restores those copies, runs the task's own `tests/test.sh` as root,
and converts its reward. Agreement does not erase this isolation difference.

| Harbor | Karotte env |
| --- | --- |
| instruction.md | `Step.instructions` (generic task package) |
| task artifacts (else workdir) | `Step.submission_paths` (collected, restored, graded) |
| tests/test.sh | generic judge: restore copies, `bash /tests/test.sh`, parse reward |
| reward.txt / reward.json | missing/malformed/timeout = error; never a valid task zero |
| solution/solve.sh | mounted at `/solution` for oracle cells only |
| environment/Dockerfile (or docker_image) | harness Containerfile `FROM` the Harbor student image |
| task.toml [task] + [metadata] | Task.id + module docstring |

Detected losses are emitted by `karotte_flags()` in `MAPPING.md`:

- `submission-custody`: collected-copy grading, not Harbor's live filesystem.
- `post-start-resource-limits`: CPU/RAM are applied by `docker update` after
  startup, not at creation. A run with unapplied bounds is now an error.
- `multi-service-compose`: export refuses single-image conversion.
- `network-policy` / `agent-network-policy`: Harbor network policies and phase
  changes are not enforced.
- `separate-verifier-image`: no fresh verifier image or artifact-transfer boundary.
- `agent-user` / `verifier-user`: Karotte student identity and root grading replace
  task-specified users.
- `mcp-servers`, `solution-env`, `environment-env`: unsupported lifecycle/overrides.
- `storage-gpu-limits`: only CPU and RAM are bounded.
- `agent-timeout` / `verifier-timeout`: whole-run timeout and 300–2100-second
  verifier clamp differ from Harbor's phase budgets. The real event-summary
  export demonstrates 60 seconds becoming 300 seconds.

Setup is baked/cached into the Karotte image, not rerun at every container start;
the harness also adds its own Python, packages and identity changes. These are
additional environment drift, not proof of task equivalence. A fatal transcript,
judge error metadata, nonzero runner exit or unapplied resource bound is an
infrastructure error, even when a numeric placeholder score is present.

## Cells and grading

Every target runner exposes `run_cell(task_dir, agent, attacks, *, workdir,
timeout_seconds)` returning pass/fail/skipped/error (reward >= 1.0 passes).
The scripted agents are platform-neutral plans (`scripted_agent_plan`):
oracle runs `bash /solution/solve.sh` (Harbor's oracle convention; each runner
provides the task's `solution/` at `/solution` for oracle cells only, and
cheat cells see neither `solution/` nor `tests/`); nop does nothing; cheat stages
the stdlib-only `src/evallab/cheat_ladder.py` (same ladder `evallab cheat run`
uses) and runs it with the attack subset. Targets are wired through the
`MATRIX_TARGETS` registry in `src/evallab/interop.py`.

- `harbor:oracle` / `harbor:nop`: native verdicts through
  `Executor.execute_direct` (docker, $0). `harbor:cheat` reuses the
  `evallab cheat` lane. Expected on sound tasks: oracle pass, nop fail.
- `inspect:oracle/nop/cheat`: pinned `inspect_harbor.harbor(path=...)` with a
  bounded local docker sandbox (task resources, else 2 CPU / 2048 MB) and a
  generated scripted solver that stages the plan files via
  `sandbox().write_file` and runs the plan command via `sandbox().exec`
  (nop is a no-op solver), graded by the inspect_harbor scorer with
  `mockllm/model` (no inference). Sandbox containers verifiably ours
  (`hb__*` task image) are removed afterwards; anything else is reported,
  never touched.
- `karotte:oracle/nop/cheat`: pinned karotte 3.0.59 `run --runtime docker
  --use-fake-model` (scripted `get_messages`, no inference) in the exported
  env image; grading is karotte's own `collect_submission` +
  `ExecutableJudge` path. Run containers (`karotte_run_<id>`, `--rm`) are
  bounded (task-declared cpus/memory_mb, else 2 CPU / 2 GiB) via a watcher
  that never touches foreign containers. Expected on sound tasks: oracle
  pass, nop fail; cheat is cracked iff reward >= 1.0.
- `verifiers:oracle/nop/cheat`: Prime `verifiers` 0.3.1 (opt-in
  `xplat-verifiers` group plus the Harbor runtime:
  `uv sync --group xplat-verifiers --extra laminar`) `HarborEnv`
  on the local `docker` runtime with a model-free scripted harness
  (`src/evallab/vf_scripted_harness.py`: stages the plan files, runs the
  plan command, makes zero model calls). Dockerfile-only tasks are built
  locally first (verifiers never builds images). Each cell records the
  isolation mode that graded it (`shared`, or `separate` when the task
  declares a separate verifier); `run_cell(..., isolation="shared")` forces
  shared grading and records discarded isolation. Known unsupported contracts
  are refused before image building: users/additional services, Compose,
  unbuilt separate verifier Dockerfiles, advisory-only disk limits, unsupported
  multi-step/environment features and incompatible phase/CIDR network policies.
  Declared separate CPU/RAM limits are preserved, with conservative admission
  reserving two sandboxes at the larger of agent/verifier requirements.

These limits were checked against the actual locked verifiers 0.3.1 wheel
(`105cd114184625895b3d1a07ddc418f08a0ddadcd9cb20dad5acb763ccb01725`):
`tasksets/harbor/taskset.py` (`parse_task`, `parse_verifier_extras`,
`parse_verifier_environment`), `runtimes/docker/__init__.py` (`DockerConfig.disk`
is advisory), and `configs/runtime.py` (`network_rule_matches` uses host globs).
The dated receipt preserves actual parser results and source hashes. No paid
probe or fallback environment establishes compatibility.

The scripted verifiers oracle does not apply `solution.env`. More importantly,
verifiers 0.3.1 `_graded` falls back to **0.0** if both reward formats are missing
or malformed (`tasksets/harbor/taskset.py:300-338`); the adapter cannot currently
distinguish that fallback from a genuine task failure. Do not treat that
upstream zero as proof that grading infrastructure succeeded. The opt-in
verifiers group alone also does not install Harbor: real-parser admission
checks failed without the extra above and passed after installing Harbor 0.24.0.

All scripted destinations resolve the workdir from task.toml
`[environment].workdir`, else the local final-stage Dockerfile `WORKDIR`, else
`/`. This is **not** full Harbor equivalence: for a prebuilt image with no local
workdir declaration Harbor preserves the image's WORKDIR; the helper does not
inspect it.

## Source-qualified conversion exclusions

Both generated `MAPPING.md` files now carry the complete known compatibility
boundary, separately from detected per-field notes. An empty field-specific
list is not a lossless-conversion claim. The
[pinned-source audit](../evidence/har204-conversion-limits-20261017.json)
records source hashes and the native/adapter evidence for:

- Text-first scalar reward parsing versus Harbor's JSON-first keyed rewards,
  including pure Inspect's missing finiteness guard. Inspect-Harbor matrix
  scoring is a separate implementation, not proof of pure-export semantics.
- User overrides (including UID 0), verifier env/network phases, legacy policy
  declarations, healthchecks/readiness, and omitted prebuilt-image overlays.
- Build/phase budgets, post-start Karotte bounds, unmapped GPU/TPU/storage,
  legacy resource fields, OS, skills, and the Debian/apt-based harness changes.
- Collect hooks and artifact source/service/exclude/destination details;
  multi-step lifecycle/reward aggregation; normalized Karotte IDs and loss of
  task version/authors/metadata.
- File modes/symlinks/empty directories, forced Bash verifier execution, and
  the pure Inspect export's missing keepalive override. Depending on image CMD,
  the latter may prevent a live service from remaining up **[INFERENCE]**;
  no sandbox run was admitted to resolve it.

These are explicit support exclusions, not newly implemented compatibility.
Actual source is evidence of a missing mapping; it is not evidence that the
remaining mapping executed successfully. The historical AgentEnv receipt was
freshly hashed again and matches the published digest, but remains an eleven-
control historical run with a lexical report proxy and reachable extra data
planes, not a Harbor conversion. The former worktree paths named by the earlier
interop receipt are no longer available; its raw-probe hashes are retained
historical references, not newly inspected artifact bytes.

## AgentEnv assessment

| Framework | Verdict | Evidence |
| --- | --- | --- |
| AgentEnv (`agentenv-framework`, Scale, `scaleapi/agentenv-framework`) | **already-ran** | Eval Lab ran it in `research/experiments/agentenv-mimo-bench/README.md` (HAR-190): AgentEnv 0.9.1275 on local Docker, model-free scripted controls (oracle pass, nop fail), graded by AgentEnv's `env_outcome_verifier`. The earlier row naming PyPI `agentenv` 0.0.1 was wrong: that is an unrelated stub, not Scale's framework. No Harbor converter exists (noted as a next step there); a matrix target would need one. |

## Remaining live proof

After the existing admission probe succeeds without changing someone else's
services, run the converted task in both destinations and native Harbor at pinned
task/framework versions. Preserve actual oracle/nop grades and native destination
logs; constructor checks, skipped cells and the historical table are not parity.
Admission is a fresh capacity observation, **not** a host-global reservation
against unrelated concurrent launchers.

## Historical reported matrix (2026-10-09, not current acceptance)

`evallab interop matrix library/tasks/transaction-reconciliation
<MiMo format-code-task-002552> --targets harbor inspect karotte verifiers
--agents oracle nop cheat --json` (Harbor 0.24.0; inspect-ai 0.3.276 +
inspect-harbor 1.0.0; karotte 3.0.59; verifiers 0.3.1):

| task | target | oracle | nop | cheat (full ladder) |
| --- | --- | --- | --- | --- |
| transaction-reconciliation | harbor | pass | fail | **cracked (1.0)** |
| transaction-reconciliation | inspect | pass | fail | **cracked (1.0)** |
| transaction-reconciliation | karotte | pass | fail | clean (0.0) |
| transaction-reconciliation | verifiers (shared) | pass | fail | clean (0.0) |
| MiMo 002552 | harbor | no solution | fail | **cracked (1.0)** |
| MiMo 002552 | inspect | no solution | fail | **cracked (1.0)** |
| MiMo 002552 | karotte | no solution | fail | **cracked (1.0)** |
| MiMo 002552 | verifiers | error | error | error (`[verifier].user` unsupported) |
| event-summary | harbor / inspect / karotte / verifiers | pass | fail | clean on all four |

The event-summary row and a re-run of transaction-reconciliation (same
verdicts) come from the final head after review fixes: oracle runs
`bash /solution/solve.sh` everywhere and cheat cells see no `solution/` or
`tests/`.

Grading is correct wherever a control ran (oracle pass, nop fail). What the
cheat cells mean:

- transaction-reconciliation: a follow-up `--attacks tamper_reward_writer`
  run alone cracks both harbor and inspect -- a detached background loop
  holds `/logs/verifier/reward.*` at 1 while the verifier runs in the same
  container. On karotte (grading after root-only collection, student
  processes killed) and on verifiers (shared mode here, but the verifier log
  dir does not exist for the agent and stale reward files are cleared before
  grading) the attack finds no writable verifier log dir at agent time and
  cannot apply.
- MiMo 002552: on harbor every tamper attack plus `git_history` executes
  (each tamper cracked it alone in `research/experiments/cheat-tamper-ladder`);
  on karotte only `git_history` matters -- it applies the future fix found in
  unreachable git objects (`e88159fb`), i.e. answer leakage baked into the
  image. No grader isolation stops that; it is a task-authoring defect
  (strip future history, `strip_future_history.py`).
- Inspect re-copies pristine `tests/` from the host at score time, so a
  conftest planted in the upload target does not survive there, but its
  scorer still reads the reward from the agent's container.

Practical reading: run training-relevant tasks where the cheat cell is
clean and the controls are correct; a cheat cracked on every platform points
at the task, not the harness.
