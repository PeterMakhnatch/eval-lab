"""Live Reward Seeker worker: streaming attempts, failure memory, and differential gating."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evallab.execution_backend import ExecutionBackendConfig
from evallab.hackbook.models import HackbookEntry
from evallab.production.seeker_worker import (
    FailedShortcutRecord,
    RewardSeekerLiveWorker,
    SeekerAttemptCandidate,
    SeekerEvidenceError,
    SeekerFailureMemory,
    SeekerRefusalError,
    compute_candidate_digest,
    compute_shortcut_signature,
    match_task_hackbook_cues,
    render_hackbook_cues_markdown,
    verify_differential_replay,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_execution_policy(monkeypatch):
    """Only nonexecuting shell syntax checks may run; never a container or provider."""
    monkeypatch.delenv("EVALLAB_DISABLE_LOCAL_DOCKER", raising=False)
    original_run = subprocess.run

    def no_process(argv, *args, **kwargs):
        if isinstance(argv, list) and len(argv) == 3 and argv[:2] == ["bash", "-n"]:
            return original_run(argv, *args, **kwargs)
        pytest.fail(f"Unexpected external process in worker unit test: {argv!r}")

    monkeypatch.setattr("subprocess.run", no_process)


class SplitLockError(ValueError):
    """Fake registry split-lock error."""


@dataclass
class FakeCorpusItem:
    dataset_id: str
    item_id: str
    split: str
    task_id: str
    task_digest: str = "sha256:item"
    source_path: str = "corpus.jsonl"
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeRegistry:
    def __init__(self, items: dict[tuple[str, str], FakeCorpusItem]) -> None:
        self._items = items

    def admit(self, dataset_id: str, item_id: str) -> FakeCorpusItem:
        item = self._items[(dataset_id, item_id)]
        if item.split != "dev":
            raise SplitLockError(f"sealed {item.split} split for {dataset_id}/{item_id}")
        return item

    def get(self, dataset_id: str, item_id: str) -> FakeCorpusItem:
        return self._items[(dataset_id, item_id)]


class FakeQueue:
    """In-memory ContinuousWorkQueue double implementing the approved synchronous API."""

    def __init__(self, *, registry: FakeRegistry | None = None) -> None:
        self.registry = registry
        self.pending: deque[dict[str, Any]] = deque()
        self.events: list[dict[str, Any]] = []
        self.chained_jobs: list[dict[str, Any]] = []
        self.acked: list[str] = []
        self.released: list[str] = []
        self.renewed: list[str] = []
        self._seq = 0
        self._idempotency: dict[str, dict[str, Any]] = {}

    def enqueue_job(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        run_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        self._seq += 1
        job = {
            "event_id": event_id,
            "run_id": run_id,
            "system": "queue",
            "event_type": job_type,
            "job_type": job_type,
            "seq": self._seq,
            "ts": "2026-10-24T00:00:00+00:00",
            "prefix_step": None,
            "status": "provisional",
            "payload": copy.deepcopy(payload),
            "lease_token": f"lease-{event_id}",
            "lease_until": "2026-10-24T00:05:00+00:00",
        }
        self.pending.append(job)
        return job

    def claim(
        self,
        worker_id: str,
        *,
        job_types: Any,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        del worker_id, lease_seconds
        for job in list(self.pending):
            if job["event_type"] in set(job_types):
                self.pending.remove(job)
                return copy.deepcopy(job)
        return None

    def append_event(
        self,
        event: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        frozen_eval_token: str | None = None,
    ) -> dict[str, Any]:
        del frozen_eval_token
        if idempotency_key and idempotency_key in self._idempotency:
            return copy.deepcopy(self._idempotency[idempotency_key])
        self._seq += 1
        stored = copy.deepcopy(event)
        stored["seq"] = self._seq
        self.events.append(stored)
        if idempotency_key:
            self._idempotency[idempotency_key] = stored

        payload = stored.get("payload") or {}
        if (
            stored.get("event_type") == "seeker.exploit_confirmed"
            and stored.get("status") == "confirmed"
            and payload.get("upstream_reward") == 1.0
            and payload.get("repaired_reward") == 0.0
        ):
            chained_payload = copy.deepcopy(payload)
            chained_payload["source_event_id"] = stored["event_id"]
            chained = {
                "event_id": f"job-chain-{len(self.chained_jobs) + 1}",
                "run_id": stored["run_id"],
                "system": "queue",
                "event_type": "task_quality.repair_task",
                "job_type": "task_quality.repair_task",
                "seq": self._seq + 1000,
                "ts": stored["ts"],
                "prefix_step": None,
                "status": "provisional",
                "payload": chained_payload,
            }
            self.chained_jobs.append(chained)
            self.pending.append(chained)
        return copy.deepcopy(stored)

    def read_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [event for event in self.events if int(event["seq"]) > after_seq]
        if run_id is not None:
            rows = [event for event in rows if event.get("run_id") == run_id]
        return copy.deepcopy(rows[:limit])

    def ack(self, event_id: str, *, lease_token: str) -> None:
        assert lease_token
        self.acked.append(event_id)

    def release(self, event_id: str, *, lease_token: str) -> None:
        assert lease_token
        self.released.append(event_id)

    def renew(self, event_id: str, *, lease_token: str, lease_seconds: int = 60) -> None:
        assert lease_token and lease_seconds > 0
        self.renewed.append(event_id)


def _entry(
    entry_id: str,
    slug: str,
    *,
    pattern: str,
    title: str,
    mechanism: str,
    goal: str,
    direction: str = "false_pass",
    taxonomy_class_id: str = "TS01",
    taxonomy_class: str = "weak_check",
    qa_category: str = "weak",
) -> HackbookEntry:
    return HackbookEntry.model_validate(
        {
            "schema": "evallab.hackbook.entry/v0",
            "id": entry_id,
            "slug": slug,
            "title": title,
            "taxonomy_class_id": taxonomy_class_id,
            "taxonomy_class": taxonomy_class,
            "side": "task",
            "direction": direction,
            "qa_category": qa_category,
            "sources": ["reward_seeker"],
            "mechanism": mechanism,
            "signatures": {
                "file_regexes": [
                    {"surface": "tests", "pattern": pattern, "description": title, "weight": 1.0}
                ]
            },
            "detection_recipe": {
                "kind": "static_check",
                "method_id": "reward_seeker",
                "summary": "Run a differential reward-seeker probe.",
                "steps": ["Grade the same candidate on original and repaired graders."],
            },
            "exploit_template": {
                "applicable": True,
                "mode": "submission-controlled output",
                "goal": goal,
                "preconditions": ["The candidate controls a file read by the grader."],
                "template_steps": ["Write the smallest output accepted by the weak check."],
            },
            "fix_pattern": {
                "fix_location": "grader",
                "summary": "Verify the computed value rather than a trusted surface artifact.",
                "checklist": ["reference passes", "nop fails", "exploit fails"],
            },
            "examples": [],
        }
    )


def _hackbook_entries() -> list[HackbookEntry]:
    return [
        _entry(
            "HB-TS01-001",
            "surface-workbook-status",
            pattern="zxqw_surface_marker",
            title="Surface workbook status is trusted",
            mechanism=(
                "The grader trusts a self reported surface workbook status marker and never "
                "recomputes the underlying answer."
            ),
            goal="Write the trusted status/output file without doing the full computation.",
        ),
        _entry(
            "HB-TS01-002",
            "trusted-answer-file",
            pattern="trusted_answer_file",
            title="Trusted answer file is submission controlled",
            mechanism=(
                "The grader reads a trusted answer file that the submission controls instead "
                "of checking the work product."
            ),
            goal="Place the accepted answer in the trusted file read by the grader.",
        ),
        _entry(
            "HB-TS05-001",
            "unstated-extra-requirement",
            pattern="zxqw_surface_marker",
            title="Unstated extra requirement",
            mechanism="The grader secretly requires an unstated extra artifact.",
            goal="Satisfy an unstated requirement.",
            direction="false_fail",
            taxonomy_class_id="TS05",
            taxonomy_class="infeasible_as_written",
            qa_category="false_negative",
        ),
    ]


def _make_task(
    root: Path,
    name: str = "task-a",
    *,
    instruction: str = (
        "Write the computed answer to /app/output.json. The zxqw surface workbook "
        "self reported status must be present.\n"
    ),
) -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    (task / "task.toml").write_text('[task]\nname = "task-a"\n', encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(
        "# zxqw_surface_marker\n"
        "# trusted_answer_file\n"
        "def test_output():\n"
        "    assert open('/app/output.json').read().strip()\n",
        encoding="utf-8",
    )
    return task


def _candidate(marker: str, entry_id: str, mechanism: str) -> SeekerAttemptCandidate:
    solve = f"#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' '{marker}' > /app/output.json\n"
    raw_stdout = json.dumps({"files": {"solve.sh": solve}})
    return SeekerAttemptCandidate(
        files={"solve.sh": solve},
        mechanism=mechanism,
        taxonomy_class="weak_check",
        hackbook_entry_id=entry_id,
        target_check="tests/test_outputs.py::test_output",
        files_written=("/app/output.json",),
        intended_work_skipped="full computed answer",
        confidence="high",
        exploit_found=True,
        model_route="test/synthetic-model",
        llm_stdout_sha256=hashlib.sha256(raw_stdout.encode()).hexdigest(),
        llm_solve_sha256=hashlib.sha256(solve.encode()).hexdigest(),
        served_provider="test",
        served_model="synthetic-model",
    )


BAD_CANDIDATE = _candidate(
    "BAD_OUTPUT",
    "HB-TS01-001",
    "Trust the zxqw surface workbook self reported status marker.",
)
EXPLOIT_CANDIDATE = _candidate(
    "EXPLOIT_OUTPUT",
    "HB-TS01-002",
    "Write the trusted answer file that the grader reads.",
)
HONEST_CANDIDATE = _candidate(
    "HONEST_OUTPUT",
    "HB-TS01-002",
    "Write the trusted answer file that the grader reads.",
)


class SequenceGenerator:
    def __init__(self, candidates: list[SeekerAttemptCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[str | None] = []

    def __call__(
        self, task: Any, attempt_index: int, cue: Any, cues: Any, memory: Any, workspace: Path
    ) -> SeekerAttemptCandidate:
        del task, attempt_index, cues, memory, workspace
        self.calls.append(cue.entry_id if cue is not None else None)
        index = min(len(self.calls) - 1, len(self.candidates) - 1)
        return self.candidates[index]


class MarkerGrader:
    """Deterministic candidate_runner double keyed by the candidate marker."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, task: Path, **kwargs: Any) -> dict[str, Any]:
        files: dict[str, str] = kwargs["files"]
        solve = files["solve.sh"]
        marker = next(
            name for name in ("BAD_OUTPUT", "EXPLOIT_OUTPUT", "HONEST_OUTPUT") if name in solve
        )
        grader = str(kwargs.get("grader") or "upstream")
        task_name = Path(task).name
        repaired_side = grader == "port" or task_name in {
            "repaired",
            "fixed-comparison",
            "comparison",
        }
        rewards = {
            "BAD_OUTPUT": (0.0, 0.0),
            "EXPLOIT_OUTPUT": (1.0, 0.0),
            "HONEST_OUTPUT": (1.0, 1.0),
        }
        reward = rewards[marker][1 if repaired_side else 0]
        self.calls.append(
            {
                "task": str(task),
                "grader": grader,
                "port_task": str(kwargs["port_task"]) if kwargs.get("port_task") else None,
                "marker": marker,
                "reward": reward,
            }
        )
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        passed = reward == 1.0
        ctrf_payload = {
            "results": {
                "tests": [
                    {
                        "name": "test_output",
                        "status": "passed" if passed else "failed",
                        "trace": "" if passed else "AssertionError: wrong output",
                    }
                ]
            }
        }
        ctrf = output_dir / "ctrf.json"
        ctrf.write_text(json.dumps(ctrf_payload), encoding="utf-8")
        # The declared trial dir must exist: confirmed evidence is pinned
        # under content custody and a missing receipt fails the promotion.
        trial = output_dir / "trial"
        trial.mkdir(parents=True, exist_ok=True)
        (trial / "result.json").write_text(
            json.dumps({"verifier_result": {"rewards": {"reward": reward}}}),
            encoding="utf-8",
        )
        (trial / "ctrf.json").write_text(json.dumps(ctrf_payload), encoding="utf-8")
        provenance = None
        if grader == "port":
            port_task = output_dir / "port-task"
            port_task.mkdir(parents=True, exist_ok=True)
            (port_task / "task.toml").write_text('[task]\nname = "port-task"\n', encoding="utf-8")
            provenance = {"task_dir": str(port_task)}
        return {
            "status": "measured",
            "reward": reward,
            "error": None,
            "output_dir": str(output_dir),
            "trial_dir": str(trial),
            "grader_provenance": provenance,
            "per_test": {
                "tests": [{"id": "test_output", "status": "passed" if passed else "failed"}],
                "sources": [str(ctrf)],
            },
        }


class SequenceGrader:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = deque(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, task: Path, **kwargs: Any) -> dict[str, Any]:
        result = dict(self.results.popleft())
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        result.setdefault("output_dir", str(output_dir))
        if "trial_dir" not in result:
            # The declared trial dir must exist: a missing declared receipt
            # blocks evidence pinning entirely.
            trial = output_dir / "trial"
            trial.mkdir(parents=True, exist_ok=True)
            (trial / "result.json").write_text(
                json.dumps(
                    {"verifier_result": {"rewards": {"reward": result.get("reward")}}}
                ),
                encoding="utf-8",
            )
            result["trial_dir"] = str(trial)
        result.setdefault("per_test", {"tests": [], "sources": []})
        self.calls.append({"task": str(task), **result})
        return result


def _worker(
    queue: FakeQueue,
    tmp_path: Path,
    *,
    generator: Any,
    grader: Any,
    max_attempts: int = 3,
    name: str = "work",
    **execution_kwargs: Any,
) -> RewardSeekerLiveWorker:
    return RewardSeekerLiveWorker(
        queue,  # type: ignore[arg-type]
        worker_id="reward-seeker-test",
        repo_root=REPO_ROOT,
        work_root=tmp_path / name,
        gym_path=tmp_path / "missing-gym.json",
        hackbook_entries=_hackbook_entries(),
        attempt_generator=generator,
        grader_fn=grader,
        max_attempts=max_attempts,
        **execution_kwargs,
    )


def _payload(original: Path, repaired: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dataset_id": "tb4-dev",
        "item_id": "task-a",
        "task_id": "task-a",
        "split": "dev",
        "measurable": True,
        "task_package": str(original),
        "repaired_task": str(repaired),
        "fixed_kind": "repaired_package",
    }
    payload.update(overrides)
    return payload


def test_live_worker_streams_attempts_memory_and_confirmed_differential(tmp_path: Path) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-seeker-1",
        event_id="job-1",
    )
    generator = SequenceGenerator([BAD_CANDIDATE, BAD_CANDIDATE, EXPLOIT_CANDIDATE])
    grader = MarkerGrader()
    worker = _worker(queue, tmp_path, generator=generator, grader=grader)

    outcome = asyncio.run(worker.run(max_jobs=1, poll_interval=0))[0]

    assert outcome.exploit_confirmed is True
    assert outcome.status == "confirmed"
    assert outcome.attempts_run == 2
    assert outcome.skipped_duplicates == 1
    assert outcome.original_reward == 1.0
    assert outcome.repaired_reward == 0.0
    assert set(outcome.matched_hackbook_ids) >= {"HB-TS01-001", "HB-TS01-002"}
    # The repeated BAD candidate was recognized from failure memory and not re-graded.
    assert [call["marker"] for call in grader.calls] == [
        "BAD_OUTPUT",
        "EXPLOIT_OUTPUT",
        "EXPLOIT_OUTPUT",
    ]
    assert [call["reward"] for call in grader.calls] == [0.0, 1.0, 0.0]
    assert outcome.failure_memory.has_candidate_digest(
        compute_candidate_digest(BAD_CANDIDATE.ensure_submission_files())
    )
    assert "HB-TS01-001" in outcome.failure_memory.blocked_entry_ids

    event_types = [event["event_type"] for event in queue.events]
    assert event_types == [
        "worker.started",
        "seeker.attempt_started",
        "seeker.grader_feedback",
        "seeker.attempt_started",
        "seeker.grader_feedback",
        "seeker.exploit_confirmed",
        "asset.verified",
        "worker.completed",
    ]
    assert all(event["system"] == "reward_seeker" for event in queue.events)
    assert all(
        set(event)
        >= {
            "event_id",
            "run_id",
            "system",
            "event_type",
            "seq",
            "ts",
            "prefix_step",
            "status",
            "payload",
        }
        for event in queue.events
    )
    assert [event["seq"] for event in queue.events] == sorted(
        event["seq"] for event in queue.events
    )
    assert queue.events[1]["payload"]["step"]["phase"] == "attempt_started"
    assert queue.events[1]["payload"]["step"]["step_id"] == 1
    assert queue.events[2]["payload"]["step"]["step_id"] == 1
    assert queue.events[2]["payload"]["failure_kind"] == "upstream_failed"
    assert queue.events[2]["payload"]["failed_tests"] == ["test_output"]

    confirmed = next(
        event for event in queue.events if event["event_type"] == "seeker.exploit_confirmed"
    )
    payload = confirmed["payload"]
    assert confirmed["status"] == "confirmed"
    assert payload["original_reward"] == payload["upstream_reward"] == 1.0
    assert payload["repaired_reward"] == 0.0
    assert payload["exploit_files"]["solve.sh"] == EXPLOIT_CANDIDATE.files["solve.sh"]
    assert payload["candidate_digest"] == compute_candidate_digest(
        EXPLOIT_CANDIDATE.ensure_submission_files()
    )
    assert payload["passing_runs"] == [payload["exploit_run"]]
    assert payload["failing_runs"] == []
    assert len(payload["repaired_runs"]) == 1
    assert payload["finding"]["finding_id"].startswith("seeker-task-a-")
    assert payload["finding"]["severity"] == "high"

    asset = next(event for event in queue.events if event["event_type"] == "asset.verified")
    assert asset["status"] == "confirmed"
    assert asset["payload"]["kind"] == "exploit_witness"
    assert asset["payload"]["dataset_id"] == "tb4-dev"
    assert asset["payload"]["item_id"] == "task-a"
    assert asset["payload"]["source_event_id"] == confirmed["event_id"]

    # The queue's shared chaining contract creates the downstream repair job.
    assert len(queue.chained_jobs) == 1
    chained = queue.chained_jobs[0]
    assert chained["event_type"] == "task_quality.repair_task"
    assert chained["payload"]["source_event_id"] == confirmed["event_id"]
    assert chained["payload"]["task_package"] == str(original)
    assert chained["payload"]["upstream_reward"] == 1.0
    assert chained["payload"]["repaired_reward"] == 0.0
    assert queue.acked == ["job-1"]
    assert queue.released == []


@pytest.mark.parametrize(
    ("results", "expected_calls", "expected_failure"),
    [
        (
            [
                {"status": "measured", "reward": 1.0, "error": None},
                {"status": "measured", "reward": 1.0, "error": None},
            ],
            2,
            "repaired_grader_also_passed",
        ),
        (
            [
                {"status": "measured", "reward": 1.0, "error": None},
                {"status": "error", "reward": None, "error": "repaired grader unavailable"},
            ],
            2,
            "repaired_grader_unverified",
        ),
        (
            [
                {"status": "measured", "reward": 1.0, "error": None},
                {"status": "error", "reward": 0.0, "error": "repaired grader crashed"},
            ],
            2,
            "repaired_grader_unverified",
        ),
        (
            [{"status": "measured", "reward": 0.5, "error": None}],
            1,
            "upstream_failed",
        ),
    ],
)
def test_unverified_or_non_differential_pass_is_never_promoted(
    tmp_path: Path,
    results: list[dict[str, Any]],
    expected_calls: int,
    expected_failure: str,
) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-gating",
        event_id="job-gating",
    )
    grader = SequenceGrader(results)
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
    )

    outcome = asyncio.run(worker.run_once())

    assert outcome is not None
    assert outcome.exploit_confirmed is False
    assert outcome.status == "refuted"
    assert len(grader.calls) == expected_calls
    feedback = next(
        event for event in queue.events if event["event_type"] == "seeker.grader_feedback"
    )
    assert feedback["status"] == "refuted"
    assert feedback["payload"]["differential_confirmed"] is False
    assert feedback["payload"]["failure_kind"] == expected_failure
    assert not any(event["event_type"] == "seeker.exploit_confirmed" for event in queue.events)
    assert not any(event["event_type"] == "asset.verified" for event in queue.events)
    assert queue.chained_jobs == []


@pytest.mark.parametrize("restart_worker", [False, True])
@pytest.mark.parametrize(
    "unavailable",
    [
        [{"status": "refused", "executed": False, "reward": None, "error": "Docker probe timeout"}],
        [
            {"status": "measured", "reward": 1.0, "error": None},
            {"status": "refused", "executed": False, "reward": None, "error": "Docker unavailable"},
        ],
    ],
)
def test_infrastructure_refusal_does_not_blacklist_candidate(
    tmp_path: Path, restart_worker: bool, unavailable: list[dict[str, Any]]
) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    grader = SequenceGrader(
        [
            *unavailable,
            {"status": "measured", "reward": 1.0, "error": None},
            {"status": "measured", "reward": 0.0, "error": None},
        ]
    )
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
    )
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-infra-recovery",
        event_id="job-infra-refused",
    )
    refused = asyncio.run(worker.run_once())
    assert refused is not None and not refused.exploit_confirmed
    assert refused.failure_memory.records == []
    assert not refused.failure_memory.blocked_entry_ids
    feedback = next(e for e in queue.events if e["event_type"] == "seeker.grader_feedback")
    assert feedback["payload"]["failure_kind"] == (
        "original_grader_unavailable" if len(unavailable) == 1 else "repaired_grader_unverified"
    )
    if restart_worker:
        worker = _worker(
            queue,
            tmp_path,
            generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
            grader=grader,
            max_attempts=1,
            name="work-restarted",
        )
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-infra-recovery",
        event_id="job-infra-recovered",
    )

    recovered = asyncio.run(worker.run_once())

    assert recovered is not None and recovered.exploit_confirmed
    assert recovered.skipped_duplicates == 0
    assert len(grader.calls) == len(unavailable) + 2
    assert len(queue.chained_jobs) == 1


def test_legacy_unmeasured_failure_does_not_mask_later_measured_rejection() -> None:
    payload = {
        "task_id": "task-a",
        "attempt_index": 1,
        "shortcut_signature": "shortcut",
        "candidate_digest": "sha256:candidate",
        "hackbook_entry_id": "HB-TS01-001",
        "failure_kind": "upstream_failed",
        "original_reward": None,
    }
    legacy = {"system": "reward_seeker", "event_type": "seeker.grader_feedback", "payload": payload}
    assert SeekerFailureMemory.from_events([legacy], task_id="task-a").records == []
    measured = {**legacy, "payload": {**payload, "original_reward": 0.0}}

    memory = SeekerFailureMemory.from_events([legacy, measured, measured], task_id="task-a")

    assert len(memory.records) == 1
    assert memory.has_candidate_digest("sha256:candidate")
    assert "HB-TS01-001" in memory.blocked_entry_ids


def test_failure_memory_hydrates_from_queue_events_across_workers(tmp_path: Path) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    grader = MarkerGrader()

    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-memory",
        event_id="job-memory-1",
    )
    first = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([BAD_CANDIDATE]),
        grader=grader,
        max_attempts=1,
        name="work-first",
    )
    first_outcome = asyncio.run(first.run_once())
    assert first_outcome is not None and first_outcome.exploit_confirmed is False

    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-memory",
        event_id="job-memory-2",
    )
    second = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([BAD_CANDIDATE, EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=2,
        name="work-second",
    )
    second_outcome = asyncio.run(second.run_once())

    assert second_outcome is not None
    assert second_outcome.exploit_confirmed is True
    assert second_outcome.skipped_duplicates == 1
    assert second_outcome.attempts_run == 1
    # One BAD grade in job 1, then only the new exploit is graded (twice) in job 2.
    assert [call["marker"] for call in grader.calls] == [
        "BAD_OUTPUT",
        "EXPLOIT_OUTPUT",
        "EXPLOIT_OUTPUT",
    ]


def test_hackbook_cue_matching_is_false_pass_only_and_blind(tmp_path: Path) -> None:
    task = _make_task(tmp_path, "task-a")
    cues = match_task_hackbook_cues(
        task,
        cue_text="zxqw surface workbook trusted answer file",
        entries=_hackbook_entries(),
        top=8,
        root=REPO_ROOT,
    )

    assert {cue.entry_id for cue in cues} >= {"HB-TS01-001", "HB-TS01-002"}
    assert "HB-TS05-001" not in {cue.entry_id for cue in cues}

    memory = SeekerFailureMemory()
    memory.record(
        FailedShortcutRecord(
            attempt_index=1,
            shortcut_signature="HB-TS01-001:test",
            candidate_digest="sha256:test",
            hackbook_entry_id="HB-TS01-001",
            taxonomy_class="weak_check",
            mechanism="surface status",
            target_check="tests/test_outputs.py::test_output",
            files_written=("/app/output.json",),
            failure_kind="upstream_failed",
            failure_reason="original grader rejected it",
            original_reward=0.0,
        )
    )
    page = render_hackbook_cues_markdown(cues, failure_memory=memory)
    assert "Mechanism:" in page
    assert "EXHAUSTED IN EARLIER ATTEMPT - SKIP" in page
    assert "Examples" not in page
    assert "preferencemodel.com" not in page


def test_candidate_digest_and_signature_reject_equivalent_retry() -> None:
    first_files = {
        "solve.sh": "#!/bin/bash\n# first wording\nprintf x > /app/output.json\n",
        "notes.json": '{"mechanism": "first"}',
    }
    second_files = {
        "solve.sh": "#!/bin/bash\n# another comment\n  printf x > /app/output.json  \n",
        "notes.json": '{"mechanism": "second"}',
    }
    first_sig = compute_shortcut_signature(
        hackbook_entry_id="HB-TS01-001",
        taxonomy_class="weak_check",
        mechanism="hardcode fixture output",
        target_check="tests/test_outputs.py::test_output",
        files_written=["/app/output.json"],
        files=first_files,
    )
    second_sig = compute_shortcut_signature(
        hackbook_entry_id="HB-TS01-001",
        taxonomy_class="weak_check",
        mechanism="hardcode fixture output attempt 2",
        target_check="tests/test_outputs.py::test_output",
        files_written=["/app/output.json"],
        files=second_files,
    )
    memory = SeekerFailureMemory()
    memory.record(
        FailedShortcutRecord(
            attempt_index=1,
            shortcut_signature=first_sig,
            candidate_digest=compute_candidate_digest(first_files),
            hackbook_entry_id="HB-TS01-001",
            taxonomy_class="weak_check",
            mechanism="hardcode fixture output",
            target_check="tests/test_outputs.py::test_output",
            files_written=("/app/output.json",),
            failure_kind="upstream_failed",
            failure_reason="failed",
            original_reward=0.0,
        )
    )

    assert compute_candidate_digest(first_files) == compute_candidate_digest(second_files)
    assert first_sig == second_sig
    assert memory.has_shortcut(
        signature=second_sig,
        candidate_digest=compute_candidate_digest(second_files),
        hackbook_entry_id="HB-TS01-001",
        mechanism="hardcode fixture output attempt 2",
        target_check="tests/test_outputs.py::test_output",
        files_written=["/app/output.json"],
    )


def test_invalid_submission_is_recorded_without_grader_call(tmp_path: Path) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-invalid",
        event_id="job-invalid",
    )
    grader = SequenceGrader([])
    invalid_solve = "#!/bin/bash\nif\n"
    invalid_stdout = json.dumps({"files": {"solve.sh": invalid_solve}})
    invalid = SeekerAttemptCandidate(
        files={"solve.sh": invalid_solve},
        mechanism="invalid shell syntax",
        hackbook_entry_id="HB-TS01-001",
        model_route="test/synthetic-model",
        llm_stdout_sha256=hashlib.sha256(invalid_stdout.encode()).hexdigest(),
        llm_solve_sha256=hashlib.sha256(invalid_solve.encode()).hexdigest(),
        served_provider="test",
        served_model="synthetic-model",
    )
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([invalid]),
        grader=grader,
        max_attempts=1,
    )

    outcome = asyncio.run(worker.run_once())

    assert outcome is not None
    assert outcome.exploit_confirmed is False
    assert outcome.attempts_run == 1
    assert grader.calls == []
    assert outcome.failure_memory.records[-1].failure_kind == "invalid_submission"


@pytest.mark.parametrize(
    ("overrides", "registry_item", "match"),
    [
        ({"split": "test"}, None, "sealed TEST"),
        ({"measurable": False}, None, "not measurable"),
        ({"repaired_task": None}, None, "missing repaired_task"),
        (
            {"split": "dev"},
            FakeCorpusItem("tb4-dev", "task-a", "test", "task-a"),
            "sealed (TEST|test)",
        ),
    ],
)
def test_split_lock_and_measurability_refuse_before_attempts(
    tmp_path: Path,
    overrides: dict[str, Any],
    registry_item: FakeCorpusItem | None,
    match: str,
) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    payload = _payload(original, repaired)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    registry = (
        FakeRegistry({("tb4-dev", "task-a"): registry_item}) if registry_item is not None else None
    )
    queue = FakeQueue(registry=registry)
    queue.enqueue_job(
        "reward_seeker.seek_task",
        payload,
        run_id="run-refused",
        event_id="job-refused",
    )
    grader = SequenceGrader([])
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
    )

    with pytest.raises((SeekerRefusalError, SplitLockError), match=match):
        asyncio.run(worker.run_once())

    assert grader.calls == []
    assert queue.acked == ["job-refused"]
    assert any(event["event_type"] == "worker.failed" for event in queue.events)
    assert any(event["event_type"] == "seeker.job_refused" for event in queue.events)
    assert not any(event["event_type"] == "seeker.attempt_started" for event in queue.events)


def test_rebreak_targets_newly_repaired_task_against_fixed_comparison(tmp_path: Path) -> None:
    old_original = _make_task(tmp_path, "old-original")
    new_repaired = _make_task(tmp_path, "new-repaired")
    fixed = _make_task(tmp_path, "fixed-comparison")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.rebreak_task",
        _payload(
            new_repaired,
            new_repaired,
            task_package=str(new_repaired),
            original_task_package=str(old_original),
            repaired_task=str(new_repaired),
            fixed_task=str(fixed),
        ),
        run_id="run-rebreak",
        event_id="job-rebreak",
    )
    grader = MarkerGrader()
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
    )

    outcome = asyncio.run(worker.run_once())

    assert outcome is not None and outcome.exploit_confirmed is True
    assert grader.calls[0]["task"] == str(new_repaired)
    assert grader.calls[1]["task"] == str(fixed)
    confirmed = outcome.confirmed_event
    assert confirmed is not None
    assert confirmed["payload"]["task_package"] == str(new_repaired)
    assert confirmed["payload"]["original_task_package"] == str(old_original)
    assert confirmed["payload"]["fixed_task"] == str(fixed)


def test_seeker_gym_pair_resolves_original_and_pm_port(tmp_path: Path) -> None:
    original = _make_task(tmp_path, "original")
    fixed = tmp_path / "fixed-port"
    fixed.mkdir()
    (fixed / "scoring_script.py").write_text("def score(path):\n    return 0.0\n", encoding="utf-8")
    gym_path = tmp_path / "gym.json"
    gym_path.write_text(
        json.dumps(
            {
                "schema": "seeker-gym/v0",
                "pairs": [
                    {
                        "pair_id": "tb4-dev:task-a",
                        "family": "tb4-dev",
                        "task": "task-a",
                        "split": "train",
                        "original_task": str(original),
                        "fixed_task": str(fixed),
                        "fixed_kind": "pm_port",
                        "runnable": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        {
            "dataset_id": "seeker-gym-v0",
            "item_id": "task-a",
            "task_id": "task-a",
            "split": "dev",
            "measurable": True,
        },
        run_id="run-gym",
        event_id="job-gym",
    )
    grader = MarkerGrader()
    worker = RewardSeekerLiveWorker(
        queue,  # type: ignore[arg-type]
        worker_id="reward-seeker-test",
        repo_root=REPO_ROOT,
        work_root=tmp_path / "work",
        gym_path=gym_path,
        hackbook_entries=_hackbook_entries(),
        attempt_generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader_fn=grader,
        max_attempts=1,
    )

    outcome = asyncio.run(worker.run_once())

    assert outcome is not None and outcome.exploit_confirmed is True
    assert grader.calls[0]["grader"] == "upstream"
    assert grader.calls[1]["grader"] == "port"
    assert grader.calls[1]["port_task"] == str(fixed)
    assert outcome.confirmed_event is not None
    assert outcome.confirmed_event["payload"]["fixed_task"] == str(fixed)
    assert outcome.confirmed_event["payload"]["repaired_task"].endswith("port-task")
    assert (
        outcome.confirmed_event["payload"]["repaired_task_materialized"]
        == outcome.confirmed_event["payload"]["repaired_task"]
    )


def test_omp_json_receipt_captures_final_text_and_served_model() -> None:
    from evallab.production.seeker_worker import _parse_omp_json_stdout

    response = {"files": {"solve.sh": "#!/bin/bash\n"}}
    raw = "\n".join(
        [
            json.dumps({"type": "agent_start"}),
            json.dumps(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "provider": "anthropic",
                            "model": "claude-opus-5-5",
                            "content": [{"type": "text", "text": json.dumps(response)}],
                        }
                    ],
                }
            ),
        ]
    )
    text, provider, model = _parse_omp_json_stdout(raw)
    assert json.loads(text) == response
    assert (provider, model) == ("anthropic", "claude-opus-5-5")


def _omp_message(
    role: str,
    *content: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": list(content), **extra}
    if provider is not None:
        message["provider"] = provider
    if model is not None:
        message["model"] = model
    return message


def _omp_stream(*messages: dict[str, Any]) -> str:
    """An inert OMP JSON event stream: the only bytes these tests treat as model output."""
    return (
        json.dumps({"type": "agent_start"})
        + "\n"
        + json.dumps({"type": "agent_end", "messages": list(messages)})
        + "\n"
    )


def test_parse_headless_candidate_response_and_model_route_attestation(tmp_path: Path) -> None:
    from evallab.production.seeker_worker import parse_headless_candidate_response

    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    cues = match_task_hackbook_cues(original, entries=_hackbook_entries(), root=REPO_ROOT)
    response_text = (
        "Working...\n```json\n"
        + json.dumps(
            {
                "files": {
                    "solve.sh": "#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' 'EXPLOIT_OUTPUT' > /app/output.json\n"
                },
                "mechanism": "Write the trusted answer file that the grader reads.",
                "taxonomy_class": "weak_check",
                "target_check": "tests/test_outputs.py::test_output",
                "files_written": ["/app/output.json"],
            }
        )
        + "\n```"
    )
    stream = _omp_stream(
        _omp_message(
            "assistant",
            {"type": "text", "text": response_text},
            provider="meta",
            model="muse-wonder-1.0-contributor",
        )
    )
    parsed = parse_headless_candidate_response(
        response_text,
        default_cue=cues[0],
        model_route="meta/muse-wonder-1.0-contributor",
        llm_stdout_text=stream,
    )
    assert parsed is not None
    assert parsed.model_route == "meta/muse-wonder-1.0-contributor"
    assert parsed.hackbook_entry_id == cues[0].entry_id
    assert parsed.llm_stdout_sha256 == hashlib.sha256(stream.encode()).hexdigest()
    assert parsed.llm_solve_sha256 == hashlib.sha256(
        parsed.files["solve.sh"].encode()
    ).hexdigest()

    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-route-attest",
        event_id="job-route-attest",
    )
    worker = _worker(
        queue,
        tmp_path,
        generator=lambda *_args: parsed,
        grader=MarkerGrader(),
        max_attempts=1,
    )
    outcome = asyncio.run(worker.run_once())
    assert outcome is not None and outcome.exploit_confirmed is True
    assert outcome.confirmed_event is not None
    assert outcome.confirmed_event["payload"]["model_route"] == "meta/muse-wonder-1.0-contributor"
    payload = outcome.confirmed_event["payload"]
    assert payload["llm_stdout_sha256"] == parsed.llm_stdout_sha256
    assert payload["llm_solve_sha256"] == parsed.llm_solve_sha256
    assert payload["served_provider"] == "meta"
    assert payload["served_model"] == "muse-wonder-1.0-contributor"
    notes = json.loads(payload["exploit_files"]["notes.json"])
    assert notes["llm_stdout_sha256"] == parsed.llm_stdout_sha256
    assert notes["llm_solve_sha256"] == parsed.llm_solve_sha256
    assert notes["served_provider"] == "meta"
    assert notes["served_model"] == "muse-wonder-1.0-contributor"
    asset = next(event for event in outcome.emitted_events if event["event_type"] == "asset.verified")
    assert asset["payload"]["llm_stdout_sha256"] == parsed.llm_stdout_sha256


def test_static_template_overwrite_refuses_promotion(tmp_path: Path) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-tampered",
        event_id="job-tampered",
    )
    candidate = copy.deepcopy(EXPLOIT_CANDIDATE)
    marker = MarkerGrader()

    def tampering_grader(task: Path, **kwargs: Any) -> dict[str, Any]:
        candidate.files["solve.sh"] = "#!/bin/bash\n# static template replacement\n"
        return marker(task, **kwargs)

    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([candidate]),
        grader=tampering_grader,
        max_attempts=1,
    )
    with pytest.raises(SeekerRefusalError, match="differs from the attested LLM output"):
        asyncio.run(worker.run_once())
    assert not any(event["event_type"] == "seeker.exploit_confirmed" for event in queue.events)
    assert not any(event["event_type"] == "asset.verified" for event in queue.events)
    assert queue.chained_jobs == []


def test_unattested_static_candidate_is_refused_before_grading(tmp_path: Path) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-static",
        event_id="job-static",
    )
    static = SeekerAttemptCandidate(
        files={"solve.sh": "#!/bin/bash\n# static template\n"},
        mechanism="static template",
    )
    grader = MarkerGrader()
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([static]),
        grader=grader,
        max_attempts=1,
    )
    with pytest.raises(SeekerRefusalError, match="missing cryptographic LLM output"):
        asyncio.run(worker.run_once())
    assert grader.calls == []
    assert queue.chained_jobs == []


@pytest.mark.parametrize("backend_name", ["modal", "auto"])
@pytest.mark.parametrize("config_style", ["object", "keywords"])
@pytest.mark.parametrize("repaired_kind", ["package", "port"])
def test_cloud_worker_forwards_policy_to_both_grades(
    tmp_path, monkeypatch, backend_name, config_style, repaired_kind
) -> None:
    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired, repaired_grader_kind=repaired_kind),
        run_id="cloud-run",
        event_id="cloud-job",
    )
    policy = ExecutionBackendConfig(
        backend=backend_name,
        modal_app_name="worker-test-seeker",
        spend_ledger=tmp_path / "shared-cloud-spend.json",
        approval="unit-test-only; no provider calls",
        max_usd=2.0,
        billing_tag="worker-test",
    )
    observed = []
    marker_grader = MarkerGrader()

    def grader(task, **kwargs):
        observed.append(kwargs)
        assert {key: kwargs[key] for key in policy.candidate_kwargs()} == policy.candidate_kwargs()
        return marker_grader(task, **kwargs)

    options = (
        {"execution_config": policy}
        if config_style == "object"
        else {
            "backend": backend_name,
            "modal_app_name": policy.modal_app_name,
            "spend_ledger": policy.spend_ledger,
            "approval": policy.approval,
            "max_usd": policy.max_usd,
            "billing_tag": policy.billing_tag,
        }
    )
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
        **options,
    )

    outcome = asyncio.run(worker.run_once())
    assert outcome is not None and outcome.exploit_confirmed is True
    assert len(observed) == 2
    assert all(call["backend"] == "modal" for call in observed)
    assert all(call["allow_local_docker"] is False for call in observed)
    assert observed[0]["files"] == observed[1]["files"]
    assert observed[1]["grader"] == ("port" if repaired_kind == "port" else "upstream")
    if repaired_kind == "port":
        assert observed[1]["port_task"] == repaired
    assert worker.execution_config.allow_local_docker is False
    assert not policy.spend_ledger.exists()


@pytest.mark.parametrize("refusal", ["environment", "explicit", "late_environment"])
def test_disabled_local_worker_returns_backend_refusal_without_blacklisting(
    tmp_path, monkeypatch, refusal
) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="disabled-run",
        event_id="disabled-job",
    )
    options = {}
    if refusal == "environment":
        monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
        options["allow_local_docker"] = True
    elif refusal == "explicit":
        options["execution_config"] = ExecutionBackendConfig(allow_local_docker=True)
        options["allow_local_docker"] = False
    grader = MarkerGrader()
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
        **options,
    )
    if refusal == "late_environment":
        monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

    outcome = asyncio.run(worker.run_once())
    assert outcome is not None and outcome.exploit_confirmed is False
    assert (outcome.original_reward, outcome.repaired_reward) == (None, None)
    assert not grader.calls
    assert not outcome.failure_memory.records
    feedback = next(
        event for event in queue.events if event["event_type"] == "seeker.grader_feedback"
    )
    assert feedback["payload"]["failure_kind"] == "execution_backend_refused"
    assert "LocalDockerDisabledError" in feedback["payload"]["failure_reason"]
    assert not queue.chained_jobs
    assert not any(event["event_type"] == "asset.verified" for event in queue.events)


def test_modal_margin_refusal_never_retries_or_falls_back_to_docker(
    tmp_path, monkeypatch
) -> None:
    from evallab.production import seeker_worker

    monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    job = queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="refused-modal",
        event_id="refused-modal-job",
    )
    calls = []

    def native_candidate(task, **kwargs):
        calls.append(kwargs)
        assert kwargs["backend"] == "modal"
        assert kwargs["solution_bundle"].joinpath("solve.sh").is_file()
        return {
            "status": "refused",
            "reward": None,
            "error": "not admitted after controller margin",
        }

    def no_fallback(*_args, **_kwargs):
        pytest.fail("A Modal refusal must not start a Docker fallback or retry")

    monkeypatch.setattr(seeker_worker, "run_candidate", native_candidate)
    monkeypatch.setattr(seeker_worker.time, "sleep", no_fallback)
    monkeypatch.setattr("evallab.qa.task.HarborBackend", no_fallback)
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=native_candidate,
        max_attempts=1,
        backend="modal",
        allow_local_docker=False,
        modal_app_name="worker-test-seeker",
        spend_ledger=tmp_path / "shared-cloud-spend.json",
        approval="unit-test-only",
        max_usd=2.0,
        billing_tag="worker-test",
    )
    resolved = worker._resolve_job_task(job)
    grade = verify_differential_replay(
        resolved,
        EXPLOIT_CANDIDATE,
        attempt_dir=tmp_path / "direct-replay",
        grader_fn=native_candidate,
        execution_config=worker.execution_config,
    )
    assert grade.original_status == "refused"
    assert grade.repaired_status == "not_run"
    assert grade.differential_confirmed is False
    assert len(calls) == 1


def test_differential_checks_protection_again_before_repaired_grade(
    tmp_path, monkeypatch
) -> None:
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    job = queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="late-protection",
        event_id="late-protection-job",
    )
    calls = []

    def grader(task, **kwargs):
        calls.append(task)
        monkeypatch.setenv("EVALLAB_DISABLE_LOCAL_DOCKER", "1")
        return {
            "status": "measured",
            "reward": 1.0,
            "output_dir": str(kwargs["output_dir"]),
        }

    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
    )
    grade = verify_differential_replay(
        worker._resolve_job_task(job),
        EXPLOIT_CANDIDATE,
        attempt_dir=tmp_path / "direct-replay",
        grader_fn=grader,
        execution_config=worker.execution_config,
    )
    assert calls == [original]
    assert grade.original_reward == 1.0
    assert grade.repaired_reward is None
    assert grade.failure_kind == "execution_backend_refused"
    assert not grade.differential_confirmed


# --------------------------------------------------------------------------- evidence custody


def _trial_tree(root: Path, name: str, marker: str) -> Path:
    trial = root / name
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    (trial / "verifier" / "reward.json").write_text(
        json.dumps({"reward": 1.0}), encoding="utf-8"
    )
    (trial / "marker.txt").write_text(marker, encoding="utf-8")
    return trial


def test_same_candidate_on_changed_repaired_package_pins_fresh_receipts(
    tmp_path: Path,
) -> None:
    """A repeated candidate digest on a changed package never reuses receipts.

    The witness dir used to be keyed on the truncated candidate digest alone,
    so the second job's confirmation silently referenced the first job's
    pinned run receipts. Custody now binds the job and package identities and
    the full source digests, so each confirmation publishes its own evidence.
    """
    original = _make_task(tmp_path, "original")
    # Both repaired packages are named "repaired" so the marker grader scores
    # the repaired side; distinct parents and bytes give distinct identities.
    repaired_a = _make_task(tmp_path / "pkg-a", "repaired")
    repaired_b = _make_task(tmp_path / "pkg-b", "repaired")
    (repaired_b / "changed.txt").write_text("changed package bytes\n", encoding="utf-8")
    queue = FakeQueue()
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=MarkerGrader(),
        max_attempts=1,
    )
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired_a),
        run_id="run-custody",
        event_id="job-custody-1",
    )
    first = asyncio.run(worker.run_once())
    assert first is not None and first.exploit_confirmed

    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired_b),
        run_id="run-custody",
        event_id="job-custody-2",
    )
    second = asyncio.run(worker.run_once())
    assert second is not None and second.exploit_confirmed

    p1 = first.confirmed_event["payload"]
    p2 = second.confirmed_event["payload"]
    assert p1["candidate_digest"] == p2["candidate_digest"]
    refs1 = (p1["exploit_run"], p1["repaired_runs"][0], p1["solution_bundle"])
    refs2 = (p2["exploit_run"], p2["repaired_runs"][0], p2["solution_bundle"])
    for ref_a, ref_b in zip(refs1, refs2, strict=True):
        assert ref_a != ref_b, f"{ref_a} silently reused the earlier pin"
        assert Path(ref_a).exists() and Path(ref_b).exists()
    witnesses = sorted(
        p.name
        for p in (tmp_path / "work" / "run-custody" / "task-a").iterdir()
        if p.name.startswith("witness-")
    )
    assert len(witnesses) == 2


def test_missing_declared_receipt_keeps_confirmed_evidence_unavailable(
    tmp_path: Path,
) -> None:
    """A declared-but-absent receipt fails the promotion; no confirmed asset."""
    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(original, repaired),
        run_id="run-missing-proof",
        event_id="job-missing-proof",
    )
    grader = SequenceGrader(
        [
            {
                "status": "measured",
                "reward": 1.0,
                "error": None,
                "trial_dir": str(tmp_path / "ghost-original"),
            },
            {
                "status": "measured",
                "reward": 0.0,
                "error": None,
                "trial_dir": str(tmp_path / "ghost-repaired"),
            },
        ]
    )
    worker = _worker(
        queue,
        tmp_path,
        generator=SequenceGenerator([EXPLOIT_CANDIDATE]),
        grader=grader,
        max_attempts=1,
    )

    with pytest.raises(SeekerEvidenceError):
        asyncio.run(worker.run_once())

    assert not any(
        event["event_type"] == "seeker.exploit_confirmed" for event in queue.events
    )
    assert not any(event["event_type"] == "asset.verified" for event in queue.events)
    assert any(event["event_type"] == "worker.failed" for event in queue.events)
    assert queue.released == ["job-missing-proof"]
    assert queue.acked == []


def test_pin_evidence_refuses_symlinks_missing_and_conflicting_bytes(
    tmp_path: Path,
) -> None:
    src = _trial_tree(tmp_path, "trial-src", "first-bytes")
    destination = tmp_path / "witness" / "exploit_run"

    pinned = RewardSeekerLiveWorker._pin_evidence(str(src), destination)
    assert pinned == str(destination)
    # An identical re-pin is an idempotent no-op.
    assert RewardSeekerLiveWorker._pin_evidence(str(src), destination) == pinned

    # Existing different bytes under the same destination are refused.
    (destination / "marker.txt").write_text("stale-bytes", encoding="utf-8")
    with pytest.raises(SeekerEvidenceError, match="conflicts"):
        RewardSeekerLiveWorker._pin_evidence(str(src), destination)
    assert (destination / "marker.txt").read_text(encoding="utf-8") == "stale-bytes"

    # A declared source that is absent raises instead of silently falling back.
    with pytest.raises(SeekerEvidenceError, match="missing"):
        RewardSeekerLiveWorker._pin_evidence(
            str(tmp_path / "gone"), tmp_path / "w2" / "exploit_run"
        )
    with pytest.raises(SeekerEvidenceError):
        RewardSeekerLiveWorker._pin_evidence(None, tmp_path / "w2" / "exploit_run")

    # Symlink destinations and sources are refused.
    link = tmp_path / "dest-link"
    link.symlink_to(src, target_is_directory=True)
    with pytest.raises(SeekerEvidenceError):
        RewardSeekerLiveWorker._pin_evidence(str(src), link)
    with pytest.raises(SeekerEvidenceError):
        RewardSeekerLiveWorker._pin_evidence(str(link), tmp_path / "w3" / "exploit_run")

    # Symlinks inside the source tree are refused too.
    linked_src = _trial_tree(tmp_path, "trial-linked", "linked")
    (linked_src / "escape").symlink_to(tmp_path / "original")
    with pytest.raises(SeekerEvidenceError, match="symlink"):
        RewardSeekerLiveWorker._pin_evidence(
            str(linked_src), tmp_path / "w4" / "exploit_run"
        )


def test_pin_evidence_interrupted_copy_leaves_no_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-copy failure stages nothing at the destination for the next retry."""
    src = _trial_tree(tmp_path, "trial-src", "full-bytes")
    destination = tmp_path / "witness" / "exploit_run"
    real_copy2 = shutil.copy2
    calls = {"count": 0}

    def interrupted(source: Any, target: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated copy interruption")
        return real_copy2(source, target)

    monkeypatch.setattr(shutil, "copy2", interrupted)
    with pytest.raises(OSError, match="simulated copy interruption"):
        RewardSeekerLiveWorker._pin_evidence(str(src), destination)

    assert not destination.exists()
    leftovers = [
        path.name
        for path in destination.parent.iterdir()
        if path.name.endswith(".tmp")
    ]
    assert leftovers == []

    monkeypatch.setattr(shutil, "copy2", real_copy2)
    pinned = RewardSeekerLiveWorker._pin_evidence(str(src), destination)
    assert pinned == str(destination)
    assert (destination / "marker.txt").read_text(encoding="utf-8") == "full-bytes"
    assert (destination / "verifier" / "reward.json").is_file()


def test_hackbook_probe_template_catalog_covers_all_entries_and_validates(tmp_path: Path) -> None:
    import subprocess

    from evallab.production.seeker_worker import (
        build_headless_seeker_prompt,
        hackbook_probe_template_catalog,
        validate_submission,
    )

    task_dir = _make_task(tmp_path, "probe-target")
    (task_dir / "task.toml").write_text(
        'version = "1.0"\nartifacts = ["/app/output/report.json"]\n',
        encoding="utf-8",
    )
    golden_dir = task_dir / "tests" / "golden"
    golden_dir.mkdir(parents=True, exist_ok=True)
    (golden_dir / "secret_golden.csv").write_text("SECRET_GOLDEN_ROW_999\n", encoding="utf-8")

    catalog = hackbook_probe_template_catalog(task_dir=task_dir, root=REPO_ROOT)
    assert len(catalog) >= 55

    signatures: set[str] = set()
    for entry_id, candidate in catalog.items():
        assert candidate.hackbook_entry_id == entry_id
        assert "/app/output/report.json" in candidate.files_written
        sub_dir = tmp_path / "probes" / entry_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        files = candidate.ensure_submission_files()
        for rel, content in files.items():
            dest = sub_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        report = validate_submission(sub_dir)
        assert report["valid"] is True, f"{entry_id} failed validation: {report}"
        syntax = subprocess.run(
            ["bash", "-n", str(sub_dir / "solve.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax.returncode == 0, f"{entry_id} syntax error: {syntax.stderr}"
        sig = compute_shortcut_signature(
            hackbook_entry_id=candidate.hackbook_entry_id,
            taxonomy_class=candidate.taxonomy_class,
            mechanism=candidate.mechanism,
            target_check=candidate.target_check,
            files_written=candidate.files_written,
            files=files,
        )
        assert sig not in signatures
        signatures.add(sig)

    repaired = _make_task(tmp_path, "repaired")
    queue = FakeQueue()
    queue.enqueue_job(
        "reward_seeker.seek_task",
        _payload(task_dir, repaired),
        run_id="run-blind",
        event_id="job-blind",
    )
    worker = RewardSeekerLiveWorker(
        queue,  # type: ignore[arg-type]
        worker_id="seeker-blind",
        repo_root=REPO_ROOT,
        work_root=tmp_path / "work",
        hackbook_entries=_hackbook_entries(),
        grader_fn=MarkerGrader(),
        max_attempts=1,
    )
    claimed = queue.claim("seeker-blind", job_types=("reward_seeker.seek_task",), lease_seconds=60)
    assert claimed is not None
    resolved = worker._resolve_job_task(claimed)
    cues = match_task_hackbook_cues(task_dir, entries=_hackbook_entries(), root=REPO_ROOT)
    memory = SeekerFailureMemory()
    ws = worker._prepare_attempt_workspace(
        resolved,
        attempt_index=1,
        cues=cues,
        memory=memory,
        last_feedback_markdown=None,
    )
    assert not (ws / "task" / "tests" / "golden").exists()
    prompt = build_headless_seeker_prompt(
        resolved,
        attempt_index=1,
        selected_cue=cues[0],
        cues=cues,
        memory=memory,
        workspace=ws,
    )
    assert "SECRET_GOLDEN_ROW_999" not in prompt
    assert "pytest_sessionfinish" not in prompt


def test_blind_workspace_and_prompt_exclude_answer_key_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.production import seeker_worker
    from evallab.production.seeker_worker import ResolvedSeekerTask, build_headless_seeker_prompt

    original = _make_task(tmp_path, "original")
    repaired = _make_task(tmp_path, "repaired")
    protected = ("golden", "golden-cache", "reference", "_verifier-setup")
    for name in protected:
        directory = original / "tests" / name
        directory.mkdir()
        (directory / "private.csv").write_text("PRIVATE_ANSWER_SENTINEL\n")
    task = ResolvedSeekerTask(
        run_id="blind-run",
        job_type="reward_seeker.seek_task",
        dataset_id="synthetic-dev",
        item_id="synthetic-dev",
        task_id="synthetic-dev",
        split="dev",
        measurable=True,
        original_task=original,
        repaired_task=repaired,
        repaired_grader_kind="package",
    )
    worker = RewardSeekerLiveWorker(
        FakeQueue(),  # type: ignore[arg-type]
        worker_id="blind-worker",
        repo_root=REPO_ROOT,
        work_root=tmp_path / "work",
        hackbook_entries=[],
    )
    memory = SeekerFailureMemory()
    workspace = worker._prepare_attempt_workspace(
        task,
        attempt_index=1,
        cues=[],
        memory=memory,
        last_feedback_markdown=None,
    )
    for name in protected:
        assert not (workspace / "task" / "tests" / name).exists()
    monkeypatch.setattr(seeker_worker, "find_tb4_test_leaks", lambda *_args, **_kwargs: [])
    prompt = build_headless_seeker_prompt(
        task,
        attempt_index=1,
        selected_cue=None,
        cues=[],
        memory=memory,
        workspace=workspace,
    )
    assert "PRIVATE_ANSWER_SENTINEL" not in prompt


LANE_ROUTE = "kimi-code/k3.1-pro-highspeed"
LANE_SOLVE = "#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' 'LANE_OUTPUT' > /app/output.json\n"


def _lane_candidate(
    tmp_path: Path,
    *messages: dict[str, Any],
    solve: str = LANE_SOLVE,
    route: str | None = LANE_ROUTE,
) -> SeekerAttemptCandidate:
    """The candidate the live worker builds from a hand-written, inert lane workspace."""
    workspace = tmp_path / "lane"
    (workspace / "submission").mkdir(parents=True)
    (workspace / "submission" / "solve.sh").write_text(solve, encoding="utf-8")
    (workspace / "transcript.jsonl").write_text(_omp_stream(*messages), encoding="utf-8")
    worker = RewardSeekerLiveWorker(
        FakeQueue(),  # type: ignore[arg-type]
        worker_id="lane-worker",
        repo_root=REPO_ROOT,
        work_root=tmp_path / "work",
        hackbook_entries=[],
    )
    return worker._candidate_from_workspace(
        workspace, lane_record={"model": route} if route else {}, default_cue=None
    )


def _lane_write(call_id: str, content: str, *, path: str = "submission/solve.sh") -> dict[str, Any]:
    return {
        "type": "toolCall",
        "id": call_id,
        "name": "write",
        "arguments": {"path": path, "content": content},
    }


def _lane_write_result(call_id: str, *, is_error: bool = False) -> dict[str, Any]:
    return _omp_message("toolResult", toolCallId=call_id, toolName="write", isError=is_error)


def _lane_answer(*content: dict[str, Any], route: str = LANE_ROUTE) -> dict[str, Any]:
    provider, _, model = route.partition("/")
    return _omp_message("assistant", *content, provider=provider, model=model)


def test_lane_candidate_digest_binds_the_assistant_write_not_the_workspace(tmp_path: Path) -> None:
    from evallab.production.seeker_worker import _require_llm_attestation

    candidate = _lane_candidate(
        tmp_path,
        _omp_message("user", {"type": "text", "text": LANE_SOLVE}),
        _lane_answer(_lane_write("w1", LANE_SOLVE)),
        _lane_write_result("w1"),
    )
    assert candidate.llm_solve_sha256 == hashlib.sha256(LANE_SOLVE.encode()).hexdigest()
    assert (candidate.served_provider, candidate.served_model) == (
        "kimi-code",
        "k3.1-pro-highspeed",
    )
    _require_llm_attestation(candidate, LANE_SOLVE)

    # The model wrote different bytes; what sits in the workspace afterwards is not
    # self-attesting, however it got there.
    rewritten = _lane_candidate(
        tmp_path / "rewritten",
        _lane_answer(_lane_write("w1", "#!/bin/bash\nexit 0\n")),
        _lane_write_result("w1"),
    )
    assert rewritten.llm_solve_sha256 == hashlib.sha256(b"#!/bin/bash\nexit 0\n").hexdigest()
    with pytest.raises(SeekerRefusalError, match="differs from the attested LLM output"):
        _require_llm_attestation(rewritten, LANE_SOLVE)


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param(
            [
                _omp_message("user", {"type": "text", "text": LANE_SOLVE}),
                _lane_answer({"type": "text", "text": "done"}),
            ],
            id="solve-text-only-in-the-prompt",
        ),
        pytest.param(
            [_lane_answer({"type": "text", "text": LANE_SOLVE})],
            id="solve-text-in-assistant-prose-without-a-write",
        ),
        pytest.param(
            [_lane_answer(_lane_write("w1", LANE_SOLVE)), _lane_write_result("w1", is_error=True)],
            id="write-reported-an-error",
        ),
        pytest.param([_lane_answer(_lane_write("w1", LANE_SOLVE))], id="write-has-no-result"),
        pytest.param(
            [
                _lane_answer(_lane_write("w1", LANE_SOLVE, path="notes.json")),
                _lane_write_result("w1"),
            ],
            id="write-to-another-file",
        ),
        pytest.param(
            [
                _lane_answer(_lane_write("w1", LANE_SOLVE, path="../submission/solve.sh")),
                _lane_write_result("w1"),
            ],
            id="write-escapes-the-workspace",
        ),
        pytest.param(
            [
                _omp_message("user", _lane_write("w1", LANE_SOLVE)),
                _lane_write_result("w1"),
            ],
            id="tool-call-not-authored-by-the-assistant",
        ),
    ],
)
def test_lane_candidate_without_an_assistant_write_is_not_attested(
    tmp_path: Path, messages: list[dict[str, Any]]
) -> None:
    from evallab.production.seeker_worker import _require_llm_attestation

    candidate = _lane_candidate(tmp_path, *messages)
    assert candidate.llm_solve_sha256 is None
    with pytest.raises(SeekerRefusalError, match="missing cryptographic LLM output attestation"):
        _require_llm_attestation(candidate, LANE_SOLVE)


@pytest.mark.parametrize(
    ("messages", "route", "match"),
    [
        pytest.param(
            [
                _lane_answer(),
                _lane_answer(
                    _lane_write("w1", LANE_SOLVE), route="openai-codex/gpt-6.1-sol"
                ),
                _lane_write_result("w1"),
            ],
            LANE_ROUTE,
            "does not match requested model_route",
            id="fallback-route-wrote-the-file",
        ),
        pytest.param(
            [
                _lane_answer(_lane_write("w1", LANE_SOLVE)),
                _lane_write_result("w1"),
                _lane_answer({"type": "text", "text": "done"}, route="openai-codex/gpt-6.1-sol"),
            ],
            LANE_ROUTE,
            "does not match requested model_route",
            id="fallback-route-answered-after-the-write",
        ),
        pytest.param(
            [
                _omp_message(
                    "assistant",
                    _lane_write("w1", LANE_SOLVE),
                    provider="openrouter",
                    model="k3.1-pro-highspeed",
                ),
                _lane_write_result("w1"),
            ],
            LANE_ROUTE,
            "does not match requested model_route",
            id="same-model-name-through-another-provider",
        ),
        pytest.param(
            [_lane_answer(_lane_write("w1", LANE_SOLVE)), _lane_write_result("w1")],
            None,
            "missing the requested model_route",
            id="no-requested-route-to-compare",
        ),
    ],
)
def test_lane_candidate_refuses_a_served_route_that_is_not_the_requested_one(
    tmp_path: Path, messages: list[dict[str, Any]], route: str | None, match: str
) -> None:
    from evallab.production.seeker_worker import _require_llm_attestation

    candidate = _lane_candidate(tmp_path, *messages, route=route)
    assert candidate.llm_solve_sha256 == hashlib.sha256(LANE_SOLVE.encode()).hexdigest()
    with pytest.raises(SeekerRefusalError, match=match):
        _require_llm_attestation(candidate, LANE_SOLVE)


def test_requested_thinking_selector_is_not_a_served_route_mismatch(tmp_path: Path) -> None:
    from evallab.production.seeker_worker import _require_llm_attestation

    candidate = _lane_candidate(
        tmp_path,
        _lane_answer(_lane_write("w1", LANE_SOLVE)),
        _lane_write_result("w1"),
        route=f"{LANE_ROUTE}:max",
    )
    _require_llm_attestation(candidate, LANE_SOLVE)


def test_headless_candidate_reads_text_and_served_route_from_the_captured_stream() -> None:
    from evallab.production.seeker_worker import (
        _require_llm_attestation,
        parse_headless_candidate_response,
    )

    response = json.dumps({"files": {"solve.sh": LANE_SOLVE}})
    stream = _omp_stream(
        _lane_answer({"type": "text", "text": response}, route="openai-codex/gpt-6.1-sol")
    )
    parsed = parse_headless_candidate_response(
        response, default_cue=None, model_route=LANE_ROUTE, llm_stdout_text=stream
    )
    assert parsed is not None
    assert (parsed.served_provider, parsed.served_model) == ("openai-codex", "gpt-6.1-sol")
    with pytest.raises(SeekerRefusalError, match="does not match requested model_route"):
        _require_llm_attestation(parsed, LANE_SOLVE)

    other = json.dumps({"files": {"solve.sh": "#!/bin/bash\nexit 0\n"}})
    with pytest.raises(SeekerRefusalError, match="not the final assistant message"):
        parse_headless_candidate_response(
            other,
            default_cue=None,
            model_route="openai-codex/gpt-6.1-sol",
            llm_stdout_text=stream,
        )


def test_muse_spark_and_deepseek_lanes_and_openrouter_prefix_attestation(tmp_path: Path) -> None:
    from evallab.production.seeker_worker import (
        DEFAULT_HEADLESS_MODELS,
        ResolvedSeekerTask,
        _require_llm_attestation,
        build_headless_seeker_prompt,
    )
    from evallab.seeker import lanes

    expected_models = {
        "meta/muse-spark-open",
        "meta/muse-spark-1.3-contributor",
        "meta/muse-wonder-1.0-contributor",
        "openrouter/deepseek/deepseek-v4.1-flash",
        "kimi-code/k3.1-pro-highspeed",
        "anthropic/claude-sonnet-5-5",
    }
    assert set(DEFAULT_HEADLESS_MODELS) == expected_models
    driver_models = {cfg["model"] for cfg in lanes.DRIVERS.values()}
    assert expected_models <= driver_models

    # OpenRouter serving a vendor-prefixed 2-segment route (meta/muse-spark-open) matches,
    # while OpenRouter serving a different model is refused.
    spark_cand = _lane_candidate(
        tmp_path / "spark-open",
        _omp_message(
            "assistant",
            _lane_write("w1", LANE_SOLVE),
            provider="openrouter",
            model="meta/muse-spark-open",
        ),
        _lane_write_result("w1"),
        route="meta/muse-spark-open",
    )
    _require_llm_attestation(spark_cand, LANE_SOLVE)

    deepseek_cand = _lane_candidate(
        tmp_path / "deepseek",
        _omp_message(
            "assistant",
            _lane_write("w1", LANE_SOLVE),
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
        ),
        _lane_write_result("w1"),
        route="openrouter/deepseek/deepseek-v4.1-flash",
    )
    _require_llm_attestation(deepseek_cand, LANE_SOLVE)

    task_dir = _make_task(tmp_path, "qa-task")
    resolved = ResolvedSeekerTask(
        run_id="qa-run",
        job_type="reward_seeker.seek_task",
        dataset_id="tb4-dev",
        item_id="qa-task",
        task_id="qa-task",
        split="dev",
        measurable=True,
        original_task=task_dir,
        repaired_task=task_dir,
        repaired_grader_kind="package",
        raw_payload={"attempt_1_mode": "surface_probe"},
    )
    ws = tmp_path / "qa-ws"
    (ws / "task").mkdir(parents=True)
    qa_prompt = build_headless_seeker_prompt(
        resolved,
        attempt_index=1,
        selected_cue=None,
        cues=(),
        memory=SeekerFailureMemory(),
        workspace=ws,
        prompt_mode="qa_review",
    )
    for phrase in ("wrong submission", "grader weakness", "defect reproducer", "verified differential"):
        assert phrase in qa_prompt
    for forbidden in ("sitecustomize", "pytest_sessionfinish", "conftest.py"):
        assert forbidden not in qa_prompt

    bare_prompt = build_headless_seeker_prompt(
        resolved,
        attempt_index=1,
        selected_cue=None,
        cues=(),
        memory=SeekerFailureMemory(),
        workspace=ws,
        prompt_mode="bare_task",
    )
    assert "Solve the benchmark task below" in bare_prompt
    assert "Matched Hackbook Cues" not in bare_prompt


def test_headless_candidate_recovers_outer_brace_variants_preserving_exact_solve_sha256() -> None:
    from evallab.production.seeker_worker import (
        _require_llm_attestation,
        parse_headless_candidate_response,
    )

    # Variant 1: extra trailing '}' at end of JSON object
    extra_brace_text = (
        json.dumps(
            {
                "files": {"solve.sh": LANE_SOLVE},
                "mechanism": "grader weakness reproducer",
                "taxonomy_class": "weak_check",
                "hackbook_entry_id": "HB-TS01-009",
                "target_check": "tests/test_outputs.py",
                "files_written": ["/app/output.json"],
            }
        )
        + "}"
    )
    stream_extra = _omp_stream(_lane_answer({"type": "text", "text": extra_brace_text}, route=LANE_ROUTE))
    cand_extra = parse_headless_candidate_response(
        extra_brace_text,
        default_cue=None,
        model_route=LANE_ROUTE,
        llm_stdout_text=stream_extra,
    )
    assert cand_extra is not None
    assert cand_extra.files["solve.sh"] == LANE_SOLVE
    assert cand_extra.llm_solve_sha256 == hashlib.sha256(LANE_SOLVE.encode()).hexdigest()
    _require_llm_attestation(cand_extra, LANE_SOLVE)

    # Variant 2: missing inner '}' before "mechanism" (metadata keys nested inside "files")
    nested_meta_text = (
        '{"files":{"solve.sh":'
        + json.dumps(LANE_SOLVE)
        + ',"mechanism":"nested metadata reproducer","taxonomy_class":"weak_check",'
        + '"hackbook_entry_id":"HB-TS01-009","target_check":"tests/test_outputs.py",'
        + '"files_written":["/app/output.json"]}'
    )
    stream_nested = _omp_stream(_lane_answer({"type": "text", "text": nested_meta_text}, route=LANE_ROUTE))
    cand_nested = parse_headless_candidate_response(
        nested_meta_text,
        default_cue=None,
        model_route=LANE_ROUTE,
        llm_stdout_text=stream_nested,
    )
    assert cand_nested is not None
    assert cand_nested.files == {"solve.sh": LANE_SOLVE}
    assert cand_nested.mechanism == "nested metadata reproducer"
    assert cand_nested.llm_solve_sha256 == hashlib.sha256(LANE_SOLVE.encode()).hexdigest()
    _require_llm_attestation(cand_nested, LANE_SOLVE)
