from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import atif, cli

TOP_LEVEL_COMMANDS = (
    "claims",
    "doctor",
    "dashboard",
    "status",
    "preflight",
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
    "curve",
    "power",
    "report",
    "analyze",
    "data",
    "db",
    "lineage",
    "analyst",
    "card",
    "behavior",
    "semantic-facts",
    "semantics",
    "evidence",
    "tasks",
    "ladder",
    "trace",
    "fetch",
    "gc",
    "registry",
    "tidy",
    "verdict",
    "traj",
)
NESTED_COMMANDS = (
    ("claims", "pack"),
    ("schedule", "install"),
    ("canary", "import-terminal-bench"),
    ("curve", "validate"),
    ("curve", "build"),
    ("curve", "report"),
    ("registry", "list"),
    ("registry", "audit"),
    ("report", "family"),
    ("report", "card"),
    ("analyze", "plan"),
    ("analyze", "trial"),
    ("analyze", "batch"),
    ("analyze", "inspect"),
    ("analyze", "calibrate"),
    ("analyze", "stub"),
    ("analyze", "ingest-sidecar"),
    ("analyze", "review"),
    ("analyze", "agreement"),
    ("db", "init"),
    ("analyze", "worker-run-one"),
    ("data", "backfill"),
    ("db", "list"),
    ("analyst", "run"),
    ("analyst", "list"),
    ("analyst", "show"),
    ("card", "generate"),
    ("semantics", "project"),
    ("semantics", "coverage"),
    ("evidence", "archive"),
    ("evidence", "restore"),
    ("tasks", "import"),
    ("ladder", "generate"),
    ("traj", "outline"),
    ("traj", "queue"),
    ("ladder", "validate"),
    ("traj", "label"),
    ("traj", "project"),
    ("traj", "report"),
    ("semantic-facts", "project"),
    ("semantic-facts", "query"),
)
HELP_PATHS = tuple((command,) for command in TOP_LEVEL_COMMANDS) + NESTED_COMMANDS


def test_cli_inventory_matches_help_audit() -> None:
    command_action = next(
        action for action in cli.parser()._actions if isinstance(action, argparse._SubParsersAction)
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
        lambda *args, **kwargs: (
            calls.append("ingest-and-project")
            or SimpleNamespace(tables=(), row_counts={}, failures=())
        ),
    )
    monkeypatch.setattr(cli, "record_projection_failures", lambda *args, **kwargs: None)
    monkeypatch.setattr(atif, "project_trial", lambda job, trial: None)

    assert cli.run_cli(["trajectories", "evidence", "--export"], workspace=tmp_path) == 0
    assert calls == ["ingest-and-project"]


def test_report_family_default_reads_shared_parquet_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "primary/derived/parquet"
    captured: dict[str, Path] = {}
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(cli, "derived_root_from_environment", lambda _root: shared)

    def read_report(_task, *, parquet_root, raw_roots):
        captured["parquet"] = parquet_root
        return {}

    monkeypatch.setattr(cli, "family_report", read_report)
    monkeypatch.setattr(
        cli,
        "write_family_report",
        lambda *args, **kwargs: pytest.fail("read-only report wrote derived state"),
    )
    monkeypatch.setattr(cli, "render_family_report", lambda _report: "report")

    assert cli.run_cli(["report", "family", "task-family"], workspace=tmp_path) == 0
    assert captured == {"parquet": shared}


def test_report_card_default_renders_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(
        cli,
        "build_eval_card",
        lambda *args, **kwargs: ("card", {"spec_digest": "sha256:test"}),
    )
    monkeypatch.setattr(
        cli,
        "draft_eval_card",
        lambda *args, **kwargs: pytest.fail("read-only report wrote an eval card"),
    )

    assert cli.run_cli(["report", "card", "done.json"], workspace=tmp_path) == 0
