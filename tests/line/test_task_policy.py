"""Task-status reason codes, per-status policy and reward-1 reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import read_json, read_rows, write_json, write_rows
from evallab.line.spec import load_spec

MANIFEST = {
    "t-gold": {"status": "gold"},
    "t-cert": {"status": "certified"},
    "t-prov": {"status": "provisional"},
    "t-unc": {"status": "uncertified"},
    "t-weak": {"status": "weak"},
    "t-odd": {"status": "mystery"},
}


@pytest.mark.parametrize(
    "task,reason,decision",
    [
        ("t-gold", "TASK_GOLD", "pass"),
        ("t-cert", "TASK_CERTIFIED", "pass"),
        ("t-prov", "TASK_PROVISIONAL", "pass"),
        ("t-unc", "TASK_UNCERTIFIED", "pass"),
        ("t-weak", "TASK_WEAK", "review"),
        # a status the manifest never defined cannot count as certified
        ("t-odd", "TASK_UNCERTIFIED", "pass"),
        # only a task missing from the manifest is "unknown"
        ("t-absent", "TASK_STATUS_UNKNOWN", "pass"),
    ],
)
def test_each_status_has_its_own_reason_and_todays_default_action(
    task: str, reason: str, decision: str
) -> None:
    verdict = hd.evaluate_task_stage(task, MANIFEST)
    assert (verdict.reasons, verdict.decision) == ([reason], decision)


def test_status_actions_override_only_what_they_name() -> None:
    actions = {"uncertified": "review", "provisional": "drop", "weak": "pass"}
    got = {
        task: hd.evaluate_task_stage(task, MANIFEST, "drop", status_actions=actions).decision
        for task in ("t-cert", "t-prov", "t-unc", "t-weak", "t-gold")
    }
    assert got == {
        "t-cert": "pass",
        "t-prov": "drop",
        "t-unc": "review",
        "t-weak": "pass",
        "t-gold": "pass",
    }
    # without an override, the legacy weak action still comes from default_action
    assert hd.evaluate_task_stage("t-weak", MANIFEST, "drop").decision == "drop"


@pytest.mark.parametrize("actions", [{"bogus": "review"}, {"weak": "maybe"}, {"certified": ""}])
def test_invalid_status_actions_fail_closed(actions: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="invalid task status action"):
        hd.evaluate_task_stage("t-cert", MANIFEST, status_actions=actions)


def _status(spec_path: Path, statuses: dict[str, str]) -> None:
    write_json(
        Path(read_json(spec_path)["task_status"]),
        {"tasks": [{"task_id": t, "status": s, "reason": "r"} for t, s in statuses.items()]},
    )


def _three_runs(spec_path: Path) -> None:
    manifest = Path(read_json(spec_path)["sources"][0]["rollouts"])
    clean = spec_path.parent / "clean.py"
    write_rows(
        manifest,
        [
            {"id": f"run-{task}", "task_id": task, "path": str(clean), "reward": 1}
            for task in ("plain-task", "prov-task", "ghost-task")
        ],
    )


def test_line_reports_status_reasons_and_policy_changes_decisions(fixture_spec: Path) -> None:
    _three_runs(fixture_spec)
    _status(fixture_spec, {"plain-task": "certified", "prov-task": "provisional"})
    spec = read_json(fixture_spec)
    spec["audit"]["rate"] = 0
    write_json(fixture_spec, spec)
    first = core.run_line(fixture_spec, fixture_spec.parent)
    rows = {r["run_id"]: r for r in read_rows(first / "decisions.jsonl")}
    assert {k: (v["decision"], v["reasons"]) for k, v in rows.items()} == {
        "run-plain-task": ("keep", ["TASK_CERTIFIED"]),
        "run-prov-task": ("keep", ["TASK_PROVISIONAL"]),
        "run-ghost-task": ("keep", ["TASK_UNCERTIFIED"]),
    }
    side = read_json(first / "scoreboard.json")["task_side"]
    assert (side["reward1_certified"], side["reward1_provisional"]) == (1, 1)
    assert (side["reward1_uncertified"], side["reward1_weak"]) == (1, 0)

    spec.update(batch_id="strict", task_policy={"uncertified": "review", "provisional": "review"})
    write_json(fixture_spec, spec)
    strict = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(strict, fixture_spec.parent)["passed"]
    decisions = {r["run_id"]: r["decision"] for r in read_rows(strict / "decisions.jsonl")}
    assert decisions == {
        "run-plain-task": "keep",
        "run-prov-task": "review",
        "run-ghost-task": "review",
    }


@pytest.mark.parametrize(
    "policy",
    [{"bogus": "review"}, {"weak": "maybe"}, "review", {"weak": "drop"}],
)
def test_task_policy_is_validated(fixture_spec: Path, policy: object) -> None:
    spec = read_json(fixture_spec)
    spec["task_action"] = "review"
    spec["task_policy"] = policy
    write_json(fixture_spec, spec)
    with pytest.raises(ValueError, match="task_policy|task_action"):
        load_spec(fixture_spec)


def test_task_policy_defaults_keep_todays_decisions(fixture_spec: Path) -> None:
    loaded = load_spec(fixture_spec)
    assert loaded["task_policy"] == {
        "certified": "pass",
        "provisional": "pass",
        "uncertified": "pass",
        "weak": "review",
    }
