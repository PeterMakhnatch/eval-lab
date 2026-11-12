from __future__ import annotations

from pathlib import Path

from evallab.traj import outline_trajectory

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
JOBS = FIXTURES / "jobs"
PASS = JOBS / "job-pass" / "t1"


def test_real_atif_outline_renders_phases_and_step_highlights() -> None:
    outline = outline_trajectory(PASS, repo_root=FIXTURES, explicit_runs_root=JOBS)

    assert outline.status == "featured"
    assert outline.phases


