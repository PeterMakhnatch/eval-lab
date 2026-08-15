import shutil
from datetime import date
from pathlib import Path

from evallab.smoke import run_smoke


def test_docker_free_smoke_proves_composed_path(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    root = tmp_path / "repo"
    fixture_destination = root / "research/evidence/runs/event-summary-oracle-evidence"
    fixture_destination.parent.mkdir(parents=True)

    shutil.copytree(
        source_root / "research/evidence/runs/event-summary-oracle-evidence",
        fixture_destination,
    )
    task = root / "library/tasks/event-summary/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text("version = '1.0'\n")
    policy = root / "policy/standing-approvals.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text((source_root / "policy/standing-approvals.yaml").read_text())
    analysis_dir = root / "research/analysis"
    analysis_dir.mkdir(parents=True)
    for name in ("stage5-prompt.md", "stage5-rubric.json", "stub-oracle-analysis.json"):
        shutil.copy2(source_root / "research/analysis" / name, analysis_dir / name)

    result = run_smoke(
        root,
        docker_free=True,
        run_token="test",
        report_date=date(2026, 8, 14),
    )

    assert result.mode == "docker-free"
    assert result.trial_count == 1
    assert result.invariant.ok
    assert result.job_id in result.invariant.catalog_job_ids
    assert result.job_id in result.invariant.projected_job_ids
    assert result.job_name in result.digest_text
    assert result.analysis_path is not None
    assert result.analysis_path.is_file()
    assert result.analysis_validation == "valid"
    assert result.status is not None
    assert tuple(result.status.section_map()) == (
        "Recent",
        "Now",
        "Next",
        "Tasks",
        "Health",
        "Analysis",
    )
    assert result.status.Analysis.availability != "unavailable"
    analysis = next(item for item in result.status.Analysis.items if item.kind == "analysis")
    assert analysis.provenance is not None
    assert analysis.job_id == result.job_id
