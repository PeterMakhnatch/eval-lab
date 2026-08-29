from __future__ import annotations

import fcntl
import hashlib
import json
import math
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import evallab.queue as queue_module
from evallab import cli
from evallab.benchmark_program_contracts import (
    CampaignCalibrationLedger,
    CampaignMeasurementLedger,
    SyntheticFamilyType,
)
from evallab.campaigns import (
    CampaignAmbiguityError,
    CampaignAnalysisCell,
    CampaignDefinition,
    CampaignDefinitionAttempt,
    CampaignDriftError,
    CampaignLimits,
    CampaignManifest,
    CampaignOrchestrator,
    CampaignSecretSanitizer,
    CampaignStore,
    MatrixRegistry,
    TrialLimits,
    build_campaign_manifest,
    campaign_manifest_digest,
    experiment_spec_digest,
)
from evallab.continuous_control_plane import (
    CampaignWorkloadOwner,
    DisabledCampaignControlLoop,
)
from evallab.credentials import DEEPSEEK_API_CREDENTIAL
from evallab.execution_contracts import (
    DEEPSEEK_MODEL_SELECTOR,
    DispatchCapacity,
    ExecutionFailure,
    RunRequest,
    TransientHarnessFailure,
)
from evallab.ops_continuous import main as operator_main
from evallab.ops_continuous import write_mode
from evallab.queue import DirectoryQueue, Executor, load_events, load_policy
from evallab.registry import compute_task_digests
from evallab.schemas import (
    ControlEvidenceRef,
    ExperimentMatrix,
    ExperimentSpec,
    QueueEvent,
    TaskControlEvidence,
    TaskLimits,
    TaskRegistryRecord,
)

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
    (task / "instruction.md").write_text("Complete the task.\n", encoding="utf-8")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (task / "tests").mkdir()
    (task / "tests/test_task.py").write_text("def test_task(): pass\n", encoding="utf-8")
    (task / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
    digests = compute_task_digests(task)
    common_evidence = {
        "evidence_digest": "sha256:" + "1" * 64,
        "lock_digest": "sha256:" + "2" * 64,
        "observed_at": NOW,
        "task_id": "task-one",
        "task_version": "1.0",
        "task_digests": digests,
        "harbor_task_digest": "sha256:" + "3" * 64,
    }
    record = TaskRegistryRecord(
        task_id="task-one",
        task_family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION.value,
        version="1.0",
        task_path="tasks/task-one",
        digests=digests,
        source_uri="local/task-one@1.0",
        provenance_zone="03-synthetic",
        is_synthetic=True,
        limits=TaskLimits(timeout_seconds=60),
        control_evidence=TaskControlEvidence(
            oracle=ControlEvidenceRef(
                job_name="task-one-oracle",
                trial_name="task-one__oracle",
                reward=1.0,
                evidence_path="research/evidence/runs/task-one-oracle/result.json",
                **common_evidence,
            ),
            nop=ControlEvidenceRef(
                job_name="task-one-nop",
                trial_name="task-one__nop",
                reward=0.0,
                evidence_path="research/evidence/runs/task-one-nop/result.json",
                **common_evidence,
            ),
        ),
        state="registered",
        allowed_uses=["measurement"],
        approved_by="Campaign Test",
        approved_at=NOW,
    )
    registry = root / "library/registry"
    registry.mkdir(parents=True)
    (registry / "task-one.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    matrix = {
        "schema_version": 2,
        "matrix_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "name": "campaign-task-one",
        "hypothesis": "Frozen matrix identity gates campaign dispatch",
        "benchmark_family": SyntheticFamilyType.FAMILY_A_STATE_INVERSION.value,
        "task_id": "task-one",
        "task": "tasks/task-one",
        "task_package_digest": digests.package,
        "verifier_digest": digests.verifier,
        "environment": "docker",
        "jobs_dir": "runs",
        "concurrency": 1,
        "timeout_seconds": 60,
        "runs": [{"name": "task-one-oracle", "agent": "oracle"}],
    }
    experiments = root / "research/experiments"
    experiments.mkdir(parents=True)
    matrix_path = experiments / "local-controls.json"
    matrix_path.write_text(
        json.dumps(matrix, indent=2) + "\n",
        encoding="utf-8",
    )
    validated_matrix = ExperimentMatrix.model_validate(matrix)
    matrix_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            validated_matrix.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    (experiments / "matrix-registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "matrices": [
                    {
                        "matrix_id": validated_matrix.matrix_id,
                        "path": "research/experiments/local-controls.json",
                        "matrix_digest": matrix_digest,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _canonical_matrix_digest(payload: dict[str, Any]) -> str:
    matrix = ExperimentMatrix.model_validate(payload)
    return "sha256:" + hashlib.sha256(
        json.dumps(
            matrix.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _update_fixture_matrix_catalog(
    root: Path,
    **updates: str,
) -> None:
    registry_path = root / "research/experiments/matrix-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["matrices"][0].update(updates)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")


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
        task_family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION.value,
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
        max_requests=2 if billable else 0,
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
            max_requests=2 * attempts if billable else 0,
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


def _analysis_definition(
    *,
    repeats: int,
    mixed: bool = False,
    duplicate_seed: bool = False,
) -> CampaignDefinition:
    definition = _definition(billable=True, attempts=repeats)
    cell = CampaignAnalysisCell(
        model=DEEPSEEK_MODEL_SELECTOR,
        agent="mini-swe-agent",
        task_id="task-one",
        harness="harbor-0.1",
        scaffold="mini-swe-agent",
        dose_axis="max_output_tokens",
        dose_value=100,
        dose_unit="tokens",
        alphabet="trajectory-actions/v1",
        base_task_pair_id="pair-task-one",
    )
    analysis_attempts = []
    for index, item in enumerate(definition.attempts, start=1):
        seed = 11 if duplicate_seed else index * 11
        declared_cell = (
            cell.model_copy(update={"scaffold": "different-scaffold"})
            if mixed and index == repeats
            else cell
        )
        analysis_attempts.append(
            item.model_copy(
                update={
                    "cell_id": "analysis-cell",
                    "analysis_cell": declared_cell,
                    "repeat_seed": seed,
                    "spec": item.spec.model_copy(
                        update={
                            "purpose": "comparison",
                            "generator_seed": seed,
                        }
                    ),
                }
            )
        )
    return definition.model_copy(update={"attempts": tuple(analysis_attempts)})


def _write_job(
    request: RunRequest,
    *,
    cost: float | None = 0.0,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    provider_calls: list[tuple[int, int, int]] | None = None,
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
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "experiment": (
            request.provenance.model_dump(mode="json")
            if request.provenance is not None
            else None
        ),
    }
    if (
        request.agent == "mini-swe-agent"
        and request.provenance is not None
        and request.max_requests is not None
        and request.max_input_tokens is not None
        and request.max_output_tokens is not None
        and request.max_total_tokens is not None
        and request.cost_limit_usd is not None
        and input_tokens is not None
        and output_tokens is not None
        and cost is not None
        and math.isfinite(cost)
        and cost >= 0
    ):
        calls_source = provider_calls
        if calls_source is None:
            calls_source = (
                []
                if input_tokens == 0 and output_tokens == 0 and cost == 0
                else [(input_tokens, output_tokens, round(cost * 1_000_000))]
            )
        calls = [
            {
                "call_id": index,
                "state": "reconciled",
                "reserved_input_tokens": call_input,
                "reserved_output_tokens": call_output,
                "reserved_cost_micros": call_cost,
                "status": 200,
                "input_tokens": call_input,
                "output_tokens": call_output,
                "cost_micros": call_cost,
            }
            for index, (call_input, call_output, call_cost) in enumerate(
                calls_source,
                start=1,
            )
        ]
        total_input = sum(int(call["input_tokens"]) for call in calls)
        total_output = sum(int(call["output_tokens"]) for call in calls)
        total_cost = sum(int(call["cost_micros"]) for call in calls)
        metadata["provider_usage"] = {
            "schema_version": 1,
            "capability_id": "sha256:" + "a" * 64,
            "attempt_id": request.provenance.campaign_attempt_id,
            "sequence": len(calls) * 2,
            "limits": {
                "max_requests": request.max_requests,
                "max_input_tokens": request.max_input_tokens,
                "max_output_tokens": request.max_output_tokens,
                "max_total_tokens": request.max_total_tokens,
                "max_cost_micros": math.ceil(request.cost_limit_usd * 1_000_000),
            },
            "totals": {
                "requests": len(calls),
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "cost_micros": total_cost,
            },
            "unresolved_requests": 0,
            "calls": calls,
        }
    (job / "lab-metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    return job


def _executor(
    root: Path,
    runner: Any,
    *,
    capacity: DispatchCapacity | None = None,
    credentials: frozenset[str] = frozenset(),
    compliance: Any = lambda _job, _spec, _ingest, _archive: "QUALITY_PASS",
) -> Executor:
    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=load_policy(root / "policy/standing-approvals.yaml"),
        runner=runner,
        ingester=lambda _job: None,
        compliance=compliance,
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()

    path, queued = executor.queue.list_specs("waiting")[0]
    payload = queued.model_dump(mode="json", exclude_none=True)
    payload["hypothesis"] = "drifted after planning"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CampaignDriftError, match="queued campaign spec drifted"):
        _orchestrator(root, manifest, executor).resume()

def test_resume_rejects_queued_campaign_spec_digest_binding_tamper(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(root, lambda request: calls.append(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()

    path, queued = executor.queue.list_specs("waiting")[0]
    payload = queued.model_dump(mode="json", exclude_none=True)
    payload["campaign_spec_digest"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")

    with pytest.raises(CampaignDriftError, match="digest binding drifted"):
        _orchestrator(root, manifest, executor).resume()
    assert calls == []

def test_campaign_rejects_task_package_substitution_before_dispatch(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(root, lambda request: calls.append(request))
    (root / "tasks/task-one/tests/test_task.py").write_text(
        "def test_task(): assert False\n",
        encoding="utf-8",
    )

    with pytest.raises(CampaignDriftError, match="task contract drifted"):
        _orchestrator(root, manifest, executor).run()
    assert calls == []


def test_partial_journal_record_refuses_resume(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.store.root.mkdir(parents=True)
    orchestrator.store.journal_lock_path.touch()
    orchestrator.store.journal_path.write_text('{"partial":', encoding="utf-8")

    with pytest.raises(CampaignAmbiguityError, match="partial record"):
        orchestrator.resume()

def test_campaign_store_rejects_symlinked_campaign_root(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
    state_root = root / "runs/campaigns"
    outside = tmp_path / "outside"
    state_root.mkdir(parents=True)
    outside.mkdir()
    (state_root / manifest.campaign_id).symlink_to(outside, target_is_directory=True)

    store = CampaignStore(state_root, manifest.campaign_id)
    with pytest.raises(CampaignAmbiguityError, match="unsafe"):
        store.freeze(manifest)


def test_campaign_store_never_follows_manifest_symlink(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
    state_root = root / "runs/campaigns"
    campaign_root = state_root / manifest.campaign_id
    campaign_root.mkdir(parents=True)
    outside = tmp_path / "outside-manifest.json"
    outside.write_text("operator-owned", encoding="utf-8")
    (campaign_root / "manifest.json").symlink_to(outside)

    store = CampaignStore(state_root, manifest.campaign_id)
    with pytest.raises(CampaignAmbiguityError, match="manifest.json"):
        store.freeze(manifest)
    assert outside.read_text(encoding="utf-8") == "operator-owned"


def test_campaign_journal_readers_take_a_shared_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
    executor = _executor(root, lambda request: _write_job(request))

    status = _orchestrator(root, manifest, executor).status()

    assert status.state == "planned"
    assert not (root / "queue/.events.lock").exists()


def test_run_reloads_started_state_after_acquiring_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False, attempts=2, circuit_failures=1), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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


def test_manifest_rejects_cross_ledger_attempt_identity(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
    payload = manifest.model_dump(mode="json")
    payload["ledger"]["ledger_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAX"

    with pytest.raises(ValidationError, match="cross-ledger"):
        CampaignManifest.model_validate(payload)


def test_campaign_manifest_and_job_names_are_deterministic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    definition = _definition(billable=False, attempts=2)
    first = build_campaign_manifest(definition, repo_root=root)
    second = build_campaign_manifest(definition, repo_root=root)

    assert first == second
    assert first.manifest_digest == second.manifest_digest
    assert [attempt.job_name for attempt in first.attempts] == [
        attempt.job_name for attempt in second.attempts
    ]
    assert len({attempt.job_name for attempt in first.attempts}) == 2

def test_analysis_cell_requires_two_distinct_declared_repeat_seeds(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_analysis_definition(repeats=2), repo_root=root)

    assert manifest.analysis_holds == ()
    assert [attempt.repeat_seed for attempt in manifest.attempts] == [11, 22]
    assert manifest.attempts[0].analysis_cell == manifest.attempts[1].analysis_cell
    assert len(manifest.attempts) == 2


def test_incomplete_analysis_cell_holds_before_dispatch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_analysis_definition(repeats=1), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(root, lambda request: calls.append(request))

    status = _orchestrator(root, manifest, executor).run()

    assert status.state == "circuit-open"
    assert status.circuit_reason == (
        "analysis_hold:analysis_cell_repeats_insufficient:analysis-cell"
    )
    assert calls == []
    assert executor.queue.list_specs("approved") == []


@pytest.mark.parametrize(
    ("mixed", "duplicate_seed", "reason"),
    [
        (True, False, "analysis_cell_mixed:analysis-cell"),
        (False, True, "analysis_cell_repeats_insufficient:analysis-cell"),
    ],
)
def test_mixed_or_duplicate_analysis_cells_hold(
    tmp_path: Path,
    mixed: bool,
    duplicate_seed: bool,
    reason: str,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(
        _analysis_definition(
            repeats=2,
            mixed=mixed,
            duplicate_seed=duplicate_seed,
        ),
        repo_root=root,
    )

    assert manifest.analysis_holds == (reason,)


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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)

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

@pytest.mark.parametrize("cost", [-0.01, float("nan"), float("inf")])
def test_invalid_billable_usage_fails_closed(
    tmp_path: Path,
    cost: float,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    executor = _executor(
        root,
        lambda request: _write_job(
            request,
            cost=cost,
            input_tokens=40,
            output_tokens=20,
        ),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")

    status = orchestrator.resume()

    assert status.state == "circuit-open"
    assert status.circuit_reason == "campaign_usage_invalid"


def test_direct_proxy_calls_are_reconciled_into_campaign_usage(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    executor = _executor(
        root,
        lambda request: _write_job(
            request,
            cost=0.01,
            input_tokens=10,
            output_tokens=5,
            provider_calls=[
                (10, 5, 10_000),
                (20, 7, 20_000),
            ],
        ),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")

    status = orchestrator.resume()

    assert status.state == "completed"
    assert status.cost_usd == pytest.approx(0.03)
    assert status.input_tokens == 30
    assert status.output_tokens == 12
    completed = [
        event
        for event in orchestrator.store.events(manifest)
        if event.event == "attempt_completed"
    ]
    assert completed[0].details["usage"]["request_count"] == 2


def test_done_unjournaled_usage_reserves_budget_before_next_dispatch(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(
        _definition(billable=True, attempts=2, campaign_cost=2.0),
        repo_root=root,
    )
    executor = _executor(
        root,
        lambda request: _write_job(
            request,
            cost=1.5,
            input_tokens=40,
            output_tokens=20,
        ),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.store.freeze(manifest)
    orchestrator._submit_missing([])
    first = manifest.attempts[0]
    executor.queue.approve(first.spec_id, actor="Peter Makhnatch")
    approved_path, approved_spec = executor.queue.list_specs("approved")[0]
    executor.execute_spec(approved_spec)
    running = executor.queue.transition(
        approved_path,
        "running",
        actor="test",
        event="dispatch_started",
    )
    executor.queue.transition(
        running,
        "done",
        actor="test",
        event="dispatch_completed",
    )

    reason = orchestrator._next_attempt_budget_reason([], manifest.attempts[1])

    assert reason == "campaign_cost_ceiling_exceeded"


@pytest.mark.parametrize("disposition", ["QUALITY_WARN", "HOLD", "QUARANTINED"])
def test_post_run_compliance_refusal_never_marks_campaign_queue_done(
    tmp_path: Path,
    disposition: str,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(
        _definition(billable=True, attempts=2),
        repo_root=root,
    )
    calls: list[str] = []

    def runner(request: RunRequest) -> Path:
        calls.append(request.name)
        return _write_job(
            request,
            cost=0.25,
            input_tokens=40,
            output_tokens=20,
        )

    executor = _executor(
        root,
        runner,
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
        compliance=lambda _job, _spec, _ingest, _archive: disposition,
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    for attempt in manifest.attempts:
        executor.queue.approve(attempt.spec_id, actor="Peter Makhnatch")

    status = orchestrator.resume()

    assert status.state == "circuit-open"
    assert status.circuit_reason == f"post_run_compliance_{disposition.casefold()}"
    assert status.cost_usd == 0.25
    assert calls == [manifest.attempts[0].job_name]
    assert executor.queue.list_specs("done") == []
    assert executor.queue.list_specs("approved") == []
    failed = executor.queue.list_specs("failed")
    assert len(failed) == 1
    events = load_events(executor.queue.events_path)
    assert any(
        event.event == "post_run_compliance_refused"
        and event.reason_code == f"post_run_compliance_{disposition.casefold()}"
        for event in events
    )



def test_executor_rejects_secret_bearing_job_before_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider-key-must-not-persist"
    monkeypatch.setenv("MSWEA_API_KEY", secret)
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)

    def leaking_runner(request: RunRequest) -> Path:
        job = _write_job(request, cost=0.25, input_tokens=40, output_tokens=20)
        (job / "leak.txt").write_text(secret, encoding="utf-8")
        return job

    executor = _executor(
        root,
        leaking_runner,
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )

    with pytest.raises(ExecutionFailure, match="credential material reached persistent artifacts"):
        executor.execute_spec(manifest.attempts[0].spec)
    job_dir = root / manifest.attempts[0].spec.jobs_dir / manifest.attempts[0].job_name
    assert not (job_dir / "leak.txt").exists()

def test_resume_rejects_tampered_campaign_cas_archive(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True, attempts=2, max_concurrency=2), repo_root=root)

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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    completed = orchestrator.run()
    assert completed.state == "completed"

    payload = _definition(billable=False).model_dump(mode="json")
    payload["attempts"][0]["spec"]["hypothesis"] = "a different frozen identity"
    drifted = build_campaign_manifest(CampaignDefinition.model_validate(payload), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
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
    manifest = build_campaign_manifest(_definition(billable=True, attempts=2), repo_root=root)
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



def test_executor_revalidates_campaign_spec_at_last_mile(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(
        root,
        lambda request: calls.append(request),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    approved = executor.queue.approve(
        manifest.attempts[0].spec_id,
        actor="Peter Makhnatch",
    )
    payload = json.loads(approved.read_text(encoding="utf-8"))
    payload["hypothesis"] = "substituted after approval"
    approved.write_text(json.dumps(payload), encoding="utf-8")

    assert executor.tick(spec_ids=[manifest.attempts[0].spec_id]) == 0
    assert calls == []
    assert executor.queue.list_specs("done") == []
    assert any(
        event.reason_code == "campaign_spec_drifted"
        for event in load_events(executor.queue.events_path)
    )


def test_executor_rejects_campaign_spec_id_prefix_substitution(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(
        root,
        lambda request: calls.append(request),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    waiting, queued = executor.queue.list_specs("waiting")[0]
    payload = queued.model_dump(mode="json", exclude_none=True)
    substituted_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    payload["spec_id"] = substituted_id
    waiting.write_text(json.dumps(payload), encoding="utf-8")
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")

    assert executor.tick(spec_ids=[substituted_id]) == 0
    assert calls == []
    assert any(
        event.reason_code == "campaign_binding_missing"
        for event in load_events(executor.queue.events_path)
    )


def test_executor_revalidates_task_snapshot_at_last_mile(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(
        root,
        lambda request: calls.append(request),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")
    (root / "tasks/task-one/instruction.md").write_text(
        "Substituted after approval.\n",
        encoding="utf-8",
    )

    assert executor.tick(spec_ids=[manifest.attempts[0].spec_id]) == 1
    assert calls == []
    assert executor.queue.list_specs("done") == []
    assert any(
        event.reason_code == "task_digest_mismatch"
        for event in load_events(executor.queue.events_path)
    )




def test_human_approval_binds_exact_campaign_spec_and_manifest(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    calls: list[RunRequest] = []
    executor = _executor(
        root,
        lambda request: calls.append(request),
        credentials=frozenset({DEEPSEEK_API_CREDENTIAL}),
    )
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    approved = executor.queue.approve(
        manifest.attempts[0].spec_id,
        actor="Peter Makhnatch",
    )
    current = executor.queue.load(approved)
    substituted = current.model_copy(update={"hypothesis": "coherently substituted"})
    substituted_spec_digest = experiment_spec_digest(substituted)
    substituted = substituted.model_copy(
        update={"campaign_spec_digest": substituted_spec_digest}
    )
    substituted_attempt = manifest.attempts[0].model_copy(
        update={
            "spec": substituted,
            "spec_digest": substituted_spec_digest,
        }
    )
    substituted_manifest = manifest.model_copy(
        update={
            "attempts": (substituted_attempt,),
            "manifest_digest": "sha256:" + "0" * 64,
        }
    )
    substituted_manifest_digest = campaign_manifest_digest(substituted_manifest)
    substituted = substituted.model_copy(
        update={"campaign_manifest_digest": substituted_manifest_digest}
    )
    substituted_attempt = substituted_attempt.model_copy(update={"spec": substituted})
    substituted_manifest = substituted_manifest.model_copy(
        update={
            "attempts": (substituted_attempt,),
            "manifest_digest": substituted_manifest_digest,
        }
    )
    CampaignManifest.model_validate(substituted_manifest.model_dump(mode="json"))
    approved.write_text(substituted.model_dump_json(), encoding="utf-8")
    orchestrator.store.manifest_path.write_text(
        substituted_manifest.model_dump_json(),
        encoding="utf-8",
    )

    assert executor.tick(spec_ids=[substituted.spec_id]) == 0
    assert calls == []
    assert any(
        event.reason_code == "paid_run_authorization_stale"
        for event in load_events(executor.queue.events_path)
    )


def test_registered_task_resolution_must_match_frozen_package_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    frozen = manifest.attempts[0].spec
    spec = frozen.model_copy(update={"task": "registered/task-one"})
    calls: list[RunRequest] = []
    executor = _executor(root, lambda request: calls.append(request))
    resolved = type(
        "ResolvedTask",
        (),
        {
            "task_path": "tasks/task-one",
            "version": spec.task_version,
            "digests": type(
                "Digests",
                (),
                {
                    "verifier": spec.verifier_digest,
                    "package": "sha256:" + "f" * 64,
                },
            )(),
            "task_id": spec.task_id,
            "limits": type("Limits", (), {"timeout_seconds": spec.timeout_seconds})(),
        },
    )()
    registry = type(
        "Registry",
        (),
        {"resolve_spec": lambda _self, _spec, _root: resolved},
    )()
    monkeypatch.setattr(
        queue_module.TaskRegistry,
        "from_repo",
        lambda _root: registry,
    )

    with pytest.raises(ExecutionFailure, match="frozen campaign digest"):
        executor.execute_spec(spec)
    assert calls == []


def test_running_reconciliation_revalidates_campaign_binding(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=True), repo_root=root)
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.run()
    approved = executor.queue.approve(
        manifest.attempts[0].spec_id,
        actor="Peter Makhnatch",
    )
    running = executor.queue.transition(
        approved,
        "running",
        actor="test",
        event="dispatch_started",
    )
    payload = json.loads(running.read_text(encoding="utf-8"))
    for field in (
        "campaign_ledger",
        "campaign_cell_id",
        "campaign_attempt_id",
        "campaign_attempt_index",
        "campaign_manifest_digest",
        "campaign_spec_digest",
        "campaign_evidence_store",
    ):
        payload.pop(field, None)
    payload["spec_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    running.write_text(json.dumps(payload), encoding="utf-8")

    executor.reconcile_running()

    assert not running.exists()
    assert executor.queue.list_specs("done") == []
    assert any(
        event.reason_code == "campaign_binding_missing"
        for event in load_events(executor.queue.events_path)
    )
@pytest.mark.parametrize("billable", [False, True])
def test_failed_attempt_without_usage_blocks_later_dispatch(
    tmp_path: Path,
    billable: bool,
) -> None:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(
        _definition(
            billable=billable,
            attempts=2,
            campaign_cost=2.0 if billable else 0.0,
        ),
        repo_root=root,
    )
    executor = _executor(root, lambda request: _write_job(request))
    orchestrator = _orchestrator(root, manifest, executor)
    orchestrator.store.freeze(manifest)
    orchestrator._submit_missing([])
    queued_path = executor.queue.locate(manifest.attempts[0].spec_id)
    approved = (
        executor.queue.approve(
            manifest.attempts[0].spec_id,
            actor="Peter Makhnatch",
        )
        if queued_path.parent.name == "waiting"
        else queued_path
    )
    executor.queue.transition(
        approved,
        "failed",
        actor="test",
        event="dispatch_failed",
        reason_code="execution_failed",
    )

    usage, reason = orchestrator._authoritative_usage([])

    assert usage == {}
    assert reason == "campaign_usage_missing"
    assert (
        orchestrator._next_attempt_budget_reason([], manifest.attempts[1])
        == "campaign_usage_missing"
    )


def _frozen_owner(tmp_path: Path) -> tuple[Path, CampaignManifest, CampaignWorkloadOwner]:
    root = _repo(tmp_path)
    manifest = build_campaign_manifest(_definition(billable=False), repo_root=root)
    CampaignStore(root / "runs/campaigns", manifest.campaign_id).freeze(manifest)
    return root, manifest, CampaignWorkloadOwner.from_repo(root, manifest.campaign_id)


def test_campaign_workload_owner_cancels_exact_active_lease(tmp_path: Path) -> None:
    root, manifest, owner = _frozen_owner(tmp_path)
    attempt = manifest.attempts[0]
    queue = DirectoryQueue(root / "queue")
    running = queue.state_dir("running") / f"{attempt.spec.agent}-{attempt.spec_id}.json"
    running.write_text(attempt.spec.model_dump_json(), encoding="utf-8")
    assert queue.acquire_lease(attempt.spec) is not None

    result = owner.request_cancel([attempt.spec_id, "unknown-lease"])

    assert result["executed"] is True
    assert result["queue_stopped"] is True
    assert result["results"] == {
        attempt.spec_id: "signalled",
        "unknown-lease": "unknown",
    }
    assert queue.cancel_path(attempt.spec).is_file()


def test_campaign_workload_owner_observes_terminal_queue_evidence(tmp_path: Path) -> None:
    root, manifest, owner = _frozen_owner(tmp_path)
    attempt = manifest.attempts[0]
    queue = DirectoryQueue(root / "queue")
    done = queue.state_dir("done") / f"{attempt.spec.agent}-{attempt.spec_id}.json"
    done.write_text(attempt.spec.model_dump_json(), encoding="utf-8")
    queue_event = QueueEvent(
        event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        spec_id=attempt.spec_id,
        occurred_at=NOW,
        event="dispatch_completed",
        from_state="running",
        to_state="done",
        actor="executor",
        job_name=attempt.job_name,
    )
    queue.append_event(queue_event)

    observed = owner.observe_lease(attempt.attempt_id)

    assert observed is not None
    assert observed["alive"] is False
    assert observed["queue_state"] == "complete"
    assert len(observed["settlement_digest"]) == 64
    assert observed["evidence"]["queue_event_id"] == queue_event.event_id


def test_campaign_control_loop_stays_disabled_without_dispatch(tmp_path: Path) -> None:
    root, _manifest, owner = _frozen_owner(tmp_path)
    state = tmp_path / "operator-state"
    state.mkdir()
    write_mode(state, "DISABLED")

    tick = DisabledCampaignControlLoop(state, owner).tick()

    assert tick == {
        "campaign_id": owner.manifest.campaign_id,
        "mode": "DISABLED",
        "running": False,
        "dispatched": 0,
        "reason": "default_disabled",
    }
    assert not (root / "queue").exists()


def test_operator_cli_binds_frozen_campaign_owner_without_dispatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest, _owner = _frozen_owner(tmp_path)
    state = tmp_path / "operator-cli-state"

    code = operator_main(
        [
            "status",
            "--state-dir",
            str(state),
            "--repo-root",
            str(root),
            "--campaign-id",
            manifest.campaign_id,
        ],
        environ={},
        secret_store=lambda _ref: None,
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["mode"] == "DISABLED"
    assert payload["running"] is False
    assert not (root / "queue").exists()


def test_campaign_refuses_registry_record_without_immutable_family(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    record_path = root / "library/registry/task-one.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("task_family")
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid registry record"):
        build_campaign_manifest(_definition(billable=True), repo_root=root)


def test_campaign_refuses_self_asserted_family_against_registry(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    record_path = root / "library/registry/task-one.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["task_family"] = SyntheticFamilyType.FAMILY_B_FUNCDAG_V2.value
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="registered benchmark family"):
        build_campaign_manifest(_definition(billable=True), repo_root=root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "benchmark_family",
            SyntheticFamilyType.FAMILY_B_FUNCDAG_V2.value,
            "benchmark family",
        ),
        ("task_id", "different-task", "registry identity"),
        ("task_package_digest", "sha256:" + "0" * 64, "registry identity"),
        ("verifier_digest", "sha256:" + "0" * 64, "registry identity"),
    ],
)
def test_campaign_refuses_spoofed_frozen_matrix_identity(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    root = _repo(tmp_path)
    matrix_path = (
        root / "research/experiments/local-controls.json"
    )
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix[field] = value
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    _update_fixture_matrix_catalog(
        root,
        matrix_digest=_canonical_matrix_digest(matrix),
    )

    with pytest.raises(ValueError, match=message):
        build_campaign_manifest(_definition(billable=True), repo_root=root)


def test_campaign_refuses_unresolved_ledger_matrix_ref(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    definition = _definition(billable=True)
    ledger = definition.ledger.model_copy(
        update={"matrix_ref": "01ARZ3NDEKTSV4RRFFQ69G5FAS"}
    )

    with pytest.raises(ValueError, match="canonical matrix registry"):
        build_campaign_manifest(
            definition.model_copy(update={"ledger": ledger}),
            repo_root=root,
        )


def test_committed_matrix_registry_resolves_local_and_baselines() -> None:
    registry = MatrixRegistry.from_repo(REPO_ROOT)
    expected_paths = {
        "research/experiments/local-controls.json",
        "research/experiments/baselines/event-summary-controls.json",
        "research/experiments/baselines/html-js-filter-controls.json",
        "research/experiments/baselines/query-optimize-controls.json",
        "research/experiments/baselines/transaction-reconciliation-controls.json",
    }

    assert {entry.path for entry in registry.matrices} == expected_paths
    for entry in registry.matrices:
        resolved_entry, matrix, digest = registry.resolve(REPO_ROOT, entry.matrix_id)
        assert resolved_entry == entry
        assert matrix.matrix_id == entry.matrix_id
        assert digest == entry.matrix_digest


def test_matrix_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    registry_path = root / "research/experiments/matrix-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    duplicate = dict(registry["matrices"][0])
    duplicate["path"] = "research/experiments/duplicate.json"
    registry["matrices"].append(duplicate)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical matrix registry is unreadable"):
        MatrixRegistry.from_repo(root)


def test_campaign_refuses_missing_matrix_catalog_entry(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    registry_path = root / "research/experiments/matrix-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["matrices"][0]["matrix_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAS"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical matrix registry"):
        build_campaign_manifest(_definition(billable=True), repo_root=root)


def test_matrix_registry_rejects_path_escape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _update_fixture_matrix_catalog(root, path="../outside.json")

    with pytest.raises(ValueError, match="canonical matrix registry is unreadable"):
        MatrixRegistry.from_repo(root)


def test_matrix_registry_rejects_missing_registered_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _update_fixture_matrix_catalog(
        root,
        path="research/experiments/missing.json",
    )
    registry = MatrixRegistry.from_repo(root)

    with pytest.raises(ValueError, match="does not resolve to a frozen matrix"):
        registry.resolve(root, "01ARZ3NDEKTSV4RRFFQ69G5FAW")


def test_matrix_registry_rejects_digest_spoof(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _update_fixture_matrix_catalog(root, matrix_digest="sha256:" + "0" * 64)
    registry = MatrixRegistry.from_repo(root)

    with pytest.raises(ValueError, match="digest"):
        registry.resolve(root, "01ARZ3NDEKTSV4RRFFQ69G5FAW")


def test_matrix_registry_rejects_unknown_identifier(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    registry = MatrixRegistry.from_repo(root)

    with pytest.raises(ValueError, match="absent from the canonical matrix registry"):
        registry.resolve(root, "01ARZ3NDEKTSV4RRFFQ69G5FAS")