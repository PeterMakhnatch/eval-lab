# Experience Infra interop (HAR-204): Harbor <-> Inspect AI <-> Karotte <-> verifiers

Model-free, $0-only bridges. No RL training infra. Live-Docker cells attempt
execution exactly like `evallab run` / `evallab cheat run` (direct execution,
no campaign-queue admission gate): infrastructure failures are `error` cells,
never `fail`. Unit + fixture tests (`tests/test_interop.py`) inject every
external seam and carry acceptance.

## Commands

```bash
evallab interop export-harbor library/tasks/<name> --to inspect --out DIR
evallab interop export-harbor library/tasks/<name> --to karotte --out DIR
evallab interop matrix library/tasks/<a> [library/tasks/<b>...] [--targets harbor,inspect,karotte,verifiers] [--agents oracle,nop,cheat] [--attacks a,b] [--json]
```

`--out` must be empty (exports refuse non-empty dirs). `matrix` runs every
(target, agent) cell, renders a table where control cells show pass/fail and
cheat cells show cracked/clean, flags "grading broken" when a comparable
oracle cell is not pass or a nop cell is not fail, and exits 1 on any broken
row. `--json` prints the envelope (rows + versions + evidence paths).
`--attacks` selects the cheat-ladder subset for cheat cells only.

## Pinned versions

| Component | Pin | Source |
| --- | --- | --- |
| harbor (lab lock) | 0.24.0 | uv.lock |
| harbor (uv tool, CI + this laptop) | 0.21.0 | `harbor --version` |
| inspect-ai | 0.3.276 | uv.lock (`inspect` group) |
| inspect-harbor | 1.0.0 | uv.lock (`inspect` group) |
| karotte (generic runner) | 3.0.59 | `src/evallab/interop.py: KAROTTE_PIN` |

Every run output records the exact versions that produced it (Harbor revision
per cell, pinned inspect/karotte revisions). `export-harbor --to inspect`
never imports `inspect_harbor`; matrix live cells provision the pinned inspect
stack in a subprocess (`uv run --no-project --with ...`) so the lab venv
stays lean.

## Harbor -> Inspect mapping

| Harbor | Inspect export |
| --- | --- |
| instruction.md | `Sample` input (`task.py harbor_task`) |
| solution/solve.sh | `oracle_solver` (stages solution/, runs solve.sh) |
| tests/test.sh | `harbor_test_scorer` (runs test.sh, grades reward file) |
| reward.txt / reward.json | `parse_reward_bytes`; **missing file = error, never fail** |
| task.toml [task] + [metadata] | metadata.json + Sample/Task metadata |
| environment/Dockerfile | environment/Dockerfile via compose.yaml |
| verifier.timeout_sec | scorer `timeout=` |
| network_mode != bridge/none | flagged (no sandbox equivalent; default docker network) |
| verifier.environment_mode = separate | flagged (verifier-only image must be folded in manually) |

## Harbor -> Karotte mapping

`export-harbor --to karotte` writes the same runnable env
`interop_karotte.run_cell` assembles (offline, deterministic: no karotte
import, no `uv lock`, no docker): the `environment` support package vendored
from the pinned karotte template, a generic task package, a nop fake model,
the task's own `tests/` under root-only `root_data/`, a harness
Containerfile `FROM` the Harbor student image, and a pinned project file.
Grading replays Harbor shared mode: the step hook collects the submission
paths root-only (killing student processes, wiping the workdir), the judge
restores the copies at their original absolute paths, runs the task's own
`tests/test.sh` from the workdir as root, and converts the reward files with
the embedded `interop.parse_reward_bytes`.

| Harbor | Karotte env |
| --- | --- |
| instruction.md | `Step.instructions` (generic task package) |
| task artifacts (else workdir) | `Step.submission_paths` (collected, restored, graded) |
| tests/test.sh | generic judge: restore copies, `bash /tests/test.sh`, parse reward |
| reward.txt / reward.json | score via embedded `parse_reward_bytes`; missing = 0 + error |
| solution/solve.sh | mounted at `<workdir>/solution` + `/solution` for oracle cells only |
| environment/Dockerfile (or docker_image) | harness Containerfile `FROM` the Harbor student image |
| task.toml [task] + [metadata] | Task.id + module docstring |

Lossy/impossible mappings are detected by `karotte_flags()` and written to
the generated `MAPPING.md`, never silently dropped. Only real losses of the
generic path are flagged (verifier packaging and artifact-less tasks map
faithfully, so those old codes are gone):

- `multi-service-compose`: own Compose file -> export errors (single image only).
- `network-policy`: karotte cannot enforce Harbor network policies.
- `resource-rounding`: task cpus/memory ignored; runs are bounded 2 CPU / 2 GiB.
- `separate-verifier-image`: grading runs in the task container, not the verifier image.
- `mcp-servers`: no sidecar support.
- `solution-env`: the oracle runs solve.sh without `[solution.env]` overrides.

Example: `library/tasks/transaction-reconciliation` flags only
`network-policy` (`public`).

## Cells and grading

Every target runner exposes `run_cell(task_dir, agent, attacks, *, workdir,
timeout_seconds)` returning pass/fail/skipped/error (reward >= 1.0 passes).
The scripted agents are platform-neutral plans (`scripted_agent_plan`):
oracle stages `solution/` and runs `solve.sh`; nop does nothing; cheat stages
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
  bounded 2 CPU / 2 GiB (4 CPU / 4 GiB for heavier tasks) via a watcher that
  never touches foreign containers. Expected on sound tasks: oracle pass,
  nop fail; cheat is cracked iff reward >= 1.0.

## AgentEnv / verifiers assessment

| Framework | Verdict | Evidence |
| --- | --- | --- |
| AgentEnv (`agentenv-framework`, Scale, `scaleapi/agentenv-framework`) | **already-ran** | Eval Lab ran it in `research/experiments/agentenv-mimo-bench/README.md` (HAR-190): AgentEnv 0.9.1275 on local Docker, model-free scripted controls (oracle pass, nop fail), graded by AgentEnv's `env_outcome_verifier`. The earlier row naming PyPI `agentenv` 0.0.1 was wrong: that is an unrelated stub, not Scale's framework. No Harbor converter exists (noted as a next step there); a matrix target would need one. |
| verifiers (Prime `verifiers`, v1 stack) | **needs-paid** | Full RL stack installs; episodes run as `Env.run(task, agents)` / `run_episode(task, ctx: ModelContext, ...)` — rollouts require model inference by construction, and there is no oracle/nop control concept. A verdict-equivalence run would need a paid model call, which violates the $0 rule. Mapping Harbor oracle/nop onto it would need a scripted-agent harness (new deps + design), out of scope for this slice. |

## Follow-ups (not in this slice)

1. Inspect export execution: the emitted task is `inspect eval`-ready
   (compose sandbox + oracle solver + reward scorer) but has not been
   executed end-to-end here; covered structurally by exec-against-stub tests.
   (The matrix `inspect` target runs the same task dir through the pinned
   inspect_harbor interface instead -- smoked live in the next section.)

## Live smoke (2026-10-09, this laptop, $0)

`evallab interop matrix library/tasks/transaction-reconciliation --targets
harbor inspect --agents oracle nop cheat --attacks reward_plant,skip_plant`:

| harbor:oracle | harbor:nop | harbor:cheat | inspect:oracle | inspect:nop | inspect:cheat | grading |
| --- | --- | --- | --- | --- | --- | --- |
| pass | fail | clean (0.0) | pass | fail | clean | ok |

Grading is correct on both targets (oracle passes, nop fails). The cheat
subset genuinely executed on both (harbor `attempts.json` shows
`reward_plant,skip_plant` executed, verifier reward 0.0; inspect
`agent-exec.json` shows the ladder exiting 0 in the sandbox) and stayed
clean: `reward_plant` writes workspace claims (never verifier paths) and
`skip_plant`'s `/taskwork/conftest.py` is invisible to the inspect_harbor
scorer, which re-copies pristine `tests/` from the host at score time -- a
real platform difference the matrix surfaces. Harbor 0.24.0; inspect-ai
0.3.276 + inspect-harbor 1.0.0. One earlier attempt hit a transient
`Docker daemon is not running` on two Harbor controls while the shared daemon
was under parallel load; the isolated retry passed, so it reads as a flaky
daemon, not a code path.

Karotte cells (`interop_karotte.run_cell`, karotte 3.0.59, same laptop, $0):

| task | karotte:oracle | karotte:nop | karotte:cheat (full ladder) |
| --- | --- | --- | --- |
| transaction-reconciliation | pass (1.0) | fail (0.0) | clean (0.0) |
| event-summary | pass (1.0) | fail (0.0) | clean (0.0, subset) |
| MiMo format-code-task-002552 | skipped (no solution/) | fail (0.0) | **cracked (1.0)** |

The MiMo cheat ran the same ladder `evallab cheat run` executes and cracked
identically: `git_history` applied the recovered fix (`e88159fb`), the same
mechanism as the Harbor shared-mode crack. `tamper_reward_writer` executed
there (the task setup chmods `/logs/verifier` 777, Harbor-faithful) but the
judge's `test.sh` overwrites the reward; on transaction-reconciliation it
skipped (no writable verifier log dir). Nop/cheat controls on event-summary
report karotte's own misbehavior verdict (`/app/output does not exist`).
