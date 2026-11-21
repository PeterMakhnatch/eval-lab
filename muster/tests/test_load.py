"""load.py: seat trials from files. Bad files become problems; nothing raises; nothing is guessed."""

import json
from pathlib import Path

import pytest
from seat_fixture import write_job, write_matrix, write_trial

from muster import load
from muster.model import Problem, SeatTrial

REAL = Path(__file__).parent / "fixtures" / "real-trial" / "hello-seats__7rFRqqz"


def edit(trial: Path, change) -> Path:
    """Rewrite result.json after `change(result)` has modified it."""
    result = json.loads((trial / "result.json").read_text())
    change(result)
    (trial / "result.json").write_text(json.dumps(result))
    return trial


def test_real_trial_of_the_fork():
    trial = load.load_trial(REAL)
    assert isinstance(trial, SeatTrial)
    assert (trial.trial_name, trial.task) == ("hello-seats__7rFRqqz", "hello-seats")
    assert trial.task_checksum == "f8ca14185fadd92a7c0936173756806b7cf0fa27c1af5aba0c0255fc1351ef90"
    assert trial.rewards == {"reward": 1.0} and trial.reward_valid and trial.exception is None
    assert trial.roster_key() == "p1=oracle,p2=oracle"
    assert [(run.seat, run.under_test, run.agent) for run in trial.seats] == [
        ("p1", True, "oracle"),
        ("p2", False, "oracle"),
    ]
    p1 = trial.seats[0]
    assert (p1.model, p1.policy, p1.exception, p1.trajectory) == (None, None, None, None)
    assert (p1.tokens_in, p1.tokens_cache, p1.tokens_out, p1.cost_usd) == (None, None, None, None)
    assert (p1.started_at, p1.finished_at) == ("2026-11-19T08:38:37.438255Z", "2026-11-19T08:38:37.720023Z")
    assert p1.logs == REAL / "seats" / "p1" / "agent" and (p1.logs / "oracle.txt").is_file()
    assert trial.artifacts == REAL / "artifacts"
    assert (trial.started_at, trial.finished_at) == (
        "2026-11-19T08:38:21.471157Z",
        "2026-11-19T08:38:42.720805Z",
    )


def test_every_field_of_a_seat_run(tmp_path):
    seats = {
        "p1": {"name": "terminus-2", "model_name": "deepseek/deepseek-flash"},
        "p2": "spoof",
        "p3": "nop",
    }
    usage = {"p1": {"n_input_tokens": 1200, "n_cache_tokens": 200, "n_output_tokens": 30, "cost_usd": 0.004}}
    path = write_trial(
        tmp_path,
        "t__001",
        seats,
        rewards={"reward": 0.5, "seat.p1": 1, "messages": 3},
        not_tested=("p3",),
        exception="AgentTimeoutError",
        seat_exceptions={"p1": "AgentTimeoutError"},
        usage=usage,
        trajectories=("p1", "p2"),
    )
    trial = load.load_trial(path)
    assert isinstance(trial, SeatTrial)
    p1, p2, p3 = trial.seats
    assert (p1.agent, p1.model, p1.policy, p1.label) == (
        "terminus-2",
        "deepseek/deepseek-flash",
        None,
        "terminus-2/deepseek/deepseek-flash",
    )
    assert (p1.tokens_in, p1.tokens_cache, p1.tokens_out, p1.cost_usd) == (1200, 200, 30, 0.004)
    assert (p1.exception, p1.under_test, p1.trajectory) == (
        "AgentTimeoutError",
        True,
        path / "seats/p1/agent/trajectory.json",
    )
    assert (p2.agent, p2.model, p2.policy, p2.label, p2.exception) == ("script", None, "spoof", "spoof", None)
    assert (p3.agent, p3.policy, p3.under_test, p3.trajectory) == ("nop", None, False, None)
    assert trial.rewards == {"reward": 0.5, "seat.p1": 1.0, "messages": 3.0}
    assert all(isinstance(value, float) for value in trial.rewards.values())
    assert (trial.exception, trial.reward_valid) == ("AgentTimeoutError", True)
    assert trial.roster_key() == "p1=terminus-2/deepseek/deepseek-flash,p2=spoof,p3=nop"


def test_task_is_the_last_part_in_lower_case(tmp_path):
    trial = load.load_trial(
        write_trial(tmp_path, "t", {"p1": "oracle"}, task="Muster/Boxes-I03", rewards={"reward": 1})
    )
    assert trial.task == "boxes-i03"


def test_rewards_are_the_finite_numbers_only(tmp_path):
    path = write_trial(tmp_path, "t", {"p1": "oracle"}, rewards={"reward": 1})
    text = (
        (path / "result.json")
        .read_text()
        .replace(
            '"reward": 1',
            '"reward": 1, "nan": NaN, "inf": Infinity, "neg": -Infinity, "text": "1", "flag": true, '
            '"none": null, "list": [1], "huge": 1' + "0" * 400 + ', "ok": -2.5',
        )
    )
    (path / "result.json").write_text(text)
    assert load.load_trial(path).rewards == {"reward": 1.0, "ok": -2.5}


def test_no_verifier_result_gives_no_rewards(tmp_path):
    trial = load.load_trial(
        write_trial(tmp_path, "t", {"p1": "oracle"}, rewards=None, exception="RuntimeError")
    )
    assert (trial.rewards, trial.reward_valid, trial.exception) == ({}, False, "RuntimeError")


def test_reward_valid_is_true_only_when_the_file_says_true(tmp_path):
    path = write_trial(tmp_path, "t", {"p1": "oracle"}, rewards={"reward": 1})
    for value, expected in ((True, True), (False, False), (None, False), ("true", False), (1, False)):
        edit(path, lambda result, value=value: result.update(reward_valid=value))
        assert load.load_trial(path).reward_valid is expected
    edit(path, lambda result: result.pop("reward_valid"))
    assert load.load_trial(path).reward_valid is False


def test_policy_needs_a_script_agent_and_a_script(tmp_path):
    def policy_of(participant: dict):
        path = write_trial(tmp_path, "t", {"p1": participant}, rewards={"reward": 1})
        return load.load_trial(path).seats[0].policy

    script = {"import_path": "muster.agents.script:ScriptAgent", "kwargs": {"script": "/x/late.sh"}}
    assert policy_of(script) == "late"
    assert policy_of({**script, "import_path": "other.pkg:MyScriptAgent"}) == "late"
    assert policy_of({**script, "import_path": "other.pkg:Agent"}) is None
    assert policy_of({**script, "kwargs": {}}) is None
    assert policy_of({**script, "kwargs": {"script": 7}}) is None
    assert policy_of({"name": "oracle", "kwargs": {"script": "/x/late.sh"}}) is None


def test_model_comes_from_the_participant_then_from_the_agent_info(tmp_path):
    path = write_trial(
        tmp_path, "t", {"p1": {"name": "codex", "model_name": "openai/gpt-5.5"}}, rewards={"reward": 1}
    )
    assert load.load_trial(path).seats[0].model == "openai/gpt-5.5"
    edit(path, lambda result: result["config"]["participants"][0].update(model_name=None))
    assert load.load_trial(path).seats[0].model == "openai/gpt-5.5"  # agent_info: provider/name
    edit(path, lambda result: result["seat_results"][0]["agent_info"]["model_info"].update(provider=None))
    assert load.load_trial(path).seats[0].model == "gpt-5.5"
    edit(path, lambda result: result["seat_results"][0]["agent_info"].update(model_info=None))
    assert load.load_trial(path).seats[0].model is None


def test_agent_name_falls_back_to_the_participant(tmp_path):
    path = write_trial(tmp_path, "t", {"p1": "oracle", "p2": "idle"}, rewards={"reward": 1})
    edit(path, lambda result: [seat.update(agent_info=None) for seat in result["seat_results"]])
    trial = load.load_trial(path)
    assert [run.agent for run in trial.seats] == ["oracle", "muster.agents.script:ScriptAgent"]
    edit(path, lambda result: result.update(config={}))
    assert load.load_trial(path) == Problem(path, "seat_results[0]: no agent name")


def test_an_exception_record_without_a_type_is_unknown(tmp_path):
    path = write_trial(tmp_path, "t", {"p1": "oracle"}, rewards={"reward": 1})
    edit(path, lambda result: result.update(exception_info={"exception_message": "boom"}))
    edit(path, lambda result: result["seat_results"][0].update(exception_info="boom"))
    trial = load.load_trial(path)
    assert (trial.exception, trial.seats[0].exception) == ("unknown", "unknown")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.pop("seat_results"), "not a seat trial"),
        (lambda r: r.update(seat_results=None), "not a seat trial"),
        (lambda r: r.update(seat_results=[]), "not a seat trial"),
        (lambda r: r.update(seat_results={"p1": {}}), "not a seat trial"),
        (lambda r: r.pop("trial_name"), "result.json has no trial_name or no task_name"),
        (lambda r: r.update(task_name=7), "result.json has no trial_name or no task_name"),
        (lambda r: r["seat_results"].append("p3"), "seat_results[2]: not a JSON object"),
        (
            lambda r: r["seat_results"][1].update(seat="../x"),
            "seat_results[1]: seat '../x' is not a service name",
        ),
        (lambda r: r["seat_results"][1].pop("seat"), "seat_results[1]: seat None is not a service name"),
        (
            lambda r: r["seat_results"][0].update(under_test="yes"),
            "seat_results[0]: under_test is not true or false",
        ),
        (lambda r: r["seat_results"][1].update(seat="p1"), "seat_results names a seat twice"),
    ],
)
def test_unusable_results_become_problems(tmp_path, change, reason):
    path = edit(write_trial(tmp_path, "t", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 1}), change)
    assert load.load_trial(path) == Problem(path, reason)


@pytest.mark.parametrize(
    "content", ["", "{", "[1, 2]", '"text"', "null", "{" * 100000, "\ud800".encode("utf-8", "surrogatepass")]
)
def test_unreadable_result_files_become_problems(tmp_path, content):
    path = tmp_path / "t"
    path.mkdir()
    (path / "result.json").write_bytes(content if isinstance(content, bytes) else content.encode())
    problem = load.load_trial(path)
    assert isinstance(problem, Problem) and problem.path == path and problem.reason


def test_a_missing_result_file_is_a_problem(tmp_path):
    problem = load.load_trial(tmp_path)
    assert isinstance(problem, Problem) and problem.reason.startswith("cannot read result.json")


def test_odd_values_never_raise(tmp_path):
    odd = [None, 0, 1.5, True, "x", [], {}, [[]], {"a": {"b": []}}]
    path = write_trial(tmp_path, "t", {"p1": "oracle", "p2": "idle"}, rewards={"reward": 1})
    original = (path / "result.json").read_text()
    top = [
        "config",
        "verifier_result",
        "exception_info",
        "seat_results",
        "started_at",
        "task_checksum",
        "reward_valid",
    ]
    inner = ["agent_info", "agent_result", "agent_execution", "exception_info", "seat", "under_test"]
    for value in odd:
        for key in top:
            (path / "result.json").write_text(original)
            edit(path, lambda result, key=key, value=value: result.update({key: value}))
            assert isinstance(load.load_trial(path), SeatTrial | Problem)
        for key in inner:
            (path / "result.json").write_text(original)
            edit(path, lambda result, key=key, value=value: result["seat_results"][0].update({key: value}))
            assert isinstance(load.load_trial(path), SeatTrial | Problem)
        for key in ("participants", "kwargs", "import_path", "model_name", "name"):
            (path / "result.json").write_text(original)
            edit(
                path,
                lambda result, key=key, value=value: result["config"]["participants"][1].update({key: value}),
            )
            edit(
                path,
                lambda result, key=key, value=value: (
                    result["config"].update({key: value}) if key == "participants" else None
                ),
            )
            assert isinstance(load.load_trial(path), SeatTrial | Problem)


def test_walk_finds_every_trial_in_sorted_order_and_prunes_inside_trials(tmp_path):
    out = write_matrix(
        tmp_path / "m", {"p1=oracle,p2=oracle": [1.0, 1.0], "p1=nop,p2=nop": [0.0]}, manifest={}
    )
    inside = out / "p1=nop,p2=nop" / "jobs" / "p1=nop,p2=nop" / "task__001"
    write_trial(inside / "artifacts", "copy__001", {"p1": "oracle", "p2": "oracle"}, rewards={"reward": 1})
    found = list(load.walk(out))
    assert all(isinstance(item, SeatTrial) for item in found)
    assert [str(item.path.relative_to(out)) for item in found] == [
        "p1=nop,p2=nop/jobs/p1=nop,p2=nop/task__001",
        "p1=oracle,p2=oracle/jobs/p1=oracle,p2=oracle/task__001",
        "p1=oracle,p2=oracle/jobs/p1=oracle,p2=oracle/task__002",
    ]
    assert [item.roster_key() for item in found] == [
        "p1=nop,p2=nop",
        "p1=oracle,p2=oracle",
        "p1=oracle,p2=oracle",
    ]


def test_walk_reports_problems_and_goes_on(tmp_path):
    job = tmp_path / "job"
    write_job(job, {"p1": "oracle", "p2": "nop"}, [1.0])
    write_trial(job, "single__001", {"p1": "oracle"}, rewards={"reward": 1})
    edit(job / "single__001", lambda result: result.pop("seat_results"))
    (job / "broken__001").mkdir()
    (job / "broken__001" / "result.json").write_text("{")
    write_trial(job / "broken__001", "nested__001", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 0})
    (job / "started__001").mkdir()
    (job / "started__001" / "config.json").write_text(json.dumps({"trial_name": "started__001", "task": {}}))
    (job / "other").mkdir()
    (job / "other" / "result.json").write_text(json.dumps({"hello": 1}))
    (job / "other" / "notes").mkdir()
    write_trial(job / ".sources", "cached__001", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 1})
    (job / "link").symlink_to(job / "task__001")
    found = {str(item.path.relative_to(job)): item for item in load.walk(tmp_path)}
    assert list(found) == [
        "broken__001",
        "broken__001/nested__001",
        "other",
        "single__001",
        "started__001",
        "task__001",
    ]
    assert found["broken__001"].reason.startswith("result.json is not valid JSON")
    assert isinstance(found["broken__001/nested__001"], SeatTrial)
    assert found["other"] == Problem(job / "other", "result.json is not a trial result")
    assert found["single__001"] == Problem(job / "single__001", "not a seat trial")
    assert found["started__001"] == Problem(job / "started__001", "trial has no result.json")
    assert isinstance(found["task__001"], SeatTrial)


def test_walk_of_something_that_is_not_a_directory(tmp_path):
    assert list(load.walk(tmp_path / "missing")) == [Problem(tmp_path / "missing", "not a directory")]
    (tmp_path / "file").write_text("x")
    assert list(load.walk(tmp_path / "file")) == [Problem(tmp_path / "file", "not a directory")]
    assert list(load.walk(tmp_path)) == []
