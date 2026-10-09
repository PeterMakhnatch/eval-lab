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
| karotte (validate-only) | 3.0.59 | `src/evallab/interop.py: KAROTTE_PIN` |

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

| Harbor | Karotte scaffold |
| --- | --- |
| instruction.md | `Step.instructions` (environment/task.py) |
| task artifacts | `Step.submission_paths` = student_data |
| tests/test.sh | `ExecutableJudge` via environment/judge_entry.py |
| reward.txt / reward.json | judge score float; missing file = score 0 + error metadata |
| solution/solve.sh | root_data reference only (never executed) |
| environment/Dockerfile | Containerfile (single image) |
| task.toml [task] + [metadata] | Task.id + module docstring |

Lossy/impossible mappings are detected by `karotte_flags()` and written to
the generated `MAPPING.md`, never silently dropped:

- `multi-service-compose`: own Compose file -> single Containerfile only.
- `verifier-as-root`: test.sh apt/sudo assumes root; judges may be confined.
- `network-policy`: Harbor network_mode has no karotte equivalent.
- `resource-rounding`: cpus/memory_mb do not map 1:1 onto hardware buckets.
- `separate-verifier-image`: dedicated verifier image must be merged.
- `no-submission-paths`: no artifacts -> whole-workdir scoring.
- `mcp-servers` / `solution-env`: no karotte mapping / documentation only.

Example: `library/tasks/transaction-reconciliation` flags
`verifier-as-root` (test.sh `apt-get install`), `network-policy` (`public`),
`no-submission-paths` (no artifacts).

## Cells and grading

Every target runner exposes `run_cell(task_dir, agent, attacks, *, workdir,
timeout_seconds)` returning pass/fail/skipped/error (reward >= 1.0 passes).
The scripted agents are platform-neutral plans (`scripted_agent_plan`):
oracle stages `solution/` and runs `solve.sh`; nop does nothing; cheat stages
the stdlib-only `src/evallab/cheat_ladder.py` (same ladder `evallab cheat run`
uses) and runs it with the attack subset. Targets are wired through the
`MATRIX_TARGETS` registry in `src/evallab/interop.py` (harbor + inspect;
karotte/verifiers at integration).

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

## AgentEnv / verifiers assessment

| Framework | Verdict | Evidence |
| --- | --- | --- |
| AgentEnv (`agentenv-framework`, Scale, `scaleapi/agentenv-framework`) | **already-ran** | Eval Lab ran it in `research/experiments/agentenv-mimo-bench/README.md` (HAR-190): AgentEnv 0.9.1275 on local Docker, model-free scripted controls (oracle pass, nop fail), graded by AgentEnv's `env_outcome_verifier`. The earlier row naming PyPI `agentenv` 0.0.1 was wrong: that is an unrelated stub, not Scale's framework. No Harbor converter exists (noted as a next step there); a matrix target would need one. |
| verifiers (Prime `verifiers`, v1 stack) | **needs-paid** | Full RL stack installs; episodes run as `Env.run(task, agents)` / `run_episode(task, ctx: ModelContext, ...)` — rollouts require model inference by construction, and there is no oracle/nop control concept. A verdict-equivalence run would need a paid model call, which violates the $0 rule. Mapping Harbor oracle/nop onto it would need a scripted-agent harness (new deps + design), out of scope for this slice. |

## Follow-ups (not in this slice)

1. Karotte fake-model run: the scaffold ships a nop-behavior
   `environment/fake_model.py` (`get_messages` returns `[]`, so a run must
   FAIL like a nop control), but executing it needs the karotte env image
   built around the scaffold (`karotte create-env` + container build). That
   is a container-building follow-up, not validate-only work.
2. Karotte/verifiers live cells: runners land in `src/evallab/interop_karotte.py`
   / `src/evallab/interop_verifiers.py` and wire into `MATRIX_TARGETS`; the
   `matrix` command runs them with no interop.py change.
3. Inspect export execution: the emitted task is `inspect eval`-ready
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
