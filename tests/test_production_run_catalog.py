"""Consumer behavior for multi-item campaigns, worker evidence and queue leases."""

from __future__ import annotations

import json
from typing import Any

import pytest

from evallab.production.run_catalog import RunCatalog

_DATASET = "public-muse-dev"
_RUN = "eight-task-campaign"
_NOW = "2026-10-24T17:00:00+00:00"


def _event(
    seq: int,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    identity: tuple[str, str] | None = (_DATASET, "task-a"),
    run_id: str = _RUN,
    system: str = "reward_seeker",
    status: str = "provisional",
    event_id: str | None = None,
    prefix_step: int | None = None,
) -> dict[str, Any]:
    context = (
        {"dataset_id": identity[0], "item_id": identity[1], "split": "dev"}
        if identity is not None
        else {}
    )
    return {
        "event_id": event_id or f"event-{seq}",
        "seq": seq,
        "run_id": run_id,
        "system": system,
        "event_type": kind,
        "status": status,
        "ts": _NOW,
        "prefix_step": prefix_step,
        "payload": {**context, **(payload or {})},
    }


def _one(catalog: RunCatalog, queue: dict[str, Any] | None = None) -> dict[str, Any]:
    return catalog.state(queue or {})["recent"][0]


def test_eight_tasks_share_campaign_without_sharing_worker_outcomes() -> None:
    catalog = RunCatalog()
    events = []

    def emit(kind: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        event = _event(len(events) + 1, kind, payload, **kwargs)
        events.append(event)
        catalog.apply(event)
        return event

    for index in range(8):
        identity = (_DATASET, f"public-task-{index}")
        job_id = f"seeker-job-{index}"
        confirmed = index < 6
        rewards = {"original_reward": 1.0 if confirmed else 0.0, "repaired_reward": 0.0}
        emit(
            "reward_seeker.seek_task",
            {"source_kind": "live_sandbox"},
            identity=identity,
            system="queue",
            status="applied",
            event_id=job_id,
        )
        emit(
            "worker.started",
            {"source_event_id": job_id, "worker_id": "shared-worker"},
            identity=identity,
        )
        emit("seeker.attempt_started", {"attempt_index": 1}, identity=identity, prefix_step=100)
        emit(
            "seeker.grader_feedback",
            rewards,
            identity=identity,
            status="confirmed" if confirmed else "refuted",
            prefix_step=999,
        )
        if confirmed:
            finding = emit(
                "seeker.exploit_confirmed", rewards, identity=identity, status="confirmed"
            )
            emit(
                "task_quality.repair_task",
                {**rewards, "source_event_id": finding["event_id"]},
                identity=identity,
                system="queue",
                status="applied",
            )
            emit(
                "asset.verified",
                {"asset_id": f"public-witness-{index}", "source_event_id": finding["event_id"]},
                identity=identity,
                status="confirmed",
            )
        emit(
            "worker.completed",
            {**rewards, "source_event_id": job_id, "worker_id": "shared-worker", "attempts_run": 1},
            identity=identity,
            status="confirmed" if confirmed else "refuted",
        )

    state = catalog.state({"jobs": {"completed": 8, "queued": 6}, "active_leases": []})
    assert (state["total"], state["retained"], state["truncated"]) == (8, 8, False)
    rows = {row["item_id"]: row for row in state["recent"]}
    assert len({row["key"] for row in rows.values()}) == 8
    for index in range(8):
        row = rows[f"public-task-{index}"]
        assert row["run_id"] == _RUN and row["dataset_id"] == _DATASET
        assert row["split"] == "dev" and row["source_kind"] == "live_sandbox"
        assert row["system_states"]["reward_seeker"] == "completed"
        assert row["jobs_submitted"]["reward_seeker.seek_task"] == 1
        assert row["attempts"] == 1 and row["active_leases"] == 0
        assert row["has_trace"] is False
        assert row["repaired_reward"] == 0.0
        if index < 6:
            assert row["original_reward"] == 1.0 and row["outcome_status"] == "confirmed"
            assert row["system_states"]["task_quality"] == "submitted"
            assert row["jobs_submitted"]["task_quality.repair_task"] == 1
            assert row["run_state"] == "submitted"
            assert row["verified_asset_ids"] == [f"public-witness-{index}"]
        else:
            assert row["original_reward"] == 0.0 and row["outcome_status"] == "refuted"
            assert "task_quality" not in row["system_states"]
            assert row["run_state"] == "completed" and row["verified_asset_ids"] == []


def test_identity_includes_dataset_and_campaign_and_keys_stay_stable() -> None:
    catalog = RunCatalog()
    catalog.apply(_event(1, "run.started"))
    original_key = _one(catalog)["key"]
    catalog.apply(_event(2, "run.started", identity=("another-public-dataset", "task-a")))
    catalog.apply(_event(3, "run.started", run_id="another-campaign"))
    catalog.apply(_event(4, "worker.completed", status="refuted"))
    rows = catalog.state({})["recent"]
    assert len(rows) == 3 and len({row["key"] for row in rows}) == 3
    selected = next(row for row in rows if row["run_id"] == _RUN and row["dataset_id"] == _DATASET)
    assert selected["key"] == original_key
    assert (
        _RUN not in original_key and _DATASET not in original_key and "task-a" not in original_key
    )
    assert sum(row["run_state"] == "completed" for row in rows) == 1


def test_submissions_leases_and_worker_lifecycle_are_separate_authorities() -> None:
    catalog = RunCatalog()
    catalog.apply(
        _event(1, "reward_seeker.seek_task", system="queue", status="applied", event_id="job-a")
    )
    catalog.apply(
        _event(
            2,
            "reward_seeker.seek_task",
            identity=(_DATASET, "task-b"),
            system="queue",
            event_id="job-b",
        )
    )
    # Even an aggregate completion count cannot identify this task's completion.
    assert all(
        row["run_state"] == "submitted"
        for row in catalog.state({"jobs": {"completed": 50}})["recent"]
    )
    lease = {
        "event_id": "job-a",
        "job_type": "reward_seeker.seek_task",
        "worker_id": "shared",
        "lease_until": 99,
    }
    snapshot = {
        "active_leases": [
            lease,
            lease,
            {**lease, "event_id": "unrelated-job"},
            {**lease, "event_id": "job-b", "job_type": "task_quality.repair_task"},
        ]
    }
    rows = {row["item_id"]: row for row in catalog.state(snapshot)["recent"]}
    assert rows["task-a"]["active_leases"] == 1 and rows["task-a"]["run_state"] == "running"
    assert rows["task-b"]["active_leases"] == 0 and rows["task-b"]["run_state"] == "submitted"
    assert all(
        row["run_state"] == "submitted" for row in catalog.state({"active_leases": []})["recent"]
    )
    catalog.apply(_event(3, "worker.started", {"source_event_id": "job-a"}))
    assert _one(catalog)["run_state"] == "running" and _one(catalog)["active_leases"] == 0
    catalog.apply(_event(4, "worker.completed", {"source_event_id": "job-a"}, status="confirmed"))
    assert _one(catalog)["run_state"] == "completed"
    catalog.apply(
        _event(5, "task_quality.repair_task", system="queue", event_id="repair-a", status="applied")
    )
    row = _one(catalog)
    assert row["system_states"] == {"reward_seeker": "completed", "task_quality": "submitted"}
    assert row["run_state"] == "submitted" and row["outcome_status"] == "confirmed"
    tq_lease = {**lease, "event_id": "repair-a", "job_type": "task_quality.repair_task"}
    assert _one(catalog, {"active_leases": [tq_lease]})["system_states"] == {
        "reward_seeker": "completed",
        "task_quality": "running",
    }
    catalog.apply(
        _event(
            6, "task_quality.failed", {"job_id": "repair-a"}, identity=None, system="task_quality"
        )
    )
    assert _one(catalog)["system_states"] == {
        "reward_seeker": "completed",
        "task_quality": "failed",
    }
    assert _one(catalog)["run_state"] == "failed"


def test_one_completed_job_does_not_finish_another_job_or_another_item() -> None:
    catalog = RunCatalog()
    for seq, job_id in enumerate(("first", "second"), 1):
        catalog.apply(_event(seq, "reward_seeker.seek_task", event_id=job_id, system="queue"))
    catalog.apply(_event(3, "worker.started", {"source_event_id": "first"}))
    catalog.apply(_event(4, "worker.completed", {"source_event_id": "first"}, status="confirmed"))
    assert _one(catalog)["run_state"] == "submitted"
    assert _one(catalog)["jobs_submitted"] == {"reward_seeker.seek_task": 2}
    catalog.apply(_event(5, "run.started", identity=(_DATASET, "task-b")))
    catalog.apply(
        _event(
            6,
            "worker.completed",
            {"worker_id": "campaign-worker"},
            identity=None,
            status="confirmed",
        )
    )
    rows = {row["item_id"]: row for row in catalog.state({})["recent"]}
    assert rows["task-b"]["run_state"] == "running" and rows["task-a"]["run_state"] == "submitted"
    catalog.apply(_event(7, "worker.started", {"source_event_id": "second"}))
    assert _one(catalog)["run_state"] == "running"
    catalog.apply(_event(8, "worker.failed", {"source_event_id": "second"}, status="refuted"))
    assert _one(catalog)["run_state"] == "failed"


def test_applied_controls_cover_all_campaign_items_without_claiming_worker_completion() -> None:
    catalog = RunCatalog()
    catalog.apply(_event(1, "reward_seeker.seek_task", system="queue"))
    catalog.apply(
        _event(2, "reward_seeker.seek_task", identity=(_DATASET, "task-b"), system="queue")
    )
    catalog.apply(_event(3, "reward_seeker.seek_task", run_id="uncontrolled", system="queue"))
    for seq, kind in ((4, "operator.control_requested"), (5, "control.acknowledged")):
        catalog.apply(_event(seq, kind, {"action": "pause"}, identity=None, system="queue"))
    assert all(row["run_state"] == "submitted" for row in catalog.state({})["recent"])
    catalog.apply(
        _event(
            6,
            "control.applied",
            {"action": "pause"},
            identity=None,
            system="queue",
            status="applied",
        )
    )
    catalog.apply(
        _event(7, "reward_seeker.seek_task", identity=(_DATASET, "task-c"), system="queue")
    )
    rows = catalog.state({})["recent"]
    assert all(row["run_state"] == "paused" for row in rows if row["run_id"] == _RUN)
    assert all(row["system_states"] == {"reward_seeker": "submitted"} for row in rows)
    catalog.apply(_event(8, "worker.completed", status="refuted"))
    assert _one(catalog)["run_state"] == "paused"
    assert _one(catalog)["system_states"] == {"reward_seeker": "completed"}
    catalog.apply(
        _event(
            9,
            "control.applied",
            {"action": "quarantine"},
            identity=None,
            system="queue",
            status="applied",
        )
    )
    assert all(
        row["run_state"] == "quarantined"
        for row in catalog.state({})["recent"]
        if row["run_id"] == _RUN
    )
    catalog.apply(
        _event(
            10,
            "control.applied",
            {"action": "resume"},
            identity=None,
            system="queue",
            status="applied",
        )
    )
    rows = catalog.state({})["recent"]
    assert len(rows) == 4
    assert next(row for row in rows if row["run_id"] == "uncontrolled")["run_state"] == "submitted"
    assert (
        next(row for row in rows if row["run_id"] == _RUN and row["item_id"] == "task-a")[
            "run_state"
        ]
        == "completed"
    )
    assert all(row["last_event_type"] == "control.applied" for row in rows if row["run_id"] == _RUN)


def test_references_attribute_later_events_but_direct_dev_identity_wins() -> None:
    catalog = RunCatalog()
    catalog.apply(
        _event(
            1,
            "reward_seeker.seek_task",
            {"source_kind": "public_replay"},
            system="queue",
            event_id="original-job",
        )
    )
    catalog.apply(
        _event(
            2,
            "worker.started",
            {"source_event_id": "original-job"},
            identity=None,
            event_id="started",
        )
    )
    catalog.apply(
        _event(
            3,
            "asset.verified",
            {"asset": {"asset_id": "inherited-witness", "qualification_event_id": "started"}},
            identity=None,
            status="confirmed",
        )
    )
    row = _one(catalog)
    assert row["item_id"] == "task-a" and row["source_kind"] == "public_replay"
    assert row["verified_asset_ids"] == ["inherited-witness"] and row["run_state"] == "running"
    catalog.apply(
        _event(
            4,
            "worker.completed",
            {"source_event_id": "original-job"},
            identity=(_DATASET, "task-b"),
            status="refuted",
            event_id="different-item",
        )
    )
    rows = {row["item_id"]: row for row in catalog.state({})["recent"]}
    assert rows["task-a"]["run_state"] == "running"
    assert rows["task-b"]["run_state"] == "completed" and rows["task-b"]["source_kind"] is None
    before = catalog.state({})
    for seq, payload in enumerate(
        [
            {
                "dataset_id": _DATASET,
                "item_id": "sealed-fixture",
                "split": "test",
                "source_event_id": "original-job",
            },
            {
                "dataset_id": _DATASET,
                "item_id": "unknown-fixture",
                "source_event_id": "original-job",
            },
            {"item_id": "partial-fixture", "split": "dev", "source_event_id": "original-job"},
            {"source_event_ids": ["original-job", "different-item"]},
            {"source_event_id": "missing-event"},
        ],
        5,
    ):
        catalog.apply(_event(seq, "worker.completed", payload, identity=None, status="confirmed"))
    assert catalog.state({}) == before


@pytest.mark.parametrize("split", [None, "test", "heldout", "DEV", "unknown"])
def test_only_normalized_effective_dev_identity_enters_catalog(split: Any) -> None:
    catalog = RunCatalog()
    catalog.apply(_event(1, "run.started", {"split": split}))
    catalog.apply(
        _event(
            2,
            "asset.verified",
            {
                "asset": {
                    "dataset_id": _DATASET,
                    "item_id": "nested-fixture",
                    "split": split,
                    "asset_id": "not-public",
                }
            },
            identity=None,
            status="confirmed",
        )
    )
    assert catalog.state({}) == {"total": 0, "retained": 0, "truncated": False, "recent": []}


def test_nested_asset_identity_cannot_borrow_another_identitys_dev_label() -> None:
    catalog = RunCatalog()
    catalog.apply(
        _event(
            1,
            "asset.verified",
            {
                "asset": {
                    "dataset_id": _DATASET,
                    "item_id": "unresolved-nested",
                    "asset_id": "unresolved",
                }
            },
            status="confirmed",
        )
    )
    catalog.apply(
        _event(
            2,
            "asset.verified",
            {
                "split": "test",
                "asset": {
                    "dataset_id": _DATASET,
                    "item_id": "public-nested",
                    "split": "dev",
                    "asset_id": "not-authorized",
                },
            },
            status="confirmed",
        )
    )
    assert catalog.state({})["total"] == 0
    catalog.apply(
        _event(
            3,
            "asset.verified",
            {
                "asset": {
                    "dataset_id": _DATASET,
                    "item_id": "public-nested",
                    "split": "dev",
                    "asset_id": "public-witness",
                }
            },
            identity=None,
            status="confirmed",
        )
    )
    assert _one(catalog)["item_id"] == "public-nested"
    assert _one(catalog)["verified_asset_ids"] == ["public-witness"]


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        ("seeker.attempt_started", {"attempt_index": 100}, False),
        ("seeker.attempt_started", {"step": {"step_id": 1, "phase": "attempt_started"}}, False),
        ("seeker.grader_feedback", {"step": {"step_id": 1, "phase": "grader_feedback"}}, False),
        (
            "trace.step",
            {"step": {"step_id": 1, "source": "agent", "message": "real rollout"}},
            True,
        ),
        ("trace.step", {"step_id": "head-1", "source": "agent"}, True),
        ("trace.updated", {"steps": [{"step_id": 1}]}, True),
        ("trace.updated", {"partial_trace": {"steps": [{"step_id": 0}]}}, True),
        ("trace.updated", {"partial_trace": [{"id": "first"}]}, True),
        ("trace.step", {"step": {"step_id": True}}, False),
        ("trace.updated", {"steps": [None, "not-a-step", {}]}, False),
    ],
)
def test_only_actual_trace_steps_enable_timeline_affordance(
    kind: str, payload: dict[str, Any], expected: bool
) -> None:
    catalog = RunCatalog()
    catalog.apply(_event(1, kind, payload, prefix_step=999))
    assert _one(catalog)["has_trace"] is expected
    if kind == "seeker.attempt_started":
        assert _one(catalog)["system_states"] == {} and _one(catalog)["run_state"] == "observed"
    catalog.apply(_event(2, "seeker.observation"))
    assert _one(catalog)["has_trace"] is expected


def test_public_replay_provenance_survives_unlabeled_trace_and_completion_events() -> None:
    catalog = RunCatalog()
    catalog.apply(_event(1, "run.started", {"source_kind": "public_replay"}, system="misalignment"))
    catalog.apply(_event(2, "trace.step", {"step": {"step_id": 1}}, system="misalignment"))
    catalog.apply(_event(3, "run.completed", system="misalignment", status="confirmed"))
    row = _one(catalog)
    assert row["source_kind"] == "public_replay" and row["has_trace"] is True
    assert row["system_states"] == {"misalignment": "completed"}


def test_rewards_are_finite_measurements_and_verified_assets_are_unique() -> None:
    catalog = RunCatalog()
    catalog.apply(
        _event(
            1,
            "seeker.grader_feedback",
            {"original_reward": 1, "repaired_reward": 0},
            status="confirmed",
        )
    )
    seq = 1
    for value in (None, True, "0", float("nan"), float("inf"), -float("inf"), 10**1000):
        seq += 1
        catalog.apply(
            _event(seq, "seeker.observation", {"original_reward": value, "repaired_reward": value})
        )
    assert _one(catalog)["original_reward"] == 1.0 and _one(catalog)["repaired_reward"] == 0.0
    for kind, status, asset_id in (
        ("asset.verified", "provisional", "unverified"),
        ("asset.verified", "refuted", "refuted"),
        ("asset.verified", "confirmed", "witness"),
        ("asset.verified", "confirmed", "witness"),
        ("verified_asset.created", "applied", "repair"),
    ):
        seq += 1
        catalog.apply(_event(seq, kind, {"asset": {"asset_id": asset_id}}, status=status))
    row = _one(catalog)
    assert (
        row["verified_asset_ids"] == ["witness", "repair"] and row["outcome_status"] == "confirmed"
    )
    json.dumps(catalog.state({}), allow_nan=False)
    catalog.apply(
        _event(
            seq + 1,
            "seeker.grader_feedback",
            {"original_reward": 0, "repaired_reward": 0},
            status="refuted",
        )
    )
    assert _one(catalog)["original_reward"] == 0.0 and _one(catalog)["outcome_status"] == "refuted"


def test_retention_bounds_rows_assets_and_event_bodies_without_recounting_items() -> None:
    catalog = RunCatalog(max_items=2)
    catalog.apply(_event(1, "run.started"))
    first_key = _one(catalog)["key"]
    for seq, item_id in enumerate(("task-b", "task-c", "task-d"), 2):
        catalog.apply(
            _event(
                seq,
                "trace.step",
                {"step": {"step_id": 1, "message": "private-body" * 10000}},
                identity=(_DATASET, item_id),
            )
        )
    state = catalog.state({})
    assert (state["total"], state["retained"], state["truncated"]) == (4, 2, True)
    assert [row["item_id"] for row in state["recent"]] == ["task-d", "task-c"]
    assert "private-body" not in json.dumps(state)
    catalog.apply(_event(5, "worker.completed", status="refuted"))
    assert _one(catalog)["key"] == first_key and catalog.state({})["total"] == 4
    for seq in range(6, 126):
        catalog.apply(
            _event(seq, "asset.verified", {"asset_id": f"public-asset-{seq}"}, status="confirmed")
        )
    state = catalog.state({})
    assert state["retained"] == 2 and len(state["recent"][0]["verified_asset_ids"]) <= 100
    assert len(json.dumps(state)) < 10000


def test_replay_stale_order_and_duplicate_attempt_ids_do_not_change_counts() -> None:
    catalog = RunCatalog()
    events = [
        _event(1, "reward_seeker.seek_task", event_id="job", system="queue"),
        _event(2, "seeker.attempt_started", event_id="attempt-one", prefix_step=100),
        _event(3, "seeker.attempt_started", event_id="attempt-two", prefix_step=1),
        _event(4, "seeker.attempt_started", event_id="attempt-one", prefix_step=1000),
        _event(
            5, "worker.completed", {"source_event_id": "job", "attempts_run": 999}, status="refuted"
        ),
        _event(6, "asset.verified", {"asset_id": "public-witness"}, status="confirmed"),
    ]
    for event in events:
        catalog.apply(event)
    before = catalog.state({})
    for event in [*events, _event(0, "run.started", identity=(_DATASET, "stale-task"))]:
        catalog.apply(event)
    assert catalog.state({}) == before
    row = _one(catalog)
    assert row["attempts"] == 2 and row["jobs_submitted"] == {"reward_seeker.seek_task": 1}
    assert row["system_states"] == {"reward_seeker": "completed"} and row["has_trace"] is False
    replay = RunCatalog()
    for event in events:
        replay.apply(event)
    assert replay.state({}) == before
    # Snapshots are detached consumer values, never mutable projection internals.
    row["system_states"]["reward_seeker"] = "running"
    row["verified_asset_ids"].append("invented")
    row["jobs_submitted"].clear()
    assert catalog.state({}) == before


def test_old_submitted_job_still_matches_current_lease_after_many_nontrace_events() -> None:
    catalog = RunCatalog(max_items=1)
    catalog.apply(_event(1, "reward_seeker.seek_task", event_id="old-job", system="queue"))
    for seq in range(2, 302):
        catalog.apply(_event(seq, "seeker.attempt_started", prefix_step=1))
    lease = {
        "event_id": "old-job",
        "job_type": "reward_seeker.seek_task",
        "worker_id": "worker",
        "lease_until": 99,
    }
    row = _one(catalog, {"active_leases": [lease]})
    assert row["active_leases"] == 1 and row["run_state"] == "running"
    assert row["attempts"] == 300 and row["has_trace"] is False
    catalog.apply(
        _event(302, "worker.completed", {"job_id": "old-job"}, identity=None, status="refuted")
    )
    assert _one(catalog)["run_state"] == "completed"


def test_delayed_asset_can_resolve_an_old_non_job_qualification_event() -> None:
    catalog = RunCatalog(max_items=1)
    catalog.apply(
        _event(
            1,
            "task_quality.repair_qualified",
            {"source_kind": "public_replay"},
            system="task_quality",
            event_id="qualification",
            status="confirmed",
        )
    )
    for seq in range(2, 302):
        catalog.apply(_event(seq, "seeker.attempt_started", prefix_step=1))
    catalog.apply(
        _event(
            302,
            "asset.verified",
            {"qualification_event_id": "qualification", "asset": {"asset_id": "delayed-repair"}},
            identity=None,
            system="task_quality",
            status="confirmed",
        )
    )
    assert _one(catalog)["verified_asset_ids"] == ["delayed-repair"]
    assert _one(catalog)["source_kind"] == "public_replay"


@pytest.mark.parametrize("max_items", [0, -1, True, 1.5])
def test_retention_limit_must_be_a_positive_integer(max_items: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        RunCatalog(max_items=max_items)
