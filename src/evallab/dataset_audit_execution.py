"""Audit execution adapters over the existing guarded Harbor executor."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal

from evallab.dataset_audit_contracts import AuditObservation, AuditStage, AuditTask
from evallab.dataset_audit_sources import file_source
from evallab.execution_contracts import new_ulid
from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.results import load_job


def audit_job_name(stage: str, task: AuditTask) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.task_id.lower()).strip("-")[:24] or "task"
    return f"audit-{stage}-{slug}-{new_ulid().lower()}"


def verify_selected_package(task: AuditTask) -> Path:
    if task.path is None or task.package_digest is None or task.harbor_digest is None:
        raise ValueError(f"{task.task_id}: no materialized, digest-bound package to execute")
    if task_directory_digest(task.path) != task.package_digest:
        raise ValueError(f"{task.task_id}: selected package bytes changed before execution")
    if harbor_task_digest(task.path) != task.harbor_digest:
        raise ValueError(f"{task.task_id}: native Harbor task identity changed before execution")
    return task.path


def run_local_control(
    task: AuditTask,
    *,
    repo_root: Path,
    agent: Literal["oracle", "nop"],
    name: str,
    timeout_seconds: int | None = None,
    linear_card: str | None = None,
) -> Path:
    """Only local, model-free, one-attempt controls can use this path."""
    from evallab.queue import Executor
    from evallab.task_prepare import prepare_task

    if agent not in ("oracle", "nop"):
        raise ValueError("the audit direct path permits only local oracle/nop controls")
    package = verify_selected_package(task)
    prepared = prepare_task(
        repo_root,
        package,
        name=name,
        agent=agent,
        model=None,
        environment="docker",
        timeout_seconds=timeout_seconds,
        est_cost_usd=0.0,
        submitted_by="dataset-audit",
        linear_card=linear_card,
    )
    if prepared.spec.task_package_digest != task.package_digest:
        raise ValueError(f"{task.task_id}: preparation changed the audited package")
    spec = prepared.spec.model_copy(update={
        "spec_id": new_ulid(),
        "task_id": task.task_id,
        "purpose": "calibration",
        "hypothesis": f"Dataset audit {agent} control for {task.dataset_id}/{task.task_id}",
    })
    request = Executor.prepare_request(spec, repo_root=repo_root)
    if (
        request.environment != "docker"
        or request.model is not None
        or request.attempts != 1
        or request.concurrency != 1
    ):
        raise ValueError("a free audit control cannot acquire remote compute or a model")
    return Executor.from_repo(repo_root, create_queue=False).execute_direct(request, ingest=False)


def control_observation(
    task: AuditTask,
    stage: Literal["oracle", "nop"],
    job_dir: Path,
    *,
    reference: str = "provided-solution",
) -> AuditObservation:
    """Read a completed native control; infrastructure is never a scored failure."""
    from evallab.evidence.facts import _task_digest
    from evallab.task_health import label_task, nop_evidence

    job_source = file_source(job_dir / "result.json", binding="native-job")
    try:
        job = load_job(job_dir)
    except (OSError, ValueError) as exc:
        return AuditObservation(
            stage=stage, status="failed", sources=(job_source,),
            facts={"job_path": str(job_dir)}, reason=f"incomplete native job: {exc}",
        )
    if len(job.trials) != 1:
        return AuditObservation(
            stage=stage, status="failed", sources=(job_source,),
            facts={"job_path": str(job_dir), "trials": len(job.trials)},
            reason="audit control requires exactly one native trial",
        )
    trial = job.trials[0]
    actual_digest = _task_digest(trial)
    if isinstance(actual_digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", actual_digest):
        actual_digest = "sha256:" + actual_digest.lower()
    config = trial.config or trial.result.get("config") or {}
    agent_config = config.get("agent") or {}
    actual_agent = agent_config.get("name") if isinstance(agent_config, dict) else None
    sources = (
        job_source,
        file_source(trial.path / "result.json", binding="native-trial"),
        file_source(trial.path / "lock.json", binding="native-task-lock"),
        file_source(trial.path / "verifier/test-stdout.txt", binding="verifier-output"),
    )
    facts = {
        "job_path": str(job_dir),
        "trial_path": str(trial.path),
        "job_id": job.id,
        "trial_id": trial.id,
        "native_task_name": trial.result.get("task_name"),
        "agent": actual_agent,
        "package_digest": task.package_digest,
        "harbor_digest": actual_digest,
        "reference": reference if stage == "oracle" else None,
        "environment": config.get("environment"),
    }
    if actual_digest != task.harbor_digest or actual_agent != stage:
        return AuditObservation(
            stage=stage, status="failed", sources=sources, facts=facts,
            reason="native control agent/task digest does not bind to the audited subject",
        )
    verifier_result = trial.result.get("verifier_result")
    raw_rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    raw_reward = raw_rewards.get("reward") if isinstance(raw_rewards, dict) else None
    reward = (
        float(raw_reward)
        if isinstance(raw_reward, (int, float))
        and not isinstance(raw_reward, bool)
        and math.isfinite(raw_reward)
        else None
    )
    exception = trial.result.get("exception_info")
    finished = trial.result.get("finished_at")
    facts.update(reward=reward, exception=exception, completed=bool(finished))
    if not finished or exception or reward is None or not math.isfinite(reward):
        return AuditObservation(
            stage=stage, status="failed", sources=sources, facts=facts,
            reason="control did not produce an exception-free completed verifier grade",
        )
    if stage == "nop":
        instruction = (
            (task.path / "instruction.md").read_text(encoding="utf-8")
            if task.path is not None and (task.path / "instruction.md").is_file() else ""
        )
        observed = nop_evidence(trial.path, instruction)
        # Descriptive static flags must not decide quality. This uses only the
        # existing recorded-output/no-agent rules, not lexical source assertions.
        label, reasons, detail = label_task({}, observed)
        facts.update(nop=observed, label=label, reasons=reasons, detail=detail)
    else:
        facts["label"] = "oracle:pass" if reward == 1.0 else "oracle:fail"
    return AuditObservation(stage=stage, status="executed", facts=facts, sources=sources)


def run_or_stage_paid(
    task: AuditTask,
    stage: AuditStage,
    *,
    repo_root: Path,
    allow_paid: bool,
    environment: str,
    model: str | None,
    estimated_cost_usd: float | None,
    cost_limit_usd: float | None,
    timeout_seconds: int | None,
    linear_card: str | None,
    approved_spec_id: str | None = None,
) -> tuple[Path | None, AuditObservation]:
    """Prepare/submit, or resume one exact approved spec through the queue.

    Opt-in is not authorization. This function never calls approve, never
    executes a paid request directly, and never ticks an unselected queue.
    """
    from evallab.exploit_probe import PREAMBLE
    from evallab.queue import Executor, approved_spec_digest
    from evallab.task_prepare import TERMINUS_LOCAL_MODEL_SELECTOR, prepare_task

    if not allow_paid:
        raise ValueError("paid audit stages require explicit --allow-paid")
    if stage not in ("leak", "oracle", "nop", "exploit"):
        raise ValueError(f"{stage} is not an execution stage")
    agent = "terminus-2" if stage == "exploit" else "nop" if stage == "nop" else "oracle"
    package = verify_selected_package(task)
    executor = Executor.from_repo(repo_root, create_queue=approved_spec_id is None)
    if approved_spec_id is None:
        if (
            estimated_cost_usd is None
            or not math.isfinite(estimated_cost_usd)
            or estimated_cost_usd <= 0
        ):
            raise ValueError("paid stages require a positive total --est-cost-usd estimate")
        if stage == "exploit" and not model:
            raise ValueError("an exploit probe requires an explicit hosted --model")
        if model == TERMINUS_LOCAL_MODEL_SELECTOR:
            raise ValueError("dataset audit does not launch local LLM inference")
        prepared = prepare_task(
            repo_root, package, name=audit_job_name(stage, task), agent=agent,
            model=model if stage == "exploit" else None, environment=environment,
            timeout_seconds=timeout_seconds, est_cost_usd=estimated_cost_usd,
            cost_limit_usd=cost_limit_usd if stage == "exploit" else None,
            submitted_by="dataset-audit", linear_card=linear_card,
        )
        if prepared.spec.task_package_digest != task.package_digest:
            raise ValueError("prepared paid task differs from the audited package")
        changes: dict[str, Any] = {
            "task_id": task.task_id,
            "question_ref": f"dataset-audit:{task.dataset_id}:{stage}",
            "purpose": "elicitation" if stage == "exploit" else "calibration",
        }
        if stage == "exploit":
            preamble = file_source(repo_root / PREAMBLE)
            if not preamble.available:
                raise ValueError("the existing exploit-probe instruction is unavailable")
            changes.update(
                extra_instruction_path=PREAMBLE,
                extra_instruction_sha256=preamble.sha256,
            )
        spec = prepared.spec.model_copy(update=changes)
        queued, decision = executor.submit(spec)
        submitted = executor.queue.load(queued)
        return None, AuditObservation(
            stage=stage, status="planned",
            facts={
                "spec_id": submitted.spec_id,
                "spec_path": str(queued),
                "estimated_cost_usd": submitted.est_cost_usd,
                "approval_required": True,
                "policy_reason": decision.reason_code,
            },
            sources=(file_source(queued, binding="unapproved-queued-spec"),),
            reason="spec prepared; separate human approval and explicit --approved-spec resume required",
        )

    queued = executor.queue.locate(approved_spec_id)
    spec = executor.queue.load(queued)
    if spec.spec_id != approved_spec_id:
        raise ValueError("approved spec selection must be an exact ID")
    if (
        spec.task_id != task.task_id
        or spec.task_package_digest != task.package_digest
        or spec.agent != agent
        or spec.question_ref != f"dataset-audit:{task.dataset_id}:{stage}"
    ):
        raise ValueError("approved spec does not bind to this dataset/task/stage")
    if spec.model == TERMINUS_LOCAL_MODEL_SELECTOR:
        raise ValueError("dataset audit does not launch local LLM inference")
    if spec.est_cost_usd <= 0:
        raise ValueError("approved paid spec has no positive total cost estimate")
    if stage == "exploit":
        preamble = file_source(repo_root / PREAMBLE)
        if (
            spec.extra_instruction_path != PREAMBLE
            or spec.extra_instruction_sha256 != preamble.sha256
            or not preamble.available
        ):
            raise ValueError("approved exploit instruction binding differs from HAR-161")
    authorization = executor.queue.authorization_for(spec)
    if authorization is None or authorization.approved_spec_digest != approved_spec_digest(spec):
        return None, AuditObservation(
            stage=stage, status="planned",
            facts={"spec_id": approved_spec_id, "approval_required": True,
                   "estimated_cost_usd": spec.est_cost_usd},
            sources=(file_source(queued, binding="authorization-unproven"),),
            reason="no live exact-spec human approval; nothing was dispatched",
        )
    if queued.parent.name not in {"done", "failed", "rejected"}:
        executor.tick(parallel=1, spec_ids=[approved_spec_id])
        queued = executor.queue.locate(approved_spec_id)
    receipt = AuditObservation(
        stage=stage,
        status=(
            "recorded" if queued.parent.name == "done"
            else "failed" if queued.parent.name in {"failed", "rejected"}
            else "planned"
        ),
        facts={"spec_id": approved_spec_id, "queue_state": queued.parent.name,
               "estimated_cost_usd": spec.est_cost_usd},
        sources=(file_source(queued, binding="approved-spec"),),
        reason=(
            None if queued.parent.name == "done"
            else f"queued audit spec is {queued.parent.name}"
            if queued.parent.name in {"failed", "rejected"}
            else executor.last_tick_reason
        ),
    )
    job_dir = repo_root / spec.jobs_dir / spec.name
    return (job_dir if queued.parent.name == "done" else None), receipt


def exploit_observation(task: AuditTask, job_dir: Path) -> AuditObservation:
    """Apply HAR-161 classification only to the selected completed native probe."""
    from evallab.evidence.facts import _task_digest
    from evallab.exploit_probe import verdict

    job = load_job(job_dir)
    if len(job.trials) != 1:
        raise ValueError("an exploit audit requires exactly one native trial")
    trial = job.trials[0]
    candidates = sorted(path for path in job_dir.glob("*__*") if path.is_dir())
    if candidates != [trial.path]:
        raise ValueError("the legacy exploit classifier has ambiguous trial directories")
    digest = _task_digest(trial)
    if digest is not None and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        digest = "sha256:" + digest.lower()
    config = trial.config or trial.result.get("config") or {}
    agent = config.get("agent") if isinstance(config, dict) else None
    agent_name = agent.get("name") if isinstance(agent, dict) else None
    verifier = trial.result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    sources = tuple(
        file_source(trial.path / relative, binding="native-probe")
        for relative in ("result.json", "config.json", "lock.json", "agent/trajectory.json")
    )
    if (
        task.harbor_digest is None or digest != task.harbor_digest
        or agent_name != "terminus-2" or not trial.result.get("finished_at")
        or trial.result.get("exception_info")
        or not isinstance(reward, (int, float)) or isinstance(reward, bool)
        or not math.isfinite(reward)
    ):
        return AuditObservation(
            stage="exploit", status="failed", sources=sources,
            facts={"job_path": str(job_dir), "observed_harbor_digest": digest,
                   "observed_agent": agent_name},
            reason="no completed, scored exploit probe binds to the selected package",
        )
    return AuditObservation(
        stage="exploit", status="executed", sources=sources,
        facts={
            **verdict(job_dir), "package_digest": task.package_digest,
            "harbor_digest": digest,
            "scope": "HAR-161 single adversarial probe; not proof of universal resistance",
        },
    )
