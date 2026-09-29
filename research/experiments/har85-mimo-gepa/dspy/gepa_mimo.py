"""dspy.GEPA over the RLM action instructions on MiMo-V2.6 terminal tasks.

HAR-85 DSPy arm (second arm next to the GEPA/Terminus arm). STAGED: the paid
path only runs via ``run-after-approval.sh`` with a recorded approval; the
``--dry-run`` path is $0 and proves every mechanical stage.

Pipeline (sealed train split ONLY; held-out appears solely in the final paired eval):
  1. Load HAR-81's sealed split (``../har81-mimo-sft/split.json``, read
     through ``../sealed_split.py`` -- never copied). Trainset/valset ids MUST
     be a subset of the sealed terminal ``train_task_ids``; anything in the
     sealed terminal held-out set is refused, and so is any train id in
     ``../train-exclusions.json`` (graders that cannot score an honest run).
  2. Each example is one real MiMo terminal task (instruction text + dir).
  3. ``dspy.GEPA`` optimises the *complete action instructions* of a
     ``LabRlm``-shaped student (same target as ``evallab.rlm.gepa_rlm``, new
     rollout path for Harbor tasks).
  4. Each metric call is one real ``harbor run`` trial (``--harbor-env``:
     Daytona by default, via the Lab's ``BoundedDaytonaEnvironment``; local
     Docker only for $0 dry runs) with the current candidate policy file
     (``--ak policy=<file>`` served by the shipped ``resolve_agent_policy``),
     scored by the task's own deterministic verifier (reward 0/1).
  5. The winner is written as a file-backed candidate policy JSON loadable by
     both ``bench_runner.load_policy`` and the Harbor agent.

Spend-relevant facts, stated plainly:
  - dspy.GEPA runs OUTSIDE the Lab queue (direct ``harbor run`` per metric
    call). The spend gate is ``run-after-approval.sh`` + per-trial
    ``cost_limit_usd``, NOT ``evallab approve`` (there is no queue spec to
    approve; inventing one would be a bypass).
  - Student route is a single swappable parameter (``--student-route``;
    default ``zai-coding-plan/glm-5.3-flash``; the self-hosted MiMo distill
    route is BLOCKED on this lane without new transport code -- see
    README.md "Student route verdict (sealed rebind)"). Reflection model is
    ``--reflection-model``.

Usage ($0 dry run, 2 train + 1 val task, real local containers + verifier)::

    runs/.harbor-dspy/bin/python research/experiments/har85-mimo-gepa/dspy/gepa_mimo.py \\
        --dry-run --harbor-env docker \\
        --train-tasks candidate-0758-ml-inference,candidate-1990-security-cryptography \\
        --val-tasks candidate-0688-hardware-rtl --max-metric-calls 8 --out runs/har85-dryrun/gepa
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPERIMENT_DIR.parent))

import sealed_split  # noqa: E402 (sibling import after path insert; cf. har81 stage.py)

DEFAULT_SPLIT = sealed_split.SEALED_SPLIT_PATH
#: Train tasks excluded from the pool with a recorded reason (train_pool.py).
TRAIN_EXCLUSIONS = EXPERIMENT_DIR.parent / "train-exclusions.json"

CURRENT_STUDENT_ROUTE = "zai-coding-plan/glm-5.3-flash"
CURRENT_REFLECTION_MODEL = "zai-coding-plan/glm-5.3"

SIGNATURE = "instruction, file_tree -> solution"
VERIFIER_TAIL_LINES = 30
STEP_TEXT_CHARS = 400
MAX_FEEDBACK_STEPS = 6


# --------------------------------------------------------------------------
# Split handling (train only; held-out refused)


def load_split(split_path: Path) -> dict:
    """Sealed split in pool shape (terminal rows only); refuses on drift."""
    manifest = sealed_split.load_sealed_manifest(Path(split_path))
    rows = sealed_split.terminal_rows(manifest)
    return {
        "manifest_digest": manifest["manifest_digest"],
        "tasks": rows,
        "train_task_ids": sealed_split.train_task_ids(rows),
        "heldout_task_ids": sealed_split.heldout_task_ids(rows),
    }


def excluded_task_ids(split: dict, exclusions_path: Path = TRAIN_EXCLUSIONS) -> set[str]:
    """Train ids excluded from the pool; fails closed on a missing or foreign record."""
    try:
        exclusions = json.loads(exclusions_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"train exclusions {exclusions_path} are unreadable: {exc}") from exc
    if exclusions.get("split_manifest_digest") != split.get("manifest_digest"):
        raise ValueError(
            f"{exclusions_path.name} was recorded against another split; re-review it"
        )
    return {entry["task_id"] for entry in exclusions["excluded"]}


def check_train_only(task_ids: list[str], split: dict) -> None:
    train = set(split["train_task_ids"])
    heldout = set(split["heldout_task_ids"])
    unknown = [tid for tid in task_ids if tid not in train and tid not in heldout]
    if unknown:
        raise ValueError(f"task ids not in the sealed split: {unknown}")
    leaked = [tid for tid in task_ids if tid in heldout]
    if leaked:
        raise ValueError(
            f"held-out task ids must never feed the optimizer: {leaked}"
        )
    excluded = sorted(set(task_ids) & excluded_task_ids(split))
    if excluded:
        raise ValueError(
            f"task ids excluded from the train pool (see {TRAIN_EXCLUSIONS.name}): {excluded}"
        )


# --------------------------------------------------------------------------
# Bound authorization (Lab out-of-queue convention: workflow.py _run_campaign)
#
# The approval is a JSON file carrying ``binding_sha256`` plus ``approved_by``
# and ``approved_at``. binding_sha256 is sha256 over canonical sorted-key JSON
# of every spend-relevant launch parameter for that phase. The paid path
# recomputes the binding and refuses on mismatch, on approved_by != "peter",
# or on a missing/unparsable approved_at. Signed refs are runtime state:
# gitignored, never committed (commit the blank templates in approvals/).

APPROVER = "peter"
LAUNCHER_NAME = "run-after-approval.sh"

#: Planning rates: API-list-price equivalents for the coding-plan student
#: route (SUBSCRIPTION window quota, not metered API spend; see BUDGET.md).
TRIAL_EXPECTED_USD = 0.06
TRACE_SEED_USD = 0.007
REFLECTION_CALL_USD = 0.10
METRIC_OVERSHOOT = 2.5

#: Where each trial's task container runs. Daytona is the default (Peter,
#: 2026-09-28: run everything in the cloud); docker remains for $0 dry runs.
HARBOR_ENVS = ("daytona", "docker")
DAYTONA_ENV_KEYS = ("DAYTONA_API_KEY", "DAYTONA_API_URL", "DAYTONA_TARGET")
#: METERED Daytona sandbox $ per trial (not subscription). MiMo terminal tasks
#: request 1 vCPU + 2 GiB: $0.0504/vCPU-h + 2 x $0.0162/GiB-h
#: (daytona.io/pricing, read 2026-09-28) = $0.0834/h; $0.03 covers ~22 min.
DAYTONA_TRIAL_USD = 0.03
#: Per-trial controller timeout; also sizes the Daytona TTL (harbor_env_args).
TRIAL_TIMEOUT_SECONDS = 1800


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def binding_sha256(binding: dict) -> str:
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def phase1_binding(
    *,
    split: dict,
    student_route: str,
    reflection_model: str,
    base_policy: str,
    train_ids: list[str],
    val_ids: list[str],
    max_metric_calls: int,
    cost_limit_usd: float,
    cap_usd: float,
    harbor_env: str,
) -> dict:
    return {
        "phase": "gepa",
        "gepa_mimo_sha256": sha256_file(Path(__file__).resolve()),
        "launcher_sha256": sha256_file(EXPERIMENT_DIR / LAUNCHER_NAME),
        "split_manifest_digest": split.get("manifest_digest"),
        "student_route": student_route,
        "reflection_model": reflection_model,
        "base_policy": base_policy,
        "train_task_ids": sorted(train_ids),
        "val_task_ids": sorted(val_ids),
        "max_metric_calls": max_metric_calls,
        "cost_limit_usd": cost_limit_usd,
        "cap_usd": cap_usd,
        "harbor_env": harbor_env,
    }


def phase2_binding(
    *,
    split: dict,
    student_route: str,
    winner_digest: str,
    attempts: int,
    cost_limit_usd: float,
    cap_usd: float,
    harbor_env: str,
) -> dict:
    return {
        "phase": "heldout",
        "launcher_sha256": sha256_file(EXPERIMENT_DIR / LAUNCHER_NAME),
        "split_manifest_digest": split.get("manifest_digest"),
        "student_route": student_route,
        "winner_policy_digest": winner_digest,
        "heldout_task_ids": sorted(split["heldout_task_ids"]),
        "attempts": attempts,
        "cost_limit_usd": cost_limit_usd,
        "cap_usd": cap_usd,
        "harbor_env": harbor_env,
    }


def expected_cost_parts(binding: dict) -> dict[str, float]:
    """BUDGET.md formula for a binding, split by spend type.

    ``model_api_equiv_usd`` is coding-plan SUBSCRIPTION quota priced at list
    rates; ``daytona_sandbox_usd`` is METERED sandbox spend (0 for docker).
    """
    if binding["phase"] == "gepa":
        trials = binding["max_metric_calls"] * METRIC_OVERSHOOT
        # Reflection calls: ~1 per 10 rollouts (measured 2 proposals / 19
        # metric calls in the $0 dry run; reflection calls >= proposals).
        reflections = max(1, math.ceil(trials / 10))
        model = trials * (TRIAL_EXPECTED_USD + TRACE_SEED_USD) + reflections * REFLECTION_CALL_USD
    else:
        trials = len(binding["heldout_task_ids"]) * 2 * binding["attempts"]
        model = trials * TRIAL_EXPECTED_USD
    sandbox = trials * DAYTONA_TRIAL_USD if binding["harbor_env"] == "daytona" else 0.0
    return {"model_api_equiv_usd": model, "daytona_sandbox_usd": sandbox}


def expected_cost_usd(binding: dict) -> float:
    """Both spend types together: the conservative total the bound cap must cover."""
    return sum(expected_cost_parts(binding).values())


def harbor_env_args(harbor_env: str, trial_timeout_seconds: int = TRIAL_TIMEOUT_SECONDS) -> list[str]:
    """Harbor environment flags for one trial.

    Daytona reuses the Lab queue's bounded lifecycle for host-side agents
    (``execution_contracts.build_command``): a named sandbox with a
    provider-side TTL, so a dead controller cannot leave it billing.
    """
    if harbor_env == "docker":
        return ["--env", "docker"]
    if harbor_env != "daytona":
        raise ValueError(f"harbor_env must be one of {HARBOR_ENVS}, got {harbor_env!r}")
    from evallab.execution_contracts import BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH

    ttl_minutes = (trial_timeout_seconds + 600 + 59) // 60
    return [
        "--env",
        BOUNDED_DAYTONA_ENVIRONMENT_IMPORT_PATH,
        "--environment-kwarg",
        f"ttl_minutes={ttl_minutes}",
    ]


def verify_approval(binding: dict, approval_path: Path) -> dict:
    """Recompute the binding and enforce the authorization. Raises PermissionError."""
    try:
        approval = json.loads(approval_path.read_text())
    except OSError as exc:
        raise PermissionError(f"approval file {approval_path} is unreadable: {exc}") from exc
    except ValueError as exc:
        raise PermissionError(f"approval file {approval_path} is not valid JSON") from exc
    if approval.get("binding_sha256") != binding_sha256(binding):
        raise PermissionError(
            f"approval binding mismatch: signed {approval.get('binding_sha256')} "
            f"!= recomputed {binding_sha256(binding)}; re-derive after ANY param change"
        )
    if approval.get("approved_by") != APPROVER:
        raise PermissionError(
            f"approval must identify the approver ({APPROVER!r}); got {approval.get('approved_by')!r}"
        )
    approved_at = approval.get("approved_at") or ""
    try:
        from datetime import datetime

        datetime.fromisoformat(str(approved_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PermissionError(
            f"approval needs a parsable approved_at; got {approved_at!r}"
        ) from exc
    cap = binding.get("cap_usd")
    expected = expected_cost_usd(binding)
    if cap is None or not isinstance(cap, (int, float)) or expected > float(cap):
        raise PermissionError(
            f"phase cap ${cap} does not cover expected ${expected:.2f}; "
            "raise the cap or shrink the scope and re-derive the binding"
        )
    return approval


def task_workdir(task_dir: Path) -> str:
    """Agent cwd for a staged task, derived from its task.toml.

    MiMo terminal tasks pin the workdir under ``[environment]`` (``/app``);
    ``[agent]`` carries only timeouts. Falls back to ``/`` when absent so
    older task shapes still stage.
    """
    import tomllib

    try:
        config = tomllib.loads((task_dir / "task.toml").read_text())
    except (OSError, ValueError):
        return "/"
    workdir = (config.get("environment") or {}).get("workdir") or "/"
    return str(workdir)



# --------------------------------------------------------------------------
# Trial outcome + runner (one real `harbor run` per metric call)


@dataclass
class TrialOutcome:
    task_id: str
    reward: float
    solution: str
    feedback: str
    trial_dir: str
    policy_digest: str | None
    wall_seconds: float
    infra_issue: str | None = None


def _compact_trajectory(steps: list[dict], limit: int = MAX_FEEDBACK_STEPS) -> str:
    lines = []
    for step in steps[:limit]:
        code = str(step.get("code", ""))[:STEP_TEXT_CHARS]
        output = str(step.get("output", ""))[:STEP_TEXT_CHARS]
        lines.append(f"$ {code}\n=> {output}")
    if len(steps) > limit:
        lines.append(f"... ({len(steps) - limit} more steps)")
    return "\n".join(lines) if lines else "(no steps recorded)"


def _parse_trial(trial_dir: Path) -> TrialOutcome:
    result = json.loads((trial_dir / "result.json").read_text())
    task_name = str(result.get("task_name", ""))
    task_id = task_name.split("/")[-1]
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward_raw = rewards.get("reward")
    reward = float(reward_raw) if isinstance(reward_raw, (int, float)) else 0.0
    meta = (result.get("agent_result") or {}).get("metadata") or {}
    digest = meta.get("rlm_policy_digest")
    rlm_dir = trial_dir / "agent" / "rlm"
    solution = ""
    if (rlm_dir / "solution.txt").is_file():
        solution = (rlm_dir / "solution.txt").read_text()[:2000]
    steps: list[dict] = []
    if (rlm_dir / "trajectory.json").is_file():
        try:
            steps = json.loads((rlm_dir / "trajectory.json").read_text())
        except ValueError:
            steps = []
    verifier_tail = ""
    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    if stdout_path.is_file():
        lines = stdout_path.read_text(errors="replace").splitlines()
        verifier_tail = "\n".join(lines[-VERIFIER_TAIL_LINES:])
    started = result.get("started_at", "")
    finished = result.get("finished_at", "")
    wall = 0.0
    try:
        from datetime import datetime

        wall = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    except (ValueError, TypeError):
        wall = 0.0
    infra = None
    if result.get("exception_info"):
        infra = f"trial exception: {str(result['exception_info'])[:300]}"
    feedback = (
        f"task {task_id}; reward={reward}; policy_digest={digest}; "
        f"trajectory steps={len(steps)}.\n"
        f"Trajectory:\n{_compact_trajectory(steps)}\n"
        f"Verifier tail:\n{verifier_tail[:3000]}"
    )
    if reward < 1.0:
        feedback += (
            "\nThe rollout did not solve the task: the action instructions should "
            "direct exploration (list files, read the probe/tests), diagnose the "
            "defect from evidence, repair the production modules, and run the "
            "offline replay before submitting."
        )
    return TrialOutcome(
        task_id=task_id,
        reward=reward,
        solution=solution,
        feedback=feedback,
        trial_dir=str(trial_dir),
        policy_digest=digest,
        wall_seconds=wall,
        infra_issue=infra,
    )


@dataclass
class RunnerConfig:
    harbor_bin: str
    repo_root: Path
    agent_bind_dir: Path  # dir holding the agent modules (this dspy/ dir)
    tasks_root: Path  # staged byte-identical task copies
    jobs_dir: Path
    agent_import: str
    model: str
    base_policy_id: str
    cost_limit_usd: float
    harbor_env: str
    job_timeout_seconds: int = TRIAL_TIMEOUT_SECONDS


class HarborTrialRunner:
    """One real `harbor run` trial per call; parses reward + feedback."""

    def __init__(self, cfg: RunnerConfig, secret_file: Path, job_tag: str) -> None:
        self.cfg = cfg
        self.secret_file = secret_file
        self.job_tag = job_tag
        self.trials = 0

    def _candidate_file(self, workdir: Path, instructions: str) -> Path:
        from evallab.rlm.policies import resolve_policy

        base = resolve_policy(self.cfg.base_policy_id)
        digest = hashlib.sha256(instructions.encode()).hexdigest()[:12]
        candidate = base.derive(
            f"gepa-mimo-{digest}",
            f"HAR-85 DSPy GEPA candidate over {base.policy_id} (instructions sha {digest})",
            source="har85-mimo-gepa/dspy/gepa_mimo.py; dspy.GEPA over action instructions",
            action_instructions_override=instructions,
        )
        path = workdir / f"candidate-{digest}.json"
        path.write_text(
            json.dumps(
                {
                    "policy": candidate.to_json(),
                    "policy_digest": candidate.digest(),
                    "base_policy": base.policy_id,
                },
                indent=2,
            )
        )
        return path

    def run(self, task_id: str, instructions: str) -> TrialOutcome:
        workdir = self.cfg.jobs_dir / self.job_tag / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        candidate_file = self._candidate_file(workdir, instructions)

        task_dir = self.cfg.tasks_root / task_id
        if not (task_dir / "task.toml").is_file():
            raise ValueError(f"staged task missing for {task_id}: {task_dir}")
        agent_cwd = task_workdir(task_dir)
        job_name = f"{self.job_tag}-{task_id[:32]}-{self.trials:03d}"
        env = {
            "HOME": os.environ.get("HOME", ""),
            "TMPDIR": os.environ.get("TMPDIR", "/private/tmp"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PATH": f"{Path(self.cfg.harbor_bin).parent}:{os.environ.get('PATH', '')}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": f"{self.cfg.repo_root / 'src'}:{self.cfg.agent_bind_dir}",
            "EVALLAB_ZAI_SECRET_FILE": str(self.secret_file),
        }
        if self.cfg.harbor_env == "daytona":
            # Only the Daytona connection settings; never the parent env.
            env.update({key: os.environ[key] for key in DAYTONA_ENV_KEYS if os.environ.get(key)})
        command = [
            self.cfg.harbor_bin,
            "run",
            "--path",
            str(task_dir),
            "--agent",
            self.cfg.agent_import,
            *harbor_env_args(self.cfg.harbor_env, self.cfg.job_timeout_seconds),
            "--model",
            self.cfg.model,
            "--ak",
            f"policy={candidate_file}",
            "--ak",
            f"cost_limit_usd={self.cfg.cost_limit_usd}",
            "--ak",
            f"working_dir={agent_cwd}",
            "--job-name",
            job_name,
            "--jobs-dir",
            str(self.cfg.jobs_dir / self.job_tag),
            "--n-attempts",
            "1",
            "--n-concurrent",
            "1",
            "-y",
        ]
        proc = subprocess.run(
            command,
            cwd=self.cfg.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.cfg.job_timeout_seconds,
        )
        self.trials += 1
        if proc.returncode != 0:
            raise RuntimeError(
                f"harbor run failed for {task_id} (rc={proc.returncode}): "
                f"{proc.stderr[-2000:]}"
            )
        job_dir = self.cfg.jobs_dir / self.job_tag / job_name
        candidates = sorted(
            p for p in job_dir.iterdir() if p.is_dir() and "__" in p.name
        )
        if not candidates:
            raise RuntimeError(f"no trial dir under {job_dir}")
        return _parse_trial(candidates[-1])


# --------------------------------------------------------------------------
# Student program (the dspy.GEPA optimisation target)


def base_action_instructions(base_policy_id: str) -> str:
    from evallab.rlm.harness import LabRlm
    from evallab.rlm.policies import resolve_policy

    probe = LabRlm(SIGNATURE, resolve_policy(base_policy_id), tools=[])
    return str(probe.generate_action.signature.instructions)

def build_student(base_policy_id: str, run_trial, trace_lm) -> object:
    import dspy

    base_instructions = base_action_instructions(base_policy_id)

    class MimoCandidateProgram(dspy.Module):
        """Candidate action instructions + real-trial rollout.

        ``generate_action`` is the predictor dspy.GEPA mutates; ``forward``
        evaluates its CURRENT instructions with one real Harbor trial and
        returns the reward + trajectory text for the metric.

        Adapter-compliance note: the rollout itself happens in a Harbor
        subprocess, so ``forward`` additionally records one genuine
        ``generate_action`` call (output unused for scoring). Without a
        predictor call in the trace, GEPA's ``make_reflective_dataset``
        finds no reflective examples and never proposes ("No valid
        predictions found for any module", observed in the first dry run).
        """

        def __init__(self) -> None:
            super().__init__()
            self.generate_action = dspy.Predict("instruction -> solution")
            self.generate_action.signature = (
                self.generate_action.signature.with_instructions(base_instructions)
            )

        def current_instructions(self) -> str:
            return str(self.generate_action.signature.instructions)

        def forward(self, instruction: str, task_id: str):  # type: ignore[override]
            outcome: TrialOutcome = run_trial(task_id, self.current_instructions())
            with dspy.context(lm=trace_lm):
                seed = self.generate_action(instruction=instruction[:4000])
            return dspy.Prediction(
                solution=outcome.solution,
                reward=outcome.reward,
                feedback=outcome.feedback,
                trial_dir=outcome.trial_dir,
                policy_digest=outcome.policy_digest or "",
                trace_seed_solution=str(getattr(seed, "solution", "")),
            )

    return MimoCandidateProgram()


def make_metric(counts: dict) -> object:
    def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
        counts["metric_calls"] = counts.get("metric_calls", 0) + 1
        reward = float(getattr(pred, "reward", 0.0) or 0.0)
        feedback = str(getattr(pred, "feedback", "") or "")
        import dspy

        return dspy.Prediction(score=reward, feedback=feedback)

    return metric


def scripted_proposer(*, candidate, reflective_dataset, components_to_update):
    """$0 deterministic proposer for dry runs (no reflection LM)."""
    result = {}
    for name in components_to_update:
        n = len(reflective_dataset.get(name, []))
        result[name] = (
            candidate[name]
            + f"\n\n[har85-dryrun proposal {scripted_proposer.calls}: "
            f"reflected over {n} scored example(s); no semantic change]"
        )
    scripted_proposer.calls += 1
    return result


scripted_proposer.calls = 0


def build_examples(task_ids: list[str], tasks_root: Path) -> list:
    import dspy

    examples = []
    for task_id in task_ids:
        instruction_file = tasks_root / task_id / "instruction.md"
        if not instruction_file.is_file():
            raise ValueError(f"staged task {task_id} lacks instruction.md")
        examples.append(
            dspy.Example(
                instruction=instruction_file.read_text(),
                task_id=task_id,
            ).with_inputs("instruction", "task_id")
        )
    return examples


def write_dummy_secret(path: Path) -> Path:
    path.write_text("har85-dummy-never-used\n")
    os.chmod(path, 0o400)
    return path


def paid_lms(args, secret_file: Path) -> tuple[object, object]:
    """Reflection LM and trace-seed LM for the paid path (coding-plan route).

    Construction makes no model call. Route selectors map to API ids exactly as
    the shipped agent does (``zai_model_id``, ``harbor_rlm.LabRlmAgent.run``).
    The trace seed is one extra student-route call per rollout (output unused
    for scoring; costed in BUDGET.md) with the base policy's root token budget
    and temperature, thinking off.
    """
    from evallab.rlm.harness import build_lm, zai_model_id
    from evallab.rlm.policies import resolve_policy

    api_key = secret_file.read_text().strip()
    reflection_lm = build_lm(
        model_id=zai_model_id(args.reflection_model),
        api_key=api_key,
        max_tokens=16_000,
        thinking=True,
        temperature=1.0,
    )
    base = resolve_policy(args.base_policy)
    trace_lm = build_lm(
        model_id=zai_model_id(args.student_route),
        api_key=api_key,
        max_tokens=base.root_max_tokens,
        thinking=False,
        temperature=base.temperature,
    )
    return reflection_lm, trace_lm


def _refuse(message: str) -> int:
    print(f"REFUSING: {message}", file=sys.stderr)
    return 2


def _binding_for_args(args, split: dict, train_ids: list[str], val_ids: list[str]) -> dict:
    if args.phase == "heldout":
        if args.winner is None or not args.winner.is_file():
            raise ValueError("--winner <phase-1 policy json> is required for phase heldout")
        winner_payload = json.loads(args.winner.read_text())
        return phase2_binding(
            split=split,
            student_route=args.student_route,
            winner_digest=winner_payload.get("policy_digest", ""),
            attempts=args.attempts,
            cost_limit_usd=args.cost_limit_usd,
            cap_usd=args.cap_usd,
            harbor_env=args.harbor_env,
        )
    return phase1_binding(
        split=split,
        student_route=args.student_route,
        reflection_model=args.reflection_model,
        base_policy=args.base_policy,
        train_ids=train_ids,
        val_ids=val_ids,
        max_metric_calls=args.max_metric_calls,
        cost_limit_usd=args.cost_limit_usd,
        cap_usd=args.cap_usd,
        harbor_env=args.harbor_env,
    )


def _run_binding_modes(args, split: dict, train_ids: list[str], val_ids: list[str]) -> int | None:
    """Handle --print-binding / phase-heldout routing. Returns exit code or None to proceed."""
    if args.print_binding:
        binding = _binding_for_args(args, split, train_ids, val_ids)
        print(json.dumps(binding, indent=1, sort_keys=True))
        print(f"binding_sha256: {binding_sha256(binding)}")
        for part, usd in expected_cost_parts(binding).items():
            print(f"expected_{part}: {usd:.2f}")
        print(f"expected_cost_usd: {expected_cost_usd(binding):.2f}")
        return 0
    if args.phase == "heldout" and not args.verify_only:
        return _refuse(
            "phase heldout executes via run-after-approval.sh "
            "(gepa_mimo.py only prints/verifies its binding)"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--split", type=Path, default=DEFAULT_SPLIT)
    parser.add_argument("--tasks-root", type=Path, required=True,
                        help="dir of staged byte-identical task copies")
    parser.add_argument("--repo-root", type=Path, required=True,
                        help="worktree root (builds PYTHONPATH=<root>/src)")
    parser.add_argument("--harbor-bin", default="harbor")
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--job-tag", default="har85-gepa-mimo")
    parser.add_argument("--base-policy", default="stock")
    parser.add_argument("--train-tasks", default="",
                        help="comma-separated task ids, MUST be ⊆ train_task_ids (phase gepa)")
    parser.add_argument("--val-tasks", default="",
                        help="comma-separated task ids, MUST be ⊆ train_task_ids")
    parser.add_argument("--student-route", default=CURRENT_STUDENT_ROUTE,
                        help="single swappable student route parameter (lane default; self-hosted BLOCKED, see README)")
    parser.add_argument("--reflection-model", default=CURRENT_REFLECTION_MODEL)
    parser.add_argument("--max-metric-calls", type=int, default=36)
    parser.add_argument("--reflection-minibatch-size", type=int, default=3)
    parser.add_argument("--cost-limit-usd", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="$0: DummyLM probe agent + scripted proposer, real containers")
    parser.add_argument("--approval-file", type=Path, default=None,
                        help="required without --dry-run: bound approval JSON (see dspy/APPROVAL.md)")
    parser.add_argument("--cap-usd", type=float, default=None,
                        help="phase $ cap; bound into the approval (required without --dry-run)")
    parser.add_argument("--phase", default="gepa", choices=["gepa", "heldout"],
                        help="phase this invocation serves (binding + templates)")
    parser.add_argument("--print-binding", action="store_true",
                        help="print the canonical binding + sha256 for these params and exit 0")
    parser.add_argument("--verify-only", action="store_true",
                        help="verify the approval against the recomputed binding and exit 0 (pre-spend point)")
    parser.add_argument("--winner", type=Path, default=None,
                        help="phase heldout: winner policy JSON from phase 1")
    parser.add_argument("--attempts", type=int, default=3,
                        help="phase heldout: attempts per task per arm")
    parser.add_argument("--harbor-env", default="daytona", choices=HARBOR_ENVS,
                        help="where task containers run (bound into the approval); "
                             "docker only for $0 dry runs")

    args = parser.parse_args(argv)
    split = load_split(args.split)
    if args.phase == "heldout":
        # Final eval runs the scorable held-out set: sealed held-out minus
        # held-out exclusions (projected split keeps the full 16).
        _, scorable = sealed_split.heldout_ids()
        split = dict(split, heldout_task_ids=scorable)
        train_ids, val_ids = [], []
    else:
        train_ids = [t for t in args.train_tasks.split(",") if t]
        val_ids = [t for t in args.val_tasks.split(",") if t]
        if not train_ids and not (args.print_binding or args.verify_only or args.dry_run):
            return _refuse("at least one --train-tasks id is required")
        try:
            check_train_only(train_ids + val_ids, split)
        except ValueError as exc:
            return _refuse(str(exc))

    mode = _run_binding_modes(args, split, train_ids, val_ids)
    if mode is not None:
        return mode

    import dspy

    if args.dry_run:
        from dspy.utils import DummyLM

        agent_import = "dryrun_rlm_agent:DryRunRlmAgent"
        secret_holder = tempfile.mkdtemp(prefix="har85-dryrun-secret-")
        secret_file = write_dummy_secret(Path(secret_holder) / "secret")
        instruction_proposer = scripted_proposer
        reflection_lm = None
        # One trace-seed call per rollout; 256 spares cover any staged budget.
        trace_lm = DummyLM([{"solution": "har85-dryrun-trace-seed"}] * 256)
    else:
        if args.phase != "gepa":
            return _refuse("only phase gepa runs the optimizer here")
        if args.approval_file is None:
            return _refuse(
                "the paid path requires --approval-file with the bound approval JSON "
                "(see dspy/APPROVAL.md). Use --dry-run for the $0 path."
            )
        if args.cap_usd is None:
            return _refuse("the paid path requires --cap-usd (bound into the approval)")
        binding = _binding_for_args(args, split, train_ids, val_ids)
        try:
            approval = verify_approval(binding, args.approval_file)
        except PermissionError as exc:
            return _refuse(str(exc))
        if args.verify_only:
            print(
                f"approval ok: binding {binding_sha256(binding)} "
                f"approved by {approval['approved_by']} at {approval['approved_at']} "
                f"(pre-spend point; no trial launched)"
            )
            return 0
        from evallab.execution_contracts import (
            HARBOR_AGENT_IMPORT_PATHS,
            RLM_AGENT,
            materialize_zai_secret_file,
        )

        agent_import = HARBOR_AGENT_IMPORT_PATHS[RLM_AGENT]
        instruction_proposer = None
        secret_holder = tempfile.mkdtemp(prefix="har85-paid-secret-")
        try:
            secret_file = materialize_zai_secret_file(Path(secret_holder) / "secret")
        except (OSError, ValueError) as exc:
            shutil.rmtree(secret_holder, ignore_errors=True)
            return _refuse(f"coding-plan credential unavailable: {exc}")
        # Built under the try/finally below so a failure never leaks the secret.
        reflection_lm = trace_lm = None
    try:
        if not args.dry_run:
            reflection_lm, trace_lm = paid_lms(args, secret_file)
        runner = HarborTrialRunner(
            RunnerConfig(
                harbor_bin=args.harbor_bin,
                repo_root=args.repo_root,
                agent_bind_dir=EXPERIMENT_DIR,
                tasks_root=args.tasks_root,
                jobs_dir=args.jobs_dir,
                agent_import=agent_import,
                model=args.student_route,
                base_policy_id=args.base_policy,
                cost_limit_usd=args.cost_limit_usd,
                harbor_env=args.harbor_env,
            ),
            secret_file=secret_file,
            job_tag=args.job_tag,
        )
        # Last refusal before the first trial (everything above is inert).
        if args.harbor_env == "daytona" and not os.environ.get("DAYTONA_API_KEY"):
            return _refuse("--harbor-env daytona needs DAYTONA_API_KEY in the environment")
        original_instructions = base_action_instructions(args.base_policy)
        student = build_student(args.base_policy, runner.run, trace_lm)
        trainset = build_examples(train_ids, args.tasks_root)
        valset = build_examples(val_ids, args.tasks_root) if val_ids else None
        counts: dict = {}
        optimizer_kwargs: dict = {
            "metric": make_metric(counts),
            "max_metric_calls": args.max_metric_calls,
            "reflection_minibatch_size": args.reflection_minibatch_size,
            "num_threads": 1,
            "track_stats": True,
            "log_dir": str(args.out / "gepa-logs"),
            "add_format_failure_as_feedback": True,
            "use_merge": False,
            "seed": args.seed,
        }
        if instruction_proposer is not None:
            optimizer_kwargs["instruction_proposer"] = instruction_proposer
        else:
            optimizer_kwargs["reflection_lm"] = reflection_lm
        optimizer = dspy.GEPA(**optimizer_kwargs)
        args.out.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        optimized = optimizer.compile(
            student, trainset=trainset, valset=valset or trainset
        )
        elapsed = time.monotonic() - started
        new_instructions = str(optimized.generate_action.signature.instructions)
        changed = new_instructions != original_instructions
        from evallab.rlm.policies import resolve_policy

        base = resolve_policy(args.base_policy)
        candidate = base.derive(
            f"gepa-mimo-{hashlib.sha256(new_instructions.encode()).hexdigest()[:12]}",
            f"HAR-85 DSPy GEPA winner over {base.policy_id} "
            f"({args.max_metric_calls} metric calls, train {train_ids}, val {val_ids})",
            source="har85-mimo-gepa/dspy/gepa_mimo.py; dspy.GEPA over action instructions",
            action_instructions_override=new_instructions if changed else None,
        )
        record = {
            "policy": candidate.to_json(),
            "policy_digest": candidate.digest(),
            "changed": changed,
            "dry_run": args.dry_run,
            "split_manifest": split.get("manifest_digest"),
            "split_salt": sealed_split.SEALED_SALT,
            "train_task_ids": train_ids,
            "val_task_ids": val_ids,
            "student_route": args.student_route,
            "reflection_model": None if args.dry_run else args.reflection_model,
            "max_metric_calls": args.max_metric_calls,
            "metric_calls_made": counts.get("metric_calls", 0),
            "proposer_calls_made": scripted_proposer.calls if args.dry_run else None,
            "harbor_trials_run": runner.trials,
            "elapsed_seconds": round(elapsed, 1),
            "approval_file": None if args.dry_run else str(args.approval_file),
            "binding_sha256": None if args.dry_run else binding_sha256(binding),
            "approved_by": None if args.dry_run else approval["approved_by"],
            "approved_at": None if args.dry_run else approval["approved_at"],
            "cap_usd": None if args.dry_run else args.cap_usd,
            "original_instructions": original_instructions,
            "optimized_instructions": new_instructions,
        }
        out_path = args.out / f"{candidate.policy_id}.json"
        out_path.write_text(json.dumps(record, indent=2))
        summary = {k: v for k, v in record.items()
                   if k not in ("original_instructions", "optimized_instructions", "policy")}
        print(json.dumps(summary, indent=1))
        print(f"policy json: {out_path}")
        return 0
    finally:
        shutil.rmtree(secret_holder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
