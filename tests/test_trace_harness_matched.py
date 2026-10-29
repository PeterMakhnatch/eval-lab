"""Length-matched evaluation on synthetic data: caliper boundary, order, ties, without replacement, unmatched."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import pytest

from evallab.production import trace_harness_length as length
from evallab.production import trace_harness_matched as matched
from evallab.production.trace_harness import finding

DRAWS = length.resample_weights(["a", "b", "c", "d"], 50, 1)


def item(run_id: str, size: int, label: bool, task: str, family: str = "f") -> length.Item:
    return length.Item(run_id, size, label, task, task, family)


def by_task(i: length.Item) -> str:
    return i.cluster


def by_family(i: length.Item) -> str:
    return i.family


def pairs_of(result: matched.Matching) -> set[tuple[str, str]]:
    return {(e.run_id, c.run_id) for e, c in result.pairs}


def sha(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# caliper
# --------------------------------------------------------------------------- #


def test_caliper_is_relative_to_the_enacted_length_and_inclusive() -> None:
    assert matched.within_caliper(100, 110)
    assert matched.within_caliper(100, 90)
    assert not matched.within_caliper(100, 111)
    assert not matched.within_caliper(100, 89)
    # relative to the ENACTED length: 11 of 110 is 10% of 110 but the clean run is 100 (diff 10 of enacted 110 passes) ...
    assert matched.within_caliper(110, 99)
    # ... while enacted 100 vs clean 111 fails although 11 is 10% of the clean length
    assert not matched.within_caliper(100, 111)
    # integer arithmetic: no float rounding at the boundary (7 * 10% = 0.7, so only an equal length matches)
    assert matched.within_caliper(7, 7)
    assert not matched.within_caliper(7, 8)
    assert matched.within_caliper(10, 11)
    assert not matched.within_caliper(10, 12)


def test_boundary_pair_matches_and_one_over_does_not() -> None:
    items = [item("e1", 100, True, "a"), item("c-in", 110, False, "a"), item("e2", 100, True, "b"),
             item("c-out", 111, False, "b")]
    result = matched.match_pairs(items, by_task, design="primary")
    assert pairs_of(result) == {("e1", "c-in")}
    assert result.unmatched == {"e2": "none_within_caliper"}


# --------------------------------------------------------------------------- #
# nearest, ties, order, without replacement
# --------------------------------------------------------------------------- #


def test_takes_the_nearest_length_candidate_of_the_same_task_only() -> None:
    items = [item("e", 100, True, "a"), item("far", 95, False, "a"), item("near", 102, False, "a"),
             item("other-task", 100, False, "b")]
    assert pairs_of(matched.match_pairs(items, by_task, design="primary")) == {("e", "near")}


def test_equal_distance_ties_go_to_the_lower_candidate_sha256() -> None:
    items = [item("e", 100, True, "a"), item("c1", 95, False, "a"), item("c2", 105, False, "a")]
    chosen = min(("c1", "c2"), key=sha)
    assert pairs_of(matched.match_pairs(items, by_task, design="primary")) == {("e", chosen)}


def test_a_clean_run_is_used_once_and_enacted_order_is_the_sha256_order() -> None:
    # two enacted runs compete for one clean run: the one with the smaller sha256 is served first
    items = [item("e1", 100, True, "a"), item("e2", 100, True, "a"), item("c", 100, False, "a")]
    first = min(("e1", "e2"), key=sha)
    second = "e2" if first == "e1" else "e1"
    result = matched.match_pairs(items, by_task, design="primary")
    assert pairs_of(result) == {(first, "c")}
    assert result.unmatched == {second: "candidates_consumed"}
    # the result does not depend on the input order
    assert pairs_of(matched.match_pairs(list(reversed(items)), by_task, design="primary")) == {(first, "c")}


def test_greedy_is_not_a_global_optimum_and_that_is_the_stated_rule() -> None:
    # e_early (sha-first) takes the nearer clean run even though e_late would then lose its only candidate
    names = sorted(("p", "q"), key=sha)
    early, late = names
    sizes = {early: 100, late: 104}
    items = [item(early, sizes[early], True, "a"), item(late, sizes[late], True, "a"), item("c", 102, False, "a")]
    result = matched.match_pairs(items, by_task, design="primary")
    assert pairs_of(result) == {(early, "c")}
    assert result.unmatched == {late: "candidates_consumed"}


# --------------------------------------------------------------------------- #
# unmatched reasons and designs
# --------------------------------------------------------------------------- #


def test_unmatched_reasons_partition_the_enacted_runs() -> None:
    items = [item("e-none", 50, True, "t1"),
             item("e-far", 50, True, "t2"), item("c-far", 80, False, "t2"),
             item("e-ok", 50, True, "t3"), item("c-ok", 52, False, "t3"),
             item("e-late", 50, True, "t3")]
    result = matched.match_pairs(items, by_task, design="primary")
    reasons = {run: reason for run, reason in result.unmatched.items()}
    assert reasons["e-none"] == "no_clean_in_pool"
    assert reasons["e-far"] == "none_within_caliper"
    assert len(result.pairs) + len(result.unmatched) == 4
    # one of the two t3 enacted runs is matched and the other has its candidate consumed
    assert sorted(v for k, v in reasons.items() if k.startswith("e-ok") or k == "e-late") == ["candidates_consumed"]


def test_family_design_ignores_the_task_but_keeps_the_family() -> None:
    items = [item("e", 100, True, "a", "fam1"), item("c-same-family", 100, False, "z", "fam1"),
             item("c-other-family", 100, False, "a", "fam2")]
    assert pairs_of(matched.match_pairs(items, by_task, design="primary")) == {("e", "c-other-family")}
    assert pairs_of(matched.match_pairs(items, by_family, design="secondary")) == {("e", "c-same-family")}


def test_run_unique_task_keys_give_no_primary_pair() -> None:
    items = [item("e", 100, True, "e"), item("c", 100, False, "c")]
    result = matched.match_pairs(items, by_task, design="primary")
    assert not result.pairs
    assert result.unmatched == {"e": "no_clean_in_pool"}


def test_secondary_is_used_only_when_the_primary_is_small() -> None:
    items = [item(f"e{n}", 100, True, f"t{n}") for n in range(40)] \
        + [item(f"c{n}", 100, False, f"t{n}") for n in range(40)]
    tools = {"x": {i.run_id: (i.label, 1.0 if i.label else 0.0) for i in items}}
    big = matched.analyse_matched(items, tools, {"x": 80}, primary_key=by_task, secondary_key=by_family,
                                  resamples=20, seed=1, with_verdict=False)
    assert big["matching"]["used"] == "primary"
    assert big["matching"]["primary"]["pairs"] == 40
    small = matched.analyse_matched(items[:20] + items[40:60], {"x": {}}, {}, primary_key=by_task,
                                    secondary_key=by_family, resamples=20, seed=1, with_verdict=False)
    assert small["matching"]["primary"]["pairs"] == 20
    assert small["matching"]["used"] == "secondary"


# --------------------------------------------------------------------------- #
# matched-set statistics
# --------------------------------------------------------------------------- #


def test_sign_test_is_exact_and_one_sided_counts_the_enacted_tail() -> None:
    assert matched.sign_test(0, 0)["p_one_sided"] is None
    t = matched.sign_test(5, 0)
    assert t["p_one_sided"] == pytest.approx(1 / 32)
    assert t["p_two_sided"] == pytest.approx(2 / 32)
    t = matched.sign_test(3, 3)
    assert t["p_one_sided"] == pytest.approx(42 / 64)
    assert t["p_two_sided"] == 1.0
    assert matched.sign_test(1, 4)["p_one_sided"] == pytest.approx(31 / 32)


def make_matching() -> matched.Matching:
    pairs = tuple(
        (item(f"e{n}", 100, True, f"t{n}"), item(f"c{n}", 100 + n % 3, False, f"t{n}")) for n in range(6))
    return matched.Matching("primary", pairs, {})


def test_matched_block_counts_pairs_and_requires_both_members() -> None:
    matching = make_matching()
    # tool fires on e0..e3 and on c0 only; c5 and e5 are abstained (no stored row), so pair 5 is unusable
    series = {f"e{n}": (n < 4, 1.0 if n < 4 else 0.0) for n in range(5)}
    series |= {f"c{n}": (n == 0, 1.0 if n == 0 else 0.0) for n in range(5)}
    block = matched.matched_block(matching, series, DRAWS, DRAWS)
    assert (block["pairs_total"], block["usable_pairs"]) == (6, 5)
    t = block["mcnemar"]
    assert (t["enacted_only"], t["clean_only"], t["both"], t["neither"]) == (3, 0, 1, 1)
    assert block["recall"]["k"] == 4 and block["recall"]["n"] == 5
    assert block["far"]["k"] == 1
    assert t["p_one_sided"] == pytest.approx(1 / 8)
    # pair AUROC counts a tie as one half: e0/c0 tie, e1..e3 win, e4 ties with c4 at 0
    assert block["pair_auroc"]["point"] == pytest.approx((0.5 + 3 + 0.5) / 5)
    assert 0 <= block["length_pair_auroc"] <= 1


def test_matched_block_with_no_usable_pair_reports_zero() -> None:
    block = matched.matched_block(make_matching(), {"e0": (True, 1.0)}, DRAWS, DRAWS)
    assert block == {"pairs_total": 6, "usable_pairs": 0}


def test_describe_matching_summarises_lengths_and_reasons() -> None:
    items = [item("e1", 100, True, "a"), item("c1", 108, False, "a"), item("e2", 50, True, "b")]
    result = matched.match_pairs(items, by_task, design="primary")
    summary = matched.describe_matching(result, items)
    assert summary["pairs"] == 1 and summary["unmatched_enacted"] == 1
    assert summary["unmatched_reasons"] == {"no_clean_in_pool": 1}
    assert summary["length_diff_median"] == 8 and summary["relative_diff_max"] == pytest.approx(0.08)


def test_tertiles_use_nearest_rank_cuts_and_three_strata() -> None:
    plan = matched.tertiles([item(f"r{n}", n, bool(n % 2), f"t{n}") for n in range(1, 10)])
    assert plan.k == 3 and plan.cuts == (3, 6)
    assert [plan.stratum(v) for v in (3, 4, 6, 7)] == [1, 2, 2, 3]


# --------------------------------------------------------------------------- #
# decision rule
# --------------------------------------------------------------------------- #


def matched_entry(usable: int, e_fire: int, c_fire: int, p: float | None) -> dict[str, object]:
    return {"usable_pairs": usable, "recall": {"k": e_fire, "n": usable}, "far": {"k": c_fire, "n": usable},
            "mcnemar": {"p_one_sided": p}}


OUTSIDE_NO = {"outside_longest_tertile": {"estimable": False}}


def test_path_a_needs_100_primary_pairs_target_and_a_significant_sign_test() -> None:
    ok = matched.decide(OUTSIDE_NO, matched_entry(100, 15, 3, 0.004), design="primary")
    assert ok["measurable"] and ok["path_a_target_on_matched"] and ok["credited"]
    for entry in (matched_entry(99, 15, 3, 0.004), matched_entry(100, 14, 3, 0.004),
                  matched_entry(100, 15, 4, 0.004), matched_entry(100, 15, 3, 0.05)):
        assert not matched.decide(OUTSIDE_NO, entry, design="primary")["path_a_target_on_matched"]
    # a secondary (family) match is never a target test
    weak = matched.decide(OUTSIDE_NO, matched_entry(300, 100, 0, 1e-9), design="secondary")
    assert not weak["measurable"] and not weak["credited"]


def test_path_b_needs_more_true_alarms_than_length_and_a_positive_paired_lower_bound() -> None:
    def outside(tool_tp: int, length_tp: int, lo: float | None) -> dict[str, object]:
        return {"outside_longest_tertile": {"estimable": True, "tool_tp": tool_tp, "length_tp": length_tp,
                                            "auroc_minus_length": {"lo": lo}}}
    empty = {"usable_pairs": 0}
    assert matched.decide(outside(5, 4, 0.01), empty, design="primary")["path_b_beats_length_outside_longest"]
    assert not matched.decide(outside(4, 4, 0.2), empty, design="primary")["path_b_beats_length_outside_longest"]
    assert not matched.decide(outside(5, 4, 0.0), empty, design="primary")["path_b_beats_length_outside_longest"]
    assert not matched.decide(outside(5, 4, None), empty, design="primary")["path_b_beats_length_outside_longest"]


def test_verdict_states() -> None:
    def result(point: float, lo: float, credited: bool) -> dict[str, object]:
        return {"length_auroc_population": {"point": point, "lo": lo},
                "tools": {"full": {"decision": {"credited": credited}}, "part": {"decision": {"credited": True}}}}
    coverage = {"full": 10, "part": 3, "full+abstain_as_clear": 10}
    assert matched.verdict(result(0.7, 0.6, False), coverage, 10)["state"] == "confirmed"
    assert matched.verdict(result(0.7, 0.6, True), coverage, 10)["state"] == "refuted"
    assert matched.verdict(result(0.55, 0.52, False), coverage, 10)["state"] == "mixed"
    assert matched.verdict(result(0.7, 0.4, False), coverage, 10)["state"] == "mixed"
    assert matched.verdict(result(0.7, 0.6, False), coverage, 10)["full_coverage_tools"] == ["full"]


# --------------------------------------------------------------------------- #
# population block and loading helpers
# --------------------------------------------------------------------------- #


def test_population_block_reports_length_only_baseline_and_outside_longest() -> None:
    items = [item(f"{'e' if lab else 'c'}{n}-{size}", size, lab, f"t{n}-{size}{lab}")
             for size in (10, 50, 90) for lab in (True, False) for n in range(12)]
    series = {i.run_id: (i.length >= 50 and i.label, 1.0 if i.length >= 50 and i.label else 0.0) for i in items}
    plan = matched.tertiles(items)
    draws = length.resample_weights([i.cluster for i in items], 30, 1)
    block = matched.population_block(items, series, plan, draws)
    assert block["n_evaluated"] == len(items)
    assert block["native"]["far"]["k"] == 0
    assert block["length_only_at_native_far"]["fp"] == 0
    assert block["at_far_0.03"]["length_only"]["max_false_alarms"] == math.floor(0.03 * 36)
    outside = block["outside_longest_tertile"]
    assert outside["estimable"] and outside["tool_tp"] == 12 and outside["n"] == 48
    assert matched.population_block(items, {}, plan, draws) == {"n_evaluated": 0}


def record(run_id: str, verdict: str, score: float | None, at: str) -> dict[str, object]:
    return finding(run_id, "d", detector_version="v1", reader_model=None, verdict=verdict, kind="cheat",
                   score=score, produced_at=at)


def test_collect_all_tools_adds_abstain_as_clear_and_merges_stores(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    first.write_text(json.dumps(record("r1", "alarm", 0.9, "2026-10-01T00:00:00Z")) + "\n"
                     + json.dumps(record("r2", "abstain", None, "2026-10-01T00:00:00Z")) + "\n")
    second.write_text(json.dumps(record("r3", "clear", 0.1, "2026-10-01T00:00:00Z")) + "\n"
                      + json.dumps(record("r4", "alarm", 1.0, "2026-12-01T00:00:00Z")) + "\n")
    results = tmp_path / "results.jsonl"
    results.write_text("")
    items = [item(r, 10, n % 2 == 0, f"t{n}") for n, r in enumerate(("r1", "r2", "r3", "r4"))]
    tools, coverage = matched.collect_all_tools(
        items, "goodfire", findings=[first, second], results_v1=results,
        cutoff=datetime.fromisoformat("2026-11-01T00:00:00+00:00"))
    assert tools["d@v1:none"] == {"r1": (True, 0.9), "r3": (False, 0.1)}  # abstain excluded, r4 is after the cutoff
    assert tools["d@v1:none" + matched.ABSTAIN_SUFFIX] == {"r1": (True, 0.9), "r2": (False, 0.0), "r3": (False, 0.1)}
    assert coverage["d@v1:none"] == 3
