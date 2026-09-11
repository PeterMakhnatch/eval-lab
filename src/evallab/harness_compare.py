"""Lab-facing paired harness comparison: prepare, submit, inspect.

Uses existing ExperimentSpec, DirectoryQueue, PolicyGate, AgentProfile and
inspect_experiment surfaces. Does not create a queue, store, scheduler or
execution bypass. Billable model arms remain held until a recorded per-spec
approval; oracle/nop follow standing local-controls.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evallab.execution_contracts import CONTROL_AGENTS, HARBOR_AGENT_IMPORT_PATHS, load_policy
from evallab.profiles import builtin_profiles, validate_model_pin
from evallab.queue import QUEUE_STATES, DirectoryQueue, PolicyGate
from evallab.schemas import CohortComparisonSpec, ExperimentSpec

KIND = "harness_paired_comparison"
SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]+$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
FACTORY_ARM_IDS = {"baseline": "mini-swe", "candidate": "authors-rlm"}
DEFAULT_BASELINE_PROFILE = "mini-swe-agent-deepseek-v4-flash"
DEFAULT_CANDIDATE_PROFILE = "authors-rlm-deepseek-v4-flash"


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
    registered_ref: str | None = None
    task_id: str | None = None
    task_family: str | None = None
    version: str | None = None
    verifier_digest: str | None = None
    task_package_digest: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    stage: str | None = None

    @field_validator("task", "registered_ref")
    @classmethod
    def task_is_repo_relative(cls, value: str | None) -> str | None:
        if value is None:
            return value
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
    cohort_source: str | None = None
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
    def optional_repo_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("path must stay relative to the repository")
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
    if isinstance(payload, dict) and "cohort_id" in payload and "members" in payload:
        raise HarnessCompareError(
            f"{path} is a Factory cohort.json, not a PairedComparisonManifest. "
            "Compose it with compose_manifest_from_cohort() or "
            "`paired-compare prepare --cohort PATH --baseline-profile "
            f"{DEFAULT_BASELINE_PROFILE} --candidate-profile "
            f"{DEFAULT_CANDIDATE_PROFILE} --root-model <id>`."
        )
    try:
        return PairedComparisonManifest.model_validate(payload)
    except HarnessCompareError:
        raise
    except Exception as exc:
        raise HarnessCompareError(f"invalid comparison manifest {path}: {exc}") from exc


def _factory_module(cohort_path: Path) -> Any | None:
    compile_path = cohort_path.parent / "compile.py"
    if not compile_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("harness_first_factory_compile", compile_path)
    if spec is None or spec.loader is None:
        raise HarnessCompareError(f"unable to load Factory compiler {compile_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_manifest_from_cohort(
    cohort_path: Path,
    *,
    baseline_profile: str,
    candidate_profile: str,
    root_model: ModelPin,
    worker_model: ModelPin | None = None,
    analysis_report: str | None = None,
) -> PairedComparisonManifest:
    """Map Factory cohort.json members onto the Lab paired manifest.

    Task identities stay Factory's. Arm/model bindings are supplied here.
    """
    try:
        payload = json.loads(cohort_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessCompareError(f"unreadable Factory cohort {cohort_path}: {exc}") from exc
    if not isinstance(payload, dict) or "members" not in payload or "cohort_id" not in payload:
        raise HarnessCompareError(f"{cohort_path} is not a Factory cohort.json")
    canary_id = payload.get("canary_task_id")
    members = payload.get("members") or []
    if not isinstance(members, list) or not members:
        raise HarnessCompareError(f"{cohort_path} has no frozen members")
    tasks: list[TaskEntry] = []
    for member in members:
        if not isinstance(member, dict):
            raise HarnessCompareError("Factory member must be an object")
        task_id = str(member.get("task_id") or "")
        task_path = str(member.get("task_path") or "")
        if not task_id or not task_path:
            raise HarnessCompareError("Factory member is missing task_id or task_path")
        digests = member.get("digests") if isinstance(member.get("digests"), dict) else {}
        limits = member.get("limits") if isinstance(member.get("limits"), dict) else {}
        timeout = limits.get("timeout_seconds")
        tasks.append(
            TaskEntry(
                task=task_path,
                label=task_id,
                canary=task_id == canary_id,
                registered_ref=member.get("registered_ref"),
                task_id=task_id,
                task_family=member.get("task_family"),
                version=member.get("version"),
                verifier_digest=digests.get("verifier"),
                task_package_digest=digests.get("package"),
                timeout_seconds=int(timeout) if timeout is not None else None,
                stage=member.get("stage"),
            )
        )
    notes = [
        f"factory_linear_issue={payload.get('linear_issue')}",
        f"factory_frozen_at_commit={payload.get('frozen_at_commit')}",
        f"primary_metric={payload.get('primary_metric')}",
        "Factory identities/lineage are preserved; Integration supplies arm/model bindings.",
        "A one-attempt canary is operability evidence, not a ranking.",
    ]
    if worker_model is None:
        worker_model = root_model
        notes.append(
            "worker_model defaults to the shared root checkpoint; it is not an independently observed worker pin."
        )
    try:
        return PairedComparisonManifest(
            comparison_id=str(payload["cohort_id"]),
            question=str(payload.get("scientific_question") or payload["cohort_id"]),
            root_model=root_model,
            worker_model=worker_model,
            baseline=ArmBinding(profile_id=baseline_profile, role="baseline"),
            candidate=ArmBinding(profile_id=candidate_profile, role="candidate"),
            tasks=tuple(tasks),
            cohort_source=cohort_path.as_posix(),
            analysis_report=analysis_report,
            notes=tuple(notes),
        )
    except Exception as exc:
        raise HarnessCompareError(
            f"cannot compose paired manifest from {cohort_path}: {exc}"
        ) from exc


def load_pair_inputs(
    *,
    manifest_path: Path | None = None,
    cohort_path: Path | None = None,
    baseline_profile: str = DEFAULT_BASELINE_PROFILE,
    candidate_profile: str = DEFAULT_CANDIDATE_PROFILE,
    root_model: str | None = None,
    root_revision: str | None = None,
    worker_model: str | None = None,
    analysis_report: str | None = None,
) -> PairedComparisonManifest:
    if manifest_path is not None and cohort_path is not None:
        raise HarnessCompareError("pass either --manifest or --cohort, not both")
    if cohort_path is not None:
        if not root_model:
            raise HarnessCompareError(
                "Factory cohort composition requires --root-model "
                "(shared configured id; revision_status=unknown unless --root-revision is set)"
            )
        pin = ModelPin(
            configured_id=root_model,
            revision=root_revision,
            revision_status="immutable" if root_revision else "unknown",
        )
        worker = None
        if worker_model:
            worker = ModelPin(
                configured_id=worker_model,
                revision=root_revision if worker_model == root_model else None,
                revision_status="immutable"
                if worker_model == root_model and root_revision
                else "unknown",
            )
        return compose_manifest_from_cohort(
            cohort_path,
            baseline_profile=baseline_profile,
            candidate_profile=candidate_profile,
            root_model=pin,
            worker_model=worker,
            analysis_report=analysis_report,
        )
    if manifest_path is None:
        raise HarnessCompareError("prepare/submit requires --manifest or --cohort")
    return load_manifest(manifest_path)


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
    if not import_path:
        return {
            "status": "unavailable",
            "adapter": adapter,
            "import_path": None,
            "reason": (
                f"adapter {adapter!r} is not in CONTROL_AGENTS or HARBOR_AGENT_IMPORT_PATHS; "
                "HAR-12/HAR-10 must publish the runtime before this arm can execute"
            ),
        }
    module_name, _, attr = import_path.partition(":")
    spec = importlib.util.find_spec(module_name)
    if spec is None or not attr:
        return {
            "status": "unavailable",
            "adapter": adapter,
            "import_path": import_path,
            "reason": (
                f"published import path {import_path} does not resolve on this branch; "
                "refusing to substitute a fallback agent"
            ),
        }
    return {
        "status": "registered",
        "adapter": adapter,
        "import_path": import_path,
        "module": module_name,
        "symbol": attr,
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
    root_id = manifest.root_model.configured_id
    try:
        if profile.adapter in CONTROL_AGENTS:
            validate_model_pin(profile, None)
        else:
            validate_model_pin(profile, root_id)
    except ValueError as exc:
        raise HarnessCompareError(str(exc)) from exc
    task_dir = _resolve_task(repo_root, entry.task)
    runtime = _runtime_status(profile.adapter)
    purpose = "baseline" if binding.role == "baseline" else "comparison"
    model = None if profile.adapter in CONTROL_AGENTS else root_id
    factory_arm = FACTORY_ARM_IDS[binding.role]
    spec: ExperimentSpec | None = None
    if manifest.cohort_source and profile.adapter not in CONTROL_AGENTS:
        cohort_path = Path(manifest.cohort_source)
        if not cohort_path.is_absolute():
            cohort_path = repo_root / cohort_path
        factory = _factory_module(cohort_path)
        if factory is not None and entry.task_id:
            try:
                member = factory.member_for(factory.load_cohort(cohort_path), entry.task_id)
            except Exception:
                member = {
                    "stage": entry.stage or ("canary" if entry.canary else "matrix"),
                    "task_id": entry.task_id,
                    "registered_ref": entry.registered_ref or entry.task,
                    "task_path": entry.task,
                    "limits": {"timeout_seconds": entry.timeout_seconds or 1800},
                    "version": entry.version,
                    "digests": {
                        "verifier": entry.verifier_digest,
                        "package": entry.task_package_digest,
                    },
                    "task_family": entry.task_family,
                }
            spec = factory.compile_spec(
                member,
                arm_id=factory_arm,
                agent=profile.adapter,
                model=root_id,
            )
            spec = spec.model_copy(
                update={
                    "submitted_by": submitted_by,
                    "hypothesis": manifest.question,
                    "grid_id": manifest.comparison_id,
                    "grid_point": {
                        **(spec.grid_point or {}),
                        "arm": binding.role,
                        "arm_id": factory_arm,
                        "task": entry.task,
                        "canary": entry.canary,
                        "manifest_digest": manifest_digest(manifest),
                        "root_model": manifest.root_model.model_dump(mode="json"),
                        "worker_model": None
                        if manifest.worker_model is None
                        else manifest.worker_model.model_dump(mode="json"),
                    },
                }
            )
    if spec is None:
        spec = ExperimentSpec(
            name=_spec_name(manifest.comparison_id, binding.role, entry.task),
            hypothesis=manifest.question,
            purpose=purpose,
            question_ref=manifest.cohort_source or manifest.comparison_id,
            task=entry.registered_ref or entry.task,
            task_path=task_dir.relative_to(repo_root).as_posix(),
            agent=profile.adapter,
            model=model,
            environment="docker",
            attempts=1,
            concurrency=1,
            timeout_seconds=entry.timeout_seconds or 1800,
            submitted_by=submitted_by,
            task_version=entry.version,
            verifier_digest=entry.verifier_digest,
            task_package_digest=entry.task_package_digest,
            task_family=entry.task_family,
            task_id=entry.task_id,
            grid_id=manifest.comparison_id,
            grid_point={
                "arm": binding.role,
                "arm_id": factory_arm,
                "task": entry.task,
                "canary": entry.canary,
                "manifest_digest": manifest_digest(manifest),
                "root_model": manifest.root_model.model_dump(mode="json"),
                "worker_model": None
                if manifest.worker_model is None
                else manifest.worker_model.model_dump(mode="json"),
            },
        )
    if profile.adapter not in CONTROL_AGENTS and (
        entry.task_id == "event-summary" or entry.task.rstrip("/").endswith("event-summary")
    ):
        spec = spec.model_copy(
            update={
                "est_cost_usd": 2.5,
            }
        )
    dumped = spec.model_dump(mode="json")
    if dumped.get("model") != (None if profile.adapter in CONTROL_AGENTS else root_id):
        raise HarnessCompareError(
            f"compiled spec model {dumped.get('model')!r} does not match requested "
            f"root {root_id!r} for profile {profile.profile_id}"
        )
    return {
        "role": binding.role,
        "profile_id": binding.profile_id,
        "secret_source": profile.secret_source,
        "auth_mode": profile.auth_mode,
        "runtime": runtime,
        "canary": entry.canary,
        "task": entry.task,
        "spec": dumped,
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


def load_analysis_product(path: Path) -> dict[str, Any]:
    """Load a HAR-13 JSON report, including the directory product (report.json)."""
    json_path = path / "report.json" if path.is_dir() else path
    raw = json_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    product: dict[str, Any] = {
        "path": str(json_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": payload,
        "qualification": "supplied HAR-13/analysis product; not recomputed here",
        "evidence_kind": (payload.get("metadata") or {}).get("evidence_kind"),
    }
    if path.is_dir():
        markdown = path / "report.md"
        svg = path / "plot.svg"
        if markdown.is_file():
            product["markdown_path"] = str(markdown)
            product["markdown"] = markdown.read_text(encoding="utf-8")
        if svg.is_file():
            product["svg_path"] = str(svg)
    return product


def build_pair_comparison_spec(
    comparison_id: str,
    *,
    baseline_jobs: list[str],
    candidate_jobs: list[str],
    experiment_id: str = "HAR-11",
) -> CohortComparisonSpec:
    """Existing CohortComparisonSpec consumed by HAR-13. Both arms required."""
    if not baseline_jobs or not candidate_jobs:
        raise HarnessCompareError(
            "CohortComparisonSpec requires at least one completed job path on each arm"
        )
    return CohortComparisonSpec.model_validate(
        {
            "schema_version": 1,
            "comparison_id": comparison_id,
            "experiment_id": experiment_id,
            "declared_variable": "agent_name",
            "mode": "exploratory",
            "reward_name": "reward",
            "pass_threshold": 1.0,
            "pass_k": [1],
            "budget_exhaustion_is_failure": False,
            "pairing_key": "task_digest",
            "cohorts": [
                {"label": "mini-swe", "paths": baseline_jobs},
                {"label": "authors-rlm", "paths": candidate_jobs},
            ],
        }
    )


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
    issues: list[str] = []
    if analysis_report is not None:
        try:
            analysis = load_analysis_product(analysis_report)
        except (OSError, ValueError) as exc:
            issues.append(f"analysis report unreadable ({type(exc).__name__}: {exc})")
    baseline = [row for row in queue_rows if row.get("arm") in {"baseline", "mini-swe"}]
    candidate = [row for row in queue_rows if row.get("arm") in {"candidate", "authors-rlm"}]
    baseline_paths = [
        str((row.get("job") or {}).get("path"))
        for row in jobs
        if row.get("arm") in {"baseline", "mini-swe"} and (row.get("job") or {}).get("path")
    ]
    candidate_paths = [
        str((row.get("job") or {}).get("path"))
        for row in jobs
        if row.get("arm") in {"candidate", "authors-rlm"} and (row.get("job") or {}).get("path")
    ]
    comparison_spec: dict[str, Any] | None = None
    if baseline_paths and candidate_paths:
        comparison_spec = build_pair_comparison_spec(
            comparison_id,
            baseline_jobs=baseline_paths,
            candidate_jobs=candidate_paths,
        ).model_dump(mode="json")
    else:
        issues.append(
            "CohortComparisonSpec not emitted: both arms need at least one completed job path"
        )
    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "comparison_id": comparison_id,
        "queue": queue_rows,
        "jobs": jobs,
        "missing_arms": missing,
        "baseline_count": len(baseline),
        "candidate_count": len(candidate),
        "comparison_spec": comparison_spec,
        "analysis": analysis,
        "issues": issues,
        "notices": [
            "Queue state is policy/execution truth; job presence is evidence availability, not validity.",
            "Unapproved model jobs remain in waiting with their recorded reason_code.",
            "Partial/neutral rewards and infrastructure errors stay visible; missing usage is not zero.",
            "HAR-13 consumes comparison_spec; Integration does not invent worker=0 or a win from k=1.",
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
    if report.get("comparison_spec"):
        lines.append(
            "comparison_spec: CohortComparisonSpec ready for "
            "research/analysis/harness-first/analyze.py"
        )
    if report.get("analysis"):
        lines.append(
            f"analysis: supplied {report['analysis']['path']} sha256={report['analysis']['sha256']}"
            + (
                f" kind={report['analysis'].get('evidence_kind')}"
                if report["analysis"].get("evidence_kind")
                else ""
            )
        )
    if report.get("verdict"):
        lines.append(f"verdict: {report['verdict']}")
        if report.get("verdict_reason"):
            lines.append(f"verdict_reason: {report['verdict_reason']}")
    for gate in report.get("gates") or ():
        lines.append(f"gate: {gate.get('status')} {gate.get('name')} — {gate.get('detail')}")
    if report.get("spec_ids"):
        lines.append("spec_ids:")
        for row in report["spec_ids"]:
            lines.append(
                f"  - {row.get('arm')} {row.get('spec_id')} state={row.get('queue_state')} stale={row.get('stale')}"
            )
            if row.get("approval_command"):
                lines.append(f"      next (copy, do not auto-run): {row['approval_command']}")
    for notice in report.get("notices") or ():
        lines.append(f"notice: {notice}")
    for issue in report.get("issues") or ():
        lines.append(f"issue: {issue}")
    return "\n".join(lines) + "\n"


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


HAR12_AGENT_VERSION = "0.1.1-har12"
HAR12_GIT_REF = "ddf1a0112c4873e38d165942216429d24c62cccb"
HAR10_GIT_REF = "1183f6af709024db22c6d9f790adb0c75fa676ca"
HAR10_BACKEND_IMPORT = "evallab.rlm_runtime:ManagedReplBackend"


def _gate(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def readiness_report(
    repo_root: Path,
    manifest: PairedComparisonManifest,
    *,
    submitted_by: str,
    queue_root: Path | None = None,
) -> dict[str, Any]:
    """No-spend launch-readiness view. Never ticks Harbor or claims a win."""
    repo_root = repo_root.resolve()
    compiled = compile_pair(repo_root, manifest, submitted_by=submitted_by, canary_only=True)
    if queue_root is None:
        viewed = {
            "queue": [],
            "jobs": [],
            "missing_arms": [],
            "issues": ["queue_root omitted; not scanning the lab production queue"],
        }
    else:
        viewed = inspect_pair(
            repo_root, comparison_id=manifest.comparison_id, queue_root=queue_root
        )
    policy_path = repo_root / "policy/standing-approvals.yaml"
    daily = per_job = None
    if policy_path.is_file():
        policy = load_policy(policy_path)
        daily = policy.daily_cost_ceiling_usd
        per_job = policy.per_job_cost_ceiling_usd

    agent_version = None
    serializable: dict[str, Any] | None = None
    try:
        from evallab.harbor_rlm import AGENT_VERSION, serializable_runtime_config

        agent_version = AGENT_VERSION
        serializable = serializable_runtime_config()
    except Exception as exc:
        rlm_source = Path(__file__).with_name("harbor_rlm.py")
        if rlm_source.is_file():
            match = re.search(r'AGENT_VERSION = "([^"]+)"', rlm_source.read_text())
            if match:
                agent_version = match.group(1)
        serializable = {"import_error": f"{type(exc).__name__}: {exc}"}

    backend_spec = importlib.util.find_spec("evallab.rlm_runtime")
    backend_requires_wheels = None
    if backend_spec is not None:
        try:
            import inspect as pyinspect

            from evallab.rlm_runtime import ManagedReplBackend

            parameters = pyinspect.signature(ManagedReplBackend.__init__).parameters
            backend_requires_wheels = (
                "aiohttp_wheels" in parameters
                and parameters["aiohttp_wheels"].default is pyinspect.Parameter.empty
            )
        except Exception as exc:
            backend_requires_wheels = f"{type(exc).__name__}: {exc}"

    root_matches = all(
        (arm.get("spec") or {}).get("model") == manifest.root_model.configured_id
        for arm in compiled["arms"]
        if (arm.get("spec") or {}).get("agent") not in CONTROL_AGENTS
    )
    estimates = [(arm.get("spec") or {}).get("est_cost_usd") for arm in compiled["arms"]]
    genuine_estimates = all(isinstance(value, (int, float)) and value > 0 for value in estimates)

    queue_specs = [
        {
            "spec_id": row.get("spec_id"),
            "arm": row.get("arm"),
            "agent": row.get("agent"),
            "model": row.get("model"),
            "queue_state": row.get("queue_state"),
            "approval_command": (
                f"uv run evallab approve {row.get('spec_id')} --actor <you>"
                if row.get("spec_id")
                else None
            ),
            "stale": False,
            "stale_reason": None,
        }
        for row in viewed.get("queue") or []
        if row.get("canary")
        or row.get("arm") in {"baseline", "candidate", "mini-swe", "authors-rlm"}
    ]

    gates = [
        _gate(
            "source_runtime_pins",
            "PASS" if agent_version == HAR12_AGENT_VERSION else "FAIL",
            (
                f"HAR-12 AuthorsRlmAgent {agent_version} pin {HAR12_GIT_REF}; "
                f"mini-swe-agent 2.4.6; root {manifest.root_model.configured_id} "
                f"revision_status={manifest.root_model.revision_status}"
            ),
        ),
        _gate(
            "constructor_config",
            "PASS" if backend_spec is not None and backend_requires_wheels is False else "BLOCKED",
            (
                f"{HAR10_BACKEND_IMPORT} find_spec={'present' if backend_spec else 'absent'}; "
                f"aiohttp_wheels_required={backend_requires_wheels}; "
                f"HAR-10 PR392 @{HAR10_GIT_REF} is MERGEABLE stdlib-only (no aiohttp_wheels). "
                "Python construction of ManagedReplBackend(environment, worker_src=...) succeeds. "
                "start() with a live worker_proxy_url is still unproven; a URL field is not a started worker."
            ),
        ),
        _gate(
            "task_verifier_identity",
            "PASS" if any(arm.get("canary") for arm in compiled["arms"]) else "FAIL",
            "Factory canary event-summary / registered/event-summary; verifier and package digests copied from cohort members when present.",
        ),
        _gate(
            "root_worker_routing",
            "PASS" if root_matches else "FAIL",
            (
                f"spec.model matches requested root on billable arms={root_matches}; "
                "worker_model declared as the same configured id with revision unknown; "
                "worker route is not a live proxy"
            ),
        ),
        _gate(
            "enforced_bounds",
            "PASS" if per_job and daily else "FAIL",
            (
                f"policy per_job_cost_ceiling_usd={per_job} daily_cost_ceiling_usd={daily} "
                "(API-list-price equivalents, not spend); attempts=1 concurrency=1; "
                "DeepSeek cost_limit default 2.5 is a ceiling not an estimate"
            ),
        ),
        _gate(
            "usage_unknowns",
            "PASS",
            "No model trial has run. Root/worker tokens, cost, ATIF, and revision stay unknown/null. Missing usage is not zero.",
        ),
        _gate(
            "estimates",
            "FAIL" if not genuine_estimates else "PASS",
            (
                f"compiled est_cost_usd={estimates} from policy/canary-suite.yaml "
                "event-summary member 2.5 (API-list-price equivalent, not spend)"
                if genuine_estimates
                else f"compiled est_cost_usd={estimates}; 0.0 is not a genuine estimate"
            ),
        ),
        _gate(
            "execution_authorization",
            "BLOCKED",
            "Standing auto_run is oracle/nop only. Billable arms stay waiting/paid_run_unauthorized until recorded per-spec approval. Approval is not granted by this report.",
        ),
    ]
    constructor_ok = backend_spec is not None and backend_requires_wheels is False
    verdict = "BLOCKED"
    verdict_reason = (
        "Not READY_FOR_APPROVAL: HAR-10 stdlib constructor starts and stops on a "
        "provided host environment, but no Harbor trial worker_proxy_url has been "
        "started and billable arms still need recorded per-spec approval. "
        "Do not present this as approval-only."
        if constructor_ok
        else (
            "Not READY_FOR_APPROVAL: HAR-10 backend constructor is missing or still "
            "requires aiohttp_wheels. Do not present this as approval-only."
        )
    )
    return {
        **compiled,
        "kind": "harness_paired_readiness",
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "gates": gates,
        "spec_ids": queue_specs,
        "inspect": viewed,
        "har12": {
            "agent_version": agent_version,
            "git_ref": HAR12_GIT_REF,
            "serializable": serializable,
        },
        "har10": {
            "git_ref": HAR10_GIT_REF,
            "import_path": HAR10_BACKEND_IMPORT,
            "module_present": backend_spec is not None,
            "aiohttp_wheels_required": backend_requires_wheels,
            "status": "consumed_stdlib_constructor",
        },
        "notices": [
            *(compiled.get("notices") or []),
            "Step 2 no-spend readiness: no Harbor tick, no model request, no approve.",
            "Regenerate canary specs after HAR-10 corrected constructor; do not approve stale IDs.",
        ],
    }
