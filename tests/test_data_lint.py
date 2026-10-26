"""Unit tests for the deterministic training-data lints (pE-SELF-9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.data_lint import (
    body_of,
    lint_dataset,
    lint_registry_split_leak,
    lint_rows,
    lint_split_leak,
)


def _row(task: str, body: str) -> dict[str, object]:
    return {"task_id": task, "files": {"submission/solve.sh": body}}


def test_body_of_reads_every_row_shape() -> None:
    assert body_of(_row("a", "x")) == "x"
    assert body_of({"task": "a", "submission": {"submission/solve.sh": "y"}}) == "y"
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "submission/solve.sh", "content": "z"}),
                    }
                }
            ],
        }
    ]
    assert body_of({"task_id": "a", "messages": messages}) == "z"
    assert body_of({"task_id": "a"}) is None


def test_dedup_reports_distinct_pairs_not_rows() -> None:
    findings = lint_rows([_row("a", "x"), _row("a", "x"), _row("b", "y")])
    dedup = [f for f in findings if f["check"] == "dedup" and f["severity"] == "high"]
    assert len(dedup) == 1
    assert dedup[0]["rows"] == 3
    assert dedup[0]["distinct_task_body_pairs"] == 2
    assert dedup[0]["extra_copies"] == 1


def test_dedup_is_quiet_when_every_row_is_distinct() -> None:
    findings = lint_rows([_row("a", "x"), _row("b", "y")])
    assert not [f for f in findings if f["check"] == "dedup" and f["severity"] == "high"]


@pytest.mark.parametrize("split", ["test", "heldout"])
def test_split_leak_flags_only_held_out_tasks(tmp_path: Path, split: str) -> None:
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(
        json.dumps({"task": "fixture/leaky", "split": split})
        + "\n"
        + json.dumps({"task": "fixture/ok", "split": "dev"})
        + "\n",
        encoding="utf-8",
    )
    findings = lint_split_leak([_row("fixture/leaky", "x"), _row("ok", "y")], keys)
    assert len(findings) == 1
    assert findings[0]["leaked_tasks"] == ["leaky"]


@pytest.mark.parametrize("split", ["test", "heldout"])
def test_registry_split_leak_flags_a_dev_family_holding_held_out_tasks(
    tmp_path: Path, split: str
) -> None:
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(
        json.dumps({"task": "fixture/leaky", "split": split})
        + "\n"
        + json.dumps({"task": "fixture/ok", "split": "dev"})
        + "\n",
        encoding="utf-8",
    )
    registry = tmp_path / "gym.json"
    registry.write_text(
        json.dumps(
            {
                "pairs": [
                    {"family": "tb4-dev", "task": "fixture/leaky"},
                    {"family": "tb4-dev", "task": "ok"},
                    {"family": "tb4-test", "task": "fixture/leaky"},
                ]
            }
        ),
        encoding="utf-8",
    )
    findings = lint_registry_split_leak(registry, keys)
    assert len(findings) == 1
    assert findings[0]["leaked_tasks"] == ["leaky"]
    assert findings[0]["dev_tasks"] == ["ok"]


def test_dataset_reports_dedup_and_coverage_without_split_leak(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    rows = [
        {**_row("a", "x"), "class": "class-a", "exploit_found": True},
        {**_row("a", "x"), "mechanism_task_class": "class-a", "exploit_found": True},
        {**_row("b", "y"), "class": "class-b", "exploit_found": False},
    ]
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(json.dumps({"task": "fixture/unrelated", "split": "test"}) + "\n")

    report = lint_dataset(dataset, answer_keys=keys)

    assert report["rows"] == 3
    assert report["status"] == "issues"
    dedup = [f for f in report["findings"] if f["check"] == "dedup" and f["severity"] == "high"]
    assert len(dedup) == 1
    assert dedup[0]["distinct_task_body_pairs"] == 2
    assert not [f for f in report["findings"] if f["check"] == "split_leak"]
    coverage = [f for f in report["findings"] if f["check"] == "coverage"]
    assert len(coverage) == 1
    assert coverage[0]["classes"] == {"class-a": 2, "class-b": 1}
    assert coverage[0]["positives"] == 2
    assert coverage[0]["negatives"] == 1


def test_dataset_checks_iterable_includes_registry_leaks(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps(_row("ok", "x")) + "\n", encoding="utf-8")
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(json.dumps({"task": "fixture/leaky", "split": "test"}) + "\n")
    registry = tmp_path / "gym.json"
    registry.write_text(json.dumps({"pairs": [{"family": "tb4-dev", "task": "leaky"}]}))

    report = lint_dataset(dataset, answer_keys=keys, registry=registry, checks=iter(["split_leak"]))

    assert report["status"] == "issues"
    assert report["checks"] == ["split_leak"]
    assert len(report["findings"]) == 1
    assert report["findings"][0]["subject"] == "registry:tb4-dev"
    assert report["findings"][0]["leaked_tasks"] == ["leaky"]


def test_dedup_does_not_report_missing_bodies_as_duplicates() -> None:
    assert lint_rows([{"task": "a"}, _row("b", "x")], checks=["dedup"]) == []


@pytest.mark.parametrize(
    ("rows", "expected_exit"),
    [
        ([_row("a", "x"), _row("b", "y")], 0),
        ([_row("a", "x"), _row("a", "x")], 1),
        ([_row("a", "x"), _row("b", "x"), _row("c", "x")], 1),
        ([_row("a", "x"), _row("b", "x")], 0),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_data_lint_cli_exit_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rows: list[dict[str, object]],
    expected_exit: int,
    json_output: bool,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    args = ["data", "lint", dataset.name]
    if json_output:
        args.append("--json")

    assert cli.run_cli(args, workspace=tmp_path) == expected_exit

    captured = capsys.readouterr()
    assert captured.err == ""
    status = "issues" if expected_exit else "clean"
    if json_output:
        report = json.loads(captured.out)
        assert report["status"] == status
        assert report["dataset"] == str(dataset)
        assert report["rows"] == len(rows)
    else:
        assert f"({len(rows)} rows) -> {status}" in captured.out


def test_data_lint_cli_resolves_split_inputs_and_check_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps(_row("leaky", "x")) + "\n", encoding="utf-8")
    keys = tmp_path / "tasks.jsonl"
    keys.write_text(json.dumps({"task": "fixture/leaky", "split": "heldout"}) + "\n")
    registry = tmp_path / "gym.json"
    registry.write_text(json.dumps({"pairs": [{"family": "tb4-dev", "task": "leaky"}]}))
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)

    assert (
        cli.run_cli(
            [
                "data",
                "lint",
                dataset.name,
                "--answer-keys",
                keys.name,
                "--registry",
                registry.name,
                "--checks",
                "split_leak",
                "--json",
            ],
            workspace=tmp_path,
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["checks"] == ["split_leak"]
    assert [f["subject"] for f in report["findings"]] == ["dataset", "registry:tb4-dev"]


def test_dataset_check_selection_and_custom_body_path(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    row = {"task": "a", "files": {"payload.txt": "x"}}
    dataset.write_text((json.dumps(row) + "\n") * 2, encoding="utf-8")

    report = lint_dataset(
        dataset,
        body_path="payload.txt",
        checks=["dedup"],
        answer_keys=tmp_path / "absent-keys.jsonl",
        registry=tmp_path / "absent-registry.json",
    )

    assert report["status"] == "issues"
    assert report["checks"] == ["dedup"]
    assert report["findings"][0]["distinct_task_body_pairs"] == 1


@pytest.mark.parametrize("content", [None, "not JSON"])
def test_data_lint_cli_input_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    if content is not None:
        dataset.write_text(content, encoding="utf-8")
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)

    assert cli.run_cli(["data", "lint", dataset.name], workspace=tmp_path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
