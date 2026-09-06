from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import cli
from evallab.evidence import atif

TOP_LEVEL_COMMANDS = (
    "claims",
    "doctor",
    "dashboard",
    "status",
    "preflight",
    "run-preflight",
    "submit",
    "tick",
    "approve",
    "reject",
    "stop",
    "resume",
    "campaign",
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
    "inspect-ingest",
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


def test_cli_inventory_matches_help_audit() -> None:
    command_action = next(
        action for action in cli.parser()._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert tuple(command_action.choices) == TOP_LEVEL_COMMANDS


def test_every_cli_command_path_responds_to_help() -> None:
    def _collect_leaf_subparsers(
        parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
    ) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
        subparsers_actions = [
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        ]
        if not subparsers_actions:
            return [(prefix, parser)] if prefix else []
        leaves: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []
        for sp_action in subparsers_actions:
            for name, subparser in sp_action.choices.items():
                leaves.extend(_collect_leaf_subparsers(subparser, prefix + (name,)))
        return leaves

    parser = cli.parser()
    leaves = _collect_leaf_subparsers(parser)
    assert len(leaves) >= 80
    for path, subparser in leaves:
        help_text = subparser.format_help()
        assert "usage:" in help_text, f"Missing usage in leaf command {path}"


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
