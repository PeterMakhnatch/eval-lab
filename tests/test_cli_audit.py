from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from evallab import cli


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
