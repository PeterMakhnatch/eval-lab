"""Focused tests for the MiMo cheat-set v7 experiment runner's pure logic."""

import importlib.util
import json
import sys
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
DIGEST = "sha256:" + "a" * 64


def _handoff(tmp_path: Path) -> Path:
    doc = {
        "verified_weak_tasks": [TASK_ID],
        "verified_reproducers": [
            {
                "task_id": TASK_ID,
                "finding_id": "f1",
                "v6_package": str(tmp_path / "pkg"),
                "v6_package_digest": DIGEST,
            },
            {
                "task_id": TASK_ID,
                "finding_id": "f2",
                "v6_package": str(tmp_path / "pkg"),
                "v6_package_digest": DIGEST,
            },
        ],
    }
    path = tmp_path / "reproducers.json"
    path.write_text(json.dumps(doc))
    return path


def _task(tmp_path: Path):
    return cs.TaskEntry(TASK_ID, tmp_path / "pkg", DIGEST, ("f1", "f2"))


def test_load_population_merges_reproducers_per_task(tmp_path):
    tasks = cs.load_population(_handoff(tmp_path))
    assert len(tasks) == 1
    assert tasks[0].short == "000143"
    assert tasks[0].reproducer_ids == ("f1", "f2")
    assert tasks[0].v6_package_digest == DIGEST


def test_load_population_refuses_ambiguous_package(tmp_path):
    path = _handoff(tmp_path)
    doc = json.loads(path.read_text())
    doc["verified_reproducers"][1]["v6_package"] = str(tmp_path / "other")
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="exactly one v6 package"):
        cs.load_population(path)


def test_plan_cells_k_by_prompt_grid(tmp_path):
    cells = cs.plan_cells([_task(tmp_path)], prompts=("neutral", "pressure"), k=3, model="m")
    assert len(cells) == 6
    assert cells[0].job_name == "mcs7-000143-neutral-a1"
    assert {c.cell_id for c in cells} == {
        "000143:neutral:a1", "000143:neutral:a2", "000143:neutral:a3",
        "000143:pressure:a1", "000143:pressure:a2", "000143:pressure:a3",
    }
    assert len({c.item_id for c in cells}) == 6


@pytest.mark.parametrize(
    ("v6", "v7", "expected"),
    [
        (1.0, 0.0, "wrong_pass"),
        (1.0, 1.0, "real_pass"),
        (0.0, 0.0, "not_a_pass"),
        (0.0, 1.0, "not_a_pass"),
        (0.5, 0.0, "partial"),
        (1.0, None, "unmeasured"),
        (None, 0.0, "unmeasured"),
        (None, None, "unmeasured"),
    ],
)
def test_disposition(v6, v7, expected):
    assert cs.disposition(v6, v7) == expected


def test_compute_rates_counts_first():
    rows = [
        {"v6_reward": 1.0, "v7_reward": 0.0, "disposition": "wrong_pass"},
        {"v6_reward": 1.0, "v7_reward": 1.0, "disposition": "real_pass"},
        {"v6_reward": 0.0, "v7_reward": 0.0, "disposition": "not_a_pass"},
        {"v6_reward": 1.0, "v7_reward": None, "disposition": "unmeasured"},
        {"v6_reward": None, "v7_reward": None, "disposition": "unmeasured"},
    ]
    rates = cs.compute_rates(rows)
    assert rates["generation_measured"] == 4
    assert rates["both_graded"] == 3
    assert rates["pending_v7"] == 1
    assert rates["wrong_pass_rate_both_graded"] == pytest.approx(1 / 3)
    assert rates["wrong_pass_rate_among_v6_passes"] == pytest.approx(1 / 2)
    assert rates["v6_pass_rate"] == pytest.approx(3 / 4)


def test_attempt_terminal_only_with_trial():
    assert cs.attempt_is_terminal({"status": "passed"})
    assert cs.attempt_is_terminal({"status": "failed"})
    assert not cs.attempt_is_terminal({"status": "infra", "stage": "prepare"})
    assert not cs.attempt_is_terminal({"status": "infra", "stage": "approve"})
    assert cs.attempt_is_terminal({"status": "infra", "stage": "tick", "trial_dir": "runs/x"})


def test_extract_trial_reward():
    assert cs.extract_trial_reward({"verifier_result": {"rewards": {"reward": 1.0}}}) == 1.0
    assert cs.extract_trial_reward({"verifier_result": {"rewards": {"reward": 0}}}) == 0.0
    assert cs.extract_trial_reward({}) is None
    assert cs.extract_trial_reward({"verifier_result": {"rewards": {"reward": True}}}) is None


@pytest.fixture()
def fake_package(tmp_path, monkeypatch):
    package = tmp_path / "v7pkg"
    package.mkdir()
    (package / "task.toml").write_text("[task]\nname='x'\n")
    import evallab.task_variants as tv

    monkeypatch.setattr(tv, "task_directory_digest", lambda p: DIGEST)
    return package


def _entry(package, **overrides):
    entry = {
        "task_id": TASK_ID,
        "package": str(package),
        "package_digest": DIGEST,
        "controls": {"reference": [1.0, 1.0, 1.0], "nop": [0.0, 0.0, 0.0]},
        "reproducers_killed": ["f1", "f2"],
    }
    entry.update(overrides)
    return entry


def test_repair_ready_with_controls_and_kills(tmp_path, fake_package):
    res = cs.resolve_repair(_entry(fake_package), _task(tmp_path), repo_root=tmp_path)
    assert res.ready, res.reasons
    assert res.package_digest == DIGEST


def test_repair_blocked_by_bad_nop(tmp_path, fake_package):
    entry = _entry(fake_package, controls={"reference": [1.0], "nop": [1.0]})
    res = cs.resolve_repair(entry, _task(tmp_path), repo_root=tmp_path)
    assert not res.ready
    assert any("nop" in r for r in res.reasons)


def test_repair_blocked_when_reproducer_survives(tmp_path, fake_package):
    entry = _entry(fake_package, reproducers_killed=["f1"])
    res = cs.resolve_repair(entry, _task(tmp_path), repo_root=tmp_path)
    assert not res.ready
    assert any("reproducers" in r for r in res.reasons)


def test_repair_blocked_when_pending_status(tmp_path, fake_package):
    entry = _entry(fake_package, status="in_progress")
    res = cs.resolve_repair(entry, _task(tmp_path), repo_root=tmp_path)
    assert not res.ready
    assert any("status" in r for r in res.reasons)


def test_repair_ready_via_asserted_qualification(tmp_path, fake_package):
    entry = {"task_id": TASK_ID, "package": str(fake_package), "package_digest": DIGEST,
             "status": "qualified"}
    res = cs.resolve_repair(entry, _task(tmp_path), repo_root=tmp_path)
    assert res.ready, res.reasons


def test_repair_blocked_without_controls_or_assertion(tmp_path, fake_package):
    entry = {"task_id": TASK_ID, "package": str(fake_package), "package_digest": DIGEST}
    res = cs.resolve_repair(entry, _task(tmp_path), repo_root=tmp_path)
    assert not res.ready


def test_load_repairs_absent_file_all_pending(tmp_path):
    resolutions = cs.load_repairs(tmp_path / "nope.json", [_task(tmp_path)], repo_root=tmp_path)
    assert not resolutions[TASK_ID].ready
