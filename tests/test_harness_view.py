"""The Harness page data: one combined card per run and one combined scorecard, read-only."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evallab.production import harness_view as harness

NEEDLE = "label-needle-0001"
RUN = "retained-trace:run-a"


def _finding(**overrides: Any) -> dict[str, Any]:
    record = {
        "run_id": RUN, "detector": "facets", "detector_version": "run-facets/v1", "reader_model": "provider/reader",
        "verdict": "alarm", "score": 0.8, "kind": "cheat", "evidence_steps": [2, 5], "summary": "Read the hidden tests.",
        "input_tokens": 900, "output_tokens": 120, "cost_usd": 0.01, "latency_ms": 1500,
        "produced_at": "2026-10-28T10:00:00+00:00",
    }
    return record | overrides


def _write_lines(path: Path, rows: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows), encoding="utf-8")
    return path


def _card(records: list[dict[str, Any]], *, steps: set[int] | None = None, actor: bool = True, facet: Any = None,
          known: set[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    parsed = [harness.parse_finding(record) for record in records]
    keyed = {(item["detector"], item["detector_version"], item["reader_model"] or ""): item for item in parsed}
    return harness.build_card(
        {"run_id": RUN, "task_id": "task-a", "step_count": 12, "trust_tier": "verified"}, keyed,
        steps={0, 2, 5, 9} if steps is None else steps, actor_inputs=actor, facet=facet, known=known or set(keyed),
    )


def test_a_card_lists_every_detector_with_run_health_in_its_own_group() -> None:
    card = _card([
        _finding(),
        _finding(detector="cascade", detector_version="window-cascade/v8", reader_model=None, verdict="clear", score=0.1),
        _finding(detector="run_health.loop", detector_version="rh/v1", reader_model=None, verdict="alarm", kind="loop",
                 evidence_steps=[9], summary="The same command ran nine times."),
    ])
    groups = {group["family"]: [item["detector"] for item in group["results"]] for group in card["groups"]}
    assert groups == {"trace": ["cascade", "facets"], "run_health": ["run_health.loop"]}
    assert card["tally"] == {"alarm": 2, "clear": 1, "abstain": 0, "unknown": 0, "results": 3}
    assert "2 of 3 detector results alarm" in card["headline"]
    assert card["totals"]["cost_usd"] == pytest.approx(0.03)


def test_a_run_nobody_read_is_not_a_clean_run() -> None:
    card = _card([], known={("facets", "run-facets/v1", "provider/reader")})
    assert "not a clean result" in card["headline"]
    assert card["no_result"] == ["facets run-facets/v1 (provider/reader)"]
    assert all(group["results"] == [] for group in card["groups"])


def test_a_clear_on_a_run_with_no_actor_transcript_is_shown_unknown() -> None:
    card = _card([_finding(verdict="clear"), _finding(detector="scout", verdict="abstain")], actor=False)
    shown = {item["detector"]: item for group in card["groups"] for item in group["results"]}
    assert shown["facets"]["verdict"] == "unknown" and shown["facets"]["recorded_verdict"] == "clear"
    assert shown["scout"]["verdict"] == "abstain" and shown["scout"]["recorded_verdict"] is None
    assert card["tally"]["clear"] == 0 and card["tally"]["unknown"] == 1
    assert card["run"]["actor_inputs_available"] is False and "no actor transcript" in card["notice"]


def test_cited_steps_outside_the_run_are_hidden_and_counted() -> None:
    card = _card([_finding(evidence_steps=[2, 5, 77])], steps={2, 5})
    only = card["groups"][0]["results"][0]
    assert only["evidence_steps"] == [2, 5] and only["hidden_steps"] == 1


def test_the_newest_record_per_detector_version_and_reader_wins(tmp_path: Path) -> None:
    path = _write_lines(tmp_path / "findings.jsonl", [
        _finding(verdict="alarm", produced_at="2026-10-28T11:00:00+00:00"),
        _finding(verdict="clear", produced_at="2026-10-28T09:00:00+00:00"),
        _finding(reader_model="provider/other", verdict="abstain"),
    ])
    loaded = harness.load_findings(path)
    kept = loaded["by_run"][RUN]
    assert kept[("facets", "run-facets/v1", "provider/reader")]["verdict"] == "alarm"
    assert kept[("facets", "run-facets/v1", "provider/other")]["verdict"] == "abstain"


def test_nothing_a_label_or_a_task_could_carry_reaches_the_card_or_the_overview(tmp_path: Path) -> None:
    extra = {"label": NEEDLE, "reward": 1.0, "outcome": NEEDLE, "task_text": NEEDLE, "dataset_id": NEEDLE, "model_id": NEEDLE}
    path = _write_lines(tmp_path / "findings.jsonl", [_finding(**extra)])
    loaded = harness.load_findings(path)
    card = harness.build_card(
        {"run_id": RUN, "task_id": "task-a", "step_count": 1, "trust_tier": "verified"}, loaded["by_run"][RUN],
        steps={2, 5}, actor_inputs=True,
        facet={"goal": "g", "approach": "a", "suspicion": 2, "outcome": NEEDLE, "input_tokens": 1},
        known=set(loaded["by_run"][RUN]),
    )
    overview = harness.build_overview(loaded, {"present": False, "error": "not retained", "rows": []}, {RUN: "task-a"}, {RUN})
    for served in (card, overview):
        assert NEEDLE not in json.dumps(served)
    assert "outcome" not in card["facet_lead"]


REFUSED: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("not a detector identifier", lambda r: r.update(detector="Facets Detector")),
    ("verdict outside the contract", lambda r: r.update(verdict="clean")),
    ("kind outside the contract", lambda r: r.update(kind="cheating")),
    ("reader without a provider", lambda r: r.update(reader_model="reader")),
    ("negative cost", lambda r: r.update(cost_usd=-0.1)),
    ("non-finite score", lambda r: r.update(score=float("inf"))),
    ("boolean token count", lambda r: r.update(input_tokens=True)),
    ("fractional latency", lambda r: r.update(latency_ms=1.5)),
    ("more than twenty steps", lambda r: r.update(evidence_steps=list(range(21)))),
    ("negative step", lambda r: r.update(evidence_steps=[-1])),
    ("summary over forty words", lambda r: r.update(summary=" ".join(["word"] * 41))),
    ("no time zone", lambda r: r.update(produced_at="2026-10-28T10:00:00")),
    ("no run", lambda r: r.pop("run_id")),
]


@pytest.mark.parametrize(("name", "mutate"), REFUSED, ids=[name for name, _ in REFUSED])
def test_a_record_outside_the_contract_is_refused_and_counted_never_stored(
    tmp_path: Path, name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    bad = _finding()
    mutate(bad)
    path = _write_lines(tmp_path / "findings.jsonl", [bad, _finding(run_id="retained-trace:run-b"), "{not json", '{"a":1,"a":2}'])
    loaded = harness.load_findings(path)
    assert set(loaded["by_run"]) == {"retained-trace:run-b"}
    assert sum(loaded["refused"].values()) == 3 and loaded["lines"] == 4
    assert NEEDLE not in json.dumps(loaded["refused"])


def test_the_findings_file_states_absent_linked_and_oversize_never_echo_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert harness.load_findings(tmp_path / "missing.jsonl")["error"] == "not retained"
    real = _write_lines(tmp_path / "real.jsonl", [_finding()])
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert harness.load_findings(link)["error"] == "unreadable findings file"
    monkeypatch.setattr(harness, "MAX_FINDINGS_BYTES", 10)
    assert harness.load_findings(real)["present"] is False


def test_the_overview_counts_admitted_runs_only_and_orders_alarms_first(tmp_path: Path) -> None:
    rows = [
        _finding(run_id="r1"), _finding(run_id="r1", detector="run_health.infra", reader_model=None, kind="infra"),
        _finding(run_id="r2", verdict="clear"), _finding(run_id="r2", detector="scout", reader_model=None, verdict="abstain"),
        _finding(run_id="r3"), _finding(run_id="elsewhere"),
    ]
    loaded = harness.load_findings(_write_lines(tmp_path / "findings.jsonl", rows))
    overview = harness.build_overview(loaded, {"present": False, "error": "not retained", "rows": []},
                                      {"r1": "t1", "r2": "t2", "r3": "t3"}, {"r1", "r3"})["findings"]
    assert (overview["runs_recorded"], overview["runs_admitted"], overview["runs_outside"]) == (4, 3, 1)
    assert [item["run_id"] for item in overview["flagged"]] == ["r1", "r3"] and overview["alarmed_runs"] == 2
    by_name = {(row["detector"], row["family"]): row for row in overview["detectors"]}
    # r2 has no actor transcript: its recorded clear counts as unknown, never clean
    assert by_name[("facets", "trace")]["alarm"] == 2 and by_name[("facets", "trace")]["clear"] == 0
    assert by_name[("facets", "trace")]["unknown"] == 1
    assert by_name[("run_health.infra", "run_health")]["runs"] == 1


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "corpus": "cheatbench", "detectors": ["facets"], "rule": "single", "reader_model": "provider/reader",
        "positives": 75, "negatives": 75,
        "budgets": [{"far_budget": 0.03, "hits": 20, "false_alarms": 2}, {"far_budget": 0.05, "hits": 30, "false_alarms": 4}],
        "localization": {"hits": 10, "scored": 30}, "cost_usd_per_1k_runs": 12.5, "latency_ms_median": 900,
        "published": {"check": "40.0% recall at 5.3% false alarms", "reproduced": True},
    }
    return row | overrides


def _scorecard(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"schema": "evallab.trace_scorecard/v1", "generated_at": "2026-10-28T12:00:00+00:00", "rows": list(rows)}


def test_the_scorecard_serves_counts_with_the_reader_and_flags_a_combination_of_unreproduced_parts(tmp_path: Path) -> None:
    path = tmp_path / "scorecard.json"
    path.write_text(json.dumps(_scorecard(
        _row(),
        _row(detectors=["cascade"], reader_model="none", published={"check": "25.3% at 0.0%", "reproduced": False}),
        _row(detectors=["facets", "cascade"], rule="any", reader_model="provider/reader", published=None),
    )), encoding="utf-8")
    loaded = harness.load_scorecard(path)
    assert loaded["present"] and [row["rule"] for row in loaded["rows"]] == ["single", "single", "any"]
    combo = loaded["rows"][2]
    assert combo["unreproduced_parts"] == ["cascade"] and combo["reader_model"] == "provider/reader"
    assert loaded["rows"][0]["budgets"][0] == {"far_budget": 0.03, "hits": 20, "false_alarms": 2}


SCORECARD_REFUSED: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = [
    ("hits above positives", lambda s: _scorecard(_row(positives=10))),
    ("false alarms above negatives", lambda s: _scorecard(_row(negatives=3))),
    ("localization above scored", lambda s: _scorecard(_row(localization={"hits": 5, "scored": 4}))),
    ("single rule with two detectors", lambda s: _scorecard(_row(detectors=["facets", "cascade"]))),
    ("combination with one detector", lambda s: _scorecard(_row(rule="any"))),
    ("published number on a combination", lambda s: _scorecard(_row(detectors=["a", "b"], rule="all"))),
    ("an unnamed reader", lambda s: _scorecard(_row(reader_model="reader"))),
    ("an unknown budget", lambda s: _scorecard(_row(budgets=[{"far_budget": 0.1, "hits": 1, "false_alarms": 0}]))),
    ("an unknown field", lambda s: _scorecard(_row(task_id=NEEDLE))),
    ("two identical rows", lambda s: _scorecard(_row(), _row())),
    ("a negative cost", lambda s: _scorecard(_row(cost_usd_per_1k_runs=-1.0))),
    ("another schema", lambda s: s | {"schema": "other/v1"}),
]


@pytest.mark.parametrize(("name", "build"), SCORECARD_REFUSED, ids=[name for name, _ in SCORECARD_REFUSED])
def test_a_scorecard_that_would_misstate_a_measure_is_refused_without_echoing_it(
    tmp_path: Path, name: str, build: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    path = tmp_path / "scorecard.json"
    path.write_text(json.dumps(build(_scorecard(_row()))), encoding="utf-8")
    loaded = harness.load_scorecard(path)
    assert loaded["present"] is False and loaded["error"] == "invalid counts-only scorecard" and loaded["rows"] == []
    assert NEEDLE not in json.dumps(loaded)


def test_the_scorecard_states_absent_and_unreadable(tmp_path: Path) -> None:
    assert harness.load_scorecard(tmp_path / "none.json")["error"] == "not retained"
    path = tmp_path / "scorecard.json"
    path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    assert harness.load_scorecard(path)["error"] == "unreadable scorecard"
