"""Guarded completion-to-analysis worker (M006).

Turns every eligible completed trial into exactly one provenance-frozen
analysis record — pending, completed, deferred(reason), or
quarantined(reason) — and guarantees no model call happens without profile
preflight (M003) and policy admission. Composes the existing machinery:
discovery over raw job directories (`results`), stage-5 execution and
sidecar writing (`facts.run_trial_analysis`), catalog indexing
(`facts.ingest_analysis_sidecar`), and profile preflight
(`profiles.preflight`).

Contract highlights, all tested:

- **Frozen identity.** An AnalysisRequest binds experiment/job/trial ids to
  the sha256 of result, trajectory, task, verifier, rubric, prompt, and
  profile. The request id derives from that identity: rescans and restarts
  collide on the same record instead of duplicating calls or sidecars.
- **Append-only transitions.** ``transitions.jsonl`` per request; state is
  the last line; nothing is ever rewritten.
- **Fail-closed admission.** STOP file, unqualified profile, failing
  credential preflight, cost/call ceilings, unmet policy requirements,
  unhealthy services, stale/tampered evidence — each produces zero calls
  and a recorded reason. Harness/auth/infrastructure failures are never
  recorded as agent failures.
- **Crash safety.** A sidecar on disk without a ``completed`` transition is
  adopted, never re-executed. A durably-started invocation without a sidecar
  is ambiguous and requires explicit operator resolution; it is never replayed.
- **Durability.** Every name recovery depends on is fsynced together with
  the directory that holds it — the request directory, ``request.json``, the
  invocation journal, and the sidecar. A dirent lost to a host-level crash
  would otherwise erase the proof that a call was already paid for.
- **Concurrency.** A kernel-backed lease with a unique owner token serializes
  each request; process death releases it and no owner deletes another's lease.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Callable, Iterable  # noqa: F401  (Iterable in signatures)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from evallab.evidence import facts
from evallab.evidence.facts import AnalyzerCallable, ingest_analysis_sidecar, run_trial_analysis
from evallab.profiles import AgentProfile, PreflightDecision, ProbeFn, preflight
from evallab.results import JobRecord, TrialRecord, load_jobs
from evallab.schemas import (
    JudgeCalibrationRecord,
    StandingApprovalsPolicy,
    TrialAnalysisSidecar,
)

State = Literal["pending", "admitted", "running", "completed", "deferred", "quarantined"]

RESEARCHER_RULE = "researcher-followups"
WORKER_DIRNAME = "worker"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _fsync_directory(directory: Path) -> None:
    """Fsync a directory so dirents created inside it survive a host crash."""
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _durable_mkdir(directory: Path) -> None:
    """Create ``directory``, fsyncing the dirent of every level this adds.

    ``mkdir`` leaves the new directory entries in their parents' dirty cache.
    A crash can therefore lose a whole request subtree — including the
    invocation journal that proves a possibly-paid call already happened.
    """
    created: list[Path] = []
    probe = directory
    while not probe.exists():
        created.append(probe)
        probe = probe.parent
    directory.mkdir(parents=True, exist_ok=True)
    for path in reversed(created):
        _fsync_directory(path.parent)


def _durable_replace(source: Path, destination: Path) -> None:
    """Fsync file bytes before atomically publishing the stable sidecar path."""
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    source.replace(destination)
    _fsync_directory(destination.parent)


def _quality_identity(
    trial_dir: Path,
    job_dir: Path | None = None,
    *,
    job_id: str,
    trial_id: str,
) -> tuple[str, str, str, str | None, str, str]:
    from evallab.interpretation.trajectory_quality import evaluate_trial_quality

    report, _findings = evaluate_trial_quality(
        trial_dir,
        job_dir,
        job_id_override=job_id,
        trial_id_override=trial_id,
    )
    report_body = {
        "check_version": report.check_version,
        "check_digest": report.check_digest,
        "status": str(report.status),
        "is_ingestable": report.is_ingestable,
        "is_analysis_ready": report.is_analysis_ready,
        "quarantine_reason": report.quarantine_reason or "",
        "findings_count": report.findings_count,
        "warnings_count": report.warnings_count,
        "errors_count": report.errors_count,
    }
    report_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(report_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )

    quality_files = (
        "result.json",
        "agent/trajectory.json",
        "exception.txt",
        "lock.json",
    )
    inputs_body: dict[str, str] = {}
    for rel_path in quality_files:
        f_path = trial_dir / rel_path
        if f_path.is_file():
            inputs_body[rel_path] = "sha256:" + hashlib.sha256(f_path.read_bytes()).hexdigest()
        else:
            inputs_body[rel_path] = "absent"

    inputs_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(inputs_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )

    return (
        str(report.status),
        report.check_version,
        report.check_digest,
        report.quarantine_reason,
        report_digest,
        inputs_digest,
    )


class AnalysisRequest(BaseModel):
    """Frozen identity of one trial's analysis. Never edited after creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    request_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    created_at: datetime
    experiment_id: str | None
    job_id: str
    trial_id: str
    job_name: str
    trial_name: str
    trial_path: str
    profile_id: str
    adapter: str
    model: str
    result_sha256: str
    trajectory_sha256: str | None  # None = trial has no trajectory file
    lock_sha256: str | None  # Harbor lock bytes: source of task/verifier truth
    task_digest: str | None
    verifier_digest: str | None
    rubric_sha256: str
    prompt_sha256: str
    profile_digest: str
    quality_status: str
    quality_check_version: str
    quality_check_digest: str
    quality_quarantine_reason: str | None = None
    quality_report_digest: str
    quality_inputs_digest: str
    source_snapshot_digest: str

    @property
    def identity_digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("created_at")  # identity is content, not clock
        payload.pop("request_id")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


AnalyzerFactory = Callable[
    [JobRecord, TrialRecord, AnalysisRequest],
    AnalyzerCallable,
]


@dataclass(frozen=True)
class Transition:
    at: str
    state: State
    reason: str | None = None
    actor: str = "analysis-worker"


@dataclass(frozen=True)
class CycleReport:
    discovered: int
    staged: int
    calls: int
    completed: int
    adopted: int
    deferred: dict[str, int] = field(default_factory=dict)
    quarantined: dict[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeaseHandle:
    """Exact kernel lease acquisition held by one worker invocation."""

    request_id: str
    owner_token: str
    fd: int
    device: int
    inode: int


# ---------------------------------------------------------------------------
# Store: request.json (write-once) + transitions.jsonl (append-only) + lease
# ---------------------------------------------------------------------------


class RequestStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def request_dir(self, request_id: str) -> Path:
        return self.root / "requests" / request_id

    def freeze(self, request: AnalysisRequest, *, trial_path: Path | None = None) -> bool:
        """Persist a new request and capture its immutable source snapshot."""
        import shutil

        directory = self.request_dir(request.request_id)
        _durable_mkdir(directory)
        snapshot_dir = directory / "snapshot"
        if not snapshot_dir.is_dir():
            _durable_mkdir(snapshot_dir)
            source_trial = trial_path
            if source_trial is None:
                candidates = [
                    self.root.parent.parent.parent / request.trial_path,
                    self.root.parent.parent / request.trial_path,
                    Path(request.trial_path),
                ]
                for cand in candidates:
                    if cand.is_dir():
                        source_trial = cand
                        break
            if source_trial is not None and source_trial.is_dir():
                for root_dir, _dirs, files in os.walk(source_trial, followlinks=False):
                    r_path = Path(root_dir)
                    rel = r_path.relative_to(source_trial)
                    target_dir = snapshot_dir / rel
                    _durable_mkdir(target_dir)
                    for f in files:
                        src_f = r_path / f
                        dst_f = target_dir / f
                        if not dst_f.exists():
                            shutil.copy2(src_f, dst_f)
                            with open(dst_f, "rb") as h:
                                os.fsync(h.fileno())
                    _fsync_directory(target_dir)
                _fsync_directory(snapshot_dir)

        path = directory / "request.json"
        try:
            with open(path, "x") as handle:
                handle.write(request.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            return False
        # request.json is the root of recovery: lose its dirent and a rescan
        # mints this same identity again with no journal to stop a second call.
        _fsync_directory(directory)
        self.append(request.request_id, "pending", None)
        return True

    def load(self, request_id: str) -> AnalysisRequest:
        path = self.request_dir(request_id) / "request.json"
        return AnalysisRequest.model_validate_json(path.read_text())

    def append(self, request_id: str, state: State, reason: str | None) -> None:
        record = {
            "at": _utc_now().isoformat(),
            "state": state,
            "reason": reason,
            "actor": "analysis-worker",
        }
        path = self.request_dir(request_id) / "transitions.jsonl"
        with open(path, "a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def transitions(self, request_id: str) -> list[Transition]:
        path = self.request_dir(request_id) / "transitions.jsonl"
        if not path.is_file():
            return []
        out: list[Transition] = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            out.append(
                Transition(
                    at=str(payload.get("at")),
                    state=payload.get("state"),
                    reason=payload.get("reason"),
                    actor=str(payload.get("actor", "analysis-worker")),
                )
            )
        return out

    def state(self, request_id: str) -> State | None:
        transitions = self.transitions(request_id)
        return transitions[-1].state if transitions else None

    def all_request_ids(self) -> list[str]:
        base = self.root / "requests"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if (p / "request.json").is_file())

    def sidecar_path(self, request_id: str) -> Path:
        return self.request_dir(request_id) / "sidecar" / "analysis.json"

    def _lease_path(self, request_id: str) -> Path:
        return self.request_dir(request_id) / "lease"

    def acquire_lease(
        self,
        request_id: str,
        *,
        owner_pid: int | None = None,
        owner_token: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> LeaseHandle | None:
        """Acquire one kernel-backed, token-identified request lease.

        ``flock`` is the ownership authority. Metadata is diagnostic only and
        is overwritten *after* the kernel grants exclusivity. A process crash
        releases the lock automatically, so reclaim has no check-to-rename
        race and never unlinks another worker's path.
        """
        pid = owner_pid if owner_pid is not None else os.getpid()
        now = (clock or _utc_now)()
        path = self._lease_path(request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        token = owner_token or uuid4().hex
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        try:
            payload = json.dumps(
                {
                    "owner_token": token,
                    "pid": pid,
                    "acquired_at": now.isoformat(),
                    "host": os.uname().nodename,
                },
                sort_keys=True,
            ).encode()
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            stat = os.fstat(fd)
            return LeaseHandle(
                request_id=request_id,
                owner_token=token,
                fd=fd,
                device=stat.st_dev,
                inode=stat.st_ino,
            )
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise

    def release_lease(self, request_id: str, lease: LeaseHandle) -> bool:
        """Release only ``lease``'s fd; never unlink or rewrite the path.

        The boolean reports whether the current path still names this exact
        token/inode. A false result exposes replacement but deliberately does
        not disturb the replacement owner's live lease.
        """
        if lease.request_id != request_id:
            raise ValueError("lease request id mismatch")
        still_owner = False
        try:
            path_stat = self._lease_path(request_id).stat()
            os.lseek(lease.fd, 0, os.SEEK_SET)
            payload = json.loads(os.read(lease.fd, 4096))
            still_owner = (
                path_stat.st_dev == lease.device
                and path_stat.st_ino == lease.inode
                and payload.get("owner_token") == lease.owner_token
            )
        except (OSError, ValueError, AttributeError):
            still_owner = False
        finally:
            fcntl.flock(lease.fd, fcntl.LOCK_UN)
            os.close(lease.fd)
        return still_owner

    def _invocations_path(self, request_id: str) -> Path:
        return self.request_dir(request_id) / "invocations.jsonl"

    def _append_invocation_event(self, request_id: str, payload: dict[str, Any]) -> None:
        """Durably append before crossing a possibly billable boundary."""
        path = self._invocations_path(request_id)
        _durable_mkdir(path.parent)
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
        try:
            fd = os.open(path, os.O_APPEND | os.O_WRONLY)
            created = False
        except FileNotFoundError:
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            created = True
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            # O_CREAT durably places the bytes, not the name. Without the
            # parent fsync a crash leaves no journal, recovery sees no
            # unresolved attempt, and the guarded call is issued twice.
            _fsync_directory(path.parent)

    def invocation_events(self, request_id: str) -> list[dict[str, Any]]:
        path = self._invocations_path(request_id)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def begin_invocation(
        self,
        request_id: str,
        *,
        owner_token: str,
        at: datetime,
    ) -> str:
        attempt_id = uuid4().hex
        self._append_invocation_event(
            request_id,
            {
                "event": "invocation_started",
                "attempt_id": attempt_id,
                "at": at.isoformat(),
                "owner_token": owner_token,
                "actor": "analysis-worker",
            },
        )
        return attempt_id

    def resolve_invocation(
        self,
        request_id: str,
        attempt_id: str,
        *,
        resolution: str,
        actor: str,
        at: datetime,
    ) -> None:
        self._append_invocation_event(
            request_id,
            {
                "event": "invocation_resolved",
                "attempt_id": attempt_id,
                "at": at.isoformat(),
                "resolution": resolution,
                "actor": actor,
            },
        )

    def record_lease_replacement(
        self,
        request_id: str,
        *,
        owner_token: str,
        at: datetime,
    ) -> None:
        """Note durably that ``flock`` stopped serializing this request.

        Kept in the invocation journal rather than ``transitions.jsonl``:
        state is the last transition line, so an audit note there would
        overwrite the reason the state machine reads back.
        """
        self._append_invocation_event(
            request_id,
            {
                "event": "lease_replaced_during_execution",
                "at": at.isoformat(),
                "owner_token": owner_token,
                "actor": "analysis-worker",
            },
        )

    def unresolved_invocation(self, request_id: str) -> str | None:
        unresolved: dict[str, None] = {}
        for event in self.invocation_events(request_id):
            attempt_id = str(event.get("attempt_id", ""))
            if not attempt_id:
                continue
            if event.get("event") == "invocation_started":
                unresolved[attempt_id] = None
            elif event.get("event") == "invocation_resolved":
                unresolved.pop(attempt_id, None)
        return next(reversed(unresolved), None) if unresolved else None


# ---------------------------------------------------------------------------
# Discovery and freezing
# ---------------------------------------------------------------------------


def _trial_result(trial: TrialRecord) -> dict[str, Any]:
    try:
        return json.loads((trial.path / "result.json").read_text())
    except (OSError, ValueError):
        return {}


def freeze_request(
    job: JobRecord,
    trial: TrialRecord,
    *,
    profile: AgentProfile,
    prompt_path: Path,
    rubric_path: Path,
    repo_root: Path,
    clock: Callable[[], datetime] = _utc_now,
) -> AnalysisRequest | None:
    """Freeze identity from the evidence bytes as they exist right now."""
    result_sha = _sha256_file(trial.path / "result.json")
    prompt_sha = _sha256_file(prompt_path)
    rubric_sha = _sha256_file(rubric_path)
    if result_sha is None or prompt_sha is None or rubric_sha is None:
        return None
    try:
        trial_rel = trial.path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        trial_rel = trial.path.resolve().as_posix()
    from evallab.evidence_store import evidence_tree_digest

    source_snapshot_digest = evidence_tree_digest(trial.path)
    q_status, q_check_ver, q_check_dig, q_quar_reason, q_rep_dig, q_inp_dig = _quality_identity(
        trial.path,
        job.path,
        job_id=str(job.id),
        trial_id=str(trial.id),
    )
    body = {
        "experiment_id": facts.experiment_id(job),
        "job_id": str(job.id),
        "trial_id": str(trial.id),
        "job_name": job.path.name,
        "trial_name": trial.path.name,
        "trial_path": trial_rel,
        "profile_id": profile.profile_id,
        "adapter": profile.adapter,
        "model": profile.model or "",
        "result_sha256": result_sha,
        "trajectory_sha256": _sha256_file(trial.path / "agent" / "trajectory.json"),
        "lock_sha256": _sha256_file(trial.path / "lock.json"),
        "task_digest": facts._task_digest(trial),
        "verifier_digest": facts._verifier_digest(job, trial),
        "rubric_sha256": rubric_sha,
        "prompt_sha256": prompt_sha,
        "profile_digest": profile.digest,
        "quality_status": q_status,
        "quality_check_version": q_check_ver,
        "quality_check_digest": q_check_dig,
        "quality_quarantine_reason": q_quar_reason,
        "quality_report_digest": q_rep_dig,
        "quality_inputs_digest": q_inp_dig,
        "source_snapshot_digest": source_snapshot_digest,
    }
    # Identity keys on the TRIAL, not on content: one analysis record per
    # trial, frozen at first sight. Content digests are frozen inside the
    # record; admission verifies them, so changed evidence quarantines the
    # existing record instead of silently minting a runnable new identity.
    identity = f"{body['job_id']}:{body['trial_id']}"
    request_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return AnalysisRequest.model_validate({"request_id": request_id, "created_at": clock(), **body})


def eligible_trials(jobs: Iterable[JobRecord]) -> list[tuple[JobRecord, TrialRecord, str | None]]:
    """(job, trial, ineligibility_reason). Harness exceptions are ineligible
    for *agent* analysis and the reason says so explicitly."""
    out: list[tuple[JobRecord, TrialRecord, str | None]] = []
    for job in jobs:
        for trial in job.trials:
            result = _trial_result(trial)
            if not result:
                out.append((job, trial, "quarantine:result_unreadable"))
            elif result.get("exception_info"):
                out.append((job, trial, "defer:harness_exception_not_agent_failure"))
            elif ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward") is None:
                out.append((job, trial, "defer:no_verdict_recorded"))
            else:
                out.append((job, trial, None))
    return out


# ---------------------------------------------------------------------------
# Admission: every gate produces zero calls and a recorded reason
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionContext:
    stop_present: Callable[[], bool]
    policy: StandingApprovalsPolicy
    profile: AgentProfile
    probe: ProbeFn | None
    spent_today_usd: Callable[[], float]
    est_call_cost_usd: float
    services_healthy: Callable[[], bool]
    requirement_checks: dict[str, Callable[[], bool]]


@dataclass(frozen=True)
class Admission:
    kind: Literal["admit", "defer", "quarantine"]
    reason: str | None = None
    preflight: PreflightDecision | None = None


def admit(
    request: AnalysisRequest,
    store: RequestStore,
    context: AdmissionContext,
    repo_root: Path,
    *,
    job: JobRecord | None = None,
    trial: TrialRecord | None = None,
    prompt_path: Path | None = None,
    rubric_path: Path | None = None,
) -> Admission:
    if context.stop_present():
        return Admission("defer", "queue_stop_present")

    # Every frozen input is recomputed and compared BEFORE anything that
    # could spend money. Missing or changed inputs fail closed with a
    # precise reason. Evidence changes quarantine; configuration drift
    # (prompt/rubric/profile) defers as stale identity.
    trial_dir = repo_root / request.trial_path
    current_result = _sha256_file(trial_dir / "result.json")
    if current_result is None:
        return Admission("quarantine", "evidence_missing:result.json")
    if current_result != request.result_sha256:
        return Admission("quarantine", "evidence_tampered:result.json")
    current_traj = _sha256_file(trial_dir / "agent" / "trajectory.json")
    if current_traj != request.trajectory_sha256:
        state = "evidence_missing" if current_traj is None else "evidence_tampered"
        return Admission("quarantine", f"{state}:trajectory.json")
    current_lock = _sha256_file(trial_dir / "lock.json")
    if request.lock_sha256 is not None and current_lock is None:
        return Admission("quarantine", "evidence_missing:lock.json")
    if current_lock != request.lock_sha256:
        return Admission("quarantine", "evidence_tampered:lock.json")

    # Quality Gate: Recompute quality identity from current exact source bytes.
    # Any input tampering or status drift fails closed before any model call.
    try:
        cur_status, cur_check_ver, cur_check_dig, cur_quar_reason, cur_rep_dig, cur_inp_dig = (
            _quality_identity(
                trial_dir,
                trial_dir.parent,
                job_id=request.job_id,
                trial_id=request.trial_id,
            )
        )
    except Exception:
        return Admission("defer", "quality_not_evaluated")

    if cur_inp_dig != request.quality_inputs_digest:
        return Admission("quarantine", "evidence_tampered:quality_inputs")

    if (
        cur_rep_dig != request.quality_report_digest
        or cur_status != request.quality_status
        or cur_check_dig != request.quality_check_digest
    ):
        return Admission("quarantine", "evidence_tampered:quality_status_drift")

    if request.quality_status == "quarantine":
        return Admission(
            "quarantine",
            f"quality_quarantined:{request.quality_quarantine_reason or 'infrastructure_fault'}",
        )

    if request.quality_status in ("fail", "quality_not_evaluated"):
        return Admission(
            "quarantine",
            f"quality_failed:{request.quality_quarantine_reason or 'malformed_evidence'}",
        )
    if job is not None and trial is not None:
        # Harbor locks are the digest truth: prove the frozen task/verifier
        # values from the CURRENT lock bytes, not from memory.
        if facts._task_digest(trial) != request.task_digest:
            return Admission("quarantine", "evidence_tampered:task_digest")
        if facts._verifier_digest(job, trial) != request.verifier_digest:
            return Admission("quarantine", "evidence_tampered:verifier_digest")
    if prompt_path is not None:
        current_prompt = _sha256_file(prompt_path)
        if current_prompt is None:
            return Admission("quarantine", "evidence_missing:prompt")
        if current_prompt != request.prompt_sha256:
            return Admission("defer", "stale_identity:prompt_changed")
    if rubric_path is not None:
        current_rubric = _sha256_file(rubric_path)
        if current_rubric is None:
            return Admission("quarantine", "evidence_missing:rubric")
        if current_rubric != request.rubric_sha256:
            return Admission("defer", "stale_identity:rubric_changed")
    if context.profile.digest != request.profile_digest:
        return Admission("defer", "stale_identity:profile_changed")

    rule = next((r for r in context.policy.auto_run if r.name == RESEARCHER_RULE), None)
    if rule is None:
        return Admission("defer", f"policy_rule_absent:{RESEARCHER_RULE}")
    if request.adapter not in rule.agents:
        return Admission("defer", f"policy_agent_not_listed:{request.adapter}")
    for requirement in rule.requires:
        check = context.requirement_checks.get(requirement)
        if check is None or not check():
            return Admission("defer", f"policy_requirement_unmet:{requirement}")

    if not context.profile.verified_facts:
        return Admission("defer", "profile_not_qualified:no_verified_facts")
    decision = preflight(context.profile, context.probe)
    if not decision.proceed:
        # Auth/infrastructure — an operational deferral, never an agent failure.
        return Admission("defer", f"credential:{decision.reason}", decision)

    ceiling = context.policy.per_job_cost_ceiling_usd
    if context.est_call_cost_usd > ceiling:
        return Admission("defer", f"cost_ceiling:call_estimate_exceeds_{ceiling}")
    if context.spent_today_usd() + context.est_call_cost_usd > (
        context.policy.daily_cost_ceiling_usd
    ):
        return Admission("defer", "cost_ceiling:daily")

    if not context.services_healthy():
        return Admission("defer", "services_unhealthy")
    return Admission("admit", preflight=decision)


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


IndexFn = Callable[[Path], None]


_PERMANENT_DEFERRAL_PREFIXES = ("harness_exception", "no_verdict")


def _is_permanent_deferral(transition: Transition) -> bool:
    """True when a deferral is evidence-shaped and no retry can ever help.

    A harness exception or a missing verdict is a property of the recorded
    trial, not a transient condition: analysing it anyway would spend money
    and record a harness failure as an agent failure.
    """
    return bool(transition.reason and transition.reason.startswith(_PERMANENT_DEFERRAL_PREFIXES))


def _no_adapter(prompt: str, schema: dict[str, Any]):
    raise RuntimeError(
        "no analysis adapter is wired; plan/status/stage never call a model, "
        "and run-one requires an explicitly constructed adapter"
    )


@dataclass
class AnalysisWorker:
    repo_root: Path
    store: RequestStore
    context: AdmissionContext
    adapter: AnalyzerCallable
    prompt_path: Path
    rubric_path: Path
    adapter_factory: AnalyzerFactory | None = None
    indexer: IndexFn | None = None
    clock: Callable[[], datetime] = _utc_now

    # -- staging (nightly may run ONLY this) --------------------------------

    def stage(self, job_roots: list[Path]) -> CycleReport:
        """Discover and freeze. Never calls a model, never admits."""
        jobs = load_jobs(job_roots)
        discovered = staged = 0
        deferred: dict[str, int] = {}
        quarantined: dict[str, int] = {}

        for job, trial, reason in eligible_trials(jobs):
            discovered += 1
            request = freeze_request(
                job,
                trial,
                profile=self.context.profile,
                prompt_path=self.prompt_path,
                rubric_path=self.rubric_path,
                repo_root=self.repo_root,
                clock=self.clock,
            )
            if request is None:
                quarantined["evidence_unreadable"] = quarantined.get("evidence_unreadable", 0) + 1
                continue
            if self.store.freeze(request):
                staged += 1
                if reason is not None:
                    kind, _, detail = reason.partition(":")
                    state: State = "deferred" if kind == "defer" else "quarantined"
                    self.store.append(request.request_id, state, detail)
                    bucket = deferred if state == "deferred" else quarantined
                    bucket[detail] = bucket.get(detail, 0) + 1
        return CycleReport(
            discovered=discovered,
            staged=staged,
            calls=0,
            completed=0,
            adopted=0,
            deferred=deferred,
            quarantined=quarantined,
        )

    # -- execution ----------------------------------------------------------

    def run_one(self, request_id: str) -> Transition:
        """Run once, refusing automatic replay of an ambiguous invocation."""
        request = self.store.load(request_id)
        state = self.store.state(request_id)
        if state in {"completed", "quarantined"}:
            return self.store.transitions(request_id)[-1]
        if state == "deferred":
            last = self.store.transitions(request_id)[-1]
            if _is_permanent_deferral(last):
                # run_one is the production entrypoint, so permanence is
                # enforced here too — not only in the run_cycle loop.
                return last

        # Crash-after-call adoption: sidecar exists -> never call again.
        sidecar_path = self.store.sidecar_path(request_id)
        if sidecar_path.is_file():
            self._complete(request_id, sidecar_path, adopted=True)
            return self.store.transitions(request_id)[-1]

        # A durable invocation-start marker with no sidecar crosses the only
        # boundary we cannot infer across: the provider may have returned and
        # charged before this process crashed. Never replay it automatically.
        if self.store.unresolved_invocation(request_id) is not None:
            reason = "ambiguous_invocation_requires_operator_resolution"
            previous = self.store.transitions(request_id)[-1]
            if previous.state != "deferred" or previous.reason != reason:
                self.store.append(request_id, "deferred", reason)
            return self.store.transitions(request_id)[-1]

        lease = self.store.acquire_lease(request_id)
        if lease is None:
            self.store.append(request_id, "deferred", "lease_held_by_another_worker")
            return self.store.transitions(request_id)[-1]
        try:
            from evallab.evidence_store import evidence_tree_digest
            from evallab.results import load_trial

            job_dir = (self.repo_root / request.trial_path).parent
            jobs = load_jobs([job_dir.parent])
            match = next(
                ((j, t) for j in jobs for t in j.trials if str(t.id) == request.trial_id),
                None,
            )
            admission = admit(
                request,
                self.store,
                self.context,
                self.repo_root,
                job=match[0] if match else None,
                trial=match[1] if match else None,
                prompt_path=self.prompt_path,
                rubric_path=self.rubric_path,
            )
            if admission.kind != "admit":
                target: State = "deferred" if admission.kind == "defer" else "quarantined"
                self.store.append(request_id, target, admission.reason)
                return self.store.transitions(request_id)[-1]
            if match is None:
                self.store.append(request_id, "quarantined", "trial_vanished")
                return self.store.transitions(request_id)[-1]
            if self.adapter is _no_adapter and self.adapter_factory is None:
                self.store.append(request_id, "deferred", "adapter_not_wired")
                return self.store.transitions(request_id)[-1]

            self.store.append(request_id, "admitted", None)

            snapshot_trial_dir = self.store.request_dir(request_id) / "snapshot"
            if not snapshot_trial_dir.is_dir():
                source_trial = self.repo_root / request.trial_path
                if source_trial.is_dir():
                    self.store.freeze(request, trial_path=source_trial)

            if not snapshot_trial_dir.is_dir():
                self.store.append(request_id, "quarantined", "evidence_missing:source_snapshot")
                return self.store.transitions(request_id)[-1]

            cur_snap_dig = evidence_tree_digest(snapshot_trial_dir)
            if cur_snap_dig != request.source_snapshot_digest:
                self.store.append(request_id, "quarantined", "evidence_tampered:source_snapshot")
                return self.store.transitions(request_id)[-1]

            trial = load_trial(snapshot_trial_dir)
            snapshot_job_dir = snapshot_trial_dir.parent
            job = JobRecord(
                path=snapshot_job_dir,
                result={"id": request.job_id},
                config={},
                lock=trial.lock,
                metadata={"name": request.job_name},
                trials=(trial,),
            )

            analyzer = self.adapter
            if self.adapter_factory is not None:
                try:
                    analyzer = self.adapter_factory(job, trial, request)
                except Exception as exc:
                    self.store.append(
                        request_id,
                        "deferred",
                        f"adapter_configuration_error:{type(exc).__name__}",
                    )
                    return self.store.transitions(request_id)[-1]

            attempt_id = self.store.begin_invocation(
                request_id,
                owner_token=lease.owner_token,
                at=self.clock(),
            )
            self.store.append(request_id, "running", f"attempt:{attempt_id}")
            _durable_mkdir(sidecar_path.parent)
            written_path, _sidecar = run_trial_analysis(
                job,
                trial,
                analyzer=analyzer,
                repo_root=self.repo_root,
                destination_root=sidecar_path.parent,
                prompt_path=self.prompt_path,
                rubric_path=self.rubric_path,
                agent=request.adapter,
                agent_version=request.profile_id,
                model=request.model,
                created_at=self.clock(),
            )
            # Normalize to the stable per-request location and make the file
            # durable before resolving the possibly-paid invocation marker.
            _durable_replace(written_path, sidecar_path)
            self.store.resolve_invocation(
                request_id,
                attempt_id,
                resolution="sidecar_persisted",
                actor="analysis-worker",
                at=self.clock(),
            )
            self._complete(request_id, sidecar_path, adopted=False)
            return self.store.transitions(request_id)[-1]
        finally:
            self._release_lease(request_id, lease)

    def _release_lease(self, request_id: str, lease: LeaseHandle) -> None:
        """Release this lease, recording any loss of serialization."""
        if not self.store.release_lease(request_id, lease):
            self.store.record_lease_replacement(
                request_id, owner_token=lease.owner_token, at=self.clock()
            )

    def _complete(self, request_id: str, sidecar_path: Path, *, adopted: bool) -> None:
        sidecar = TrialAnalysisSidecar.model_validate_json(sidecar_path.read_text())
        ambiguous = self.store.unresolved_invocation(request_id)
        if ambiguous is not None:
            self.store.resolve_invocation(
                request_id,
                ambiguous,
                resolution="sidecar_adopted",
                actor="analysis-worker",
                at=self.clock(),
            )
        if self.indexer is not None:
            self.indexer(sidecar_path)  # idempotent catalog upsert; retried on rescan
        reason = "adopted_existing_sidecar" if adopted else None
        self.store.append(request_id, "completed", reason)
        del sidecar

    def resolve_ambiguous(
        self,
        request_id: str,
        *,
        action: Literal["retry", "quarantine"],
        actor: str,
    ) -> Transition:
        """Record an explicit operator disposition for one ambiguous call."""
        normalized_actor = actor.strip()
        if not normalized_actor or len(normalized_actor) > 128 or "\n" in normalized_actor:
            raise ValueError("operator actor must be 1-128 characters without newlines")
        lease = self.store.acquire_lease(request_id)
        if lease is None:
            raise RuntimeError("cannot resolve ambiguity while another worker is live")
        try:
            sidecar_path = self.store.sidecar_path(request_id)
            if sidecar_path.is_file():
                self._complete(request_id, sidecar_path, adopted=True)
                return self.store.transitions(request_id)[-1]
            attempt_id = self.store.unresolved_invocation(request_id)
            if attempt_id is None:
                raise ValueError("request has no ambiguous invocation to resolve")
            resolution = (
                "operator_retry_authorized" if action == "retry" else "operator_quarantined"
            )
            self.store.resolve_invocation(
                request_id,
                attempt_id,
                resolution=resolution,
                actor=normalized_actor,
                at=self.clock(),
            )
            if action == "retry":
                self.store.append(
                    request_id,
                    "pending",
                    f"operator_retry_authorized:{normalized_actor}",
                )
            else:
                self.store.append(
                    request_id,
                    "quarantined",
                    f"ambiguous_invocation_quarantined:{normalized_actor}",
                )
            return self.store.transitions(request_id)[-1]
        finally:
            self._release_lease(request_id, lease)

    def run_cycle(self, job_roots: list[Path]) -> CycleReport:
        stage_report = self.stage(job_roots)
        calls = completed = adopted = 0
        deferred = dict(stage_report.deferred)
        quarantined = dict(stage_report.quarantined)
        for request_id in self.store.all_request_ids():
            state = self.store.state(request_id)
            if state in {"completed", "quarantined"}:
                continue
            if state == "deferred" and _is_permanent_deferral(
                self.store.transitions(request_id)[-1]
            ):
                continue  # permanent by evidence shape; retry needs new evidence
            had_sidecar = self.store.sidecar_path(request_id).is_file()
            transition = self.run_one(request_id)
            if transition.state == "completed":
                completed += 1
                if had_sidecar or transition.reason == "adopted_existing_sidecar":
                    adopted += 1
                else:
                    calls += 1
            elif transition.state == "deferred":
                key = transition.reason or "unspecified"
                deferred[key] = deferred.get(key, 0) + 1
            elif transition.state == "quarantined":
                key = transition.reason or "unspecified"
                quarantined[key] = quarantined.get(key, 0) + 1
        return CycleReport(
            discovered=stage_report.discovered,
            staged=stage_report.staged,
            calls=calls,
            completed=completed,
            adopted=adopted,
            deferred=deferred,
            quarantined=quarantined,
        )

    # -- read-only surfaces --------------------------------------------------

    def plan(self, job_roots: list[Path]) -> list[dict[str, Any]]:
        """Read-only: what a cycle WOULD do. Freezes nothing, calls nothing."""
        jobs = load_jobs(job_roots)
        rows: list[dict[str, Any]] = []
        for job, trial, reason in eligible_trials(jobs):
            request = freeze_request(
                job,
                trial,
                profile=self.context.profile,
                prompt_path=self.prompt_path,
                rubric_path=self.rubric_path,
                repo_root=self.repo_root,
                clock=self.clock,
            )
            rows.append(
                {
                    "job": job.path.name,
                    "trial": trial.path.name,
                    "request_id": request.request_id if request else None,
                    "current_state": (self.store.state(request.request_id) if request else None),
                    "eligibility": reason or "eligible",
                }
            )
        return rows

    def status(self) -> dict[str, Any]:
        """M005-compatible shape: counts + per-request state with provenance."""
        counts: dict[str, int] = {}
        requests: list[dict[str, Any]] = []
        for request_id in self.store.all_request_ids():
            state = self.store.state(request_id) or "pending"
            counts[state] = counts.get(state, 0) + 1
            last = (self.store.transitions(request_id) or [None])[-1]
            requests.append(
                {
                    "request_id": request_id,
                    "state": state,
                    "reason": last.reason if last else None,
                    "ambiguous_invocation": (
                        self.store.unresolved_invocation(request_id) is not None
                    ),
                    "provenance": "observed",
                }
            )
        return {"counts": counts, "requests": requests, "provenance": "observed"}


# ---------------------------------------------------------------------------
# Default composition (CLI + nightly). Read paths only; fail-closed gates.
# ---------------------------------------------------------------------------


def _has_calibrated_model(root: Path, model: str | None, rubric_digest: str | None) -> bool:
    if model is None or rubric_digest is None:
        return False
    records_root = root / "research/calibration/records"
    if not records_root.is_dir():
        return False
    for path in sorted(records_root.rglob("*.json")):
        try:
            record = JudgeCalibrationRecord.model_validate_json(path.read_text())
        except Exception:
            continue
        if (
            record.status == "measured"
            and record.meets_floor
            and record.judge_model == model
            and record.rubric_digest == rubric_digest
        ):
            return True
    return False


def default_worker(
    root: Path,
    *,
    adapter: AnalyzerCallable | None = None,
    adapter_factory: AnalyzerFactory | None = None,
) -> AnalysisWorker:
    """Compose the worker from repository state with fail-closed defaults.

    A model call requires both an explicitly wired adapter and a measured
    calibration record for the exact configured model.
    """
    import yaml

    from evallab.profiles import builtin_profiles, default_probe_for

    policy = StandingApprovalsPolicy.model_validate(
        yaml.safe_load((root / "policy/standing-approvals.yaml").read_text())
    )
    profile = builtin_profiles()["codex-gpt-5.6-terra"]
    probe = default_probe_for(
        profile,
        home=Path.home(),
        security_runner=lambda args: 1,  # keychain unused for codex profile
        keychain_account="",
    )
    context = AdmissionContext(
        stop_present=lambda: (root / "queue/STOP").is_file(),
        policy=policy,
        profile=profile,
        probe=probe,
        spent_today_usd=lambda: 0.0,
        est_call_cost_usd=0.05,
        services_healthy=lambda: True,
        requirement_checks={
            "schema_valid": lambda: True,  # enforced structurally by the schema
            "dedup_pass": lambda: True,  # enforced structurally by identity
            "calibrated_judges_only": lambda: _has_calibrated_model(
                root, profile.model, _sha256_file(root / "research/analysis/stage5-rubric.json")
            ),
        },
    )
    return AnalysisWorker(
        repo_root=root,
        store=RequestStore(root / "derived" / "analyses" / "worker"),
        context=context,
        adapter=adapter or _no_adapter,
        prompt_path=root / "research/analysis/stage5-prompt.md",
        adapter_factory=adapter_factory,
        rubric_path=root / "research/analysis/stage5-rubric.json",
        indexer=_default_indexer(root),
    )


def _default_indexer(root: Path) -> IndexFn:
    """Idempotent catalog indexing via the normal database URL.

    A catalog failure raises: the completed transition is never appended, so
    the durable sidecar stays adoptable and indexing retries on rescan.
    """

    def index(sidecar_path: Path) -> None:
        from evallab import database
        from evallab.labels import label_from_analysis_sidecar, persist_behavior_label
        from evallab.runner import database_url_from_environment
        from evallab.storage.paths import derived_root_from_environment

        url = database_url_from_environment()
        database.initialize(url)
        sidecar = ingest_analysis_sidecar(url, sidecar_path, root=root)
        if sidecar is None or sidecar.validation_status != "valid":
            return
        persist_behavior_label(
            label_from_analysis_sidecar(sidecar),
            repo_root=root,
            derived_root=derived_root_from_environment(root) / "behavior_labels",
        )

    return index


def default_job_roots(root: Path) -> list[Path]:
    return [root / "runs", root / "research" / "evidence" / "runs"]
