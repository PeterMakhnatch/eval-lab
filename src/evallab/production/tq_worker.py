"""Durable Task Quality consumption and execution-backed live findings.

``task_quality.audit_task`` jobs name ``task_package``, ``repaired_task`` and
``failing_runs``/``passing_runs`` (retained Harbor trial directories). A repair
job also names ``exploit_run`` or one passing run; a Seeker's immutable
``exploit_files``/``solution_bundle`` can instead supply its replay input.
Relative paths resolve against the worker's repository, never the process cwd.

``task_quality.annotation_task`` jobs carry normalized human severity/rationale
(for example the public SWE-bench annotation loader's ``payload.annotation``).
Each job records a provisional ``qa.finding/v1`` preserving the human
assessment; ``execution_available`` marks whether measured execution evidence
backs the emitted finding. A declared ``task_dir`` (or ``task_package``) with a
``repaired_task`` directory and retained runs or an immutable candidate feeds
the same differential machinery as an audit job — only measured executions
upgrade the annotation finding to confirmed/refuted. A ``task_dir`` alone is
not confirmation: missing prerequisites and unusable executions remain
provisional. Annotation jobs never run repair qualification, so they never
emit ``asset.verified``.

The existing QA static scan/bounded reader bundle produce advisory findings.
Only fresh execution of the *same* submission against both pinned packages can
confirm or refute a differential hypothesis. Missing/failed execution remains
provisional, not a refutation. Repair qualification uses nine independent fresh
executions, not cached scores or reward claims supplied in the job. The durable
queue alone owns chaining a qualified repair into a Seeker re-break job.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from evallab.execution_backend import ExecutionBackendConfig
from evallab.qa import counterfactual
from evallab.qa import task as qa_task
from evallab.qa.models import (
    Finding,
    FindingSubject,
    Refutation,
    Reproducer,
    Reviewer,
    Severity,
)
from evallab.registry import task_directory_digest

#: Queue job type carrying normalized human task-quality annotations. Kept in
#: sync with ``evallab.production.public_corpus_loader.ANNOTATION_JOB``.
ANNOTATION_JOB = "task_quality.annotation_task"
JOB_TYPES = ("task_quality.audit_task", "task_quality.repair_task", ANNOTATION_JOB)
Direction = Literal["unfair_fail", "wrong_pass"]


class WorkQueue(Protocol):
    """The synchronous ContinuousWorkQueue surface used by this consumer."""

    def claim(
        self, worker_id: str, *, job_types: Sequence[str], lease_seconds: int = 60
    ) -> dict[str, Any] | None: ...

    def renew(self, event_id: str, *, lease_token: str, lease_seconds: int = 60) -> None: ...

    def ack(self, event_id: str, *, lease_token: str) -> None: ...

    def release(self, event_id: str, *, lease_token: str) -> None: ...

    def append_event(
        self, event: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Job:
    """A claimed job's resolved inputs.

    Audit/repair jobs always resolve both pinned packages. Annotation jobs may
    resolve none (annotation-only), one, or both — ``None`` marks a missing
    execution prerequisite, never a fabricated package.
    """

    event_id: str
    run_id: str
    payload: dict[str, Any]
    original: Path | None
    repaired: Path | None
    original_digest: str | None
    repaired_digest: str | None
    task_id: str
    lease_token: str | None = None


#: Human-annotation values that assert *no* defect; anything else on a mapped
#: axis supports the unfair-failure hypothesis.
_ANNOTATION_NEGATIVE = {"", "none", "no", "fair", "clean", "false", "negative"}

#: Human severity words mapped onto the finding vocabulary; the raw values stay
#: verbatim under ``payload.annotation``.
_ANNOTATION_SEVERITY: dict[str, Severity] = {
    "critical": "critical",
    "severe": "critical",
    "major": "high",
    "high": "high",
    "moderate": "medium",
    "medium": "medium",
    "minor": "medium",
    "low": "low",
    "trivial": "low",
    "none": "low",
}
_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _annotation_indicates(annotation: Mapping[str, Any], *keys: str) -> bool:
    """True when any named axis carries a defect-asserting human value."""
    for key in keys:
        value = annotation.get(key)
        if value is None or value is False:
            continue
        if isinstance(value, str):
            if value.strip().lower() in _ANNOTATION_NEGATIVE:
                continue
        elif isinstance(value, int | float) and not isinstance(value, bool) and value <= 0:
            continue
        return True
    return False


#: Scalar human axes an annotation can assert or explicitly negate.
_ANNOTATION_AXES = ("underspecified", "unfair_tests", "severity", "max_severity")


def _annotation_severity(annotation: Mapping[str, Any]) -> Severity | None:
    """The highest mapped human severity, or ``None`` when unmappable/absent."""
    best: Severity | None = None
    for key in _ANNOTATION_AXES:
        value = annotation.get(key)
        if not isinstance(value, str):
            continue
        mapped = _ANNOTATION_SEVERITY.get(value.strip().lower())
        if mapped is not None and (best is None or _SEVERITY_RANK[mapped] > _SEVERITY_RANK[best]):
            best = mapped
    return best


def _annotation_axes_negative(annotation: Mapping[str, Any]) -> bool:
    """True when explicit human axes exist and every one denies a defect."""
    return any(
        annotation.get(key) is not None for key in _ANNOTATION_AXES
    ) and not _annotation_indicates(annotation, *_ANNOTATION_AXES)


def _annotation_summary(direction: Direction, annotation: Mapping[str, Any]) -> str:
    """Human words first; an all-negative assessment never reads as a defect."""
    for key in ("comment", "rationale", "reason", "explanation"):
        text = annotation.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    if _annotation_axes_negative(annotation):
        return "Human annotation asserts no task defect"
    return (
        "Human-annotated possible unfair failure"
        if direction == "unfair_fail"
        else "Human-annotated possible wrong pass"
    )


class TaskQualityLiveWorker:
    """A continuously runnable, model-free-by-default Task Quality consumer.

    ``backend`` selects ``docker``, ``modal`` or ``auto``; an injected QA backend
    remains supported for model-free tests. ``execution_config`` carries one
    worker-owned cloud approval, ledger, cap and attribution across every replay
    and repair control. Queue payloads cannot override that policy.
    ``replay_runner`` and ``candidate_runner`` replace the canonical executors in
    tests; an optional read-only ``reader`` can refine static explanations but
    cannot manufacture confirmed evidence.
    """

    def __init__(
        self,
        queue: WorkQueue,
        *,
        repo_root: Path | str,
        worker_id: str = "task-quality-live",
        backend: Any = None,
        execution_config: ExecutionBackendConfig | None = None,
        modal_app_name: str | None = None,
        spend_ledger: Path | str | None = None,
        approval: str | None = None,
        max_usd: float | None = None,
        billing_tag: str | None = None,
        allow_local_docker: bool | None = None,
        replay_runner: Callable[..., dict[str, Any]] | None = None,
        candidate_runner: Callable[..., dict[str, Any]] | None = None,
        reader: Callable[[Finding, dict[str, Any]], Finding] | None = None,
        trial_timeout_seconds: int = 600,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.queue = queue
        self.repo_root = Path(repo_root).resolve()
        self.worker_id = worker_id
        self.execution_config = execution_config or ExecutionBackendConfig(
            backend=backend if isinstance(backend, str) else "docker",
            modal_app_name=modal_app_name,
            spend_ledger=spend_ledger,
            approval=approval,
            max_usd=max_usd,
            billing_tag=billing_tag,
            **({"allow_local_docker": allow_local_docker} if allow_local_docker is not None else {}),
        )
        if allow_local_docker is not None:
            self.execution_config = replace(
                self.execution_config,
                allow_local_docker=self.execution_config.allow_local_docker and allow_local_docker,
            )
        self._injected_backend = backend is not None and not isinstance(backend, str)
        self.backend = backend if self._injected_backend else qa_task.HarborBackend(
            repo_root=self.repo_root, allow_local_docker=self.execution_config.allow_local_docker
        )
        self._replay_spend_gate: Any = None
        self.replay_runner = replay_runner or counterfactual.replay
        self.candidate_runner = candidate_runner
        self.reader = reader
        self.trial_timeout_seconds = trial_timeout_seconds
        # Counterfactual's native matrix timeout is 3600s. Renew before every
        # execution with enough lease for either executor plus collection.
        self.lease_seconds = max(7200, trial_timeout_seconds * 2)
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def _spend_gate(self, usd_per_execution: float) -> Any:
        from evallab.regrade import CloudSpendGate

        config = self.execution_config
        if config.spend_ledger is None:
            raise ValueError("Modal worker execution requires a persisted spend_ledger")
        return CloudSpendGate(
            approval=config.approval or "",
            cap_usd=config.max_usd or 0.0,
            usd_per_execution=usd_per_execution,
            spend_ledger=config.spend_ledger,
            modal_app_name=config.modal_app_name,
            billing_tag=config.billing_tag,
        )

    def _qa_backend(self) -> Any:
        """Select repair controls lazily; annotation-only work needs no runtime."""
        selected = self.execution_config.require_backend()
        if not self._injected_backend and selected == "modal" and self.backend.environment != "modal":
            from evallab.qa.task_modal import ModalBackend
            from evallab.regrade import MODAL_USD_PER_ALLOCATION_UNIT

            self.backend = ModalBackend(
                repo_root=self.repo_root,
                spend_gate=self._spend_gate(MODAL_USD_PER_ALLOCATION_UNIT),
                modal_app_name=self.execution_config.modal_app_name,
                billing_tag=self.execution_config.billing_tag,
            )
        return self.backend

    def _path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.repo_root / path).resolve()

    def _prepare(self, envelope: Mapping[str, Any]) -> _Job:
        if envelope["event_type"] not in JOB_TYPES:
            raise ValueError("Task Quality cannot consume another system's job")
        payload = dict(envelope["payload"])
        if envelope["event_type"] == ANNOTATION_JOB:
            return self._prepare_annotation(envelope, payload)
        original = self._path(payload["task_package"])
        repaired = self._path(payload["repaired_task"])
        task = self.backend.resolve(original)
        original_digest = task_directory_digest(original)
        repaired_digest = task_directory_digest(repaired)
        for name, actual in (
            ("task_package_digest", original_digest),
            ("repaired_task_digest", repaired_digest),
        ):
            if payload.get(name) is not None and payload[name] != actual:
                raise ValueError(f"{name} no longer matches the queued package")
        return _Job(
            event_id=str(envelope["event_id"]),
            run_id=str(envelope["run_id"]),
            payload=payload,
            original=original,
            repaired=repaired,
            original_digest=original_digest,
            repaired_digest=repaired_digest,
            task_id=str(payload.get("task_id") or getattr(task, "task_name", None) or task.task_id),
            lease_token=envelope.get("lease_token"),
        )

    def _existing_dir(self, value: Any) -> Path | None:
        """Resolve a declared package locator only when it names a real directory."""
        if value is None:
            return None
        path = self._path(str(value))
        return path if path.is_dir() else None

    def _prepare_annotation(
        self, envelope: Mapping[str, Any], payload: dict[str, Any]
    ) -> _Job:
        """Resolve an annotation job's inputs; absent locators stay ``None``.

        ``task_dir`` supplies the runnable package when ``task_package`` is not
        declared. Declared-but-missing directories are missing prerequisites,
        not failures and never fabricated packages. Declared digests pin
        whichever directories do resolve.
        """
        original = self._existing_dir(payload.get("task_package") or payload.get("task_dir"))
        repaired = self._existing_dir(payload.get("repaired_task"))
        task = self.backend.resolve(original) if original is not None else None
        original_digest = task_directory_digest(original) if original is not None else None
        repaired_digest = task_directory_digest(repaired) if repaired is not None else None
        checks: list[tuple[str, str]] = []
        if original_digest is not None:
            checks.append(("task_package_digest", original_digest))
            checks.append(("task_dir_digest", original_digest))
        if repaired_digest is not None:
            checks.append(("repaired_task_digest", repaired_digest))
        for name, actual in checks:
            if payload.get(name) is not None and payload[name] != actual:
                raise ValueError(f"{name} no longer matches the queued package")
        task_id = str(
            payload.get("task_id")
            or (getattr(task, "task_name", None) if task is not None else None)
            or (task.task_id if task is not None else None)
            or payload.get("item_id")
            or payload.get("source_id")
            or envelope["event_id"]
        )
        return _Job(
            event_id=str(envelope["event_id"]),
            run_id=str(envelope["run_id"]),
            payload=payload,
            original=original,
            repaired=repaired,
            original_digest=original_digest,
            repaired_digest=repaired_digest,
            task_id=task_id,
            lease_token=envelope.get("lease_token"),
        )

    def _unchanged(self, job: _Job) -> None:
        if (
            job.original is not None
            and task_directory_digest(job.original) != job.original_digest
        ) or (
            job.repaired is not None
            and task_directory_digest(job.repaired) != job.repaired_digest
        ):
            raise ValueError("task package changed during Task Quality execution")

    def _renew(self, job: _Job) -> None:
        if job.lease_token is not None:
            self.queue.renew(
                job.event_id, lease_token=job.lease_token, lease_seconds=self.lease_seconds
            )

    def _emit(
        self,
        job: _Job,
        event_type: str,
        status: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        context = {key: job.payload[key] for key in ("dataset_id", "item_id") if key in job.payload}
        return self.queue.append_event(
            {
                "event_id": str(uuid.uuid4()),
                "run_id": job.run_id,
                "system": "task_quality",
                "event_type": event_type,
                "seq": 0,
                "ts": self.clock(),
                "prefix_step": None,
                "status": status,
                "payload": {**context, **payload, "job_id": job.event_id, "status": status},
            },
            idempotency_key=idempotency_key,
        )

    def _finding_event(
        self, job: _Job, finding: Finding, status: str, phase: str, **evidence: Any
    ) -> dict[str, Any]:
        return self._emit(
            job,
            "qa.finding/v1",
            status,
            {
                "finding_id": finding.id,
                "summary": finding.summary,
                "severity": finding.severity,
                "phase": phase,
                "finding": finding.model_dump(mode="json", by_alias=True),
                **evidence,
            },
        )

    def _sources(self, job: _Job) -> list[tuple[Direction, Path | None]]:
        sources: list[tuple[Direction, Path | None]] = []
        for direction, key in (("unfair_fail", "failing_runs"), ("wrong_pass", "passing_runs")):
            paths = job.payload.get(key, [])
            if isinstance(paths, str | Path) or not isinstance(paths, Sequence):
                raise ValueError(f"{key} must be a list of retained trial paths")
            sources.extend((direction, self._path(path)) for path in paths)
        if not any(direction == "wrong_pass" for direction, _ in sources):
            if job.payload.get("exploit_run"):
                sources.append(("wrong_pass", self._path(job.payload["exploit_run"])))
            elif self._has_candidate(job):
                sources.append(("wrong_pass", None))
        return list(dict.fromkeys(sources))

    @staticmethod
    def _has_candidate(job: _Job) -> bool:
        return (
            job.payload.get("exploit_files") is not None
            or job.payload.get("solution_bundle") is not None
        )

    def _candidate_source(self, job: _Job, source: Path | None) -> bool:
        if not self._has_candidate(job):
            return False
        exploit = job.payload.get("exploit_run")
        if exploit is not None:
            return source == self._path(exploit)
        return source is None or len(job.payload.get("passing_runs", [])) == 1

    def _input_digest(self, job: _Job, source: Path | None, direction: Direction) -> str:
        if direction != "wrong_pass" or not self._candidate_source(job, source):
            if source is None:
                raise ValueError("missing replay source")
            return counterfactual.source_digest(source)
        bundle = job.payload.get("solution_bundle")
        if bundle is not None:
            return task_directory_digest(self._path(bundle))
        digest = hashlib.sha256()
        for path, content in sorted(job.payload["exploit_files"].items()):
            raw = content.encode("utf-8") if isinstance(content, str) else content
            digest.update(path.encode("utf-8") + b"\0")
            digest.update(str(len(raw)).encode("ascii") + b"\0" + raw)
        return digest.hexdigest()

    def _execute(
        self,
        job: _Job,
        source: Path | None,
        package: Path,
        *,
        direction: Direction,
        label: str,
    ) -> dict[str, Any]:
        self._renew(job)
        selected = self.execution_config.require_backend()
        self._unchanged(job)
        work = self.repo_root / "runs" / "task-quality-live" / uuid.uuid4().hex / label
        digest = job.original_digest if package == job.original else job.repaired_digest
        if digest is None:
            raise ValueError("execution package lost its pinned digest")
        if direction == "wrong_pass" and self._candidate_source(job, source):
            from evallab.candidate_runner import run_candidate

            kwargs: dict[str, Any] = {
                **self.execution_config.candidate_kwargs(),
                "timeout_seconds": self.trial_timeout_seconds,
                "output_dir": work,
                "target_dir": job.payload.get("target_dir"),
            }
            bundle = job.payload.get("solution_bundle")
            if bundle is not None:
                kwargs["solution_bundle"] = self._path(bundle)
            else:
                kwargs["files"] = job.payload["exploit_files"]
            raw = (self.candidate_runner or run_candidate)(package, **kwargs)
            reward = qa_task.finite_reward(raw.get("reward"))
            usable = (
                raw.get("status") == "measured"
                and raw.get("executed") is True
                and not raw.get("error")
            )
            ref = raw.get("output_dir")
        else:
            if source is None:
                raise ValueError(
                    "replay requires retained trial evidence or immutable candidate input"
                )
            grader = counterfactual.Grader(
                job.task_id, "task-quality-live", digest, package, job.event_id
            )
            replay_kwargs: dict[str, Any] = {
                "repo_root": self.repo_root,
                "environment": "modal" if selected == "modal" else "docker",
            }
            if selected == "modal":
                if self._replay_spend_gate is None:
                    self._replay_spend_gate = self._spend_gate(counterfactual.CLOUD_USD_PER_EXECUTION)
                replay_kwargs["spend_gate"] = self._replay_spend_gate
            raw = self.replay_runner(source, grader, work, **replay_kwargs)
            reward = qa_task.finite_reward(raw.get("regraded"))
            usable = (
                not raw.get("refusals")
                and not raw.get("execution_error")
                and raw.get("outcome") != "refused"
            )
            ref = raw.get("job_dir") or raw.get("regrade_dir") or raw.get("matrix_receipt")
        measured = bool(usable and reward is not None and isinstance(ref, str) and ref)
        return {
            "reward": reward if measured else None,
            "measured": measured,
            "evidence_ref": ref,
            "execution": raw,
        }

    def _run_differential(
        self,
        job: _Job,
        direction: Direction,
        source: Path | None,
        finding: Finding,
        input_digest: str,
    ) -> tuple[Finding, str, bool, dict[str, Any]]:
        """Replay one candidate against both pinned packages.

        Returns the execution-updated finding, its event status, whether both
        sides measured independently, and the raw execution evidence. Only a
        measured ``expected`` pair confirms; any other measured pair refutes;
        anything else stays provisional.
        """
        if job.original is None or job.repaired is None:
            raise ValueError("differential replay needs both resolved packages")
        original = self._execute(
            job, source, job.original, direction=direction, label="original"
        )
        repaired = self._execute(
            job, source, job.repaired, direction=direction, label="repaired"
        )
        if self._input_digest(job, source, direction) != input_digest:
            raise ValueError("submission changed during differential replay")
        measured = bool(
            original["measured"]
            and repaired["measured"]
            and self._path(original["evidence_ref"]) != self._path(repaired["evidence_ref"])
        )
        expected = (0.0, 1.0) if direction == "unfair_fail" else (1.0, 0.0)
        confirmed = measured and (original["reward"], repaired["reward"]) == expected
        status = "confirmed" if confirmed else "refuted" if measured else "provisional"
        finding = finding.model_copy(
            update={
                "stage": "differential_replay",
                "verdict": "fail" if confirmed else "pass" if measured else "unknown",
                "measurement": "measured" if measured else "ran_without_usable_result",
                "evidence_refs": finding.evidence_refs
                + [r["evidence_ref"] for r in (original, repaired) if r["evidence_ref"]],
                "refutation": Refutation(
                    status="verified" if confirmed else "refuted" if measured else "unverified",
                    by="task-quality-live/differential",
                    reason=f"original={original['reward']!r}, repaired={repaired['reward']!r}; expected {expected}"
                    if measured
                    else "Differential execution unavailable; hypothesis remains provisional",
                ),
            }
        )
        self._unchanged(job)
        return (
            finding,
            status,
            measured,
            {"direction": direction, "original": original, "repaired": repaired},
        )

    def _annotation_sources(
        self, job: _Job, annotation: Mapping[str, Any]
    ) -> list[tuple[Direction, Path | None]]:
        """Candidate hypotheses for an annotation job.

        Every supplied replay input keeps its direction. The human assessment
        itself is an ``unfair_fail`` hypothesis (underspecified/unfair tests)
        even when it supplies no retained run, so annotation-only jobs still
        produce one provisional finding.
        """
        sources = self._sources(job)
        if not any(direction == "unfair_fail" for direction, _ in sources) and (
            not sources or _annotation_indicates(annotation, "underspecified", "unfair_tests")
        ):
            sources.append(("unfair_fail", None))
        return sources

    def _annotation_runnable(
        self, job: _Job, direction: Direction, source: Path | None
    ) -> bool:
        """True when the job's resolved inputs can drive real execution."""
        if job.original is None or job.repaired is None:
            return False
        if source is not None:
            return True
        return direction == "wrong_pass" and self._candidate_source(job, source)

    @staticmethod
    def _missing_prerequisites(
        job: _Job, runnable: bool
    ) -> list[str]:
        missing = []
        if job.original is None:
            missing.append("runnable task_dir/task_package directory")
        if job.repaired is None:
            missing.append("repaired_task directory")
        if job.original is not None and job.repaired is not None and not runnable:
            missing.append(
                "retained failing_runs/passing_runs or an immutable exploit candidate"
            )
        return missing

    def _process_annotation(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Record normalized human annotations; verify only by real execution.

        Provisional findings stream first with ``execution_available=False``.
        Only candidates whose resolved packages and replay inputs are complete
        run the standard differential; their follow-up finding upgrades to
        confirmed/refuted (``execution_available=True``) or stays provisional
        with the unusable execution record attached. Missing prerequisites
        never fabricate locators, rewards, or statuses.
        """
        job = self._prepare(envelope)
        raw_annotation = job.payload.get("annotation")
        annotation = dict(raw_annotation) if isinstance(raw_annotation, Mapping) else {}
        provenance = job.payload.get("provenance")
        self._emit(
            job,
            "task_quality.started",
            "provisional",
            {"worker_id": self.worker_id, "source_kind": job.payload.get("source_kind")},
        )
        self._renew(job)
        static: dict[str, Any] | None = None
        bundle: dict[str, Any] | None = None
        if job.original is not None:
            task = self.backend.resolve(job.original)
            static = self.backend.static(task).model_dump(mode="json")
            bundle = qa_task.bundle_package(job.original)
            bundle["static"] = static
        severity: Severity = _annotation_severity(annotation) or (
            "low" if _annotation_axes_negative(annotation) else "high"
        )
        root_cause = (
            ["unstated_requirement"]
            if _annotation_indicates(annotation, "underspecified")
            else []
        )
        candidates: list[tuple[Direction, Path | None, Finding, str | None, bool]] = []
        for direction, source in self._annotation_sources(job, annotation):
            runnable = self._annotation_runnable(job, direction, source)
            identity = f"{job.event_id}\0{direction}\0{source or 'annotation'}"
            finding = Finding(
                id="tq-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                subject=FindingSubject(
                    kind="trace" if source is not None else "task",
                    task_id=job.task_id,
                    task_digest=job.original_digest,
                    trial_id=str(source) if source is not None else None,
                ),
                stage="human_annotation",
                verdict="unknown",
                category="false_negative" if direction == "unfair_fail" else "false_positive",
                summary=_annotation_summary(direction, annotation),
                severity=severity,
                fault="eval",
                evidence_refs=[
                    str(ref) for ref in (job.original, source) if ref is not None
                ],
                reproducer=Reproducer(kind="harbor_trial", path=str(source))
                if source is not None
                else None,
                reviewer=Reviewer(agent="task-quality-live"),
                measurement="not_run",
                root_cause=root_cause if direction == "unfair_fail" else [],
            )
            if self.reader is not None and bundle is not None:
                reviewed = self.reader(finding.model_copy(deep=True), bundle)
                if reviewed.id != finding.id or reviewed.subject != finding.subject:
                    raise ValueError("reader cannot change the candidate's identity or subject")
                finding = reviewed.model_copy(
                    update={
                        "verdict": "unknown",
                        "measurement": "not_run",
                        "refutation": Refutation(),
                    }
                )
            input_digest = self._input_digest(job, source, direction) if runnable else None
            candidates.append((direction, source, finding, input_digest, runnable))
            self._finding_event(
                job,
                finding,
                "provisional",
                "human_annotation",
                direction=direction,
                annotation=annotation,
                provenance=provenance,
                execution_available=False,
                static=static,
                input_digest=input_digest,
                missing_prerequisites=self._missing_prerequisites(job, runnable),
            )
        findings: list[dict[str, Any]] = []
        executed = False
        complete = True
        for direction, source, finding, input_digest, runnable in candidates:
            if not runnable:
                findings.append(finding.model_dump(mode="json", by_alias=True))
                continue
            if input_digest is None:
                raise ValueError("runnable annotation candidate lost its input digest")
            executed = True
            finding, status, measured, evidence = self._run_differential(
                job, direction, source, finding, input_digest
            )
            complete = complete and measured
            self._finding_event(
                job,
                finding,
                status,
                "differential_replay",
                annotation=annotation,
                provenance=provenance,
                execution_available=measured,
                **evidence,
            )
            findings.append(finding.model_dump(mode="json", by_alias=True))
        self._emit(
            job,
            "task_quality.completed" if complete else "task_quality.blocked",
            "applied" if complete and executed else "provisional",
            {"worker_id": self.worker_id, "finding_ids": [f["id"] for f in findings]},
        )
        return {"complete": complete, "findings": findings}

    def process_job(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Stream the static candidates before differential execution starts."""
        if envelope["event_type"] == ANNOTATION_JOB:
            return self._process_annotation(envelope)
        job = self._prepare(envelope)
        if job.original is None or job.repaired is None:
            raise ValueError("audit job lost its resolved task packages")
        sources = self._sources(job)
        if not sources:
            raise ValueError("Task Quality needs failing/passing runs or an exploit candidate")
        self._emit(job, "task_quality.started", "provisional", {"worker_id": self.worker_id})
        self._renew(job)
        static = self.backend.static(self.backend.resolve(job.original))
        bundle = qa_task.bundle_package(job.original)
        bundle["static"] = static.model_dump(mode="json")
        candidates: list[tuple[Direction, Path | None, Finding, str]] = []
        for direction, source in sources:
            identity = f"{job.event_id}\0{direction}\0{source or job.payload.get('candidate_digest', 'candidate')}"
            finding = Finding(
                id="tq-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                subject=FindingSubject(
                    kind="trace" if source is not None else "task",
                    task_id=job.task_id,
                    task_digest=job.original_digest,
                    trial_id=str(source) if source is not None else None,
                ),
                stage="static_reader",
                verdict="unknown",
                category="false_negative" if direction == "unfair_fail" else "false_positive",
                summary=(
                    "Possible unfair failure (including unstated timing/format requirements)"
                    if direction == "unfair_fail"
                    else "Possible wrong pass"
                ),
                severity="high",
                fault="eval",
                evidence_refs=[str(job.original), *([str(source)] if source is not None else [])],
                reproducer=Reproducer(kind="harbor_trial", path=str(source))
                if source is not None
                else None,
                reviewer=Reviewer(agent="task-quality-live"),
                measurement="not_run",
                root_cause=["unstated_requirement"]
                if direction == "unfair_fail" and static.facts.get("a_unstated_literal") is True
                else [],
            )
            if self.reader is not None:
                reviewed = self.reader(finding.model_copy(deep=True), bundle)
                if reviewed.id != finding.id or reviewed.subject != finding.subject:
                    raise ValueError("reader cannot change the candidate's identity or subject")
                finding = reviewed.model_copy(
                    update={
                        "verdict": "unknown",
                        "measurement": "not_run",
                        "refutation": Refutation(),
                    }
                )
            input_digest = self._input_digest(job, source, direction)
            candidates.append((direction, source, finding, input_digest))
            self._finding_event(
                job,
                finding,
                "provisional",
                "static_reader",
                static=bundle["static"],
                input_digest=input_digest,
            )
        findings: list[dict[str, Any]] = []
        complete = True
        for direction, source, finding, input_digest in candidates:
            finding, status, measured, evidence = self._run_differential(
                job, direction, source, finding, input_digest
            )
            complete = complete and measured
            self._finding_event(
                job,
                finding,
                status,
                "differential_replay",
                **evidence,
            )
            findings.append(finding.model_dump(mode="json", by_alias=True))
        qualification = None
        if envelope["event_type"] == "task_quality.repair_task" and complete:
            qualification = self._qualify_repair(job)
            complete = qualification["measured"]
        self._emit(
            job,
            "task_quality.completed" if complete else "task_quality.blocked",
            "applied" if complete else "provisional",
            {"worker_id": self.worker_id, "finding_ids": [f["id"] for f in findings]},
        )
        return {"complete": complete, "findings": findings, "qualification": qualification}

    def qualify_repair(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Measure reference=3/3, nop=0/3, exploit=0/3 on the pinned repair."""
        return self._qualify_repair(self._prepare(envelope))

    def _qualify_repair(self, job: _Job) -> dict[str, Any]:
        passing = job.payload.get("passing_runs", [])
        source_value = job.payload.get("exploit_run")
        if source_value is None and len(passing) == 1:
            source_value = passing[0]
        source = self._path(source_value) if source_value is not None else None
        if source is None and not self._has_candidate(job):
            raise ValueError("repair qualification needs one explicit exploit replay input")
        input_digest = self._input_digest(job, source, "wrong_pass")
        if job.repaired is None or job.repaired_digest is None:
            raise ValueError("repair job lost its resolved repaired package")
        backend = self._qa_backend()
        task = backend.resolve(job.repaired)
        blocker = backend.resource_blocker(job.repaired)
        if blocker:
            raise ValueError(blocker)
        reference_patch = job.payload.get("reference_patch")
        if reference_patch is not None:
            backend.bind_reference_patch(job.repaired, self._path(reference_patch))
        records: dict[str, list[dict[str, Any]]] = {"reference": [], "nop": [], "exploit": []}
        for name, agent in (("reference", "oracle"), ("nop", "nop")):
            for repeat in range(3):
                self._renew(job)
                self._unchanged(job)
                self.execution_config.require_backend()
                observation = backend.control(
                    task, agent, timeout_seconds=self.trial_timeout_seconds
                )
                reward = qa_task.finite_reward(observation.facts.get("reward"))
                ref = observation.facts.get("job_path")
                measured = (
                    observation.status == "executed"
                    and not observation.facts.get("exception")
                    and reward is not None
                    and isinstance(ref, str)
                    and bool(ref)
                )
                record = {
                    "reward": reward if measured else None,
                    "measured": measured,
                    "evidence_ref": ref,
                    "execution": observation.model_dump(mode="json"),
                }
                records[name].append(record)
                self._emit(
                    job,
                    "task_quality.repair_control",
                    "provisional",
                    {"control": name, "repeat": repeat + 1, **record},
                )
        for repeat in range(3):
            record = self._execute(
                job, source, job.repaired, direction="wrong_pass", label=f"exploit-{repeat + 1}"
            )
            records["exploit"].append(record)
            self._emit(
                job,
                "task_quality.repair_control",
                "provisional",
                {"control": "exploit", "repeat": repeat + 1, **record},
            )
        self._unchanged(job)
        if self._input_digest(job, source, "wrong_pass") != input_digest:
            raise ValueError("exploit input changed during repair qualification")
        all_records = [record for group in records.values() for record in group]
        references = [
            self._path(record["evidence_ref"]) for record in all_records if record["measured"]
        ]
        measured = all(record["measured"] for record in all_records) and len(set(references)) == 9
        rewards = {name: [record["reward"] for record in group] for name, group in records.items()}
        qualified = measured and rewards == {
            "reference": [1.0] * 3,
            "nop": [0.0] * 3,
            "exploit": [0.0] * 3,
        }
        payload = {
            **job.payload,
            "original_task_package": str(job.original),
            "task_package": str(job.repaired),
            "task_package_digest": job.repaired_digest,
            "repaired_task": str(job.repaired),
            "repaired_task_digest": job.repaired_digest,
            "ref_passes": sum(reward == 1.0 for reward in rewards["reference"]),
            "nop": sum(reward is not None and reward > 0.0 for reward in rewards["nop"]),
            "exploit": sum(reward is not None and reward > 0.0 for reward in rewards["exploit"]),
            "reference_trials": 3,
            "nop_trials": 3,
            "exploit_trials": 3,
            "control_rewards": rewards,
            "control_evidence": records,
            "qualified": qualified,
            "measured": measured,
        }
        event_type = (
            "task_quality.repair_qualified"
            if qualified
            else "task_quality.repair_rejected"
            if measured
            else "task_quality.repair_unavailable"
        )
        event = self._emit(
            job,
            event_type,
            "confirmed" if qualified else "refuted" if measured else "provisional",
            payload,
            idempotency_key=f"tq-qualified:{job.event_id}:{job.repaired_digest}"
            if qualified
            else None,
        )
        if qualified:
            # The asset's mechanism signature is the producer's own semantic
            # declaration: the shortcut mechanism this repair was qualified
            # against, namespaced so a repaired task never collides with the
            # exploit-witness row for the same mechanism. A repair job without
            # one emits no signature and is honestly rejected at export.
            shortcut = job.payload.get("shortcut_signature") or job.payload.get("mechanism")
            signature = (
                {"mechanism_signature": f"repaired-task:{shortcut}"}
                if isinstance(shortcut, str) and shortcut.strip()
                else {}
            )
            self._emit(
                job,
                "asset.verified",
                "confirmed",
                {
                    "asset_id": "repaired-task:" + job.repaired_digest,
                    "kind": "repaired_task",
                    "task_package": str(job.repaired),
                    "task_package_digest": job.repaired_digest,
                    "qualification_event_id": event["event_id"],
                    "control_rewards": rewards,
                    "control_evidence": records,
                    **signature,
                },
                idempotency_key=f"tq-asset:{job.event_id}:{job.repaired_digest}",
            )
        return {
            "qualified": qualified,
            "measured": measured,
            "control_rewards": rewards,
            "event": event,
        }

    def run_once(self) -> dict[str, Any] | None:
        """Lease only Task Quality jobs; acknowledge only measured completion."""
        envelope = self.queue.claim(
            self.worker_id, job_types=JOB_TYPES, lease_seconds=self.lease_seconds
        )
        if envelope is None:
            return None
        try:
            result = self.process_job(envelope)
        except BaseException as exc:
            try:
                if isinstance(exc, Exception):
                    self.queue.append_event(
                        {
                            "event_id": str(uuid.uuid4()),
                            "run_id": envelope["run_id"],
                            "system": "task_quality",
                            "event_type": "task_quality.failed",
                            "seq": 0,
                            "ts": self.clock(),
                            "prefix_step": None,
                            "status": "provisional",
                            "payload": {
                                "job_id": envelope["event_id"],
                                "worker_id": self.worker_id,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        }
                    )
            finally:
                self.queue.release(envelope["event_id"], lease_token=envelope["lease_token"])
            raise
        if result["complete"]:
            self.queue.ack(envelope["event_id"], lease_token=envelope["lease_token"])
        else:
            self.queue.release(envelope["event_id"], lease_token=envelope["lease_token"])
        return result

    def run(self, stop: threading.Event, *, poll_seconds: float = 1.0) -> None:
        """Consume continuously until the caller's shutdown event is set.

        Failed work is released durably and does not kill the consumer. A wait
        on empty, blocked or failed work prevents an unavailable task from
        spinning; the queue retains ownership of scheduling and delivery.
        """
        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception:
                stop.wait(poll_seconds)
                continue
            if result is None or not result["complete"]:
                stop.wait(poll_seconds)
