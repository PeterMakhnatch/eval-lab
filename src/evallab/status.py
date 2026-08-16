"""Read-only operator snapshot of completed Harbor evidence and lab stores."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from evallab.facts import experiment_id
from evallab.paths import derived_root_from_environment
from evallab.queue import QUEUE_STATES
from evallab.results import discover_job_dirs, load_job
from evallab.runner import database_url_from_environment
from evallab.schemas import (
    ANALYSIS_REVIEWS_DIRNAME,
    ANALYSIS_SIDECAR_FILENAME,
    ExperimentSpec,
    TrialAnalysisSidecar,
)

Availability = Literal["observed", "unavailable", "draft", "review-needed"]
SECTION_KEYS = ("Recent", "Now", "Next", "Tasks", "Health", "Analysis")
PHOENIX_HOST = "127.0.0.1"
PHOENIX_PORT = 6006

BooleanProbe = Callable[[], bool]


class StatusItem(BaseModel):
    availability: Availability
    label: str
    detail: str | None = None
    kind: str | None = None
    experiment_id: str | None = None
    job_id: str | None = None
    trial_id: str | None = None
    trajectory_present: bool | None = None
    analysis_id: str | None = None
    exception_class: str | None = None
    scored_as_model_failure: bool | None = None
    provenance: dict[str, Any] | None = None


class StatusSection(BaseModel):
    availability: Availability
    items: list[StatusItem] = Field(default_factory=list)


class StatusSnapshot(BaseModel):
    generated_at: datetime
    Recent: StatusSection
    Now: StatusSection
    Next: StatusSection
    Tasks: StatusSection
    Health: StatusSection
    Analysis: StatusSection

    def section_map(self) -> dict[str, StatusSection]:
        return {key: getattr(self, key) for key in SECTION_KEYS}


class StatusLayout(BaseModel):
    root: Path
    queue_root: Path
    job_roots: list[Path]
    analysis_roots: list[Path]
    parquet_root: Path
    scratch: bool


def resolve_status_layout(root: Path) -> StatusLayout:
    resolved = root.resolve()
    scratch = (resolved / "jobs").is_dir() and not (resolved / "src" / "evallab").is_dir()
    if scratch:
        return StatusLayout(
            root=resolved,
            queue_root=resolved / "queue",
            job_roots=[resolved / "jobs"],
            analysis_roots=[resolved / "analyses"],
            parquet_root=resolved / "parquet",
            scratch=True,
        )
    return StatusLayout(
        root=resolved,
        queue_root=resolved / "queue",
        job_roots=[resolved / "runs", resolved / "research" / "evidence" / "runs"],
        analysis_roots=[resolved / "derived" / "analyses"],
        parquet_root=derived_root_from_environment(resolved),
        scratch=False,
    )


def probe_postgres(url: str | None = None) -> bool:
    from evallab import database

    try:
        target = url or database_url_from_environment()
        database.ping(target)
    except Exception:
        return False
    return True


def probe_phoenix(host: str = PHOENIX_HOST, port: int = PHOENIX_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _availability_for_presence(present: bool) -> Availability:
    return "observed" if present else "unavailable"


def _exception_class(result: dict[str, Any]) -> str | None:
    exception = result.get("exception_info")
    if not isinstance(exception, dict):
        return None
    value = exception.get("exception_type")
    return str(value) if value else None


def _trajectory_present(trial_dir: Path) -> bool:
    return (trial_dir / "agent" / "trajectory.json").is_file()


def _safe_load_spec(path: Path) -> tuple[ExperimentSpec | None, str | None]:
    try:
        return ExperimentSpec.model_validate_json(path.read_text()), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _queue_entries(
    queue_root: Path,
) -> dict[str, list[tuple[Path, ExperimentSpec | None, str | None]]]:
    entries: dict[str, list[tuple[Path, ExperimentSpec | None, str | None]]] = {
        state: [] for state in QUEUE_STATES
    }
    if not queue_root.is_dir():
        return entries
    for state in QUEUE_STATES:
        state_dir = queue_root / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("*.json")):
            spec, error = _safe_load_spec(path)
            entries[state].append((path, spec, error))
    return entries


def _load_jobs(job_roots: Iterable[Path]) -> tuple[list[Any], list[StatusItem]]:
    jobs = []
    malformed: list[StatusItem] = []
    seen: set[Path] = set()
    for raw_root in job_roots:
        root = raw_root.expanduser()
        if not root.exists():
            continue
        for path in discover_job_dirs([root]):
            if path in seen:
                continue
            seen.add(path)
            try:
                jobs.append(load_job(path))
            except Exception as exc:
                malformed.append(
                    StatusItem(
                        availability="review-needed",
                        label=path.name,
                        detail=f"{type(exc).__name__}: {exc}",
                        kind="malformed-job",
                    )
                )
        for result_path in root.rglob("result.json"):
            candidate = result_path.parent
            if candidate in seen:
                continue
            try:
                payload = json.loads(result_path.read_text())
            except Exception as exc:
                if candidate.name.startswith("."):
                    continue
                malformed.append(
                    StatusItem(
                        availability="review-needed",
                        label=candidate.name,
                        detail=f"{type(exc).__name__}: {exc}",
                        kind="malformed-result",
                    )
                )
                seen.add(candidate)
                continue
            if not isinstance(payload, dict):
                continue
            if "n_total_trials" in payload and "stats" in payload:
                seen.add(candidate)
                malformed.append(
                    StatusItem(
                        availability="review-needed",
                        label=candidate.name,
                        detail="job result.json is present but is not a completed Harbor job",
                        kind="malformed-job",
                    )
                )
    jobs.sort(key=lambda job: str(job.result.get("finished_at") or ""), reverse=True)
    return jobs, malformed


def _load_analyses(
    analysis_roots: Iterable[Path],
) -> tuple[list[tuple[Path, TrialAnalysisSidecar]], list[StatusItem]]:
    loaded: list[tuple[Path, TrialAnalysisSidecar]] = []
    malformed: list[StatusItem] = []
    for raw_root in analysis_roots:
        root = raw_root.expanduser()
        if not root.exists():
            continue
        for path in sorted(root.rglob(ANALYSIS_SIDECAR_FILENAME)):
            try:
                sidecar = TrialAnalysisSidecar.model_validate_json(path.read_text())
            except Exception as exc:
                malformed.append(
                    StatusItem(
                        availability="review-needed",
                        label=path.parent.name,
                        detail=f"{type(exc).__name__}: {exc}",
                        kind="malformed-analysis",
                    )
                )
                continue
            loaded.append((path, sidecar))
    return loaded, malformed


def _analysis_item(path: Path, sidecar: TrialAnalysisSidecar) -> StatusItem:
    reviews = path.parent / ANALYSIS_REVIEWS_DIRNAME
    reviewed = reviews.is_dir() and any(reviews.glob("*.json"))
    if sidecar.validation_status != "valid":
        availability: Availability = "review-needed"
    elif reviewed:
        availability = "observed"
    else:
        availability = "draft"
    provenance = sidecar.analysis_provenance.model_dump(mode="json")
    return StatusItem(
        availability=availability,
        label=str(sidecar.analysis_id),
        detail=sidecar.output.summary,
        kind="analysis",
        experiment_id=sidecar.experiment_id,
        job_id=str(sidecar.job_id),
        trial_id=str(sidecar.source_trial_id),
        analysis_id=str(sidecar.analysis_id),
        provenance=provenance,
    )


def _recent_items(
    jobs: list[Any],
    analyses: list[tuple[Path, TrialAnalysisSidecar]],
    malformed_jobs: list[StatusItem],
) -> list[StatusItem]:
    analysis_by_trial = {str(sidecar.source_trial_id): sidecar for _, sidecar in analyses}
    items: list[StatusItem] = []
    for job in jobs:
        exp = experiment_id(job)
        for trial in job.trials:
            exception = _exception_class(trial.result)
            sidecar = analysis_by_trial.get(trial.id)
            items.append(
                StatusItem(
                    availability="observed",
                    label=f"{job.name}/{trial.name}",
                    detail=(
                        f"harness exception {exception}"
                        if exception
                        else f"reward={trial.primary_reward}"
                    ),
                    kind="trial",
                    experiment_id=exp,
                    job_id=job.id,
                    trial_id=trial.id,
                    trajectory_present=_trajectory_present(trial.path),
                    analysis_id=str(sidecar.analysis_id) if sidecar else None,
                    exception_class=exception,
                    scored_as_model_failure=exception is None
                    and trial.primary_reward is not None
                    and trial.primary_reward < 1.0,
                )
            )
    items.extend(malformed_jobs)
    return items


def _queue_items(
    entries: dict[str, list[tuple[Path, ExperimentSpec | None, str | None]]],
    states: tuple[str, ...],
    *,
    empty_label: str,
    kind: str,
) -> tuple[Availability, list[StatusItem]]:
    items: list[StatusItem] = []
    for state in states:
        for path, spec, error in entries.get(state, []):
            if error or spec is None:
                items.append(
                    StatusItem(
                        availability="review-needed",
                        label=path.name,
                        detail=error,
                        kind="malformed-spec",
                    )
                )
                continue
            items.append(
                StatusItem(
                    availability="observed",
                    label=spec.name,
                    detail=f"{state} {spec.task} agent={spec.agent}",
                    kind=kind,
                    experiment_id=spec.spec_id,
                )
            )
    if items:
        if any(item.availability == "review-needed" for item in items):
            return "review-needed", items
        return "observed", items
    return "observed", [
        StatusItem(availability="observed", label=empty_label, detail=None, kind=kind)
    ]


def _task_items(
    jobs: list[Any],
    entries: dict[str, list[tuple[Path, ExperimentSpec | None, str | None]]],
) -> list[StatusItem]:
    seen: dict[str, StatusItem] = {}
    for job in jobs:
        for trial in job.trials:
            name = str(trial.result.get("task_name") or job.name)
            exception = _exception_class(trial.result)
            seen.setdefault(
                name,
                StatusItem(
                    availability="observed",
                    label=name,
                    detail=(
                        "harness exception excluded from model-failure denominator"
                        if exception
                        else "completed trial"
                    ),
                    kind="task",
                    job_id=job.id,
                    trial_id=trial.id,
                    exception_class=exception,
                    scored_as_model_failure=False if exception else None,
                ),
            )
    for state in QUEUE_STATES:
        for _path, spec, error in entries.get(state, []):
            if spec is None or error:
                continue
            seen.setdefault(
                spec.task,
                StatusItem(
                    availability="observed",
                    label=spec.task,
                    detail=f"queued as {state}",
                    kind="task",
                    experiment_id=spec.spec_id,
                ),
            )
    return list(seen.values()) or [
        StatusItem(
            availability="unavailable",
            label="no tasks observed",
            detail="no completed jobs or queue specs",
            kind="task",
        )
    ]


def _health_items(
    layout: StatusLayout,
    *,
    postgres_ok: bool,
    phoenix_ok: bool,
    queue_present: bool,
) -> list[StatusItem]:
    parquet_present = False
    if layout.parquet_root.exists():
        parquet_present = any(layout.parquet_root.glob("**/*.parquet"))
    return [
        StatusItem(
            availability=_availability_for_presence(postgres_ok),
            label="postgres",
            detail="reachable" if postgres_ok else "catalog unavailable",
            kind="store",
        ),
        StatusItem(
            availability=_availability_for_presence(phoenix_ok),
            label="phoenix",
            detail="reachable" if phoenix_ok else "trace UI unavailable",
            kind="store",
        ),
        StatusItem(
            availability=_availability_for_presence(queue_present),
            label="queue",
            detail=str(layout.queue_root),
            kind="store",
        ),
        StatusItem(
            availability=_availability_for_presence(parquet_present),
            label="parquet",
            detail=str(layout.parquet_root),
            kind="store",
        ),
    ]


def build_status_snapshot(
    root: Path,
    *,
    postgres_probe: BooleanProbe | None = None,
    phoenix_probe: BooleanProbe | None = None,
    postgres_url: str | None = None,
    generated_at: datetime | None = None,
) -> StatusSnapshot:
    """Pure reader. Never creates directories, never writes files."""

    layout = resolve_status_layout(root)
    lab_checkout = (layout.root / "src" / "evallab").is_dir() or (
        layout.root / "AGENTS.md"
    ).is_file()
    if postgres_probe is not None:
        postgres_ok = postgres_probe()
    elif lab_checkout:
        postgres_ok = probe_postgres(postgres_url)
    else:
        postgres_ok = False
    if phoenix_probe is not None:
        phoenix_ok = phoenix_probe()
    elif lab_checkout:
        phoenix_ok = probe_phoenix()
    else:
        phoenix_ok = False
    queue_present = layout.queue_root.is_dir()
    entries = _queue_entries(layout.queue_root)
    jobs, malformed_jobs = _load_jobs(layout.job_roots)
    analyses, malformed_analyses = _load_analyses(layout.analysis_roots)

    recent_items = _recent_items(jobs, analyses, malformed_jobs)
    now_availability, now_items = _queue_items(
        entries,
        ("approved", "running"),
        empty_label="no approved or running work",
        kind="current",
    )
    next_availability, next_items = _queue_items(
        entries,
        ("waiting", "pending", "proposed"),
        empty_label="no waiting work",
        kind="next",
    )
    task_items = _task_items(jobs, entries)
    health_items = _health_items(
        layout,
        postgres_ok=postgres_ok,
        phoenix_ok=phoenix_ok,
        queue_present=queue_present,
    )
    analysis_items = [_analysis_item(path, sidecar) for path, sidecar in analyses]
    analysis_items.extend(malformed_analyses)
    if not analysis_items:
        analysis_items = [
            StatusItem(
                availability="unavailable",
                label="no saved analysis",
                detail="no stage-5 sidecar under analysis roots",
                kind="analysis",
            )
        ]

    def _section(items: list[StatusItem], *, empty: Availability = "unavailable") -> StatusSection:
        if not items:
            return StatusSection(availability=empty, items=[])
        if any(item.availability == "review-needed" for item in items):
            availability: Availability = "review-needed"
        elif all(item.availability == "unavailable" for item in items):
            availability = "unavailable"
        elif any(item.availability == "draft" for item in items):
            availability = "draft"
        else:
            availability = "observed"
        return StatusSection(availability=availability, items=items)

    return StatusSnapshot(
        generated_at=generated_at or datetime.now(UTC),
        Recent=_section(recent_items, empty="unavailable"),
        Now=StatusSection(availability=now_availability, items=now_items),
        Next=StatusSection(availability=next_availability, items=next_items),
        Tasks=_section(task_items, empty="unavailable"),
        Health=_section(health_items),
        Analysis=_section(analysis_items, empty="unavailable"),
    )


def snapshot_as_dict(snapshot: StatusSnapshot) -> dict[str, Any]:
    return snapshot.model_dump(mode="json")


def render_status_text(snapshot: StatusSnapshot) -> str:
    lines = ["Eval Lab status", f"generated_at: {snapshot.generated_at.isoformat()}", ""]
    for key in SECTION_KEYS:
        section: StatusSection = getattr(snapshot, key)
        lines.append(f"{key} [{section.availability}]")
        if not section.items:
            lines.append("  (empty)")
            continue
        for item in section.items:
            detail = f" — {item.detail}" if item.detail else ""
            lines.append(f"  [{item.availability}] {item.label}{detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def iter_labeled_items(snapshot: StatusSnapshot) -> list[StatusItem]:
    items: list[StatusItem] = []
    for section in snapshot.section_map().values():
        items.extend(section.items)
    return items
