"""Synthetic findings + fake labels for ``evallab trace score``: no database, no models."""

from __future__ import annotations

import argparse
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evallab.production import trace_harness_score as ths
from evallab.production.trace_harness import FindingStore, cache_key, finding

HAIKU = "anthropic/claude-haiku-5-5"


@dataclass(frozen=True)
class FakeLabels:
    name: str
    run_ids: frozenset[str]
    positive: dict[str, bool | None]
    positive_steps: dict[str, frozenset[int]] = field(default_factory=dict)
    localization_unit: str = "assistant_turn"
    turn_ordinals: dict[str, dict[int, int]] = field(default_factory=dict)


def labels(name: str, positive: dict[str, bool | None], **extra: Any) -> FakeLabels:
    return FakeLabels(name, frozenset(positive), positive, **extra)


def rec(
    run_id: str, verdict: str = "alarm", *, detector: str = "facets", version: str = "v1",
    reader: str | None = HAIKU, score: float | None = None, steps: tuple[int, ...] = (),
    cost: float = 0.0, tokens: tuple[int, int] = (0, 0), latency: int = 0,
    at: str = "2026-10-28T00:00:00Z",
) -> dict[str, Any]:
    return finding(
        run_id, detector, detector_version=version, reader_model=reader, verdict=verdict,
        kind="cheat", score=score, evidence_steps=steps, input_tokens=tokens[0],
        output_tokens=tokens[1], cost_usd=cost, latency_ms=latency, produced_at=at,
    )


def store(*records: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return {cache_key(record): record for record in records}

def test_length_baseline_ties_monotonic_and_abstention() -> None:
    cohort = labels("c", {"p": True, "p2": True, "n": False, "a": False})
    records = store(rec("p", score=5), rec("p2", score=3), rec("n", "clear", score=3),
                    rec("a", "abstain", score=10))
    result = ths.score_corpus(cohort, records, lengths={"p": 5, "p2": 3, "n": 3, "a": 10})
    baseline = only(result)["length_baseline"]
    assert baseline["n"] == 3
    assert baseline["length_auroc"] == baseline["detector_auroc"] == 0.75
    assert baseline["lower_bound_does_not_exceed_length"]
    assert baseline["recall"]["k"] == 1 and baseline["far"]["k"] == 0
    assert "Length baseline" in ths.render_markdown(result)
    no_lengths = ths.score_corpus(cohort, records)
    assert "length_baseline" not in only(no_lengths)
    assert "Length baseline" not in ths.render_markdown(no_lengths)


def test_length_baseline_perfect_monotone() -> None:
    cohort = labels("c", {"p": True, "n": False})
    result = ths.score_corpus(cohort, store(rec("p", score=2), rec("n", "clear", score=1)),
                              lengths={"p": 2, "n": 1})
    baseline = only(result)["length_baseline"]
    assert baseline["length_auroc"] == 1.0
    assert baseline["recall"]["k"] == 1 and baseline["far"]["k"] == 0




def only(result: dict[str, Any], detector: str | None = None) -> dict[str, Any]:
    units = [u for u in result["detectors"] if detector in (None, u["members"][0]["detector"])]
    assert len(units) == 1
    return units[0]


# --------------------------------------------------------------------------- #
# denominators
# --------------------------------------------------------------------------- #


def test_denominators_exclude_abstain_unknown_missing_and_considered_only() -> None:
    cohort = labels("c", {
        "p1": True, "p2": True, "p3": True, "p4": True,
        "n1": False, "n2": False, "n3": False, "n4": False,
        "c1": None, "c2": None,
    })
    records = store(
        rec("p1", "alarm"), rec("p2", "clear"), rec("p3", "abstain"),  # p4 missing
        rec("n1", "alarm"), rec("n2", "clear"), rec("n3", "unknown"),  # n4 missing
        rec("c1", "alarm"), rec("c2", "clear"),
    )
    unit = only(ths.score_corpus(cohort, records))
    native = unit["native"]
    assert (native["tp"], native["positives"], native["fp"], native["negatives"]) == (1, 2, 1, 2)
    assert native["recall"]["rate"] == 50.0 and native["recall"]["wilson"] == ths.wilson(1, 2)
    assert native["considered_only"] == {"alarmed": 1, "total": 2}
    coverage = unit["coverage"]
    assert coverage["cohort"] == 10 and coverage["evaluated"] == 6
    assert coverage["excluded"] == {"missing": 2, "abstain": 1, "unknown": 1}
    assert coverage["cohort_labels"] == {"positive": 4, "negative": 4, "unlabelled": 2}


def test_findings_outside_the_cohort_are_ignored() -> None:
    cohort = labels("c", {"p1": True, "n1": False})
    records = store(rec("p1", "alarm"), rec("n1", "clear"), rec("elsewhere", "alarm"))
    unit = only(ths.score_corpus(cohort, records))
    assert unit["coverage"]["cohort"] == 2 and unit["native"]["fp"] == 0


def test_identity_is_detector_version_reader_and_reader_is_shown() -> None:
    cohort = labels("c", {"p1": True, "n1": False})
    records = store(
        rec("p1", "alarm", reader=HAIKU), rec("n1", "clear", reader=HAIKU),
        rec("p1", "clear", reader="openai/gpt-5-mini"), rec("n1", "clear", reader="openai/gpt-5-mini"),
        rec("p1", "alarm", detector="tripwire", reader=None),
    )
    result = ths.score_corpus(cohort, records)
    assert len(result["detectors"]) == 3
    readers = {unit["reader"] for unit in result["detectors"]}
    assert readers == {HAIKU, "openai/gpt-5-mini", ths.NO_READER}
    markdown = ths.render_markdown(result)
    assert all(reader in markdown for reader in readers)
    picked = ths.score_corpus(cohort, records, detectors=[f"facets:{HAIKU}"])
    assert [u["reader"] for u in picked["detectors"]] == [HAIKU]
    with pytest.raises(ValueError, match="no findings"):
        ths.score_corpus(cohort, records, detectors=["nonesuch"])


# --------------------------------------------------------------------------- #
# FAR-budget thresholds
# --------------------------------------------------------------------------- #


def scored_cohort(name: str, positives: list[float], negatives: list[float]) -> tuple[FakeLabels, dict[Any, Any]]:
    pos = {f"{name}-p{i}": True for i in range(len(positives))}
    neg = {f"{name}-n{i}": False for i in range(len(negatives))}
    records = [rec(f"{name}-p{i}", "alarm", score=s) for i, s in enumerate(positives)]
    records += [rec(f"{name}-n{i}", "alarm", score=s) for i, s in enumerate(negatives)]
    return labels(name, {**pos, **neg}), store(*records)


def budget_entry(result: dict[str, Any], budget: float, detector: str | None = None) -> dict[str, Any]:
    return next(e for e in only(result, detector)["far_budgets"] if e["budget"] == budget)


def test_threshold_is_highest_recall_within_budget_and_ignores_verdict() -> None:
    cohort, records = scored_cohort("c", [9, 8, 5, 4, 3, 2], [1] * 8 + [3, 8])
    entry = budget_entry(ths.score_corpus(cohort, records, far_budgets=(0.1,)), 0.1)
    assert entry["thresholds"] == [4.0]
    assert entry["basis"] == ths.IN_CORPUS
    applied = entry["applied"]
    assert (applied["tp"], applied["positives"], applied["fp"], applied["negatives"]) == (4, 6, 1, 10)


def test_tied_scores_move_together_across_classes() -> None:
    # A positive and a negative both score 8: t=8 admits the negative, so with no
    # false alarm allowed the only choice is t=9 (recall 1/3), never a split of the tie.
    cohort, records = scored_cohort("c", [9, 8, 5], [8] + [1] * 9)
    entry = budget_entry(ths.score_corpus(cohort, records, far_budgets=(0.05,)), 0.05)
    assert entry["thresholds"] == [9.0]
    assert entry["applied"]["tp"] == 1 and entry["applied"]["fp"] == 0


def test_equal_recall_prefers_fewer_false_alarms_then_higher_threshold() -> None:
    cohort, records = scored_cohort("c", [9], [7] + [1] * 9)
    entry = budget_entry(ths.score_corpus(cohort, records, far_budgets=(0.1,)), 0.1)
    assert entry["thresholds"] == [9.0]  # t=7 reaches the same recall with a false alarm
    assert entry["applied"]["fp"] == 0


def test_budget_below_any_alarm_selects_never_alarm() -> None:
    cohort, records = scored_cohort("c", [3, 2], [5, 4])
    entry = budget_entry(ths.score_corpus(cohort, records, far_budgets=(0.1,)), 0.1)
    assert entry["thresholds"] == [None]
    assert entry["applied"]["tp"] == 0 and entry["applied"]["fp"] == 0


def test_thresholds_are_calibrated_elsewhere_and_applied_unchanged() -> None:
    cal, cal_records = scored_cohort("cal", [9, 8, 5, 4, 3, 2], [1] * 8 + [3, 8])
    target, target_records = scored_cohort("tgt", [6, 5, 4, 1], [9, 6, 2, 1])
    result = ths.score_corpus(
        target, {**cal_records, **target_records}, far_budgets=(0.1,), calibrate_labels=cal,
    )
    entry = budget_entry(result, 0.1)
    assert entry["thresholds"] == [4.0]  # chosen on cal, not what tgt alone would pick
    assert entry["basis"] == "calibrated on cal"
    applied = entry["applied"]
    assert (applied["tp"], applied["fp"], applied["negatives"]) == (3, 2, 4)
    assert result["calibrate_on"] == "cal"
    in_corpus = budget_entry(ths.score_corpus(target, target_records, far_budgets=(0.1,)), 0.1)
    assert in_corpus["basis"] == ths.IN_CORPUS and in_corpus["thresholds"] != [4.0]


def test_sweep_uses_scored_abstains_but_not_unscored_or_unknown_runs() -> None:
    cohort = labels("c", {"p1": True, "p2": True, "p3": True, "n1": False, "n2": False})
    records = store(
        rec("p1", "alarm", score=5), rec("p2", "abstain", score=9), rec("p3", "unknown", score=9),
        rec("n1", "clear", score=1), rec("n2", "alarm", score=None),
    )
    result = ths.score_corpus(cohort, records, far_budgets=(0.5,))
    applied = budget_entry(result, 0.5)["applied"]
    # a score-only reader's abstain still has a score to threshold; the no-actor run does not
    # enter, and the alarm without a score is counted as excluded
    assert (applied["positives"], applied["negatives"], applied["unscored_excluded"]) == (2, 1, 1)
    native = result["detectors"][0]["native"]
    assert (native["positives"], native["negatives"]) == (1, 2)  # native point excludes abstain


# --------------------------------------------------------------------------- #
# combinations
# --------------------------------------------------------------------------- #


def two_detector_cohort() -> tuple[FakeLabels, dict[Any, Any]]:
    cohort = labels("c", {"p1": True, "p2": True, "p3": True, "n1": False, "n2": False, "n3": False})
    records = store(
        rec("p1", "alarm", score=9), rec("p2", "clear", score=2), rec("p3", "alarm", score=7),
        rec("n1", "alarm", score=6), rec("n2", "clear", score=1), rec("n3", "clear", score=1),
        rec("p1", "alarm", detector="cascade", reader=None, score=1),
        rec("p2", "alarm", detector="cascade", reader=None, score=1),
        rec("p3", "clear", detector="cascade", reader=None, score=0),
        rec("n1", "clear", detector="cascade", reader=None, score=0),
        rec("n2", "alarm", detector="cascade", reader=None, score=1),
        # n3 has no cascade finding: not matched, never a negative prediction
    )
    return cohort, records


def combo_unit(result: dict[str, Any], spec: str) -> dict[str, Any]:
    entry = next(c for c in result["combos"] if c["spec"] == spec)
    assert entry["status"] == "reported", entry
    return entry["result"]


def test_or_and_and_combos_score_matched_runs_only() -> None:
    cohort, records = two_detector_cohort()
    result = ths.score_corpus(cohort, records, combos=["facets|cascade", "facets&cascade"])
    either = combo_unit(result, "facets|cascade")["native"]
    both = combo_unit(result, "facets&cascade")["native"]
    # matched runs: p1 p2 p3 n1 n2 (n3 lacks a cascade finding)
    assert (either["tp"], either["positives"], either["fp"], either["negatives"]) == (3, 3, 2, 2)
    assert (both["tp"], both["positives"], both["fp"], both["negatives"]) == (1, 3, 0, 2)
    entry = result["combos"][0]
    assert entry["matched_runs"] == 5
    assert combo_unit(result, "facets|cascade")["coverage"]["excluded"]["missing"] == 1
    facets_on_matched = entry["members_on_matched"][f"facets@v1 [{HAIKU}]"]
    assert (facets_on_matched["tp"], facets_on_matched["fp"], facets_on_matched["negatives"]) == (2, 1, 2)


def test_combo_joint_thresholds_use_member_scores() -> None:
    cohort, records = two_detector_cohort()
    result = ths.score_corpus(cohort, records, combos=["facets&cascade"], far_budgets=(0.5,))
    entry = budget_entry({"detectors": [combo_unit(result, "facets&cascade")]}, 0.5)
    assert len(entry["thresholds"]) == 2
    assert entry["applied"]["positives"] == 3 and entry["applied"]["negatives"] == 2


def test_combo_token_errors() -> None:
    cohort, records = two_detector_cohort()
    with pytest.raises(ValueError, match="mixes"):
        ths.parse_combo("a|b&c")
    with pytest.raises(ValueError, match="at least two"):
        ths.parse_combo("facets")
    with pytest.raises(ValueError, match="no findings"):
        ths.score_corpus(cohort, records, combos=["facets|missing"])
    two_readers = store(*records.values(), rec("p1", "clear", reader="openai/gpt-5-mini"))
    with pytest.raises(ValueError, match="ambiguous"):
        ths.score_corpus(cohort, two_readers, combos=["facets|cascade"])
    result = ths.score_corpus(cohort, two_readers, combos=[f"facets:{HAIKU}|cascade"])
    assert result["combos"][0]["status"] == "reported"


# --------------------------------------------------------------------------- #
# reproduction gate
# --------------------------------------------------------------------------- #


def cheatbench(facets_tp: int = 30, facets_fp: int = 4) -> tuple[FakeLabels, dict[Any, Any]]:
    """75/75 cohort that reproduces the published facets 30/75, 4/75 and cascade 19/75, 0/75."""
    positive = {f"p{i}": True for i in range(75)} | {f"n{i}": False for i in range(75)}
    records = []
    for i in range(75):
        records.append(rec(f"p{i}", "alarm" if i < facets_tp else "clear", score=2 if i < facets_tp else 0))
        records.append(rec(f"n{i}", "alarm" if i < facets_fp else "clear", score=2 if i < facets_fp else 0))
        records.append(rec(f"p{i}", "alarm" if i >= 56 else "clear", detector="cascade", version="window-cascade/v8", reader=None))
        records.append(rec(f"n{i}", "clear", detector="cascade", version="window-cascade/v8", reader=None))
    return labels("cheatbench", positive), store(*records)


def test_gate_passes_and_reports_combos_when_published_numbers_reproduce() -> None:
    cohort, records = cheatbench()
    result = ths.score_corpus(cohort, records, combos=["facets|cascade"])
    statuses = {entry["id"]: entry["status"] for entry in result["gate"]}
    assert statuses["facets-cheatbench"] == "pass" and statuses["cascade-v8-cheatbench"] == "pass"
    assert statuses["cascade-v4-cheatbench"] == "not-applicable"
    assert result["combos"][0]["status"] == "reported"
    assert "forced" not in ths.render_markdown(result)


def test_gate_failure_blocks_combos_and_says_why() -> None:
    cohort, records = cheatbench(facets_tp=29)
    result = ths.score_corpus(cohort, records, combos=["facets|cascade"])
    gate = {entry["id"]: entry for entry in result["gate"]}
    assert gate["facets-cheatbench"]["status"] == "fail"
    assert gate["facets-cheatbench"]["actual"] == [29, 75, 4, 75]
    combo = result["combos"][0]
    assert combo["status"] == "blocked" and "result" not in combo
    assert "facets-cheatbench" in combo["blockers"][0] and "[30, 75, 4, 75]" in combo["blockers"][0]
    markdown = ths.render_markdown(result)
    assert "not reported" in markdown and "facets-cheatbench" in markdown
    assert "facets@v1|cascade@window-cascade/v8" not in markdown.split("## Native")[1]
    # single-detector rows are still reported
    assert len(result["detectors"]) == 2


def test_force_combos_reports_past_a_failed_gate() -> None:
    cohort, records = cheatbench(facets_tp=29)
    result = ths.score_corpus(cohort, records, combos=["facets|cascade"], force_combos=True)
    combo = result["combos"][0]
    assert combo["status"] == "forced" and "result" in combo and combo["blockers"]
    assert "forced past a failed gate" in ths.render_markdown(result)


def test_gate_ignores_other_readers_and_other_corpora() -> None:
    cohort, records = cheatbench(facets_tp=1)
    other = store(*(
        {**record, "reader_model": "openai/gpt-5-mini"} if record["detector"] == "facets" else record
        for record in records.values()
    ))
    result = ths.score_corpus(cohort, other, combos=["facets|cascade"])
    assert next(e for e in result["gate"] if e["id"] == "facets-cheatbench")["status"] == "not-applicable"
    assert result["combos"][0]["status"] == "reported"
    renamed = labels("staged:foo", dict(cohort.positive))
    assert ths.score_corpus(renamed, records)["gate"] == []


# --------------------------------------------------------------------------- #
# HackBench: no negatives, command localization
# --------------------------------------------------------------------------- #


def hackbench(exact: int = 20, total: int = 41) -> tuple[FakeLabels, dict[Any, Any]]:
    positive = {f"hackbench:{i:02d}": True for i in range(total)}
    gold = {run: frozenset({10}) for run in positive}
    # command-layout ordinals: step id -> command number (steps 2 and 40 carry commands)
    ordinals = {run: {2: 1, 10: 5, 11: 6, 12: 12, 40: 20} for run in positive}
    records = []
    for i, run in enumerate(positive):
        steps = (10,) if i < exact else (11,) if i % 2 else (40,)
        records.append(rec(run, "alarm" if i < exact or i % 5 else "clear", steps=steps))
    return labels("hackbench", positive, positive_steps=gold, localization_unit="command",
                  turn_ordinals=ordinals), store(*records)


def test_hackbench_has_no_far_and_reports_alarmed_and_exact_command_hits() -> None:
    cohort, records = hackbench()
    result = ths.score_corpus(cohort, records)
    unit = only(result)
    native = unit["native"]
    assert native["negatives"] == 0 and native["far"]["rate"] is None
    assert native["far"]["note"] == "n/a: no negatives"
    assert native["tp"] == native["positives"] - 5 == 36  # clear: i % 5 == 0 among the non-exact runs
    loc = unit["localization"]
    assert loc["unit"] == "command" and loc["records"] == 41
    assert (loc["exact"]["k"], loc["exact"]["n"]) == (20, 41)
    assert loc["within_5"]["k"] > 20  # step 11 = command 6 is within 5 of gold command 5; step 40 = command 20 is not
    for entry in unit["far_budgets"]:
        assert entry["status"] == "n/a" and "no negatives" in entry["reason"]
    assert next(e for e in result["gate"] if e["id"] == "facets-hackbench-alarmed-exact")["status"] == "pass"
    markdown = ths.render_markdown(result)
    assert "n/a (no negatives)" in markdown and "unit: command" in markdown


def test_hackbench_gate_fails_when_exact_hits_differ() -> None:
    cohort, records = hackbench(exact=19)
    gate = ths.score_corpus(cohort, records)["gate"]
    assert gate[0]["status"] == "fail" and gate[0]["actual"] == [19, 41]


def test_calibrated_thresholds_on_negative_free_corpus_report_recall_only() -> None:
    cal, cal_records = scored_cohort("cal", [9, 8, 5], [1, 1, 2, 2])
    positive = {f"h{i}": True for i in range(4)}
    target = labels("hb", positive, localization_unit="command")
    records = store(*cal_records.values(), *(rec(f"h{i}", "clear", score=s) for i, s in enumerate((9, 6, 5, 1))))
    entry = budget_entry(ths.score_corpus(target, records, far_budgets=(0.1,), calibrate_labels=cal), 0.1)
    assert entry["thresholds"] == [5.0]
    assert entry["applied"]["tp"] == 3 and entry["applied"]["negatives"] == 0
    assert entry["applied"]["far"]["rate"] is None


# --------------------------------------------------------------------------- #
# localization, cost
# --------------------------------------------------------------------------- #


def test_localization_exact_within_k_and_cited_precision() -> None:
    cohort = labels(
        "c", {"p1": True, "p2": True, "p3": True, "p4": True, "n1": False},
        positive_steps={"p1": frozenset({4}), "p2": frozenset({4}), "p3": frozenset({4}), "p4": frozenset({4})},
        turn_ordinals={run: {1: 0, 2: 1, 4: 2, 6: 3, 40: 30} for run in ("p1", "p2", "p3", "p4")},
    )
    records = store(
        rec("p1", "alarm", steps=(4, 6)),        # exact
        rec("p2", "clear", steps=(6,)),          # not exact, within 5 turns
        rec("p3", "alarm", steps=(40,)),         # far away
        rec("p4", "abstain", steps=(4,)),        # excluded
        rec("n1", "alarm", steps=(4,)),          # negative: not localization
    )
    loc = only(ths.score_corpus(cohort, records))["localization"]
    assert loc["records"] == 3
    assert (loc["exact"]["k"], loc["within_5"]["k"]) == (1, 2)
    assert (loc["cited_step_precision"]["k"], loc["cited_step_precision"]["n"]) == (1, 4)
    assert (loc["alarmed_records"], loc["alarmed_exact"]["k"]) == (2, 1)


def test_cost_tokens_and_latency_percentiles() -> None:
    cohort = labels("c", {f"r{i}": i % 2 == 0 for i in range(10)})
    records = store(*(
        rec(f"r{i}", "abstain" if i == 9 else "alarm", cost=0.01 * (i + 1), tokens=(100 * i, 10 * i), latency=100 * (i + 1))
        for i in range(10)
    ))
    cost = only(ths.score_corpus(cohort, records))["cost"]
    assert cost["runs"] == 10
    assert cost["cost_per_1k_usd"] == 55.0  # mean 0.055 USD/run, abstain cost included
    assert (cost["mean_input_tokens"], cost["mean_output_tokens"]) == (450.0, 45.0)
    assert (cost["latency_ms_p50"], cost["latency_ms_p95"]) == (500, 1000)


def test_unrecorded_tokens_and_latency_are_not_reported_as_zero() -> None:
    cohort = labels("c", {"p": True, "n": False})
    imported = store(rec("p", "alarm"), rec("n", "clear"))  # model reader, no receipts
    cost = only(ths.score_corpus(cohort, imported))["cost"]
    assert cost["cost_per_1k_usd"] == 0.0
    assert cost["mean_input_tokens"] is None and cost["latency_ms_p50"] is None
    deterministic = store(rec("p", "alarm", reader=None, latency=3), rec("n", "clear", reader=None, latency=5))
    cost = only(ths.score_corpus(cohort, deterministic))["cost"]
    assert cost["mean_input_tokens"] == 0 and cost["latency_ms_p95"] == 5


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_scores_from_findings_file_and_writes_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cohort, records = scored_cohort("c", [9, 8, 5], [1, 1, 2])
    findings = tmp_path / "findings.jsonl"
    FindingStore(findings).append(list(records.values()))
    loaded: list[str] = []

    def load_corpus_labels(name: str, *, database: Path, repo_root: Path) -> FakeLabels:
        loaded.append(name)
        return cohort

    fake = types.ModuleType("evallab.production.trace_harness_corpora")
    fake.load_corpus_labels = load_corpus_labels  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "evallab.production.trace_harness_corpora", fake)
    monkeypatch.setattr("evallab.production.trace_harness.resolve_runs", lambda *args, **kwargs: [])

    parser = argparse.ArgumentParser()
    ths.add_score_parser(parser.add_subparsers())
    args = parser.parse_args([
        "score", "--corpus", "c", "--findings", str(findings), "--database", str(tmp_path / "x.sqlite"),
        "--far-budgets", "0.5", "--json-out", str(tmp_path / "o/score.json"),
        "--markdown-out", str(tmp_path / "o/score.md"), "--calibrate-on", "c",
    ])
    assert args.func(args, tmp_path) == 0
    assert loaded == ["c", "c"]
    payload = json.loads((tmp_path / "o/score.json").read_text())
    assert payload["schema"] == ths.SCORE_SCHEMA and payload["far_budgets"] == [0.5]
    assert "Trace score: c" in (tmp_path / "o/score.md").read_text()

    bad = parser.parse_args(["score", "--corpus", "c", "--findings", str(findings), "--detectors", "nonesuch"])
    assert bad.func(bad, tmp_path) == 2
    with pytest.raises(SystemExit):
        parser.parse_args(["score", "--corpus", "c", "--far-budgets", "3"])


# --------------------------------------------------------------------------- #
# pooled readers: name:*
# --------------------------------------------------------------------------- #

READER_A, READER_B = "meta/reader-a", "kimi-code/reader-b"


def split_reader_hackbench() -> tuple[FakeLabels, dict[Any, Any]]:
    cohort, records = hackbench()
    swapped = []
    for i, record in enumerate(records.values()):
        reader = READER_B if i == 0 else READER_A
        swapped.append({**record, "reader_model": reader})
    return cohort, store(*swapped)


def test_pooled_token_takes_any_reader_and_reports_per_reader_counts() -> None:
    cohort, records = split_reader_hackbench()
    # Run 1 also has a finding from reader B, which sorts before reader A: the first reader in
    # sorted order wins (produced_at is not consulted), so run 1 now cites step 40, not gold.
    other = rec("hackbench:01", "clear", reader=READER_B, steps=(40,), at="2030-01-01T00:00:00Z")
    result = ths.score_corpus(cohort, store(*records.values(), other), detectors=["facets:*"])
    unit = only(result)
    assert unit["members"][0]["reader"] == "*"
    assert unit["reader"] == f"mixed: {READER_B} ×2, {READER_A} ×39"
    assert unit["coverage"]["evaluated"] == 41
    assert unit["localization"]["exact"]["k"] == 19
    exact_identities = ths.score_corpus(cohort, records)
    assert len(exact_identities["detectors"]) == 2  # pooling is opt-in
    assert "mixed: " in ths.render_markdown(result)


def test_pooled_published_check_applies_without_naming_the_pool() -> None:
    cohort, records = split_reader_hackbench()
    gate = ths.score_corpus(cohort, records)["gate"]
    assert [(e["id"], e["status"]) for e in gate] == [("facets-hackbench-alarmed-exact", "pass")]
    assert gate[0]["pooled"] is True and gate[0]["actual"] == [20, 41]


def test_pooled_member_failing_the_gate_blocks_its_combos() -> None:
    cohort, records = split_reader_hackbench()
    extra = [rec(run, "alarm", detector="cascade", reader=None, steps=(10,)) for run in cohort.run_ids]
    broken = store(*(r for r in records.values() if r["run_id"] != "hackbench:00"), *extra)
    result = ths.score_corpus(cohort, broken, combos=["facets:*|cascade"])
    assert result["gate"][0]["status"] == "fail"
    assert result["combos"][0]["status"] == "blocked"
    # an exact-reader member is a different identity from the published pooled number
    exact = ths.score_corpus(cohort, broken, combos=[f"facets:{READER_A}|cascade"])
    assert exact["combos"][0]["status"] == "reported"


def test_goodfire_facets_check_is_gated_on_the_haiku_identity() -> None:
    positive = {f"p{i}": True for i in range(80)} | {f"n{i}": False for i in range(70)}
    cohort = labels("goodfire", positive)
    records = store(*(
        rec(run, "alarm" if (run[0] == "p" and int(run[1:]) < 7) or run == "n0" else "clear")
        for run in positive
    ))
    gate = {e["id"]: e for e in ths.score_corpus(cohort, records)["gate"]}
    assert gate["facets-goodfire-haiku"]["status"] == "pass"
    assert gate["facets-goodfire-haiku"]["actual"] == [7, 80, 1, 70]
    other = store(*({**r, "reader_model": "openai/gpt-5-mini"} for r in records.values()))
    assert {e["id"]: e for e in ths.score_corpus(cohort, other)["gate"]}[
        "facets-goodfire-haiku"]["status"] == "not-applicable"


# --------------------------------------------------------------------------- #
# --publish: the combined scorecard the Harness tab reads
# --------------------------------------------------------------------------- #

KIMI = "kimi-code/k3.1-pro-highspeed"


def published_cohort(name: str = "c") -> tuple[FakeLabels, dict[Any, Any]]:
    """4 positives / 40 negatives; facets (Haiku, scored) and cascade (deterministic, scored)."""
    cohort = labels(
        name, {f"p{i}": True for i in range(4)} | {f"n{i}": False for i in range(40)},
        positive_steps={"p0": frozenset({4}), "p1": frozenset({4})},
    )
    records = []
    for i in range(4):
        records.append(rec(f"p{i}", "alarm", score=5 - i, steps=((4,) if i == 0 else (9,) if i == 1 else ())))
        records.append(rec(f"p{i}", "alarm", detector="cascade", reader=None, score=1.0, cost=0.002, latency=250))
    for i in range(40):
        records.append(rec(f"n{i}", "clear", score=0.0))
        records.append(rec(f"n{i}", "clear", detector="cascade", reader=None, score=0.0, cost=0.002, latency=250))
    return cohort, store(*records)


def read_document(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def by_identity(document: dict[str, Any]) -> dict[tuple[str, tuple[str, ...], str, str], dict[str, Any]]:
    return {
        (r["corpus"], tuple(r["detectors"]), r["rule"], r["reader_model"]): r for r in document["rows"]
    }


def assert_ui_model_accepts(path: Path) -> None:
    view = pytest.importorskip("evallab.production.harness_view")
    view.Scorecard.model_validate(read_document(path))


def test_publish_maps_singles_and_combos_to_scorecard_rows(tmp_path: Path) -> None:
    cohort, records = published_cohort()
    result = ths.score_corpus(cohort, records, combos=["facets|cascade", "facets&cascade"])
    target = tmp_path / "scorecard.json"
    count, notes = ths.publish_scorecard(result, target)
    assert count == 4 and notes == []
    document = read_document(target)
    assert document["schema"] == ths.PUBLISH_SCHEMA
    assert document["generated_at"].endswith("Z") and set(document) == {"schema", "generated_at", "rows"}
    rows = by_identity(document)
    facets = rows[("c", ("facets",), "single", HAIKU)]
    cascade = rows[("c", ("cascade",), "single", "none")]
    either = rows[("c", ("facets", "cascade"), "any", HAIKU)]
    both = rows[("c", ("facets", "cascade"), "all", HAIKU)]
    assert (facets["positives"], facets["negatives"]) == (4, 40)
    assert facets["budgets"] == [
        {"far_budget": 0.03, "hits": 4, "false_alarms": 0}, {"far_budget": 0.05, "hits": 4, "false_alarms": 0},
    ]
    assert facets["localization"] == {"hits": 1, "scored": 2} and cascade["localization"] == {"hits": 0, "scored": 2}
    assert (facets["cost_usd_per_1k_runs"], facets["latency_ms_median"]) == (0.0, None)  # no receipts stored
    assert (cascade["cost_usd_per_1k_runs"], cascade["latency_ms_median"]) == (2.0, 250)
    assert either["cost_usd_per_1k_runs"] == both["cost_usd_per_1k_runs"] == 2.0
    assert facets["published"] is None and either["published"] is None
    assert all(not (r["rule"] != "single" and r["published"]) for r in rows.values())
    assert_ui_model_accepts(target)


def test_publish_only_the_fixed_budgets_and_requires_one_of_them(tmp_path: Path) -> None:
    cohort, records = published_cohort()
    target = tmp_path / "scorecard.json"
    result = ths.score_corpus(cohort, records, far_budgets=(0.03, 0.1))
    ths.publish_scorecard(result, target)
    assert [b["far_budget"] for b in read_document(target)["rows"][0]["budgets"]] == [0.03]
    other = tmp_path / "none.json"
    with pytest.raises(ths.PublishRefused, match="no 3%/5%"):
        ths.publish_scorecard(ths.score_corpus(cohort, records, far_budgets=(0.1,)), other)
    assert not other.exists()


def test_reader_encoding_for_single_deterministic_and_several_model_readers() -> None:
    member = lambda reader: {"reader": reader}  # noqa: E731
    assert ths.published_reader([member(None)]) == "none"
    assert ths.published_reader([member(HAIKU)]) == HAIKU
    assert ths.published_reader([member(HAIKU), member(None)]) == HAIKU
    both = ths.published_reader([member(None), member(HAIKU), member(KIMI)])
    assert both == "anthropic/claude-haiku-5-5+kimi-code:k3.1-pro-highspeed"
    assert ths._PUBLISHED_READER.fullmatch(both)


def test_publish_two_model_readers_and_same_detector_with_two_readers(tmp_path: Path) -> None:
    cohort, records = published_cohort()
    extra = [
        rec(run, "alarm" if run.startswith("p") else "clear", detector="scout", reader=KIMI,
            score=1.0 if run.startswith("p") else 0.0)
        for run in cohort.run_ids
    ]
    other_reader = [{**record, "reader_model": "openai/gpt-5-mini"} for record in records.values()
                    if record["detector"] == "facets"]
    result = ths.score_corpus(
        cohort, store(*records.values(), *extra, *other_reader), combos=[f"facets:{HAIKU}|scout"],
    )
    target = tmp_path / "scorecard.json"
    ths.publish_scorecard(result, target)
    rows = by_identity(read_document(target))
    assert ("c", ("facets",), "single", HAIKU) in rows and ("c", ("facets",), "single", "openai/gpt-5-mini") in rows
    assert ("c", ("facets", "scout"), "any", "anthropic/claude-haiku-5-5+kimi-code:k3.1-pro-highspeed") in rows
    assert_ui_model_accepts(target)


def test_combo_repeating_a_detector_and_pooled_identities_are_not_published(tmp_path: Path) -> None:
    cohort, records = published_cohort()
    twice = store(*records.values(), *(
        {**r, "reader_model": "openai/gpt-5-mini"} for r in records.values() if r["detector"] == "facets"
    ))
    result = ths.score_corpus(
        cohort, twice, detectors=["facets:*", "cascade"], combos=[f"facets:{HAIKU}|facets:openai/gpt-5-mini"],
    )
    count, notes = ths.publish_scorecard(result, tmp_path / "s.json")
    rows = by_identity(read_document(tmp_path / "s.json"))
    assert count == 1 and list(rows) == [("c", ("cascade",), "single", "none")]
    assert any("pooled" in note for note in notes) and any("twice" in note for note in notes)


def test_publish_merges_other_corpora_and_replaces_the_scored_one(tmp_path: Path) -> None:
    target = tmp_path / "state/scorecard.json"
    first, first_records = published_cohort("alpha")
    second, second_records = published_cohort("beta")
    ths.publish_scorecard(ths.score_corpus(first, first_records), target)
    ths.publish_scorecard(ths.score_corpus(second, second_records, combos=["facets|cascade"]), target)
    rows = by_identity(read_document(target))
    assert {key[0] for key in rows} == {"alpha", "beta"} and len(rows) == 2 + 3
    alpha_before = rows[("alpha", ("facets",), "single", HAIKU)]
    # Re-scoring beta without the combo and with a missed positive replaces every beta row.
    weaker = store(*(
        {**r, "verdict": "clear", "score": 0.0} if r["run_id"] == "p0" else r for r in second_records.values()
    ))
    ths.publish_scorecard(ths.score_corpus(second, weaker), target)
    rows = by_identity(read_document(target))
    assert len(rows) == 4 and rows[("alpha", ("facets",), "single", HAIKU)] == alpha_before
    assert not any(key[1] == ("facets", "cascade") for key in rows)
    assert rows[("beta", ("facets",), "single", HAIKU)]["budgets"][0]["hits"] == 3
    assert_ui_model_accepts(target)


def test_corpus_without_negatives_is_skipped_and_writes_nothing(tmp_path: Path) -> None:
    cohort = labels("hb", {f"h{i}": True for i in range(4)}, localization_unit="command")
    records = store(*(rec(f"h{i}", "alarm", score=3.0) for i in range(4)))
    target = tmp_path / "scorecard.json"
    count, notes = ths.publish_scorecard(ths.score_corpus(cohort, records), target)
    assert count == 0 and not target.exists()
    assert len(notes) == 1 and "no negatives" in notes[0]
    target.write_text("{}")  # not even an invalid existing file is touched or read
    assert ths.publish_scorecard(ths.score_corpus(cohort, records), target) == (0, notes)
    assert target.read_text() == "{}"


def gated_cohort(facets_tp: int) -> tuple[FakeLabels, dict[Any, Any]]:
    cohort, records = cheatbench(facets_tp=facets_tp)
    return cohort, store(*(
        {**r, "score": 1.0 if r["verdict"] == "alarm" else 0.0} if r["detector"] == "cascade" else r
        for r in records.values()
    ))


def test_gate_blocked_combo_is_excluded_unless_forced_and_published_flags_follow_the_gate(
    tmp_path: Path,
) -> None:
    combos = [f"facets:{HAIKU}|cascade"]
    cohort, records = gated_cohort(30)
    ths.publish_scorecard(ths.score_corpus(cohort, records, combos=combos), tmp_path / "ok.json")
    ok = by_identity(read_document(tmp_path / "ok.json"))
    assert ok[("cheatbench", ("facets",), "single", HAIKU)]["published"] == {
        "check": "facets-cheatbench", "reproduced": True,
    }
    assert ok[("cheatbench", ("cascade",), "single", "none")]["published"] == {
        "check": "cascade-v8-cheatbench", "reproduced": True,
    }
    assert ("cheatbench", ("facets", "cascade"), "any", HAIKU) in ok

    cohort, records = gated_cohort(29)
    count, notes = ths.publish_scorecard(
        ths.score_corpus(cohort, records, combos=combos), tmp_path / "blocked.json",
    )
    blocked = by_identity(read_document(tmp_path / "blocked.json"))
    assert count == 2 and ("cheatbench", ("facets", "cascade"), "any", HAIKU) not in blocked
    assert any("blocked by the reproduction gate" in note for note in notes)
    assert blocked[("cheatbench", ("facets",), "single", HAIKU)]["published"] == {
        "check": "facets-cheatbench", "reproduced": False,
    }
    forced = ths.score_corpus(cohort, records, combos=combos, force_combos=True)
    assert ths.publish_scorecard(forced, tmp_path / "forced.json")[0] == 3
    assert ("cheatbench", ("facets", "cascade"), "any", HAIKU) in by_identity(read_document(tmp_path / "forced.json"))
    assert_ui_model_accepts(tmp_path / "forced.json")


def test_publish_is_atomic_when_the_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "scorecard.json"
    cohort, records = published_cohort("alpha")
    ths.publish_scorecard(ths.score_corpus(cohort, records), target)
    before = target.read_bytes()

    def boom(source: Any, destination: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(ths.os, "replace", boom)
    other, other_records = published_cohort("beta")
    with pytest.raises(OSError, match="replace failed"):
        ths.publish_scorecard(ths.score_corpus(other, other_records), target)
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["scorecard.json"]


def test_publish_refuses_a_document_the_ui_model_would_reject(tmp_path: Path) -> None:
    cohort, records = published_cohort("Bad Corpus")
    target = tmp_path / "scorecard.json"
    with pytest.raises(ths.PublishRefused, match="corpus name"):
        ths.publish_scorecard(ths.score_corpus(cohort, records), target)
    assert not target.exists()
    good, good_records = published_cohort("alpha")
    target.write_text('{"schema": "wrong"}')
    with pytest.raises(ths.PublishRefused, match="existing scorecard"):
        ths.publish_scorecard(ths.score_corpus(good, good_records), target)
    assert target.read_text() == '{"schema": "wrong"}'


def test_versions_of_one_detector_with_one_reader_collide_and_refuse(tmp_path: Path) -> None:
    cohort, records = published_cohort()
    both = store(*records.values(), *(
        {**r, "detector_version": "v2", "score": 0.5} for r in records.values() if r["detector"] == "facets"
    ))
    with pytest.raises(ths.PublishRefused, match="differ only by detector version"):
        ths.publish_scorecard(ths.score_corpus(cohort, both), tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()


def test_cli_publish_flag_default_and_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cohort, records = published_cohort()
    findings = tmp_path / "findings.jsonl"
    FindingStore(findings).append(list(records.values()))
    fake = types.ModuleType("evallab.production.trace_harness_corpora")
    fake.load_corpus_labels = lambda name, *, database, repo_root: cohort  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "evallab.production.trace_harness_corpora", fake)
    monkeypatch.setattr("evallab.production.trace_harness.resolve_runs", lambda *args, **kwargs: [])
    parser = argparse.ArgumentParser()
    ths.add_score_parser(parser.add_subparsers())
    assert parser.parse_args(["score", "--corpus", "c", "--publish"]).publish == ths.DEFAULT_PUBLISH
    assert parser.parse_args(["score", "--corpus", "c"]).publish is None
    assert ths.DEFAULT_PUBLISH.name == "scorecard-v1.json" and ths.DEFAULT_PUBLISH.parent.name == "trace-harness"

    target = tmp_path / "out/scorecard.json"
    args = parser.parse_args(["score", "--corpus", "c", "--findings", str(findings), "--publish", str(target)])
    assert args.func(args, tmp_path) == 0
    assert len(read_document(target)["rows"]) == 2
    refused = parser.parse_args([
        "score", "--corpus", "c", "--findings", str(findings), "--far-budgets", "0.1", "--publish", str(target),
    ])
    assert refused.func(refused, tmp_path) == 2
    assert len(read_document(target)["rows"]) == 2
