"""The gate-rerun progress file holds counts only and never turns a missing count into a zero."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evallab.production import gate_progress as progress

TASK_NEEDLE = "task-secret-needle-0001"
KEY_NEEDLE = "key-body-needle-0002"
TEXT_NEEDLE = "finding text needle 0003"
SONNET, KIMI = "anthropic/claude-sonnet-5-5", "kimi-code/kimi-for-coding"


def _gate(matched: int | None, eligible: int | None, keys: int) -> dict[str, Any]:
    return {
        "matched_keys": matched, "eligible_findings": eligible, "evaluable_keys": keys,
        "key_backed_precision": 0.5, "key_text": KEY_NEEDLE,
    }


def _group(route: str, arm: str, *, answered: int, backlog: int, matched: int | None, eligible: int | None,
           old: tuple[int | None, int | None], keys: int, failed: int = 0) -> dict[str, Any]:
    return {
        "active": True, "phase": "unique_union", "stage": "dev", "auditor_route": route, "arm": arm,
        "represented_tasks": 399, "auditor_answered": answered, "auditor_dispatched": answered + failed,
        "auditor_unsent_backlog": backlog, "terminal_consumed_failures": failed,
        "scorer_only_unsent_backlog": 0, "fully_union_scored": answered, "case_ids": [TASK_NEEDLE],
        "quality": {
            "planned_tasks": 399, "planned_keys": 118, "evaluable_keys": keys, "finding_text": TEXT_NEEDLE,
            "gates": {"new_verified_0_5": _gate(matched, eligible, keys), "old_0_8": _gate(old[0], old[1], keys)},
        },
    }


def _coverage() -> dict[str, Any]:
    return {
        "schema": progress.COVERAGE_SCHEMA, "true_population_false_positive_rate": None,
        "unkeyed_tasks_are_not_certified_negatives": True, "task_text": TEXT_NEEDLE,
        "unique_union_groups": [
            _group(SONNET, "A", answered=64, backlog=335, matched=4, eligible=43, old=(0, 4), keys=118),
            _group(SONNET, "B", answered=63, backlog=336, matched=8, eligible=50, old=(0, 4), keys=118),
            _group(KIMI, "A", answered=0, backlog=399, matched=None, eligible=None, old=(None, None), keys=0),
            {**_group(SONNET, "A", answered=12, backlog=0, matched=1, eligible=13, old=(0, 0), keys=24), "stage": "cross"},
            {**_group(KIMI, "B", answered=0, backlog=399, matched=None, eligible=None, old=(None, None), keys=0), "active": False},
        ],
    }


def _checkpoint() -> dict[str, Any]:
    cohort = {
        "DEV_tasks": 399, "DEV_keyed_tasks": 44, "DEV_unkeyed_tasks_not_certified_negatives": 355,
        "DEV_evaluable_annotations": 118,
    }
    return {
        "schema": progress.CHECKPOINT_SCHEMA, "cohort": cohort, "key_body": KEY_NEEDLE,
        "groups": [
            {"stage": "dev", "auditor_route": SONNET, "arm": "A", "auditor_answered": 44},
            {"stage": "dev", "auditor_route": SONNET, "arm": "B", "auditor_answered": 44},
            {"stage": "dev", "auditor_route": KIMI, "arm": "A", "auditor_answered": 0},
            {"stage": "cross", "auditor_route": SONNET, "arm": "A", "auditor_answered": 12},
        ],
    }


def _build(coverage: dict[str, Any] | None = None, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    return progress.from_summaries(
        coverage or _coverage(), checkpoint or _checkpoint(),
        coverage_sha256="a" * 64, checkpoint_sha256="b" * 64, exported_at="2026-10-28T12:00:00+00:00",
    )


def test_the_export_copies_named_counts_and_nothing_a_task_or_key_could_carry() -> None:
    document = _build()
    body = json.dumps(document)
    for needle in (TASK_NEEDLE, KEY_NEEDLE, TEXT_NEEDLE):
        assert needle not in body
    rows = {(row["route"], row["arm"]): row for row in document["rows"]}
    assert set(rows) == {(SONNET, "A"), (SONNET, "B"), (KIMI, "A")}  # DEV, active, unique-union only
    sonnet = rows[(SONNET, "A")]
    assert (sonnet["answered"], sonnet["quota_pending"], sonnet["baseline_answered"]) == (64, 335, 44)
    assert sonnet["new_0_5"] == {"matched_keys": 4, "eligible_findings": 43}
    assert sonnet["old_0_8"] == {"matched_keys": 0, "eligible_findings": 4}
    assert document["cohort"] == {"dev_tasks": 399, "keyed_tasks": 44, "unkeyed_tasks": 355, "planned_keys": 118}


def test_a_model_that_has_read_nothing_is_unavailable_not_zero() -> None:
    kimi = next(row for row in _build()["rows"] if row["route"] == KIMI)
    assert kimi["new_0_5"] is None and kimi["old_0_8"] is None and kimi["keys_read"] == 0
    assert (kimi["answered"], kimi["quota_pending"], kimi["baseline_answered"]) == (0, 399, 0)


def _mutated(mutate: Callable[[dict[str, Any], dict[str, Any]], None]) -> dict[str, Any]:
    coverage, checkpoint = copy.deepcopy(_coverage()), copy.deepcopy(_checkpoint())
    mutate(coverage, checkpoint)
    return _build(coverage, checkpoint)


def _first(coverage: dict[str, Any]) -> dict[str, Any]:
    return coverage["unique_union_groups"][0]


REFUSED: list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], None]]] = [
    ("coverage schema", lambda c, k: c.update(schema="other/v1")),
    ("checkpoint schema", lambda c, k: k.update(schema="other/v1")),
    ("population false-alarm rate present", lambda c, k: c.update(true_population_false_positive_rate=0.1)),
    ("unkeyed treated as negatives", lambda c, k: c.update(unkeyed_tasks_are_not_certified_negatives=False)),
    ("other cohort", lambda c, k: k["cohort"].update(DEV_tasks=398, DEV_unkeyed_tasks_not_certified_negatives=354)),
    ("keys differ", lambda c, k: _first(c)["quality"].update(planned_keys=117)),
    ("count is a string", lambda c, k: _first(c).update(auditor_answered="64")),
    ("count is a boolean", lambda c, k: _first(c).update(auditor_answered=True)),
    ("answered above consumed", lambda c, k: _first(c).update(auditor_dispatched=63)),
    ("consumed and pending exceed the cohort", lambda c, k: _first(c).update(auditor_unsent_backlog=400)),
    ("match without a finding", lambda c, k: _first(c)["quality"]["gates"]["new_verified_0_5"].update(eligible_findings=3)),
    ("matches above keys read", lambda c, k: _first(c)["quality"]["gates"]["old_0_8"].update(matched_keys=119, eligible_findings=119)),
    ("a route that is not an identifier", lambda c, k: _first(c).update(auditor_route=f"x/{TEXT_NEEDLE}")),
    ("a gate read without the other", lambda c, k: _first(c)["quality"]["gates"]["old_0_8"].update(matched_keys=None, eligible_findings=None)),
    ("checkpoint answered above now", lambda c, k: k["groups"][0].update(auditor_answered=65)),
]


@pytest.mark.parametrize(("name", "mutate"), REFUSED, ids=[name for name, _ in REFUSED])
def test_a_summary_that_would_misstate_progress_is_refused(
    name: str, mutate: Callable[[dict[str, Any], dict[str, Any]], None]
) -> None:
    with pytest.raises((ValueError, ValidationError, KeyError, TypeError)):
        _mutated(mutate)


def _write(path: Path, document: Any) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_served_file_is_absent_partial_or_finished_and_never_echoes_a_bad_file(tmp_path: Path) -> None:
    target = tmp_path / "task-qa" / progress.FILE
    target.parent.mkdir()
    absent = progress.load_progress(target)
    assert (absent["present"], absent["error"], absent["progress"]) == (False, "not retained", None)

    _write(target, _build())
    partial = progress.load_progress(target)
    assert partial["present"] and partial["progress"]["all_answered"] is False
    assert partial["progress"]["scope"] == "keyed subset, not population false-alarm rate"
    assert [row["route"] for row in partial["progress"]["rows"]] == [SONNET, SONNET, KIMI]

    finished = _build()
    for row in finished["rows"]:
        row.update(answered=399, consumed=399, quota_pending=0, fully_scored=399, keys_read=118,
                   new_0_5={"matched_keys": 9, "eligible_findings": 90}, old_0_8={"matched_keys": 0, "eligible_findings": 5})
    _write(target, finished)
    assert progress.load_progress(target)["progress"]["all_answered"] is True

    for bad in (
        {**_build(), TASK_NEEDLE: 1},
        {**_build(), "rows": [{**_build()["rows"][0], "route": f"x/{TEXT_NEEDLE}"}]},
    ):
        _write(target, bad)
        broken = progress.load_progress(target)
        assert broken["present"] is False and broken["error"] == "invalid counts-only gate progress"
        assert TASK_NEEDLE not in json.dumps(broken) and TEXT_NEEDLE not in json.dumps(broken)
    target.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    assert progress.load_progress(target)["error"] == "unreadable counts-only gate progress"


def test_the_cli_exports_the_file_and_refuses_to_overwrite_a_different_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    coverage, checkpoint = _write(tmp_path / "coverage.json", _coverage()), _write(tmp_path / "checkpoint.json", _checkpoint())
    arguments = [
        "prod", "export-gate-progress", "--coverage-summary", str(coverage),
        "--checkpoint-summary", str(checkpoint), "--output-directory", str(tmp_path / "task-qa"),
    ]
    assert cli.run_cli(arguments, workspace=tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["command"] == "export-gate-progress" and receipt["rows"] == 3
    written = (tmp_path / "task-qa" / progress.FILE).read_text(encoding="utf-8")
    assert not any(needle in written for needle in (TASK_NEEDLE, KEY_NEEDLE, TEXT_NEEDLE))
    (tmp_path / "task-qa" / progress.FILE).write_text("{}", encoding="utf-8")
    assert cli.run_cli(arguments, workspace=tmp_path) != 0
    assert "already differs" in capsys.readouterr().err
    assert cli.run_cli([*arguments, "--replace"], workspace=tmp_path) == 0


def test_the_exporter_refuses_a_linked_summary(tmp_path: Path) -> None:
    real = _write(tmp_path / "real.json", _coverage())
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        progress.export_progress(link, _write(tmp_path / "checkpoint.json", _checkpoint()))
