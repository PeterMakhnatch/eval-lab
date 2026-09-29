# HAR-85 DSPy arm: dspy.GEPA over the RLM action instructions (STAGED)

Second arm next to the GEPA/Terminus arm (owned by Har85Gepa). This directory
owns the DSPy RLM Harbor agent path: feasibility proof, staged optimizer,
gated launch, budget.

Status: **FEASIBLE at $0, STAGED for paid approval.** No paid call has been
made from this directory; no model weights downloaded.

## What was proven (real execution, 2026-09-28)

1. **Feasibility trial** on the real MiMo terminal task
   `candidate-0036-software-data-engineering` (train split) with
   `scripted_rlm_agent:ScriptedRlmAgent` (DummyLM, fixed 3-action script):
   - startup: MiMo container from the pinned image, `[environment.healthcheck]`
     ran and passed (`trial.log`: `Running healthcheck: bash -c 'test -f
     /var/lib/mimo/ready || ...'` → `Healthcheck passed`; setup ~1 s, ready
     file baked into the image);
   - tool loop: 3 RLM steps with REAL container output
     (`agent/rlm/trajectory.json`: `pwd && ls /app` → `/app` listing,
     `head -30 /app/workflow_probe.py` → real file head, then
     `SUBMIT(solution='har85-feasibility-dummy')`; agent execution 1.4 s);
   - verifier: task pytest ran in-container, `FFFFF`, `reward 0.0`
     (`verifier/reward.txt`), 1.3 s of the 240 s timeout;
   - reporting: `evallab report run <trial-dir>` reads it
     (verdict `failed`, reward `0.0`, agent `rlm 0.1.0+dspy-3.3.1`,
     total 8.2 s). Limit: the report marks `trajectory: absent` because it
     looks for the native ATIF file; the RLM trajectory sidecar lives at
     `agent/rlm/trajectory.json` (with `trajectory.partial.json` per-step).
2. **$0 dry run of the dspy.GEPA loop** (`gepa_mimo.py --dry-run`, 2 train +
   1 val task, real containers + verifier per metric call, DummyLM probe
   agent, scripted proposer): 13 real trials / 19 metric calls / 2 proposals
   in 152.7 s. The scripted proposer FIRED and challengers were evaluated
   on real 3-trial subsamples; ties at 0.0 were correctly rejected
   (`changed=false` — acceptance needs score variance, i.e. a solving
   agent). First dry-run attempt caught a real design flaw (no predictor
   call in the trace ⇒ GEPA never proposes); `forward` now records one
   genuine `generate_action` call per rollout (output unused for scoring).
3. **Agent cwd proven**: a `DryRunRlmAgent` probe trial on
   `candidate-0260-security-appsec` (train) with `--ak working_dir=/app`
   (derived from the task's `[environment].workdir`): the recorded first
   tool output is `/app` (`pwd && ls` with a relative listing), verifier
   reward 0.0 (a grader collection error on this task, not an agent result;
   0260 is excluded from the train pool since, `../train-exclusions.json`).
   Job `har85-wd-probe`, trial
   `candidate-0260-security-appsec__qKyzm6u` (`/private/tmp/har85-wd-probe`,
   since removed). Both staged runners (`gepa_mimo.py`,
   `run-after-approval.sh` phase 2) now derive `--ak working_dir` from each
   task's `task.toml` instead of relying on the `/` default.
4. **Gate refusal exercised**: `run-after-approval.sh` without phase +
   bound approval exits 2; `gepa_mimo.py` paid path without
   `--approval-file`/`--cap-usd` exits 2; tampered binding, wrong approver,
   blank date, and under-cover cap each refuse via `verify_approval`;
   held-out ids in `--train-tasks` refuse; train ids listed in
   `../train-exclusions.json` refuse; direct `--phase heldout`
   execution outside the launcher refuses.
5. **Paid path exercised at $0 up to the first trial** (2026-09-28, #492):
   with a probe approval and `DAYTONA_API_KEY` unset, the paid invocation
   verifies the binding, materializes the coding-plan credential, builds
   both LMs (`paid_lms`: API ids `glm-5.3` / `glm-5.3-flash` on the coding
   endpoint, no call), takes the agent from the Lab registry
   (`HARBOR_AGENT_IMPORT_PATHS["rlm"]`), builds the runner, then refuses at
   the last gate; the secret directory is removed. `HarborTrialRunner.run`
   with `subprocess.run` stubbed shows the Daytona flags and child env. This
   probe found three paid-only defects the DummyLM dry run could not reach,
   all fixed: trace LM built without the required `max_tokens`/`temperature`
   (crash, leaking the secret dir because it ran outside the cleanup guard),
   `agent_import` unset outside `--dry-run`, and raw route selectors passed
   as API model ids.

## Student route verdict (sealed rebind, 2026-09-29): self-hosted BLOCKED

The experiment student is now the self-hosted distill route
(`selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`, Terminus-2, Daytona,
HAR-81 `harness/` tree). The RLM lane **cannot use it without new transport
code**, so the pilot keeps its current coding-plan student, noted as blocked
from the experiment student (cross-arm comparability waits on a lane change
that is out of scope here; paid runs wait for HAR-81's baseline and Peter's
approval regardless):

- `src/evallab/harbor_rlm.py:160-163`: `LabRlmAgent.run` builds its LMs with
  `build_lms(policy, model_id=zai_model_id(...), api_key=key)` and never
  passes `api_base`, so it always dials `ZAI_CODING_API_BASE`
  (`src/evallab/rlm/harness.py:128` default). The self-hosted route needs a
  different base URL (Modal upstream) and a different credential
  (`MIMO_SELFHOSTED_API_KEY` + `EVALLAB_MIMO_SELFHOSTED_UPSTREAM`), neither of
  which the agent reads or forwards.
- `src/evallab/rlm/harness.py:94-98`: `zai_model_id` strips any selector to
  its last path component, so the distill selector would be dialled as
  `openai/MiMo-V2.6-Distill-Qwen-9B` against the Z.ai coding endpoint with the
  coding-plan key -- a silent misroute, not a refusal.
- `src/evallab/execution_contracts.py:1311-1317,1612-1616`: the queue lane
  pins rlm to `ZAI_OPENCODE_MODEL_SELECTORS`; a self-hosted spec is refused
  at `validate_request`/`build_command`. (This arm runs outside the queue, so
  the refusal would not even fire -- the agent would misroute first.)
- `src/evallab/harbor_rlm.py:73-80`: the only key transport is
  `EVALLAB_ZAI_SECRET_FILE` (coding-plan). No self-hosted key transport
  exists for rlm.
- `src/evallab/rlm/harness.py:159-163` (`LmUsage.cost_usd` at $1.40/$4.40 per
  M): the in-harness ceiling accounts Z.ai list prices. On the $0-token
  distill route a normal trial (2.4M+ input tokens) would compute $3+ of
  phantom spend and trip `cost_limit_usd` mid-trial.
- `dspy/gepa_mimo.py:paid_lms` builds the reflection and trace-seed LMs with
  `zai_model_id` + `build_lm` for the coding endpoint; the reflection model
  stays on the coding plan by design.

Prior verdict (2026-09-28, unchanged for the staged path): the RLM lane uses
the Z.ai coding-plan route (`zai-coding-plan/glm-5.3-flash`, subscription
window quota); the metered OpenAPI route needs a lane change the agent never
passes. The pilot tasks are re-bound to sealed-split train tasks (APPROVAL.md).

## Files

| File | Role |
|---|---|
| `scripted_rlm_agent.py` | $0-only feasibility agent (fixed `policy=stock` + DummyLM). Never for paid runs. |
| `dryrun_rlm_agent.py` | $0-only dry-run agent (honours `--ak policy=<file>` + DummyLM). Never for paid runs. |
| `gepa_mimo.py` | Staged optimizer: split loading (train-only assertion), bound approval (`verify_approval`, `--print-binding`, `--verify-only`), derived workdir, real-trial metric, dspy.GEPA, winner policy file. |
| `run-after-approval.sh` | Gated paid launcher (phase 1: GEPA train/val; phase 2: final paired held-out). Refuses without a bound approval. |
| `approvals/` | Committed BLANK templates per phase (binding filled, approver blank for phase 1; binding filled post-phase-1 for phase 2). Signed refs live outside the repo, never committed. |
| `APPROVAL.md` | Exact approval commands + gate design + no-bypass statement. |
| `BUDGET.md` | Budget formula with measured inputs: model API-equivalent (subscription quota) and metered Daytona sandbox time, reported separately. |

The split manifest is HAR-81's sealed split
(`../har81-mimo-sft/split.json`, manifest_digest `sha256:c3df70a5…`), read
through `../sealed_split.py` (terminal rows only) and re-checked on every
load. The provisional manifest is deleted.

## Map: DSPy RLM Harbor agent + launch path

- Agent: `src/evallab/harbor_rlm.py` (`LabRlmAgent`, agent id `rlm`,
  version `0.1.0+dspy-3.3.1` as reported in the trial). Host-side
  `LabRlm(dspy.RLM)` + Harbor `EnvironmentToolBridge` (`exec_command`,
  `read_file`, `write_file`; `ContainerPythonBridge` adds container-side
  `run_python`). Writes `agent/rlm/{policy,trajectory,trajectory.partial,
  usage,solution}.json`.
- Harness: `src/evallab/rlm/harness.py` (`LabRlm`, `build_lms`,
  `run_rlm`), policies `src/evallab/rlm/policies.py` (19 named policies;
  `stock` used here). Optimizer precedent: `src/evallab/rlm/gepa_rlm.py`
  (dspy.GEPA over `generate_action` on the synthetic suite — same target,
  new rollout path).
- Launch: `harbor run --path <task> --agent <import-path> --env <env>
  --model <route> --ak policy=<id|policy-JSON> --ak cost_limit_usd=<n>`.
  Paid runs use `--env evallab.harbor_daytona:BoundedDaytonaEnvironment
  --environment-kwarg ttl_minutes=40` (`gepa_mimo.harbor_env_args`, the Lab
  queue's bounded lifecycle for host-side agents: named sandbox, provider-side
  TTL). The RLM loop stays host-side and drives the sandbox through
  `environment.exec`; `--env docker` is kept for $0 dry runs only.
  Agent import registry: `execution_contracts.AGENT_IMPORTS`
  (`rlm → evallab.harbor_rlm:LabRlmAgent`); profile `rlm-glm-5.3-flash`
  (`profiles.py`); lane validation `validate_request` (rlm binds exactly
  one trial, selector model, billable ack).
- Change in this branch: `--ak policy=` additionally accepts a policy JSON
  file (GEPA candidate), mirroring `bench_runner.load_policy`
  (`resolve_agent_policy`; tests in `tests/test_rlm_harness.py`).
  Catalog behaviour unchanged.

## Model routing (single swappable parameter)

- Student route: `--student-route` trial `--model`, default
  `zai-coding-plan/glm-5.3-flash` (the only route this lane can dial; the
  self-hosted distill route is BLOCKED here -- see the verdict above, not
  staged, no transport built).
- Reflection model: `--reflection-model` (GEPA `reflection_lm`, coding plan).
- This arm's dspy.GEPA runs OUTSIDE the Lab queue (direct `harbor run` per
  metric call). The spend gate is `run-after-approval.sh` + per-trial
  `cost_limit_usd`, not `evallab approve` (no queue spec exists to approve).

## Reproduce ($0 only)

```bash
# lane runtime (untracked): harbor 0.21.0 + daytona 0.210.0 (same as the Lab's
# harbor tool) + dspy 3.3.1 + pytest
uv venv runs/.harbor-dspy --python 3.12
uv pip install --python runs/.harbor-dspy/bin/python \
  "harbor[dspy,daytona]==0.21.0" "daytona==0.210.0" "dspy==3.3.1" "litellm==1.101.0" "numpy==2.5.2" pytest
# focused tests
PYTHONPATH=src runs/.harbor-dspy/bin/python -m pytest tests/test_rlm_harness.py -q -o addopts=""
# feasibility trial (~15 s + one docker image, already local)
D=$PWD; PYTHONPATH=$D/src:$D/research/experiments/har85-mimo-gepa/dspy \
EVALLAB_ZAI_SECRET_FILE=/private/tmp/har85-dummy-secret \
$D/runs/.harbor-dspy/bin/harbor run --path $D/runs/har85-feasibility/tasks/candidate-0036-software-data-engineering \
  --agent scripted_rlm_agent:ScriptedRlmAgent --env docker --model scripted-dummy \
  --ak policy=stock --ak cost_limit_usd=1.0 --job-name har85-dspy-feasibility-0036 \
  --jobs-dir $D/runs/har85-feasibility/jobs --n-attempts 1 --n-concurrent 1 -y
# GEPA dry run (needs staged task copies + split copy under runs/; see recipe in BUDGET.md)
```

## Limits

- Dry run proves mechanics, not proposal quality: with a scripted proposer
  and DummyLM rollouts every candidate scores 0 and `changed=false`. Paid
  reflection quality is the staged hypothesis.
- One feasibility task + three dry-run tasks (+ one cwd probe trial);
  wall-clock numbers are single samples on one host.
- `report run` does not surface the `agent/rlm/` sidecar trajectory.
- Agent cwd is derived from each task's `[environment].workdir` (`/app`
  proven in-trial); tasks without that field fall back to `/`.
