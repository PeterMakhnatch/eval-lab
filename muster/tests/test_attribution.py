"""attribution.py: the roster table, the seat effects and the findings of DESIGN section 6."""

import pytest
from seat_fixture import TIMEOUT, write_matrix, write_trial

from muster import attribution, load, render
from muster.model import InputError

# hello-seats as the fork ships it: p1's note gives the reward, and both seats must leave a record.
HELLO = {
    "p1=oracle,p2=oracle": [1.0],
    "p1=oracle,p2=nop": [0.0],
    "p1=nop,p2=oracle": [0.0],
    "p1=nop,p2=nop": [0.0],
}


def analyze(tmp_path, table, **facts):
    settings = {key: facts.pop(key) for key in ("not_tested", "task") if key in facts}
    out = write_matrix(tmp_path / "m", table, **settings)
    return attribution.analyze(load.walk(out), root=out, **facts)


def codes(report, severity=None):
    return [f["code"] for f in report["findings"] if severity in (None, f["severity"])]


def finding(report, code):
    found = [f for f in report["findings"] if f["code"] == code]
    assert len(found) == 1, found
    return found[0]


def test_roster_table_has_the_mean_and_n_of_every_reward_key(tmp_path):
    table = {
        "p1=oracle,p2=oracle": [
            {"reward": 1.0, "seat.p1": 1, "messages": 4},
            {"reward": 0.5, "seat.p1": 1, "messages": 6},
        ],
        "p1=nop,p2=oracle": [{"reward": 0.25, "seat.p1": 0, "messages": 2}],
    }
    report = analyze(tmp_path, table)
    assert report["reward_keys"] == [
        "reward",
        "seat.p1",
        "messages",
    ]  # reward, then seat credit, then diagnostics
    first, second = report["rosters"]
    assert (first["roster"], first["trials"], first["n"]) == (
        "p1=nop,p2=oracle",
        1,
        1,
    )  # sorted without a plan
    assert second["rewards"]["reward"] == {"mean": 0.75, "n": 2, "values": [1.0, 0.5]}
    assert second["rewards"]["messages"]["mean"] == 5.0
    assert (report["task"], report["trials"]) == ("hello-seats", 3)
    assert report["seats"] == [{"seat": "p1", "under_test": True}, {"seat": "p2", "under_test": False}]


def test_planned_rosters_keep_the_matrix_order_and_show_what_is_missing(tmp_path):
    planned = [{"roster": key} for key in [*HELLO, "p1=oracle,p2=late"]]
    report = analyze(
        tmp_path, {k: v for k, v in HELLO.items() if k != "p1=oracle,p2=nop"}, planned=planned, attempts=1
    )
    assert [row["roster"] for row in report["rosters"]] == [*HELLO, "p1=oracle,p2=late"]
    assert [row["trials"] for row in report["rosters"]] == [1, 0, 1, 1, 0]
    messages = [f["message"] for f in report["findings"] if f["code"] == "trial-error"]
    assert messages == [
        "Roster p1=oracle,p2=nop has 0 of 1 trials.",
        "Roster p1=oracle,p2=late has 0 of 1 trials.",
    ]
    assert "no trial" in attribution.text(report)


def test_seat_effect_is_oracle_minus_null_over_matched_pairs(tmp_path):
    report = analyze(tmp_path, HELLO)
    by_seat = {e["seat"]: e for e in report["effects"]}
    assert by_seat["p1"] == {
        "key": "reward",
        "seat": "p1",
        "under_test": True,
        "pairs": 2,
        "oracle": 0.5,
        "null": 0.0,
        "effect": 0.5,
    }
    assert (by_seat["p2"]["effect"], by_seat["p2"]["pairs"], by_seat["p2"]["under_test"]) == (0.5, 2, False)


def test_seat_effect_uses_only_the_pairs_that_exist(tmp_path):
    table = {"p1=oracle,p2=oracle": [1.0], "p1=nop,p2=oracle": [0.25], "p1=oracle,p2=spoof": [1.0]}
    by_seat = {e["seat"]: e for e in analyze(tmp_path, table)["effects"]}
    assert (by_seat["p1"]["effect"], by_seat["p1"]["pairs"]) == (0.75, 1)  # p1=nop,p2=spoof was not run
    assert (by_seat["p2"]["effect"], by_seat["p2"]["pairs"], by_seat["p2"]["oracle"]) == (None, 0, None)


def test_a_sound_matrix_has_no_error(tmp_path):
    table = {
        "p1=oracle,p2=oracle": [1.0, 1.0],
        "p1=oracle,p2=nop": [0.5, 0.5],
        "p1=nop,p2=oracle": [0.5, 0.5],
    }
    report = analyze(tmp_path, {**table, "p1=nop,p2=nop": [0.0, 0.0]}, not_tested=())
    assert codes(report) == [] and (report["errors"], report["warnings"]) == (0, 0)
    assert attribution.status(report) == 0
    assert [item["code"] for item in report["not_checked"]] == ["credit-leak", "adversary-wins"]


def test_unsolvable_reads_the_expected_oracle_reward(tmp_path):
    table = {**HELLO, "p1=oracle,p2=oracle": [0.75, 0.75]}
    report = analyze(tmp_path, table)
    assert finding(report, "unsolvable")["message"] == "Every seat is oracle and reward is 0.75, below 1."
    assert (
        finding(report, "unsolvable")["roster"] == "p1=oracle,p2=oracle" and attribution.status(report) == 1
    )
    assert "unsolvable" not in codes(analyze(tmp_path / "meta", table, oracle_reward=0.75))
    assert "unsolvable" in codes(analyze(tmp_path / "more", table, oracle_reward=0.8))


def test_free_pass_when_doing_nothing_pays(tmp_path):
    report = analyze(tmp_path, {**HELLO, "p1=nop,p2=nop": [0.2]})
    assert finding(report, "free-pass")["message"] == "Every seat is nop and reward is 0.2, above 0."


def test_tested_seat_not_needed(tmp_path):
    report = analyze(tmp_path, {**HELLO, "p1=nop,p2=oracle": [1.0]})
    found = finding(report, "tested-seat-not-needed")
    assert (found["roster"], found["seats"], found["severity"]) == ("p1=nop,p2=oracle", ["p1"], "error")
    # p2 is not under test: the reward can stay when p2 does nothing
    assert "tested-seat-not-needed" not in codes(
        analyze(tmp_path / "b", {**HELLO, "p1=oracle,p2=nop": [1.0]})
    )
    # an all-oracle reward of 0 is `unsolvable`, not a reason to report every roster
    zero = analyze(tmp_path / "c", {**HELLO, "p1=oracle,p2=oracle": [0.0]})
    assert "tested-seat-not-needed" not in codes(zero) and "unsolvable" in codes(zero)


def test_credit_leak(tmp_path):
    table = {
        "p1=oracle,p2=oracle": [{"reward": 1.0, "seat.p1": 1.0, "seat.p2": 1.0}],
        "p1=oracle,p2=nop": [{"reward": 0.5, "seat.p1": 1.0, "seat.p2": 0.5}],
        "p1=nop,p2=oracle": [{"reward": 0.5, "seat.p1": 0.0, "seat.p2": 1.0}],
    }
    found = finding(analyze(tmp_path, table), "credit-leak")
    assert (found["roster"], found["seat"]) == ("p1=oracle,p2=nop", "p2")
    assert found["message"] == "Roster p1=oracle,p2=nop: seat p2 is nop and seat.p2 is 0.5, above 0."


def test_decorative_seat_reads_scores_and_not_diagnostics(tmp_path):
    table = {
        "p1=oracle,p2=oracle": [{"reward": 1.0, "messages": 9}],
        "p1=oracle,p2=nop": [{"reward": 1.0, "messages": 2}],
        "p1=nop,p2=oracle": [{"reward": 0.0, "messages": 7}],
        "p1=nop,p2=nop": [{"reward": 0.0, "messages": 0}],
    }
    report = analyze(tmp_path, table)
    found = finding(report, "decorative-seat")
    assert (found["seat"], found["severity"]) == ("p2", "warn")
    assert found["message"] == "No score changes with the policy of seat p2."


def test_decorative_seat_needs_two_rosters_that_differ_in_the_seat(tmp_path):
    report = analyze(tmp_path, {"p1=oracle,p2=oracle": [1.0], "p1=nop,p2=oracle": [0.0]})
    assert "decorative-seat" not in codes(report)
    assert {
        "code": "decorative-seat",
        "reason": "seat p2: no two rosters with a score differ only in this seat",
    } in report["not_checked"]


def test_adversary_wins_against_the_null_baseline(tmp_path):
    table = {**HELLO, "p1=nop,p2=spoof": [{"reward": 1.0, "messages": 1}], "p1=oracle,p2=spoof": [1.0]}
    table["p1=oracle,p2=nop"] = [1.0]
    report = analyze(tmp_path, table, exploits=["spoof"])
    found = finding(report, "adversary-wins")
    assert (found["roster"], found["seat"], found["key"]) == ("p1=nop,p2=spoof", "p2", "reward")
    assert found["message"] == "Roster p1=nop,p2=spoof: p2=spoof gives reward 1, above 0 with p2=nop."
    assert "tested-seat-not-needed" in codes(report)  # p1 did nothing and the team has the full reward
    assert attribution.status(report) == 1


def test_adversary_wins_ignores_diagnostics_and_unnamed_policies(tmp_path):
    table = {
        "p1=oracle,p2=oracle": [{"reward": 1.0, "rejected": 0}],
        "p1=oracle,p2=nop": [{"reward": 0.5, "rejected": 0}],
        "p1=oracle,p2=spoof": [{"reward": 0.5, "rejected": 3}],
        "p1=oracle,p2=late": [{"reward": 1.0, "rejected": 0}],
    }
    assert "adversary-wins" not in codes(analyze(tmp_path, table, exploits=["spoof"]))
    report = analyze(tmp_path / "b", table, exploits=["spoof", "late"])
    assert finding(report, "adversary-wins")["roster"] == "p1=oracle,p2=late"


def test_adversary_wins_says_when_it_was_not_checked(tmp_path):
    def reasons(report):
        return [item["reason"] for item in report["not_checked"] if item["code"] == "adversary-wins"]

    assert reasons(analyze(tmp_path / "a", HELLO)) == [
        "no exploit policy is named ([metadata] exploits or --exploits)"
    ]
    assert reasons(analyze(tmp_path / "b", HELLO, exploits=["spoof"])) == [
        "no roster with a score has an exploit policy (spoof)"
    ]
    table = {"p1=oracle,p2=oracle": [1.0], "p1=idle,p2=spoof": [1.0]}
    assert reasons(analyze(tmp_path / "c", table, exploits=["spoof"])) == [
        "roster p1=idle,p2=spoof: no score for p1=idle,p2=nop"
    ]


def test_not_deterministic(tmp_path):
    table = {**HELLO, "p1=oracle,p2=nop": [0.0, 1.0, 0.0], "p1=nop,p2=nop": [0.0, 0.0]}
    report = analyze(tmp_path, table)
    found = finding(report, "not-deterministic")
    assert (
        found["message"] == "Roster p1=oracle,p2=nop: reward is 0, 1, 0 in 3 trials."
        and found["severity"] == "warn"
    )
    single = analyze(tmp_path / "b", HELLO)
    assert {
        "code": "not-deterministic",
        "reason": "no roster has 2 or more trials with a valid reward",
    } in single["not_checked"]


def test_a_reward_key_in_some_trials_only_is_not_deterministic(tmp_path):
    report = analyze(
        tmp_path, {**HELLO, "p1=oracle,p2=oracle": [{"reward": 1.0, "bonus": 1.0}, {"reward": 1.0}]}
    )
    assert finding(report, "not-deterministic")["key"] == "bonus"


def test_trial_error_for_exceptions_and_for_trials_without_a_valid_reward(tmp_path):
    table = {
        **HELLO,
        "p1=oracle,p2=nop": [
            {
                "rewards": {"reward": 0.0},
                "exception": "NonZeroAgentExitCodeError",
                "seat_exceptions": {"p1": "NonZeroAgentExitCodeError"},
            },
            {"rewards": None, "exception": "RuntimeError"},
            {"rewards": {"reward": 0.0}},
        ],
    }
    report = analyze(tmp_path, table)
    row = next(row for row in report["rosters"] if row["roster"] == "p1=oracle,p2=nop")
    assert (row["trials"], row["n"], row["errors"], row["timeouts"]) == (3, 1, 2, 0)
    assert row["rewards"]["reward"]["n"] == 1  # only the trials with a valid reward count
    messages = [f["message"] for f in report["findings"] if f["code"] == "trial-error"]
    assert messages == [
        "Roster p1=oracle,p2=nop: trial task__001 has NonZeroAgentExitCodeError.",
        "Roster p1=oracle,p2=nop: trial task__002 has RuntimeError.",
    ]
    assert "error 2/3" in attribution.text(report) and attribution.status(report) == 0  # warnings only


def test_a_timeout_is_a_normal_outcome_of_a_control_roster(tmp_path):
    timeout = {"rewards": {"reward": 0.0}, "exception": TIMEOUT, "seat_exceptions": {"p1": TIMEOUT}}
    report = analyze(tmp_path, {**HELLO, "p1=oracle,p2=nop": [timeout, timeout]})
    row = next(row for row in report["rosters"] if row["roster"] == "p1=oracle,p2=nop")
    assert (row["n"], row["timeouts"], row["timeout_seats"], row["errors"]) == (2, 2, ["p1"], 0)
    assert row["rewards"]["reward"]["mean"] == 0.0  # the reward of a timed-out trial counts
    assert "trial-error" not in codes(report)
    line = next(line for line in attribution.text(report).splitlines() if line.startswith("p1=oracle,p2=nop"))
    assert line.split()[1:] == ["2", "0", "timeout", "p1", "2/2"]


def test_a_timeout_of_the_all_oracle_roster_is_a_trial_error(tmp_path):
    timeout = {"rewards": {"reward": 1.0}, "exception": TIMEOUT, "seat_exceptions": {"p2": TIMEOUT}}
    report = analyze(tmp_path, {**HELLO, "p1=oracle,p2=oracle": [timeout]})
    assert (
        finding(report, "trial-error")["message"]
        == "Roster p1=oracle,p2=oracle: trial task__001 has AgentTimeoutError."
    )


def test_a_timeout_with_another_exception_is_an_error(tmp_path):
    mixed = {
        "rewards": {"reward": 0.0},
        "exception": TIMEOUT,
        "seat_exceptions": {"p1": TIMEOUT, "p2": "NonZeroAgentExitCodeError"},
    }
    report = analyze(tmp_path, {**HELLO, "p1=oracle,p2=late": [mixed]})
    message = finding(report, "trial-error")["message"]
    assert (
        message
        == "Roster p1=oracle,p2=late: trial task__001 has AgentTimeoutError, NonZeroAgentExitCodeError."
    )


def test_problems_and_failed_harbor_runs_are_trial_errors(tmp_path):
    out = write_matrix(tmp_path / "m", HELLO)
    (out / "p1=nop,p2=nop" / "jobs" / "p1=nop,p2=nop" / "task__001" / "result.json").write_text("{")
    planned = [{"roster": key, "returncode": 0, "timed_out": False} for key in HELLO]
    planned[1].update(returncode=1)
    planned[2].update(timed_out=True, returncode=-15)
    report = attribution.analyze(load.walk(out), root=out, planned=planned, attempts=1)
    messages = [f["message"] for f in report["findings"] if f["code"] == "trial-error"]
    assert "Roster p1=nop,p2=nop has 0 of 1 trials." in messages
    assert "harbor ended with status 1 for roster p1=oracle,p2=nop." in messages
    assert "harbor did not end in time for roster p1=nop,p2=oracle." in messages
    assert any(
        m.startswith("p1=nop,p2=nop/jobs/p1=nop,p2=nop/task__001: result.json is not valid JSON")
        for m in messages
    )
    assert report["harbor_failures"] == 2 and len(report["problems"]) == 1
    assert report["problems"][0]["path"] == "p1=nop,p2=nop/jobs/p1=nop,p2=nop/task__001"


def test_the_null_policy_can_be_a_script(tmp_path):
    """The fork's hello-seats wants a record from every seat, so `idle` (a script) stands for nop."""
    table = {
        "p1=oracle,p2=oracle": [1.0],
        "p1=oracle,p2=idle": [
            {"rewards": {"reward": 1.0}, "exception": TIMEOUT, "seat_exceptions": {"p1": TIMEOUT}}
        ],
        "p1=oracle,p2=spoof": [
            {"rewards": {"reward": 1.0}, "exception": TIMEOUT, "seat_exceptions": {"p1": TIMEOUT}}
        ],
        "p1=idle,p2=oracle": [0.0],
        "p1=idle,p2=idle": [0.0],
        "p1=idle,p2=spoof": [1.0],
    }
    report = analyze(tmp_path, table, exploits=["spoof"], null="idle")
    assert codes(report) == ["tested-seat-not-needed", "adversary-wins"]
    assert finding(report, "tested-seat-not-needed")["roster"] == "p1=idle,p2=spoof"
    assert (
        finding(report, "adversary-wins")["message"]
        == "Roster p1=idle,p2=spoof: p2=spoof gives reward 1, above 0 with p2=idle."
    )
    by_seat = {e["seat"]: e for e in report["effects"]}
    assert (by_seat["p1"]["pairs"], by_seat["p2"]["pairs"], by_seat["p2"]["effect"]) == (3, 2, 0.0)
    assert by_seat["p1"]["effect"] == pytest.approx(2 / 3)
    assert "free-pass" not in codes(report) and report["null_policy"] == "idle"
    with_nop = analyze(tmp_path / "nop", table, exploits=["spoof"])
    assert codes(with_nop) == [] and len(with_nop["not_checked"]) > 3  # nothing to compare with nop


def test_unusable_input(tmp_path):
    with pytest.raises(InputError, match="no seat trial found"):
        attribution.analyze(load.walk(tmp_path))
    write_trial(tmp_path / "a", "t__1", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 1})
    write_trial(
        tmp_path / "a", "t__2", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 1}, task="muster/boxes"
    )
    with pytest.raises(InputError, match="more than one task: hello-seats, boxes"):
        attribution.analyze(load.walk(tmp_path / "a"))
    write_trial(tmp_path / "b", "t__1", {"p1": "oracle", "p2": "nop"}, rewards={"reward": 1})
    write_trial(tmp_path / "b", "t__2", {"p1": "oracle", "p2": "nop", "p3": "nop"}, rewards={"reward": 1})
    with pytest.raises(InputError, match="do not have the same seats"):
        attribution.analyze(load.walk(tmp_path / "b"))
    for key in ("p1=oracle", "p2=oracle,p1=nop", "p1,p2", 7):
        with pytest.raises(InputError, match="does not fit the seats"):
            attribution.analyze(load.walk(tmp_path / "a" / "t__1"), planned=[{"roster": key}])


def test_the_same_files_give_the_same_bytes(tmp_path):
    table = {**HELLO, "p1=oracle,p2=nop": [0.0, 1.0], "p1=nop,p2=spoof": [{"reward": 1.0, "seat.p1": 0.25}]}
    first = analyze(tmp_path, table, exploits=["spoof"])
    again = attribution.analyze(
        reversed(list(load.walk(tmp_path / "m"))), root=tmp_path / "m", exploits=["spoof"]
    )
    assert render.to_json(first) == render.to_json(again)
    assert attribution.text(first) == attribution.text(again)
    assert str(tmp_path) not in render.to_json(first)  # paths are relative to the matrix directory


def test_text_report(tmp_path):
    timeout = {"rewards": {"reward": 0.0}, "exception": TIMEOUT, "seat_exceptions": {"p1": TIMEOUT}}
    table = {**HELLO, "p1=oracle,p2=nop": [timeout], "p1=nop,p2=oracle": [1.0]}
    report = analyze(tmp_path, table, planned=[{"roster": key} for key in HELLO], attempts=1)
    assert attribution.text(report) == TEXT


TEXT = """\
task hello-seats: 4 trials in 4 rosters
seats: p1 (under test), p2
null policy: nop   oracle reward: 1   exploits: none

Rosters: the mean of each reward key over the n trials with a valid reward
roster               n  reward  notes
p1=oracle,p2=oracle  1       1
p1=oracle,p2=nop     1       0  timeout p1 1/1
p1=nop,p2=oracle     1       1
p1=nop,p2=nop        1       0

Seat effects: the seat as oracle, minus the same rosters with only that seat as nop
key     seat  under test  oracle  nop  effect  pairs
reward  p1    yes            0.5  0.5       0      2
reward  p2    no               1    0      +1      2

Findings
severity  code                    finding
error     tested-seat-not-needed  Roster p1=nop,p2=oracle: tested seat p1 is nop and reward is 1, the all-oracle reward.
warn      decorative-seat         No score changes with the policy of seat p1.

Not checked
credit-leak: the verifier wrote no seat.<name> key
adversary-wins: no exploit policy is named ([metadata] exploits or --exploits)
not-deterministic: no roster has 2 or more trials with a valid reward

1 errors, 1 warnings
"""


def test_a_finding_that_the_task_design_expects_is_shown_and_not_counted(tmp_path):
    table = {**HELLO, "p1=nop,p2=oracle": [1.0]}
    plain = analyze(tmp_path / "a", table)
    assert finding(plain, "tested-seat-not-needed")["severity"] == "error"
    assert attribution.status(plain) == 1
    declared = analyze(tmp_path / "b", table, expected_findings=["tested-seat-not-needed"])
    found = finding(declared, "tested-seat-not-needed")
    assert found["severity"] == "expected" and found["roster"] == "p1=nop,p2=oracle"
    assert (declared["errors"], declared["expected"], attribution.status(declared)) == (0, 1, 0)
    assert "1 expected by the task's design (tested-seat-not-needed)" in attribution.text(declared)


def test_trial_errors_are_never_expected(tmp_path):
    report = analyze(tmp_path, {**HELLO, "p1=oracle,p2=nop": [None]}, expected_findings=["trial-error"])
    errors = [f for f in report["findings"] if f["code"] == "trial-error"]
    assert errors and all(f["severity"] == "warn" for f in errors)
