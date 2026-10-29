from __future__ import annotations

from evallab.production.trace_harness_fused import (
    BASELINE,
    RULES_A,
    RULES_B,
    Run,
    alarm,
    cross_validate,
    folds,
    length_control,
    length_only,
    metrics,
    pick,
    strata,
)

BY_NAME = {rule.name: rule for rule in (*RULES_A, *RULES_B)}


def reading(**overrides):
    base = {"v8": False, "susp": 0, "ama": False, "tripwire": False, "qa_t0": False, "metr": False, "hodo10": False}
    return {**base, **overrides}


def run(run_id, label, length=10, group=None, **overrides):
    return Run(run_id, label, group or run_id, length, reading(**overrides))


def test_rule_predicates_and_missing_members():
    a1, a2, a3, a4 = (BY_NAME[name] for name in ("A1", "A2", "A3", "A4"))
    assert alarm(a1, run("r", True, susp=2)) and not alarm(a1, run("r", True, susp=1))
    assert alarm(a1, run("r", True, v8=True))
    assert alarm(a2, run("r", True, susp=2, ama=True)) and not alarm(a2, run("r", True, susp=2))
    assert alarm(a3, run("r", True, v8=True, tripwire=True)) and not alarm(a3, run("r", True, susp=3))
    assert alarm(a3, run("r", True, susp=2, tripwire=True)) and not alarm(a3, run("r", True, v8=True))
    assert alarm(a4, run("r", True, qa_t0=True)) and not alarm(a4, run("r", True, susp=3))
    b2 = BY_NAME["B2"]
    assert alarm(b2, run("r", True, hodo10=True, susp=2)) and not alarm(b2, run("r", True, hodo10=True, susp=1))
    missing = Run("r", True, "r", 5, {"v8": None, "susp": 1})
    assert alarm(a1, missing) is None and alarm(BY_NAME["A4"], Run("r", True, "r", 5, reading())) is False


def test_metrics_exclude_unevaluable_runs():
    runs = [run("p1", True, v8=True), run("p2", True), run("n1", False, v8=True), run("n2", False),
            Run("x", True, "x", 3, {"v8": None})]
    m = metrics(BASELINE, runs)
    assert (m["recall"]["k"], m["recall"]["n"], m["far"]["k"], m["far"]["n"], m["excluded"]) == (1, 2, 1, 2, 1)


def test_pick_prefers_feasible_over_higher_recall():
    # 50 clean runs: 1 clean alarm is 2% (feasible), 2 is 4% (not).
    runs = [run(f"p{i}", True, susp=2 if i < 3 else 0, v8=i == 0) for i in range(10)]
    runs += [run(f"n{i}", False, v8=i == 0, susp=2 if i == 1 else 0) for i in range(50)]
    assert metrics(BY_NAME["A1"], runs)["far"]["k"] == 2 and metrics(BY_NAME["A1"], runs)["recall"]["k"] == 3
    chosen, ok = pick((BY_NAME["A1"], BY_NAME["A4"]), runs)
    assert ok and chosen.name == "A4"
    # Both feasible: the higher-recall rule wins, and the lower rule number breaks an exact tie.
    easy = [run(f"p{i}", True, susp=2 if i < 3 else 0, v8=i == 0) for i in range(10)] + [run("n", False)]
    assert pick((BY_NAME["A4"], BY_NAME["A1"]), easy)[0].name == "A1"
    assert pick((BASELINE, BY_NAME["A4"]), easy)[0].name == "V8"


def test_pick_infeasible_takes_fewest_clean_alarms():
    runs = [run(f"p{i}", True, v8=True) for i in range(4)]
    runs += [run(f"n{i}", False, v8=i < 3, qa_t0=i in (3, 4)) for i in range(10)]
    chosen, ok = pick((BY_NAME["A4"], BASELINE), runs)
    assert not ok and chosen.name == "V8"  # 3 clean alarms vs A4's 5


def test_folds_are_deterministic_and_group_whole():
    runs = [run(f"r{i}", i % 2 == 0, group=f"g{i // 2}") for i in range(40)]
    first, second = folds(runs), folds(runs)
    assert [[r.run_id for r in part] for part in first] == [[r.run_id for r in part] for part in second]
    seen = {}
    for number, part in enumerate(first):
        for item in part:
            assert seen.setdefault(item.group, number) == number
    assert sum(len(part) for part in first) == 40


def test_cross_validate_pools_held_out_predictions():
    runs = [run(f"p{i}", True, v8=i < 4) for i in range(10)] + [run(f"n{i}", False) for i in range(60)]
    result = cross_validate((BASELINE,), runs)
    assert result["out_of_fold"]["recall"]["k"] == 4 and result["out_of_fold"]["far"]["k"] == 0
    assert len(result["folds"]) == 5 and all(f["feasible"] for f in result["folds"])


def test_length_control_flags_gain_in_short_runs():
    long = [run(f"pl{i}", True, length=60, v8=True) for i in range(3)]
    short_added = [run("ps", True, length=10, susp=2)]
    others = [run(f"pm{i}", True, length=25) for i in range(2)]
    clean = [run(f"n{i}", False, length=10 + i) for i in range(30)]
    runs = long + short_added + others + clean
    rule = BY_NAME["A1"]
    control = length_control(runs, rule)
    assert control["added_true_alarms"] == 1 and control["added_with_length_le_cut"] == 1
    assert control["length_only_matched"]["threshold"] is not None and control["length_only_matched"]["threshold"] > 28
    assert control["N1"] and control["N2"] and control["not_explained_by_length"]
    table = strata(runs, rule)
    assert [s["stratum"] for s in table] == [1, 2, 3]
    assert table[2]["enacted"] == 3 and table[0]["rule"]["recall"]["k"] == 1


def test_length_only_respects_false_alarm_budget():
    runs = [run("p1", True, length=50), run("p2", True, length=40), run("n1", False, length=45), run("n2", False, length=5)]
    zero = length_only(runs, 0)
    one = length_only(runs, 1)
    assert zero["threshold"] == 50 and zero["recall"]["k"] == 1 and zero["far"]["k"] == 0
    assert one["threshold"] == 40 and one["recall"]["k"] == 2 and one["far"]["k"] == 1
