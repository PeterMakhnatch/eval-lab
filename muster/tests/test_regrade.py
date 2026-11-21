"""Replay grading: the reward of a finished trial is recomputed from its event log."""

import json
from pathlib import Path

import pytest

from muster import cli, load, regrade

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tasks" / "_kit" / "worldkit.py"

VERIFY = """
import sys
sys.path.insert(0, "/world")
import worldkit


def score(events, instance):
    opened = {event["request"]["box"] for event in worldkit.in_play(events) if event["kind"] == "open"}
    return {"reward": len(opened & set(instance["flags"])) / len(instance["flags"]), "opens": len(opened)}
"""


def event(seq, seat, kind, request=None, status=200):
    return {
        "seq": seq,
        "t": 100.0 + seq,
        "seat": seat,
        "kind": kind,
        "method": "POST",
        "status": status,
        "ok": status == 200,
        "request": request,
        "response": {},
    }


def write_log(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in events))


@pytest.fixture
def task(tmp_path):
    root = tmp_path / "task"
    (root / "tests").mkdir(parents=True)
    (root / "environment" / "world").mkdir(parents=True)
    (root / "tests" / "verify.py").write_text(VERIFY)
    (root / "environment" / "world" / "worldkit.py").write_text(KIT.read_text())
    (root / "environment" / "world" / "instance.json").write_text(json.dumps({"flags": [1, 2]}))
    return root


def trial(tmp_path, task, name, rewards, graded=None, collected=None):
    root = tmp_path / "job" / name
    root.mkdir(parents=True)
    result = {
        "trial_name": name,
        "task_name": "muster/toy-i01",
        "config": {"task": {"path": str(task)}, "participants": [{"seat": "p1", "name": "oracle"}]},
        "verifier_result": {"rewards": rewards},
        "reward_valid": True,
        "seat_results": [{"seat": "p1", "under_test": True, "agent_info": {"name": "oracle"}}],
    }
    (root / "result.json").write_text(json.dumps(result))
    if graded is not None:
        write_log(root / "verifier" / "events.jsonl", graded)
    if collected is not None:
        write_log(root / "artifacts" / "logs" / "world" / "events.jsonl", collected)
    return root


PLAY = [event(1, "p1", "open", {"box": 1}), event(2, "p1", "open", {"box": 9})]
CLOSE = event(3, None, "admin.close", {})


def test_the_score_function_is_loaded_without_leaving_cache_files(task):
    score, instance, reason = regrade.load_score(task)
    assert reason is None and instance == {"flags": [1, 2]}
    assert score(PLAY, instance) == {"reward": 0.5, "opens": 2}
    assert not list(task.rglob("__pycache__"))


def test_a_task_without_a_score_function_is_a_reason_and_not_a_crash(tmp_path, task):
    assert regrade.load_score(tmp_path)[2] == "the task has no tests/verify.py"
    (task / "tests" / "verify.py").write_text("x = 1\n")
    assert regrade.load_score(task)[2] == "tests/verify.py has no score(events, instance)"
    (task / "tests" / "verify.py").write_text("raise RuntimeError('boom')")
    assert "does not import" in regrade.load_score(task)[2]


def test_match_mismatch_and_late_actions(task):
    score, instance, _ = regrade.load_score(task)
    assert (
        regrade.regrade_trial({"reward": 0.5, "opens": 2.0}, [*PLAY, CLOSE], PLAY, score, instance)["status"]
        == "match"
    )
    wrong = regrade.regrade_trial({"reward": 1.0, "opens": 2.0}, [*PLAY, CLOSE], PLAY, score, instance)
    assert wrong["status"] == "mismatch" and wrong["differences"] == {"reward": [1.0, 0.5]}
    # box 2 was opened after Harbor collected the artifacts: the verifier counted it
    late_open = event(3, "p1", "open", {"box": 2})
    counted = regrade.regrade_trial(
        {"reward": 1.0, "opens": 3.0},
        [*PLAY, late_open, event(4, None, "admin.close", {})],
        PLAY,
        score,
        instance,
    )
    assert counted["status"] == "match" and counted["late_actions"] == [
        {"seq": 3, "seat": "p1", "kind": "open"}
    ]


def test_without_a_graded_log_the_collected_log_is_replayed(task):
    score, instance, _ = regrade.load_score(task)
    result = regrade.regrade_trial({"reward": 0.5, "opens": 2.0}, None, PLAY, score, instance)
    assert (result["status"], result["log"], result["late_actions"]) == ("match", "collected", [])
    assert regrade.regrade_trial({"reward": 0.5}, None, None, score, instance)["status"] == "no-log"


def test_event_log_readers(tmp_path, task):
    root = trial(tmp_path, task, "t1", {"reward": 0.5}, graded=[*PLAY, CLOSE], collected=PLAY)
    logs = load.event_logs(root)
    assert logs["graded"].name == "events.jsonl" and "verifier" in logs["graded"].parts
    assert "artifacts" in logs["collected"].parts
    assert len(load.read_events(logs["graded"])[0]) == 3
    logs["collected"].write_text(json.dumps(PLAY[1]) + "\n")
    assert load.read_events(logs["collected"]) == (None, "line 1 is not event 1")
    assert load.event_logs(tmp_path) == {"graded": None, "collected": None}


def test_command_exit_status_and_text(tmp_path, task, capsys):
    trial(tmp_path, task, "good", {"reward": 0.5, "opens": 2.0}, graded=[*PLAY, CLOSE], collected=PLAY)
    assert cli.main(["regrade", str(tmp_path / "job")]) == 0
    assert "Every recorded reward equals" in capsys.readouterr().out
    trial(tmp_path, task, "bad", {"reward": 1.0, "opens": 2.0}, graded=[*PLAY, CLOSE], collected=PLAY)
    assert cli.main(["regrade", str(tmp_path / "job"), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {"match": 1, "mismatch": 1}
    assert cli.main(["regrade", str(tmp_path / "nothing-here")]) == 3


def test_events_command_reads_the_same_logs(tmp_path, task, capsys):
    trial(tmp_path, task, "good", {"reward": 0.5}, graded=[*PLAY, CLOSE], collected=PLAY)
    assert cli.main(["events", f"arm-a={tmp_path / 'job'}", "--json"]) == 0
    arm = json.loads(capsys.readouterr().out)["arms"][0]
    assert (arm["arm"], arm["trials"], arm["with_log"], arm["means"]["actions"]) == ("arm-a", 1, 1, 2.0)
