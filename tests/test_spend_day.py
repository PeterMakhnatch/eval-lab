"""Unit tests for evallab.spend_day (HAR-122, HAR-131).

Covers:
- UTC-day boundary split (midnight crossing split, duration preservation)
- Deduplication of identical ledger records across checkouts/files
- Billed vs estimate vs ledger basis flagging
- No double count of self-hosted proxy calls (0/0 pricing returns None)
- Card attribution: job prefixes, ledger paths, and strict unattributed bucket
- Cap arithmetic: under, equal, over, headroom calculation
- CLI argument parsing
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

from evallab import cli
from evallab.spend_day import (
    BASIS_BILLED,
    BASIS_ESTIMATE,
    BASIS_LEDGER,
    UNATTRIBUTED,
    SpendRow,
    cap_status,
    card_for_job,
    card_for_ledger_path,
    collect_spend_jsonl_rows,
    day_overlap_seconds,
    dedupe_records,
    model_usd_from_provider_usage,
    parse_day,
    session_spend_for_job,
    summarize_day,
    trial_daytona_resources,
)

# ---------------------------------------------------------------------------
# 1. UTC-day boundary split
# ---------------------------------------------------------------------------


def test_utc_day_boundary_split_exact_slices() -> None:
    target_day = date(2026, 9, 30)

    # 1. Trial entirely within 2026-09-30 (10:00 to 11:30 = 90 min = 5400s)
    t_start = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    t_end = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)
    assert day_overlap_seconds(t_start, t_end, target_day) == pytest.approx(5400.0)

    # 2. Trial crossing midnight: starts 2026-09-29 23:30, ends 2026-09-30 01:00 (90 min total)
    c_start = datetime(2026, 9, 29, 23, 30, tzinfo=UTC)
    c_end = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
    # 2026-09-29 gets 30 min (1800s); 2026-09-30 gets 60 min (3600s)
    overlap_prev = day_overlap_seconds(c_start, c_end, date(2026, 9, 29))
    overlap_curr = day_overlap_seconds(c_start, c_end, date(2026, 9, 30))
    assert overlap_prev == pytest.approx(1800.0)
    assert overlap_curr == pytest.approx(3600.0)
    assert overlap_prev + overlap_curr == pytest.approx((c_end - c_start).total_seconds())

    # 3. Trial crossing next midnight: starts 2026-09-30 23:00, ends 2026-10-01 02:00
    n_start = datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    n_end = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    assert day_overlap_seconds(n_start, n_end, target_day) == pytest.approx(3600.0)
    assert day_overlap_seconds(n_start, n_end, date(2026, 10, 1)) == pytest.approx(7200.0)

    # 4. Trial completely outside the target day
    o_start = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    o_end = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
    assert day_overlap_seconds(o_start, o_end, target_day) == 0.0


# ---------------------------------------------------------------------------
# 2. Deduplication of duplicate ledgers across checkouts
# ---------------------------------------------------------------------------


def test_dedupe_duplicate_records_in_memory() -> None:
    rec1 = {"ts": 1790747612.0, "tag": "smoke", "model": "glm-5.3-flash", "cost_usd": 0.001}
    rec2 = {"ts": 1790747612.0, "tag": "smoke", "model": "glm-5.3-flash", "cost_usd": 0.001}
    rec3 = {"ts": 1790747700.0, "tag": "check", "model": "glm-5.3-flash", "cost_usd": 0.002}

    deduped = dedupe_records([rec1, rec2, rec3])
    assert len(deduped) == 2
    assert deduped[0] == rec1
    assert deduped[1] == rec3


def test_collect_spend_jsonl_dedupes_across_sibling_checkouts(tmp_path: Path) -> None:
    target_day = date(2026, 9, 30)
    # Create main repo tree with a spend.jsonl
    main_dir = tmp_path / "worktree-main"
    ledger1 = main_dir / "research/explorations/trace-lab/har111/checker/spend.jsonl"
    ledger1.parent.mkdir(parents=True)

    # Sibling checkout with an exact copy of the spend.jsonl
    sibling_dir = tmp_path / "worktree-sibling"
    ledger2 = sibling_dir / "research/explorations/trace-lab/har111/checker/spend.jsonl"
    ledger2.parent.mkdir(parents=True)

    row1 = json.dumps(
        {"ts": 1790747612.0, "tag": "run1", "cost_usd": 0.005}
    )  # 2026-09-30 05:53 UTC
    row2 = json.dumps({"ts": 1790747712.0, "tag": "run2", "cost_usd": 0.003})

    ledger1.write_text(f"{row1}\n{row2}\n", encoding="utf-8")
    ledger2.write_text(f"{row1}\n{row2}\n", encoding="utf-8")

    rows, notes = collect_spend_jsonl_rows(main_dir, target_day, extra_roots=[sibling_dir])
    # The two identical files should produce only 1 spend row with total $0.008, not doubled to $0.016
    assert len(rows) == 1
    assert rows[0].card == "HAR-111"
    assert rows[0].usd == pytest.approx(0.008)
    assert rows[0].basis == BASIS_LEDGER
    assert any("2 duplicate rows across checkouts ignored" in n for n in notes)


# ---------------------------------------------------------------------------
# 3. Billed vs estimate vs ledger basis flagging
# ---------------------------------------------------------------------------


def test_basis_flagging_integrity() -> None:
    day = date(2026, 9, 30)
    rows = [
        SpendRow(
            source="modal",
            card=UNATTRIBUTED,
            job="modal-account",
            usd=7.22,
            basis=BASIS_BILLED,
            evidence="catalog:modal_billing_rows",
        ),
        SpendRow(
            source="daytona",
            card="HAR-113",
            job="har113-nop",
            usd=2.44,
            basis=BASIS_ESTIMATE,
            evidence="runs/har113-nop:10 trials",
        ),
        SpendRow(
            source="model",
            card="HAR-111",
            job="checker",
            usd=0.18,
            basis=BASIS_LEDGER,
            evidence="research/explorations/trace-lab/har111/checker/spend.jsonl",
        ),
    ]

    ledger = summarize_day(day, rows, cap_usd=20.0)
    assert ledger.rows[0].basis == BASIS_BILLED
    assert ledger.rows[1].basis == BASIS_ESTIMATE
    assert ledger.rows[2].basis == BASIS_LEDGER

    # Ensure basis values are strictly partitionable
    assert {r.basis for r in ledger.rows} == {BASIS_BILLED, BASIS_ESTIMATE, BASIS_LEDGER}


# ---------------------------------------------------------------------------
# 4. No double count of self-hosted proxy calls
# ---------------------------------------------------------------------------


def test_no_double_count_self_hosted_proxy_calls() -> None:
    # 1. Zero-priced route: self-hosted MiMo (tokens are $0 at proxy, billed via Modal)
    zero_priced_usage = {
        "schema_version": 2,
        "pricing": {
            "input_cost_micros_per_million": 0,
            "output_cost_micros_per_million": 0,
        },
        "totals": {
            "requests": 1,
            "input_tokens": 1_000,
            "output_tokens": 500,
            "total_tokens": 1_500,
            "cost_micros": 0,
        },
        "attempted": {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_micros": 0,
        },
        "unresolved_requests": 0,
        "calls": [
            {
                "state": "reconciled",
                "call_id": 1,
                "input_tokens": 1_000,
                "output_tokens": 500,
                "cost_micros": 0,
            }
        ],
    }
    cost, reason = model_usd_from_provider_usage(zero_priced_usage)
    assert cost is None
    assert "self-hosted route bills by server time" in reason

    # 2. Priced route: metered tokens (e.g. Z.ai or OpenRouter)
    priced_usage = {
        "schema_version": 2,
        "pricing": {
            "input_cost_micros_per_million": 150_000,
            "output_cost_micros_per_million": 500_000,
        },
        "totals": {
            "requests": 1,
            "input_tokens": 10_000,
            "output_tokens": 2_000,
            "total_tokens": 12_000,
            "cost_micros": 2_500,  # $0.0025
        },
        "attempted": {
            "requests": 1,
            "input_tokens": 20_000,
            "output_tokens": 4_000,
            "total_tokens": 24_000,
            "cost_micros": 10_000,  # Unresolved reservations must NOT count in used cost
        },
        "unresolved_requests": 1,
        "calls": [
            {
                "state": "reconciled",
                "call_id": 1,
                "input_tokens": 10_000,
                "output_tokens": 2_000,
                "cost_micros": 2_500,
            },
            {
                "state": "reserved",
                "call_id": 2,
                "reserved_input_tokens": 20_000,
                "reserved_output_tokens": 4_000,
                "reserved_cost_micros": 10_000,
            },
        ],
    }
    cost_priced, reason_priced = model_usd_from_provider_usage(priced_usage)
    assert cost_priced == pytest.approx(0.0025)
    assert reason_priced == ""


# ---------------------------------------------------------------------------
# 5. Card attribution: prefixes, paths, and strict unattributed bucket
# ---------------------------------------------------------------------------


def test_card_attribution_and_unattributed_bucket() -> None:
    # Job name prefixes
    assert card_for_job("har81-mimo-sft-001") == "HAR-81"
    assert card_for_job("har113-nop-000240") == "HAR-113"
    assert card_for_job("har0104-d-001") == "HAR-104"
    assert card_for_job("har-105-fix") == "HAR-105"
    assert card_for_job("screening-metered-funcdag") == UNATTRIBUTED
    assert card_for_job("event-summary-oracle") == UNATTRIBUTED
    assert card_for_job(None) == UNATTRIBUTED

    # Ledger paths
    assert (
        card_for_ledger_path("research/explorations/trace-lab/har111/checker/spend.jsonl")
        == "HAR-111"
    )
    assert card_for_ledger_path("research/explorations/trace-lab/har112/spend.jsonl") == "HAR-112"
    assert card_for_ledger_path("/tmp/worktree/research/experiments/har-90/spend.jsonl") == "HAR-90"
    assert card_for_ledger_path("experiments/general/spend.jsonl") == UNATTRIBUTED


# ---------------------------------------------------------------------------
# 6. Cap arithmetic: under, equal, over
# ---------------------------------------------------------------------------


def test_cap_arithmetic() -> None:
    cap = 20.0

    # Under cap ($15 spend)
    over, over_by, headroom = cap_status(15.0, cap)
    assert not over
    assert over_by == 0.0
    assert headroom == pytest.approx(5.0)

    # Exactly at cap ($20 spend)
    over_eq, over_by_eq, headroom_eq = cap_status(20.0, cap)
    assert not over_eq
    assert over_by_eq == 0.0
    assert headroom_eq == 0.0

    # Over cap ($24.50 spend)
    over_hi, over_by_hi, headroom_hi = cap_status(24.5, cap)
    assert over_hi
    assert over_by_hi == pytest.approx(4.5)
    assert headroom_hi == pytest.approx(-4.5)

    # Summarize day handles cap arithmetic correctly
    day = date(2026, 9, 30)
    row = SpendRow(
        source="modal",
        card=UNATTRIBUTED,
        job="test",
        usd=22.50,
        basis=BASIS_BILLED,
        evidence="test",
    )
    ledger = summarize_day(day, [row], cap_usd=cap)
    assert ledger.total_usd == pytest.approx(22.50)
    assert ledger.over_cap is True
    assert ledger.over_by_usd == pytest.approx(2.50)
    assert ledger.headroom_usd == pytest.approx(-2.50)


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
# 8. CLI parser coverage
# ---------------------------------------------------------------------------


def test_spend_day_cli_argument_parser() -> None:
    p = cli.parser()

    args = p.parse_args(["spend", "day", "--date", "2026-09-30"])
    assert args.spend_command == "day"
    assert args.date == "2026-09-30"
    assert args.cap_usd == 20.0
    assert args.json is False

    args_custom = p.parse_args(
        [
            "spend",
            "day",
            "--date",
            "2026-09-29",
            "--cap-usd",
            "35.5",
            "--json",
            "--database-url",
            "sqlite:///:memory:",
        ]
    )
    assert args_custom.date == "2026-09-29"
    assert args_custom.cap_usd == 35.5
    assert args_custom.json is True
    assert args_custom.database_url == "sqlite:///:memory:"

    with pytest.raises(ValueError):
        parse_day("invalid-date")


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
        members.append({
            "job_id": f"job-{name}-{index}",
            "job_name": f"original-{name}-{index}",
            "spec_id": f"spec-{name}-{index}",
            "repository_commit": commit,
            "lab_metadata_sha256": hashlib.sha256(raw_metadata).hexdigest(),
            "trial_wall_seconds": seconds,
            "daytona_estimate_usd": daytona,
        })
    session_id, app = f"ap-{name}", f"eval-app-{name}"
    rows = [{
        "object_id": session_id,
        "description": app,
        "environment": "main",
        "interval_start": "2026-10-01T00:00:00Z",
        "resource": resource,
        "cost_usd": cost,
        "resolution": "day",
        "reported_at": "2026-10-01T02:23:10Z",
    } for resource, cost in (("GPU", 1.1), ("CPU", 0.1))]
    payload = {
        "schema": "evallab.session_spend/v1",
        "sessions": [{
            "session_id": session_id,
            "billing_rows": rows,
            "deployment": {"commit": commit[:7], "time_deployed": "2026-10-01T00:01:27Z"},
            "teardown": {
                "app": app,
                "recorded_at": "2026-10-01T01:45:38Z",
                "completed_spec_ids": [m["spec_id"] for m in members],
            },
            "members": members,
        }],
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
            payload["sessions"][0]["members"][0]["lab_metadata_sha256"] = hashlib.sha256(raw).hexdigest()
            receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session_spend_for_job(job, receipt)


@pytest.mark.parametrize(
    "defect",
    ["missing_member", "undeclared_member", "duplicate_job", "duplicate_spec", "duplicate_teardown", "empty_members"],
)
def test_session_spend_rejects_incomplete_or_duplicate_membership(tmp_path: Path, defect: str) -> None:
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


@pytest.mark.parametrize("defect", ["zero_weights", "overflow_weights", "overflow_pool", "missing_daytona", "missing_hash"])
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
def test_session_spend_rejects_duplicate_billing_identity(
    tmp_path: Path, interval: str
) -> None:
    jobs, receipt, payload = _session_spend_fixture(tmp_path)
    rows = payload["sessions"][0]["billing_rows"]
    rows.append(dict(rows[0], interval_start=interval, resolution="hour", cost_usd=2.0))
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


def test_session_spend_large_finite_prices_do_not_overflow_intermediate_product(tmp_path: Path) -> None:
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
        + json.dumps(payload["sessions"]) + "}"
    )
    with pytest.raises(ValueError):
        session_spend_for_job(jobs[0], receipt)
