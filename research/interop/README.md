# Experience Infra interop (HAR-204): Harbor <-> Inspect AI <-> Karotte

Model-free, $0-only bridges. No RL training infra. All live-Docker paths are
admission-gated through the repo's own `docker_available_resources` gate:
when the shared laptop daemon refuses, commands print
`shared daemon not admitted` and skip instead of failing. Unit + fixture
tests (`tests/test_interop.py`) carry acceptance.

## Commands

```bash
evallab interop export-harbor library/tasks/<name> --to inspect --out DIR
evallab interop export-harbor library/tasks/<name> --to karotte --out DIR
evallab interop run-inspect library/tasks/<name>
evallab interop parity library/tasks/<a> library/tasks/<b> [--targets harbor,inspect,karotte-validate]
```

`--out` must be empty (exports refuse non-empty dirs). `parity` exits 1 on a
verdict disagreement, 0 when everything comparable agrees (or nothing is
comparable, e.g. all skips).

## Pinned versions

| Component | Pin | Source |
| --- | --- | --- |
| harbor (lab lock) | 0.24.0 | uv.lock |
| harbor (uv tool, CI + this laptop) | 0.21.0 | `harbor --version` |
| inspect-ai | 0.3.276 | uv.lock (`inspect` group) |
| inspect-harbor | 1.0.0 | uv.lock (`inspect` group) |
| karotte (validate-only) | 3.0.59 | `src/evallab/interop.py: KAROTTE_PIN` |

Every run output records the exact `harbor_rev` that produced the native
verdict. `export-harbor --to inspect` never imports `inspect_harbor`;
`run-inspect` provisions the pinned inspect stack in a subprocess
(`uv run --no-project --with ...`) so the lab venv stays lean.

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

## Target verdicts

- `harbor:oracle` / `harbor:nop`: native verdicts through the repo's guarded
  `Executor.execute_direct` (docker, $0). Expected on sound tasks:
  oracle pass, nop fail.
- `inspect:oracle`: pinned `inspect_harbor.harbor(path=...)` + oracle solver +
  local docker sandbox, `mockllm/model` (no inference). nop is `n/a`
  (inspect_harbor ships no nop solver).
- `karotte-validate`: structural validation of the exported scaffold against
  pinned karotte (import Task, instantiate, check steps/instructions/judge/
  submission_paths). Validate-only by design.

## AgentEnv / verifiers assessment (no code shipped)

| Framework | Verdict | Evidence |
| --- | --- | --- |
| AgentEnv (PyPI `agentenv`) | **needs-deps** | Installs cleanly but is an empty stub: version `0.0.1`, `dir(agentenv)` is `[]`. There is no task/verifier/solver API to map onto and nothing model-free to execute against. Reassess when the package publishes a real surface. |
| verifiers (Prime `verifiers`, v1 stack) | **needs-paid** | Full RL stack installs; episodes run as `Env.run(task, agents)` / `run_episode(task, ctx: ModelContext, ...)` — rollouts require model inference by construction, and there is no oracle/nop control concept. A verdict-equivalence run would need a paid model call, which violates the $0 rule. Mapping Harbor oracle/nop onto it would need a scripted-agent harness (new deps + design), out of scope for this slice. |

## Follow-ups (not in this slice)

1. Karotte fake-model run: the scaffold ships a nop-behavior
   `environment/fake_model.py` (`get_messages` returns `[]`, so a run must
   FAIL like a nop control), but executing it needs the karotte env image
   built around the scaffold (`karotte create-env` + container build). That
   is a container-building follow-up, not validate-only work.
2. Live agreement proof: `run-inspect` and `parity` harbor/inspect cells are
   implemented and print side-by-side verdicts, but the shared laptop daemon
   currently refuses admission, so they report `shared daemon not admitted`.
   Re-run `evallab interop parity library/tasks/transaction-reconciliation
   library/tasks/event-summary` on an admitted daemon for the live matrix.
3. Inspect export execution: the emitted task is `inspect eval`-ready
   (compose sandbox + oracle solver + reward scorer) but has not been
   executed end-to-end here for the same daemon reason; covered structurally
   by exec-against-stub tests.
