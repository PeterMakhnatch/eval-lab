"""One explicit audit pipeline over native Harbor packages and existing evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from evallab.dataset_audit_contracts import (
    ALL_STAGES,
    AuditAction,
    AuditDataset,
    AuditObservation,
    AuditRecord,
    AuditStage,
    AuditTask,
)


def parse_stages(value: str) -> tuple[AuditStage, ...]:
    names = tuple(part.strip() for part in value.split(","))
    if not names or any(name not in ALL_STAGES for name in names):
        raise ValueError("stages must be a comma-separated subset of " + ",".join(ALL_STAGES))
    if len(set(names)) != len(names):
        raise ValueError("audit stages must not be repeated")
    return cast(tuple[AuditStage, ...], names)


def _bound_control(record: AuditRecord, stage: AuditStage) -> bool:
    observation = record.stages.get(stage)
    if observation is None or observation.status not in ("recorded", "executed"):
        return False
    facts = observation.facts
    reward = facts.get("reward")
    return (
        record.task.package_digest is not None
        and record.task.harbor_digest is not None
        and facts.get("package_digest") == record.task.package_digest
        and facts.get("harbor_digest") == record.task.harbor_digest
        and facts.get("agent") == stage
        and facts.get("completed") is True
        and not facts.get("exception")
        and isinstance(reward, (int, float))
        and not isinstance(reward, bool)
        and math.isfinite(reward)
    )


def _generic_finalize(record: AuditRecord) -> AuditRecord:
    """Conservative routing from completed controls, never descriptive flags."""
    nop = record.stages.get("nop")
    oracle = record.stages.get("oracle")
    leak = record.stages.get("leak")
    nop_known = _bound_control(record, "nop")
    oracle_known = _bound_control(record, "oracle")
    leak_bound = (
        leak is not None
        and leak.status in ("recorded", "executed")
        and record.task.package_digest is not None
        and leak.facts.get("audited_package_digest") == record.task.package_digest
    )
    verdict = "unknown"
    reason = "no completed, digest-bound positive and negative controls"
    sources = ()
    if nop_known and nop is not None and nop.facts.get("reward") == 1.0:
        verdict, reason, sources = "fix", "no-agent control receives full reward", nop.sources
    elif leak_bound and leak is not None and leak.facts.get("has_future_history") is True:
        verdict, reason, sources = "fix", "image inspection observed history beyond its base", leak.sources
    elif (
        nop_known and oracle_known and nop is not None and oracle is not None
        and nop.facts.get("reward") == 0.0
        and nop.facts.get("label") == "sound"
        and oracle.facts.get("reward") == 1.0
    ):
        verdict = "keep"
        reason = "reference oracle passes and the completed no-agent control fails"
        sources = (*oracle.sources, *nop.sources)
    health_label = (
        str(nop.facts.get("label")) if nop_known and nop is not None else "unchecked"
    )
    if health_label not in {"sound", "broken_environment", "grader_suspect"}:
        health_label = "unchecked"
    health_tag = "health:" + health_label.replace("_", "-")
    tags = (health_tag, "verdict:" + verdict)
    facets = dict(record.facets)
    facets.update(
        health={"tag": health_tag, "scope": "selected package/control evidence; not admission"},
        verdict={"tag": "verdict:" + verdict, "verdict": verdict, "evidence": reason},
        tags=list(tags),
        repair=None,
        failing_tests=facets.get("failing_tests"),
        sources=facets.get("sources", {}),
        errors=facets.get("errors", []),
    )
    facets["leak"] = None if leak is None else {
        "found": leak.facts.get("has_future_history") if leak_bound else None,
        "binding_matches": leak_bound,
        "coverage": leak.facts.get("coverage"),
        "evidence": [source.model_dump(mode="json") for source in leak.sources],
    }
    static = record.stages.get("static")
    facets["static_flags"] = static.facts if static is not None else None
    exploit = record.stages.get("exploit")
    facets["exploit_probes"] = [exploit.facts] if exploit is not None and exploit.status in ("recorded", "executed") else None
    return record.model_copy(update={
        "verdict": verdict, "verdict_reason": reason, "verdict_sources": sources,
        "tags": tags, "facets": facets,
    })


def _reference_available(task: AuditTask) -> bool:
    return task.path is not None and any(
        (task.path / "solution" / ("solve." + suffix)).is_file()
        for suffix in ("sh", "ps1", "cmd", "bat")
    )


def _approved_specs(ids: Sequence[str], root: Path, dataset: AuditDataset) -> dict[tuple[str, AuditStage], Any]:
    if not ids:
        return {}
    from evallab.queue import Executor

    queue = Executor.from_repo(root, create_queue=False).queue
    result = {}
    for spec_id in ids:
        spec = queue.load(queue.locate(spec_id))
        if spec.spec_id != spec_id:
            raise ValueError("approved spec selector must be an exact ID")
        prefix = f"dataset-audit:{dataset.dataset_id}:"
        if not spec.question_ref or not spec.question_ref.startswith(prefix):
            raise ValueError(f"{spec_id}: spec belongs to a different audit dataset")
        stage_name = spec.question_ref.removeprefix(prefix)
        if stage_name not in ALL_STAGES or not spec.task_id:
            raise ValueError(f"{spec_id}: spec has no exact audit task/stage binding")
        key = (spec.task_id, stage_name)
        if key in result:
            raise ValueError(f"multiple approved specs selected for {key}")
        result[key] = spec
    return result


def _action(
    task: AuditTask, stage: AuditStage, observed: AuditObservation | None,
    *, dry_run: bool, allow_paid: bool, environment: str, model: str | None,
    estimated_cost_usd: float | None,
) -> AuditAction:
    if stage in ("static", "history"):
        return AuditAction(stage=stage, task_id=task.task_id, estimated_cost_usd=0,
                           disposition="inspect")
    agent = "terminus-2" if stage == "exploit" else "nop" if stage == "nop" else "oracle"
    paid = stage == "exploit" or environment != "docker"
    if dry_run and not (paid and allow_paid) and observed is not None and observed.status == "recorded":
        return AuditAction(stage=stage, task_id=task.task_id, estimated_cost_usd=0,
                           disposition="reuse", reason="stored evidence; no new execution")
    if paid and not allow_paid:
        return AuditAction(stage=stage, task_id=task.task_id, agent=agent,
                           environment=environment, model=model if stage == "exploit" else None,
                           paid=True, estimated_cost_usd=estimated_cost_usd,
                           disposition="requires_opt_in", reason="paid stage disabled")
    if task.path is None:
        return AuditAction(stage=stage, task_id=task.task_id, paid=paid,
                           estimated_cost_usd=estimated_cost_usd if paid else 0,
                           disposition="unavailable", reason="selected package is unavailable")
    if stage == "oracle" and not _reference_available(task):
        return AuditAction(stage=stage, task_id=task.task_id, paid=paid,
                           estimated_cost_usd=estimated_cost_usd if paid else 0,
                           disposition="unavailable", reason="no materialized reference oracle")
    if paid and (estimated_cost_usd is None or estimated_cost_usd <= 0 or (stage == "exploit" and not model)):
        return AuditAction(stage=stage, task_id=task.task_id, agent=agent,
                           environment=environment, model=model, paid=True,
                           estimated_cost_usd=estimated_cost_usd, disposition="unavailable",
                           reason="paid plan requires a hosted model where applicable and a positive total cost estimate")
    return AuditAction(stage=stage, task_id=task.task_id, agent=agent,
                       environment=environment, model=model if stage == "exploit" else None,
                       paid=paid, estimated_cost_usd=estimated_cost_usd if paid else 0,
                       disposition="run", reason="dry-run: execution not requested" if dry_run else None)


def _execute_stage(
    task: AuditTask, stage: AuditStage, *, root: Path, reports: Path,
    allow_paid: bool, environment: str, model: str | None, estimate: float | None,
    cost_limit: float | None, timeout: int | None, card: str | None,
    approved_spec_id: str | None,
) -> AuditObservation:
    from evallab.dataset_audit_checks import leak_observation, prepare_leak_probe
    from evallab.dataset_audit_execution import (
        audit_job_name,
        control_observation,
        exploit_observation,
        run_local_control,
        run_or_stage_paid,
    )

    subject = task
    if stage == "leak":
        subject = prepare_leak_probe(task, repo_root=root, records_dir=reports / "probe-records")
    if stage == "exploit" or environment != "docker" or approved_spec_id is not None:
        job, receipt = run_or_stage_paid(
            subject, stage, repo_root=root, allow_paid=allow_paid, environment=environment,
            model=model, estimated_cost_usd=estimate, cost_limit_usd=cost_limit,
            timeout_seconds=timeout, linear_card=card, approved_spec_id=approved_spec_id,
        )
        if job is None:
            return receipt
    else:
        job = run_local_control(
            subject, repo_root=root, agent="nop" if stage == "nop" else "oracle",
            name=audit_job_name(stage, subject), timeout_seconds=timeout, linear_card=card,
        )
    if stage == "leak":
        observation = leak_observation(subject, job)
        return observation.model_copy(update={"facts": {
            **observation.facts, "audited_package_digest": task.package_digest,
            "probe_package_digest": subject.package_digest,
        }})
    if stage in ("oracle", "nop"):
        return control_observation(subject, stage, job)
    return exploit_observation(subject, job)


def audit_dataset(
    selector: str,
    *,
    repo_root: Path,
    stages: Sequence[AuditStage] = ALL_STAGES,
    dry_run: bool = True,
    allow_paid: bool = False,
    environment: str = "docker",
    model: str | None = None,
    est_cost_usd: float | None = None,
    cost_limit_usd: float | None = 0.25,
    timeout_seconds: int | None = None,
    linear_card: str | None = None,
    approved_specs: Sequence[str] = (),
    derived_root: Path | None = None,
    output_dir: Path | None = None,
    static_audit: Path | None = Path("/tmp/static-task-audit.csv"),
    reader_store: Path | None = None,
) -> dict[str, Any]:
    from evallab.dataset_audit_checks import static_observation
    from evallab.dataset_audit_plugins import dataset_plugin
    from evallab.dataset_audit_sources import resolve_harbor_dataset
    from evallab.storage.paths import task_audit_path
    from evallab.storage.task_audit import read_audit_records
    from evallab.task_dossier_trials import task_trials_for_tasks

    selected = tuple(stages)
    if not selected or any(stage not in ALL_STAGES for stage in selected) or len(set(selected)) != len(selected):
        raise ValueError("invalid or repeated audit stages")
    if est_cost_usd is not None and (not math.isfinite(est_cost_usd) or est_cost_usd < 0):
        raise ValueError("cost estimate must be finite and nonnegative")
    root = repo_root.resolve()
    plugin = dataset_plugin(selector)
    parquet = task_audit_path(root, derived_root=derived_root)
    if plugin is not None:
        dataset, records = plugin.read_stored_audit(root, stages=selected, static_audit=static_audit)
    else:
        dataset = resolve_harbor_dataset(selector, repo_root=root, download=not dry_run)
        cached = read_audit_records(parquet) if parquet.is_file() else []
        records = []
        for task in dataset.tasks:
            matches = [row for row in cached if row.task.dataset_id == dataset.dataset_id
                       and row.task.task_id == task.task_id
                       and row.task.package_digest == task.package_digest
                       and row.task.harbor_digest == task.harbor_digest]
            records.append(matches[0].model_copy(update={"task": task}) if len(matches) == 1 else AuditRecord(task=task))
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", dataset.dataset_id)[:60]
    reports = output_dir or parquet.parent / "audit-reports" / (slug + "-" + hashlib.sha256(dataset.source_uri.encode()).hexdigest()[:12])
    approved = _approved_specs(approved_specs, root, dataset)
    actions: list[AuditAction] = []
    for record in records:
        for stage in selected:
            spec = approved.get((record.task.task_id, stage))
            actions.append(_action(
                record.task, stage, record.stages.get(stage), dry_run=dry_run,
                allow_paid=allow_paid, environment=spec.environment if spec else environment,
                model=spec.model if spec else model,
                estimated_cost_usd=spec.est_cost_usd if spec else est_cost_usd,
            ))
    enabled_paid = [action for action in actions if action.paid and action.disposition == "run"]
    estimated = sum(action.estimated_cost_usd or 0 for action in actions if action.disposition == "run")
    if not dry_run:
        print(f"estimated total cost: ${estimated:.4f} (not an infrastructure spending cap)", file=sys.stderr)
        if enabled_paid:
            print("Paid opt-in does not authorize a spec; the existing exact-spec approval gate remains mandatory.", file=sys.stderr)
    by_action = {(action.task_id, action.stage): action for action in actions}
    updated = []
    for record in records:
        observations = dict(record.stages)
        for stage in selected:
            action = by_action[(record.task.task_id, stage)]
            if stage == "history":
                continue
            if stage == "static":
                if not dry_run or observations.get(stage) is None:
                    observations[stage] = plugin.static_observation(record.task) if plugin is not None else static_observation(record.task)
                continue
            if dry_run or action.disposition != "run":
                if stage not in observations:
                    observations[stage] = AuditObservation(
                        stage=stage, status="unavailable" if action.disposition == "unavailable" else "planned",
                        facts={"estimated_cost_usd": action.estimated_cost_usd}, reason=action.reason,
                    )
                continue
            spec = approved.get((record.task.task_id, stage))
            try:
                observations[stage] = _execute_stage(
                    record.task, stage, root=root, reports=reports,
                    allow_paid=allow_paid, environment=spec.environment if spec else environment,
                    model=spec.model if spec else model, estimate=spec.est_cost_usd if spec else est_cost_usd,
                    cost_limit=cost_limit_usd, timeout=timeout_seconds, card=linear_card,
                    approved_spec_id=spec.spec_id if spec else None,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                observations[stage] = AuditObservation(
                    stage=stage, status="failed", reason=f"{type(exc).__name__}: {exc}",
                )
        updated.append(record.model_copy(update={"stages": observations}))
    trial_facets = task_trials_for_tasks(dataset.tasks, repo_root=root, derived_root=derived_root,
                                        reader_store=reader_store) if "history" in selected or not dry_run else None
    final_records = []
    for record in updated:
        if "history" in selected and trial_facets is not None:
            history = trial_facets[record.task.task_id]
            prior = record.stages.get("history")
            fields = ("job", "trial", "job_id", "trial_id", "model", "harness", "legit",
                      "reward", "reward_gated", "copy_verdict", "stop_reason", "task_digest")
            facts = dict(prior.facts) if prior is not None else {}
            facts.update(native_trials=[{key: trial.get(key) for key in fields} for trial in history["trials"]],
                         native_coverage=history["coverage"], native_trial_count=len(history["trials"]))
            observation = AuditObservation(stage="history", status="recorded", facts=facts,
                                           sources=prior.sources if prior else (),
                                           reason="read-only native census, including controls; no model was run")
            record = record.model_copy(update={"stages": {**record.stages, "history": observation}})
        final_records.append(plugin.finalize_record(record) if plugin is not None else _generic_finalize(record))
    outputs: dict[str, str] = {}
    if not dry_run:
        from evallab.dataset_audit_outputs import export_audit_dossiers, publish_audit_tags
        from evallab.storage.task_audit import write_audit

        write_audit(final_records, parquet, dataset=dataset)
        manifest = publish_audit_tags(final_records, repo_root=root, output_dir=reports)
        dossiers = export_audit_dossiers(final_records, repo_root=root, output_dir=reports,
                                         derived_root=derived_root, reader_store=reader_store,
                                         trial_facets=trial_facets)
        outputs = {"audit_parquet": str(parquet), "task_health_manifest": str(manifest),
                   "dossiers": str(dossiers)}
    failures = [f"{row.task.task_id}:{stage}:{observation.reason}" for row in final_records
                for stage, observation in row.stages.items() if observation.status == "failed"]
    return {
        "schema": "evallab.dataset_audit/v1", "dataset": dataset.model_dump(mode="json", exclude={"tasks"}),
        "dry_run": dry_run, "paid_opt_in": allow_paid, "stages": list(selected),
        "counts": dict(Counter(record.verdict for record in final_records)),
        "task_count": len(final_records), "estimated_cost_usd": estimated,
        "estimate_is_infrastructure_cap": False, "outputs": outputs,
        "actions": [action.model_dump(mode="json") for action in actions],
        "records": [record.model_dump(mode="json") for record in final_records],
        "failures": failures,
    }


def _command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    try:
        result = audit_dataset(
            args.dataset, repo_root=root, stages=parse_stages(args.stages), dry_run=not args.execute,
            allow_paid=args.allow_paid, environment=args.environment, model=args.model,
            est_cost_usd=args.est_cost_usd, cost_limit_usd=args.cost_limit_usd,
            timeout_seconds=args.timeout_seconds, linear_card=args.linear_card,
            approved_specs=args.approved_spec, derived_root=args.derived_root,
            output_dir=args.output_dir, static_audit=args.static_audit, reader_store=args.reader_store,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    else:
        print(f"Dataset: {result['dataset']['dataset_id']} ({result['task_count']} tasks)")
        print("Mode: " + ("dry-run; no execution or output writes" if result["dry_run"] else "explicit execution"))
        print("Verdicts: " + ", ".join(f"{name}={result['counts'].get(name, 0)}" for name in ("keep", "fix", "discard", "unknown")))
        print(f"Estimated new execution cost: ${result['estimated_cost_usd']:.4f} (not an infrastructure spending cap)")
        if not result["paid_opt_in"]:
            print("Paid stages: disabled; --allow-paid and separate exact-spec approval are required")
        for stage in result["stages"]:
            statuses = Counter(record["stages"][stage]["status"] for record in result["records"] if stage in record["stages"])
            print(f"  {stage}: " + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items())))
        for name, path in result["outputs"].items():
            print(f"{name}: {path}")
        for failure in result["failures"]:
            print(f"Evidence failure: {failure}", file=sys.stderr)
    return 1 if result["failures"] else 0


def build_dataset_audit_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("audit", help="Audit any Harbor dataset; stored evidence and dry-run by default")
    parser.add_argument("dataset", help="Pinned Harbor dataset, cached dataset name, or real task/dataset directory")
    parser.add_argument("--stages", default=",".join(ALL_STAGES))
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--execute", action="store_true", help="Run selected enabled stages and write their projections")
    execution.add_argument("--dry-run", action="store_false", dest="execute", help="Read/plan only; no downloads, runs, queue writes, or output writes (default)")
    parser.set_defaults(execute=False)
    parser.add_argument("--allow-paid", action="store_true", help="Opt in to paid preparation; never substitutes for exact-spec human approval")
    parser.add_argument("--environment", default="docker")
    parser.add_argument("--model", help="Explicit hosted model for opt-in adversarial probes")
    parser.add_argument("--est-cost-usd", type=float, help="Per-task/stage total execution estimate, including infrastructure")
    parser.add_argument("--cost-limit-usd", type=float, default=0.25)
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--linear-card")
    parser.add_argument("--approved-spec", action="append", default=[], metavar="ID", help="Resume only this already queued, task/stage-bound spec after human approval")
    parser.add_argument("--derived-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--reader-store", type=Path)
    parser.add_argument("--static-audit", type=Path, default=Path("/tmp/static-task-audit.csv"))
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_command)
