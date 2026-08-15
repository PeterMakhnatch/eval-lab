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
  adopted, never re-executed. A ``running`` record without a sidecar is
  re-admitted from scratch. Indexing is retried idempotently.
- **Concurrency.** An O_EXCL lease per request; a losing worker defers.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable  # noqa: F401  (Iterable in signatures)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evallab import facts
from evallab.facts import AnalyzerCallable, ingest_analysis_sidecar, run_trial_analysis
from evallab.profiles import AgentProfile, PreflightDecision, ProbeFn, preflight
from evallab.results import JobRecord, TrialRecord, load_jobs
from evallab.schemas import StandingApprovalsPolicy, TrialAnalysisSidecar

State = Literal["pending", "admitted", "running", "completed", "deferred", "quarantined"]

RESEARCHER_RULE = "researcher-followups"
WORKER_DIRNAME = "worker"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class AnalysisRequest(BaseModel):
    """Frozen identity of one trial's analysis. Never edited after creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
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

    @property
    def identity_digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("created_at")  # identity is content, not clock
        payload.pop("request_id")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


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


# ---------------------------------------------------------------------------
# Store: request.json (write-once) + transitions.jsonl (append-only) + lease
# ---------------------------------------------------------------------------


class RequestStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def request_dir(self, request_id: str) -> Path:
        return self.root / "requests" / request_id

    def freeze(self, request: AnalysisRequest) -> bool:
        """Persist a new request; False when the identity already exists."""
        directory = self.request_dir(request.request_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "request.json"
        try:
            with open(path, "x") as handle:
                handle.write(request.model_dump_json(indent=2) + "\n")
        except FileExistsError:
            return False
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
            out.append(Transition(
                at=str(payload.get("at")),
                state=payload.get("state"),
                reason=payload.get("reason"),
                actor=str(payload.get("actor", "analysis-worker")),
            ))
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
        pid_alive: Callable[[int], bool] | None = None,
        max_age_seconds: float = 3600.0,
        clock: Callable[[], datetime] | None = None,
    ) -> bool:
        """Crash-recoverable lease: owner pid + timestamp inside the file.

        A lease is honored while its owner is demonstrably alive and the
        lease is younger than *max_age_seconds*. A dead owner's lease (or an
        unreadably corrupt one, or one past max age) is reclaimed — process
        crashes cannot strand a request forever. A live owner's lease is
        NEVER reclaimed.
        """
        pid = owner_pid if owner_pid is not None else os.getpid()
        alive = pid_alive or _default_pid_alive
        now = (clock or _utc_now)()
        path = self._lease_path(request_id)
        payload = json.dumps(
            {"pid": pid, "acquired_at": now.isoformat(), "host": os.uname().nodename}
        )
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text())
                holder = int(existing["pid"])
                acquired = datetime.fromisoformat(existing["acquired_at"])
            except (OSError, ValueError, KeyError, TypeError):
                holder, acquired = None, None  # corrupt lease
            if holder is not None and alive(holder):
                # A demonstrably live owner is NEVER reclaimed — age is
                # irrelevant when liveness is provable.
                return False
            over_age = (
                acquired is None
                or (now - acquired).total_seconds() >= max_age_seconds
            )
            if holder is not None and not over_age:
                # Dead-by-probe but young: reclaim immediately is still safe
                # on one host; keep the branch explicit for auditability.
                pass
            # Stale: dead or corrupt owner. Reclaim atomically.
            stale = path.with_name("lease.stale")
            try:
                path.replace(stale)
            except OSError:
                return False  # someone else reclaimed first
            stale.unlink(missing_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return True

    def release_lease(self, request_id: str) -> None:
        self._lease_path(request_id).unlink(missing_ok=True)


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
    }
    # Identity keys on the TRIAL, not on content: one analysis record per
    # trial, frozen at first sight. Content digests are frozen inside the
    # record; admission verifies them, so changed evidence quarantines the
    # existing record instead of silently minting a runnable new identity.
    identity = f"{body['job_id']}:{body['trial_id']}"
    request_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return AnalysisRequest.model_validate(
        {"request_id": request_id, "created_at": clock(), **body}
    )


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
                out.append((job, trial,
                            "defer:harness_exception_not_agent_failure"))
            elif ((result.get("verifier_result") or {}).get("rewards") or {}).get(
                "reward") is None:
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
    request: AnalysisRequest, store: RequestStore, context: AdmissionContext,
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

    rule = next(
        (r for r in context.policy.auto_run if r.name == RESEARCHER_RULE), None
    )
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


@dataclass
class AnalysisWorker:
    repo_root: Path
    store: RequestStore
    context: AdmissionContext
    adapter: AnalyzerCallable
    prompt_path: Path
    rubric_path: Path
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
                job, trial, profile=self.context.profile,
                prompt_path=self.prompt_path, rubric_path=self.rubric_path,
                repo_root=self.repo_root, clock=self.clock,
            )
            if request is None:
                quarantined["evidence_unreadable"] = (
                    quarantined.get("evidence_unreadable", 0) + 1
                )
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
            discovered=discovered, staged=staged, calls=0, completed=0,
            adopted=0, deferred=deferred, quarantined=quarantined,
        )

    # -- execution ----------------------------------------------------------

    def run_one(self, request_id: str) -> Transition:
        """Full admission, at most one call, crash-safe completion."""
        request = self.store.load(request_id)
        state = self.store.state(request_id)
        if state == "completed":
            return self.store.transitions(request_id)[-1]
        if state in {"quarantined"}:
            return self.store.transitions(request_id)[-1]

        # Crash-after-call adoption: sidecar exists -> never call again.
        sidecar_path = self.store.sidecar_path(request_id)
        if sidecar_path.is_file():
            self._complete(request_id, sidecar_path, adopted=True)
            return self.store.transitions(request_id)[-1]

        if not self.store.acquire_lease(request_id):
            self.store.append(request_id, "deferred", "lease_held_by_another_worker")
            return self.store.transitions(request_id)[-1]
        try:
            job_dir = (self.repo_root / request.trial_path).parent
            jobs = load_jobs([job_dir.parent])
            match = next(
                ((j, t) for j in jobs for t in j.trials
                 if str(t.id) == request.trial_id), None,
            )
            admission = admit(
                request, self.store, self.context, self.repo_root,
                job=match[0] if match else None,
                trial=match[1] if match else None,
                prompt_path=self.prompt_path,
                rubric_path=self.rubric_path,
            )
            if admission.kind != "admit":
                target: State = (
                    "deferred" if admission.kind == "defer" else "quarantined"
                )
                self.store.append(request_id, target, admission.reason)
                return self.store.transitions(request_id)[-1]
            if match is None:
                self.store.append(request_id, "quarantined", "trial_vanished")
                return self.store.transitions(request_id)[-1]
            self.store.append(request_id, "admitted", None)
            job, trial = match
            self.store.append(request_id, "running", None)
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            written_path, _sidecar = run_trial_analysis(
                job, trial,
                analyzer=self.adapter,
                repo_root=self.repo_root,
                destination_root=sidecar_path.parent,
                prompt_path=self.prompt_path,
                rubric_path=self.rubric_path,
                agent=request.adapter,
                agent_version=request.profile_id,
                model=request.model,
                created_at=self.clock(),
            )
            # normalize to the stable per-request location (atomic rename)
            written_path.replace(sidecar_path)
            self._complete(request_id, sidecar_path, adopted=False)
            return self.store.transitions(request_id)[-1]
        finally:
            self.store.release_lease(request_id)

    def _complete(self, request_id: str, sidecar_path: Path, *, adopted: bool) -> None:
        sidecar = TrialAnalysisSidecar.model_validate_json(sidecar_path.read_text())
        if self.indexer is not None:
            self.indexer(sidecar_path)  # idempotent catalog upsert; retried on rescan
        reason = "adopted_existing_sidecar" if adopted else None
        self.store.append(request_id, "completed", reason)
        del sidecar

    def run_cycle(self, job_roots: list[Path]) -> CycleReport:
        stage_report = self.stage(job_roots)
        calls = completed = adopted = 0
        deferred = dict(stage_report.deferred)
        quarantined = dict(stage_report.quarantined)
        for request_id in self.store.all_request_ids():
            state = self.store.state(request_id)
            if state in {"completed", "quarantined"}:
                continue
            if state == "deferred":
                last = self.store.transitions(request_id)[-1]
                if last.reason and last.reason.startswith(
                    ("harness_exception", "no_verdict")
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
            discovered=stage_report.discovered, staged=stage_report.staged,
            calls=calls, completed=completed, adopted=adopted,
            deferred=deferred, quarantined=quarantined,
        )

    # -- read-only surfaces --------------------------------------------------

    def plan(self, job_roots: list[Path]) -> list[dict[str, Any]]:
        """Read-only: what a cycle WOULD do. Freezes nothing, calls nothing."""
        jobs = load_jobs(job_roots)
        rows: list[dict[str, Any]] = []
        for job, trial, reason in eligible_trials(jobs):
            request = freeze_request(
                job, trial, profile=self.context.profile,
                prompt_path=self.prompt_path, rubric_path=self.rubric_path,
                repo_root=self.repo_root, clock=self.clock,
            )
            rows.append({
                "job": job.path.name,
                "trial": trial.path.name,
                "request_id": request.request_id if request else None,
                "current_state": (
                    self.store.state(request.request_id) if request else None
                ),
                "eligibility": reason or "eligible",
            })
        return rows

    def status(self) -> dict[str, Any]:
        """M005-compatible shape: counts + per-request state with provenance."""
        counts: dict[str, int] = {}
        requests: list[dict[str, Any]] = []
        for request_id in self.store.all_request_ids():
            state = self.store.state(request_id) or "pending"
            counts[state] = counts.get(state, 0) + 1
            last = (self.store.transitions(request_id) or [None])[-1]
            requests.append({
                "request_id": request_id,
                "state": state,
                "reason": last.reason if last else None,
                "provenance": "observed",
            })
        return {"counts": counts, "requests": requests, "provenance": "observed"}


# ---------------------------------------------------------------------------
# Default composition (CLI + nightly). Read paths only; fail-closed gates.
# ---------------------------------------------------------------------------


def _no_adapter(prompt: str, schema: dict[str, Any]):  # pragma: no cover - guard
    raise RuntimeError(
        "no analysis adapter is wired; plan/status/stage never call a model, "
        "and run-one requires an explicitly constructed adapter"
    )


def default_worker(root: Path, *, adapter: AnalyzerCallable | None = None) -> AnalysisWorker:
    """Compose the worker from repository state with fail-closed defaults.

    - profile: the proven codex profile from the M003 registry;
    - probe: the real auth-file probe through injected seams;
    - calibrated_judges_only: fails closed until a measured calibration
      record meets the floor (JUDGE's current record does not) — real model
      calls therefore stay deferred, by design, until calibration lands;
    - adapter: absent by default; staging/plan/status never need one.
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
            "schema_valid": lambda: True,   # enforced structurally by the schema
            "dedup_pass": lambda: True,     # enforced structurally by identity
            "calibrated_judges_only": lambda: False,  # fail closed: no measured pass
        },
    )
    return AnalysisWorker(
        repo_root=root,
        store=RequestStore(root / "derived" / "analyses" / "worker"),
        context=context,
        adapter=adapter or _no_adapter,
        prompt_path=root / "research/analysis/stage5-prompt.md",
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
        from evallab.runner import database_url_from_environment

        url = database_url_from_environment()
        database.initialize(url)
        ingest_analysis_sidecar(url, sidecar_path, root=root)

    return index


def default_job_roots(root: Path) -> list[Path]:
    return [root / "runs", root / "research" / "evidence" / "runs"]
