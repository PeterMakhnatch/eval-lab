from __future__ import annotations

from pathlib import Path

from evallab.traj import outline_trajectory, render_outline

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
JOBS = FIXTURES / "jobs"
PASS = JOBS / "job-pass" / "t1"


def test_real_atif_outline_renders_phases_and_step_highlights() -> None:
    outline = outline_trajectory(PASS, repo_root=FIXTURES, explicit_runs_root=JOBS)
    rendered = render_outline(outline)

    assert outline.status == "featured"
    assert outline.phases
    assert "ORDERED PHASES:" in rendered
    assert "STEP HIGHLIGHTS:" in rendered
    assert "run_pytest" in rendered


