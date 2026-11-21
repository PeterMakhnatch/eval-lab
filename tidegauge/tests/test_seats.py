"""Seat trials: the olamlabs/harbor-multiagent fork runs several agents in 1 trial, 1 per seat."""

import dataclasses
import json

import pytest

from tidegauge.audit import audit_arm
from tidegauge.cli import main
from tidegauge.commands import context
from tidegauge.load import load_arm

from harbor_fixture import arm, seat, seat_trial, trial, write_job

# A seat without a model, as Harbor's oracle agent writes it: every usage value is null.
ORACLE = {"agent": "oracle", "version": "1.0.0", "model": None, "tokens": (None, None, None), "cost": None}
FLASH = "deepseek/deepseek-flash"


def codes(findings):
    return sorted(f.code for f in findings)


def run(capsys, *argv):
    status = main(list(argv))
    return status, capsys.readouterr().out


def test_a_seat_trial_reads_as_its_roster(tmp_path):
    # The shape of a real trial of the fork: 2 oracle seats, only p1 under test, no usage, reward 1.
    seats = [seat("p1", **ORACLE), seat("p2", **ORACLE, under_test=False)]
    job = write_job(tmp_path, "smoke", [seat_trial("harbor/hello-seats", 1.0, seats)])
    (t,) = load_arm(job).trials
    assert (t.task, t.reward, t.valid, t.exception) == ("hello-seats", 1.0, True, None)
    assert (t.agent, t.agent_version, t.model) == ("p1=oracle,p2=oracle", "p1=1.0.0,p2=1.0.0", "p1=-,p2=-")
    assert (t.seats, t.model_seats, t.seat_exceptions, t.partial_usage) == (("p1", "p2"), (), {}, ())
    assert (t.tokens_in, t.tokens_cache, t.tokens_out, t.cost_usd) == (None, None, None, None)
    assert t.metric("agent_execution_seconds") == 600.0


def test_a_trial_with_1_agent_reads_as_before(tmp_path):
    plain = trial("a", 1.0)
    fork = trial("a", 1.0)  # the fork writes these 2 fields for a trial without seats
    fork["seat_results"] = None
    fork["config"]["participants"] = []
    first, second = load_arm(write_job(tmp_path, "job", [plain, fork])).trials
    assert dataclasses.replace(second, path=first.path, trial_name=first.trial_name) == first
    assert (first.agent, first.model, first.seats, first.model_seats) == (
        "terminus-2",
        "anthropic/claude-opus-4-6",
        (),
        (),
    )


def test_one_roster_is_one_arm_identity(tmp_path):
    team = [seat("p1"), seat("p2", **ORACLE)]
    same = load_arm(arm(tmp_path, "same", {"a": [1.0, 0.0], "b": [1.0, 1.0]}, seats=team))
    assert {(t.agent, t.model) for t in same.trials} == {("p1=terminus-2,p2=oracle", f"p1={FLASH},p2=-")}
    assert "mixed-arm" not in codes(audit_arm(same))
    other = [seat("p1"), seat("p2", model="openai/gpt-5.5")]
    mixed = write_job(
        tmp_path,
        "mixed",
        [seat_trial("a", 1.0, team), seat_trial("a", 1.0, other, finished_at="2026-02-05T16:11:00Z")],
    )
    found = {
        f.detail["field"]: f.detail["values"] for f in audit_arm(load_arm(mixed)) if f.code == "mixed-arm"
    }
    assert found == {
        "agent": {"p1=terminus-2,p2=oracle": 1, "p1=terminus-2,p2=terminus-2": 1},
        "agent version": {"p1=2.0.0,p2=1.0.0": 1, "p1=2.0.0,p2=2.0.0": 1},
        "model": {f"p1={FLASH},p2=-": 1, f"p1={FLASH},p2=openai/gpt-5.5": 1},
    }


def test_agent_versions_that_no_seat_knows_read_as_unknown(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            seat_trial("a", 1.0, [seat("p1", version="unknown"), seat("p2", version="unknown")]),
            seat_trial("b", 1.0, [seat("p1", version="unknown"), seat("p2")]),
        ],
    )
    assert [t.agent_version for t in load_arm(job).trials] == ["unknown", "p1=unknown,p2=2.0.0"]


def test_usage_is_the_sum_over_the_seats_when_every_model_seat_reports(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            # Both seats report: 1000 + 3000, 400 + 0, 200 + 50, 0.05 + 0.25.
            seat_trial("a", 1.0, [seat("p1"), seat("p2", tokens=(3000, 0, 50), cost=0.25)]),
            # The oracle seat used no model: the usage of the team is that of p1.
            seat_trial("b", 1.0, [seat("p1"), seat("p2", **ORACLE)]),
            # p2 used a model and reports no cost: the team has no cost. Its tokens are complete.
            seat_trial("c", 1.0, [seat("p1"), seat("p2", cost=None)]),
            # p2 names a model and reports nothing.
            seat_trial("d", 1.0, [seat("p1"), seat("p2", tokens=(None, None, None), cost=None)]),
            # p2 names no model and reports tokens, so it used a model. It gives no cached tokens and no cost.
            seat_trial("e", 1.0, [seat("p1"), seat("p2", model=None, tokens=(500, None, 20), cost=None)]),
        ],
    )
    loaded = load_arm(job)
    usage = {
        t.task: (t.tokens_in, t.tokens_cache, t.tokens_out, t.cost_usd, t.partial_usage, t.model_seats)
        for t in loaded.trials
    }
    every = ("tokens_in", "tokens_cache", "tokens_out", "cost_usd")
    assert usage["a"] == (4000, 400, 250, pytest.approx(0.30), (), ("p1", "p2"))
    assert usage["b"] == (1000, 400, 200, 0.05, (), ("p1",))
    assert usage["c"] == (2000, 800, 400, None, ("cost_usd",), ("p1", "p2"))
    assert usage["d"] == (None, None, None, None, every, ("p1", "p2"))
    assert usage["e"] == (1500, None, 220, None, ("tokens_cache", "cost_usd"), ("p1", "p2"))
    # A trial without the cost of its team is left out. It does not count with the cost of 1 seat.
    assert loaded.outcomes("cost_usd") == {"a": [pytest.approx(0.30)], "b": [0.05]}
    (finding,) = [f for f in audit_arm(loaded) if f.code == "missing-usage"]
    assert finding.detail == {"no_tokens": 1, "no_cost": 3, "partial": 3}
    assert (
        "In 3 of 5 (60.0%) trials only a part of the seats that used a model report a value."
        in finding.message
    )


def test_missing_usage_of_a_trial_with_1_agent_is_worded_as_before(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0, tokens=None)])
    (finding,) = [f for f in audit_arm(load_arm(job)) if f.code == "missing-usage"]
    assert finding.detail == {"no_tokens": 1, "no_cost": 1}
    assert finding.message.endswith("Cost and token figures cover only the rest.")


def test_a_seat_exception_follows_harbors_reward_rule(tmp_path):
    timeout = [
        seat("p1"),
        seat("p2", exception="AgentTimeoutError"),
    ]  # a fault of the agent: the reward counts
    api = [seat("p1"), seat("p2", exception="ApiError")]  # Harbor does not trust the reward
    job = write_job(
        tmp_path,
        "job",
        [
            seat_trial("a", 1.0, timeout),
            seat_trial("b", 1.0, api),
            # Without a stored reward_valid, tidegauge rebuilds it from the trial and the seat exceptions.
            seat_trial("c", 1.0, timeout, modern=False),
            seat_trial("d", 1.0, api, modern=False),
            # The trial itself records a fault of the agent. Only the seat exception spoils the reward.
            seat_trial("e", 1.0, api, modern=False, exception="AgentTimeoutError"),
            seat_trial("f", None, timeout),
            seat_trial("g", 0.0, [seat("p1"), seat("p2")]),
        ],
    )
    loaded = load_arm(job)
    trials = {t.task: t for t in loaded.trials}
    assert {task: t.valid for task, t in trials.items()} == {
        "a": True,
        "b": False,
        "c": True,
        "d": False,
        "e": False,
        "f": False,
        "g": True,
    }
    # Harbor copies the first seat exception to the trial. The seat keeps its own.
    assert (trials["a"].exception, trials["a"].seat_exceptions) == (
        "AgentTimeoutError",
        {"p2": "AgentTimeoutError"},
    )
    assert (trials["e"].exception, trials["e"].seat_exceptions) == ("AgentTimeoutError", {"p2": "ApiError"})
    assert trials["g"].seat_exceptions == {}
    (finding,) = [f for f in audit_arm(loaded) if f.code == "seat-errors"]
    # a to e have a reward and a seat exception. f has no reward. a and c have a valid reward.
    assert finding.severity == "note"
    assert finding.detail == {
        "count": 5,
        "valid": 2,
        "by_seat": {"p2 AgentTimeoutError": 2, "p2 ApiError": 3},
    }
    assert finding.message.startswith(
        "5 of 7 (71.4%) trials have a reward, and a seat in them raised an exception"
    )
    clean = load_arm(arm(tmp_path, "clean", {"a": [1.0, 0.0]}, seats=[seat("p1"), seat("p2")]))
    assert "seat-errors" not in codes(audit_arm(clean))


def test_a_roster_of_control_agents_inside_real_attempts_is_an_error(tmp_path):
    controls = [seat("p1", **ORACLE), seat("p2", **ORACLE)]
    real = [seat("p1"), seat("p2", **ORACLE)]  # a real agent with a scripted partner is a real attempt
    job = write_job(
        tmp_path,
        "job",
        [seat_trial("a", 0.0, real), seat_trial("a", 1.0, controls, finished_at="2026-02-05T16:11:00Z")],
    )
    (finding,) = [f for f in audit_arm(load_arm(job)) if f.code == "control-agent-trials"]
    assert (finding.severity, finding.detail["agents"], finding.detail["passed"]) == (
        "error",
        {"p1=oracle,p2=oracle": 1},
        1,
    )
    only = load_arm(write_job(tmp_path, "only", [seat_trial("a", 1.0, controls)]))
    assert "control-agent-trials" not in codes(audit_arm(only))


def test_the_arguments_of_each_seat_are_a_condition(tmp_path):
    seats = [seat("p1", kwargs={"effort": "high"}), seat("p2")]
    job = write_job(tmp_path, "job", [seat_trial("a", 1.0, seats)])
    assert load_arm(job).trials[0].conditions == {"agent.kwargs": '{"p1": {"effort": "high"}}'}
    # config.agent never ran in a seat trial: its arguments are not a condition, also when no seat has any.
    plain = seat_trial("a", 1.0, [seat("p1"), seat("p2")])
    plain["config"]["agent"]["kwargs"] = {"effort": "low"}
    assert load_arm(write_job(tmp_path, "plain", [plain])).trials[0].conditions == {}


# --------------------------------------------------------------------------- #
# context: 1 trajectory per seat
# --------------------------------------------------------------------------- #

# p1: 3 calls. Call 3 is a context drop: 1200 < 0.5 * 3000. New tokens: 1000 + 2000 + 0 = 3000.
P1 = [
    {"prompt_tokens": 1000, "completion_tokens": 100, "cost_usd": 0.01, "at": 0},
    {"prompt_tokens": 3000, "cached_tokens": 900, "cost_usd": 0.02, "at": 10},
    {"prompt_tokens": 1200, "cost_usd": 0.03, "at": 40},
]
# p2: 2 calls. New tokens: 500 + 4500 = 5000. Its first prompt is below half of the last prompt of p1:
# that is not a drop, because a pair of calls is 2 calls of 1 seat.
P2 = [
    {"prompt_tokens": 500, "cost_usd": 0.10, "at": 5},
    {"prompt_tokens": 5000, "cached_tokens": 400, "completion_tokens": 50, "cost_usd": 0.20, "at": 25},
]


@pytest.fixture
def team_job(tmp_path):
    """3 seat trials: both seats have calls, then a model seat without a trajectory, then an oracle seat."""
    p1 = seat("p1", tokens=(5200, 900, 100), calls=P1)
    return write_job(
        tmp_path,
        "team",
        [
            seat_trial("a", 1.0, [p1, seat("p2", tokens=(5500, 400, 50), calls=P2)]),
            seat_trial("b", 1.0, [p1, seat("p2")]),
            seat_trial("c", 0.0, [p1, seat("p2", **ORACLE)]),
        ],
    )


def test_context_adds_up_the_seats_of_a_trial(team_job):
    report = context.build([load_arm(team_job)])
    (team,) = report["arms"]
    both, lacking, with_oracle = team["per_trial"]
    assert both == {
        "trial": "a__0000",
        "task": "a",
        "passed": True,
        "trajectory": "usable",
        "seats": {"p1": "usable", "p2": "usable"},
        "calls": 5,  # 3 + 2
        "input_tokens": 10700,  # 5200 + 5500
        "output_tokens": 150,  # 100 + 50
        "first_context": 1000,  # the largest first prompt: 1000 and 500
        "peak_context": 5000,  # the largest prompt of 1 seat
        "fresh_tokens": 8000,  # 3000 + 5000
        "resent_share": pytest.approx(2700 / 10700),
        "cached_tokens": 1300,  # 900 + 400
        "cached_share": pytest.approx(1300 / 10700),
        "context_drops": 1,
        "cost_usd": pytest.approx(0.36),  # 0.06 + 0.30
        "seconds_per_call": 20.0,  # the gaps inside a seat: 10 and 30 for p1, 20 for p2
    }
    # p2 used a model and has no trajectory: the calls of p1 alone are not the numbers of the trial.
    assert lacking == {
        "trial": "b__0001",
        "task": "b",
        "passed": True,
        "trajectory": "missing",
        "seats": {"p1": "usable", "p2": "missing"},
    }
    # An oracle seat used no model and needs no trajectory: the trial has the numbers of p1.
    assert (with_oracle["trajectory"], with_oracle["seats"]) == ("usable", {"p1": "usable", "p2": "missing"})
    assert (with_oracle["calls"], with_oracle["input_tokens"], with_oracle["peak_context"]) == (3, 5200, 3000)
    assert with_oracle["cost_usd"] == pytest.approx(0.06)
    assert (team["trials"], team["usable"], team["input_tokens_total"]) == (3, 2, 15900)
    assert team["cost_per_pass"] == pytest.approx(0.42)  # (0.36 + 0.06) over the 1 passed trial with a cost
    (finding,) = report["findings"]
    assert (finding["code"], finding["severity"], finding["detail"]) == (
        "trajectory-missing",
        "note",
        {"count": 1},
    )
    assert finding["message"].startswith("1 of 3 trials have no agent/trajectory.json for 1 or more seats. ")
    assert context.text(report).splitlines()[4 + len(context.LEGEND)] == context.SEATS_LEGEND


def test_context_reads_a_roster_without_a_model_as_missing(tmp_path):
    controls = [seat("p1", **ORACLE), seat("p2", **ORACLE)]
    report = context.build([load_arm(write_job(tmp_path, "smoke", [seat_trial("a", 1.0, controls)]))])
    (row,) = report["arms"][0]["per_trial"]
    assert (row["trajectory"], row["seats"]) == ("missing", {"p1": "missing", "p2": "missing"})
    assert [f["code"] for f in report["findings"]] == ["trajectory-missing"]


def test_context_compares_the_calls_of_a_team_with_the_totals_of_the_team(tmp_path):
    # result.json holds 1000 + 1000 input tokens. The calls hold 1000 + 2000: 50% above.
    seats = [
        seat("p1", calls=[{"prompt_tokens": 1000, "cost_usd": 0.01}]),
        seat("p2", calls=[{"prompt_tokens": 2000}]),
    ]
    report = context.build([load_arm(write_job(tmp_path, "team", [seat_trial("a", 1.0, seats)]))])
    (finding,) = report["findings"]
    assert finding["code"] == "usage-mismatch"
    assert (finding["detail"]["vs_result"], finding["detail"]["with_result"]) == (1, 1)
    assert finding["detail"]["calls_share_of_result"] == pytest.approx(1.5)
    # p2 gives no cost in its trajectory, so the cost of the team comes from result.json: 0.05 + 0.05.
    assert report["arms"][0]["per_trial"][0]["cost_usd"] == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# A reward name with a dot: the credit of 1 seat
# --------------------------------------------------------------------------- #


def credit(tmp_path, name, p1_credit):
    """A job of seat trials. The verifier writes the team score and the credit of each seat."""
    seats = [seat("p1"), seat("p2")]
    trials = []
    for task, values in p1_credit.items():
        for value in values:
            stamp = f"2026-02-05T17:00:{len(trials):02d}Z"
            rewards = {"reward": 1.0, "seat.p1": value, "seat.p2": 0.0}
            trials.append(seat_trial(task, None, seats, rewards=rewards, finished_at=stamp))
    # 1 more trial of the last task has no verifier result: it has no reward of any name.
    trials.append(
        seat_trial(task, None, seats, exception="VerifierTimeoutError", finished_at="2026-02-05T18:00:00Z")
    )
    return str(write_job(tmp_path, name, trials))


@pytest.fixture
def credits(tmp_path):
    low = {f"t{i}": [1.0, 0.0, 0.0, 0.0] if i < 4 else [0.0] * 4 for i in range(12)}  # 4 * 0.25 / 12
    high = {f"t{i}": [1.0, 1.0, 1.0, 0.0] if i < 8 else [0.0] * 4 for i in range(12)}  # 8 * 0.75 / 12
    return credit(tmp_path, "low", low), credit(tmp_path, "high", high)


@pytest.mark.parametrize("option", ["--reward-key", "--metric"])
def test_a_reward_name_with_a_dot_works_in_every_command(capsys, credits, option):
    low, high = credits
    status, out = run(capsys, "score", low, high, option, "seat.p1", "--json")
    assert status == 0
    assert [a["score"] for a in json.loads(out)["arms"]] == [pytest.approx(1 / 12), pytest.approx(0.5)]
    status, out = run(capsys, "compare", low, high, option, "seat.p1", "--json")
    report = json.loads(out)
    assert (status, report["difference"]) == (0, pytest.approx(0.5 - 1 / 12))
    assert report["rerun"]["verdict"] == "candidate is higher"
    status, out = run(capsys, "rank", low, high, option, "seat.p1", "--json")
    assert (status, [a["name"] for a in json.loads(out)["arms"]]) == (0, ["high", "low"])
    status, out = run(capsys, "plan", low, high, option, "seat.p1", "--json")
    assert (status, json.loads(out)["tasks"]) == (0, 12)
    assert run(capsys, "gate", low, high, option, "seat.p1", "--require-gain")[0] == 0
    assert run(capsys, "gate", high, low, option, "seat.p1")[0] == 1
    assert "+41.7 pts" in run(capsys, "compare", low, high, option, "seat.p1")[1]


def test_reward_key_with_a_dot_in_audit_and_context(capsys, credits):
    low, _ = credits
    status, out = run(capsys, "audit", low, "--reward-key", "seat.p1", "--json")
    report = json.loads(out)
    assert (status, report["arms"][0]["trials"], report["arms"][0]["valid"]) == (0, 49, 48)
    assert "no-primary-reward" not in {f["code"] for f in report["findings"]}
    status, out = run(capsys, "context", low, "--reward-key", "seat.p1", "--json")
    rows = json.loads(out)["arms"][0]["per_trial"]
    assert (status, sum(row["passed"] for row in rows)) == (0, 4)  # the 4 trials with seat.p1 = 1


def test_a_metric_that_no_trial_has_is_an_error(capsys, credits):
    assert main(["score", credits[0], "--metric", "seat.p9"]) == 3
    assert "unknown metric: seat.p9" in capsys.readouterr().err
