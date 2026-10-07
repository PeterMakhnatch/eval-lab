"""The task CLI keeps absent evidence unknown and remains read-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.task_dossier import task_dossier


@pytest.mark.parametrize("task_id", ["format-code-task-999999", "never-recorded-task"])
def test_missing_task_json_keeps_sources_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, task_id: str,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    derived = tmp_path / "derived"
    readers = tmp_path / "readers"
    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(tmp_path / "results"))
    monkeypatch.setenv("EVALLAB_READERS_STORE", str(readers))
    monkeypatch.setenv("EVALLAB_RUNS_ROOT", str(runs))
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    code = run_cli(
        ["task", task_id, "--json", "--runs-dir", str(runs),
         "--derived-root", str(derived), "--reader-store", str(readers),
         "--static-audit", str(tmp_path / "absent-static.csv")],
        workspace=tmp_path,
    )

    assert code == 0
    dossier = json.loads(capsys.readouterr().out)
    assert dossier["trials"] == []
    for field in (
        "health", "verdict", "tags", "leak", "repair", "static_flags",
        "failing_tests", "exploit_probes",
    ):
        assert dossier[field] is None, field
    assert dossier["coverage"]["read_only"] is True
    assert not derived.exists()
    assert not readers.exists()
    assert list(runs.iterdir()) == []


@pytest.mark.parametrize("selector", ["../outside", "task/*", "task\nsecond-command"])
def test_dossier_rejects_paths_and_patterns(tmp_path: Path, selector: str) -> None:
    with pytest.raises(ValueError):
        task_dossier(selector, repo_root=tmp_path, roots=[], static_audit=None)
