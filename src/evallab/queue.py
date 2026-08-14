from __future__ import annotations

import fnmatch
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from evallab import database
from evallab.atif import IngestProjectionResult, ingest_and_project
from evallab.credentials import (
    DEFAULT_AGENT_MODELS,
    available_credentials,
    missing_credential_for,
)
from evallab.results import load_job
from evallab.runner import (
    CONTROL_AGENTS,
    RunRequest,
    database_url_from_environment,
    run_experiment,
    tool_version,
)
from evallab.schemas import (
    ExperimentSpec,
    PolicyDecision,
    QueueEvent,
    QueueReason,
    QueueState,
    RunProvenance,
    StandingApprovalsPolicy,
)

QUEUE_STATES: tuple[QueueState, ...] = (
    "proposed",
    "pending",
    "approved",
    "waiting",
    "rejected",
    "running",
    "done",
    "failed",
)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(*, timestamp_ms: int | None = None, randomness: int | None = None) -> str:
    """Return a lexically sortable ULID without adding a runtime ID dependency."""
    millis = timestamp_ms if timestamp_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    if not 0 <= millis < 2**48:
        raise ValueError("ULID timestamp is outside the 48-bit range")
    random_bits = randomness if randomness is not None else secrets.randbits(80)
    if not 0 <= random_bits < 2**80:
        raise ValueError("ULID randomness is outside the 80-bit range")
    value = (millis << 80) | random_bits
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def load_policy(path: Path) -> StandingApprovalsPolicy:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load standing-approvals policy: {exc}") from exc
    try:
        return StandingApprovalsPolicy.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid standing-approvals policy: {exc}") from exc


class PolicyGate:
    def __init__(self, policy: StandingApprovalsPolicy) -> None:
        self.policy = policy

    def decide(
        self,
        spec: ExperimentSpec,
        *,
        spent_today_usd: float,
        consecutive_harness_failures: int = 0,
        human_approved: bool = False,
    ) -> PolicyDecision:
        if spec.billable:
            if spec.est_cost_usd > self.policy.per_job_cost_ceiling_usd:
                return PolicyDecision(
                    admitted=False,
                    reason_code="per_job_cost_ceiling",
                    message=(
                        f"estimated cost {spec.est_cost_usd:.2f} exceeds per-job ceiling "
                        f"{self.policy.per_job_cost_ceiling_usd:.2f}"
                    ),
                )
            if spent_today_usd + spec.est_cost_usd > self.policy.daily_cost_ceiling_usd:
                return PolicyDecision(
                    admitted=False,
                    reason_code="daily_cost_ceiling",
                    message=(
                        f"estimated daily total {spent_today_usd + spec.est_cost_usd:.2f} "
                        f"exceeds ceiling {self.policy.daily_cost_ceiling_usd:.2f}"
                    ),
                )
            if consecutive_harness_failures >= self.policy.quiet_failure_rule:
                return PolicyDecision(
                    admitted=False,
                    reason_code="quiet_failure_rule",
                    message=(
                        f"{consecutive_harness_failures} consecutive harness failures "
                        "quarantine billable dispatch"
                    ),
                )

        if human_approved:
            return PolicyDecision(
                admitted=True,
                policy_rule="human-approval",
                message="admitted by an explicit human queue approval",
            )

        if spec.environment != "docker":
            return PolicyDecision(
                admitted=False,
                reason_code="cloud_or_remote_environment",
                message="non-Docker environments require human approval",
            )

        for rule in self.policy.auto_run:
            if spec.policy_rule and spec.policy_rule != rule.name:
                continue
            if spec.agent not in rule.agents:
                continue
            if rule.tasks and not any(
                fnmatch.fnmatchcase(spec.task, pattern) for pattern in rule.tasks
            ):
                continue
            if rule.max_attempts is not None and spec.attempts > rule.max_attempts:
                continue
            if not set(rule.requires).issubset(spec.requires):
                continue
            return PolicyDecision(
                admitted=True,
                policy_rule=rule.name,
                message=f"admitted by standing policy rule {rule.name}",
            )

        return PolicyDecision(
            admitted=False,
            reason_code="out_of_policy",
            message="no standing-approvals rule covers this experiment",
        )


class DirectoryQueue:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.reasons_dir = root / "reasons"
        for state in QUEUE_STATES:
            (root / state).mkdir(parents=True, exist_ok=True)
        self.reasons_dir.mkdir(parents=True, exist_ok=True)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def stop_path(self) -> Path:
        return self.root / "STOP"

    def state_dir(self, state: QueueState) -> Path:
        return self.root / state

    def submit(
        self,
        spec: ExperimentSpec,
        *,
        gate: PolicyGate,
        spent_today_usd: float,
        consecutive_harness_failures: int = 0,
    ) -> tuple[Path, PolicyDecision]:
        now = datetime.now(UTC)
        spec_id = spec.spec_id or new_ulid()
        normalized = spec.model_copy(update={"spec_id": spec_id, "submitted_at": now})
        filename = f"{_safe_component(spec.agent)}-{spec_id}.json"
        pending = self.state_dir("pending") / filename
        self._create_exclusive(pending, normalized)
        self.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=now,
                event="submitted",
                to_state="pending",
                actor=spec.submitted_by,
                job_name=spec.name,
            )
        )
        decision = gate.decide(
            normalized,
            spent_today_usd=spent_today_usd,
            consecutive_harness_failures=consecutive_harness_failures,
        )
        if decision.admitted:
            normalized = normalized.model_copy(update={"policy_rule": decision.policy_rule})
            self._replace_model(pending, normalized)
            destination = self.transition(
                pending,
                "approved",
                actor="policy-gate",
                event="policy_admitted",
                policy_rule=decision.policy_rule,
            )
        else:
            destination = self.transition(
                pending,
                "waiting",
                actor="policy-gate",
                event="policy_waiting",
                reason_code=decision.reason_code,
            )
            self.write_reason(normalized, decision)
        return destination, decision

    def load(self, path: Path) -> ExperimentSpec:
        try:
            return ExperimentSpec.model_validate_json(path.read_text())
        except (OSError, ValidationError) as exc:
            raise ValueError(f"Invalid queued experiment {path}: {exc}") from exc

    def locate(self, spec_id: str, states: Iterable[QueueState] = QUEUE_STATES) -> Path:
        matches = [
            path
            for state in states
            for path in self.state_dir(state).glob(f"*-{spec_id}.json")
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one queued spec {spec_id}, found {len(matches)}")
        return matches[0]

    def transition(
        self,
        source: Path,
        destination_state: QueueState,
        *,
        actor: str,
        event: str,
        policy_rule: str | None = None,
        reason_code: str | None = None,
    ) -> Path:
        source_state = source.parent.name
        if source_state not in QUEUE_STATES:
            raise ValueError(f"Unknown source queue state: {source_state}")
        spec = self.load(source)
        destination = self.state_dir(destination_state) / source.name
        if destination.exists():
            raise FileExistsError(f"Queue destination already exists: {destination}")
        source.rename(destination)
        self.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=str(spec.spec_id),
                occurred_at=datetime.now(UTC),
                event=event,
                from_state=cast(QueueState, source_state),
                to_state=destination_state,
                actor=actor,
                policy_rule=policy_rule or spec.policy_rule,
                reason_code=reason_code,
                job_name=spec.name,
            )
        )
        return destination

    def write_reason(self, spec: ExperimentSpec, decision: PolicyDecision) -> Path:
        reason = QueueReason(
            spec_id=str(spec.spec_id),
            occurred_at=datetime.now(UTC),
            code=decision.reason_code or "unspecified",
            message=decision.message,
            policy_rule=decision.policy_rule,
        )
        path = self.reasons_dir / f"{spec.spec_id}-{new_ulid()}.json"
        self._create_exclusive(path, reason)
        return path

    def append_event(self, event: QueueEvent) -> None:
        payload = event.model_dump_json(exclude_none=True) + "\n"
        descriptor = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, payload.encode())
        finally:
            os.close(descriptor)

    def approve(self, spec_id: str, *, actor: str) -> Path:
        source = self.locate(spec_id, ("proposed", "pending", "waiting"))
        spec = self.load(source).model_copy(update={"policy_rule": "human-approval"})
        self._replace_model(source, spec)
        return self.transition(
            source,
            "approved",
            actor=actor,
            event="human_approved",
            policy_rule="human-approval",
        )

    def reject(self, spec_id: str, *, actor: str, message: str) -> Path:
        source = self.locate(spec_id, ("proposed", "pending", "approved", "waiting"))
        spec = self.load(source)
        decision = PolicyDecision(
            admitted=False,
            reason_code="human_rejected",
            message=message,
        )
        destination = self.transition(
            source,
            "rejected",
            actor=actor,
            event="human_rejected",
            reason_code="human_rejected",
        )
        self.write_reason(spec, decision)
        return destination

    def stop(self) -> None:
        self.stop_path.touch(exist_ok=True)

    def resume(self) -> None:
        self.stop_path.unlink(missing_ok=True)

    def list_specs(self, state: QueueState) -> list[tuple[Path, ExperimentSpec]]:
        # Two ticks may run concurrently (launchd schedule plus a manual tick).
        # A file listed here can be claimed — moved to another state — before we
        # read it. That is normal contention, not corruption: skip vanished
        # files instead of failing the whole tick.
        records: list[tuple[Path, ExperimentSpec]] = []
        for path in self.state_dir(state).glob("*.json"):
            try:
                records.append((path, self.load(path)))
            except ValueError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    continue
                raise
        return sorted(
            records,
            key=lambda record: (
                record[1].priority,
                record[1].submitted_at or datetime.min.replace(tzinfo=UTC),
                record[0].name,
            ),
        )

    @staticmethod
    def _create_exclusive(path: Path, model: Any) -> None:
        payload = model.model_dump_json(indent=2, exclude_none=True) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload.encode())
        finally:
            os.close(descriptor)

    @staticmethod
    def _replace_model(path: Path, model: Any) -> None:
        temporary = path.with_name(f".{path.name}.{new_ulid()}.tmp")
        DirectoryQueue._create_exclusive(temporary, model)
        temporary.replace(path)


CredentialProbe = Callable[[], frozenset[str]]
RunCallable = Callable[[RunRequest], Path]
IngestCallable = Callable[[Path], IngestProjectionResult | None]
SpendCallable = Callable[[], float]
FailureCallable = Callable[[], int]


def record_projection_failures(
    queue: DirectoryQueue,
    result: IngestProjectionResult,
    *,
    actor: str,
    spec_id: str,
) -> None:
    for failure in result.failures:
        queue.append_event(
            QueueEvent(
                event_id=new_ulid(),
                spec_id=spec_id,
                occurred_at=datetime.now(UTC),
                event="projection_failed",
                actor=actor,
                reason_code=failure.reason_code,
                job_name=failure.job_name,
            )
        )


class Executor:
    """The sole application boundary allowed to start Harbor experiments."""

    def __init__(
        self,
        *,
        repo_root: Path,
        queue: DirectoryQueue,
        policy: StandingApprovalsPolicy,
        runner: RunCallable | None = None,
        ingester: IngestCallable | None = None,
        spent_today: SpendCallable | None = None,
        consecutive_harness_failures: FailureCallable | None = None,
        credential_probe: CredentialProbe | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.queue = queue
        self.gate = PolicyGate(policy)
        self._runner = runner or self._run_harbor
        self._ingester = ingester or self._ingest
        self._spent_today = spent_today or self._catalog_spend
        self._credential_probe = credential_probe or available_credentials
        self._consecutive_harness_failures = (
            consecutive_harness_failures or self._catalog_harness_failures
        )

    @classmethod
    def from_repo(cls, root: Path) -> Executor:
        return cls(
            repo_root=root,
            queue=DirectoryQueue(root / "queue"),
            policy=load_policy(root / "policy/standing-approvals.yaml"),
        )

    def submit(self, spec: ExperimentSpec) -> tuple[Path, PolicyDecision]:
        return self.queue.submit(
            spec,
            gate=self.gate,
            spent_today_usd=self._spent_today(),
            consecutive_harness_failures=self._consecutive_harness_failures(),
        )

    def tick(self) -> int:
        self.reconcile_running()
        if self.queue.stop_path.exists():
            return 0
        dispatched = 0
        credentials = self._credential_probe()
        for path, spec in self.queue.list_specs("approved"):
            if self.queue.stop_path.exists():
                break
            missing = missing_credential_for(spec.agent, credentials)
            if missing is not None:
                # The spec stays in approved/ and is retried on a later tick;
                # a missing credential is an operator condition, not a policy
                # refusal, so it must not land in waiting/.
                self.queue.append_event(
                    QueueEvent(
                        event_id=new_ulid(),
                        spec_id=spec.spec_id,
                        occurred_at=datetime.now(UTC),
                        event="dispatch_deferred",
                        actor="executor",
                        reason_code=f"missing_credential:{missing}",
                    )
                )
                continue
            human_approved = spec.policy_rule == "human-approval"
            decision = self.gate.decide(
                spec,
                spent_today_usd=self._spent_today(),
                consecutive_harness_failures=self._consecutive_harness_failures(),
                human_approved=human_approved,
            )
            if not decision.admitted:
                waiting = self.queue.transition(
                    path,
                    "waiting",
                    actor="executor",
                    event="dispatch_refused",
                    reason_code=decision.reason_code,
                )
                self.queue.write_reason(self.queue.load(waiting), decision)
                continue
            running = self.queue.transition(
                path,
                "running",
                actor="executor",
                event="dispatch_started",
                policy_rule=decision.policy_rule,
            )
            try:
                job_dir = self.execute_spec(spec)
            except Exception as exc:
                failure = PolicyDecision(
                    admitted=False,
                    reason_code="execution_failed",
                    message=(
                        "execution failed; inspect the immutable job evidence and logs "
                        f"({type(exc).__name__})"
                    ),
                )
                failed = self.queue.transition(
                    running,
                    "failed",
                    actor="executor",
                    event="dispatch_failed",
                    reason_code=failure.reason_code,
                )
                self.queue.write_reason(self.queue.load(failed), failure)
            else:
                try:
                    ingest_result = self._ingester(job_dir)
                except Exception as exc:
                    failure = PolicyDecision(
                        admitted=False,
                        reason_code="catalog_ingest_failed",
                        message=(
                            "completed evidence could not be cataloged; retry ingestion "
                            f"before interpreting the result ({type(exc).__name__})"
                        ),
                    )
                    failed = self.queue.transition(
                        running,
                        "failed",
                        actor="executor",
                        event="catalog_ingest_failed",
                        reason_code=failure.reason_code,
                    )
                    self.queue.write_reason(self.queue.load(failed), failure)
                else:
                    if ingest_result is not None:
                        record_projection_failures(
                            self.queue,
                            ingest_result,
                            actor="executor",
                            spec_id=str(spec.spec_id),
                        )
                    self.queue.transition(
                        running,
                        "done",
                        actor="executor",
                        event="dispatch_completed",
                        policy_rule=decision.policy_rule,
                    )
            dispatched += 1
        return dispatched

    def execute_spec(self, spec: ExperimentSpec) -> Path:
        task = self._safe_repo_path(spec.executable_task_path)
        jobs_dir = self._safe_repo_path(spec.jobs_dir)
        request = RunRequest(
            task=task,
            agent=spec.agent,
            name=spec.name,
            jobs_dir=jobs_dir,
            environment=spec.environment,
            # Harbor's installed agents hard-require a model name; specs that
            # do not pin one fall back to the per-agent default.
            model=spec.model or DEFAULT_AGENT_MODELS.get(spec.agent),
            concurrency=spec.concurrency,
            attempts=spec.attempts,
            allow_billable=spec.billable,
            provenance=RunProvenance(
                spec_id=str(spec.spec_id),
                task=spec.task,
                task_version=spec.task_version,
                verifier_digest=spec.verifier_digest,
                policy_rule=spec.policy_rule,
            ),
        )
        return self._runner(request)

    def download_dataset(self, dataset_ref: str, output_dir: Path) -> Path:
        """Download an immutable Harbor dataset through the executor boundary."""
        if "@" not in dataset_ref:
            raise ValueError("dataset downloads require an explicit immutable version")
        ref = dataset_ref.rsplit("@", 1)[1].lower()
        if ref in {"latest", "head", "main", "master"}:
            raise ValueError("dataset downloads cannot use a mutable ref")
        destination = output_dir.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError(f"dataset download destination is not empty: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "harbor",
                "dataset",
                "download",
                dataset_ref,
                "--output-dir",
                str(destination),
                "--export",
            ],
            cwd=self.repo_root,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Harbor dataset download exited {completed.returncode}")
        return destination

    def execute_direct(self, request: RunRequest, *, ingest: bool = True) -> Path:
        if request.agent not in CONTROL_AGENTS:
            raise ValueError(
                "direct execution is restricted to oracle/nop; submit billable work "
                "through the standing-policy queue"
            )
        job_dir = self._runner(request)
        if ingest:
            ingest_result = self._ingester(job_dir)
            if ingest_result is not None:
                provenance = request.provenance
                record_projection_failures(
                    self.queue,
                    ingest_result,
                    actor="executor-direct",
                    spec_id=(
                        provenance.spec_id
                        if provenance is not None
                        else f"system-{new_ulid()}"
                    ),
                )
        return job_dir

    def local_runtime_checks(self) -> list[tuple[str, bool, str]]:
        """Inspect executable runtimes through the executor's process boundary."""
        checks: list[tuple[str, bool, str]] = []
        for command in ("harbor", "docker", "uv"):
            version = tool_version(command)
            checks.append((command, version is not None, version or "not found"))
        if shutil.which("docker"):
            completed = subprocess.run(
                [
                    "docker",
                    "version",
                    "--format",
                    "client={{.Client.Version}} server={{.Server.Version}}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            output = (completed.stdout or completed.stderr).strip().splitlines()
            detail = output[0] if output else "no version output"
            checks.append(("docker-daemon", completed.returncode == 0, detail))
        return checks

    def reconcile_running(self) -> None:
        for path, spec in self.queue.list_specs("running"):
            job_dir = self._safe_repo_path(spec.jobs_dir) / spec.name
            if not (job_dir / "result.json").is_file():
                continue
            try:
                load_job(job_dir)
                ingest_result = self._ingester(job_dir)
            except Exception:
                continue
            if ingest_result is not None:
                record_projection_failures(
                    self.queue,
                    ingest_result,
                    actor="executor-reconcile",
                    spec_id=str(spec.spec_id),
                )
            self.queue.transition(
                path,
                "done",
                actor="executor-reconcile",
                event="running_reconciled",
                policy_rule=spec.policy_rule,
            )

    def _safe_repo_path(self, relative: str) -> Path:
        candidate = (self.repo_root / relative).resolve()
        if candidate != self.repo_root and self.repo_root not in candidate.parents:
            raise ValueError(f"path escapes repository: {relative}")
        return candidate

    def _run_harbor(self, request: RunRequest) -> Path:
        return run_experiment(request, repo_root=self.repo_root)

    def _ingest(self, job_dir: Path) -> IngestProjectionResult:
        url = database_url_from_environment()
        return ingest_and_project(
            url,
            [load_job(job_dir)],
            root=self.repo_root,
            output_root=self.repo_root / "derived/parquet",
        )

    def _catalog_spend(self) -> float:
        try:
            return database.daily_cost_usd(database_url_from_environment(), date.today())
        except Exception as exc:
            raise RuntimeError(
                "cannot enforce cost policy because the catalog is unavailable"
            ) from exc

    def _catalog_harness_failures(self) -> int:
        try:
            return database.consecutive_harness_failures(database_url_from_environment())
        except Exception as exc:
            raise RuntimeError(
                "cannot enforce failure policy because the catalog is unavailable"
            ) from exc


def _safe_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "-" for character in value.lower())
    return cleaned.strip("-") or "agent"


def load_events(path: Path) -> list[QueueEvent]:
    if not path.is_file():
        return []
    events: list[QueueEvent] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(QueueEvent.model_validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"Invalid queue event at {path}:{line_number}: {exc}") from exc
    return events


def read_spec(path: Path) -> ExperimentSpec:
    return ExperimentSpec.model_validate_json(path.read_text())


def write_spec(path: Path, spec: ExperimentSpec) -> None:
    path.write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
