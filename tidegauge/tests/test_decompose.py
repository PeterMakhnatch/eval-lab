"""``tidegauge decompose`` on 3 arms that this file builds. Every expected number is computed by hand.

The 3 arms have 20 tasks with 3 trials each. A digit is the number of passed trials of 1 task.
A task with 1 or 2 passes in 3 trials (a mixed task) has the trial variance 1/3, so its term
s^2 / k is 1/9. A task with 0 or 3 passes has the term 0.
"""

import itertools
import json
import math
import random
import statistics
from pathlib import Path

import pytest

from tidegauge import stats
from tidegauge.cli import main
from tidegauge.commands import decompose
from tidegauge.load import load_arm
from tidegauge.model import Counting

from harbor_fixture import arm, seat, seat_trial, trial, write_job

SOLO = "33332321111100000000"  # 24 of 60 trials pass. 7 mixed tasks
INDEP = "33333333232132211010"  # 42 of 60. 8 mixed tasks
TEAM = "33333333313313022201"  # 45 of 60. 6 mixed tasks
# Per task, in thirds: the sum and the sum of squares of (candidate - baseline).
SUMS = {"headcount": (18, 32), "coordination": (3, 27), "total": (21, 41)}
# Student's t quantiles for a 95% interval (published table), and the normal quantile for 80% power.
T975 = {12: 2.178813, 14: 2.144787, 16: 2.119905, 19: 2.093024, 26: 2.055529, 28: 2.048407, 30: 2.042272}
Z80 = 0.841621
NEAR = 2e-6
EFFECTS = ("headcount", "coordination", "total")


def passes(counts):
    return {f"t{i:02d}": [1.0] * int(c) + [0.0] * (3 - int(c)) for i, c in enumerate(counts)}


def pair(cost=0.05):
    return [seat("p1", cost=cost), seat("p2", cost=cost)]


def job(root, name, trials):
    """Write the trials as 1 job. Every trial gets its own end time."""
    stamped = [
        dict(t, finished_at=f"2026-02-05T17:{i // 60:02d}:{i % 60:02d}Z") for i, t in enumerate(trials)
    ]
    return str(write_job(root, name, stamped))


def trio(root, team=TEAM, indep=INDEP):
    """1 agent, 2 seats at $0.05 each, and 2 seats at $0.065 each. A trial of 1 agent costs $0.05."""
    return [
        str(arm(root, "solo", passes(SOLO), model="deepseek/deepseek-flash")),
        str(arm(root, "indep", passes(indep), seats=pair())),
        str(arm(root, "team", passes(team), seats=pair(0.065))),
    ]


def report_of(paths, metric="reward", counting=Counting.VALID, level=0.95):
    return decompose.build([load_arm(path) for path in paths], metric, counting, level)


def differences(report):
    return [report["effects"][name]["difference"] for name in EFFECTS]


def run(capsys, *argv):
    status = main(["decompose", *argv])
    return status, capsys.readouterr().out


def interval(center, quantile, se):
    return [pytest.approx(center - quantile * se, abs=NEAR), pytest.approx(center + quantile * se, abs=NEAR)]


def thirds(name):
    """The difference of each task for 1 effect of the 3 arms of ``trio``, in thirds."""
    counts = {"solo": SOLO, "indep": INDEP, "team": TEAM}
    baseline, candidate = decompose.EFFECTS[name]
    return [int(b) - int(a) for a, b in zip(counts[baseline], counts[candidate], strict=True)]


def signflip_by_hand(steps):
    """The share of the sign patterns of the nonzero steps whose sum is at least as far from 0 as observed."""
    values = [s for s in steps if s]
    patterns = list(itertools.product((1, -1), repeat=len(values)))
    hits = sum(
        abs(sum(s * v for s, v in zip(signs, values, strict=True))) >= abs(sum(values)) for signs in patterns
    )
    return hits / len(patterns)


def single(root, name, passed, n=12, **kwargs):
    """1 trial per task on the tasks t00 to t(n - 1). A task passes when its index is in ``passed``."""
    return str(arm(root, name, {f"t{i:02d}": [float(i in passed)] for i in range(n)}, **kwargs))


WIDTH_0 = (
    "The tasks interval of {} has width 0 because every task gives the same difference. "
    "More tasks can measure the spread between tasks."
)


def test_the_sums_that_these_tests_use():
    counts = {
        "solo": [int(c) for c in SOLO],
        "indep": [int(c) for c in INDEP],
        "team": [int(c) for c in TEAM],
    }
    assert [sum(c) for c in counts.values()] == [24, 42, 45]
    assert [sum(1 for x in c if x in (1, 2)) for c in counts.values()] == [7, 8, 6]
    assert sum(x * x for x in counts["solo"]) == 58
    for name, (baseline, candidate) in decompose.EFFECTS.items():
        steps = [b - a for a, b in zip(counts[baseline], counts[candidate], strict=True)]
        assert (sum(steps), sum(d * d for d in steps)) == SUMS[name]


def test_the_3_arms(tmp_path):
    solo, indep, team = report_of(trio(tmp_path))["arms"]
    assert [(a["role"], a["name"], a["seats"]) for a in (solo, indep, team)] == [
        ("solo", "solo", [1, 1]),
        ("indep", "indep", [2, 2]),
        ("team", "team", [2, 2]),
    ]
    assert [(a["tasks"], a["trials"]) for a in (solo, indep, team)] == [(20, 60)] * 3
    assert [a["score"] for a in (solo, indep, team)] == [pytest.approx(x) for x in (0.40, 0.70, 0.75)]
    # rerun: se = sqrt(mixed tasks / 9) / 20, with 2 degrees of freedom for each mixed task.
    assert solo["ci_rerun"] == interval(0.40, T975[14], math.sqrt(7 / 9) / 20)
    assert indep["ci_rerun"] == interval(0.70, T975[16], math.sqrt(8 / 9) / 20)
    assert team["ci_rerun"] == interval(0.75, T975[12], math.sqrt(6 / 9) / 20)
    # tasks, for solo: the pass counts have the sum 24 and the sum of squares 58.
    variance = (58 - 24**2 / 20) / 19 / 9
    assert solo["ci_tasks"] == interval(0.40, T975[19], math.sqrt(variance / 20))
    # cost: 1 agent at 0.05, 2 seats at 0.05, 2 seats at 0.065
    assert [a["cost_per_trial"] for a in (solo, indep, team)] == [
        pytest.approx(x) for x in (0.05, 0.10, 0.13)
    ]
    assert [a["cost_ratio"] for a in (solo, indep, team)] == [pytest.approx(x) for x in (1.0, 2.0, 2.6)]
    assert [(a["trials_with_cost"], a["trials_on_shared_tasks"]) for a in (solo, indep, team)] == [
        (60, 60)
    ] * 3


def test_the_3_effects_and_their_2_error_bars(tmp_path):
    report = report_of(trio(tmp_path))
    head, coordination, total = (report["effects"][name] for name in EFFECTS)
    assert [(e["baseline"], e["candidate"]) for e in (head, coordination, total)] == [
        ("solo", "indep"),
        ("indep", "team"),
        ("solo", "team"),
    ]
    assert differences(report) == [pytest.approx(x) for x in (0.30, 0.05, 0.35)]
    # rerun: the terms of both arms add. headcount has 7 + 8 mixed tasks, coordination 8 + 6, total 7 + 6.
    assert head["rerun"]["se"] == pytest.approx(math.sqrt(15 / 9) / 20)
    assert head["rerun"]["interval"] == interval(0.30, T975[30], math.sqrt(15 / 9) / 20)
    assert coordination["rerun"]["interval"] == interval(0.05, T975[28], math.sqrt(14 / 9) / 20)
    assert total["rerun"]["interval"] == interval(0.35, T975[26], math.sqrt(13 / 9) / 20)
    # tasks: the variance of the differences per task, from their sum and their sum of squares in thirds.
    se = {name: math.sqrt((squares - total**2 / 20) / 19 / 9 / 20) for name, (total, squares) in SUMS.items()}
    assert head["tasks"]["se"] == pytest.approx(se["headcount"])
    assert head["tasks"]["interval"] == interval(0.30, T975[19], se["headcount"])
    assert coordination["tasks"]["interval"] == interval(0.05, T975[19], se["coordination"])
    assert total["tasks"]["interval"] == interval(0.35, T975[19], se["total"])
    # All 3 arms have 3 trials on every task and 0/1 rewards: the exact permutation test decides.
    assert {e["rerun"]["test"] for e in (head, coordination, total)} == {"exact permutation"}
    assert [(e["rerun"]["verdict"], e["tasks"]["verdict"]) for e in (head, coordination, total)] == [
        ("resolved", "resolved"),
        ("not resolved", "not resolved"),
        ("resolved", "resolved"),
    ]
    assert head["rerun"]["p"] < 0.0001 < 0.05 < coordination["rerun"]["p"]
    assert total["rerun"]["p"] < 0.0001
    # The paired t over tasks for coordination: t = 0.05 / se = 0.5675 with 19 degrees of freedom.
    # The two-sided p of that t is 0.5770 (numerical integration of the t density).
    assert 0.05 / se["coordination"] == pytest.approx(0.5675, abs=1e-4)
    assert coordination["tasks"]["p"] == pytest.approx(0.5770, abs=1e-4)
    # The sign-flip test, from every sign pattern of the differences. headcount and total have 12 nonzero
    # differences of 1 sign, so p = 2/4096. coordination has 12 nonzero differences, and 2,896 of the
    # 4,096 sign patterns reach its sum. A tasks verdict needs both tests below 0.05.
    flips = [e["tasks"]["p_signflip"] for e in (head, coordination, total)]
    assert flips == [pytest.approx(signflip_by_hand(thirds(name)), abs=1e-12) for name in EFFECTS]
    assert flips == [pytest.approx(x, abs=1e-12) for x in (2 / 4096, 2896 / 4096, 2 / 4096)]
    assert all(e["tasks"]["signflip_exact"] for e in (head, coordination, total))
    # The detectable effect: (t quantile + z for 80% power) * se.
    assert coordination["rerun"]["detectable"] == pytest.approx(
        (T975[28] + Z80) * math.sqrt(14 / 9) / 20, abs=NEAR
    )
    assert coordination["tasks"]["detectable"] == pytest.approx(
        (T975[19] + Z80) * se["coordination"], abs=NEAR
    )
    # The total is resolved for rerun, so the share exists: 0.05 / 0.35.
    assert report["coordination_share"] == pytest.approx(1 / 7)
    assert (report["shared_tasks"], report["dropped_tasks"], report["alpha"]) == (20, {}, 0.05)


def test_total_is_headcount_plus_coordination(tmp_path):
    # 3 arms with different tasks and different numbers of trials per task.
    solo = arm(tmp_path, "solo", {"a": [1, 0, 0], "b": [0, 0], "c": [1, 1, 1, 0], "d": [1], "x": [1, 1]})
    indep = arm(tmp_path, "indep", {"a": [1, 1], "b": [1, 0, 0], "c": [1, 0], "d": [0, 0, 1], "y": [0]})
    team = arm(
        tmp_path, "team", {"a": [1], "b": [1, 1, 0, 0], "c": [1, 1], "d": [1, 1, 0], "x": [0], "y": [1]}
    )
    report = report_of([solo, indep, team])
    head, coordination, total = differences(report)
    # All 3 arms have a, b, c and d. Their task means: solo 1/3, 0, 3/4, 1. indep 1, 1/3, 1/2, 1/3. team 1, 1/2, 1, 2/3.
    assert head == pytest.approx((2 / 3 + 1 / 3 - 1 / 4 - 2 / 3) / 4)
    assert coordination == pytest.approx((0 + 1 / 6 + 1 / 2 + 1 / 3) / 4)
    assert total == pytest.approx((2 / 3 + 1 / 2 + 1 / 4 - 1 / 3) / 4)
    assert abs(total - head - coordination) <= 1e-12
    assert [a["score"] for a in report["arms"]] == [pytest.approx(x) for x in (25 / 48, 13 / 24, 19 / 24)]
    assert report["dropped_tasks"] == {"x": ["indep"], "y": ["solo"]}
    # Without the cut to the shared tasks, each pair uses its own tasks, and the 3 differences do not add up.
    outs = [load_arm(path).outcomes() for path in (solo, indep, team)]
    loose = [stats.compare(outs[i], outs[j]).delta for i, j in ((0, 1), (1, 2), (0, 2))]
    assert loose == [pytest.approx(x) for x in (1 / 48, 2 / 5, 1 / 60)]
    assert abs(loose[2] - loose[0] - loose[1]) > 0.4


def test_additivity_allows_for_the_rounding_of_large_values(tmp_path):
    # Input tokens near 10 and 20 million. Each arm has 3 tasks with 3 trials: the millions of
    # the task, and the tokens above them in each trial.
    tokens = {
        "solo": {10: (1, 2, 2), 9: (1, 1, 2), 8: (3, 1, 1)},
        "indep": {20: (1, 1, 2), 19: (2, 2, 1), 17: (1, 2, 4)},
        "team": {22: (2, 2, 3), 21: (1, 3, 3), 19: (1, 1, 3)},
    }
    paths = []
    for name, tasks in tokens.items():
        counts = [millions * 1_000_000 + more for millions, above in tasks.items() for more in above]
        trials = [trial("abc"[i // 3], 1.0, tokens=(count, 0, 0)) for i, count in enumerate(counts)]
        paths.append(job(tmp_path, name, trials))
    report = report_of(paths, metric="tokens_in")
    head, coordination, total = differences(report)
    # In ninths: headcount is (60000004 - 30000005) + (57000005 - 27000004) + (51000007 - 24000005) = 87000002,
    # coordination is 6000003 + 6000002 + 5999998 = 18000003, and the total is their sum.
    assert head == pytest.approx(87_000_002 / 9, rel=1e-14)
    assert coordination == pytest.approx(18_000_003 / 9, rel=1e-14)
    assert total == pytest.approx(105_000_005 / 9, rel=1e-14)
    # The 3 numbers add up to within the rounding of values near 2e7. That is far above 1e-12.
    assert 1e-12 < abs(total - head - coordination) < 1e-7
    assert report["other_counting"] is None  # the counting rules are about rewards
    text = decompose.text(report)
    # 3 tasks cannot resolve the tasks question: 3 differences give a sign-flip p of at least 2/8.
    assert (
        "More agents without communication add 9,666,667 tokens_in (headcount, resolved for rerun, not for tasks)."
        in text
    )
    assert "rerun 95%  " in text and "(pts)" not in text


def test_luck_in_indep_moves_the_2_parts_in_opposite_directions(tmp_path):
    # 1 more passed trial in indep, on t17: headcount gains 1/60, coordination loses 1/60, the total stays.
    lucky = INDEP[:17] + "1" + INDEP[18:]
    assert (INDEP[17], sum(int(c) for c in lucky)) == ("0", 43)
    moved = differences(report_of(trio(tmp_path / "lucky", indep=lucky)))
    assert moved == [pytest.approx(x) for x in (0.30 + 1 / 60, 0.05 - 1 / 60, 0.35)]
    # The 2 parts share V(indep): Corr(H, K) = -V(I) / (se(H) * se(K)) = -8 / sqrt(15 * 14) for the 3 arms.
    paths = trio(tmp_path / "plain")
    effects = report_of(paths)["effects"]
    v_indep = stats.score(load_arm(paths[1]).outcomes()).se_rerun ** 2
    correlation = -v_indep / (effects["headcount"]["rerun"]["se"] * effects["coordination"]["rerun"]["se"])
    assert correlation == pytest.approx(-8 / math.sqrt(15 * 14))
    assert correlation == pytest.approx(-0.552, abs=5e-4)


def test_the_2_parts_have_the_correlation_minus_one_half_with_equal_noise():
    # 4,000 simulated runs of 3 arms with the same noise: 30 tasks, 2 trials per task, pass rate 0.5.
    rng = random.Random(0)

    def simulated_score():
        outcomes = {str(task): [float(rng.random() < 0.5) for _ in range(2)] for task in range(30)}
        return stats.score(outcomes).mean

    runs = [(simulated_score(), simulated_score(), simulated_score()) for _ in range(4000)]
    head = [indep - solo for solo, indep, _ in runs]
    coordination = [team - indep for _, indep, team in runs]
    total = [team - solo for solo, _, team in runs]
    assert statistics.correlation(head, coordination) == pytest.approx(-0.5, abs=0.04)
    # Coordination and the total share the arm team: Cov(K, D) = V(C), so their correlation is +1/2.
    assert statistics.correlation(coordination, total) == pytest.approx(0.5, abs=0.04)


def test_coordination_of_exactly_0(tmp_path):
    report = report_of(trio(tmp_path, team=INDEP))  # the team has the outcomes of indep
    head, coordination, total = (report["effects"][name] for name in EFFECTS)
    assert coordination["difference"] == 0.0
    assert (coordination["rerun"]["p"], coordination["rerun"]["verdict"]) == (1.0, "not resolved")
    assert (coordination["tasks"]["p"], coordination["tasks"]["verdict"]) == (1.0, "not resolved")
    assert coordination["tasks"]["interval"] == [0.0, 0.0]  # every task has the difference 0
    assert WIDTH_0.format("coordination") in decompose.text(report).splitlines()
    # 8 + 8 mixed tasks: se = sqrt(16 / 9) / 20 with 32 degrees of freedom, and t = 2.036933.
    assert coordination["rerun"]["interval"] == interval(0.0, 2.036933, math.sqrt(16 / 9) / 20)
    assert total["difference"] == head["difference"] == pytest.approx(0.30)
    assert (head["rerun"]["verdict"], total["rerun"]["verdict"]) == ("resolved", "resolved")
    assert report["coordination_share"] == 0.0
    text = decompose.text(report)
    assert "Communication adds 0.0 pts to the same agents (coordination, not resolved)." in text
    share = "Coordination gives 0.0% of the total gain and headcount gives 100.0%. The coordination effect is not resolved, so its share can be 0%."
    assert share in text


def test_the_share_needs_a_resolved_total(tmp_path):
    same = [str(arm(tmp_path, name, passes(INDEP), seats=pair())) for name in ("a", "b", "c")]
    report = report_of(same)
    assert differences(report) == [0.0, 0.0, 0.0]
    assert report["coordination_share"] is None
    expected = "The share of coordination is not defined: the total effect is not resolved for rerun."
    assert expected in decompose.text(report)
    # solo and indep have the same outcomes: the total of +35 points is all coordination, and it is resolved.
    paths = [str(arm(tmp_path, name, passes(SOLO))) for name in ("solo", "indep")]
    report = report_of([*paths, str(arm(tmp_path, "team", passes(TEAM), seats=pair()))])
    assert report["coordination_share"] == 1.0
    whole = "Coordination gives 100.0% of the total gain and headcount gives 0.0%. A share is a ratio of 2 estimates and has no interval."
    assert whole in decompose.text(report)


def test_effects_with_opposite_signs(tmp_path):
    # solo 70%, indep 40%, team 75%: headcount -30, coordination +35, total +5 points.
    paths = [
        str(arm(tmp_path, "solo", passes(INDEP))),
        str(arm(tmp_path, "indep", passes(SOLO), seats=pair())),
        str(arm(tmp_path, "team", passes(TEAM), seats=pair())),
    ]
    report = report_of(paths)
    assert differences(report) == [pytest.approx(x) for x in (-0.30, 0.35, 0.05)]
    assert report["coordination_share"] is None  # the total of +5 points is not resolved
    text = decompose.text(report)
    assert (
        "More agents without communication remove 30.0 pts (headcount, resolved for rerun and tasks)." in text
    )
    assert (
        "Communication adds 35.0 pts to the same agents (coordination, resolved for rerun and tasks)." in text
    )
    assert "The team with communication is 5.0 pts above 1 agent (total, not resolved)." in text
    # solo 40%, indep 75%, team 70%: the total of +30 points is resolved, and coordination removes 5 points.
    paths = [
        str(arm(tmp_path, "low", passes(SOLO))),
        str(arm(tmp_path, "high", passes(TEAM), seats=pair())),
        str(arm(tmp_path, "mid", passes(INDEP), seats=pair())),
    ]
    report = report_of(paths)
    assert report["coordination_share"] == pytest.approx(-0.05 / 0.30)
    text = decompose.text(report)
    assert "Communication removes 5.0 pts from the same agents (coordination, not resolved)." in text
    share = "Coordination gives -16.7% of the total gain and headcount gives 116.7%: the 2 effects have opposite signs."
    assert share in text


def test_tasks_that_an_arm_lacks_are_left_out(tmp_path):
    solo = passes(SOLO) | {"t18": [None, None, None]}  # no trial of t18 has a reward
    indep = passes(INDEP) | {"extra": [1.0, 1.0, 1.0]}
    team = {task: rewards for task, rewards in passes(TEAM).items() if task != "t19"}
    paths = [
        str(arm(tmp_path, "solo", solo)),
        str(arm(tmp_path, "indep", indep, seats=pair())),
        str(arm(tmp_path, "team", team, seats=pair())),
    ]
    report = report_of(paths)
    assert report["shared_tasks"] == 18
    assert report["dropped_tasks"] == {"extra": ["solo", "team"], "t18": ["solo"], "t19": ["team"]}
    # On t00 to t17 the passed trials are 24, 42 - 1 and 45 - 1 of 54.
    assert [a["score"] for a in report["arms"]] == [pytest.approx(x / 54) for x in (24, 41, 44)]
    assert differences(report) == [pytest.approx(x / 54) for x in (17, 3, 20)]
    (finding,) = [f for f in report["findings"] if f["code"] == "task-set-mismatch"]
    assert (finding["severity"], finding["detail"]["count"]) == ("warn", 3)
    start = "3 tasks do not have counted trials in all 3 arms and are left out: extra, t18, t19."
    assert finding["message"].startswith(start)
    assert "  warn  task-set-mismatch: 3 tasks do not have counted trials" in decompose.text(report)
    # When every trial counts, the 3 trials of t18 without a reward score 0, and solo has the task.
    every = report_of(paths, counting=Counting.ALL)
    assert (every["shared_tasks"], sorted(every["dropped_tasks"])) == (19, ["extra", "t19"])
    assert [a["score"] for a in every["arms"]] == [pytest.approx(x / 57) for x in (24, 42, 44)]


def test_the_other_counting_rule_is_shown_when_it_moves_an_effect(tmp_path):
    # In the team, 1 trial of each of t00 to t07 has no reward: 2 of 2 valid trials pass, or 2 of 3 trials.
    broken = passes(TEAM) | {f"t{i:02d}": [1.0, 1.0, None] for i in range(8)}
    paths = [
        str(arm(tmp_path, "solo", passes(SOLO))),
        str(arm(tmp_path, "indep", passes(INDEP), seats=pair())),
        str(arm(tmp_path, "team", broken, seats=pair())),
    ]
    report = report_of(paths)
    assert differences(report) == [pytest.approx(x) for x in (0.30, 0.05, 0.35)]
    alt = report["other_counting"]
    assert alt["trials"] == "all"
    # (45 - 8) / 60 - 42 / 60 for coordination, and (45 - 8) / 60 - 24 / 60 for the total
    assert alt["effects"]["coordination"] == {"difference": pytest.approx(-5 / 60), "verdict": "not resolved"}
    assert alt["effects"]["total"]["difference"] == pytest.approx(13 / 60)
    assert alt["effects"]["headcount"]["difference"] == pytest.approx(0.30)
    # A trial without a valid reward is not counted for the score. It still has a cost, and that counts.
    team = report["arms"][2]
    assert (team["trials"], team["trials_with_cost"], team["trials_on_shared_tasks"]) == (52, 60, 60)
    assert team["cost_per_trial"] == pytest.approx(0.10)
    line = next(x for x in decompose.text(report).splitlines() if x.startswith("other counting rule"))
    assert (
        "(every trial counts, no reward means 0)" in line and "coordination -8.3 pts (not resolved)" in line
    )
    clean = decompose.text(report_of(trio(tmp_path / "clean")))
    assert "other counting rule" not in clean


def test_checks_across_the_3_arms(tmp_path):
    # Different task files and different rosters between the arms are the design: no finding.
    paths = [
        str(arm(tmp_path, "solo", passes(SOLO), checksum="files-of-solo")),
        str(arm(tmp_path, "indep", passes(INDEP), seats=pair(), checksum="files-of-indep")),
        str(arm(tmp_path, "team", passes(TEAM), seats=pair(), checksum="files-of-team")),
    ]
    assert report_of(paths)["findings"] == []
    # A different sandbox in 1 arm is a finding for each pair with that arm.
    paths[2] = str(arm(tmp_path, "cloud", passes(TEAM), seats=pair(), environment="daytona"))
    found = [f for f in report_of(paths)["findings"] if f["code"] == "condition-mismatch"]
    assert [(f["severity"], f["detail"]) for f in found] == [
        ("warn", {"key": "environment", "a": ["docker"], "b": ["daytona"]})
    ] * 2
    assert "in solo and ['daytona'] in cloud." in found[0]["message"]
    assert "in indep and ['daytona'] in cloud." in found[1]["message"]


def test_notes_that_the_text_shows(tmp_path):
    # The team has 2 trials per task, with 2 rosters. In the second roster, p2 raised an exception.
    late = [seat("p1"), seat("p2", model="openai/gpt-5.5", exception="AgentTimeoutError")]
    trials = [seat_trial(task, rewards[0], pair()) for task, rewards in passes(TEAM).items()]
    trials += [seat_trial(task, rewards[1], late) for task, rewards in passes(TEAM).items()]
    solo, indep, _ = trio(tmp_path)
    report = report_of([solo, indep, job(tmp_path, "mixed", trials)])
    found = {(f["code"], f["severity"]) for f in report["findings"] if f["arm"] == "mixed"}
    assert {("mixed-arm", "warn"), ("seat-errors", "note"), ("pass-with-exception", "note")} <= found
    # 2 trials per task in 1 arm and 3 in the others: a note, and the t-test decides for that arm.
    (note,) = [f for f in report["findings"] if f["code"] == "attempts-mismatch"]
    assert (note["severity"], note["arm"], note["detail"]) == ("note", None, {"tasks": 20})
    assert note["message"] == "20 of 20 shared tasks have different numbers of counted trials in the 3 arms."
    assert report["effects"]["headcount"]["rerun"]["test"] == "exact permutation"
    assert report["effects"]["coordination"]["rerun"]["test"] == "t-test"
    shown = [line.split()[:2] for line in decompose.text(report).split("audit:\n")[1].splitlines()]
    assert shown == [["warn", "mixed-arm:"], ["note", "seat-errors:"], ["note", "attempts-mismatch:"]]


def test_an_arm_with_1_trial_per_task_gets_the_weaker_rerun_test(tmp_path):
    # solo has only the first trial of each task: 12 of 20 tasks pass. It shows no trial noise of its own.
    single = {task: rewards[:1] for task, rewards in passes(SOLO).items()}
    _, indep, team = trio(tmp_path)
    report = report_of([str(arm(tmp_path, "single", single)), indep, team])
    head, coordination, total = (report["effects"][name] for name in EFFECTS)
    assert differences(report) == [pytest.approx(x) for x in (0.70 - 0.60, 0.05, 0.75 - 0.60)]
    assert [e["rerun"]["test"] for e in (head, coordination, total)] == [
        "permutation, unequal trial counts",
        "exact permutation",
        "permutation, unequal trial counts",
    ]
    assert (head["rerun"]["interval"], head["rerun"]["detectable"]) == (None, None)
    assert head["rerun"]["p"] is not None and head["tasks"]["interval"] is not None
    lines = decompose.text(report).splitlines()
    assert lines[10].split()[:5] == ["headcount", "+10.0", "pts", "n/a", f"{head['rerun']['p']:.4f}"]
    weaker = (
        "rerun verdict of headcount, total: the trial noise of the 2 arms cannot be compared, "
        'so the test checks only "no change on any task"'
    )
    assert weaker in lines


def test_cost_that_only_a_part_of_the_trials_record(tmp_path):
    # In the team, p2 gives no cost on t00 to t09: the team has a cost only on t10 to t19, $0.40 per trial.
    partial = [seat("p1", cost=0.20), seat("p2", cost=None)]
    team = [
        seat_trial(task, reward, partial if task < "t10" else pair(0.20))
        for task, rewards in passes(TEAM).items()
        for reward in rewards
    ]
    # 1 agent costs $0.01 per trial on t00 to t09 and $0.03 on t10 to t19.
    solo = [
        trial(task, reward, cost=0.01 if task < "t10" else 0.03)
        for task, rewards in passes(SOLO).items()
        for reward in rewards
    ]
    indep = str(arm(tmp_path, "indep", passes(INDEP), seats=pair()))
    report = report_of([job(tmp_path, "solo", solo), indep, job(tmp_path, "team", team)])
    solo_row, indep_row, team_row = report["arms"]
    assert solo_row["cost_per_trial"] == pytest.approx(0.02)
    assert (team_row["cost_per_trial"], team_row["trials_with_cost"]) == (pytest.approx(0.40), 30)
    assert team_row["trials_on_shared_tasks"] == 60
    assert indep_row["cost_ratio"] == pytest.approx(0.10 / 0.02)
    # The ratio of the team uses the tasks with a cost in both arms: t10 to t19, where solo costs $0.03.
    assert team_row["cost_ratio"] == pytest.approx(0.40 / 0.03)
    lines = decompose.text(report).splitlines()
    assert lines[6].split()[-2:] == ["$0.4000*", "13.33"]
    assert "* only a part of the trials of this arm has a cost. The mean covers that part." in lines


def test_text_of_the_readme_example(capsys, tmp_path):
    status, out = run(capsys, *trio(tmp_path))
    assert status == 0
    assert out.splitlines() == README_EXAMPLE.splitlines()
    readme = Path(__file__).parent.parent / "README.md"
    assert (
        "$ tidegauge decompose jobs/solo jobs/indep jobs/team\n" + README_EXAMPLE + "```"
        in readme.read_text()
    )


README_EXAMPLE = """\
metric: reward · 20 shared tasks · only trials with a valid reward count · 95% intervals

arm    seats  tasks  trials  score  rerun  tasks  $/trial  cost ratio
-----  -----  -----  ------  -----  -----  -----  -------  ----------
solo       1     20      60  40.0%   ±9.5  ±19.3  $0.0500        1.00
indep      2     20      60  70.0%  ±10.0  ±16.7  $0.1000        2.00
team       2     20      60  75.0%   ±8.9  ±16.7  $0.1300        2.60

effect        difference  rerun 95% (pts)        p  verdict       tasks 95% (pts)       p  sign-flip p  verdict
------------  ----------  ---------------  -------  ------------  ---------------  ------  -----------  ------------
headcount      +30.0 pts  +16.8 to +43.2   <0.0001  resolved      +15.8 to +44.2   0.0003       0.0005  resolved
coordination    +5.0 pts  -7.8 to +17.8     0.6226  not resolved  -13.4 to +23.4   0.5770       0.7070  not resolved
total          +35.0 pts  +22.6 to +47.4   <0.0001  resolved      +19.4 to +50.6   0.0002       0.0005  resolved

seats: agents in 1 trial · $/trial: mean cost of 1 trial · cost ratio: $/trial over the $/trial of solo
headcount = indep - solo · coordination = team - indep · total = team - solo = headcount + coordination
rerun: the same tasks run again · tasks: a new sample of similar tasks · resolved: p < 0.05 for that question
For tasks, the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < 0.05.
Headcount and coordination share the arm indep, so their 2 estimates are negatively correlated.
Each interval is valid on its own. The table makes no joint claim.

More agents without communication add 30.0 pts (headcount, resolved for rerun and tasks).
Communication adds 5.0 pts to the same agents (coordination, not resolved).
The team with communication is 35.0 pts above 1 agent (total, resolved for rerun and tasks).
Coordination gives 14.3% of the total gain and headcount gives 85.7%. The coordination effect is not resolved, so its share can be 0%.
This design detects a true coordination effect of about 18.0 pts (rerun) or 25.9 pts (tasks).
"""


def test_task_key_pairs_names_that_hold_the_condition(capsys, tmp_path):
    # The 3 arms of the README example, with the condition in every task name: t00-solo, t00-silent, t00-talk.
    def named(name, counts, condition, **kwargs):
        outcomes = {f"flotilla/{task}-{condition}": rewards for task, rewards in passes(counts).items()}
        return str(arm(tmp_path, name, outcomes, **kwargs))

    paths = [
        named("solo", SOLO, "solo", model="deepseek/deepseek-flash"),
        named("indep", INDEP, "silent", seats=pair()),
        named("team", TEAM, "talk", seats=pair(0.065)),
    ]
    assert main(["decompose", *paths]) == 3
    assert (
        "share no task with counted trials. Tasks pair by name, or by --task-key" in capsys.readouterr().err
    )
    status, out = run(capsys, *paths, "--task-key", "^(.*)-(?:solo|silent|talk)$")
    assert status == 0
    assert out.splitlines() == README_EXAMPLE.splitlines()
    # A pattern that misses the solo names: those trials keep their keys, and the 3 arms share no task.
    assert main(["decompose", *paths, "--task-key", "^(.*)-(?:silent|talk)$"]) == 3


def test_free_arm_names_and_mixed_seat_counts(capsys, tmp_path):
    solo, indep, _ = trio(tmp_path)
    trials = [seat_trial(task, rewards[0], pair()) for task, rewards in passes(TEAM).items()]
    trials += [trial(task, rewards[1]) for task, rewards in passes(TEAM).items()]
    status, out = run(capsys, f"s1={solo}", f"s0n={indep}", f"sn={job(tmp_path, 'mixed', trials)}")
    rows = [line.split()[:3] for line in out.splitlines()[4:7]]
    assert status == 0
    assert rows == [["solo:", "s1", "1"], ["indep:", "s0n", "2"], ["team:", "sn", "1-2"]]


def test_json_shape(capsys, tmp_path):
    status, out = run(capsys, *trio(tmp_path), "--json")
    report = json.loads(out)
    assert status == 0
    assert list(report) == [
        "command",
        "metric",
        "trials",
        "level",
        "alpha",
        "shared_tasks",
        "dropped_tasks",
        "arms",
        "effects",
        "coordination_share",
        "other_counting",
        "findings",
    ]
    assert (report["command"], report["metric"], report["trials"]) == ("decompose", "reward", "valid")
    assert (report["level"], report["alpha"], report["findings"]) == (0.95, 0.05, [])
    assert list(report["arms"][0]) == [
        "role",
        "name",
        "seats",
        "tasks",
        "trials",
        "score",
        "ci_rerun",
        "ci_tasks",
        "cost_per_trial",
        "trials_with_cost",
        "trials_on_shared_tasks",
        "cost_ratio",
    ]
    assert list(report["effects"]) == list(EFFECTS)
    for effect in report["effects"].values():
        assert list(effect) == ["baseline", "candidate", "difference", "rerun", "tasks"]
        assert list(effect["rerun"]) == ["se", "interval", "p", "verdict", "detectable", "test"]
        tasks = ["se", "interval", "p", "verdict", "detectable", "p_signflip", "signflip_exact"]
        assert list(effect["tasks"]) == tasks
        assert isinstance(effect["tasks"]["p_signflip"], float) and effect["tasks"]["signflip_exact"] is True
    # No trial lacks a reward, so the other counting rule gives the same 3 effects.
    brief = {
        n: {"difference": e["difference"], "verdict": e["rerun"]["verdict"]}
        for n, e in report["effects"].items()
    }
    assert report["other_counting"] == {"trials": "all", "effects": brief}


def test_level_sets_the_intervals_and_the_verdicts(tmp_path):
    # At the level 99.99%, alpha is 0.0001. The paired t over tasks of the total has p = 0.0002.
    report = report_of(trio(tmp_path), level=0.9999)
    assert report["alpha"] == pytest.approx(0.0001)
    total = report["effects"]["total"]
    assert (total["rerun"]["verdict"], total["tasks"]["verdict"]) == ("resolved", "not resolved")
    text = decompose.text(report)
    assert "(total, resolved for rerun, not for tasks)" in text
    assert "resolved: p < 0.0001 for that question" in text and "99.99% intervals" in text


def test_exit_status(capsys, tmp_path):
    solo, indep, team = trio(tmp_path)
    assert run(capsys, solo, indep, team)[0] == 0
    assert main(["decompose", solo, indep, str(tmp_path / "missing")]) == 3
    assert "not a directory" in capsys.readouterr().err
    apart = str(arm(tmp_path, "apart", {"other-task": [1.0, 0.0]}, seats=pair()))
    assert main(["decompose", solo, indep, apart]) == 3
    assert "the 3 arms share no task with counted trials" in capsys.readouterr().err
    with pytest.raises(SystemExit) as wrong:
        main(["decompose", solo, indep])  # 2 paths
    assert wrong.value.code == 2
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="no trials found"):
        main(["decompose", solo, indep, str(tmp_path / "empty")])


def test_the_credit_of_1_seat(capsys, tmp_path):
    def credit(name, counts):
        trials = [
            seat_trial(task, None, pair(), rewards={"reward": 1.0, "seat.p1": value, "seat.p2": 0.0})
            for task, values in passes(counts).items()
            for value in values
        ]
        return job(tmp_path, name, trials)

    paths = [credit("solo", SOLO), credit("indep", INDEP), credit("team", TEAM)]
    for option in ("--reward-key", "--metric"):
        status, out = run(capsys, *paths, option, "seat.p1", "--json")
        assert status == 0
        assert differences(json.loads(out)) == [pytest.approx(x) for x in (0.30, 0.05, 0.35)]
    # The score of the team is 1 in every trial: no difference, and no trial noise for an interval.
    report = json.loads(run(capsys, *paths, "--json")[1])
    assert differences(report) == [0.0, 0.0, 0.0]
    assert report["effects"]["total"]["rerun"]["interval"] is None
    lines = decompose.text(report).splitlines()
    assert UNMEASURED in lines
    # Every task differs by 0: a detectable effect of 0 is not a size, so the text shows none.
    assert report["effects"]["coordination"]["tasks"]["detectable"] == 0.0
    assert not any(line.startswith("This design detects") for line in lines)


UNMEASURED = (
    "The rerun noise of the coordination effect is not measured: it needs 2 or more trials per task "
    "and trials that disagree."
)


def test_1_trial_per_task_gives_the_tasks_question_only(tmp_path):
    # The first trial of each task. indep passes 18 of 20 (not t17, t19), team 18 of 20 (not t14, t18).
    counts = zip(decompose.ROLES, (SOLO, INDEP, TEAM), strict=True)
    paths = [str(arm(tmp_path, role, {t: r[:1] for t, r in passes(c).items()})) for role, c in counts]
    coordination = report_of(paths)["effects"]["coordination"]
    # team - indep is -1 on t14 and t18 and +1 on t17 and t19: sum 0, sum of squares 4.
    se = math.sqrt(4 / 19 / 20)
    assert (coordination["difference"], coordination["tasks"]["se"]) == (0.0, pytest.approx(se))
    assert coordination["rerun"]["detectable"] is None
    assert coordination["tasks"]["detectable"] == pytest.approx((T975[19] + Z80) * se, abs=NEAR)
    lines = decompose.text(report_of(paths)).splitlines()
    assert "This design detects a true coordination effect of about 30.1 pts (tasks)." in lines
    assert UNMEASURED in lines


def test_a_pilot_with_1_task_and_1_trial_per_arm(tmp_path):
    # The shape of a real pilot: 1 seat, 3 seats without a channel, 3 seats with a channel, 1 trial each.
    def one(name, reward, seats, cost):
        team = [seat(f"p{i + 1}", cost=cost) for i in range(seats)]
        return job(tmp_path, name, [seat_trial("muster/boxes-i01", reward, team)])

    report = report_of([one("solo", 0.25, 1, 0.05), one("indep", 0.75, 3, 0.05), one("team", 0.9, 3, 0.065)])
    assert [(a["seats"], a["score"]) for a in report["arms"]] == [
        ([1, 1], 0.25),
        ([3, 3], 0.75),
        ([3, 3], 0.9),
    ]
    assert [a["cost_ratio"] for a in report["arms"]] == [1.0, pytest.approx(3.0), pytest.approx(3.9)]
    assert differences(report) == [pytest.approx(x) for x in (0.50, 0.15, 0.65)]
    # Every relabelling of 1 trial against 1 trial gives the same difference: p = 1. 1 task gives no tasks test.
    for effect in report["effects"].values():
        assert (effect["rerun"]["p"], effect["rerun"]["verdict"], effect["rerun"]["interval"]) == (
            1.0,
            "not resolved",
            None,
        )
        assert (effect["tasks"]["p"], effect["tasks"]["verdict"]) == (None, "cannot tell")
    assert report["coordination_share"] is None
    lines = decompose.text(report).splitlines()
    assert lines[0].startswith("metric: reward · 1 shared task · ")
    assert UNMEASURED in lines and "The tasks question needs 2 or more shared tasks." in lines
    assert "The share of coordination is not defined: the total effect is not resolved for rerun." in lines


def test_a_tasks_verdict_needs_the_sign_flip_test_too(tmp_path):
    # The shape of a real run: 12 tasks, 1 trial per task. solo passes 6 tasks, indep none, and team 4.
    paths = [
        single(tmp_path, "solo", range(6)),
        single(tmp_path, "indep", (), seats=pair()),
        single(tmp_path, "team", range(4), seats=pair()),
    ]
    report = report_of(paths)
    head, coordination, total = (report["effects"][name] for name in EFFECTS)
    # coordination is +1 on 4 tasks and 0 on 8: t^2 = (1/9) / (2/99) = 5.5 with 11 degrees of freedom, so
    # p = 0.0388 (closed form, A&S 26.7.3). The sign-flip test sees 4 differences of +1: p = 2/16.
    assert (coordination["difference"], coordination["tasks"]["p"]) == (
        pytest.approx(1 / 3),
        pytest.approx(0.0388, abs=5e-5),
    )
    assert (coordination["tasks"]["p_signflip"], coordination["tasks"]["verdict"]) == (0.125, "not resolved")
    # headcount is -1 on 6 tasks: t^2 = 11 with 11 degrees of freedom, p = 0.0069. The sign-flip p is 2/64.
    assert head["tasks"]["p"] == pytest.approx(0.0069, abs=5e-5)
    assert (head["tasks"]["p_signflip"], head["tasks"]["verdict"]) == (2 / 64, "resolved")
    # total is -1 on t04 and t05 and 0 elsewhere: the sign-flip p is 2/4.
    assert (total["tasks"]["p_signflip"], total["tasks"]["verdict"]) == (0.5, "not resolved")
    # With 1 trial per task in every arm, the exact permutation test of the rerun question is the sign-flip test.
    assert [e["rerun"]["p"] for e in (head, coordination, total)] == [2 / 64, 0.125, 0.5]
    lines = decompose.text(report).splitlines()
    assert lines[11].split()[-5:] == ["+64.6", "0.0388", "0.1250", "not", "resolved"]
    assert (
        "For tasks, the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < 0.05."
        in lines
    )
    assert "Communication adds 33.3 pts to the same agents (coordination, not resolved)." in lines
    assert not any("has width 0" in line for line in lines)


def test_a_tasks_interval_of_width_0(tmp_path):
    # solo passes every task and indep none: headcount is -1 on every task, so se_tasks = 0, the interval
    # has width 0 and the t-test gives p = 0. Only the sign-flip test counts the tasks: p = 2 / 2^n.
    for n, verdict in ((5, "not resolved"), (6, "resolved")):
        root = tmp_path / str(n)
        paths = [
            single(root, "solo", range(n), n),
            single(root, "indep", (), n, seats=pair()),
            single(root, "team", range(n - 1), n, seats=pair()),
        ]
        report = report_of(paths)
        head = report["effects"]["headcount"]["tasks"]
        assert (head["interval"], head["p"], head["p_signflip"]) == ([-1.0, -1.0], 0.0, 2 / 2**n)
        assert head["verdict"] == verdict
        lines = decompose.text(report).splitlines()
        assert WIDTH_0.format("headcount") in lines
        assert lines[10].startswith("headcount") and "-100.0 to -100.0  <0.0001" in lines[10]
