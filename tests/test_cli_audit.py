from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import atif, cli

TOP_LEVEL_COMMANDS = (
    "doctor",
    "dashboard",
    "submit",
    "tick",
    "approve",
    "reject",
    "stop",
    "resume",
    "schedule",
    "digest",
    "nightly",
    "research",
    "canary",
    "calibrate",
    "run",
    "matrix",
    "summarize",
    "ingest",
    "trajectories",
    "compare",
    "analyze",
    "db",
    "trace",
    "fetch",
    "gc",
)
NESTED_COMMANDS = (
    ("schedule", "install"),
    ("canary", "import-terminal-bench"),
    ("analyze", "plan"),
    ("analyze", "stub"),
    ("analyze", "ingest-sidecar"),
    ("analyze", "review"),
    ("analyze", "agreement"),
    ("db", "init"),
    ("db", "list"),
)
HELP_PATHS = tuple((command,) for command in TOP_LEVEL_COMMANDS) + NESTED_COMMANDS


def test_cli_inventory_matches_help_audit() -> None:
    command_action = next(
        action
        for action in cli.parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert tuple(command_action.choices) == TOP_LEVEL_COMMANDS


@pytest.mark.parametrize("command_path", HELP_PATHS)
def test_every_cli_command_path_responds_to_help(
    command_path: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.parser().parse_args([*command_path, "--help"])

    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_trajectories_default_action_is_read_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(
        cli,
        "load_jobs",
        lambda _paths: [SimpleNamespace(name="completed-job", trials=())],
    )
    monkeypatch.setattr(
        cli,
        "ingest_and_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("read-only trajectories action wrote derived state")
        ),
    )

    assert cli.run_cli(["trajectories", "evidence"], workspace=tmp_path) == 0


def test_trajectories_export_rebuilds_derived_stores(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(
        cli,
        "load_jobs",
        lambda _paths: [SimpleNamespace(name="completed-job", trials=())],
    )
    monkeypatch.setattr(
        cli,
        "ingest_and_project",
        lambda *args, **kwargs: calls.append("ingest-and-project")
        or SimpleNamespace(tables=(), row_counts={}, failures=()),
    )
    monkeypatch.setattr(cli, "record_projection_failures", lambda *args, **kwargs: None)
    monkeypatch.setattr(atif, "project_trial", lambda job, trial: None)

    assert (
        cli.run_cli(["trajectories", "evidence", "--export"], workspace=tmp_path)
        == 0
    )
    assert calls == ["ingest-and-project"]
