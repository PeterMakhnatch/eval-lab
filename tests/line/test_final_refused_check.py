"""``line check`` on a batch the line refuses before any gate runs still writes a finished result.

A declared input swapped for a link after the run makes ``check_batch`` raise (it refuses to read
through the link). The check used to leave ``check.json`` all ``PENDING``; it now names the refusal
on every gate, is not passed, and leaves the scoreboard and its view exactly as found.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evallab import cli
from evallab.line import core
from evallab.line.io import read_json


def _swap_for_a_link(path: Path) -> None:
    copy = path.with_name(f"copy-{path.name}")
    shutil.copy(path, copy)
    path.unlink()
    path.symlink_to(copy)


@pytest.mark.parametrize("which", ["rollouts", "labels", "task_status"])
def test_a_refused_check_is_a_finished_failing_result(
    fixture_spec: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    which: str,
) -> None:
    root = fixture_spec.parent
    batch = core.run_line(fixture_spec, root)
    spec = read_json(fixture_spec)
    target = Path(spec["sources"][0][which] if which != "task_status" else spec["task_status"])
    _swap_for_a_link(target)
    code = cli.run_cli(["line", "check", str(batch)], workspace=root)
    assert code == 2
    assert "declared input is a symlink" in capsys.readouterr().out
    saved = read_json(batch / "check.json")
    assert saved["passed"] is False
    assert [c["name"] for c in saved["checks"]] == list(core.CHECK_NAMES)
    assert all(
        c["status"] == "FAIL" and "declared input is a symlink" in c["details"]
        for c in saved["checks"]
    )
    # the scoreboard shows the same failing list (its view matched before the check began)
    assert read_json(batch / "scoreboard.json")["quality_checks"] == saved["checks"]
    assert "FAIL" in (batch / "SCOREBOARD.md").read_text()

