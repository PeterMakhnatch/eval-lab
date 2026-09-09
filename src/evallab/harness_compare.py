"""Lab-facing paired harness comparison: prepare, submit, inspect.

Uses existing ExperimentSpec, DirectoryQueue, PolicyGate, AgentProfile and
inspect_experiment surfaces. Does not create a queue, store, scheduler or
execution bypass. Billable model arms remain held until a recorded per-spec
approval; oracle/nop follow standing local-controls.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evallab.execution_contracts import CONTROL_AGENTS, HARBOR_AGENT_IMPORT_PATHS, load_policy
from evallab.profiles import builtin_profiles, validate_model_pin
from evallab.queue import QUEUE_STATES, DirectoryQueue, PolicyGate
from evallab.schemas import ExperimentSpec

KIND = "harness_paired_comparison"
SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]+$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class HarnessCompareError(ValueError):
    """Fail-closed refusal from paired comparison compilation."""


class ArmBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1)
    role: Literal["baseline", "candidate"]


class TaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=1)
    label: str | None = None
    canary: bool = False

    @field_validator("task")
    @classmethod
    def task_is_repo_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("task paths must stay relative to the repository")
        return value


class ModelPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    configured_id: str = Field(min_length=1)
    revision: str | None = None
    revision_status: Literal["immutable", "unknown"] = "unknown"

    @model_validator(mode="after")
    def unknown_revision_is_explicit(self) -> ModelPin:
        if self.revision is None and self.revision_status != "unknown":
            raise ValueError("missing model revision must be revision_status=unknown")
        if self.revision is not None and self.revision_status != "immutable":
            raise ValueError("a recorded revision must be revision_status=immutable")
        return self


class PairedComparisonManifest(BaseModel):
    """Frozen cohort/conditions both arms consume unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    comparison_id: str
    question: str = Field(min_length=1)
    root_model: ModelPin
    worker_model: ModelPin | None = None
    baseline: ArmBinding
    candidate: ArmBinding
    tasks: tuple[TaskEntry, ...]
    analysis_report: str | None = None
    notes: tuple[str, ...] = ()

    @field_validator("comparison_id")
    @classmethod
    def comparison_id_is_slug(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value) or len(value) > 64:
            raise ValueError(
                "comparison_id must be a lowercase hyphenated slug of at most 64 characters"
            )
        return value

    @field_validator("analysis_report")
    @classmethod
    def analysis_report_is_repo_relative(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("analysis_report must stay relative to the repository")
        return value

    @model_validator(mode="after")
    def arms_and_canary_are_distinct(self) -> PairedComparisonManifest:
        if self.baseline.role != "baseline" or self.candidate.role != "candidate":
            raise ValueError("baseline and candidate roles must be exactly those labels")
        if self.baseline.profile_id == self.candidate.profile_id:
            raise ValueError("baseline and candidate must use different profiles")
        if not self.tasks:
            raise ValueError("manifest requires at least one task")
        if len(self.tasks) > 4:
            raise ValueError("initial live grid is at most four development tasks")
        canaries = [entry for entry in self.tasks if entry.canary]
        if len(canaries) != 1:
            raise ValueError("manifest must mark exactly one canary task")
        labels = [entry.task for entry in self.tasks]
        if len(set(labels)) != len(labels):
            raise ValueError("manifest task paths must be unique")
        return self


def load_manifest(path: Path) -> PairedComparisonManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessCompareError(f"unreadable comparison manifest {path}: {exc}") from exc
    try:
        return PairedComparisonManifest.model_validate(payload)
    except Exception as exc:
        raise HarnessCompareError(f"invalid comparison manifest {path}: {exc}") from exc


def manifest_digest(manifest: PairedComparisonManifest) -> str:
    canonical = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _slug(value: str) -> str:
    text = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (text or "task")[:32].rstrip("-")


def _spec_name(comparison_id: str, role: str, task: str) -> str:
    name = f"{comparison_id}-{role}-{_slug(task.split('/')[-1])}"
    return name[:80].rstrip("-")


def _resolve_task(repo_root: Path, task_ref: str) -> Path:
    task_dir = repo_root / task_ref
    if not task_dir.is_dir() or not (task_dir / "task.toml").is_file():
        raise HarnessCompareError(f"task package not found: {task_ref}")
    return task_dir


def _profile(profile_id: str) -> Any:
    profiles = builtin_profiles()
    if profile_id not in profiles:
        raise HarnessCompareError(f"unknown agent profile {profile_id!r}")
    return profiles[profile_id]


def _runtime_status(adapter: str) -> dict[str, Any]:
    if adapter in CONTROL_AGENTS:
        return {"status": "control", "adapter": adapter, "import_path": None}
    import_path = HARBOR_AGENT_IMPORT_PATHS.get(adapter)
    if import_path:
        return {"status": "registered", "adapter": adapter, "import_path": import_path}
    return {
        "status": "unavailable",
        "adapter": adapter,
        "import_path": None,
        "reason": (
            f"adapter {adapter!r} is not in CONTROL_AGENTS or HARBOR_AGENT_IMPORT_PATHS; "
            "HAR-12/HAR-10 must publish the runtime before this arm can execute"
        ),
    }


def compile_arm_spec(
    repo_root: Path,
    *,
    manifest: PairedComparisonManifest,
    entry: TaskEntry,
    binding: ArmBinding,
    submitted_by: str,
) -> dict[str, Any]:
    profile = _profile(binding.profile_id)
    validate_model_pin(profile, profile.model)
    task_dir = _resolve_task(repo_root, entry.task)
    runtime = _runtime_status(profile.adapter)
    purpose = "baseline" if binding.role == "baseline" else "comparison"
    model = None if profile.adapter in CONTROL_AGENTS else profile.model
    spec = ExperimentSpec(
        name=_spec_name(manifest.comparison_id, binding.role, entry.task),
        hypothesis=manifest.question,
        purpose=purpose,
        question_ref=manifest.comparison_id,
        task=entry.task,
        task_path=task_dir.relative_to(repo_root).as_posix(),
        agent=profile.adapter,
        model=model,
        attempts=1,
        concurrency=1,
        submitted_by=submitted_by,
        grid_id=manifest.comparison_id,
        grid_point={
            "arm": binding.role,
            "task": entry.task,
            "canary": entry.canary,
            "manifest_digest": manifest_digest(manifest),
        },
    )
    return {
        "role": binding.role,
        "profile_id": binding.profile_id,
        "secret_source": profile.secret_source,
        "auth_mode": profile.auth_mode,
        "runtime": runtime,
        "canary": entry.canary,
        "task": entry.task,
        "spec": spec.model_dump(mode="json"),
        "root_model": manifest.root_model.model_dump(mode="json"),
        "worker_model": None
        if manifest.worker_model is None
        else manifest.worker_model.model_dump(mode="json"),
    }


def compile_pair(
    repo_root: Path,
    manifest: PairedComparisonManifest,
    *,
    submitted_by: str,
    canary_only: bool = True,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    selected = (
        [entry for entry in manifest.tasks if entry.canary] if canary_only else list(manifest.tasks)
    )
    arms = [
        compile_arm_spec(
            repo_root, manifest=manifest, entry=entry, binding=binding, submitted_by=submitted_by
        )
        for entry in selected
        for binding in (manifest.baseline, manifest.candidate)
    ]
    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "comparison_id": manifest.comparison_id,
        "question": manifest.question,
        "manifest_digest": manifest_digest(manifest),
        "canary_only": canary_only,
        "root_model": manifest.root_model.model_dump(mode="json"),
        "worker_model": None
        if manifest.worker_model is None
        else manifest.worker_model.model_dump(mode="json"),
        "notes": list(manifest.notes),
        "arms": arms,
        "notices": [
            "Compilation does not approve, tick, or run Harbor jobs.",
            "Billable arms require a recorded per-spec approval; oracle/nop follow standing local-controls.",
            "A one-attempt canary is operability evidence, not a statistically established win.",
            "Missing HAR-12/HAR-10 runtime bindings stay unavailable rather than executing a fallback agent.",
        ],
    }


def _queue_and_gate(
    repo_root: Path, queue_root: Path | None, *, create: bool = True
) -> tuple[DirectoryQueue, PolicyGate]:
    queue = DirectoryQueue((queue_root or (repo_root / "queue")).resolve(), create=create)
    policy_path = repo_root / "policy/standing-approvals.yaml"
    if not policy_path.is_file():
        raise HarnessCompareError(f"standing approvals policy missing: {policy_path}")
    policy = load_policy(policy_path)
    return queue, PolicyGate(policy, repo_root=repo_root)


def submit_pair(
    repo_root: Path,
    manifest: PairedComparisonManifest,
    *,
    submitted_by: str,
    canary_only: bool = True,
    queue_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    compiled = compile_pair(repo_root, manifest, submitted_by=submitted_by, canary_only=canary_only)
    queue, gate = _queue_and_gate(repo_root, queue_root, create=True)
    submitted: list[dict[str, Any]] = []
    for arm in compiled["arms"]:
        spec = ExperimentSpec.model_validate(arm["spec"])
        destination, decision = queue.submit(spec, gate=gate, spent_today_usd=0.0)
        stored = queue.load(destination)
        row = {
            **arm,
            "spec": stored.model_dump(mode="json"),
            "spec_id": stored.spec_id,
            "agent": stored.agent,
            "model": stored.model,
            "name": stored.name,
            "queue_path": str(destination),
            "queue_state": destination.parent.name,
            "admitted": decision.admitted,
            "policy_rule": decision.policy_rule,
            "reason_code": decision.reason_code,
            "policy_message": decision.message,
            "runtime": arm["runtime"],
        }
        if not decision.admitted:
            row["hold"] = {
                "reason_code": decision.reason_code,
                "message": decision.message,
                "approval_command": (
                    f"uv run evallab approve {stored.spec_id} --actor <you>"
                    if decision.reason_code == "paid_run_unauthorized"
                    else None
                ),
            }
        submitted.append(row)
    compiled["arms"] = submitted
    compiled["submitted"] = True
    return compiled


def _queue_rows_for_comparison(queue: DirectoryQueue, comparison_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in QUEUE_STATES:
        state_dir = queue.state_dir(state)
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            try:
                spec = queue.load(path)
            except ValueError:
                continue
            if spec.grid_id != comparison_id and spec.question_ref != comparison_id:
                continue
            point = spec.grid_point if isinstance(spec.grid_point, dict) else {}
            rows.append(
                {
                    "spec_id": spec.spec_id,
                    "name": spec.name,
                    "agent": spec.agent,
                    "model": spec.model,
                    "task": spec.task,
                    "arm": point.get("arm"),
                    "canary": point.get("canary"),
                    "queue_state": state,
                    "queue_path": str(path),
                    "policy_rule": spec.policy_rule,
                }
            )
    return rows


def inspect_pair(
    repo_root: Path,
    *,
    comparison_id: str,
    analysis_report: Path | None = None,
    queue_root: Path | None = None,
) -> dict[str, Any]:
    """Link baseline/candidate queue rows and selected native jobs. Read-only."""
    from evallab.explorer import inspect_experiment

    repo_root = repo_root.resolve()
    queue, _ = _queue_and_gate(repo_root, queue_root, create=False)
    queue_rows = _queue_rows_for_comparison(queue, comparison_id)
    jobs: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row in queue_rows:
        spec_id = row["spec_id"]
        if not spec_id:
            missing.append({**row, "missing": "spec_id"})
            continue
        view = inspect_experiment(repo_root, experiment_id=spec_id)
        selected = list(view.get("jobs") or [])
        if not selected:
            missing.append({**row, "missing": "job", "notices": view.get("notices") or []})
        for job in selected:
            jobs.append({**row, "job": job})
    analysis: dict[str, Any] | None = None
    analysis_issue: str | None = None
    if analysis_report is not None:
        try:
            raw = analysis_report.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
            analysis = {
                "path": str(analysis_report),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "data": payload,
                "qualification": "supplied HAR-13/analysis product; not recomputed here",
            }
        except (OSError, ValueError) as exc:
            analysis_issue = f"analysis report unreadable ({type(exc).__name__})"
    baseline = [row for row in queue_rows if row.get("arm") == "baseline"]
    candidate = [row for row in queue_rows if row.get("arm") == "candidate"]
    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "comparison_id": comparison_id,
        "queue": queue_rows,
        "jobs": jobs,
        "missing_arms": missing,
        "baseline_count": len(baseline),
        "candidate_count": len(candidate),
        "analysis": analysis,
        "issues": [analysis_issue] if analysis_issue else [],
        "notices": [
            "Queue state is policy/execution truth; job presence is evidence availability, not validity.",
            "Unapproved model jobs remain in waiting with their recorded reason_code.",
            "Partial/neutral rewards and infrastructure errors stay visible; missing usage is not zero.",
        ],
    }


def render_pair_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"paired harness comparison ({report.get('kind')})",
        f"comparison_id: {report.get('comparison_id')}",
        f"question: {report.get('question') or '(inspect)'}",
        f"manifest: {report.get('manifest_digest') or 'n/a'}",
        f"canary_only: {report.get('canary_only', 'n/a')}",
        f"root_model: {report.get('root_model')}",
        "",
        "arms:",
    ]
    for arm in report.get("arms") or report.get("queue") or ():
        hold = arm.get("hold") or {}
        runtime = arm.get("runtime") or {}
        lines.append(
            f"  - {arm.get('role') or arm.get('arm')} {arm.get('name') or arm.get('spec_id')} "
            f"agent={arm.get('agent') or (arm.get('spec') or {}).get('agent')} "
            f"state={arm.get('queue_state', 'compiled')} "
            f"admitted={arm.get('admitted')}"
        )
        if runtime:
            lines.append(f"      runtime: {runtime.get('status')} adapter={runtime.get('adapter')}")
            if runtime.get("reason"):
                lines.append(f"      runtime reason: {runtime['reason']}")
        if hold:
            lines.append(f"      hold: {hold.get('reason_code')}")
            if hold.get("message"):
                lines.append(f"      {hold['message'].splitlines()[0]}")
            if hold.get("approval_command"):
                lines.append(f"      next (copy, do not auto-run): {hold['approval_command']}")
    if report.get("missing_arms"):
        lines.append("missing:")
        for row in report["missing_arms"]:
            lines.append(f"  - {row.get('arm')} {row.get('spec_id')} missing={row.get('missing')}")
    if report.get("jobs"):
        lines.append("jobs:")
        for row in report["jobs"]:
            job = row.get("job") or {}
            lines.append(
                f"  - {row.get('arm')} {job.get('name')} status={job.get('execution_status')} {job.get('path')}"
            )
    if report.get("analysis"):
        lines.append(
            f"analysis: supplied {report['analysis']['path']} sha256={report['analysis']['sha256']}"
        )
    for notice in report.get("notices") or ():
        lines.append(f"notice: {notice}")
    for issue in report.get("issues") or ():
        lines.append(f"issue: {issue}")
    return "\n".join(lines) + "\n"


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
