"""Unit tests for evallab.spend_day (HAR-122, HAR-131).

Covers:
- UTC-day boundary split (midnight crossing split, duration preservation)
- Deduplication of identical ledger records across checkouts/files
- Billed vs estimate vs ledger basis flagging
- No double count of self-hosted proxy calls (0/0 pricing returns None)
- Card attribution: job prefixes, ledger paths, and strict unattributed bucket
- Cap arithmetic: under, equal, over, headroom calculation
- Policy day caps and launch-time overrides are covered in test_daily_ceiling_override.py
- Complete billed-session membership, weighted conservation and source binding
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from evallab.spend_day import (
    day_to_window,
    session_spend_for_job,
    sibling_worktree_roots,
    trial_daytona_resources,
    window_overlap_seconds,
)

# ---------------------------------------------------------------------------
# 1. UTC-day boundary split
# ---------------------------------------------------------------------------


def test_utc_day_boundary_split_exact_slices() -> None:
    target_day = date(2026, 9, 30)
    target_window = day_to_window(target_day)

    # 1. Trial entirely within 2026-09-30 (10:00 to 11:30 = 90 min = 5400s)
    t_start = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    t_end = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)
    assert window_overlap_seconds(t_start, t_end, *target_window) == pytest.approx(5400.0)

    # 2. Trial crossing midnight: starts 2026-09-29 23:30, ends 2026-09-30 01:00 (90 min total)
    c_start = datetime(2026, 9, 29, 23, 30, tzinfo=UTC)
    c_end = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
    # 2026-09-29 gets 30 min (1800s); 2026-09-30 gets 60 min (3600s)
    overlap_prev = window_overlap_seconds(c_start, c_end, *day_to_window(date(2026, 9, 29)))
    overlap_curr = window_overlap_seconds(c_start, c_end, *target_window)
    assert overlap_prev == pytest.approx(1800.0)
    assert overlap_curr == pytest.approx(3600.0)
    assert overlap_prev + overlap_curr == pytest.approx((c_end - c_start).total_seconds())

    # 3. Trial crossing next midnight: starts 2026-09-30 23:00, ends 2026-10-01 02:00
    n_start = datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    n_end = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    assert window_overlap_seconds(n_start, n_end, *target_window) == pytest.approx(3600.0)
    assert window_overlap_seconds(
        n_start, n_end, *day_to_window(date(2026, 10, 1))
    ) == pytest.approx(7200.0)

    # 4. Trial completely outside the target day
    o_start = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    o_end = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
    assert window_overlap_seconds(o_start, o_end, *target_window) == 0.0


# ---------------------------------------------------------------------------
# 2. Deduplication of duplicate ledgers across checkouts
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# 3. Billed vs estimate vs ledger basis flagging
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 4. No double count of self-hosted proxy calls
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 5. Card attribution: prefixes, paths, and strict unattributed bucket
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 6. Cap arithmetic: under, equal, over
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 7. Daytona resource resolution and fallback
# ---------------------------------------------------------------------------


def test_trial_daytona_resources_resolution() -> None:
    # 1. Environment overrides take first precedence
    env_override = {"environment": {"override_cpus": 4, "override_memory_mb": 16384}}
    task_env = {"cpus": 2, "memory_mb": 8192}
    (cpus, mem, _), basis = trial_daytona_resources(
        task_family="mimo-v2.6-rl",
        environment_configs=[env_override],
        task_toml_env=task_env,
    )
    assert (cpus, mem) == (4, 16384)
    assert "overrides" in basis

    # 2. task.toml takes precedence if no overrides
    (cpus2, mem2, _), basis2 = trial_daytona_resources(
        task_family="mimo-v2.6-rl",
        environment_configs=[],
        task_toml_env=task_env,
    )
    assert (cpus2, mem2) == (2, 8192)
    assert "task.toml" in basis2

    # 3. Family fallback when task.toml is missing
    (cpus3, mem3, _), basis3 = trial_daytona_resources(
        task_family="mimo-v2.6-rl",
        environment_configs=[],
        task_toml_env=None,
    )
    assert (cpus3, mem3) == (2, 8192)
    assert "family-fallback:mimo-v2.6-rl" in basis3

    # 4. Unknown family with no task.toml and no overrides cannot be rated
    (cpus4, mem4, _), basis4 = trial_daytona_resources(
        task_family="unknown-task-family",
        environment_configs=[],
        task_toml_env=None,
    )
    assert (cpus4, mem4) == (None, None)
    assert basis4 == "missing"


# ---------------------------------------------------------------------------
# 9. HAR-122 regression tests: sibling worktree discovery & UTC finish times
# ---------------------------------------------------------------------------


def test_sibling_worktree_roots_discovers_from_linked_worktree(tmp_path: Path) -> None:
    """Linked worktrees must discover siblings through git common-dir (HAR-122)."""
    import subprocess

    primary = tmp_path / "repo-primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(primary)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(primary),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    linked = tmp_path / "repo-worktree-a"
    sibling = tmp_path / "repo-worktree-b"
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", "-b", "branch-a", str(linked)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", "-b", "branch-b", str(sibling)],
        check=True,
        capture_output=True,
    )

    # Discovery from the LINKED worktree must find the sibling
    roots = sibling_worktree_roots(linked)
    assert sibling.resolve() in [r.resolve() for r in roots]
    assert primary.resolve() in [r.resolve() for r in roots]
    assert linked.resolve() not in [r.resolve() for r in roots]




def _session_spend_fixture(
    root: Path, *, name: str = "a"
) -> tuple[list[Path], Path, dict[str, Any]]:
    commit = name * 40
    members, jobs = [], []
    for index, (seconds, daytona) in enumerate(
        zip((60.0, 180.0, 360.0), (0.05, 0.10, 0.15), strict=True)
    ):
        job = root / name / f"published-prefix-{index}"
        job.mkdir(parents=True)
        (job / "result.json").write_text(json.dumps({"id": f"job-{name}-{index}"}))
        metadata = {
            "experiment": {"spec_id": f"spec-{name}-{index}"},
            "repository": {"commit": commit},
        }
        raw_metadata = (json.dumps(metadata, sort_keys=True) + "\n").encode()
        (job / "lab-metadata.json").write_bytes(raw_metadata)
        jobs.append(job)
        members.append(
            {
                "job_id": f"job-{name}-{index}",
                "job_name": f"original-{name}-{index}",
                "spec_id": f"spec-{name}-{index}",
                "repository_commit": commit,
                "lab_metadata_sha256": hashlib.sha256(raw_metadata).hexdigest(),
                "trial_wall_seconds": seconds,
                "daytona_estimate_usd": daytona,
            }
        )
    session_id, app = f"ap-{name}", f"eval-app-{name}"
    rows = [
        {
            "object_id": session_id,
            "description": app,
            "environment": "main",
            "interval_start": "2026-10-01T00:00:00Z",
            "resource": resource,
            "cost_usd": cost,
            "resolution": "d",
            "reported_at": "2026-10-01T02:23:10Z",
        }
        for resource, cost in (("GPU", 1.1), ("CPU", 0.1))
    ]
    payload = {
        "schema": "evallab.session_spend/v1",
        "sessions": [
            {
                "session_id": session_id,
                "billing_rows": rows,
                "deployment": {"commit": commit[:7], "time_deployed": "2026-10-01T00:01:27Z"},
                "teardown": {
                    "app": app,
                    "recorded_at": "2026-10-01T01:45:38Z",
                    "completed_spec_ids": [m["spec_id"] for m in members],
                },
                "members": members,
            }
        ],
    }
    receipt = root / name / "receipt.json"
    receipt.write_text(json.dumps(payload))
    return jobs, receipt, payload


def test_session_spend_unequal_weights_conserve_pool_and_existing_daytona(tmp_path: Path) -> None:
    jobs, receipt, _ = _session_spend_fixture(tmp_path)
    allocations = [session_spend_for_job(job, receipt) for job in jobs]
    shares = [a["modal_allocated_usd"] for a in allocations]
    assert shares == pytest.approx([0.12, 0.36, 0.72], rel=1e-14, abs=1e-15)
    assert math.fsum(shares) == pytest.approx(1.2, rel=1e-14, abs=1e-15)
    assert math.fsum(a["daytona_estimate_usd"] for a in allocations) == pytest.approx(0.3)
    assert math.fsum(a["total_usd"] for a in allocations) == pytest.approx(1.5)
    assert {a["member_count"] for a in allocations} == {3}
    assert allocations[1]["trial_wall_seconds"] == 180.0
    assert allocations[1]["session_trial_wall_seconds"] == 600.0


def test_session_spend_unknown_daytona_is_not_zero_or_legacy_fallback(tmp_path: Path) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    members = payload["sessions"][0]["members"]
    members[0]["daytona_estimate_usd"] = 0.0
    members[1]["daytona_estimate_usd"] = None
    receipt.write_text(json.dumps(payload))
    unknown = session_spend_for_job(jobs[1], receipt)
    assert unknown["modal_allocated_usd"] == pytest.approx(0.36)
    assert unknown["daytona_estimate_usd"] is None
    assert unknown["total_usd"] is None
    assert unknown["reason"] is not None
    known_zero = session_spend_for_job(jobs[0], receipt)
    assert known_zero["total_usd"] == pytest.approx(0.12)
    assert known_zero["reason"] is None


@pytest.mark.parametrize("binding", ["job_id", "spec_id", "repository_commit", "metadata_bytes"])
def test_session_spend_rejects_stale_or_wrong_target_binding(tmp_path: Path, binding: str) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    job = jobs[0]
    if binding == "job_id":
        (job / "result.json").write_text(json.dumps({"id": "outsider"}))
    else:
        metadata = json.loads((job / "lab-metadata.json").read_text())
        if binding == "spec_id":
            metadata["experiment"]["spec_id"] = "another-spec"
        elif binding == "repository_commit":
            metadata["repository"]["commit"] = "c" * 40
        else:
            metadata["changed"] = True
        raw = json.dumps(metadata).encode()
        (job / "lab-metadata.json").write_bytes(raw)
        if binding != "metadata_bytes":
            payload["sessions"][0]["members"][0]["lab_metadata_sha256"] = hashlib.sha256(
                raw
            ).hexdigest()
            receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(job, receipt)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_member",
        "undeclared_member",
        "duplicate_job",
        "duplicate_spec",
        "duplicate_teardown",
        "empty_members",
    ],
)
def test_session_spend_rejects_incomplete_or_duplicate_membership(
    tmp_path: Path, defect: str
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    session = payload["sessions"][0]
    members = session["members"]
    if defect == "missing_member":
        members.pop()
    elif defect == "undeclared_member":
        session["teardown"]["completed_spec_ids"].pop()
    elif defect == "duplicate_job":
        members[1]["job_id"] = members[0]["job_id"]
    elif defect == "duplicate_spec":
        members[1]["spec_id"] = members[0]["spec_id"]
    elif defect == "duplicate_teardown":
        session["teardown"]["completed_spec_ids"].append(members[0]["spec_id"])
    else:
        members.clear()
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize("field", ["cost_usd", "trial_wall_seconds", "daytona_estimate_usd"])
@pytest.mark.parametrize("value", [True, "1.2", -1.0, float("nan"), float("inf")])
def test_session_spend_rejects_invalid_prices_or_weights(
    tmp_path: Path, field: str, value: Any
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    session = payload["sessions"][0]
    entries = session["billing_rows"] if field == "cost_usd" else session["members"]
    entries[-1][field] = value
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize(
    "defect",
    ["zero_weights", "overflow_weights", "overflow_pool", "missing_daytona", "missing_hash"],
)
def test_session_spend_rejects_unknown_or_unusable_pool_inputs(tmp_path: Path, defect: str) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    session = payload["sessions"][0]
    if defect in {"zero_weights", "overflow_weights"}:
        for member in session["members"]:
            member["trial_wall_seconds"] = 0.0 if defect == "zero_weights" else 1e308
    elif defect == "overflow_pool":
        for row in session["billing_rows"]:
            row["cost_usd"] = 1e308
    elif defect == "missing_daytona":
        del session["members"][-1]["daytona_estimate_usd"]
    else:
        session["members"][-1]["lab_metadata_sha256"] = "not-a-hash"
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize("interval", ["2026-10-01T00:00:00Z", "2026-10-01T00:00:00+00:00"])
def test_session_spend_rejects_duplicate_billing_identity(tmp_path: Path, interval: str) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    rows = payload["sessions"][0]["billing_rows"]
    rows.append(dict(rows[0], interval_start=interval, resolution="h", cost_usd=2.0))
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize(
    "field,value",
    [("object_id", "ap-unrelated"), ("description", "other-app"), ("resolution", None)],
)
def test_session_spend_rejects_wrong_or_missing_billing_binding(
    tmp_path: Path, field: str, value: Any
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    payload["sessions"][0]["billing_rows"][0][field] = value
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


def test_session_spend_disjoint_pools_and_full_receipt_rejection(tmp_path: Path) -> None:
    jobs_a, receipt, payload_a = _session_spend_fixture(tmp_path, name="a")
    jobs_b, _, payload_b = _session_spend_fixture(tmp_path, name="b")
    session_b = payload_b["sessions"][0]
    session_b["teardown"]["app"] = payload_a["sessions"][0]["teardown"]["app"]
    for row in session_b["billing_rows"]:
        row["description"] = session_b["teardown"]["app"]
    session_b["billing_rows"][0]["cost_usd"] = 2.3
    payload_a["sessions"].append(session_b)
    receipt.write_text(json.dumps(payload_a))
    allocations_a = [session_spend_for_job(job, receipt) for job in jobs_a]
    allocations_b = [session_spend_for_job(job, receipt) for job in jobs_b]
    assert math.fsum(a["modal_allocated_usd"] for a in allocations_a) == pytest.approx(1.2)
    assert math.fsum(a["modal_allocated_usd"] for a in allocations_b) == pytest.approx(2.4)
    assert allocations_a[0]["modal_allocated_usd"] == pytest.approx(0.12)
    assert allocations_b[0]["modal_allocated_usd"] == pytest.approx(0.24)
    session_b["members"].pop()
    receipt.write_text(json.dumps(payload_a))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs_a[0], receipt)


@pytest.mark.parametrize("defect", ["shared_job", "duplicate_session"])
def test_session_spend_rejects_shared_job_or_session_identity(tmp_path: Path, defect: str) -> None:
    jobs, receipt, payload_a = _session_spend_fixture(tmp_path, name="a")
    _, _, payload_b = _session_spend_fixture(tmp_path, name="b")
    session_b = payload_b["sessions"][0]
    if defect == "shared_job":
        session_b["members"][0]["job_id"] = payload_a["sessions"][0]["members"][0]["job_id"]
    else:
        session_b["session_id"] = payload_a["sessions"][0]["session_id"]
        for row in session_b["billing_rows"]:
            row["object_id"] = session_b["session_id"]
    payload_a["sessions"].append(session_b)
    receipt.write_text(json.dumps(payload_a))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize("defect", ["wrong_revision", "too_short", "ambiguous_prefix"])
def test_session_spend_rejects_wrong_or_ambiguous_deployment_revision(
    tmp_path: Path, defect: str
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    session = payload["sessions"][0]
    if defect == "wrong_revision":
        session["deployment"]["commit"] = "b" * 40
    elif defect == "too_short":
        session["deployment"]["commit"] = "a" * 6
    else:
        session["members"][-1]["repository_commit"] = "a" * 7 + "b" * 33
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


def test_session_spend_reloads_changed_receipt_without_touching_job_bytes(tmp_path: Path) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    raw_result = (jobs[0] / "result.json").read_bytes()
    raw_metadata = (jobs[0] / "lab-metadata.json").read_bytes()
    first = session_spend_for_job(jobs[0], receipt)
    assert first["source_receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    payload["sessions"][0]["billing_rows"][0]["cost_usd"] = 2.3
    receipt.write_text(json.dumps(payload))
    second = session_spend_for_job(jobs[0], receipt)
    assert second["modal_allocated_usd"] == pytest.approx(first["modal_allocated_usd"] * 2)
    assert second["source_receipt_sha256"] != first["source_receipt_sha256"]
    assert second["source_receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert (jobs[0] / "result.json").read_bytes() == raw_result
    assert (jobs[0] / "lab-metadata.json").read_bytes() == raw_metadata


def test_session_spend_spec_fallback_and_conflicting_saved_spec(tmp_path: Path) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    metadata = json.loads((jobs[0] / "lab-metadata.json").read_text())
    del metadata["experiment"]
    raw = json.dumps(metadata).encode()
    (jobs[0] / "lab-metadata.json").write_bytes(raw)
    spec_file = jobs[0] / "experiment-spec.json"
    spec_file.write_text(json.dumps({"spec_id": "spec-a-0"}))
    payload["sessions"][0]["members"][0]["lab_metadata_sha256"] = hashlib.sha256(raw).hexdigest()
    receipt.write_text(json.dumps(payload))
    assert session_spend_for_job(jobs[0], receipt)["modal_allocated_usd"] == pytest.approx(0.12)
    metadata["experiment"] = {"spec_id": "spec-a-0"}
    raw = json.dumps(metadata).encode()
    (jobs[0] / "lab-metadata.json").write_bytes(raw)
    payload["sessions"][0]["members"][0]["lab_metadata_sha256"] = hashlib.sha256(raw).hexdigest()
    receipt.write_text(json.dumps(payload))
    spec_file.write_text(json.dumps({"spec_id": "conflicting-spec"}))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


def test_session_spend_latest_reported_timestamp_retains_source_offset(tmp_path: Path) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    rows = payload["sessions"][0]["billing_rows"]
    rows[1]["reported_at"] = "2026-10-01T03:00:00+01:00"
    receipt.write_text(json.dumps(payload))
    assert session_spend_for_job(jobs[0], receipt)["billing_reported_at"] == rows[0]["reported_at"]
    rows[1]["reported_at"] = "2026-10-01T03:30:00+01:00"
    receipt.write_text(json.dumps(payload))
    assert session_spend_for_job(jobs[0], receipt)["billing_reported_at"] == rows[1]["reported_at"]


def test_session_spend_large_finite_prices_do_not_overflow_intermediate_product(
    tmp_path: Path,
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    rows = payload["sessions"][0]["billing_rows"]
    rows[0]["cost_usd"], rows[1]["cost_usd"] = 1e308, 0.0
    receipt.write_text(json.dumps(payload))
    allocations = [session_spend_for_job(job, receipt) for job in jobs]
    assert [a["modal_allocated_usd"] for a in allocations] == pytest.approx([1e307, 3e307, 6e307])
    assert math.fsum(a["modal_allocated_usd"] for a in allocations) == pytest.approx(1e308)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("billing_rows", "interval_start", "not-a-date"),
        ("billing_rows", "reported_at", "not-a-date"),
        ("billing_rows", "reported_at", "2026-10-01T03:00:00"),
        ("deployment", "time_deployed", "2026-10-02T00:00:00Z"),
        ("teardown", "recorded_at", "2026-10-01T01:45:38"),
    ],
)
def test_session_spend_rejects_malformed_or_ambiguous_timestamps(
    tmp_path: Path, section: str, key: str, value: str
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    entry = payload["sessions"][0][section]
    if section == "billing_rows":
        entry = entry[0]
    entry[key] = value
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


def test_session_spend_rejects_ambiguous_json_fields(tmp_path: Path) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    receipt.write_text(
        '{"schema": "evallab.session_spend/v1", "sessions": [], "sessions": '
        + json.dumps(payload["sessions"])
        + "}"
    )
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize(
    "hour_start",
    [
        "2026-10-01T01:00:00Z",
        "2026-10-02T00:00:00+02:00",
    ],
)
@pytest.mark.parametrize("hour_first", [False, True])
def test_session_spend_rejects_daily_hourly_billing_overlap(
    tmp_path: Path, hour_start: str, hour_first: bool
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    rows = payload["sessions"][0]["billing_rows"]
    hourly = dict(rows[0], resolution="h", interval_start=hour_start, cost_usd=0.2)
    rows.insert(0 if hour_first else len(rows), hourly)
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)


@pytest.mark.parametrize(
    "first_resolution,first_start,second_resolution,second_start",
    [
        ("h", "2026-10-01T01:00:00Z", "h", "2026-10-01T02:00:00Z"),
        ("d", "2026-10-01T00:00:00Z", "h", "2026-10-02T00:00:00Z"),
    ],
)
def test_session_spend_accepts_adjacent_nonoverlapping_billing_intervals(
    tmp_path: Path,
    first_resolution: str,
    first_start: str,
    second_resolution: str,
    second_start: str,
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    rows = payload["sessions"][0]["billing_rows"]
    rows[0].update(resolution=first_resolution, interval_start=first_start)
    rows[1].update(
        resource=rows[0]["resource"], resolution=second_resolution, interval_start=second_start
    )
    receipt.write_text(json.dumps(payload))
    allocations = [session_spend_for_job(job, receipt) for job in jobs]
    assert math.fsum(a["modal_allocated_usd"] for a in allocations) == pytest.approx(1.2)
    assert allocations[0]["modal_allocated_usd"] == pytest.approx(0.12)


def test_session_spend_overlapping_times_on_different_resources_remain_additive(
    tmp_path: Path,
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    payload["sessions"][0]["billing_rows"][1].update(
        resolution="h", interval_start="2026-10-01T01:00:00Z"
    )
    receipt.write_text(json.dumps(payload))
    assert session_spend_for_job(jobs[0], receipt)["modal_allocated_usd"] == pytest.approx(0.12)
