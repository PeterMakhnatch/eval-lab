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
        {"task_id": "a", "v6_reward": 1.0, "v7_reward": 0.0, "disposition": "wrong_pass"},
        {"task_id": "a", "v6_reward": 1.0, "v7_reward": 0.0, "disposition": "wrong_pass"},
        {"task_id": "b", "v6_reward": 1.0, "v7_reward": 1.0, "disposition": "real_pass"},
        {"task_id": "c", "v6_reward": 1.0, "v7_reward": None, "disposition": "unmeasured"},
        {"task_id": "d", "v6_reward": 0.0, "v7_reward": None, "disposition": "not_a_pass"},
        {"task_id": "e", "v6_reward": None, "v7_reward": None, "disposition": "unmeasured"},
    ]
    rates = cs.compute_rates(rows)
    assert (rates["generation_measured"], rates["v6_passes"]) == (5, 4)
    assert rates["v6_passes_v7_pending"] == 1
    among = rates["wrong_pass_rate_among_v7_graded_v6_passes"]
    assert (among["k"], among["n"]) == (2, 3)
    lo, hi = among["wilson95"]
    assert lo < 2 / 3 < hi
    assert rates["wrong_pass_rate_per_measured_attempt_lower_bound"]["n"] == 5
    # Two wrong passes on one task count once at task level.
    tasks = rates["tasks_with_wrong_pass"]
    assert (tasks["k"], tasks["n"]) == (1, 2)


def test_compute_rates_empty_group_has_no_rate():
    rates = cs.compute_rates([])
    assert rates["wrong_pass_rate_among_v7_graded_v6_passes"]["rate"] is None


def test_attempt_terminal_only_after_tick():
    assert cs.attempt_is_terminal({"stage": "tick", "status": "passed"})
    assert cs.attempt_is_terminal({"stage": "tick", "status": "infra"})
    assert not cs.attempt_is_terminal({"stage": "submit", "status": "submitted"})
    assert not cs.attempt_is_terminal({"stage": "preflight", "status": "blocked"})


def test_spend_summary_releases_reservation_and_excludes_serving():
    rows = [
        {"kind": "attempt_reservation", "usd": 0.05, "cell_id": "a"},
        {"kind": "attempt_reservation", "usd": 0.05, "cell_id": "b"},
        {"kind": "sandbox_estimate", "usd": 0.03, "cell_id": "a"},
        {"kind": "grade_reservation", "usd": 0.01, "cell_id": "a"},
        {"kind": "serving_time_info", "usd": 0.5},
    ]
    spend = cs.spend_summary(rows)
    assert spend["committed_usd"] == pytest.approx(0.04)
    assert spend["open_reservations_usd"] == pytest.approx(0.05)
    assert spend["total_usd"] == pytest.approx(0.09)


def test_wave_fits_counts_sandboxes_margin_and_replays():
    fits, projected = cs.wave_fits(total_so_far_usd=2.0, cells=26, mean_sandbox_usd=0.04)
    assert projected == pytest.approx(26 * (0.04 * 1.25 + cs.GRADE_USD_PER_EXECUTION))
    assert fits
    assert not cs.wave_fits(total_so_far_usd=2.5, cells=26, mean_sandbox_usd=0.04)[0]


def test_metered_route_keeps_llm_spend_out_of_the_sandbox_cap(monkeypatch):
    monkeypatch.setattr(cs, "ROUTE", cs.ROUTES["oss"])
    rows = [
        {"kind": "attempt_reservation", "usd": 0.038, "pool": "llm", "cell_id": "a"},
        {"kind": "attempt_reservation", "usd": 0.038, "pool": "llm", "cell_id": "b"},
        {"kind": "llm_estimate", "usd": 0.012, "cell_id": "a"},
        {"kind": "grade_reservation", "usd": 0.01, "cell_id": "a"},
    ]
    spend = cs.spend_summary(rows)
    assert spend["llm_usd"] == pytest.approx(0.012 + 0.038)
    assert spend["total_usd"] == pytest.approx(0.01)
    assert spend["cap_usd"] == cs.ROUTES["oss"].cap_usd


def test_metered_route_wave_must_fit_the_llm_allocation(monkeypatch):
    monkeypatch.setattr(cs, "ROUTE", cs.ROUTES["oss"])
    ceiling = cs.ROUTES["oss"].ceilings["cost_limit_usd"]
    assert cs.wave_fits(total_so_far_usd=0.0, cells=26, mean_sandbox_usd=0.0)[0]
    room = cs.ROUTES["oss"].llm_cap_usd - 26 * ceiling
    assert not cs.wave_fits(
        total_so_far_usd=0.0, cells=26, mean_sandbox_usd=0.0, llm_so_far_usd=room + 0.01
    )[0]


def test_trial_llm_usd_prices_recorded_tokens(monkeypatch):
    monkeypatch.setattr(cs, "ROUTE", cs.ROUTES["oss"])
    price_in, price_out = cs.ROUTES["oss"].llm_price_per_mtok
    usd = cs.trial_llm_usd({"n_input_tokens": 2_000_000, "n_output_tokens": 100_000})
    assert usd == pytest.approx(2 * price_in + 0.1 * price_out)
    assert cs.trial_llm_usd({"n_input_tokens": 5}) is None


def test_v71_identity_reuses_v7_reward_only_for_identical_bytes(tmp_path):
    target = cs.V7Target(TASK_ID, True, (), tmp_path, V7, "semantic_repair")
    other = cs.V7Target("mimo-v2.6-rl/format-code-task-000149", True, (), tmp_path, V7, "semantic_repair")
    doc = {"tasks": [
        {"task_id": "format-code-task-000143", "evaluation_target": {"package_digest": V7}},
        {"task_id": "format-code-task-000149", "evaluation_target": {"package_digest": V6}},
    ]}
    path = tmp_path / "heldout-v7.1.json"
    path.write_text(json.dumps(doc))
    out, _ = cs.v71_identity(path, {TASK_ID: target, other.task_id: other})
    assert out[TASK_ID]["identical_to_v7"] is True
    assert out[other.task_id]["identical_to_v7"] is False



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


def _row(package, *, selection="semantic-repair", digest=V7, v6=V6):
    return {
        "task_id": TASK_ID.rsplit("/", 1)[-1],  # the release uses native ids
        "successor_selection": selection,
        "v6_package_digest": v6,
        "evaluation_target": {
            "package_path": str(package), "package_digest": digest,
            "repair_scope": "append-only-verifier",
        },
    }


def _heldout(tmp_path, rows, *, status="released", allowed=True):
    path = tmp_path / "heldout-v7.json"
    path.write_text(json.dumps({"status": status, "scoring_allowed": allowed, "tasks": rows}))
    return path


def test_released_semantic_repair_is_ready(tmp_path, repaired_package):
    res, header = cs.load_v7_targets(_heldout(tmp_path, [_row(repaired_package)]), [_task(tmp_path)])
    assert res[TASK_ID].ready and res[TASK_ID].kind == "semantic_repair"
    assert res[TASK_ID].package_digest == V7
    assert header["status"] == "released"


@pytest.mark.parametrize(("status", "allowed"), [("candidate", False), ("released", False)])
def test_unreleased_heldout_grades_nothing(tmp_path, repaired_package, status, allowed):
    path = _heldout(tmp_path, [_row(repaired_package)], status=status, allowed=allowed)
    res = cs.load_v7_targets(path, [_task(tmp_path)])[0][TASK_ID]
    assert not res.ready and res.kind == "pending"


@pytest.mark.parametrize(
    ("row_kwargs", "reason"),
    [
        ({"digest": "sha256:" + "d" * 64}, "do not hash"),
        ({"v6": "sha256:" + "c" * 64}, "population v6 digest"),
        ({"selection": "unchanged-v6"}, "not the v6 package"),
        ({"selection": "mystery"}, "unknown successor_selection"),
    ],
)
def test_v7_target_fails_closed(tmp_path, repaired_package, row_kwargs, reason):
    res = cs.resolve_v7_target(_row(repaired_package, **row_kwargs), _task(tmp_path))
    assert not res.ready and res.kind == "pending"
    assert any(reason in r for r in res.reasons), res.reasons


def test_absent_task_is_pending(tmp_path):
    res = cs.load_v7_targets(_heldout(tmp_path, []), [_task(tmp_path)])[0][TASK_ID]
    assert not res.ready
