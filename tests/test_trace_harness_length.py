"""Length-control statistics on synthetic data: boundaries, ties, empty strata, abstain exclusion."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from evallab.production import trace_harness_length as length
from evallab.production.trace_harness import finding


def item(run_id: str, size: int, label: bool, cluster: str | None = None) -> length.Item:
    return length.Item(run_id, size, label, cluster or run_id, cluster or run_id)


def population(per_cell: int, sizes: tuple[int, ...]) -> list[length.Item]:
    """``per_cell`` enacted and clean runs at each length in ``sizes``."""
    return [
        item(f"{label}-{size}-{n}", size, label)
        for size in sizes for label in (True, False) for n in range(per_cell)
    ]


# --------------------------------------------------------------------------- #
# AUROC, percentile, best cut
# --------------------------------------------------------------------------- #


def test_auroc_orders_ties_and_missing_classes() -> None:
    assert length.weighted_auroc([3, 4], [False, True]) == 1.0
    assert length.weighted_auroc([4, 3], [False, True]) == 0.0
    assert length.weighted_auroc([1, 1, 1, 1], [True, False, True, False]) == 0.5
    # positives 2, 3 against negatives 2, 1: one tie (0.5) and three wins
    assert length.weighted_auroc([2, 2, 3, 1], [True, False, True, False]) == pytest.approx(0.875)
    assert length.weighted_auroc([1, 2], [True, True]) is None
    assert length.weighted_auroc([], []) is None


def test_auroc_weights_are_multiplicities() -> None:
    scores, labels = [1.0, 2.0, 3.0, 2.0], [False, True, True, False]
    expanded_s = [*scores, 2.0, 2.0]
    expanded_l = [*labels, True, True]
    # run index 1 counted three times
    assert length.weighted_auroc(scores, labels, [1, 3, 1, 1]) == pytest.approx(
        length.weighted_auroc(expanded_s, expanded_l))
    # a zero weight drops the run, and a dropped class gives None
    assert length.weighted_auroc(scores, labels, [1, 0, 0, 1]) is None


def test_percentile_interpolates_linearly() -> None:
    assert length.percentile([1, 2, 3, 4], 0) == 1
    assert length.percentile([1, 2, 3, 4], 100) == 4
    assert length.percentile([1, 2, 3, 4], 50) == 2.5
    assert length.percentile([5], 97.5) == 5


def test_best_cut_respects_ties_and_budget() -> None:
    scores = [5.0, 4.0, 4.0, 3.0, 1.0]
    labels = [True, True, False, True, False]
    # 4.0 is a tie group of one positive and one negative: admitted only with a false-alarm budget of 1
    assert length.best_cut(scores, labels, 0) == (5.0, 1, 0)
    assert length.best_cut(scores, labels, 1) == (3.0, 3, 1)
    assert length.best_cut(scores, labels, 2) == (1.0, 3, 2)
    assert length.best_cut([1.0, 1.0], [False, True], 0) == (None, 0, 0)  # never alarming is admissible
    assert length.best_cut([], [], 3) == (None, 0, 0)


# --------------------------------------------------------------------------- #
# strata
# --------------------------------------------------------------------------- #


def test_strata_prefer_quartiles_and_cut_at_nearest_rank() -> None:
    items = population(10, (10, 20, 30, 40))
    plan = length.choose_strata(items)
    assert (plan.k, plan.cuts) == (4, (10, 20, 30))
    # a length equal to a cut point belongs to the lower stratum, one above it to the next
    assert [plan.stratum(x) for x in (10, 11, 20, 21, 30, 31, 99)] == [1, 2, 2, 3, 3, 4, 4]


def test_strata_fall_back_when_a_cell_is_under_ten() -> None:
    items = population(10, (10, 20, 30, 40))
    # enacted cell of stratum 1 -> 9, one extra clean run keeps the total at 80
    items.remove(next(i for i in items if i.label and i.length == 10))
    items.append(item("extra-clean", 10, False))
    plan = length.choose_strata(items)
    assert plan.k == 3
    assert [t["ok"] for t in plan.tried] == [False, True]
    assert plan.tried[0]["smallest_cell"] == 9


def test_strata_fall_back_to_halves_then_overall_only() -> None:
    halves = length.choose_strata(population(10, (10, 20)))
    assert (halves.k, halves.cuts) == (2, (10,))
    small = length.choose_strata(population(9, (10, 20)))
    assert (small.k, small.cuts) == (1, ())  # no k qualifies: overall only
    assert [t["k"] for t in small.tried] == [4, 3, 2]


def test_strata_with_ties_never_leave_an_empty_stratum() -> None:
    # every run has the same length: cut points coincide, strata 2.. would be empty
    plan = length.choose_strata(population(30, (7,)))
    assert plan.k == 1
    assert all(t["smallest_cell"] == 0 for t in plan.tried)


# --------------------------------------------------------------------------- #
# clustered bootstrap
# --------------------------------------------------------------------------- #


def test_resample_weights_are_seeded_and_conserve_cluster_count() -> None:
    clusters = [f"t{i}" for i in range(7)]
    first = length.resample_weights(clusters, 20, seed=3)
    assert first == length.resample_weights(clusters, 20, seed=3)
    assert first != length.resample_weights(clusters, 20, seed=4)
    assert all(sum(draw.values()) == 7 for draw in first)


def test_clustered_interval_counts_skipped_resamples() -> None:
    # one enacted run among 9 clean ones: draws without the enacted cluster cannot be scored
    items = [item("pos", 9, True, "t0"), *(item(f"neg{i}", i, False, f"t{i + 1}") for i in range(9))]
    draws = length.resample_weights([i.cluster for i in items], 3000, seed=1)
    block = length._auroc_block(items, length.everyone(items), draws, length=True)
    assert block["valid"] + block["skipped"] == 3000
    assert block["skipped"] > 0
    assert block["point"] == 1.0
    assert block["lo"] == 1.0 and block["hi"] == 1.0


def test_interval_is_withheld_below_the_valid_resample_floor() -> None:
    items = [item("pos", 9, True, "t0"), item("neg", 1, False, "t1")]
    draws = length.resample_weights([i.cluster for i in items], 50, seed=1)  # fewer than MIN_VALID draws
    block = length._auroc_block(items, length.everyone(items), draws, length=True)
    assert block["point"] == 1.0 and block["lo"] is None and block["hi"] is None


def test_bootstrap_resamples_whole_tasks() -> None:
    # Two runs of one task are either both in or both out of a resample, so duplicates enter in pairs.
    items = [item("a1", 1, True, "a"), item("a2", 2, False, "a"), item("b1", 3, True, "b"), item("b2", 4, False, "b")]
    draws = length.resample_weights([i.cluster for i in items], 100, seed=5)
    block = length._auroc_block(items, length.everyone(items), draws, length=True)
    # enacted lengths {1, 3} against clean lengths {2, 4}: only the pair (3, 2) is a win
    assert block["point"] == pytest.approx(0.25)
    assert block["valid"] == 100  # every draw keeps both classes, whatever clusters it holds


# --------------------------------------------------------------------------- #
# stored readings
# --------------------------------------------------------------------------- #


def record(run: str, verdict: str, score: float | None, *, at: str = "2026-10-28T10:00:00Z", reader: str | None = "a/b") -> dict:
    return finding(run, "cascade", detector_version="v", reader_model=reader, verdict=verdict, kind="cheat",
                   score=score, produced_at=at)


def test_readings_exclude_abstain_and_unknown_and_fall_back_to_binary() -> None:
    assert length.reading_from_finding(record("r", "abstain", 3.0)) is None
    assert length.reading_from_finding(record("r", "unknown", None)) is None
    assert length.reading_from_finding(record("r", "alarm", None)) == (True, 1.0)
    assert length.reading_from_finding(record("r", "clear", None)) == (False, 0.0)
    assert length.reading_from_finding(record("r", "clear", 2.0)) == (False, 2.0)
    assert length.reading_from_finding({**record("r", "alarm", None), "score": float("nan")}) == (True, 1.0)


def test_findings_after_the_cutoff_are_ignored_and_the_earlier_row_stays(tmp_path: Path) -> None:
    store = tmp_path / "findings.jsonl"
    rows = [record("r1", "clear", 0.0, at="2026-10-28T10:00:00Z"), record("r1", "alarm", 4.0, at="2026-10-30T10:00:00Z"),
            record("r2", "alarm", 2.0, at="2026-10-30T10:00:00Z"), record("r3", "alarm", 1.0, at="2026-10-28T11:00:00Z")]
    store.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    records = length.read_findings(store, datetime.fromisoformat("2026-10-29T04:15:00Z"))
    series = length.finding_series(records, ["r1", "r2", "r3", "r4"], length.Identity("cascade", "v", "a/b"))
    assert series == {"r1": (False, 0.0), "r3": (True, 1.0)}  # r2 is later than the cutoff, r4 never stored


def product_row(tool: str, detector: str, version: str, run: str, *, flag: bool | None = None, value: object = None,
                at: str = "2026-10-28T10:00:00+00:00", source: str = "cloud-trace-db") -> dict:
    return {"tool": tool, "detector": detector, "detector_version": version, "run_id": run, "flag": flag,
            "value": value, "at": at, "source": source}


def test_product_series_unions_thresholds_and_excludes_abstains(tmp_path: Path) -> None:
    arm = "metr-scanners/0.1.11@x;scanner-v0;openrouter-moonshotai-kimi-k3-inference-net-fp4;projection-native-actor-v2"
    other = "metr-scanners/0.1.11@x;scanner-v0;omp-xai-oauth-grok-4.3"
    rows = [
        product_row("scout_metr", "reward_hacking_scanner", arm, "a", value=6.0),
        product_row("scout_metr", "reward_hacking_scanner@ge5", arm, "a", flag=True),
        product_row("scout_metr", "reward_hacking_scanner@ge7", arm, "a", flag=False),
        product_row("scout_metr", "reward_hacking_scanner", arm, "b", value=2.0),
        product_row("scout_metr", "reward_hacking_scanner@ge5", arm, "b", flag=False),
        product_row("scout_metr", "reward_hacking_scanner@ge7", arm, "b", flag=False),
        product_row("scout_metr", "reward_hacking_scanner", arm, "c", value=None),  # reader abstained
        product_row("scout_metr", "reward_hacking_scanner", other, "a", value=9.0),  # other arm: not this tool
        product_row("hodoscope", "top25_per_cohort", "h", "a", flag=True),
        product_row("hodoscope", "top25_per_cohort", "h", "b", flag=None),
        product_row("pxi", "generic_investigation_r1", "p", "a", flag=None),
        product_row("pxi", "generic_investigation_r2", "p", "a", flag=False),
        product_row("pxi", "generic_investigation_r3", "p", "a", flag=True),
        product_row("pxi", "generic_investigation_r1", "p", "b", flag=None),
        product_row("pxi", "generic_investigation_r1", "p", "a", flag=True, at="2026-10-28T11:00:00+00:00"),  # newer wins
        product_row("pxi", "generic_investigation_r1", "p", "z", flag=True, source="cheatbench-actions-v1"),
        product_row("pxi", "generic_investigation_r1", "p", "d", flag=True, at="2026-10-30T00:00:00+00:00"),
    ]
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    cutoff = datetime.fromisoformat("2026-10-29T04:15:00Z")
    series = length.product_series(length.read_product_rows(path, cutoff, "cloud-trace-db", ["a", "b", "c", "d"]))
    assert series["metr_openrouter_kimi_k3"] == {"a": (True, 6.0), "b": (False, 2.0)}
    assert series["hodoscope_top25"] == {"a": (True, 1.0)}
    assert series["pxi_generic_investigation_r1"] == {"a": (True, 1.0)}
    assert series["pxi_generic_union"] == {"a": (True, 1.0)}
    assert series["laminar"] == {}  # no laminar rows: n=0, never invented


def test_union_flags() -> None:
    assert length.union_flags([None, True, False]) is True
    assert length.union_flags([None, False]) is False
    assert length.union_flags([None, None]) is None
    assert length.union_flags([]) is None


# --------------------------------------------------------------------------- #
# the whole analysis
# --------------------------------------------------------------------------- #


BLOCKS = ((10, 30), (18, 22), (22, 18), (30, 10))  # (enacted, clean) per length block of 40 runs


def synthetic() -> tuple[list[length.Item], dict[str, length.Series]]:
    """Enacted runs are longer. Blocks of lengths 100b+n never overlap, so the quartiles are the blocks."""
    items = []
    for block, (enacted, clean) in enumerate(BLOCKS, 1):
        items += [item(f"e-{block}-{n}", 100 * block + n, True) for n in range(enacted)]
        items += [item(f"c-{block}-{n}", 100 * block + n, False) for n in range(clean)]
    by_length = {i.run_id: (i.length >= 300, float(i.length)) for i in items}
    perfect = {i.run_id: (i.label, 1.0 if i.label else 0.0) for i in items}
    constant = {i.run_id: (False, 0.0) for i in items}
    reference = {i.run_id: (i.label and i.length >= 400, float(i.label)) for i in items}
    return items, {"cascade_v8": reference, "by_length": by_length, "perfect": perfect, "constant": constant}


def test_analysis_flags_length_proxies_and_real_signal_apart() -> None:
    items, tools = synthetic()
    result = length.analyse(items, tools, resamples=1200, seed=7)
    assert result["strata"]["k"] == 4
    assert [c["positives"] + c["negatives"] for c in result["strata"]["cells"]] == [40, 40, 40, 40]
    decision = {key: entry["decision"] for key, entry in result["tools"].items()}
    assert decision["perfect"]["beats_length"] is True  # AUROC 1.0 above length, and above 0.5 in every stratum
    assert decision["perfect"]["adds_within_strata"] is True
    assert decision["by_length"]["beats_length"] is False  # a pure function of length cannot beat length
    assert decision["constant"]["beats_length"] is False
    assert result["tools"]["constant"]["auroc"]["point"] == 0.5
    assert result["tools"]["by_length"]["auroc"]["point"] == pytest.approx(
        result["tools"]["by_length"]["length_auroc_same_runs"]["point"])


def test_length_rule_is_matched_to_the_reference_false_alarms() -> None:
    items, tools = synthetic()
    reference = tools["cascade_v8"]
    result = length.analyse(items, tools, resamples=1200, seed=7)
    rule = result["length_rule"]["matched_to_reference_far"]
    assert result["reference"]["native"]["fp"] == 0
    assert rule["fp"] == 0 and rule["tp"] > 0
    assert rule["reference_true_alarms"] == sum(1 for i in items if i.label and reference[i.run_id][0])
    assert rule["reference_true_alarms_in_top_stratum"] == rule["reference_true_alarms"]  # all have length >= 400
    assert result["length_rule"]["far_0.03"]["max_false_alarms"] == 2  # floor(0.03 * 80 clean)
    assert result["length_rule"]["far_0.05"]["max_false_alarms"] == 4


def test_abstained_runs_are_excluded_from_a_tool_not_counted_as_clear() -> None:
    items, tools = synthetic()
    partial = {run: reading for run, reading in tools["perfect"].items() if run.split("-")[1] == "4"}
    result = length.analyse(items, {**tools, "partial": partial}, resamples=1200, seed=7)
    entry = result["tools"]["partial"]
    assert entry["n_evaluated"] == 40
    assert (entry["native"]["positives"], entry["native"]["negatives"]) == (30, 10)
    assert [s["estimable"] for s in entry["strata"]] == [False, False, False, True]
    assert entry["strata"][0]["native"]["n"] == 0  # nothing evaluated in stratum 1: no recall, no FAR
    assert entry["decision"]["strata_estimable"] == 1 and entry["decision"]["adds_within_strata"] is False
    assert result["tools"]["perfect"]["n_evaluated"] == len(items)


def test_a_tool_with_no_evaluated_runs_is_listed_with_n_zero() -> None:
    items, tools = synthetic()
    result = length.analyse(items, {**tools, "laminar": {}}, resamples=1200, seed=7)
    assert result["tools"]["laminar"] == {"key": "laminar", "n_evaluated": 0}


def test_analysis_needs_its_reference() -> None:
    items, tools = synthetic()
    with pytest.raises(ValueError, match="reference"):
        length.analyse(items, {"perfect": tools["perfect"]}, resamples=1200, seed=7)


def test_results_are_reproducible() -> None:
    items, tools = synthetic()
    first = length.analyse(items, tools, resamples=1200, seed=11)
    assert first == length.analyse(items, tools, resamples=1200, seed=11)
    assert first != length.analyse(items, tools, resamples=1200, seed=12)
