# HAR-169 separate-verifier prototype: facts

Scope: 3 MiMo tasks under Harbor 0.24.0 (isolated wheel
`/tmp/harbor024/harbor-0.24.0-py3-none-any.whl`, `--env docker`, $0, no
Daytona, no queue, no model spend). Transform: `separate-verifier@1`
(`src/evallab/separate_verifier.py`, via `evallab.task_variants.derive_task`).

## Variants

| task | parent (ledger run_digest) | parent digest OK | variant digest | record |
|---|---|---|---|---|
| format-code-task-001269 | variant `541d416818c7` (leak-closed) | OK | `sha256:e0633f90aead5f0f17734140d18ec2375c226f960d6692a9b38ad61ef3ffb2d5` | `library/task-variants/mimo-v2.6-rl__format-code-task-001269/e0633f90aead.json` |
| format-code-task-002391 | hf original `d0a399b8633c` | OK | `sha256:5cca1570210fefd308e7a771cc9b084b9f3f3d90c94a1e820894b1e41785fada` | `library/task-variants/mimo-v2.6-rl__format-code-task-002391/5cca1570210f.json` |
| format-code-task-000905 | hf original `bf0e19c83a7e` | OK | `sha256:da570bba75dd28503882bdd72a1a3826eb1779ad203d5038a579960bcfc50633` | `library/task-variants/mimo-v2.6-rl__format-code-task-000905/da570bba75dd.json` |

Packages: `derived/task-store/variants/<slug>/<digest12>/` (shared store,
gitignored). Layouts: 001269 + 000905 `/testbed`, 002391 `/workspace/repo`.
Harbor 0.24 resolves all three variants to `environment_mode=separate`,
`bundled_tests=True`, `check_task_regradable=None`.

## Matrix (jobs under `runs/har169-sepver/`; wall = trial started->finished)

| task | mode | agent | reward | tests visible in agent env? | evidence (trial dir) | wall |
|---|---|---|---|---|---|---|
| 000905 | original (shared) | nop | 0 | no: `/tests` absent during agent run (no probe hook in original; oracle probe below covers the env) | `runs/har169-sepver/sepver-calib-000905-orig-nop/format-code-task-000905__p8TGuMH` | 29s |
| 000905 | original (shared) | oracle | 1 | no: `agent/oracle.txt` lines 1-5 (`ls /tests` fails, marker grep rc=1) | `runs/har169-sepver/sepver-000905-orig-oracle/format-code-task-000905__PhS2Tnf` | 34s |
| 000905 | separate | nop | 0 | no: `artifacts/var/tmp/mimo-separate/probe-agent-env.txt` (`/tests` absent, marker grep rc=1); verifier `verifier/separate-probe.txt` lists `/tests/test.patch` | `runs/har169-sepver/sepver-000905-sep-nop/da570bba75dd__CMGxz2n` | 14s |
| 000905 | separate | oracle | 1 | no: `agent/oracle.txt` probe as above | `runs/har169-sepver/sepver-000905-sep-oracle/da570bba75dd__Yj8vC2C` | 15s |
| 001269 | original (shared) | nop | 0 | n/a (nop writes no logs) | `runs/har169-sepver/sepver-001269-orig-nop/format-code-task-001269__jGyGFbC` | 28s |
| 001269 | original (shared) | oracle | 1 | no: `agent/oracle.txt` probe | `runs/har169-sepver/sepver-001269-orig-oracle/format-code-task-001269__4gaRBcb` | 35s |
| 001269 | separate | nop | 0 | no: `probe-agent-env.txt` as above | `runs/har169-sepver/sepver-001269-sep-nop/e0633f90aead__B2RGGQ6` | 14s |
| 001269 | separate | oracle | 1 | no: `agent/oracle.txt` lines 1-5 | `runs/har169-sepver/sepver-001269-sep-oracle/e0633f90aead__oAFBpgU` | 14s |
| 002391 | original (shared) | nop | 0 | n/a | `runs/har169-sepver/sepver-002391-orig-nop/format-code-task-002391__DiskeCM` | 30s |
| 002391 | original (shared) | oracle | 1 | no: `agent/oracle.txt` probe | `runs/har169-sepver/sepver-002391-orig-oracle2/format-code-task-002391__bujkNgC` | 27s |
| 002391 | separate | nop | 0 | no: `probe-agent-env.txt` as above | `runs/har169-sepver/sepver-002391-sep-nop/5cca1570210f__ovQTm8V` | 14s |
| 002391 | separate | oracle | 1 | no: `agent/oracle.txt` probe; verifier `separate-probe.txt` lists `/tests/test.patch` | `runs/har169-sepver/sepver-002391-sep-oracle/5cca1570210f__LKiHY2q` | 15s |

Grading identical under oracle and nop in both modes: oracle 1, nop 0 for
all 3 tasks in both modes. Trial `result.json` carries
`verifier_environment_mode: shared|separate`. Trial `lock.json` pins the
verifier image built from `tests/`.

Note: `sepver-002391-orig-oracle` (reward 0, 57s) ran with a first-cut
oracle fix whose alias-merge rule was wrong (merged ids instead of aliases
only); superseded by `sepver-002391-orig-oracle2` after the fix. The faulty
trial dir is retained.

## Regrade (`harbor trials regrade ... --env docker`, $0, no agent)

| source trial | task package | reward before -> after | wall |
|---|---|---|---|
| `runs/har169-sepver/sepver-001269-traj-oracle/har169-sepver-traj__xqjrUCv` (oracle + dirty-trajectory solution on an ephemeral copy of `e0633f90aead`) | variant `e0633f90aead` | 1 -> 1 | 8s |

Regraded trial: `research/experiments/har169-separate-verifier/regrade-trials/sepver-regrade-001269/`.
`result.json:config.source_trial` records the source trial path. No model,
no agent container, no charge. Verifier image rebuild was cached.

Coverage-gate fact: regrading a trajectory-less source trial (e.g. the plain
`sepver-001269-sep-oracle` oracle run, manifest
`('/logs/agent/trajectory.json', 'failed')`) with the current
trajectory-declaring package is refused before execution:
`RegradeError: ... /logs/agent/trajectory.json: collection failed in the
source trial; the record does not contain it`. Trials that predate an
artifact declaration cannot be regraded with tasks that declare it.

## Trial environment config (per trial)

- `<trial>/config.json`: trial inputs (agent, artifacts incl.
  `/var/tmp/mimo-separate` in separate mode).
- `<trial>/lock.json`: resolved inputs incl. `verifier.environment_mode`
  and the content-addressed verifier image.
- `<trial>/artifacts/manifest.json`: collection record; separate trials
  carry `var/tmp/mimo-separate/{base,workspace.tgz,git-hidden.tgz,probe-agent-env.txt}`.
- `<trial>/verifier/`: `reward.txt`, `test_output.log`, `apply.log`,
  `agent.diff`, `separate-probe.txt` (separate mode only).
- Oracle trials: `<trial>/agent/oracle.txt` (agent-env probe + fix log).

## Commands

- Derive: `uv run python research/experiments/har169-separate-verifier/run_matrix.py derive`
- Stage baselines: `... run_matrix.py stage` (ephemeral `/tmp` copies, no lineage)
- Run: `uvx --from /tmp/harbor024/harbor-0.24.0-py3-none-any.whl harbor run --env docker -a oracle|nop -p <pkg> --jobs-dir runs/har169-sepver --job-name <name> -n 1 -y -q`
  (wrapped by `run_matrix.py run --task-pkg ... --agent ... --job-name ... --jobs-dir ...`)
- Regrade: `uvx --from /tmp/harbor024/harbor-0.24.0-py3-none-any.whl harbor trials regrade <source-trial-dir> -p <variant-pkg> --env docker --trial-name <name> -o research/experiments/har169-separate-verifier/regrade-trials`
- Collect: `... run_matrix.py collect --jobs-dir ... --out ...`
- Checks: `uv run ruff check src/evallab/separate_verifier.py tests/test_separate_verifier.py research/experiments/har169-separate-verifier/run_matrix.py`
  + `uv run pytest tests/test_separate_verifier.py -q`

## Separate-verifier mode references (Harbor 0.24 source)

- `src/harbor/models/task/config.py:555-611`: `VerifierEnvironmentMode`,
  `[verifier].environment_mode` / `[verifier.environment]`, shared+environment
  rejected.
- `src/harbor/models/task/verifier_mode.py:14-25,47-68,88-130`: mode
  resolution; verifier env falls back to a copy of task environment;
  `tests/Dockerfile` (or compose) presence => `bundled_tests=True` with
  `tests/` as the build directory.
- `src/harbor/verifier/verifier.py:124-131,175-187`: `skip_tests_upload`
  when bundled; otherwise upload `tests/` into the agent environment.
- `src/harbor/trial/trial.py:898-966` (`_run_separate_verifier`),
  `969-997` (verifier env built from `definition.directory` with
  `enable_environment_dir_upload=False` when bundled),
  `1428-1474` (collect hooks), `1475+` (artifact collection).
- `src/harbor/trial/regrade.py:97-122` (`check_task_regradable`),
  `310+` (`RegradeTrial`: NopAgent, no agent env, artifact-coverage gate
  `430-452`), `src/harbor/trial/artifact_handler.py` (flat `artifacts/`
  base, manifest, no-translation re-materialization).
- `src/harbor/models/task/artifacts.py`: convention entry
  (`with_convention_entry`), `src/harbor/models/trial/paths.py`
  (`EnvironmentPaths`: `/tests`, `/solution`, `/logs/{agent,verifier,artifacts}`).
- `src/harbor/agents/oracle.py:57-156`, `src/harbor/agents/nop.py`,
  `src/harbor/cli/trials.py:994+` (`trials regrade`).

## Blockers / bugs hit

- Harbor 0.24 bug (prototype-side, worked around in-transform): none in
  Harbor itself for this path. Two transform bugs found by the first
  separate trial and fixed in `separate_verifier.py`: (1) `tests/test-orig.sh`
  lost its exec bit via `COPY . /tests` (`Permission denied`; fixed with
  `RUN chmod +x` in `tests/Dockerfile`); (2) verifier marker probe used
  non-recursive `grep` on `/tests` (fixed with `-rl`). Stale variants from
  before the fix were deleted and re-derived; digests above are final.

## What HAR-171's $0 regrade needs from this

- Task in separate mode (`environment_mode=separate` + `tests/Dockerfile`
  so `bundled_tests=True`), or `check_task_regradable` refuses.
- Declared artifact covering every verifier input: regrade's
  artifact-coverage gate (`_validate_artifact_coverage`) requires each
  declared artifact (plus the implicit `/logs/artifacts` convention entry)
  to have a `collected` manifest entry with bytes present; entries recorded
  `failed`/`skipped` are incompatible. Do not declare paths nested under
  `/logs/artifacts` (collision with the convention entry => `skipped`).
- Same `[task].name` in the regrade task package as the source trial
  (`Task name mismatch` otherwise).
- Source trial must keep `result.json` + `artifacts/manifest.json` + artifact
  bytes; the job dir is the unit to retain.
- Regrade replays verifier-only: agent cost $0, wall 26-70s per task here
  (verifier image build cached; cold build adds ~1-2 min under emulation).

## Composition: rewardkit-integrity@1 inside separate-verifier@1 (001269)

Order: integrity FIRST on the ledger parent, separate SECOND on the integrity
variant. Reverse order strands the scoring tail after the wrapper's `exec`
line. Chain: parent variant `541d416818c7` -> integrity
`sha256:eecb8d4bbd954d9d42d21ebd384b6d162391992a531fb56de7607112429be07e`
(record `eecb8d4bbd95`) -> separate `66feb63b1db7` (dirty-trajectory solution)
and `ea7d55d3c6d4` (clean-trajectory solution).
`separate-verifier@1` now also declares `/logs/agent/trajectory.json`, which
re-materializes at the integrity runner's default in-image path, so no
integrity-runner change was needed. Missing file (nop) records manifest
`failed` and the trial still grades (best-effort,
`artifact_handler.py:287-341`).

| run | reward.json | evidence | wall |
|---|---|---|---|
| composed-dirty + oracle | `{"reward": 1.0, "integrity": 0, "reward_gated": 0.0}` (only `copy_check_v1` fired, matched_lines=6) | `runs/har169-sepver/sepver-comp-dirty-oracle/66feb63b1db7__nGG8jfx` (`verifier/reward.json`, `verifier/reward-details.json`) | 17s |
| composed-clean + oracle | `{"reward": 1.0, "integrity": 1, "reward_gated": 1.0}` | `runs/har169-sepver/sepver-comp-clean-oracle/ea7d55d3c6d4__bwWFi6a` | 14s |
| composed-dirty + nop (no trajectory) | `{"reward": 0.0, "integrity": 1, "reward_gated": 0.0}`; manifest has `('/logs/agent/trajectory.json', 'failed')` | `runs/har169-sepver/sepver-comp-dirty-nop/66feb63b1db7__TbCfWuH` | 14s |
| regrade of dirty-oracle with `66feb63b1db7` | `{"reward": 1.0, "integrity": 0, "reward_gated": 0.0}` byte-identical to source | `research/experiments/har169-separate-verifier/regrade-trials/sepver-regrade-comp-dirty` | 8s |

Oracle solutions: `solutions/format-code-task-001269-{dirty,clean}.solve.sh`
(responses fix + synthetic ATIF trajectory into `/logs/agent/trajectory.json`;
dirty reuses the `ADDED` fixture lines from
`tests/test_integrity_reward.py` and writes them to `/testbed/synth_mod.py`
so `agent.diff` carries the matching lines).
