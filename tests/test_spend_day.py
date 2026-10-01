"""Unit tests for evallab.spend_day (HAR-122).

Covers:
- UTC-day boundary split (midnight crossing split, duration preservation)
- Deduplication of identical ledger records across checkouts/files
- Billed vs estimate vs ledger basis flagging
- No double count of self-hosted proxy calls (0/0 pricing returns None)
- Card attribution: job prefixes, ledger paths, and strict unattributed bucket
- Cap arithmetic: under, equal, over, headroom calculation
- CLI argument parsing
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

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
    day_to_window,
    dedupe_records,
    model_usd_from_provider_usage,
    parse_day,
    query_model_job_rows,
    sibling_worktree_roots,
    summarize_day,
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

    rows, notes = collect_spend_jsonl_rows(
        main_dir,
        *day_to_window(target_day),
        extra_roots=[sibling_dir],
        label=target_day.isoformat(),
    )
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

    # Ledger with $25 in the sibling worktree
    ledger = sibling / "research/experiments/exp1/spend.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"ts": "2026-10-01T05:00:00Z", "cost_usd": 25.0}) + "\n")

    # Discovery from the LINKED worktree must find the sibling
    roots = sibling_worktree_roots(linked)
    assert sibling.resolve() in [r.resolve() for r in roots]
    assert primary.resolve() in [r.resolve() for r in roots]
    assert linked.resolve() not in [r.resolve() for r in roots]

    # Collecting spend from the linked worktree discovers the $25 sibling ledger
    window = (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), datetime(2026, 10, 2, 0, 0, tzinfo=UTC))
    rows, _ = collect_spend_jsonl_rows(linked, *window, extra_roots=roots)
    assert sum(r.usd for r in rows) == pytest.approx(25.0)


def test_model_job_rows_grouped_by_utc_finish_time_har53(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs must be grouped by UTC finish time, not naive local time (HAR-122).

    HAR-53 fixture: local 2026-09-15T20:02 EDT is UTC 2026-09-16T00:02.
    The token dollars must land on 2026-09-16, never 2026-09-15.
    """
    import types

    class _FakeCursor:
        def fetchall(self):
            # One job with local finished_at 20:02 on 09-15, but lab_metadata finished_at 00:02 on 09-16 UTC
            return [
                (
                    "har53-job",
                    "runs/har53",
                    "job-53",
                    "2026-09-15T20:02:00",
                    {
                        "finished_at": "2026-09-16T00:02:00+00:00",
                        "provider_usage": {
                            "schema_version": 2,
                            "pricing": {
                                "input_cost_micros_per_million": 1,
                                "output_cost_micros_per_million": 1,
                            },
                            "totals": {
                                "requests": 1,
                                "input_tokens": 1000,
                                "output_tokens": 500,
                                "total_tokens": 1500,
                                "cost_micros": 50000,
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
                                    "input_tokens": 1000,
                                    "output_tokens": 500,
                                    "cost_micros": 50000,
                                }
                            ],
                        },
                    },
                ),
                # Second job: naive local only (lab_metadata lacks finished_at)
                (
                    "har53-naive-only",
                    "runs/har53-naive",
                    "job-53-naive",
                    "2026-09-15T20:02:00",
                    {
                        "provider_usage": {
                            "schema_version": 2,
                            "pricing": {
                                "input_cost_micros_per_million": 1,
                                "output_cost_micros_per_million": 1,
                            },
                            "totals": {
                                "requests": 1,
                                "input_tokens": 1000,
                                "output_tokens": 500,
                                "total_tokens": 1500,
                                "cost_micros": 30000,
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
                                    "input_tokens": 1000,
                                    "output_tokens": 500,
                                    "cost_micros": 30000,
                                }
                            ],
                        },
                    },
                ),
            ]

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, *args):
            return _FakeCursor()

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda url: _FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    # Querying for 2026-09-16 UTC must find both jobs (both land on 2026-09-16 UTC)
    rows_16, _ = query_model_job_rows("postgresql://fake/db", *day_to_window(date(2026, 9, 16)))
    assert len(rows_16) == 2
    assert sum(r.usd for r in rows_16) == pytest.approx(0.08)

    # Querying for 2026-09-15 UTC must find zero jobs
    rows_15, _ = query_model_job_rows("postgresql://fake/db", *day_to_window(date(2026, 9, 15)))
    assert len(rows_15) == 0
