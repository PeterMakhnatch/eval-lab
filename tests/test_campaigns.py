from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.benchmark_program_contracts import (
    CampaignMeasurementLedger,
    SyntheticFamilyType,
)
from evallab.campaigns import (
    CampaignAttempt,
    CampaignDriftError,
    CampaignLimits,
    CampaignManifest,
    CampaignOrchestrator,
    CampaignSecretSanitizer,
    TrialLimits,
    campaign_manifest_digest,
    experiment_spec_digest,
)
from evallab.execution_contracts import (
    DEEPSEEK_MODEL_SELECTOR,
    DispatchCapacity,
    RunRequest,
)
from evallab.profiles import DEEPSEEK_API_CREDENTIAL, ZAI_OPENAPI_API_CREDENTIAL
from evallab.queue import DirectoryQueue, Executor, load_events, load_policy
from evallab.registry import compute_task_digests
from evallab.schemas import (
    ControlEvidenceRef,
    ExperimentMatrix,
    ExperimentSpec,
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


def _manifest(root: Path, *, attempts: int = 1, agent: str = "mini-swe-agent",
              model: str = DEEPSEEK_MODEL_SELECTOR) -> CampaignManifest:
    """An explicit frozen manifest for the retained resume and custody contracts."""
    ledger = CampaignMeasurementLedger(
        ledger_id="01ARZ3NDEKTSV4RRFFQ69G5FAV", matrix_ref="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        campaign_phase="billable_cohort", family=SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
        status="pending",
    )
    digests = compute_task_digests(root / "tasks/task-one")
    task_contract = {
        "task_id": "task-one", "task_ref": "tasks/task-one", "task_path": "tasks/task-one",
        "task_family": ledger.family.value, "task_version": "1.0",
        "verifier_digest": digests.verifier, "package_digest": digests.package,
    }
    matrix = json.loads((root / "research/experiments/local-controls.json").read_text())
    trial_limits = TrialLimits(max_requests=2, max_cost_usd=1.0, max_input_tokens=100,
                               max_output_tokens=100, max_total_tokens=200, max_wall_clock_seconds=60)
    placeholder = "sha256:" + "0" * 64
    rows = []
    for index in range(1, attempts + 1):
        attempt_id, spec_id = f"attempt-{index:024x}", f"campaign-{index:024x}"
        name = f"fixture-campaign-{index}"
        spec = _spec(billable=True).model_copy(update={
            "agent": agent, "model": model, "name": name, "spec_id": spec_id,
            "task_path": "tasks/task-one", "task_version": "1.0",
            "verifier_digest": digests.verifier, "task_package_digest": digests.package,
            "max_requests": 2, "max_input_tokens": 100, "max_output_tokens": 100,
            "max_total_tokens": 200, "cost_limit_usd": 1.0, "est_cost_usd": 1.0,
            "campaign_ledger": ledger, "campaign_cell_id": f"cell-{index}",
            "campaign_attempt_id": attempt_id, "campaign_attempt_index": index,
            "campaign_manifest_digest": placeholder, "campaign_spec_digest": placeholder,
            "campaign_evidence_store": "derived/evidence-cas",
        })
        spec_digest = experiment_spec_digest(spec)
        spec = spec.model_copy(update={"campaign_spec_digest": spec_digest})
        rows.append(CampaignAttempt(
            attempt_id=attempt_id, spec_id=spec_id, job_name=name, spec_digest=spec_digest,
            identity={"ledger": ledger, "cell_id": f"cell-{index}", "task_id": "task-one", "attempt": index},
            spec=spec, limits=trial_limits, task_contract=task_contract,
        ).model_dump(mode="json", exclude_none=True))
    raw = {
        "schema_version": "campaign-manifest/v3", "ledger": ledger.model_dump(mode="json"),
        "submitted_by": "campaign-test", "evidence_store": "derived/evidence-cas",
        "limits": CampaignLimits(max_requests=2 * attempts, max_cost_usd=float(attempts),
                                 max_input_tokens=100 * attempts, max_output_tokens=100 * attempts,
                                 max_total_tokens=200 * attempts, max_wall_clock_seconds=60 * attempts,
                                 max_concurrency=1, max_consecutive_transient_failures=1).model_dump(mode="json"),
        "matrix": {"matrix_id": matrix["matrix_id"], "matrix_path": "research/experiments/local-controls.json",
                   "matrix_digest": json.loads((root / "research/experiments/matrix-registry.json").read_text())["matrices"][0]["matrix_digest"], "benchmark_family": ledger.family.value,
                   "task_id": "task-one", "task_path": "tasks/task-one", "verifier_digest": digests.verifier,
                   "package_digest": digests.package},
        "attempts": rows, "analysis_holds": [],
    }
    raw["manifest_digest"] = campaign_manifest_digest(raw)
    for row in rows:
        row["spec"]["campaign_manifest_digest"] = raw["manifest_digest"]
    return CampaignManifest.model_validate(raw)


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
        request.agent in {"mini-swe-agent", "terminus-2"}
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


@pytest.mark.parametrize(
    ("agent", "model", "credential"),
    [
        ("mini-swe-agent", DEEPSEEK_MODEL_SELECTOR, DEEPSEEK_API_CREDENTIAL),
        ("terminus-2", "zai/glm-5.3-flash", ZAI_OPENAPI_API_CREDENTIAL),
    ],
)
def test_billable_run_waits_for_explicit_approval_then_resume_is_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    agent: str,
    model: str,
    credential: str,
) -> None:
    root = _repo(tmp_path)
    manifest = _manifest(root, agent=agent, model=model)
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
        credentials=frozenset({credential}),
    )
    first = _orchestrator(root, manifest, executor)

    waiting = first.run()
    assert waiting.state == "waiting-approval"
    assert calls == []
    assert waiting.attempts[0].approval_command == (
        f"uv run evallab approve {manifest.attempts[0].spec_id} --actor <you>"
    )

    executor.queue.approve(manifest.attempts[0].spec_id, actor="Peter Makhnatch")
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


def test_campaign_cli_status_and_run_dry_run_are_operational(tmp_path: Path, capsys) -> None:
    root = _repo(tmp_path)
    manifest = _manifest(root)
    manifest_path = root / "runs/campaigns" / manifest.campaign_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(manifest.model_dump_json(indent=2))
    relative = str(manifest_path.relative_to(root))
    assert cli.run_cli(["campaign", "run", relative, "--dry-run", "--json"], workspace=root) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["state"] == "planned" and dry_run["dry_run"] is True
    assert not (root / "queue").exists()
    assert not (manifest_path.parent / "journal.jsonl").exists()
    assert cli.run_cli(["campaign", "status", relative, "--json"], workspace=root) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "planned"


@pytest.mark.parametrize("disposition", ["QUALITY_WARN", "HOLD", "QUARANTINED"])
def test_post_run_compliance_refusal_never_marks_campaign_queue_done(
    tmp_path: Path,
    disposition: str,
) -> None:
    root = _repo(tmp_path)
    manifest = _manifest(root, attempts=2)
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


def test_resume_rejects_valid_archive_with_wrong_content_digest(tmp_path: Path) -> None:
    from evallab.evidence_store import archive_evidence

    root = _repo(tmp_path)
    manifest = _manifest(root)
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


def test_executor_revalidates_campaign_spec_at_last_mile(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = _manifest(root)
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
    manifest = _manifest(root)
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
    manifest = _manifest(root)
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
    manifest = _manifest(root)
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


def test_running_reconciliation_revalidates_campaign_binding(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = _manifest(root)
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


