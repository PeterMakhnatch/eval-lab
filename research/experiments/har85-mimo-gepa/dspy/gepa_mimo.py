"""dspy.GEPA over the RLM action instructions on MiMo-V2.6 terminal tasks.

HAR-85 DSPy arm (second arm next to the GEPA/Terminus arm). STAGED: the paid
path only runs via ``run-after-approval.sh`` with a recorded approval; the
``--dry-run`` path is $0 and proves every mechanical stage.

Pipeline (train split ONLY; held-out appears solely in the final paired eval):
  1. Load ``../split.provisional.json`` (PROVISIONAL until HAR-81 seals its
     split). Trainset/valset ids MUST be a subset of ``train_task_ids``;
     anything in ``heldout_task_ids`` is refused.
  2. Each example is one real MiMo terminal task (instruction text + dir).
  3. ``dspy.GEPA`` optimises the *complete action instructions* of a
     ``LabRlm``-shaped student (same target as ``evallab.rlm.gepa_rlm``, new
     rollout path for Harbor tasks).
  4. Each metric call is one real ``harbor run`` trial in local Docker with
     the current candidate policy file (``--ak policy=<file>`` served by the
     shipped ``resolve_agent_policy``), scored by the task's own deterministic
     verifier (reward 0/1).
  5. The winner is written as a file-backed candidate policy JSON loadable by
     both ``bench_runner.load_policy`` and the Harbor agent.

Spend-relevant facts, stated plainly:
  - dspy.GEPA runs OUTSIDE the Lab queue (direct ``harbor run`` per metric
    call). The spend gate is ``run-after-approval.sh`` + per-trial
    ``cost_limit_usd``, NOT ``evallab approve`` (there is no queue spec to
    approve; inventing one would be a bypass).
  - Student route is a single swappable parameter (``--student-route``;
    PROVISIONAL default ``zai-coding-plan/glm-5.3-flash``; HAR-81's
    Qwen-on-Tinker route later). Reflection model is ``--reflection-model``.

Usage ($0 dry run, 2 train + 1 val task, real containers + verifier)::

    runs/.harbor-dspy/bin/python research/experiments/har85-mimo-gepa/dspy/gepa_mimo.py \\
        --dry-run --train-tasks candidate-0260-security-appsec,candidate-0390-security-appsec \\
        --val-tasks candidate-0688-hardware-rtl --max-metric-calls 8 --out runs/har85-dryrun/gepa
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
DEFAULT_SPLIT = EXPERIMENT_DIR.parent / "split.provisional.json"

PROVISIONAL_STUDENT_ROUTE = "zai-coding-plan/glm-5.3-flash"
PROVISIONAL_REFLECTION_MODEL = "zai-coding-plan/glm-5.3"

SIGNATURE = "instruction, file_tree -> solution"
VERIFIER_TAIL_LINES = 30
STEP_TEXT_CHARS = 400
MAX_FEEDBACK_STEPS = 6


# --------------------------------------------------------------------------
# Split handling (train only; held-out refused)


def load_split(split_path: Path) -> dict:
    try:
        payload = json.loads(split_path.read_text())
    except OSError as exc:
        raise ValueError(f"split manifest {split_path} is unreadable: {exc}") from exc
    if payload.get("contract") != "har85.provisional_split/v1":
        raise ValueError(f"split manifest {split_path} has unexpected contract")
    return payload


def check_train_only(task_ids: list[str], split: dict) -> None:
    train = set(split["train_task_ids"])
    heldout = set(split["heldout_task_ids"])
    unknown = [tid for tid in task_ids if tid not in train and tid not in heldout]
    if unknown:
        raise ValueError(f"task ids not in the provisional split: {unknown}")
    leaked = [tid for tid in task_ids if tid in heldout]
    if leaked:
        raise ValueError(
            f"REFUSING: held-out task ids must never feed the optimizer: {leaked}"
        )


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
    job_timeout_seconds: int = 1800


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
        command = [
            self.cfg.harbor_bin,
            "run",
            "--path",
            str(task_dir),
            "--agent",
            self.cfg.agent_import,
            "--env",
            "docker",
            "--model",
            self.cfg.model,
            "--ak",
            f"policy={candidate_file}",
            "--ak",
            f"cost_limit_usd={self.cfg.cost_limit_usd}",
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
    parser.add_argument("--train-tasks", required=True,
                        help="comma-separated task ids, MUST be ⊆ train_task_ids")
    parser.add_argument("--val-tasks", default="",
                        help="comma-separated task ids, MUST be ⊆ train_task_ids")
    parser.add_argument("--student-route", default=PROVISIONAL_STUDENT_ROUTE,
                        help="single swappable student route parameter (PROVISIONAL default)")
    parser.add_argument("--reflection-model", default=PROVISIONAL_REFLECTION_MODEL)
    parser.add_argument("--max-metric-calls", type=int, default=36)
    parser.add_argument("--reflection-minibatch-size", type=int, default=3)
    parser.add_argument("--cost-limit-usd", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="$0: DummyLM probe agent + scripted proposer, real containers")
    parser.add_argument("--approval-file", type=Path, default=None,
                        help="required without --dry-run: recorded approval token")
    args = parser.parse_args(argv)

    split = load_split(args.split)
    train_ids = [t for t in args.train_tasks.split(",") if t]
    val_ids = [t for t in args.val_tasks.split(",") if t]
    if not train_ids:
        raise ValueError("at least one --train-tasks id is required")
    check_train_only(train_ids + val_ids, split)

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
        if args.approval_file is None or not args.approval_file.is_file():
            print(
                "REFUSING: the paid path requires --approval-file pointing at "
                "the recorded approval token (see dspy/APPROVAL.md). "
                "Use --dry-run for the $0 path.",
                file=sys.stderr,
            )
            return 2
        key = os.environ.get("ZAI_OPENAPI_API_KEY", "")
        if not key:
            print("REFUSING: ZAI_OPENAPI_API_KEY is missing from the environment.",
                  file=sys.stderr)
            return 2
        agent_import = "evallab.harbor_rlm:LabRlmAgent"
        secret_holder = tempfile.mkdtemp(prefix="har85-paid-secret-")
        secret_file = Path(secret_holder) / "secret"
        secret_file.write_text(key + "\n")
        os.chmod(secret_file, 0o400)
        del key
        instruction_proposer = None
        from evallab.rlm.harness import build_lm

        reflection_lm = build_lm(
            model_id=args.reflection_model,
            api_key=secret_file.read_text().strip(),
            max_tokens=16_000,
            thinking=True,
            temperature=1.0,
        )
        # One extra student-route call per rollout (trace seed for the GEPA
        # adapter; output unused for scoring). Costed in BUDGET.md.
        trace_lm = build_lm(
            model_id=args.student_route,
            api_key=secret_file.read_text().strip(),
            thinking=False,
        )
    try:
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
            ),
            secret_file=secret_file,
            job_tag=args.job_tag,
        )
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
            "base_policy": args.base_policy,
            "split_manifest": split.get("manifest_digest"),
            "split_status": split.get("status"),
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
