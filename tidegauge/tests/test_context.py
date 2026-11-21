import argparse
import json

import pytest

from tidegauge.commands import context
from tidegauge.load import load_arm

from harbor_fixture import trial, write_job

LFS_POINTER = "version https://git-lfs.github.com/spec/v1\noid sha256:4d7a2146\nsize 89374\n"


def call(prompt, cached=None, output=None, cost=None, at=None):
    """One model call as an ATIF agent step. ``at`` is the time of the call in seconds after 16:00."""
    numbers = {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": output,
        "cost_usd": cost,
    }
    metrics = {key: value for key, value in numbers.items() if value is not None}
    step = {"source": "agent", "message": "ok", "metrics": metrics}
    if at is not None:
        step["timestamp"] = f"2026-02-05T16:{at // 60:02d}:{at % 60:02d}Z"
    return step


def trajectory(job, name, calls, **root):
    """Write ``agent/trajectory.json`` in the trial directory ``name``. ``calls`` can be the file text."""
    path = job / name / "agent" / "trajectory.json"
    path.parent.mkdir()
    if isinstance(calls, str):
        path.write_text(calls)
        return
    steps = [{"source": "user", "message": "do the task"}, *calls]
    steps = [{"step_id": i, **step} for i, step in enumerate(steps, start=1)]
    path.write_text(json.dumps({"schema_version": "ATIF-v1.6", "steps": steps, **root}))


def report_of(job):
    report = context.build([load_arm(job)])
    return report["arms"][0], report["findings"]


def run(capsys, *argv):
    """Run the command as the command line does, with a parser that this test builds."""
    parser = argparse.ArgumentParser()
    context.configure(parser)
    status = context.run(parser.parse_args(list(argv)))
    return status, capsys.readouterr().out


# 4 calls. Call 3 is a context drop: 1200 < 0.5 * 3000. Only calls 2 and 4 report cached tokens.
#   new tokens:     1000, 3000 - 1000 = 2000, 0 (the prompt shrinks), 2000 - 1200 = 800 -> 3800
#   re-sent tokens: 7200 - 3800 = 3400
FOUR = [
    call(1000, output=100, cost=0.01, at=0),
    call(3000, cached=900, output=200, cost=0.02, at=10),
    call(1200, output=50, cost=0.03, at=40),
    call(2000, cached=1100, cost=0.04, at=100),
]


@pytest.fixture
def mixed(tmp_path):
    """7 trials: 3 with usable calls, then no file, a Git LFS pointer, cut JSON, and ATIF without metrics."""
    job = write_job(
        tmp_path,
        "mixed",
        [
            trial("a", 1.0, tokens=(7200, 2000, 350)),
            trial("b", 0.0, tokens=(2010, 0, 0)),
            trial("c", 1.0, tokens=None),
            trial("d", 1.0),
            trial("e", 0.0),
            trial("f", 0.0),
            trial("g", 1.0),
        ],
    )
    trajectory(job, "a__0000", FOUR, final_metrics={"total_prompt_tokens": 7200})
    two = [call(500, cost=0.2, at=0), call(1500, cost=0.3, at=20)]
    trajectory(job, "b__0001", two, final_metrics={"total_prompt_tokens": 2100})
    trajectory(job, "c__0002", [call(4000, cost=0.4)])
    trajectory(job, "e__0004", LFS_POINTER)
    trajectory(job, "f__0005", '{"schema_version": "ATIF-v1.6", "steps": [')
    trajectory(job, "g__0006", [{"source": "agent", "message": "no metrics"}])
    return job


def test_trial_numbers_on_4_calls(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0, tokens=(7200, 2000, 350))])
    trajectory(job, "a__0000", FOUR)
    arm, findings = report_of(job)
    assert arm["per_trial"] == [
        {
            "trial": "a__0000",
            "task": "a",
            "passed": True,
            "trajectory": "usable",
            "calls": 4,
            "input_tokens": 7200,  # 1000 + 3000 + 1200 + 2000
            "output_tokens": 350,  # 100 + 200 + 50; call 4 reports none
            "first_context": 1000,
            "peak_context": 3000,
            "fresh_tokens": 3800,
            "resent_share": pytest.approx(3400 / 7200),
            "cached_tokens": 2000,  # 900 + 1100
            "cached_share": pytest.approx(2000 / 7200),
            "context_drops": 1,
            "cost_usd": pytest.approx(0.10),
            "seconds_per_call": 30.0,  # the gaps are 10, 30 and 60 seconds
        }
    ]
    assert findings == []


def test_a_context_drop_is_a_prompt_below_half_of_the_one_before(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0)])
    # 500 is exactly half of 1000: not a drop. 249 is below half of 500: a drop.
    trajectory(job, "a__0000", [call(1000), call(500), call(249)])
    (row,) = report_of(job)[0]["per_trial"]
    assert (row["context_drops"], row["fresh_tokens"], row["input_tokens"]) == (1, 1000, 1749)
    assert row["resent_share"] == pytest.approx(749 / 1749)
    assert (row["cached_tokens"], row["cached_share"]) == (None, None)
    assert (row["output_tokens"], row["seconds_per_call"]) == (None, None)


def test_calls_with_0_prompt_tokens_are_not_usable(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0, tokens=(0, 0, 0))])
    zeros = [call(0, output=0), call(0, output=0)]
    trajectory(job, "a__0000", zeros, final_metrics={"total_prompt_tokens": 0})
    arm, findings = report_of(job)
    assert (arm["usable"], arm["input_tokens_total"], arm["peak_context_median"]) == (0, None, None)
    assert arm["per_trial"][0]["trajectory"] == "no-call-metrics"
    assert [f["code"] for f in findings] == ["no-call-metrics"]


def test_cost_comes_from_the_calls_then_the_trajectory_then_the_result(tmp_path):
    costs = {"a": 9.0, "b": 9.0, "c": 0.05, "d": None, "e": 0.0}  # the cost in result.json
    job = write_job(tmp_path, "job", [trial(task, 1.0, cost=cost) for task, cost in costs.items()])
    both = [call(100, cost=0.2), call(200, cost=0.3)]
    trajectory(job, "a__0000", both, final_metrics={"total_cost_usd": 8.0})
    trajectory(job, "b__0001", [call(100, cost=0.2), call(200)], final_metrics={"total_cost_usd": 0.7})
    trajectory(job, "c__0002", [call(100, cost=0.2), call(200)])
    trajectory(job, "d__0003", [call(100), call(200)])
    # A cost of 0 is not a cost: the adapter did not know the price.
    zeros = [call(100, cost=0), call(200, cost=0)]
    trajectory(job, "e__0004", zeros, final_metrics={"total_cost_usd": 0})
    arm, _ = report_of(job)
    assert [row["cost_usd"] for row in arm["per_trial"]] == [pytest.approx(0.5), 0.7, 0.05, None, None]
    assert arm["trials_with_cost"] == 3
    assert arm["cost_median"] == pytest.approx(0.5)
    # (0.5 + 0.7 + 0.05) over the 3 passed trials that have a cost
    assert arm["cost_per_pass"] == pytest.approx(1.25 / 3)


def test_cost_per_pass_counts_valid_rewards_above_0(tmp_path):
    job = write_job(
        tmp_path,
        "job",
        [
            trial("a", 1.0),
            trial("b", 1.0, exception="VerifierTimeoutError"),  # Harbor does not trust this reward
            trial("c", 0.0),
            trial("d", 1.0, cost=None),  # no cost anywhere: not in the sum and not in the count
            trial("e", 0.5),
        ],
    )
    call_costs = {"a__0000": 0.2, "b__0001": 0.3, "c__0002": 0.4, "d__0003": None, "e__0004": 0.1}
    for name, cost in call_costs.items():
        trajectory(job, name, [call(100, cost=cost)])
    arm, _ = report_of(job)
    assert [row["passed"] for row in arm["per_trial"]] == [True, False, False, True, True]
    assert arm["cost_per_pass"] == pytest.approx(0.5)  # (0.2 + 0.3 + 0.4 + 0.1) / 2
    assert arm["cost_median"] == pytest.approx(0.25)
    failed = write_job(tmp_path, "failed", [trial("a", 0.0)])
    trajectory(failed, "a__0000", [call(100, cost=0.2)])
    arm, _ = report_of(failed)
    assert (arm["cost_median"], arm["cost_per_pass"]) == (0.2, None)


def test_seconds_per_call_is_the_median_gap(tmp_path):
    job = write_job(tmp_path, "job", [trial(task, 1.0) for task in "abcd"])
    trajectory(job, "a__0000", [call(100), call(200)])  # no times
    trajectory(job, "b__0001", [call(100, at=0)])  # 1 call has no gap
    trajectory(job, "c__0002", [call(100, at=0), call(200, at=5), call(300)])  # 1 gap: 5
    trajectory(job, "d__0003", [call(100, at=0), call(200, at=4), call(300, at=14)])  # gaps 4 and 10
    arm, _ = report_of(job)
    assert [row["seconds_per_call"] for row in arm["per_trial"]] == [None, None, 5.0, 7.0]
    assert arm["seconds_per_call_median"] == 6.0


def test_p90_is_defined_for_every_sample_size():
    assert context.p90([]) is None
    assert context.p90([7]) == 7.0
    assert context.p90([10, 20]) == pytest.approx(19.0)  # 0.9 of the way from 10 to 20
    assert context.p90([4, 1, 3, 2]) == pytest.approx(3.7)  # position 0.9 * 3 = 2.7 in 1, 2, 3, 4
    assert context.p90(range(1, 12)) == pytest.approx(10.0)  # position 0.9 * 10 = 9 in 1 ... 11
    assert context.p90([5, 5, 5]) == 5.0


def test_arm_numbers_pool_the_usable_trials(mixed):
    arm, _ = report_of(mixed)
    rows = arm.pop("per_trial")
    states = ["usable", "usable", "usable", "missing", "unreadable", "unreadable", "no-call-metrics"]
    assert [row["trajectory"] for row in rows] == states
    # usable trials: a has 7200 input, 3800 new, 2000 cached. b has 2000 input, 1500 new. c has 4000, all new.
    assert arm == {
        "name": "mixed",
        "trials": 7,
        "usable": 3,
        "calls_median": 2,  # 4, 2, 1
        "input_tokens_median": 4000,
        "input_tokens_total": 13200,
        "peak_context_median": 3000,  # 3000, 1500, 4000
        "peak_context_p90": pytest.approx(3800.0),  # position 0.9 * 2 = 1.8 in 1500, 3000, 4000
        "resent_share": pytest.approx(3900 / 13200),  # 1 - 9300 / 13200
        "cached_share": pytest.approx(2000 / 13200),
        "trials_with_drop": 1,
        "trials_with_cost": 3,
        "cost_median": pytest.approx(0.4),  # 0.1, 0.5, 0.4
        "cost_per_pass": pytest.approx(0.5),  # 1.0 over the 2 passed trials a and c
        "seconds_per_call_median": 25.0,  # 30 and 20; c has 1 call
    }
    assert (rows[1]["resent_share"], rows[1]["cached_share"]) == (pytest.approx(500 / 2000), None)
    assert (rows[2]["resent_share"], rows[2]["cached_share"]) == (0.0, None)
    assert rows[3] == {"trial": "d__0003", "task": "d", "passed": True, "trajectory": "missing"}


def test_findings_count_what_is_absent_and_what_does_not_add_up(mixed):
    _, findings = report_of(mixed)
    assert [(f["code"], f["severity"], f["arm"]) for f in findings] == [
        ("trajectory-missing", "note", "mixed"),
        ("trajectory-unreadable", "warn", "mixed"),
        ("no-call-metrics", "note", "mixed"),
        ("usage-mismatch", "warn", "mixed"),
    ]
    missing, unreadable, no_metrics, mismatch = findings
    assert missing["detail"] == {"count": 1}
    assert missing["message"].startswith("1 of 7 trials have no agent/trajectory.json. ")
    assert unreadable["detail"]["count"] == 2
    assert unreadable["detail"]["reasons"] == {"Git LFS pointer": 1, "unreadable: JSONDecodeError": 1}
    assert [path.split("/")[-3] for path in unreadable["detail"]["paths"]] == ["e__0004", "f__0005"]
    assert no_metrics["detail"] == {"count": 1}
    # a: 7200 against 7200 in both totals.
    # b: 2000 against 2100 in final_metrics (4.8% off) and against 2010 in result.json (0.5% off).
    assert mismatch["detail"] == {
        "vs_final_metrics": 1,
        "with_final_metrics": 2,
        "calls_share_of_final_metrics": pytest.approx(9200 / 9300),
        "vs_result": 0,
        "with_result": 2,
        "calls_share_of_result": pytest.approx(9200 / 9210),
    }
    assert "In 1 of 2 trials" in mismatch["message"] and "In 0 of 2 trials" in mismatch["message"]
    assert "The calls hold 98.9% and 99.9% of those totals." in mismatch["message"]


def test_usage_mismatch_starts_above_1_percent(tmp_path):
    results = {"x": 1000, "y": 0, "z": 1000}  # input tokens in result.json
    job = write_job(tmp_path, "job", [trial(task, 1.0, tokens=(n, 0, 0)) for task, n in results.items()])
    trajectory(job, "x__0000", [call(990)], final_metrics={"total_prompt_tokens": 1000})  # 1.0% below
    trajectory(job, "y__0001", [call(989)], final_metrics={"total_prompt_tokens": 1000})  # 1.1% below
    trajectory(job, "z__0002", [call(400), call(611)], final_metrics={"total_prompt_tokens": 0})  # 1.1% above
    (finding,) = report_of(job)[1]
    # A total of 0 is not a total: y is not compared with result.json, and z not with final_metrics.
    # final_metrics: x and y, (990 + 989) / 2000. result.json: x and z, (990 + 1011) / 2000.
    assert finding["code"] == "usage-mismatch"
    assert finding["detail"] == {
        "vs_final_metrics": 1,
        "with_final_metrics": 2,
        "calls_share_of_final_metrics": pytest.approx(1979 / 2000),
        "vs_result": 1,
        "with_result": 2,
        "calls_share_of_result": pytest.approx(2001 / 2000),
    }
    exact = write_job(tmp_path, "exact", [trial("x", 1.0, tokens=(1000, 0, 0))])
    trajectory(exact, "x__0000", [call(990)], final_metrics={"total_prompt_tokens": 1000})
    assert report_of(exact)[1] == []


def test_cached_tokens_above_prompt_tokens_are_a_finding(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0, tokens=None), trial("b", 1.0, tokens=None)])
    trajectory(job, "a__0000", [call(500, cached=600), call(700, cached=700), call(800, cached=900)])
    trajectory(job, "b__0001", [call(500), call(700, cached=500)])
    arm, (finding,) = report_of(job)
    assert (finding["code"], finding["severity"]) == ("cached-exceeds-input", "warn")
    assert finding["detail"] == {"calls": 2, "trials": 1}
    # The share stays as reported: 2700 cached over 3200 input tokens. The finding says that it is not reliable.
    assert arm["cached_share"] == pytest.approx(2700 / 3200)


def test_json_has_1_row_per_trial(capsys, mixed):
    status, out = run(capsys, str(mixed), "--json")
    report = json.loads(out)
    assert status == 0
    assert list(report) == ["command", "arms", "findings"] and report["command"] == "context"
    (arm,) = report["arms"]
    assert list(arm) == [
        "name",
        "trials",
        "usable",
        "calls_median",
        "input_tokens_median",
        "input_tokens_total",
        "peak_context_median",
        "peak_context_p90",
        "resent_share",
        "cached_share",
        "trials_with_drop",
        "trials_with_cost",
        "cost_median",
        "cost_per_pass",
        "seconds_per_call_median",
        "per_trial",
    ]
    assert [row["trial"] for row in arm["per_trial"]] == [
        f"{task}__000{i}" for i, task in enumerate("abcdefg")
    ]
    assert arm["per_trial"][0]["input_tokens"] == 7200 and len(arm["per_trial"][0]) == 16
    assert all(set(row) == {"trial", "task", "passed", "trajectory"} for row in arm["per_trial"][3:])
    assert all(set(f) == {"code", "severity", "arm", "message", "detail"} for f in report["findings"])


def test_text_has_the_table_the_legend_and_the_findings(capsys, mixed):
    status, out = run(capsys, f"{'x' * 50}={mixed}")
    lines = out.splitlines()
    assert status == 0
    header = "arm trials usable calls input peak re-sent cached drops $/trial $/pass s/call"
    assert lines[0].split() == header.split()
    # 3900 / 13200 = 29.5% re-sent, 2000 / 13200 = 15.2% cached
    row = "7 3 2.0 4.0k 3.0k 29.5% 15.2% 1 $0.40 $0.50 25.0"
    assert lines[2].split() == ["x" * 43 + "…", *row.split()]
    # the legend has 1 line per column group, then the line that says where the token counts come from
    end = 4 + len(context.LEGEND)
    assert lines[3] == "" and lines[4:end] == list(context.LEGEND)
    assert [line.split()[0] for line in lines[4 : end - 1]] == [
        "usable",
        "calls",
        "input",
        "peak",
        "re-sent",
        "cached",
        "drops",
        "$/trial",
        "s/call",
    ]
    assert "the adapter of each agent reports" in lines[end - 1]
    assert all(len(line) <= 110 for line in context.LEGEND)
    assert lines[end : end + 2] == ["", "accounting:"]
    assert [line.split()[:2] for line in lines[end + 2 :]] == [
        ["note", "trajectory-missing:"],
        ["warn", "trajectory-unreadable:"],
        ["note", "no-call-metrics:"],
        ["warn", "usage-mismatch:"],
    ]


def test_an_arm_without_trajectories_shows_no_number(tmp_path):
    job = write_job(tmp_path, "bare", [trial("a", 1.0), trial("b", 0.0)])
    report = context.build([load_arm(job)])
    (arm,) = report["arms"]
    assert (arm["trials"], arm["usable"], arm["trials_with_cost"]) == (2, 0, 0)
    unknown = set(arm) - {"name", "trials", "usable", "trials_with_cost", "per_trial"}
    assert len(unknown) == 11 and all(arm[key] is None for key in unknown)
    assert context.text(report).splitlines()[2].split() == ["bare", "2", "0", *["n/a"] * 9]
    assert [f["code"] for f in report["findings"]] == ["trajectory-missing"]


def test_token_counts_are_short_in_the_table(tmp_path):
    arms = []
    for name, prompt in (("small", 950), ("large", 2_540_000), ("huge", 3_000_000_000)):
        job = write_job(tmp_path, name, [trial("a", 0.0, tokens=None)])
        trajectory(job, "a__0000", [call(prompt)])
        arms.append(load_arm(job))
    lines = context.text(context.build(arms)).splitlines()
    # 1 trial with 1 call: input and peak are the same number, and no token is re-sent
    assert lines[2].split() == "small 1 1 1.0 950 950 0.0% n/a 0 n/a n/a n/a".split()
    assert lines[3].split()[4:6] == ["2.5M"] * 2
    assert lines[4].split()[4:6] == ["3.0G"] * 2


def test_split_named_arms_and_reward_key(capsys, tmp_path):
    for name, last in (("base", 300), ("cand", 500)):
        job = write_job(tmp_path / "runs", name, [trial("a", None, rewards={"accuracy": 1.0, "speed": 0.0})])
        trajectory(job, "a__0000", [call(100, cost=0.25), call(last, cost=0.25)])
    _, out = run(capsys, "--split", str(tmp_path / "runs"), "--json")
    arms = json.loads(out)["arms"]
    assert [(a["name"], a["input_tokens_total"]) for a in arms] == [("base", 400), ("cand", 600)]
    # 2 rewards and none is named "reward": no trial passed until --reward-key names one.
    assert [a["cost_per_pass"] for a in arms] == [None, None]
    _, out = run(capsys, f"old={tmp_path / 'runs' / 'base'}", "--reward-key", "accuracy", "--json")
    (arm,) = json.loads(out)["arms"]
    assert (arm["name"], arm["cost_per_pass"]) == ("old", 0.5)
    with pytest.raises(SystemExit):
        run(capsys, str(tmp_path / "runs" / "base"), str(tmp_path / "runs" / "cand"), "--split")


def test_cached_tokens_counted_twice_are_a_finding(tmp_path):
    job = write_job(tmp_path, "job", [trial("a", 1.0, tokens=None), trial("b", 1.0, tokens=None)])
    # True prompts 1000, 1500, 2100 with a full cache hit on the prompt before. The adapter reports prompt + cached.
    doubled = [
        call(1000),
        call(1500 + 1000, cached=1000),
        call(2100 + 1500, cached=1500),
        call(2500 + 2100, cached=2100),
    ]
    trajectory(job, "a__0000", doubled)
    trajectory(job, "b__0001", doubled)
    _, findings = report_of(job)
    (finding,) = [f for f in findings if f["code"] == "cached-counted-twice"]
    # pairs with cached tokens on both calls: (2, 3) and (3, 4) in each trial. 2500 - 1000 = 1500 and 3600 - 1500 = 2100.
    assert finding["detail"] == {"pairs": 4, "matching": 4}
    # A correct adapter: the prompt holds the cached tokens once.
    correct = [call(1000), call(1500, cached=1000), call(2100, cached=1500), call(2500, cached=2100)]
    clean = write_job(tmp_path, "clean", [trial("a", 1.0, tokens=None)])
    trajectory(clean, "a__0000", correct)
    assert [f["code"] for f in report_of(clean)[1]] == []
