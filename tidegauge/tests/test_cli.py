import json

import pytest

from tidegauge.cli import main, parser
from tidegauge.commands import COMMANDS

from harbor_fixture import arm, trial, write_job


def job(tmp_path, name, outcomes, **kwargs):
    return str(arm(tmp_path, name, outcomes, **kwargs))


@pytest.fixture
def arms(tmp_path):
    weak = {f"t{i:02d}": [1.0, 0.0, 0.0, 0.0] if i < 10 else [0.0] * 4 for i in range(30)}
    strong = {f"t{i:02d}": [1.0, 1.0, 1.0, 0.0] if i < 20 else [0.0] * 4 for i in range(30)}
    return job(tmp_path, "weak", weak), job(tmp_path, "strong", strong)


def run(capsys, *argv):
    status = main(list(argv))
    return status, capsys.readouterr().out


def test_score_json(capsys, arms):
    status, out = run(capsys, "score", *arms, "--json")
    report = json.loads(out)
    assert status == 0
    weak, strong = report["arms"]
    assert (weak["name"], weak["tasks"], weak["trials"]) == ("weak", 30, 120)
    assert weak["score"] == pytest.approx(10 * 0.25 / 30)
    assert strong["score"] == pytest.approx(20 * 0.75 / 30)
    assert weak["ci_rerun"][0] < weak["score"] < weak["ci_rerun"][1]
    assert report["findings"] == []


def test_score_text_names_both_error_bars(capsys, arms):
    status, out = run(capsys, "score", *arms)
    assert status == 0
    assert "rerun" in out and "noise floor" in out and "weak" in out


def test_compare(capsys, arms):
    status, out = run(capsys, "compare", *arms, "--json")
    report = json.loads(out)
    assert status == 0
    assert (report["baseline"], report["candidate"], report["shared_tasks"]) == ("weak", "strong", 30)
    assert report["difference"] == pytest.approx(20 * 0.75 / 30 - 10 * 0.25 / 30)
    assert report["rerun"]["verdict"] == "candidate is higher"
    assert report["rerun"]["test"] == "exact permutation"
    assert report["tasks"]["verdict"] == "candidate is higher"
    assert report["per_task"]["candidate_higher"] == 20
    status, out = run(capsys, "compare", *arms, "--tasks")
    assert "candidate is higher" in out and "t00" in out


def test_compare_identical_arms_is_not_resolved(capsys, arms):
    _, out = run(capsys, "compare", arms[0], f"again={arms[0]}", "--json")
    report = json.loads(out)
    assert report["difference"] == 0
    assert report["rerun"]["verdict"] == "not resolved" and report["rerun"]["p"] == 1.0


def test_rank(capsys, arms):
    status, out = run(capsys, "rank", *arms, "--json")
    report = json.loads(out)
    assert status == 0
    assert [a["name"] for a in report["arms"]] == ["strong", "weak"]
    assert report["arms"][0]["separated_from_next"] is True
    assert report["arms"][0]["rank_interval"] == [1, 1]
    assert (report["neighbor_pairs"], report["neighbor_pairs_separated"]) == (1, 1)


def test_rank_split(capsys, arms, tmp_path):
    status, out = run(capsys, "rank", "--split", str(tmp_path))
    assert status == 0 and "1 of 1 neighbor pairs" in out


def test_plan(capsys, arms):
    status, out = run(capsys, "plan", *arms, "--detect", "0.05", "--json")
    report = json.loads(out)
    assert status == 0
    assert report["tasks"] == 30 and report["attempts_needed"] >= 1
    detectable = [row["detectable_rerun"] for row in report["rows"]]
    assert detectable == sorted(detectable, reverse=True)
    assert report["cost_per_trial"] == pytest.approx(0.05)
    assert report["cost_needed_per_arm"] == pytest.approx(report["attempts_needed"] * 30 * 0.05)
    status, out = run(capsys, "plan", arms[0])
    assert status == 0 and "attempts per task" in out


def test_gate_decisions(capsys, arms):
    weak, strong = arms
    assert run(capsys, "gate", weak, strong)[0] == 0
    assert run(capsys, "gate", weak, strong, "--require-gain")[0] == 0
    assert run(capsys, "gate", strong, weak)[0] == 1
    status, out = run(capsys, "gate", weak, f"again={weak}", "--json")
    assert status == 2 and json.loads(out)["decision"] == "undecided"
    assert run(capsys, "gate", weak, f"again={weak}", "--margin", "0.2")[0] == 0


def priced(tmp_path, name, costs):
    """A job with 10 tasks, where each task has 1 trial per cost in ``costs``."""
    trials = []
    for task in range(10):
        for cost in costs:
            stamp = f"2026-02-05T18:{len(trials) // 60:02d}:{len(trials) % 60:02d}Z"
            trials.append(trial(f"t{task}", 1.0, cost=cost, finished_at=stamp))
    return str(write_job(tmp_path, name, trials))


def test_gate_cost_metric_lower_is_better(capsys, tmp_path):
    cheap = priced(tmp_path, "cheap", [0.009, 0.011])
    costly = priced(tmp_path, "costly", [0.019, 0.021])
    assert run(capsys, "gate", cheap, costly, "--metric", "cost_usd", "--lower-is-better")[0] == 1
    assert run(capsys, "gate", costly, cheap, "--metric", "cost_usd", "--lower-is-better")[0] == 0
    assert (
        run(capsys, "gate", cheap, costly, "--metric", "cost_usd")[0] == 0
    )  # higher cost passes when higher is better


def test_gate_does_not_decide_without_measured_noise(capsys, tmp_path):
    # Every trial of an arm has the same cost: the data shows no noise, so no interval exists.
    cheap = priced(tmp_path, "cheap", [0.01, 0.01])
    costly = priced(tmp_path, "costly", [0.02, 0.02])
    status, out = run(capsys, "gate", cheap, costly, "--metric", "cost_usd", "--lower-is-better", "--json")
    report = json.loads(out)
    assert status == 2 and report["interval"] is None and "not measured" in report["reason"]


def test_gate_needs_the_exact_test_to_agree(capsys, tmp_path):
    # 1 task flips and no task shows trial noise: an interval from the noise alone would be a point.
    flat = {f"t{i:02d}": [1.0, 1.0, 1.0] if i < 20 else [0.0, 0.0, 0.0] for i in range(40)}
    base = job(tmp_path, "base", flat)
    cand = job(tmp_path, "cand", flat | {"t39": [1.0, 1.0, 1.0]})
    assert run(capsys, "gate", base, cand, "--require-gain")[0] == 2
    assert run(capsys, "gate", cand, base)[0] == 2
    _, out = run(capsys, "compare", base, cand, "--json")
    report = json.loads(out)
    assert report["rerun"]["interval"] is None and report["rerun"]["p"] == pytest.approx(0.1)
    assert report["rerun"]["verdict"] == "not resolved"


RULE = "tasks verdict: the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < 0.05"


def test_compare_tasks_verdict_needs_the_sign_flip_test_too(capsys, tmp_path):
    # 12 tasks, 1 trial per task: the candidate passes 4 tasks that the baseline fails, and 8 are equal.
    base = job(tmp_path, "base", {f"t{i:02d}": [0.0] for i in range(12)})
    cand = job(tmp_path, "cand", {f"t{i:02d}": [float(i < 4)] for i in range(12)})
    tasks = json.loads(run(capsys, "compare", base, cand, "--json")[1])["tasks"]
    keys = ["se", "interval", "p", "test", "p_signflip", "signflip_exact", "verdict", "detectable"]
    assert list(tasks) == keys
    # t^2 = 5.5 with 11 degrees of freedom gives p = 0.0388. 4 differences of +1 give the sign-flip p 2/16.
    assert tasks["p"] == pytest.approx(0.0388, abs=5e-5) and tasks["interval"][0] > 0
    assert (tasks["p_signflip"], tasks["signflip_exact"]) == (0.125, True)
    assert (tasks["test"], tasks["verdict"]) == ("paired t and sign-flip over tasks", "not resolved")
    lines = run(capsys, "compare", base, cand)[1].splitlines()
    assert lines[8:10] == [
        "question                  95% interval            p  sign-flip p  verdict",
        "------------------------  -----------------  ------  -----------  ------------",
    ]
    assert lines[11].split()[-4:] == ["0.0388", "0.1250", "not", "resolved"]
    assert RULE in lines and not any("width 0" in line for line in lines)


def test_compare_tasks_interval_of_width_0(capsys, tmp_path):
    # The candidate passes every task and the baseline none: every task gives +1, the tasks interval has
    # width 0, and the t-test gives p = 0. The sign-flip p is 2 / 2^n: 6 tasks resolve it at 0.05, 5 do not.
    for n, verdict in ((5, "not resolved"), (6, "candidate is higher")):
        base = job(tmp_path / str(n), "base", {f"t{i}": [0.0] for i in range(n)})
        cand = job(tmp_path / str(n), "cand", {f"t{i}": [1.0] for i in range(n)})
        tasks = json.loads(run(capsys, "compare", base, cand, "--json")[1])["tasks"]
        assert (tasks["interval"], tasks["p"], tasks["p_signflip"]) == ([1.0, 1.0], 0.0, 2 / 2**n)
        assert tasks["verdict"] == verdict
        width = "the tasks interval has width 0 because every task gives the same difference: more tasks can measure the spread between tasks"
        assert width in run(capsys, "compare", base, cand)[1].splitlines()


def test_gate_on_the_tasks_question_needs_the_sign_flip_test_too(capsys, tmp_path):
    # 12 tasks, 2 trials per task. The candidate gains 1 on 4 tasks, and the other 8 are equal: the 95% t
    # interval is above 0 (t^2 = 5.5, p = 0.0388), but the sign-flip test gives p = 2/16. No gain is claimed.
    base = job(tmp_path, "base", {f"t{i:02d}": [0.0, 0.0] if i < 4 else [1.0, 0.0] for i in range(12)})
    cand = job(tmp_path, "cand", {f"t{i:02d}": [1.0, 1.0] if i < 4 else [0.0, 1.0] for i in range(12)})
    status, out = run(capsys, "gate", base, cand, "--question", "tasks", "--require-gain", "--json")
    report = json.loads(out)
    assert report["interval"][0] > 0
    assert (status, report["decision"]) == (2, "undecided")
    assert report["reason"] == "the test does not agree with the interval: run more trials"
    # Without --require-gain the interval above 0 claims no difference, so no test is needed.
    assert run(capsys, "gate", base, cand, "--question", "tasks")[0] == 0


def test_gate_with_one_trial_per_task_is_undecided_and_not_an_error(capsys, tmp_path):
    # 12 tasks, 1 trial each: the t interval is above 0, the sign-flip test is not below 0.05, and no plan exists.
    base = job(tmp_path, "base", {f"t{i:02d}": [0.0] for i in range(12)})
    cand = job(tmp_path, "cand", {f"t{i:02d}": [1.0] if i < 4 else [0.0] for i in range(12)})
    status, out = run(capsys, "gate", base, cand, "--question", "tasks", "--require-gain", "--json")
    report = json.loads(out)
    assert (status, report["decision"], report["attempts_to_resolve"]) == (2, "undecided", None)


def test_gate_is_undecided_when_the_counting_rules_disagree(capsys, tmp_path):
    base = job(tmp_path, "base", {f"t{i:02d}": [1.0, 1.0, 0.0, 0.0] for i in range(30)})
    # The candidate passes every trial that ran, and half of its trials did not run.
    broken = {f"t{i:02d}": [1.0, 1.0, None, None] for i in range(30)}
    cand = job(tmp_path, "cand", broken)
    status, out = run(capsys, "gate", base, cand, "--json")
    report = json.loads(out)
    assert status == 2 and "counting rule" in report["reason"]
    assert run(capsys, "gate", base, cand, "--one-rule")[0] == 0
    assert run(capsys, "gate", base, cand, "--one-rule", "--trials", "all")[0] == 2


def test_gate_refuses_broken_data(capsys, tmp_path):
    base = job(tmp_path, "base", {"a": [1.0, 0.0], "b": [1.0, 1.0]})
    cand = job(tmp_path, "cand", {"a": [1.0, 0.0], "b": [1.0, 1.0]}, agent="oracle")
    mixed = write_job(
        tmp_path,
        "mixed",
        [trial("a", 1.0), trial("a", 1.0, agent="oracle", finished_at="2026-02-05T16:30:00Z")],
    )
    assert run(capsys, "gate", base, cand)[0] in (0, 2)
    assert run(capsys, "gate", base, str(mixed))[0] == 3
    assert run(capsys, "gate", base, str(mixed), "--allow-errors")[0] != 3


def test_audit(capsys, arms, tmp_path):
    status, out = run(capsys, "audit", *arms)
    assert status == 0 and "0 errors, 0 warnings, 0 notes" in out
    mixed = write_job(
        tmp_path,
        "mixed",
        [trial("a", 1.0), trial("a", 1.0, agent="oracle", finished_at="2026-02-05T16:30:00Z")],
    )
    status, out = run(capsys, "audit", str(mixed), "--json")
    report = json.loads(out)
    assert status == 1 and report["worst"] == "error"
    assert "control-agent-trials" in {f["code"] for f in report["findings"]}


def test_counting_rule_changes_the_score(capsys, tmp_path):
    path = job(tmp_path, "arm", {"a": [1.0, None], "b": [1.0, 1.0]})
    valid = json.loads(run(capsys, "score", path, "--json")[1])["arms"][0]
    every = json.loads(run(capsys, "score", path, "--json", "--trials", "all")[1])["arms"][0]
    assert (valid["score"], every["score"]) == (1.0, 0.75)
    assert valid["score_other_counting"] == 0.75


def test_missing_path_is_an_error(capsys):
    assert main(["score", "/no/such/path"]) == 3
    assert "not a directory" in capsys.readouterr().err


def test_no_trials_is_an_error(tmp_path):
    with pytest.raises(SystemExit):
        main(["score", str(tmp_path)])


def test_every_command_that_reads_arms_takes_task_key(arms):
    for command in COMMANDS:
        paths = [*arms, arms[0]] if command == "decompose" else list(arms)
        args = parser().parse_args([command, *paths, "--task-key", "^(.*)-talk$"])
        assert args.task_key == "^(.*)-talk$"


def test_a_task_key_that_is_not_usable_exits_with_3(capsys, arms):
    wrong = {
        "(": "--task-key is not a valid regular expression: missing ), unterminated subpattern at position 0",
        "-talk$": "--task-key needs a capture group, as in '^(.*)-(?:solo|talk)$'",
    }
    for pattern, message in wrong.items():
        for command in ("score", "compare"):
            assert main([command, *arms, f"--task-key={pattern}"]) == 3
            assert capsys.readouterr().err == f"tidegauge: {message}\n"


def test_compare_with_task_key(capsys, tmp_path):
    silent = job(tmp_path, "silent", {f"x/t{i}-silent": [0.0, 0.0] for i in range(6)})
    talk = job(tmp_path, "talk", {f"x/t{i}-talk": [1.0, 1.0] for i in range(6)})
    assert main(["compare", silent, talk]) == 3
    assert "the arms share no task" in capsys.readouterr().err
    report = json.loads(
        run(capsys, "compare", silent, talk, "--task-key", "^(.*)-(?:silent|talk)$", "--json")[1]
    )
    assert (report["shared_tasks"], report["difference"], report["findings"]) == (6, 1.0, [])


def test_compare_says_which_tasks_are_left_out(capsys, tmp_path):
    base = job(tmp_path, "base", {"a": [1.0, 0.0], "b": [1.0, 1.0], "c": [0.0, 0.0]})
    cand = job(tmp_path, "cand", {"a": [1.0, 1.0], "b": [None, None]})
    _, out = run(capsys, "compare", base, cand)
    assert "1 shared tasks" in out
    assert (
        "2 tasks are left out: 2 have counted trials only in the baseline and 0 only in the candidate" in out
    )
