"""Task Quality live streaming and repair gates; no Docker, network or models."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.dataset_audit_checks import static_observation
from evallab.dataset_audit_contracts import AuditObservation, AuditTask
from evallab.execution_backend import ExecutionBackendConfig, LocalDockerDisabledError
from evallab.production.swarm_coordinator import ModelCall, ModelResponse, Role, WorkKind
from evallab.production.tq_worker import (
    JOB_TYPES,
    MAX_REPAIR_DIAGNOSTICS_CHARS,
    GraderRepairRequest,
    GraderRepairResponse,
    TaskQualityLiveWorker,
    TracebackGraderRepair,
)
from evallab.qa import counterfactual
from evallab.qa.models import Finding, Refutation
from evallab.registry import task_directory_digest

NOW = "2026-10-24T15:00:00+00:00"
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
RETRY_GRADER = b'assert result.strip() == "answer"\r\n'


@pytest.fixture(autouse=True)
def isolated_execution_policy(monkeypatch):
    """Host environment is not a fixture, and unit tests may never start Docker."""
    monkeypatch.delenv("EVALLAB_DISABLE_LOCAL_DOCKER", raising=False)

    def no_process(*args, **kwargs):
        pytest.fail(f"Unexpected external process in worker unit test: {args!r}")

    monkeypatch.setattr("subprocess.run", no_process)


class MemoryQueue:
    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.events: list[dict[str, Any]] = []
        self.claims: list[tuple[str, tuple[str, ...], int]] = []
        self.acks: list[str] = []
        self.releases: list[str] = []
        self.renewals: list[str] = []
        self.keys: dict[str, dict[str, Any]] = {}
        self.stop: threading.Event | None = None

    def append_event(
        self, event: Mapping[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if idempotency_key in self.keys:
            return self.keys[idempotency_key]
        row = {**event, "seq": len(self.events) + 1}
        self.events.append(row)
        if idempotency_key is not None:
            self.keys[idempotency_key] = row
        return row

    def claim(self, worker_id: str, *, job_types, lease_seconds: int = 60):
        self.claims.append((worker_id, tuple(job_types), lease_seconds))
        for index, job in enumerate(self.jobs):
            if job["event_type"] in job_types:
                return {**self.jobs.pop(index), "lease_token": "owned-lease"}
        return None

    def renew(self, event_id: str, *, lease_token: str, lease_seconds: int = 60):
        assert lease_token == "owned-lease"
        assert lease_seconds >= 3600
        self.renewals.append(event_id)

    def ack(self, event_id: str, *, lease_token: str):
        assert lease_token == "owned-lease"
        self.acks.append(event_id)
        if self.stop is not None:
            self.stop.set()

    def release(self, event_id: str, *, lease_token: str):
        assert lease_token == "owned-lease"
        self.releases.append(event_id)


class QABackend:
    environment = "docker"

    def __init__(self, root: Path, *, real_static: bool = False) -> None:
        self.root = root
        self.real_static = real_static
        self.rewards: dict[str, list[Any]] = {"oracle": [1.0] * 3, "nop": [0.0] * 3}
        self.status = "executed"
        self.exception: str | None = None
        self.same_evidence = False
        self.calls: list[tuple[Path, str]] = []
        self.bound_patches: list[tuple[Path, Path]] = []
        self.blocker: str | None = None
        self.extra_facts: dict[str, Any] = {}

    def resolve(self, package: Path) -> AuditTask:
        return AuditTask(
            dataset_id="fixture",
            task_id="format-task",
            task_name="fixture/format-task",
            path=package,
            package_digest=task_directory_digest(package),
            source_uri=package.as_uri(),
        )

    def static(self, task: AuditTask) -> AuditObservation:
        if self.real_static:
            return static_observation(task)
        return AuditObservation(
            stage="static",
            status="recorded",
            facts={"coverage": "harbor-test-source", "a_unstated_literal": True},
        )

    def resource_blocker(self, package: Path) -> str | None:
        return self.blocker

    def bind_reference_patch(self, package: Path, patch: Path) -> None:
        self.bound_patches.append((package, patch))

    def control(self, task: AuditTask, agent: str, *, timeout_seconds: int) -> AuditObservation:
        assert timeout_seconds == 600
        assert task.path is not None
        self.calls.append((task.path, agent))
        values = self.rewards[agent]
        reward = values.pop(0) if len(values) > 1 else values[0]
        name = "reused" if self.same_evidence else f"{agent}-{len(self.calls)}"
        return AuditObservation(
            stage="oracle" if agent == "oracle" else "nop",
            status=self.status,
            facts={
                "reward": reward,
                "exception": self.exception,
                "job_path": str(self.root / name),
                **self.extra_facts,
            },
        )


class Replay:
    def __init__(
        self, original: Path, *, original_reward: Any = 1.0, repaired_reward: Any = 0.0
    ) -> None:
        self.original = original
        self.original_reward = original_reward
        self.repaired_reward = repaired_reward
        self.calls: list[dict[str, Any]] = []
        self.refusals: list[str] = []
        self.error: str | None = None
        self.same_evidence = False
        self.before = None

    def __call__(self, trial, grader, work, *, repo_root, environment):
        assert environment == "docker"
        if self.before is not None:
            self.before(trial, grader, work)
        self.calls.append({"trial": trial, "grader": grader, "work": work})
        return {
            "regraded": self.original_reward
            if grader.package == self.original
            else self.repaired_reward,
            "refusals": self.refusals,
            "execution_error": self.error,
            "outcome": "flips",
            "job_dir": str(repo_root / "reused-replay")
            if self.same_evidence
            else str(work / "job"),
        }


def package(path: Path, *, repaired: bool = False) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "task.toml").write_text(
        '[task]\nname = "fixture/format-task"\n[environment]\nworkdir = "/testbed"\n'
    )
    (path / "instruction.md").write_text("Write a correct answer. Any whitespace is acceptable.\n")
    (path / "tests/test.py").write_text(
        'assert result.strip() == "answer"\n' if repaired else 'assert result == "answer\\n"\n'
    )
    return path


def trial(path: Path, *, reward: float = 1.0) -> Path:
    (path / "verifier").mkdir(parents=True)
    (path / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": reward}}})
    )
    (path / "verifier/agent.diff").write_text(PATCH)
    return path


@pytest.fixture
def setup(tmp_path: Path):
    original = package(tmp_path / "original")
    repaired = package(tmp_path / "repaired", repaired=True)
    source = trial(tmp_path / "trial")
    envelope = {
        "event_id": "job-1",
        "run_id": "live-run-1",
        "system": "queue",
        "event_type": "task_quality.audit_task",
        "seq": 1,
        "ts": NOW,
        "prefix_step": None,
        "status": "applied",
        "payload": {
            "task_package": "original",
            "repaired_task": "repaired",
            "passing_runs": ["trial"],
            "failing_runs": [],
            "dataset_id": "fixture",
            "item_id": "format-task",
        },
    }
    queue = MemoryQueue()
    backend = QABackend(tmp_path)
    replay = Replay(original)
    worker = TaskQualityLiveWorker(
        queue, repo_root=tmp_path, backend=backend, replay_runner=replay, clock=lambda: NOW
    )
    return SimpleNamespace(
        root=tmp_path,
        original=original,
        repaired=repaired,
        source=source,
        envelope=envelope,
        queue=queue,
        backend=backend,
        replay=replay,
        worker=worker,
    )


def finding_events(queue: MemoryQueue) -> list[dict[str, Any]]:
    return [event for event in queue.events if event["event_type"] == "qa.finding/v1"]


def attested_repair(
    request: GraderRepairRequest, raw: bytes = RETRY_GRADER
) -> GraderRepairResponse:
    return GraderRepairResponse(
        raw_output=raw,
        call=ModelCall(
            call_id="trusted-repair-call",
            role=Role.STRONG,
            requested_model="fixture-provider/fixture-model",
            served_model="fixture-provider/fixture-model",
            prompt_sha256=hashlib.sha256(b"trusted repair prompt").hexdigest(),
            response_sha256=hashlib.sha256(raw).hexdigest(),
            tokens_in=100,
            tokens_out=20,
            latency_ms=1,
            quarantined_fallback=False,
            work_kind=WorkKind.REPAIR_PROPOSAL,
            subject_id=request.task_id,
        ),
    )


def retry_worker(s, callback) -> TaskQualityLiveWorker:
    return TaskQualityLiveWorker(
        s.queue,
        repo_root=s.root,
        backend=s.backend,
        replay_runner=s.replay,
        repair_callback=callback,
        repair_grader_path="tests/test.py",
        clock=lambda: NOW,
    )


@pytest.mark.parametrize("direction", ["unfair_fail", "wrong_pass"])
def test_streams_provisional_before_execution_then_confirms(setup, direction: str) -> None:
    s = setup
    if direction == "unfair_fail":
        s.envelope["payload"].update(failing_runs=["trial"], passing_runs=[])
        s.replay.original_reward, s.replay.repaired_reward = 0.0, 1.0

    def before(*_):
        events = finding_events(s.queue)
        assert len(events) == 1
        assert events[0]["status"] == "provisional"
        finding = Finding.model_validate(events[0]["payload"]["finding"])
        assert finding.verdict == "unknown"
        assert finding.measurement == "not_run"
        assert finding.refutation.status == "unverified"

    s.replay.before = before
    result = s.worker.process_job(s.envelope)
    events = finding_events(s.queue)
    assert result["complete"] is True
    assert [event["status"] for event in events] == ["provisional", "confirmed"]
    assert events[0]["payload"]["finding_id"] == events[1]["payload"]["finding_id"]
    final = Finding.model_validate(events[1]["payload"]["finding"])
    assert final.verdict == "fail"
    assert final.category == ("false_negative" if direction == "unfair_fail" else "false_positive")
    assert final.refutation.status == "verified"
    assert final.measurement == "measured"
    if direction == "unfair_fail":
        assert final.root_cause == ["unstated_requirement"]
    assert [call["grader"].package for call in s.replay.calls] == [s.original, s.repaired]
    assert all(call["trial"] == s.source for call in s.replay.calls)
    assert s.queue.events[0]["event_type"] == "task_quality.started"
    assert s.queue.events[-1]["event_type"] == "task_quality.completed"
    assert [event["seq"] for event in s.queue.events] == [1, 2, 3, 4]
    for event in s.queue.events:
        assert event["system"] == "task_quality"
        assert event["run_id"] == "live-run-1"
        assert event["ts"] == NOW
        assert event["payload"]["dataset_id"] == "fixture"
        assert event["payload"]["item_id"] == "format-task"


@pytest.mark.parametrize("original,repaired", [(1.0, 1.0), (0.0, 0.0), (0.0, 1.0), (0.5, 0.0)])
def test_refutes_wrong_pass_only_after_both_measured_executions(setup, original, repaired) -> None:
    s = setup
    s.replay.original_reward, s.replay.repaired_reward = original, repaired
    result = s.worker.process_job(s.envelope)
    events = finding_events(s.queue)
    assert result["complete"] is True
    assert [event["status"] for event in events] == ["provisional", "refuted"]
    final = Finding.model_validate(events[-1]["payload"]["finding"])
    assert final.verdict == "pass"
    assert final.refutation.status == "refuted"
    assert len(s.replay.calls) == 2


@pytest.mark.parametrize("score", [None, True, float("nan"), float("inf"), -1.0, 2.0])
def test_invalid_scores_remain_provisional_and_release_claim(setup, score) -> None:
    s = setup
    s.replay.repaired_reward = score
    s.queue.jobs.append(s.envelope)
    result = s.worker.run_once()
    assert result is not None and result["complete"] is False
    assert [event["status"] for event in finding_events(s.queue)] == ["provisional", "provisional"]
    assert s.queue.events[-1]["event_type"] == "task_quality.blocked"
    assert s.queue.acks == []
    assert s.queue.releases == ["job-1"]
    final = Finding.model_validate(result["findings"][0])
    assert final.verdict == "unknown"
    assert final.refutation.status == "unverified"
    assert final.measurement == "ran_without_usable_result"


@pytest.mark.parametrize("failure", ["refusal", "exception", "reused_evidence"])
def test_infrastructure_or_shared_execution_cannot_confirm_or_refute(setup, failure: str) -> None:
    s = setup
    if failure == "refusal":
        s.replay.refusals = ["source_patch_unavailable"]
    elif failure == "exception":
        s.replay.error = "container failed"
    else:
        s.replay.same_evidence = True
    assert s.worker.process_job(s.envelope)["complete"] is False
    assert finding_events(s.queue)[-1]["status"] == "provisional"


def test_existing_static_reader_and_counterfactual_matrix_are_integrated(
    setup, monkeypatch
) -> None:
    s = setup
    s.backend.real_static = True
    matrices = []
    monkeypatch.setattr(counterfactual, "_local_docker_gate", lambda *_: nullcontext())

    def matrix_runner(matrix, *, root):
        matrices.append(matrix)
        return SimpleNamespace(
            results=[
                {"status": "ok", "rewards": [1.0 if len(matrices) == 1 else 0.0], "error": None}
            ],
            receipt_path=root / "runs" / matrix.name / "receipt.json",
        )

    def replay(*args, **kwargs):
        return counterfactual.replay(*args, matrix_runner=matrix_runner, **kwargs)

    s.worker.replay_runner = replay
    s.worker.process_job(s.envelope)
    events = finding_events(s.queue)
    assert events[0]["payload"]["static"]["facts"]["coverage"] == "harbor-test-source"
    assert events[0]["payload"]["static"]["facts"]["a_unstated_literal"] is True
    assert events[-1]["status"] == "confirmed"
    assert len(matrices) == 2
    assert [matrix.task for matrix in matrices] == [str(s.original), str(s.repaired)]
    final = events[-1]["payload"]
    assert final["original"]["execution"]["input_kind"] == "native_patch_replay"
    assert (
        final["original"]["execution"]["script_sha256"]
        == final["repaired"]["execution"]["script_sha256"]
    )
    assert (s.source / "verifier/agent.diff").read_text() == PATCH


def test_reader_explanation_never_bypasses_differential_confirmation(setup) -> None:
    s = setup
    seen = []

    def reader(finding: Finding, bundle: dict[str, Any]) -> Finding:
        seen.append(bundle)
        return finding.model_copy(
            update={
                "summary": "Exact newline rule is absent from the instruction",
                "verdict": "fail",
                "measurement": "measured",
                "refutation": Refutation(status="verified"),
            }
        )

    s.worker.reader = reader
    s.worker.process_job(s.envelope)
    first, last = finding_events(s.queue)
    assert "instruction.md" in seen[0]["files"]
    assert "tests/test.py" in seen[0]["files"]
    assert first["payload"]["finding"]["verdict"] == "unknown"
    assert first["payload"]["finding"]["measurement"] == "not_run"
    assert last["status"] == "confirmed"
    assert last["payload"]["summary"] == "Exact newline rule is absent from the instruction"


def test_qualifies_only_nine_independent_executions_and_emits_verified_asset(setup) -> None:
    s = setup
    s.envelope["payload"].update(
        ref_passes=99,
        nop=-99,
        exploit=-99,
        reference_patch="reference.patch",
        status="obsolete",
        fixed_task="independent-heldout",
        candidate_digest="sha256:known-candidate",
    )
    result = s.worker.qualify_repair(s.envelope)
    assert result["qualified"] is True
    assert result["measured"] is True
    assert result["control_rewards"] == {
        "reference": [1.0] * 3,
        "nop": [0.0] * 3,
        "exploit": [0.0] * 3,
    }
    assert s.backend.calls == [(s.repaired, "oracle")] * 3 + [(s.repaired, "nop")] * 3
    assert len(s.replay.calls) == 3
    assert s.backend.bound_patches == [(s.repaired, s.root / "reference.patch")]
    controls = [
        event for event in s.queue.events if event["event_type"] == "task_quality.repair_control"
    ]
    assert len(controls) == 9
    assert all(event["status"] == "provisional" for event in controls)
    qualified = result["event"]
    assert qualified["event_type"] == "task_quality.repair_qualified"
    assert qualified["status"] == "confirmed"
    payload = qualified["payload"]
    assert payload["status"] == "confirmed"
    assert (payload["ref_passes"], payload["nop"], payload["exploit"]) == (3, 0, 0)
    assert (payload["reference_trials"], payload["nop_trials"], payload["exploit_trials"]) == (
        3,
        3,
        3,
    )
    assert payload["task_package"] == str(s.repaired)
    assert payload["original_task_package"] == str(s.original)
    assert payload["fixed_task"] == "independent-heldout"
    assert payload["task_package_digest"] == task_directory_digest(s.repaired)
    assert payload["candidate_digest"] == "sha256:known-candidate"
    asset = s.queue.events[-1]
    assert asset["event_type"] == "asset.verified"
    assert asset["status"] == "confirmed"
    assert asset["payload"]["kind"] == "repaired_task"
    assert asset["payload"]["qualification_event_id"] == qualified["event_id"]
    assert asset["payload"]["dataset_id"] == "fixture"
    assert asset["payload"]["item_id"] == "format-task"
    assert result["retry_count"] == 0
    assert result["first_qualification"] == result["final_qualification"]


@pytest.mark.parametrize(
    "group,score",
    [
        ("oracle", 0.0),
        ("oracle", 0.5),
        ("nop", 1.0),
        ("nop", 0.1),
        ("exploit", 1.0),
        ("exploit", 0.5),
    ],
)
def test_any_incorrect_control_rejects_repair_without_promoting(setup, group, score) -> None:
    s = setup
    if group == "exploit":
        s.replay.repaired_reward = score
    else:
        s.backend.rewards[group] = [score, *s.backend.rewards[group][1:]]
    result = s.worker.qualify_repair(s.envelope)
    assert result["qualified"] is False
    assert result["measured"] is True
    assert result["event"]["event_type"] == "task_quality.repair_rejected"
    assert result["event"]["status"] == "refuted"
    assert result["retry_count"] == 0
    assert result["retry_status"] == "callback_absent"
    assert not any(
        event["event_type"] in {"task_quality.repair_qualified", "asset.verified"}
        for event in s.queue.events
    )


@pytest.mark.parametrize(
    "failure",
    ["missing_reward", "boolean_reward", "exception", "status", "reused_control", "reused_exploit"],
)
def test_incomplete_or_reused_controls_never_qualify(setup, failure: str) -> None:
    s = setup
    if failure == "missing_reward":
        s.backend.rewards["oracle"] = [None, 1.0, 1.0]
    elif failure == "boolean_reward":
        s.backend.rewards["oracle"] = [True, 1.0, 1.0]
    elif failure == "exception":
        s.backend.exception = "infrastructure failure"
    elif failure == "status":
        s.backend.status = "failed"
    elif failure == "reused_control":
        s.backend.same_evidence = True
    else:
        s.replay.same_evidence = True
    result = s.worker.qualify_repair(s.envelope)
    assert result["qualified"] is False
    assert result["measured"] is False
    assert result["event"]["event_type"] == "task_quality.repair_unavailable"
    assert result["event"]["status"] == "provisional"
    assert result["retry_count"] == 0
    assert result["retry_status"] == "execution_unavailable"


def test_repair_job_chains_the_live_audit_and_qualification_before_ack(setup) -> None:
    s = setup
    s.envelope["event_type"] = "task_quality.repair_task"
    s.queue.jobs = [s.envelope, {**s.envelope, "event_type": "reward_seeker.rebreak_task"}]
    result = s.worker.run_once()
    assert result is not None and result["complete"] is True
    assert result["qualification"]["qualified"] is True
    assert len(s.replay.calls) == 5
    assert s.queue.acks == ["job-1"]
    assert not s.queue.releases
    assert s.queue.claims[0][1] == JOB_TYPES
    assert len(s.queue.renewals) == 12  # static + 2 differential + 9 independent controls
    assert s.worker.run_once() is None
    assert s.queue.jobs[0]["event_type"] == "reward_seeker.rebreak_task"


def test_ambiguous_or_missing_exploit_input_does_not_run_controls(setup) -> None:
    s = setup
    s.envelope["payload"]["passing_runs"] = ["trial", "another-trial"]
    with pytest.raises(ValueError, match="explicit exploit"):
        s.worker.qualify_repair(s.envelope)
    assert not s.backend.calls
    assert not s.replay.calls


def test_continuous_worker_reports_failed_job_releases_then_consumes_next(setup) -> None:
    s = setup
    broken = {**s.envelope, "event_id": "bad-job", "payload": {}}
    s.queue.jobs = [broken, s.envelope]
    stop = threading.Event()
    s.queue.stop = stop
    s.worker.run(stop, poll_seconds=0)
    assert s.queue.releases == ["bad-job"]
    assert s.queue.acks == ["job-1"]
    failure = s.queue.events[0]
    assert failure["event_type"] == "task_quality.failed"
    assert failure["payload"]["job_id"] == "bad-job"
    assert "task_package" in failure["payload"]["error"]


@pytest.mark.parametrize("mutate", ["package", "submission"])
def test_mutating_inputs_cannot_publish_a_confirmation(setup, mutate: str) -> None:
    s = setup

    def before(trial_dir, grader, work):
        if s.replay.calls:
            path = (
                s.repaired / "tests/test.py"
                if mutate == "package"
                else trial_dir / "verifier/agent.diff"
            )
            path.write_text("changed during replay\n")

    s.replay.before = before
    with pytest.raises(ValueError, match="changed during"):
        s.worker.process_job(s.envelope)
    assert [event["status"] for event in finding_events(s.queue)] == ["provisional"]


def test_queued_package_digest_is_not_silently_rebound(setup) -> None:
    s = setup
    s.envelope["payload"]["task_package_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="no longer matches"):
        s.worker.process_job(s.envelope)
    assert not s.replay.calls
    assert not s.queue.events


def test_seeker_immutable_candidate_replays_same_files_on_both_graders_and_controls(setup) -> None:
    s = setup
    calls = []
    s.envelope["payload"].update(
        exploit_files={"out.txt": "wrong answer"}, exploit_run="trial", target_dir="/app"
    )

    def candidate_runner(task, **kwargs):
        calls.append((task, kwargs))
        assert kwargs["files"] == {"out.txt": "wrong answer"}
        assert kwargs["backend"] == "docker"
        assert kwargs["target_dir"] == "/app"
        return {
            "status": "measured",
            "executed": True,
            "reward": float(task == s.original),
            "error": None,
            "output_dir": str(kwargs["output_dir"]),
        }

    s.worker.candidate_runner = candidate_runner
    s.worker.process_job(s.envelope)
    assert finding_events(s.queue)[-1]["status"] == "confirmed"
    assert [task for task, _ in calls] == [s.original, s.repaired]
    assert s.worker.qualify_repair(s.envelope)["qualified"] is True
    assert len(calls) == 5
    assert not s.replay.calls


def test_candidate_plan_is_not_executed_evidence(setup) -> None:
    s = setup
    s.envelope["payload"].update(exploit_files={"out.txt": "wrong"}, exploit_run="trial")
    s.worker.candidate_runner = lambda task, **kwargs: {
        "status": "measured",
        "executed": False,
        "reward": 0.0,
        "output_dir": str(kwargs["output_dir"]),
        "error": None,
    }
    assert s.worker.process_job(s.envelope)["complete"] is False
    assert finding_events(s.queue)[-1]["status"] == "provisional"


# --------------------------------------------------------------- annotations


def _annotation_envelope(s, **payload) -> dict[str, Any]:
    return {
        **s.envelope,
        "event_type": "task_quality.annotation_task",
        "payload": {
            "dataset_id": "swebench-annotations",
            "item_id": "django__django-17084",
            "source_id": "swe-anno-1",
            "source_kind": "public_annotation",
            "license": "MIT",
            "provenance": {"loader": "swebench_annotations", "source_id": "swe-anno-1"},
            "packaging_state": "annotation_only",
            "annotation": {
                "underspecified": "major",
                "unfair_tests": "minor",
                "comment": "Instruction never states the required trailing newline.",
            },
            **payload,
        },
    }


def test_annotation_only_job_records_provisional_finding(setup) -> None:
    s = setup
    result = s.worker.process_job(_annotation_envelope(s))
    assert result["complete"] is True
    events = finding_events(s.queue)
    assert len(events) == 1
    event = events[0]
    assert event["status"] == "provisional"
    assert event["payload"]["phase"] == "human_annotation"
    assert event["payload"]["execution_available"] is False
    assert event["payload"]["annotation"]["underspecified"] == "major"
    assert event["payload"]["annotation"]["unfair_tests"] == "minor"
    assert event["payload"]["provenance"]["source_id"] == "swe-anno-1"
    assert event["payload"]["missing_prerequisites"]
    finding = Finding.model_validate(event["payload"]["finding"])
    assert finding.verdict == "unknown"
    assert finding.measurement == "not_run"
    assert finding.refutation.status == "unverified"
    assert finding.severity == "high"
    assert finding.category == "false_negative"
    assert finding.root_cause == ["unstated_requirement"]
    assert "trailing newline" in (finding.summary or "")
    assert s.queue.events[0]["event_type"] == "task_quality.started"
    assert s.queue.events[-1]["event_type"] == "task_quality.completed"
    assert s.queue.events[-1]["status"] == "provisional"
    assert not s.replay.calls
    assert not s.backend.calls
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)


def test_annotation_job_is_claimed_and_acked_through_run_once(setup) -> None:
    s = setup
    s.queue.jobs.append(_annotation_envelope(s))
    result = s.worker.run_once()
    assert result is not None and result["complete"] is True
    assert s.queue.claims[0][1] == JOB_TYPES
    assert "task_quality.annotation_task" in s.queue.claims[0][1]
    assert s.queue.acks == ["job-1"]
    assert not s.queue.releases


def test_runnable_annotation_confirms_only_by_real_differential(setup) -> None:
    s = setup
    envelope = _annotation_envelope(
        s, task_dir="original", repaired_task="repaired", failing_runs=["trial"]
    )
    s.replay.original_reward, s.replay.repaired_reward = 0.0, 1.0
    result = s.worker.process_job(envelope)
    assert result["complete"] is True
    events = finding_events(s.queue)
    assert [event["status"] for event in events] == ["provisional", "confirmed"]
    assert events[0]["payload"]["execution_available"] is False
    assert events[-1]["payload"]["execution_available"] is True
    assert events[-1]["payload"]["annotation"]["underspecified"] == "major"
    assert events[-1]["payload"]["provenance"]["source_id"] == "swe-anno-1"
    final = Finding.model_validate(events[-1]["payload"]["finding"])
    assert final.verdict == "fail"
    assert final.refutation.status == "verified"
    assert final.severity == "high"
    assert "trailing newline" in (final.summary or "")
    assert [call["grader"].package for call in s.replay.calls] == [s.original, s.repaired]
    assert all(call["trial"] == s.source for call in s.replay.calls)
    assert s.queue.events[-1]["event_type"] == "task_quality.completed"
    assert s.queue.events[-1]["status"] == "applied"
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)


@pytest.mark.parametrize("rewards", [(1.0, 1.0), (0.0, 0.0), (1.0, 0.0)])
def test_runnable_annotation_refutes_when_differential_disagrees(setup, rewards) -> None:
    s = setup
    envelope = _annotation_envelope(
        s, task_dir="original", repaired_task="repaired", failing_runs=["trial"]
    )
    s.replay.original_reward, s.replay.repaired_reward = rewards
    result = s.worker.process_job(envelope)
    assert result["complete"] is True
    assert [event["status"] for event in finding_events(s.queue)] == [
        "provisional",
        "refuted",
    ]
    final = Finding.model_validate(finding_events(s.queue)[-1]["payload"]["finding"])
    assert final.verdict == "pass"
    assert final.refutation.status == "refuted"


def test_task_dir_alone_is_not_confirmation_evidence(setup) -> None:
    """A resolvable ``task_dir`` without repaired package/runs stays provisional."""
    s = setup
    result = s.worker.process_job(_annotation_envelope(s, task_dir="original"))
    assert result["complete"] is True
    events = finding_events(s.queue)
    assert len(events) == 1 and events[0]["status"] == "provisional"
    assert events[0]["payload"]["execution_available"] is False
    missing = events[0]["payload"]["missing_prerequisites"]
    assert any("repaired" in entry for entry in missing)
    finding = Finding.model_validate(events[0]["payload"]["finding"])
    assert finding.subject.task_digest == task_directory_digest(s.original)
    assert events[0]["payload"]["static"]
    assert not s.replay.calls


def test_declared_but_missing_task_dir_is_a_missing_prerequisite(setup) -> None:
    s = setup
    result = s.worker.process_job(
        _annotation_envelope(s, task_dir="absent", repaired_task="repaired")
    )
    assert result["complete"] is True
    events = finding_events(s.queue)
    assert [event["status"] for event in events] == ["provisional"]
    assert events[0]["payload"]["execution_available"] is False
    assert events[0]["payload"]["missing_prerequisites"]
    assert not s.replay.calls


def test_unmeasured_annotation_execution_blocks_releases_and_redelivers(setup) -> None:
    s = setup
    envelope = _annotation_envelope(
        s, task_dir="original", repaired_task="repaired", failing_runs=["trial"]
    )
    s.replay.error = "container failed"
    s.queue.jobs.append(envelope)
    result = s.worker.run_once()
    assert result is not None and result["complete"] is False
    assert s.queue.events[-1]["event_type"] == "task_quality.blocked"
    assert s.queue.releases == ["job-1"] and not s.queue.acks
    first_ids = [event["payload"]["finding_id"] for event in finding_events(s.queue)]
    s.replay.error = None
    s.replay.original_reward, s.replay.repaired_reward = 0.0, 1.0
    s.queue.jobs.append(envelope)
    assert s.worker.run_once()["complete"] is True
    assert s.queue.acks == ["job-1"]
    events = finding_events(s.queue)
    # Redelivery deterministically re-derives the same finding identities.
    assert [event["payload"]["finding_id"] for event in events[2:]] == first_ids
    assert events[-1]["status"] == "confirmed"


def test_annotation_candidate_input_runs_standard_differential(setup) -> None:
    s = setup
    calls = []
    envelope = _annotation_envelope(
        s,
        task_dir="original",
        repaired_task="repaired",
        exploit_files={"out.txt": "wrong answer"},
    )

    def candidate_runner(task, **kwargs):
        calls.append(task)
        return {
            "status": "measured",
            "executed": True,
            "reward": float(task == s.original),
            "error": None,
            "output_dir": str(kwargs["output_dir"]),
        }

    s.worker.candidate_runner = candidate_runner
    result = s.worker.process_job(envelope)
    assert result["complete"] is True
    events = finding_events(s.queue)
    assert [event["status"] for event in events] == [
        "provisional",
        "provisional",
        "confirmed",
    ]
    directions = [event["payload"]["direction"] for event in events]
    assert directions == ["wrong_pass", "unfair_fail", "wrong_pass"]
    assert [task for task in calls] == [s.original, s.repaired]


def test_annotation_summary_uses_rationale_when_comment_absent(setup) -> None:
    s = setup
    envelope = _annotation_envelope(
        s,
        annotation={
            "underspecified": "major",
            "rationale": "The grader rejects answers the instruction never excludes.",
            "annotator": "human-7",
        },
    )
    result = s.worker.process_job(envelope)
    assert result["complete"] is True
    event = finding_events(s.queue)[0]
    assert event["payload"]["annotation"]["rationale"] == (
        "The grader rejects answers the instruction never excludes."
    )
    assert event["payload"]["annotation"]["annotator"] == "human-7"
    finding = Finding.model_validate(event["payload"]["finding"])
    assert finding.summary == "The grader rejects answers the instruction never excludes."
    assert finding.severity == "high"


@pytest.mark.parametrize(
    "annotation",
    [
        {"underspecified": "none", "unfair_tests": "none", "severity": "none"},
        {"underspecified": "no", "unfair_tests": "fair"},
        {"underspecified": False, "unfair_tests": 0, "severity": "none"},
    ],
)
def test_all_negative_annotation_records_no_defect(setup, annotation) -> None:
    """Explicitly negative axes never invent a defect, severity, or cause."""
    s = setup
    result = s.worker.process_job(_annotation_envelope(s, annotation=annotation))
    assert result["complete"] is True
    events = finding_events(s.queue)
    assert len(events) == 1 and events[0]["status"] == "provisional"
    finding = Finding.model_validate(events[0]["payload"]["finding"])
    assert finding.severity == "low"
    assert finding.root_cause == []
    assert finding.summary == "Human annotation asserts no task defect"
    assert "unfair failure" not in (finding.summary or "")
    assert not s.replay.calls


def test_annotation_only_never_resolves_scans_or_reads_a_package(setup) -> None:
    """Without declared packages the backend and reader stay untouched."""
    s = setup
    calls: list[str] = []
    resolve, static = s.backend.resolve, s.backend.static
    s.backend.resolve = lambda package: calls.append("resolve") or resolve(package)
    s.backend.static = lambda task: calls.append("static") or static(task)
    s.worker.reader = lambda finding, bundle: calls.append("reader") or finding
    result = s.worker.process_job(_annotation_envelope(s))
    assert result["complete"] is True
    assert calls == []
    assert not s.replay.calls
    assert not s.backend.calls


@pytest.mark.parametrize("candidate_kind", ["files", "solution_bundle"])
@pytest.mark.parametrize("backend_name", ["modal", "auto"])
def test_cloud_candidates_forward_one_policy_to_both_graders(
    setup, monkeypatch, candidate_kind, backend_name
) -> None:
    s = setup
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    policy = ExecutionBackendConfig(
        backend=backend_name,
        modal_app_name="worker-test-tq",
        spend_ledger=s.root / "cloud-spend.json",
        approval="unit-test-only; no provider calls",
        max_usd=2.0,
        billing_tag="worker-test",
    )
    calls = []

    def candidate_runner(task, **kwargs):
        calls.append((task, kwargs))
        assert {key: kwargs[key] for key in policy.candidate_kwargs()} == policy.candidate_kwargs()
        return {
            "status": "measured",
            "executed": True,
            "reward": float(task == s.original),
            "error": None,
            "output_dir": str(kwargs["output_dir"]),
        }

    payload = {"passing_runs": [], "target_dir": "/app"}
    if candidate_kind == "files":
        payload["exploit_files"] = {"out.txt": "wrong answer"}
    else:
        bundle = s.root / "immutable-solution"
        bundle.mkdir()
        (bundle / "solve.sh").write_text("#!/bin/sh\nprintf wrong > /app/out.txt\n")
        payload["solution_bundle"] = str(bundle)
    s.envelope["payload"].update(payload)
    worker = TaskQualityLiveWorker(
        s.queue,
        repo_root=s.root,
        backend=s.backend,
        execution_config=policy,
        candidate_runner=candidate_runner,
        clock=lambda: NOW,
    )

    assert worker.process_job(s.envelope)["complete"] is True
    assert [task for task, _ in calls] == [s.original, s.repaired]
    assert all(kwargs["backend"] == "modal" for _, kwargs in calls)
    assert all(kwargs["allow_local_docker"] is False for _, kwargs in calls)
    assert not s.replay.calls
    assert not policy.spend_ledger.exists()


def test_cloud_retained_replays_share_durable_gate_and_attribution(setup, monkeypatch) -> None:
    s = setup
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    calls = []

    def replay(source, grader, work, **kwargs):
        calls.append(kwargs)
        assert kwargs["environment"] == "modal"
        gate = kwargs["spend_gate"]
        assert gate.approval == "unit-test-only"
        assert gate.cap_usd == 2.0
        assert gate.spend_ledger == s.root / "shared-cloud-spend.json"
        assert gate.modal_app_name == "worker-test-tq"
        assert gate.billing_tag == "worker-test"
        return {
            "regraded": float(grader.package == s.original),
            "outcome": "flips",
            "job_dir": str(work),
        }

    worker = TaskQualityLiveWorker(
        s.queue,
        repo_root=s.root,
        backend="modal",
        modal_app_name="worker-test-tq",
        spend_ledger=s.root / "shared-cloud-spend.json",
        approval="unit-test-only",
        max_usd=2.0,
        billing_tag="worker-test",
        replay_runner=replay,
        clock=lambda: NOW,
    )
    worker.backend = s.backend
    assert worker.process_job(s.envelope)["complete"] is True
    assert len(calls) == 2
    assert calls[0]["spend_gate"] is calls[1]["spend_gate"]
    assert worker.execution_config.allow_local_docker is False
    assert not (s.root / "shared-cloud-spend.json").exists()


def test_cloud_repair_routes_all_nine_controls_without_docker(setup, monkeypatch) -> None:
    s = setup
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    made = []

    class ModalBackend(QABackend):
        environment = "modal"

        def __init__(self, *, repo_root, spend_gate, modal_app_name, billing_tag):
            super().__init__(repo_root)
            self.spend_gate = spend_gate
            self.modal_app_name = modal_app_name
            self.billing_tag = billing_tag
            made.append(self)

    monkeypatch.setitem(
        sys.modules, "evallab.qa.task_modal", SimpleNamespace(ModalBackend=ModalBackend)
    )
    candidates = []

    def candidate_runner(task, **kwargs):
        candidates.append(kwargs)
        assert kwargs["backend"] == "modal"
        assert kwargs["allow_local_docker"] is False
        return {
            "status": "measured",
            "executed": True,
            "reward": 0.0,
            "output_dir": str(kwargs["output_dir"]),
        }

    (s.root / "reference.diff").write_text(PATCH)
    s.envelope["payload"].update(
        passing_runs=[],
        exploit_files={"out.txt": "wrong"},
        reference_patch="reference.diff",
    )
    worker = TaskQualityLiveWorker(
        s.queue,
        repo_root=s.root,
        backend="modal",
        modal_app_name="worker-test-tq",
        spend_ledger=s.root / "shared-cloud-spend.json",
        approval="unit-test-only",
        max_usd=2.0,
        billing_tag="worker-test",
        candidate_runner=candidate_runner,
        clock=lambda: NOW,
    )
    worker.backend = s.backend
    assert worker.qualify_repair(s.envelope)["qualified"] is True
    assert len(made) == 1
    assert len(made[0].calls) == 6
    assert len(candidates) == 3
    assert made[0].bound_patches == [(s.repaired, s.root / "reference.diff")]
    assert made[0].spend_gate.spend_ledger == s.root / "shared-cloud-spend.json"
    assert made[0].modal_app_name == "worker-test-tq"
    assert made[0].billing_tag == "worker-test"
    assert not s.backend.calls
    assert not s.replay.calls


@pytest.mark.parametrize("refusal", ["environment", "explicit", "late_environment"])
@pytest.mark.parametrize("operation", ["candidate", "retained", "repair"])
def test_disabled_local_execution_refuses_before_any_collaborator(
    setup, monkeypatch, refusal, operation
) -> None:
    s = setup
    kwargs = {}
    if refusal == "environment":
        monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
        kwargs["allow_local_docker"] = True
    elif refusal == "explicit":
        kwargs["execution_config"] = ExecutionBackendConfig(allow_local_docker=True)
        kwargs["allow_local_docker"] = False
    worker = TaskQualityLiveWorker(
        s.queue,
        repo_root=s.root,
        backend=s.backend,
        replay_runner=s.replay,
        candidate_runner=lambda *_args, **_kwargs: pytest.fail("candidate runner must not start"),
        **kwargs,
    )
    if refusal == "late_environment":
        monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    if operation == "candidate":
        s.envelope["payload"].update(exploit_files={"out.txt": "wrong"}, exploit_run="trial")
    action = worker.qualify_repair if operation == "repair" else worker.process_job

    with pytest.raises(LocalDockerDisabledError, match="Local Docker execution is disabled"):
        action(s.envelope)
    assert not s.replay.calls
    assert not s.backend.calls
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)


def test_cloud_retained_replay_requires_persisted_approval_before_execution(setup) -> None:
    s = setup
    worker = TaskQualityLiveWorker(
        s.queue,
        repo_root=s.root,
        backend=s.backend,
        execution_config=ExecutionBackendConfig(backend="modal", allow_local_docker=False),
        replay_runner=s.replay,
    )
    with pytest.raises(ValueError, match="spend_ledger"):
        worker.process_job(s.envelope)
    assert not s.replay.calls


@pytest.mark.parametrize("final_passes", [True, False])
def test_measured_rejection_gets_one_new_attested_package_and_at_most_one_retry(
    setup, final_passes
) -> None:
    s = setup
    original_digest = task_directory_digest(s.original)
    old_digest = task_directory_digest(s.repaired)
    old_grader = (s.repaired / "tests/test.py").read_bytes()
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, float(final_passes), 1.0, 1.0]
    requests = []

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    result = retry_worker(s, repair).qualify_repair(s.envelope)
    first, final = result["first_qualification"], result["final_qualification"]
    staged = Path(final["task_package"])
    assert len(requests) == 1
    assert result["retry_count"] == 1
    assert result["retry_status"] == "completed"
    assert first["qualified"] is False and first["measured"] is True
    assert final["qualified"] is final_passes
    assert result["qualified"] is final_passes and result["measured"] is True
    assert first["task_package"] == str(s.repaired)
    assert first["task_package_digest"] == old_digest
    assert staged != s.repaired
    assert staged.is_relative_to(s.root / "runs/task-quality-live")
    assert final["task_package_digest"] == task_directory_digest(staged) != old_digest
    assert (staged / "tests/test.py").read_bytes() == RETRY_GRADER
    assert (s.repaired / "tests/test.py").read_bytes() == old_grader
    assert task_directory_digest(s.repaired) == old_digest
    assert task_directory_digest(s.original) == original_digest
    assert (staged / "instruction.md").read_bytes() == (s.repaired / "instruction.md").read_bytes()
    assert (staged / "task.toml").read_bytes() == (s.repaired / "task.toml").read_bytes()
    assert s.backend.calls == (
        [(s.repaired, "oracle")] * 3 + [(s.repaired, "nop")] * 3
        + [(staged, "oracle")] * 3 + [(staged, "nop")] * 3
    )
    assert len(s.replay.calls) == 6
    assert all(call["trial"] == s.source for call in s.replay.calls)
    assert [call["grader"].package for call in s.replay.calls] == [s.repaired] * 3 + [staged] * 3
    controls = [
        event["payload"] for event in s.queue.events
        if event["event_type"] == "task_quality.repair_control"
    ]
    assert len(controls) == 18
    assert len({control["evidence_ref"] for control in controls}) == 18
    assert [control["retry_count"] for control in controls] == [0] * 9 + [1] * 9
    assert all(control["task_package_digest"] == final["task_package_digest"] for control in controls[9:])
    payload = final["event"]["payload"]
    assert payload["first_qualification_event_id"] == first["event"]["event_id"]
    assert payload["repair_output_sha256"] == hashlib.sha256(RETRY_GRADER).hexdigest()
    assert payload["repair_call"]["call_id"] == "trusted-repair-call"
    assert payload["repair_call"]["response_sha256"] == payload["repair_output_sha256"]
    assets = [event for event in s.queue.events if event["event_type"] == "asset.verified"]
    assert len(assets) == int(final_passes)
    if assets:
        assert assets[0]["payload"]["task_package_digest"] == final["task_package_digest"]
        assert assets[0]["payload"]["qualification_event_id"] == final["event"]["event_id"]


@pytest.mark.parametrize("outcome", ["passed", "unavailable", "reused_evidence"])
def test_retry_callback_is_not_invoked_for_passing_or_unavailable_controls(setup, outcome) -> None:
    s = setup
    if outcome == "unavailable":
        s.backend.rewards["oracle"] = [None, 1.0, 1.0]
    elif outcome == "reused_evidence":
        s.backend.same_evidence = True

    def forbidden(request):
        pytest.fail("passed or unmeasured controls cannot invoke repair")

    result = retry_worker(s, forbidden).qualify_repair(s.envelope)
    assert result["retry_count"] == 0
    assert result["qualified"] is (outcome == "passed")
    assert result["measured"] is (outcome == "passed")
    assert result["first_qualification"] == result["final_qualification"]
    assert len(s.backend.calls) == 6
    assert len(s.replay.calls) == 3
    assert not any("retry_" in event["event_type"] for event in s.queue.events)


def test_retry_receives_bounded_traceback_control_diagnostics_and_prior_evidence(setup) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    traceback = "Traceback (most recent call last):\n" + "detail\n" * 10_000 + "AssertionError: spacing"
    s.backend.extra_facts = {"pytest_traceback": traceback, "agent_stdout": "NOT_FOR_REPAIR"}
    s.envelope["payload"]["repair_evidence"] = {"call_id": "prior-repair", "rationale": "spacing fix"}
    requests = []

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    result = retry_worker(s, repair).qualify_repair(s.envelope)
    request = requests[0]
    assert len(request.diagnostics) <= MAX_REPAIR_DIAGNOSTICS_CHARS
    assert "Traceback (most recent call last)" in request.diagnostics
    assert "AssertionError: spacing" in request.diagnostics
    assert "reference #1: reward=0.0, expected=1.0" in request.diagnostics
    assert "nop #3: reward=0.0, expected=0.0" in request.diagnostics
    assert "exploit #3: reward=0.0, expected=0.0" in request.diagnostics
    assert "NOT_FOR_REPAIR" not in request.diagnostics
    assert "prior-repair" in request.prior_repair_evidence
    assert result["first_qualification"]["event"]["event_id"] in request.prior_repair_evidence
    assert request.original_task == s.original
    assert request.repaired_task == s.repaired
    assert request.repaired_task_digest == task_directory_digest(s.repaired)
    assert request.grader_path == "tests/test.py"
    assert request.grader_source.encode("utf-8") == (s.repaired / "tests/test.py").read_bytes()


@pytest.mark.parametrize(
    "invalid",
    ["mutated_output", "no_call", "no_served_model", "providerless", "fallback", "wrong_subject", "wrong_work"],
)
def test_unattested_or_overwritten_retry_output_is_refused_before_new_execution(setup, invalid) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0]
    old_digest = task_directory_digest(s.repaired)
    requests = []

    def repair(request):
        requests.append(request)
        response = attested_repair(request)
        if invalid == "mutated_output":
            return replace(response, raw_output=b"assert False\n")
        changes = {
            "no_served_model": {"served_model": None},
            "providerless": {"served_model": "fixture-model"},
            "fallback": {"quarantined_fallback": True},
            "wrong_subject": {"subject_id": "another-task"},
            "wrong_work": {"work_kind": WorkKind.REWARD_SEEKING},
        }
        return replace(
            response,
            call=None if invalid == "no_call" else replace(response.call, **changes[invalid]),
        )

    result = retry_worker(s, repair).qualify_repair(s.envelope)
    assert len(requests) == 1
    assert result["retry_count"] == 1 and result["retry_status"] == "refused"
    assert result["qualified"] is False and result["measured"] is True
    assert result["first_qualification"] == result["final_qualification"]
    assert result["task_package"] == str(s.repaired)
    assert task_directory_digest(s.repaired) == old_digest
    assert len(s.backend.calls) == 6 and len(s.replay.calls) == 3
    assert s.queue.events[-1]["event_type"] == "task_quality.repair_retry_refused"
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)
    assert not list(s.root.glob("runs/task-quality-live/*/repaired-task"))


def test_retry_can_use_only_the_measured_control_retained_verifier_log(setup, monkeypatch) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    retained = s.root / "runs/failed-reference"
    (retained / "verifier").mkdir(parents=True)
    (retained / "verifier/test-stdout.txt").write_text(
        "pytest progress\n" * 10_000
        + "Traceback (most recent call last):\nAssertionError: retained pytest failure\n"
    )
    control = s.backend.control
    requests = []

    def observed(task, agent, **kwargs):
        observation = control(task, agent, **kwargs)
        if len(s.backend.calls) == 1:
            return observation.model_copy(
                update={"facts": {**observation.facts, "job_path": str(retained)}}
            )
        return observation

    monkeypatch.setattr(s.backend, "control", observed)

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    result = retry_worker(s, repair).qualify_repair(s.envelope)
    assert result["qualified"] is True
    assert len(requests) == 1
    assert "retained pytest failure" in requests[0].diagnostics
    assert "retained verifier/test-stdout.txt" in requests[0].diagnostics


@pytest.mark.parametrize("locator", ["outside_runs", "symlink"])
def test_retry_does_not_read_arbitrary_or_symlinked_verifier_logs(
    setup, monkeypatch, locator
) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    outside = s.root / "outside-control"
    (outside / "verifier").mkdir(parents=True)
    (outside / "verifier/test-stdout.txt").write_text("PRIVATE_LOG_NOT_AUTHORIZED_FOR_REPAIR")
    evidence = outside
    if locator == "symlink":
        (s.root / "runs").mkdir()
        evidence = s.root / "runs/redirected-control"
        evidence.symlink_to(outside, target_is_directory=True)
    control = s.backend.control
    requests = []

    def observed(task, agent, **kwargs):
        observation = control(task, agent, **kwargs)
        if len(s.backend.calls) == 1:
            return observation.model_copy(
                update={"facts": {**observation.facts, "job_path": str(evidence)}}
            )
        return observation

    monkeypatch.setattr(s.backend, "control", observed)

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    assert retry_worker(s, repair).qualify_repair(s.envelope)["qualified"] is True
    assert len(requests) == 1
    assert "PRIVATE_LOG_NOT_AUTHORIZED_FOR_REPAIR" not in requests[0].diagnostics


@pytest.mark.parametrize("when", ["before_execution", "after_execution"])
def test_staged_grader_attestation_is_enforced_before_and_after_execution(
    setup, monkeypatch, when
) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    worker = retry_worker(s, attested_repair)
    if when == "before_execution":
        stage_repair = worker._stage_repair

        def mutate(job, response):
            staged = stage_repair(job, response)
            (staged.repaired / "tests/test.py").write_bytes(b"assert False\n")
            return staged

        monkeypatch.setattr(worker, "_stage_repair", mutate)
    else:
        control = s.backend.control

        def mutate(task, agent, **kwargs):
            observation = control(task, agent, **kwargs)
            if task.path != s.repaired:
                (task.path / "tests/test.py").write_bytes(b"assert False\n")
            return observation

        monkeypatch.setattr(s.backend, "control", mutate)
    with pytest.raises(ValueError, match="attested model output"):
        worker.qualify_repair(s.envelope)
    assert len(s.backend.calls) == (6 if when == "before_execution" else 7)
    assert len(s.replay.calls) == 3
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)


def test_retry_cannot_reuse_any_first_qualification_control_evidence(setup, monkeypatch) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    first_refs = []
    control = s.backend.control
    requests = []

    def observed(task, agent, **kwargs):
        observation = control(task, agent, **kwargs)
        if len(s.backend.calls) <= 6:
            first_refs.append(observation.facts["job_path"])
            return observation
        return observation.model_copy(
            update={
                "facts": {
                    **observation.facts,
                    "job_path": first_refs[len(s.backend.calls) - 7],
                }
            }
        )

    monkeypatch.setattr(s.backend, "control", observed)

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    result = retry_worker(s, repair).qualify_repair(s.envelope)
    assert len(requests) == 1 and result["retry_count"] == 1
    assert result["first_qualification"]["measured"] is True
    assert result["final_qualification"]["measured"] is False
    assert result["event"]["event_type"] == "task_quality.repair_unavailable"
    assert result["qualified"] is False
    assert len(s.backend.calls) == 12 and len(s.replay.calls) == 6
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)


def test_retry_preserves_the_pinned_retained_wrong_submission(setup) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]

    def changed_input(request):
        response = attested_repair(request)
        (s.source / "verifier/agent.diff").write_text(PATCH + "\n")
        return response

    with pytest.raises(ValueError, match="wrong-submission input changed"):
        retry_worker(s, changed_input).qualify_repair(s.envelope)
    assert len(s.backend.calls) == 6 and len(s.replay.calls) == 3
    assert not any(event["event_type"] == "asset.verified" for event in s.queue.events)


@pytest.mark.parametrize("final_outcome", ["qualified", "rejected", "unavailable"])
def test_repair_retry_preserves_queue_ack_and_release_semantics(setup, final_outcome) -> None:
    s = setup
    score = {"qualified": 1.0, "rejected": 0.0, "unavailable": None}[final_outcome]
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, score, 1.0, 1.0]
    s.envelope["event_type"] = "task_quality.repair_task"
    s.queue.jobs = [s.envelope]
    requests = []

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    result = retry_worker(s, repair).run_once()
    assert result is not None
    assert result["complete"] is (final_outcome != "unavailable")
    assert result["qualification"]["qualified"] is (final_outcome == "qualified")
    assert result["qualification"]["retry_count"] == len(requests) == 1
    assert s.queue.acks == ([] if final_outcome == "unavailable" else ["job-1"])
    assert s.queue.releases == (["job-1"] if final_outcome == "unavailable" else [])
    assert len(s.backend.calls) == 12 and len(s.replay.calls) == 8


@pytest.mark.parametrize(
    "path",
    ["/tests/test.py", "../tests/test.py", "tests/../instruction.md",
     "instruction.md", "tests/data.json", "tests//test.py", "tests/./test.py", "tests\\test.py"],
)
def test_repair_path_is_designated_by_trusted_constructor_not_model_or_queue(setup, path) -> None:
    s = setup
    with pytest.raises(ValueError, match="relative tests/ source"):
        TaskQualityLiveWorker(
            s.queue,
            repo_root=s.root,
            backend=s.backend,
            repair_callback=attested_repair,
            repair_grader_path=path,
        )
    assert not s.backend.calls and not s.replay.calls
    assert not s.queue.events


def test_repair_callback_requires_a_designated_grader(setup) -> None:
    s = setup
    with pytest.raises(ValueError, match="requires one designated grader"):
        TaskQualityLiveWorker(
            s.queue, repo_root=s.root, backend=s.backend, repair_callback=attested_repair
        )


@pytest.mark.parametrize("fallback", [False, True])
def test_concrete_transport_callback_attests_raw_grader_output_without_rewriting(
    setup, fallback
) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    s.backend.extra_facts = {"verifier_stdout": "Traceback:\nAssertionError: format"}
    raw = '# Preserve Unicode: café\r\nassert result.strip() == "answer"\r\n'.encode()
    requests = []
    responses = []

    class Transport:
        def complete(self, request):
            requests.append(request)
            return ModelResponse(
                text=raw.decode("utf-8"),
                served_model="fixture-provider/fixture-model",
                tokens_in=321,
                tokens_out=54,
                latency_ms=12,
                resolvedModelIsFallback=fallback,
            )

    callback = TracebackGraderRepair(Transport(), model="fixture-provider/fixture-model")

    def repair(request):
        response = callback(
            replace(request, audit_context={"failed_run": {"traceback": "context\n" * 10_000}})
        )
        responses.append(response)
        return response

    result = retry_worker(s, repair).qualify_repair(s.envelope)
    assert len(requests) == len(responses) == 1
    request, response = requests[0], responses[0]
    assert "complete replacement file" in request.prompt
    assert "no Markdown fences, JSON, diff" in request.prompt
    assert "AssertionError: format" in request.prompt
    assert "CURRENT GRADER (data)" in request.prompt
    assert "PRIOR REPAIR EVIDENCE (data)" in request.prompt
    assert len(request.prompt) < 120_000
    assert request.work_kind is WorkKind.REPAIR_PROPOSAL
    assert response.raw_output == raw
    assert response.call.prompt_sha256 == hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    assert response.call.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert response.call.quarantined_fallback is fallback
    assert response.call.tokens_in == 321 and response.call.tokens_out == 54
    assert response.call.latency_ms == 12
    assert result["qualified"] is not fallback
    assert result["retry_status"] == ("refused" if fallback else "completed")
    if not fallback:
        assert (Path(result["task_package"]) / "tests/test.py").read_bytes() == raw


def test_repair_transport_failure_is_reported_once_without_new_execution(setup) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0]
    requests = []

    class Transport:
        def complete(self, request):
            requests.append(request)
            raise RuntimeError("provider unavailable")

    callback = TracebackGraderRepair(Transport(), model="fixture-provider/fixture-model")
    result = retry_worker(s, callback).qualify_repair(s.envelope)
    assert len(requests) == 1
    assert result["retry_count"] == 1 and result["retry_status"] == "refused"
    assert "provider unavailable" in result["retry_error"]
    assert result["first_qualification"] == result["final_qualification"]
    assert len(s.backend.calls) == 6 and len(s.replay.calls) == 3


def test_reader_receives_audit_context_before_execution_and_journal_keeps_provenance(setup) -> None:
    s = setup
    calls = []
    observed = []

    class Retriever:
        def failed_run_summary(self, task_id):
            calls.append(("failed", task_id))
            return {
                "task_id": task_id, "split": "dev", "trace_id": "prior-failure",
                "reward": 0.0, "outcome": "failed",
            }

        def retrieve_similar_examples(self, query, **kwargs):
            calls.append(("related", query, kwargs))
            return []

    def reader(finding, bundle):
        assert not s.replay.calls
        observed.append(bundle["audit_context"])
        return finding

    s.envelope["payload"]["retrieval_query"] = "format whitespace result extra"
    worker = TaskQualityLiveWorker(
        s.queue, repo_root=s.root, backend=s.backend, replay_runner=s.replay,
        reader=reader, audit_retriever=Retriever(), audit_source_digest="a" * 64,
    )
    result = worker.process_job(s.envelope)
    assert result["complete"] is True
    assert len(observed) == 1
    assert observed[0]["failed_run"]["trace_id"] == "prior-failure"
    assert calls == [
        ("failed", "fixture/format-task"),
        ("related", "format whitespace result", {
            "kind": "task_quality", "top_k": 3, "exclude_task_ids": {"fixture/format-task"},
        }),
    ]
    static = next(
        event for event in finding_events(s.queue)
        if event["payload"]["phase"] == "static_reader"
    )
    assert static["payload"]["audit_context"] == observed[0]
    assert static["payload"]["audit_context"]["provenance"]["source_sha256"] == "a" * 64


def test_audit_bundle_without_retriever_marks_history_unavailable(setup) -> None:
    s = setup
    bundle = s.worker.audit_bundle(s.original, task_id="fixture/format-task")
    assert bundle["files"]["instruction.md"]
    assert bundle["audit_context"]["failed_run"] is None
    assert bundle["audit_context"]["related_issues"] == []
    assert bundle["audit_context"]["provenance"]["retriever_available"] is False
    assert not s.backend.calls and not s.replay.calls


def test_retry_callback_is_durably_limited_across_worker_reclaims(setup) -> None:
    s = setup
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 0.0, 1.0, 1.0]
    requests = []

    def repair(request):
        requests.append(request)
        return attested_repair(request)

    first = retry_worker(s, repair).qualify_repair(s.envelope)
    assert first["retry_count"] == 1 and not first["qualified"]
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0]
    reclaimed = retry_worker(s, repair).qualify_repair(s.envelope)
    assert len(requests) == 1
    assert reclaimed["retry_status"] == "refused"
    assert "already consumed" in reclaimed["retry_error"]
    assert reclaimed["qualified"] is False


def test_patch_based_grader_retry_keeps_exact_attested_patch_bytes(setup) -> None:
    s = setup
    (s.repaired / "tests/test.patch").write_bytes(b"original grader patch\n")
    s.backend.rewards["oracle"] = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    raw = (
        b"diff --git a/tests/test_answer.py b/tests/test_answer.py\n"
        b"--- a/tests/test_answer.py\n+++ b/tests/test_answer.py\n"
        b"@@ -1 +1 @@\n"
        b'-assert result == "answer"\n+assert result.strip() == "answer"\n'
    )
    requests = []

    class Transport:
        def complete(self, request):
            requests.append(request)
            return ModelResponse(
                text=raw.decode(), served_model="fixture-provider/fixture-model",
                tokens_in=50, tokens_out=20, latency_ms=1,
            )

    repair = TracebackGraderRepair(Transport(), model="fixture-provider/fixture-model")

    worker = TaskQualityLiveWorker(
        s.queue, repo_root=s.root, backend=s.backend, replay_runner=s.replay,
        repair_callback=repair, repair_grader_path="tests/test.patch", clock=lambda: NOW,
    )
    result = worker.qualify_repair(s.envelope)
    assert result["qualified"] is True and len(requests) == 1
    assert "grader is itself a patch file" in requests[0].prompt
    assert (Path(result["task_package"]) / "tests/test.patch").read_bytes() == raw
    assert (s.repaired / "tests/test.patch").read_bytes() == b"original grader patch\n"
    assert result["event"]["payload"]["repair_output_sha256"] == hashlib.sha256(raw).hexdigest()
