"""Live Reward Seeker worker: streaming attempts, failure memory, and differential gating."""

from __future__ import annotations

import asyncio
import copy
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evallab.hackbook.models import HackbookEntry
from evallab.production.seeker_worker import (
    FailedShortcutRecord,
    RewardSeekerLiveWorker,
    SeekerAttemptCandidate,
    SeekerFailureMemory,
    SeekerRefusalError,
    compute_candidate_digest,
    compute_shortcut_signature,
    match_task_hackbook_cues,
    render_hackbook_cues_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    return SeekerAttemptCandidate(
        files={
            "solve.sh": (
                f"#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' '{marker}' > /app/output.json\n"
            )
        },
        mechanism=mechanism,
        taxonomy_class="weak_check",
        hackbook_entry_id=entry_id,
        target_check="tests/test_outputs.py::test_output",
        files_written=("/app/output.json",),
        intended_work_skipped="full computed answer",
        confidence="high",
        exploit_found=True,
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
        bundle = Path(kwargs["solution_bundle"])
        solve = (bundle / "solve.sh").read_text(encoding="utf-8")
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
        ctrf = output_dir / "ctrf.json"
        ctrf.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        return {
            "status": "measured",
            "reward": reward,
            "error": None,
            "output_dir": str(output_dir),
            "trial_dir": str(output_dir / "trial"),
            "grader_provenance": (
                {"task_dir": str(output_dir / "port-task")} if grader == "port" else None
            ),
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
        result.setdefault("trial_dir", str(output_dir / "trial"))
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
    invalid = SeekerAttemptCandidate(
        files={"notes.json": "{}"},
        mechanism="missing solve script",
        hackbook_entry_id="HB-TS01-001",
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
