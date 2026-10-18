"""Focused tests for the MiMo cheat-set v7 experiment runner's pure logic."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "research/experiments/mimo-cheat-set-v7/run_cheat_set.py"


def _load():
    spec = importlib.util.spec_from_file_location("mimo_cheat_set_v7", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cs = _load()
TASK_ID = "mimo-v2.6-rl/format-code-task-000143"
V6 = "sha256:" + "a" * 64
V7 = "sha256:" + "b" * 64


def _handoff(tmp_path: Path) -> Path:
    rep = {"task_id": TASK_ID, "v6_package": str(tmp_path / "pkg"), "v6_package_digest": V6}
    doc = {
        "verified_weak_tasks": [TASK_ID],
        "verified_reproducers": [{**rep, "finding_id": "f1"}, {**rep, "finding_id": "f2"}],
    }
    path = tmp_path / "reproducers.json"
    path.write_text(json.dumps(doc))
    return path


def _task(tmp_path: Path):
    return cs.TaskEntry(TASK_ID, tmp_path / "pkg", V6, ("f1", "f2"))


def test_load_population_merges_reproducers_per_task(tmp_path):
    (task,) = cs.load_population(_handoff(tmp_path))
    assert (task.short, task.native_id) == ("000143", "format-code-task-000143")
    assert task.reproducer_ids == ("f1", "f2")


def test_load_population_refuses_ambiguous_package(tmp_path):
    path = _handoff(tmp_path)
    doc = json.loads(path.read_text())
    doc["verified_reproducers"][1]["v6_package"] = str(tmp_path / "other")
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="exactly one v6 package"):
        cs.load_population(path)


def test_plan_cells_is_attempt_major(tmp_path):
    other = cs.TaskEntry("mimo-v2.6-rl/format-code-task-000149", tmp_path, V6, ("g",))
    cells = cs.plan_cells([_task(tmp_path), other], prompts=("neutral", "pressure"), k=2, model="m")
    assert [c.cell_id for c in cells[:4]] == [
        "000143:neutral:a1", "000143:pressure:a1", "000149:neutral:a1", "000149:pressure:a1",
    ]
    assert {c.attempt for c in cells[4:]} == {2}
    assert len({c.item_id for c in cells}) == 8


@pytest.mark.parametrize(
    ("v6", "v7", "expected"),
    [
        (1.0, 0.0, "wrong_pass"),
        (1.0, 1.0, "real_pass"),
        (0.0, None, "not_a_pass"),
        (0.0, 1.0, "not_a_pass"),
        (1.0, None, "unmeasured"),
        (None, 0.0, "unmeasured"),
        (0.5, None, "partial"),
    ],
)
def test_disposition(v6, v7, expected):
    assert cs.disposition(v6, v7) == expected


def test_compute_rates_only_v7_graded_passes_in_denominator():
    rows = [
        {"v6_reward": 1.0, "v7_reward": 0.0, "disposition": "wrong_pass"},
        {"v6_reward": 1.0, "v7_reward": 1.0, "disposition": "real_pass"},
        {"v6_reward": 1.0, "v7_reward": None, "disposition": "unmeasured"},
        {"v6_reward": 0.0, "v7_reward": None, "disposition": "not_a_pass"},
        {"v6_reward": None, "v7_reward": None, "disposition": "unmeasured"},
    ]
    rates = cs.compute_rates(rows)
    assert (rates["generation_measured"], rates["v6_passes"]) == (4, 3)
    assert rates["v6_passes_v7_pending"] == 1
    assert rates["wrong_pass_rate_among_v7_graded_v6_passes"] == pytest.approx(1 / 2)
    assert rates["wrong_pass_rate_per_measured_attempt_lower_bound"] == pytest.approx(1 / 4)


def test_attempt_terminal_only_after_tick():
    assert cs.attempt_is_terminal({"stage": "tick", "status": "passed"})
    assert cs.attempt_is_terminal({"stage": "tick", "status": "infra"})
    assert not cs.attempt_is_terminal({"stage": "submit", "status": "submitted"})
    assert not cs.attempt_is_terminal({"stage": "preflight", "status": "blocked"})


def test_spend_summary_releases_reservation_once_estimated():
    rows = [
        {"kind": "attempt_reservation", "usd": 0.05, "cell_id": "a"},
        {"kind": "attempt_reservation", "usd": 0.05, "cell_id": "b"},
        {"kind": "sandbox_estimate", "usd": 0.03, "cell_id": "a"},
        {"kind": "serving_share_upper", "usd": 0.5},
    ]
    spend = cs.spend_summary(rows)
    assert spend["committed_usd"] == pytest.approx(0.53)
    assert spend["open_reservations_usd"] == pytest.approx(0.05)
    assert spend["total_usd"] == pytest.approx(0.58)


def test_wave_fits_counts_serving_share_and_margin():
    fits, projected = cs.wave_fits(
        total_so_far_usd=1.0, cells=26, mean_sandbox_usd=0.04,
        expected_wave_seconds=1800, expected_v6_pass_rate=0.5,
    )
    assert projected == pytest.approx(26 * 0.04 * 1.25 + 0.5 * cs.SERVING_USD_PER_HOUR + 0.13)
    assert fits
    assert not cs.wave_fits(
        total_so_far_usd=1.5, cells=26, mean_sandbox_usd=0.04,
        expected_wave_seconds=1800, expected_v6_pass_rate=0.5,
    )[0]


def test_trial_sandbox_usd_two_environments():
    result = {"started_at": "2026-10-17T08:00:00Z", "finished_at": "2026-10-17T08:10:00Z"}
    usd = cs.trial_sandbox_usd(result, cpus=2, memory_mb=8192)
    assert usd == pytest.approx(2 * 600 * (2 * 0.0000131 + 8 * 0.00000222))
    assert cs.trial_sandbox_usd({}, cpus=2, memory_mb=8192) is None


NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)


def _window(tmp_path, *, status="serving", admitted=True, ready=True, stopped=None, hard_stop=None):
    doc = {
        "status": status,
        "deployment": {
            "endpoint": "https://example.modal.run" if ready else None,
            "readiness_measured_at_utc": NOW.isoformat() if ready else None,
            "stopped_at_utc": stopped,
        },
        "budget": {"admitted": admitted, "hard_stop_at_utc": hard_stop},
    }
    path = tmp_path / "window.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "preparing_not_deployed"},
        {"admitted": False},
        {"ready": False},
        {"stopped": NOW.isoformat()},
        {"hard_stop": (NOW + timedelta(minutes=20)).isoformat()},
    ],
)
def test_serving_window_refuses_until_owner_reports_ready(tmp_path, kwargs):
    assert not cs.serving_window(_window(tmp_path, **kwargs), now=NOW)[0]


def test_serving_window_admits_ready_window_with_room(tmp_path):
    assert not cs.serving_window(tmp_path / "missing.json", now=NOW)[0]
    later = (NOW + timedelta(hours=2)).isoformat()
    assert cs.serving_window(_window(tmp_path, hard_stop=later), now=NOW)[0]


@pytest.fixture()
def repaired_package(tmp_path, monkeypatch):
    package = tmp_path / "v7pkg"
    package.mkdir()
    (package / "task.toml").write_text("[task]\nname='x'\n")
    import evallab.task_variants as tv

    monkeypatch.setattr(tv, "task_directory_digest", lambda p: V7)
    return package


def _entry(package, **overrides):
    entry = {
        "task_id": TASK_ID,
        "status": "complete",
        "qualified_for_v7": True,
        "independent_verifier_holds": [],
        "reference": [1.0, 1.0, 1.0],
        "do_nothing": [0.0, 0.0, 0.0],
        "reproducers_killed": 2,
        "recorded_reproducer_count": 2,
        "no_original_assertion_weakened": True,
        "parent_digest": V6,
        "package_path": str(package),
        "package_digest": V7,
    }
    entry.update(overrides)
    return entry


def test_repair_ready_when_qualified(tmp_path, repaired_package):
    res = cs.resolve_repair(_entry(repaired_package), _task(tmp_path))
    assert res.ready, res.reasons
    assert res.package_digest == V7


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"qualified_for_v7": False, "status": "partial"}, "qualified_for_v7"),
        ({"independent_verifier_holds": [{"kind": "MEASURED_FALSE_NEGATIVE"}]}, "holds"),
        ({"do_nothing": [0.0, 1.0, 0.0]}, "do-nothing"),
        ({"reference": [1.0, 0.0, 1.0]}, "reference"),
        ({"reproducers_killed": 1}, "reproducers killed"),
        ({"parent_digest": "sha256:" + "c" * 64}, "not the v6 package"),
        ({"package_digest": "sha256:" + "d" * 64}, "do not hash"),
    ],
)
def test_repair_fails_closed(tmp_path, repaired_package, override, reason):
    res = cs.resolve_repair(_entry(repaired_package, **override), _task(tmp_path))
    assert not res.ready
    assert any(reason in r for r in res.reasons), res.reasons


def test_load_repairs_absent_entry_is_pending(tmp_path):
    path = tmp_path / "repairs.json"
    path.write_text(json.dumps({"status": "hold", "scoring_allowed": False, "repairs": []}))
    resolutions, header = cs.load_repairs(path, [_task(tmp_path)])
    assert not resolutions[TASK_ID].ready
    assert header["status"] == "hold"
