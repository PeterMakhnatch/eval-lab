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
3. **Gate refusal exercised**: `run-after-approval.sh` without approval exits
   2 (recorded output in `APPROVAL.md`); `gepa_mimo.py` without `--dry-run`
   and without `--approval-file` exits 2.

## Files

| File | Role |
|---|---|
| `scripted_rlm_agent.py` | $0-only feasibility agent (fixed `policy=stock` + DummyLM). Never for paid runs. |
| `dryrun_rlm_agent.py` | $0-only dry-run agent (honours `--ak policy=<file>` + DummyLM). Never for paid runs. |
| `gepa_mimo.py` | Staged optimizer: split loading (train-only assertion), examples, real-trial metric, dspy.GEPA, winner policy file. |
| `run-after-approval.sh` | Gated paid launcher (phase 1: GEPA train/val; phase 2: final paired held-out). Refuses without approval. |
| `APPROVAL.md` | Exact approval commands + recorded refusal output. |
| `BUDGET.md` | Budget formula with measured inputs. |

The split manifest lives at `../split.provisional.json` (owned by Har85Gepa;
PROVISIONAL until HAR-81 seals its split). This arm reads it at runtime and
re-checks the manifest digest.

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
- Launch: `harbor run --path <task> --agent <import-path> --env docker
  --model <route> --ak policy=<id|policy-JSON> --ak cost_limit_usd=<n>`.
  Agent import registry: `execution_contracts.AGENT_IMPORTS`
  (`rlm → evallab.harbor_rlm:LabRlmAgent`); profile `rlm-glm-5.3-flash`
  (`profiles.py`); lane validation `validate_request` (rlm binds exactly
  one trial, selector model, billable ack).
- Change in this branch: `--ak policy=` additionally accepts a policy JSON
  file (GEPA candidate), mirroring `bench_runner.load_policy`
  (`resolve_agent_policy`; tests in `tests/test_rlm_harness.py`).
  Catalog behaviour unchanged.

## Model routing (single swappable parameter)

- Student route: `--student-route` trial `--model`. PROVISIONAL default
  `zai-coding-plan/glm-5.3-flash` (existing qualified Terminus-2 route
  family); HAR-81's Qwen-on-Tinker route later by changing one flag.
- Reflection model: `--reflection-model` (GEPA `reflection_lm`).
- This arm's dspy.GEPA runs OUTSIDE the Lab queue (direct `harbor run` per
  metric call). The spend gate is `run-after-approval.sh` + per-trial
  `cost_limit_usd`, not `evallab approve` (no queue spec exists to approve).

## Reproduce ($0 only)

```bash
# lane runtime (untracked): harbor 0.21.0 + dspy 3.3.1 + pytest
uv venv runs/.harbor-dspy --python 3.12
uv pip install --python runs/.harbor-dspy/bin/python \
  "harbor[dspy]==0.21.0" "dspy==3.3.1" "litellm==1.101.0" "numpy==2.5.2" pytest
# focused tests
PYTHONPATH=src runs/.harbor-dspy/bin/python -m pytest tests/test_rlm_harness.py -q -o addopts=""
# feasibility trial (~15 s + one docker image, already local)
printf 'har85-dummy-never-used' >/private/tmp/har85-dummy-secret && chmod 400 /private/tmp/har85-dummy-secret
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
- One feasibility task + three dry-run tasks; wall-clock numbers are
  single samples on one host.
- `report run` does not surface the `agent/rlm/` sidecar trajectory.
- The LabRlm bridge default `working_dir="/"` (scripts here use absolute
  paths); staged runs should pass `--ak working_dir=/app` — recorded as a
  rollout parameter, not yet exercised.
