import json
import re

from tidegauge.load import load_arm, load_arms, parse_trial, rekey, split_arms, task_key
from tidegauge.model import Counting

from harbor_fixture import trial, write_job


def test_task_key_merges_the_names_harbor_uses_for_one_task():
    assert task_key("regex-log") == "regex-log"
    assert task_key("terminal-bench/regex-log") == "regex-log"
    assert task_key("install-windows-3.11") == task_key("terminal-bench/install-windows-3-11")
    assert task_key("Some_Task") == "some_task"


def test_task_key_option_takes_the_key_from_the_first_group(tmp_path):
    names = ["flotilla/schedule-009-solo", "flotilla/Schedule_009-talk", "flotilla/schedule-009", "-talk"]
    arm = load_arm(write_job(tmp_path, "job", [trial(name, 1.0) for name in names]))
    assert {t.key_matched for t in arm.trials} == {None}  # no --task-key
    keyed = rekey(arm, re.compile(r"^(.*)-(?:solo|silent|talk)$"))
    # The group goes through the usual normalization. A name that does not match, or an empty group, keeps its key.
    assert {t.task_name: (t.task, t.key_matched) for t in keyed.trials} == {
        "flotilla/schedule-009-solo": ("schedule-009", True),
        "flotilla/Schedule_009-talk": ("schedule_009", True),
        "flotilla/schedule-009": ("schedule-009", False),
        "-talk": ("talk", False),
    }
    assert [t.trial_name for t in keyed.trials] == [t.trial_name for t in arm.trials]
    assert (keyed.name, keyed.jobs) == (arm.name, arm.jobs)


def test_trial_fields(tmp_path):
    job = write_job(tmp_path, "job", [trial("terminal-bench/regex-log", 1.0)], harbor_version="0.26.0")
    arm = load_arm(job)
    (t,) = arm.trials
    assert (t.task, t.task_name) == ("regex-log", "terminal-bench/regex-log")
    assert (t.reward, t.valid, t.exception) == (1.0, True, None)
    assert (t.agent, t.agent_version, t.model, t.environment) == (
        "terminus-2",
        "2.0.0",
        "anthropic/claude-opus-4-6",
        "docker",
    )
    assert (t.tokens_in, t.tokens_cache, t.tokens_out, t.cost_usd) == (1000, 400, 200, 0.05)
    assert t.metric("seconds") == 600.0
    assert t.metric("agent_execution_seconds") == 600.0
    assert t.harbor_version == "0.26.0"
    assert t.conditions == {}
    assert arm.jobs[0].n_total_trials == 1


def test_validity_follows_harbor_for_old_results(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            trial("a", 1.0, exception="AgentTimeoutError"),  # agent fault: the reward counts
            trial("b", 1.0, exception="VerifierTimeoutError"),  # the reward is not trusted
            trial("c", None, exception="EnvironmentStartTimeoutError"),
            trial("d", 0.0, exception="SomeNewError"),  # unknown types are valid
            trial("e", None),
        ],
    )
    valid = {t.task: t.valid for t in load_arm(job).trials}
    assert valid == {"a": True, "b": False, "c": False, "d": True, "e": False}


def test_stored_reward_valid_wins(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            trial("a", 1.0, exception="AgentTimeoutError", reward_valid=False),
            trial("b", 1.0, exception="VerifierTimeoutError", reward_valid=True),
        ],
    )
    assert [t.valid for t in load_arm(job).trials] == [False, True]


def test_exception_policy_stored_on_the_exception(tmp_path):
    result = trial("a", 1.0, exception="AgentTimeoutError")
    result["exception_info"]["policy"] = {"has_valid_reward": False}
    job = write_job(tmp_path, "job", [result])
    assert load_arm(job).trials[0].valid is False


def test_counting_rules(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [trial("a", 1.0), trial("a", None, exception="RuntimeRequestError"), trial("b", None)],
    )
    arm = load_arm(job)
    assert arm.outcomes(counting=Counting.VALID) == {"a": [1.0]}
    assert arm.outcomes(counting=Counting.ALL) == {"a": [1.0, 0.0], "b": [0.0]}
    assert arm.outcomes("cost_usd") == {"a": [0.05, 0.05], "b": [0.05]}


def test_rewards_that_are_not_numbers_are_ignored(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            trial("a", None, rewards={"reward": True}),
            trial("b", None, rewards={"reward": float("nan")}),
            trial("c", None, rewards={"accuracy": 0.5}),
            trial("d", None, rewards={"accuracy": 0.5, "speed": 0.9}),
        ],
    )
    trials = {t.task: t for t in load_arm(job).trials}
    assert trials["a"].reward is None and not trials["a"].valid
    assert trials["b"].reward is None
    assert trials["c"].reward == 0.5  # a single reward is the primary one
    assert trials["d"].reward is None  # several rewards and none is named "reward"
    assert load_arm(job, reward_key="speed").trials[3].reward == 0.9


def test_legacy_single_reward_number():
    result = trial("a", None)
    result["verifier_result"] = {"reward": 1}
    parsed = parse_trial(__import__("pathlib").Path("x"), result)
    assert parsed.reward == 1.0 and parsed.valid


def test_multi_step_usage_is_summed(tmp_path):
    result = trial("a", 1.0, tokens=None)
    result["step_results"] = [
        {"step_name": "one", "agent_result": {"n_input_tokens": 10, "n_output_tokens": 1, "cost_usd": 0.1}},
        {"step_name": "two", "agent_result": {"n_input_tokens": 5, "n_output_tokens": 2, "cost_usd": 0.2}},
    ]
    t = load_arm(write_job(tmp_path, "job", [result])).trials[0]
    assert (t.tokens_in, t.tokens_cache, t.tokens_out) == (15, None, 3)
    assert abs(t.cost_usd - 0.3) < 1e-12


def test_step_exception_spoils_the_reward(tmp_path):
    result = trial("a", 1.0)
    result["step_results"] = [{"step_name": "one", "exception_info": {"exception_type": "ApiError"}}]
    t = load_arm(write_job(tmp_path, "job", [result])).trials[0]
    assert t.exception == "ApiError" and not t.valid


def test_non_default_limits_are_recorded(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [trial("a", 1.0, config={"agent_timeout_multiplier": 9.0, "agent.kwargs": {"effort": "high"}})],
    )
    conditions = load_arm(job).trials[0].conditions
    assert conditions == {"agent_timeout_multiplier": 9.0, "agent.kwargs": '{"effort": "high"}'}


def test_unreadable_and_unfinished_trials_become_problems(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0)])
    broken = job / "b__0001"
    broken.mkdir()
    (broken / "result.json").write_text("{not json")
    unfinished = job / "c__0002"
    unfinished.mkdir()
    (unfinished / "config.json").write_text(json.dumps({"trial_name": "c__0002", "task": {"path": "c"}}))
    arm = load_arm(job)
    assert len(arm.trials) == 1
    assert sorted(p.reason.split(":")[0] for p in arm.problems) == ["trial has no result.json", "unreadable"]


def test_walk_does_not_enter_trial_directories(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0)])
    nested = job / "a__0000" / "agent" / "copy"
    nested.mkdir(parents=True)
    (nested / "result.json").write_text(json.dumps(trial("z", 1.0) | {"trial_name": "z"}))
    assert [t.task for t in load_arm(job).trials] == ["a"]


def test_arm_names_and_split(tmp_path):
    write_job(tmp_path / "runs", "base", [trial("a", 1.0)])
    write_job(tmp_path / "runs", "cand", [trial("a", 0.0)])
    (tmp_path / "runs" / "empty").mkdir()
    assert [a.name for a in split_arms(tmp_path / "runs")] == ["base", "cand"]
    named = load_arms([f"old={tmp_path / 'runs' / 'base'}", str(tmp_path / "runs" / "cand")])
    assert [a.name for a in named] == ["old", "cand"]
    write_job(tmp_path / "other", "base", [trial("a", 1.0)])
    same = load_arms([str(tmp_path / "runs" / "base"), str(tmp_path / "other" / "base")])
    assert same[0].name != same[1].name


def test_a_number_too_large_for_a_float_is_not_a_reward(tmp_path):
    result = trial("a", None, rewards={"reward": 10**400})
    result["agent_result"]["n_input_tokens"] = 10**400
    t = load_arm(write_job(tmp_path, "job", [result])).trials[0]
    assert t.reward is None and t.tokens_in is None and not t.valid
