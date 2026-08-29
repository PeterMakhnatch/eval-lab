from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evallab import cli
from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
    SyntheticFamilyType,
)
from evallab.campaigns import (
    CampaignAmbiguityError,
    CampaignDefinition,
    CampaignDefinitionAttempt,
    CampaignDriftError,
    CampaignLimits,
    CampaignManifest,
    CampaignOrchestrator,
    CampaignSecretSanitizer,
    CampaignStore,
    TrialLimits,
    build_campaign_manifest,
)
from evallab.credentials import DEEPSEEK_API_CREDENTIAL
from evallab.execution_contracts import (
    DEEPSEEK_MODEL_SELECTOR,
    DispatchCapacity,
    RunRequest,
    TransientHarnessFailure,
)
from evallab.queue import DirectoryQueue, Executor, load_policy
from evallab.schemas import ExperimentSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "policy").mkdir(parents=True)
    (root / "policy/standing-approvals.yaml").write_text(
        (REPO_ROOT / "policy/standing-approvals.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    task = root / "tasks/task-one"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
    return root


def _spec(*, billable: bool) -> ExperimentSpec:
    return ExperimentSpec(
        name="definition-placeholder",
        hypothesis="Campaign orchestration preserves exact attempts",
        purpose="baseline",
        task="tasks/task-one",
        task_id="task-one",
        agent="mini-swe-agent" if billable else "oracle",
        model=DEEPSEEK_MODEL_SELECTOR if billable else None,
        jobs_dir="runs",
        attempts=1,
        concurrency=1,
        timeout_seconds=60,
        submitted_by="campaign-test",
        est_cost_usd=0.5 if billable else 0.0,
        requires=["fresh-deepseek-key"] if billable else [],
    )


def _definition(
    *,
    billable: bool,
    attempts: int = 1,
    campaign_cost: float | None = None,
    circuit_failures: int = 1,
    max_concurrency: int = 1,
) -> CampaignDefinition:
    trial = TrialLimits(
        max_cost_usd=1.0 if billable else 0.0,
        max_input_tokens=100 if billable else 0,
        max_output_tokens=100 if billable else 0,
        max_total_tokens=200 if billable else 0,
        max_wall_clock_seconds=60,
    )
    ledger = (
        CampaignMeasurementLedger(
            ledger_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            matrix_ref="01ARZ3NDEKTSV4RRFFQ69G5FAW",
            campaign_phase="billable_cohort",
            family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
            status="pending",
        )
        if billable
        else CampaignCalibrationLedger(
            ledger_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            matrix_ref="01ARZ3NDEKTSV4RRFFQ69G5FAW",
            family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
            status="pending",
        )
    )
    return CampaignDefinition(
        ledger=ledger,
        submitted_by="campaign-test",
        limits=CampaignLimits(
            max_cost_usd=(attempts if billable else 0.0)
            if campaign_cost is None
            else campaign_cost,
            max_input_tokens=100 * attempts if billable else 0,
            max_output_tokens=100 * attempts if billable else 0,
            max_total_tokens=200 * attempts if billable else 0,
            max_wall_clock_seconds=60 * attempts,
            max_concurrency=max_concurrency,
            max_consecutive_transient_failures=circuit_failures,
        ),
        attempts=tuple(
            CampaignDefinitionAttempt(
                cell_id=f"cell-{index}",
                task_id="task-one",
                attempt=index,
                spec=_spec(billable=billable),
                limits=trial,
            )
            for index in range(1, attempts + 1)
        ),
    )


def _write_job(
    request: RunRequest,
    *,
    cost: float | None = 0.0,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
) -> Path:
    job = request.jobs_dir / request.name
    trial_name = "task-one__trial"
    trial = job / trial_name
    trial.mkdir(parents=True)
    started = "2026-08-28T12:00:00+00:00"
    finished = "2026-08-28T12:00:01+00:00"
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": f"job-{request.name}",
                "finished_at": finished,
                "n_total_trials": 1,
                "stats": {},
            }
        ),
        encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": f"trial-{request.name}",
                "task_name": "evallab/task-one",
                "trial_name": trial_name,
                "started_at": started,
                "finished_at": finished,
                "agent_result": {
                    "n_input_tokens": input_tokens,
                    "n_output_tokens": output_tokens,
                    "cost_usd": cost,
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
                "exception_info": None,
            }
        ),
        encoding="utf-8",
    )
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiment": request.provenance.model_dump(mode="json")
                if request.provenance
                else None,
            }
        ),
        encoding="utf-8",
    )
    return job


def _executor(
    root: Path,
    runner: Any,
    *,
    capacity: DispatchCapacity | None = None,
    credentials: frozenset[str] = frozenset(),
) -> Executor:
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=load_policy(root / "policy/standing-approvals.yaml"),
        runner=runner,
        ingester=lambda _job: None,
        spent_today=lambda: 0.0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: credentials,
        sleeper=lambda _seconds: None,
        max_transient_retries=0,
        parallel=1,
        capacity=capacity or DispatchCapacity(max_specs_per_tick=1, max_active_trials=1),
    )


def _orchestrator(
    root: Path,
    manifest: CampaignManifest,
    executor: Executor,
    *,
    sanitizer: CampaignSecretSanitizer | None = None,
    backfill_hook: Any | None = None,
) -> CampaignOrchestrator:
    return CampaignOrchestrator(
        repo_root=root,
        manifest=manifest,
        state_root=root / "runs/campaigns",
        executor=executor,
        dispatch=lambda current, spec_ids: current.tick(spec_ids=spec_ids),
        backfill_hook=backfill_hook,
        sanitizer=sanitizer or CampaignSecretSanitizer(frozenset()),
        credential_probe=executor._credential_probe,
        clock=Clock(),
    )


def test_dry_run_is_read_only_and_billable_defaults_to_no_dispatch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    calls: list[RunRequest] = []
    executor = _executor(root, lambda request: calls.append(request))
    orchestrator = _orchestrator(root, manifest, executor)

    status = orchestrator.run(dry_run=True)

    assert status.dry_run is True
    assert status.state == "planned"
    assert status.attempts[0].queue_state == "planned"
    assert calls == []
    assert executor.queue.list_specs("waiting") == []
    assert not orchestrator.store.journal_path.exists()
    assert not orchestrator.store.root.exists()


def test_campaign_refuses_reserved_budget_above_ceiling() -> None:
    with pytest.raises(
        ValidationError,
        match="reserved per-trial cost ceilings exceed the campaign ceiling",
    ):
        _definition(billable=True, attempts=2, campaign_cost=1.5)


def test_billable_run_waits_for_explicit_approval_then_resume_is_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    calls: list[str] = []

    def runner(request: RunRequest) -> Path:
        assert request.max_output_tokens == 100
        assert request.cost_limit_usd == 1.0
        assert request.attempts == request.concurrency == 1
        assert request.provenance is not None
        assert request.provenance.campaign_ledger == manifest.ledger
        assert request.provenance.campaign_manifest_digest == manifest.manifest_digest
        assert request.lease_path is not None
        assert request.lease_path.is_file()
        calls.append(request.name)
        return _write_job(request, cost=0.25, input_tokens=40, output_tokens=20)

    executor = _executor(
        root,
        runner,
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    first = _orchestrator(root, manifest, executor)

    waiting = first.run()
    assert waiting.state == "waiting-approval"
    assert calls == []
    assert waiting.attempts[0].approval_command == (
        f"uv run evallab approve {manifest.attempts[0].spec_id} --actor <you>"
    )

    assert (
        cli.run_cli(
            [
                "approve",
                manifest.attempts[0].spec_id,
                "--actor",
                "Peter Makhnatch",
            ],
            workspace=root,
        )
        == 0
    )
    approval_output = capsys.readouterr().out
    assert (
        f"next: uv run evallab campaign resume runs/campaigns/{manifest.campaign_id}/manifest.json"
    ) in approval_output
    assert "\nnext: uv run evallab tick\n" not in approval_output
    assert executor.tick() == 0
    assert executor.last_tick_reason == "campaign_specs_require_campaign_resume"
    assert calls == []
    completed = first.resume()
    assert completed.state == "completed"
    assert calls == [manifest.attempts[0].job_name]
    assert completed.attempts[0].cas_uri is not None

    resumed = _orchestrator(root, manifest, executor).resume()
    assert resumed.state == "completed"
    assert calls == [manifest.attempts[0].job_name]


def test_resume_refuses_queued_spec_digest_drift(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()

    path, queued = executor.queue.list_specs("waiting")[0]
    payload = queued.model_dump(mode="json", exclude_none=True)
    payload["hypothesis"] = "drifted after planning"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CampaignDriftError, match="queued campaign spec drifted"):
        _orchestrator(root, manifest, executor).resume()


def test_partial_journal_record_refuses_resume(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.store.root.mkdir(parents=True)
    orchestrator.store.journal_lock_path.touch()
    orchestrator.store.journal_path.write_text('{"partial":', encoding="utf-8")

    with pytest.raises(CampaignAmbiguityError, match="partial record"):
        orchestrator.resume()


def test_campaign_journal_readers_take_a_shared_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.store.freeze(manifest)
    orchestrator.store.append(
        manifest,
        event="probe",
        sanitizer=orchestrator.sanitizer,
        occurred_at=NOW,
    )
    finished = threading.Event()
    result: list[Any] = []

    def read_events() -> None:
        result.extend(orchestrator.store.events(manifest))
        finished.set()

    with orchestrator.store.journal_lock_path.open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        reader = threading.Thread(target=read_events)
        reader.start()
        assert not finished.wait(0.05)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    reader.join(timeout=1)

    assert finished.is_set()
    assert [event.event for event in result] == ["probe"]


def test_status_does_not_create_missing_queue_event_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "queue").mkdir()
    manifest = build_campaign_manifest(_definition(billable=False))
    executor = _executor(root, lambda request: _write_job(request))

    status = _orchestrator(root, manifest, executor).status()

    assert status.state == "planned"
    assert not (root / "queue/.events.lock").exists()


def test_run_reloads_started_state_after_acquiring_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)

    @contextmanager
    def raced_lease(_manifest: CampaignManifest, *, now: datetime) -> Any:
        orchestrator.store.append(
            manifest,
            event="campaign_started",
            sanitizer=orchestrator.sanitizer,
            occurred_at=now,
        )
        yield

    monkeypatch.setattr(orchestrator.store, "lease", raced_lease)

    with pytest.raises(CampaignAmbiguityError, match="already started"):
        orchestrator.run()


def test_transient_failure_opens_circuit_before_next_attempt(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False, attempts=2, circuit_failures=1))
    calls: list[str] = []

    def transient(request: RunRequest) -> Path:
        calls.append(request.name)
        raise TransientHarnessFailure("transient_harness:provider_http_429")

    executor = _executor(
        root,
        transient,
        capacity=DispatchCapacity(max_specs_per_tick=1, max_active_trials=1),
    )
    status = _orchestrator(root, manifest, executor).run()

    assert status.state == "circuit-open"
    assert status.circuit_reason == "transient_failure_circuit_breaker"
    assert calls == [manifest.attempts[0].job_name]
    assert status.attempts[1].queue_state == "rejected"
    assert executor.queue.list_specs("approved") == []
    assert executor.tick() == 0
    assert calls == [manifest.attempts[0].job_name]


def test_campaign_journal_redacts_secrets_and_archive_scan_fails_closed(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    secret = "deepseek-secret-never-persist"
    sanitizer = CampaignSecretSanitizer(frozenset({secret}))
    manifest = build_campaign_manifest(_definition(billable=True))
    store = CampaignStore(root / "runs/campaigns", manifest.campaign_id)
    store.freeze(manifest)
    store.append(
        manifest,
        event="probe",
        sanitizer=sanitizer,
        occurred_at=NOW,
        details={"message": f"credential={secret}"},
    )

    journal = store.journal_path.read_text(encoding="utf-8")
    assert secret not in journal
    assert "<redacted>" in journal

    store.journal_path.write_text(
        journal.replace('"event":"probe"', '"event":"tampered"'),
        encoding="utf-8",
    )
    with pytest.raises(CampaignAmbiguityError, match="record 1 is invalid"):
        store.events(manifest)

    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "leak.log").write_text(secret, encoding="utf-8")
    with pytest.raises(Exception, match="credential material detected") as caught:
        sanitizer.assert_tree_safe(evidence)
    assert secret not in str(caught.value)


def test_manifest_rejects_cross_ledger_attempt_identity() -> None:
    manifest = build_campaign_manifest(_definition(billable=False))
    payload = manifest.model_dump(mode="json")
    payload["ledger"]["ledger_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAX"

    with pytest.raises(ValidationError, match="cross-ledger"):
        CampaignManifest.model_validate(payload)


def test_campaign_manifest_and_job_names_are_deterministic() -> None:
    definition = _definition(billable=False, attempts=2)
    first = build_campaign_manifest(definition)
    second = build_campaign_manifest(definition)

    assert first == second
    assert first.manifest_digest == second.manifest_digest
    assert [attempt.job_name for attempt in first.attempts] == [
        attempt.job_name for attempt in second.attempts
    ]
    assert len({attempt.job_name for attempt in first.attempts}) == 2


def test_campaign_cli_plan_status_and_dry_run_are_operational(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    definition_path = root / "campaign.json"
    definition_path.write_text(
        _definition(billable=True).model_dump_json(indent=2),
        encoding="utf-8",
    )

    assert (
        cli.run_cli(
            ["campaign", "plan", "campaign.json", "--json"],
            workspace=root,
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    manifest_path = root / plan["manifest_path"]
    assert manifest_path.is_file()
    assert not (root / "queue").exists()

    assert (
        cli.run_cli(
            [
                "campaign",
                "run",
                plan["manifest_path"],
                "--dry-run",
                "--json",
            ],
            workspace=root,
        )
        == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["state"] == "planned"
    assert dry_run["dry_run"] is True
    assert not (root / "queue").exists()
    assert not (manifest_path.parent / "journal.jsonl").exists()

    assert (
        cli.run_cli(
            ["campaign", "status", plan["manifest_path"], "--json"],
            workspace=root,
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "planned"


def test_missing_credential_blocks_without_duplicate_events_then_recovers(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    calls: list[str] = []

    def runner(request: RunRequest) -> Path:
        calls.append(request.name)
        return _write_job(request, cost=0.25, input_tokens=40, output_tokens=20)

    blocked_executor = _executor(root, runner)
    orchestrator = _orchestrator(root, manifest, blocked_executor)
    orchestrator.run()
    blocked_executor.queue.approve(
        manifest.attempts[0].spec_id,
        actor="Peter Makhnatch",
    )

    blocked = orchestrator.resume()
    assert blocked.state == "blocked-credential"
    assert blocked.block_reason == "missing_credential:deepseek_api_environment"
    assert calls == []
    orchestrator.resume()
    events = orchestrator.store.events(manifest)
    assert sum(event.event == "credential_preflight_refused" for event in events) == 1

    ready_executor = _executor(
        root,
        runner,
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    completed = _orchestrator(root, manifest, ready_executor).resume()
    assert completed.state == "completed"
    assert completed.block_reason is None
    assert calls == [manifest.attempts[0].job_name]


def test_observed_trial_overage_opens_campaign_circuit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))

    def runner(request: RunRequest) -> Path:
        return _write_job(request, cost=1.25, input_tokens=40, output_tokens=20)

    executor = _executor(
        root,
        runner,
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")

    status = orchestrator.resume()
    assert status.state == "circuit-open"
    assert status.circuit_reason == "trial_cost_ceiling_exceeded"
    assert status.completed_attempts == 1


def test_resume_rejects_tampered_campaign_cas_archive(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    executor = _executor(
        root,
        lambda request: _write_job(
            request,
            cost=0.25,
            input_tokens=40,
            output_tokens=20,
        ),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")
    completed = orchestrator.resume()
    uri = completed.attempts[0].cas_uri
    assert uri is not None
    digest = uri.removeprefix("cas://sha256/")
    blob = root / manifest.evidence_store / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz"
    blob.write_bytes(b"tampered")
    with pytest.raises(CampaignDriftError, match="CAS archive digest mismatch"):
        _orchestrator(root, manifest, executor).status()

    with pytest.raises(CampaignDriftError, match="CAS archive digest mismatch"):
        _orchestrator(root, manifest, executor).resume()


def test_partial_custom_backfill_is_not_replayed_on_resume(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    executor = _executor(root, lambda request: _write_job(request))
    hook_calls = 0

    def partial_backfill(
        _manifest: CampaignManifest,
        _attempt: Any,
        _job_dir: Path,
    ) -> dict[str, Any]:
        nonlocal hook_calls
        hook_calls += 1
        raise RuntimeError("simulated partial backfill")

    orchestrator = _orchestrator(
        root,
        manifest,
        executor,
        backfill_hook=partial_backfill,
    )
    with pytest.raises(RuntimeError, match="simulated partial backfill"):
        orchestrator.run()
    assert hook_calls == 1

    with pytest.raises(CampaignAmbiguityError, match="partially completed"):
        _orchestrator(
            root,
            manifest,
            executor,
            backfill_hook=partial_backfill,
        ).resume()
    assert hook_calls == 1


def test_campaign_rejects_unbounded_executor_capacity(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    executor = _executor(root, lambda request: _write_job(request))
    executor.capacity = None

    with pytest.raises(ValueError, match="executor capacity exceeds"):
        CampaignOrchestrator(
            repo_root=root,
            manifest=manifest,
            state_root=root / "runs/campaigns",
            executor=executor,
        )

    with pytest.raises(ValueError, match="requested parallelism exceeds"):
        CampaignOrchestrator(
            repo_root=root,
            manifest=manifest,
            state_root=root / "runs/campaigns",
            requested_parallel=0,
        )
    with pytest.raises(CampaignAmbiguityError, match="state root must be"):
        CampaignOrchestrator(
            repo_root=root,
            manifest=manifest,
            state_root=root / "alternate-campaign-state",
        )


def test_campaign_rejects_evidence_store_symlink_escape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    outside = tmp_path / "outside-evidence"
    outside.mkdir()
    store_path = root / manifest.evidence_store
    store_path.parent.mkdir(parents=True)
    store_path.symlink_to(outside, target_is_directory=True)
    executor = _executor(root, lambda request: _write_job(request))

    with pytest.raises(CampaignDriftError, match="evidence store resolves outside"):
        _orchestrator(root, manifest, executor)._evidence_store_root()


def test_billable_campaign_parallelism_is_serialized_before_policy_dispatch(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True, attempts=2, max_concurrency=2))

    orchestrator = CampaignOrchestrator(
        repo_root=root,
        manifest=manifest,
        state_root=root / "runs/campaigns",
    )

    assert manifest.limits.max_concurrency == 2
    assert orchestrator.executor.parallel == 1
    assert orchestrator.executor.capacity == DispatchCapacity(
        max_specs_per_tick=1,
        max_active_trials=1,
        per_agent_active_trials={"mini-swe-agent": 1},
    )


def test_campaign_dispatch_does_not_execute_foreign_approved_specs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    calls: list[str] = []

    def runner(request: RunRequest) -> Path:
        calls.append(request.name)
        return _write_job(request)

    executor = _executor(root, runner)
    foreign = ExperimentSpec(
        name="foreign-priority-job",
        spec_id="foreign-approved-spec",
        hypothesis="must not be claimed by another campaign tick",
        purpose="baseline",
        task="tasks/task-one",
        task_id="task-one",
        agent="oracle",
        jobs_dir="runs",
        attempts=1,
        concurrency=1,
        timeout_seconds=60,
        submitted_by="other-operator",
        est_cost_usd=0.0,
        priority=1,
    )
    destination, decision = executor.submit(foreign)
    assert decision.admitted is True
    assert destination.parent.name == "approved"

    status = _orchestrator(root, manifest, executor).run()

    assert status.state == "completed"
    assert calls == [manifest.attempts[0].job_name]
    remaining = executor.queue.list_specs("approved")
    assert [spec.spec_id for _path, spec in remaining] == ["foreign-approved-spec"]


def test_targeted_campaign_tick_preserves_global_running_barrier(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    calls: list[str] = []
    executor = _executor(
        root,
        lambda request: calls.append(request.name) or _write_job(request),
    )
    foreign = ExperimentSpec(
        name="foreign-running-job",
        spec_id="foreign-running-spec",
        hypothesis="global running work blocks every targeted tick",
        purpose="baseline",
        task="tasks/task-one",
        task_id="task-one",
        agent="oracle",
        jobs_dir="runs",
        timeout_seconds=60,
        submitted_by="other-operator",
    )
    destination, decision = executor.submit(foreign)
    assert decision.admitted is True
    executor.queue.transition(
        destination,
        "running",
        actor="other-executor",
        event="claimed",
    )

    status = _orchestrator(root, manifest, executor).run()

    assert status.state == "ready"
    assert calls == []
    assert executor.last_tick_reason == "running_specs_unresolved"


def test_status_binds_frozen_manifest_and_unreconciled_done_is_not_planned(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False))
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    completed = orchestrator.run()
    assert completed.state == "completed"

    payload = _definition(billable=False).model_dump(mode="json")
    payload["attempts"][0]["spec"]["hypothesis"] = "a different frozen identity"
    drifted = build_campaign_manifest(CampaignDefinition.model_validate(payload))
    assert drifted.campaign_id == manifest.campaign_id
    assert drifted.manifest_digest != manifest.manifest_digest
    with pytest.raises(CampaignDriftError, match="frozen campaign manifest differs"):
        CampaignOrchestrator(
            repo_root=root,
            manifest=drifted,
            state_root=root / "runs/campaigns",
            executor=executor,
            dispatch=lambda current, spec_ids: current.tick(spec_ids=spec_ids),
            sanitizer=CampaignSecretSanitizer(frozenset()),
            credential_probe=executor._credential_probe,
            clock=Clock(),
        ).status()

    orchestrator.store.journal_path.unlink()
    unreconciled = _orchestrator(root, manifest, executor).status()
    assert unreconciled.state == "running"
    assert unreconciled.attempts[0].queue_state == "done"
    assert unreconciled.attempts[0].completed is False


def test_resume_rejects_rewritten_cas_blob_and_record_digest(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    executor = _executor(
        root,
        lambda request: _write_job(
            request,
            cost=0.25,
            input_tokens=40,
            output_tokens=20,
        ),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")
    completed = orchestrator.resume()
    uri = completed.attempts[0].cas_uri
    assert uri is not None
    digest = uri.removeprefix("cas://sha256/")
    store_root = root / manifest.evidence_store
    blob = store_root / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz"
    blob.write_bytes(b"tampered-archive-bytes")
    record_path = store_root / "records/campaign-job" / f"{manifest.attempts[0].attempt_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["archive_digest"] = "sha256:" + hashlib.sha256(b"tampered-archive-bytes").hexdigest()
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(CampaignDriftError, match="archive event does not match"):
        _orchestrator(root, manifest, executor).resume()


def test_resume_rejects_valid_archive_with_wrong_content_digest(tmp_path: Path) -> None:
    from evallab.evidence_store import archive_evidence

    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True))
    executor = _executor(
        root,
        lambda request: _write_job(
            request,
            cost=0.25,
            input_tokens=40,
            output_tokens=20,
        ),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")
    completed = orchestrator.resume()
    uri = completed.attempts[0].cas_uri
    assert uri is not None
    digest = uri.removeprefix("cas://sha256/")
    store_root = root / manifest.evidence_store
    blob = store_root / "blobs/sha256" / digest[:2] / f"{digest}.tar.gz"
    wrong_job = tmp_path / "wrong-job"
    wrong_job.mkdir()
    (wrong_job / "result.json").write_text('{"wrong":true}\n', encoding="utf-8")
    wrong_archive = archive_evidence(
        wrong_job,
        tmp_path / "wrong-store",
        record_id="wrong",
        kind="campaign-job",
    )
    wrong_blob = (
        tmp_path
        / "wrong-store/blobs/sha256"
        / wrong_archive.content_digest.removeprefix("sha256:")[:2]
        / f"{wrong_archive.content_digest.removeprefix('sha256:')}.tar.gz"
    )
    blob.write_bytes(wrong_blob.read_bytes())
    archive_digest = "sha256:" + hashlib.sha256(blob.read_bytes()).hexdigest()
    record_path = store_root / "records/campaign-job" / f"{manifest.attempts[0].attempt_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["archive_digest"] = archive_digest
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    details = {
        "uri": uri,
        "content_digest": f"sha256:{digest}",
        "archive_digest": archive_digest,
        "record_path": record_path.relative_to(root).as_posix(),
    }
    job_dir = root / manifest.attempts[0].spec.jobs_dir / manifest.attempts[0].job_name

    with pytest.raises(CampaignDriftError, match="CAS content digest mismatch"):
        orchestrator._verify_archive_details(manifest.attempts[0], job_dir, details)


def test_remaining_token_reservation_opens_circuit_before_next_dispatch(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True, attempts=2))
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.store.freeze(manifest)
    orchestrator.store.append(
        manifest,
        event="campaign_started",
        sanitizer=orchestrator.sanitizer,
        occurred_at=NOW,
    )
    orchestrator.store.append(
        manifest,
        event="attempt_completed",
        attempt=manifest.attempts[0],
        sanitizer=orchestrator.sanitizer,
        occurred_at=NOW,
        details={
            "usage": {
                "input_tokens": 150,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "wall_clock_seconds": 1.0,
            }
        },
    )
    events = orchestrator.store.events(manifest)
    assert (
        orchestrator._next_attempt_budget_reason(events, manifest.attempts[1])
        == "campaign_input_token_ceiling_exceeded"
    )
