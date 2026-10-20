"""1-to-1 finding-level scoring, task-collision inflation and strata for tb4-audit-v1."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY_DIR = ROOT / "research/answer-keys/tb4-audit-v1"
_spec = importlib.util.spec_from_file_location("tb4_audit_score", KEY_DIR / "score_audit.py")
assert _spec is not None and _spec.loader is not None
scorer = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scorer  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(scorer)

LADDER = ("exact_id", "locus_and_class", "category_1to1", "task_any_1to1")


def _item(
    item_id: str,
    task: str,
    *,
    split: str = "test",
    category: str = "weak",
    taxonomy: str = "weak_check",
    tests: tuple[tuple[str, str], ...] = (),
    strength: str = "pm_reported",
    support: int = 1,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "scope": "task",
        "task": task,
        "split": split,
        "expected_category": category,
        "taxonomy_class": taxonomy,
        "taxonomy_class_id": "TS01",
        "label_strength": strength,
        "auditor_evidence": {"counted_source_count": support},
        "port": {"tests": [{"path": f"{task}/{p}", "symbol": s} for p, s in tests]},
    }


def _task(name: str, split: str = "test", stratum: str = "unexposed") -> dict[str, Any]:
    return {"task": name, "split": split, "exposure_stratum": stratum, "historical_exposures": []}


def _pred(task: str, category: str | None = "weak", **extra: Any) -> dict[str, Any]:
    return {
        "subject": {"kind": "task", "task_id": f"terminal-bench/{task}"},
        "verdict": "fail",
        "category": category,
        **extra,
    }


def _k(report: dict[str, Any], mode: str, cell: str = "all") -> tuple[int, int]:
    recall = report["modes"][mode]["headline"][cell]["recall"]
    return recall["k"], recall["n"]


def test_one_prediction_on_six_finding_task_credits_one_finding() -> None:
    key = scorer.build_key(
        [_item(f"f{i}", "six") for i in range(6)] + [_item("other", "two")],
        [_task("six"), _task("two")],
    )
    report = scorer.score(key, [_pred("six")])
    assert _k(report, "category_1to1") == (1, 7)
    assert _k(report, "task_any_1to1") == (1, 7)
    assert _k(report, "legacy_task_broadcast") == (6, 7)
    assert _k(report, "legacy_any") == (6, 7)
    assert report["task_collision_inflation"]["all"] == {
        "category": round(5 / 7, 6),
        "any": round(5 / 7, 6),
    }
    six_only = scorer.build_key([_item(f"f{i}", "six") for i in range(6)], [_task("six")])
    single = scorer.score(six_only, [_pred("six")])
    assert single["modes"]["category_1to1"]["headline"]["all"]["recall"]["rate"] == round(1 / 6, 6)
    assert single["modes"]["legacy_task_broadcast"]["headline"]["all"]["recall"]["rate"] == 1.0
    assert single["modes"]["category_1to1"]["headline"]["all"]["precision"]["k"] == 1
    # Two predictions earn exactly two credits; a third category is not credited by category.
    double = scorer.score(six_only, [_pred("six"), _pred("six"), _pred("six", "broken")])
    assert _k(double, "category_1to1") == (2, 6)
    assert _k(double, "task_any_1to1") == (3, 6)
    assert _k(double, "legacy_task_broadcast") == (6, 6)


def test_matching_is_maximum_not_greedy_and_ids_credit_once() -> None:
    key = scorer.build_key([_item("x", "t"), _item("y", "t")], [_task("t")])
    # Greedy would give the first prediction `x` and leave the second unmatched.
    preds = [
        _pred("t", None, id="a", root_cause=["x", "y"]),
        _pred("t", None, id="b", root_cause=["x"]),
    ]
    report = scorer.score(key, preds)
    assert _k(report, "exact_id") == (2, 2)
    assert sorted(report["matches"]["exact_id"]) == [["a", "y"], ["b", "x"]]
    # One prediction naming both findings is still one credit.
    one = scorer.score(key, [_pred("t", None, root_cause=["x", "y"])])
    assert _k(one, "exact_id") == (1, 2)
    # Predictions on other tasks never match by id.
    other = scorer.build_key([_item("x", "t"), _item("z", "u")], [_task("t"), _task("u")])
    assert _k(scorer.score(other, [_pred("u", None, root_cause=["x"])]), "exact_id") == (0, 2)


def test_locus_and_class_requires_a_cited_locus_and_a_matching_class() -> None:
    key = scorer.build_key(
        [
            _item("loc", "sq", tests=(("tests/test_sq.py", "test_negative_input"),)),
            _item("other", "sq", category="broken", taxonomy="grader_logic_bug"),
        ],
        [_task("sq")],
    )
    path_and_class = _pred("sq", None, evidence_refs=["tasks/sq/tests/test_sq.py"])
    path_and_class["taxonomy_class"] = "weak_check"
    symbol_and_category = _pred("sq", "weak", summary="test_negative_input accepts anything")
    longer_symbol = _pred("sq", "weak", summary="test_negative_input_v2 is fine")
    wrong_class = _pred("sq", "tamperable", evidence_refs=["tests/test_sq.py"])
    category_only = _pred("sq", "weak", summary="checks are weak")

    def counts(pred: dict[str, Any]) -> tuple[int, ...]:
        report = scorer.score(key, [pred])
        return tuple(_k(report, mode)[0] for mode in LADDER)

    assert counts(path_and_class) == (0, 1, 1, 1)
    assert counts(symbol_and_category) == (0, 1, 1, 1)
    assert counts(longer_symbol) == (0, 0, 1, 1)
    assert counts(wrong_class) == (0, 0, 0, 1)
    assert counts(category_only) == (0, 0, 1, 1)
    named = _pred("sq", "tamperable", id="loc")
    assert counts(named) == (1, 1, 1, 1)


def _random_predictions(rng: random.Random, n: int) -> list[dict[str, Any]]:
    items = [
        json.loads(line)
        for line in (KEY_DIR / "items.jsonl").read_text().splitlines()
        if json.loads(line)["scope"] == "task"
    ]
    tasks = [
        json.loads(line)["task"] for line in (KEY_DIR / "tasks.jsonl").read_text().splitlines()
    ]
    categories = sorted({i["expected_category"] for i in items})
    preds = []
    for _ in range(n):
        item = rng.choice(items)
        task = item["task"] if rng.random() < 0.8 else rng.choice(tasks)
        pred = _pred(task, rng.choice([*categories, None]))
        if rng.random() < 0.3:
            pred["root_cause"] = [item["item_id"]]
        if rng.random() < 0.4 and item["port"]["tests"]:
            test = rng.choice(item["port"]["tests"])
            pred["evidence_refs"] = [test["path"].split("/", 1)[-1]]
        if rng.random() < 0.3:
            pred["taxonomy_class"] = item["taxonomy_class"]
        preds.append(pred)
    return preds


def _cells(report: dict[str, Any], mode: str) -> dict[str, int]:
    """Credited findings (and credited predictions where defined) for every cell."""
    out = {}
    modes = report["modes"][mode]
    cells = [
        (f"{group}/{name}", cell)
        for group in ("headline", "split", "label_strength", "auditor_support", "taxonomy_class")
        for name, cell in modes[group].items()
    ] + [
        (f"exposure/{exposure}/{split}", cell)
        for exposure, by_split in modes["exposure_stratum"].items()
        for split, cell in by_split.items()
    ]
    for name, cell in cells:
        out[f"{name}:recall"] = cell["recall"]["k"]
        if cell["precision"] is not None:
            out[f"{name}:precision"] = cell["precision"]["k"]
    return out


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_mode_ladder_holds_on_every_cell_of_the_real_key(seed: int) -> None:
    key = scorer.load_key(KEY_DIR)
    report = scorer.score(key, _random_predictions(random.Random(seed), 150))
    ladder = [_cells(report, mode) for mode in LADDER]
    for cell in ladder[0]:
        values = [counts[cell] for counts in ladder]
        assert values == sorted(values), (cell, values)
    category = _cells(report, "category_1to1")
    broadcast = _cells(report, "legacy_task_broadcast")
    any_1to1 = _cells(report, "task_any_1to1")
    legacy_any = _cells(report, "legacy_any")
    for cell in category:
        assert broadcast[cell] >= category[cell]
        assert legacy_any[cell] >= any_1to1[cell] >= category[cell]


def test_exposure_and_label_strength_breakdowns() -> None:
    items = [
        _item("a1", "a"),
        _item("a2", "a", strength="confirmed", support=0),
        _item("b1", "b", support=2),
        _item("c1", "c", support=3),
        _item("d1", "d", split="dev", strength="confirmed", category="broken", taxonomy="x"),
    ]
    tasks = [
        _task("a", "test", "unexposed"),
        _task("b", "test", "inspected_only"),
        _task("c", "test", "tuned"),
        _task("d", "dev", "unexposed"),
    ]
    key = scorer.build_key(items, tasks)
    preds = [_pred("a", "weak"), _pred("c", "weak"), _pred("d", "broken"), _pred("b", None)]
    modes = scorer.score(key, preds)["modes"]["category_1to1"]
    exposure = modes["exposure_stratum"]
    assert exposure["unexposed"]["test"]["recall"] | {"wilson_95": None} == {
        "k": 1,
        "n": 2,
        "rate": 0.5,
        "wilson_95": None,
    }
    assert exposure["unexposed"]["all"]["recall"]["n"] == 3
    assert exposure["inspected_only"]["test"]["recall"]["k"] == 0
    assert exposure["tuned"]["test"]["recall"]["k"] == 1
    assert exposure["untuned"]["test"]["recall"]["n"] == 3
    assert exposure["untuned"]["test"]["precision"] | {"wilson_95": None} == {
        "k": 1,
        "n": 2,
        "rate": 0.5,
        "wilson_95": None,
    }
    assert modes["headline"]["untuned_test"] == exposure["untuned"]["test"]
    assert modes["headline"]["unexposed_test"] == exposure["unexposed"]["test"]
    lo, hi = exposure["unexposed"]["test"]["recall"]["wilson_95"]
    assert 0 < lo < 0.5 < hi < 1
    confirmed = modes["label_strength"]["confirmed"]
    assert (confirmed["recall"]["k"], confirmed["recall"]["n"]) == (1, 2)
    assert confirmed["precision"] is None and confirmed["f1"] is None
    assert modes["label_strength"]["pm_reported"]["recall"]["n"] == 3
    support = {name: cell["recall"]["n"] for name, cell in modes["auditor_support"].items()}
    assert support == {"0_github_only": 1, "1": 2, "2": 1, "3": 1}
    assert modes["taxonomy_class"]["x"]["recall"]["k"] == 1
    assert modes["split"]["dev"]["precision"]["k"] == 1


def test_non_claims_off_key_and_benchmark_predictions_are_not_scored() -> None:
    key = scorer.build_key([_item("x", "t")], [_task("t")])
    preds = [
        _pred("t", verdict="pass"),
        _pred("t", verdict="unknown"),
        _pred("elsewhere"),
        {"subject": {"kind": "task", "task_id": "__benchmark__/terminal-bench-4"}},
        {"category": "weak"},
        _pred("t"),
    ]
    tally = scorer.score(key, preds)["predictions"]
    assert tally == {
        "benchmark_scope": 1,
        "ignored_no_task": 1,
        "ignored_non_fail_verdict": 2,
        "off_key_task": 1,
        "scored": 1,
        "total": 6,
    }


def test_real_key_strata_partition_all_task_findings() -> None:
    key = scorer.load_key(KEY_DIR)
    assert len(key.findings) == 238 and key.excluded_benchmark_items == 3
    modes = scorer.score(key, [])["modes"]["exact_id"]
    n = {
        group: {k: v["recall"]["n"] for k, v in modes[group].items()}
        for group in ("headline", "split", "label_strength", "auditor_support", "taxonomy_class")
    }
    assert n["headline"]["all"] == n["split"]["dev"] + n["split"]["test"] == 238
    exposure = {k: v["all"]["recall"]["n"] for k, v in modes["exposure_stratum"].items()}
    assert exposure["unexposed"] + exposure["inspected_only"] + exposure["tuned"] == 238
    assert exposure["untuned"] == exposure["unexposed"] + exposure["inspected_only"]
    assert n["label_strength"] == {"confirmed": 10, "pm_reported": 228}
    assert sum(n["auditor_support"].values()) == 238
    # 18 of all 241 rows are GitHub-only; 2 of them are benchmark-scope context entries.
    assert n["auditor_support"]["0_github_only"] == 16
    assert sum(n["taxonomy_class"].values()) == 238


def test_fallback_exposure_rule_matches_the_key_rows() -> None:
    tasks = [json.loads(line) for line in (KEY_DIR / "tasks.jsonl").read_text().splitlines()]
    fallback = scorer.fallback_exposures(row["task"] for row in tasks)
    tuned = {name for name, row in fallback.items() if row["exposure_stratum"] == "tuned"}
    assert {"atrx-vep-crispr", "cargo-flight-dispatch", "wal-recovery-ordering"} <= tuned
    assert "qa-task-s2-prompt-dev" in fallback["atrx-vep-crispr"]["historical_exposures"]
    assert {row["exposure_stratum"] for row in fallback.values()} == {
        "unexposed",
        "inspected_only",
        "tuned",
    }
    keyed = [row for row in tasks if "exposure_stratum" in row]
    for row in keyed:
        assert row["exposure_stratum"] == fallback[row["task"]]["exposure_stratum"], row["task"]
        assert row["historical_exposures"] == fallback[row["task"]]["historical_exposures"]
    stripped = [{k: v for k, v in row.items() if k != "exposure_stratum"} for row in tasks]
    items = [json.loads(line) for line in (KEY_DIR / "items.jsonl").read_text().splitlines()]
    rebuilt = scorer.build_key(items, stripped, fallback)
    assert rebuilt.exposure_source.startswith("fallback")
    assert len(rebuilt.findings) == 238
    with pytest.raises(ValueError, match="no exposure stratum"):
        scorer.build_key(items, stripped)


def test_cli_benchmark_baselines_writes_the_evaluation_schema(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "scorer-evaluation.json"
    assert scorer.main(["--benchmark-baselines", "--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["schema"] == scorer.EVALUATION_SCHEMA
    assert payload["key_id"] == "tb4-audit-v1" and payload["paid_cost_usd"] == 0.0
    assert payload["modes"] == list(LADDER)
    assert payload["exposure"].get("fallback_disagreements", []) == []
    assert sum(payload["exposure"]["tasks"].values()) == 66
    assert sum(payload["exposure"]["task_findings"].values()) == 238
    baselines = payload["baselines"]
    assert set(baselines) == {
        "one_generic_weak_per_flagged_task",
        "one_per_task_category_oracle",
        "epoch_auditor_projection",
        "tokenless_auditor_projection",
        "tokenless_auditor_projection_direct",
        "confirmed_dev_witnesses",
    }
    for baseline in baselines.values():
        assert set(baseline["summary"]) == set(scorer.HEADLINE)
        assert baseline["score"]["schema"] == scorer.SCORE_SCHEMA
        for cell in baseline["summary"].values():
            values = [cell[mode]["matched"] for mode in LADDER]
            assert values == sorted(values)

    generic = baselines["one_generic_weak_per_flagged_task"]
    assert generic["n_predictions"] == 60
    assert generic["predictions_by_split"] == {"dev": 22, "test": 38}
    summary = generic["summary"]
    assert summary["all"]["legacy_any"]["recall"] == 1.0
    assert (summary["all"]["task_any_1to1"]["matched"], summary["all"]["task_any_1to1"]["of"]) == (
        60,
        238,
    )
    for cell in scorer.HEADLINE:
        inflation = summary[cell]["task_collision_inflation"]
        assert inflation["any"] > 0.5 and inflation["category"] > 0
        assert summary[cell]["exact_id"]["matched"] == 0

    oracle = baselines["one_per_task_category_oracle"]["summary"]["all"]
    assert oracle["category_1to1"]["precision"] == 1.0
    assert oracle["legacy_task_broadcast"]["recall"] == 1.0
    assert baselines["epoch_auditor_projection"]["n_predictions"] == 30
    epoch = baselines["epoch_auditor_projection"]["summary"]["all"]
    assert epoch["exact_id"]["matched"] == 25  # 26 joined findings; one flag covers two
    assert baselines["tokenless_auditor_projection"]["n_predictions"] == 24
    direct = baselines["tokenless_auditor_projection_direct"]["summary"]["all"]
    assert direct["exact_id"]["matched"] == 9
    witnesses = baselines["confirmed_dev_witnesses"]
    assert witnesses["n_predictions"] == 10 and witnesses["predictions_by_split"] == {"dev": 10}
    assert witnesses["summary"]["all"]["exact_id"]["matched"] == 0
    assert witnesses["summary"]["all"]["locus_and_class"]["matched"] == 10

    prevalence = payload["prevalence_by_stratum"]["strata"]
    test_tasks = payload["exposure"]["tasks"]
    assert {name: s["reviewed_tasks"] for name, s in prevalence.items()} == {
        "all_reviewed": 65,
        "dev_reviewed": 24,
        "test_reviewed": 41,
        "untuned_test_reviewed": test_tasks["test/unexposed"] + test_tasks["test/inspected_only"],
        "unexposed_test_reviewed": test_tasks["test/unexposed"],
    }
    every = prevalence["all_reviewed"]
    assert every["task_level"]["any_pm_finding"]["k"] == 60
    assert every["finding_level"]["findings"] == 238
    lo, hi = every["task_level"]["any_pm_finding"]["wilson_95"]
    assert lo < 60 / 65 < hi
    capture = every["capture_recapture"]["direct"]
    assert capture["pm_x_epoch"]["overlap"] == 26
    assert capture["pm_x_tokenless"]["overlap"] == 9
    assert capture["chao_pm_epoch_tokenless"]["undetected_lower_bound"] >= 0


def test_cli_scores_a_predictions_file(tmp_path: Path) -> None:
    item = json.loads((KEY_DIR / "items.jsonl").read_text().splitlines()[0])
    path = tmp_path / "findings.jsonl"
    rows = [
        _pred(item["task"], item["expected_category"], id="p1", root_cause=[item["item_id"]]),
        _pred(item["task"], "weak", verdict="pass"),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    out = tmp_path / "score.json"
    assert scorer.main(["--predictions", str(path), "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["predictions"]["scored"] == 1
    assert report["matches"]["exact_id"] == [["p1", item["item_id"]]]
    assert report["modes"]["exact_id"]["headline"]["all"]["precision"]["rate"] == 1.0
