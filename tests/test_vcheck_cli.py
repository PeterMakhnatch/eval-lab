"""VerifierCheck CLI contracts: offline, no Docker, no model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.vcheck_cli import estimate_task_usd


def _write_manifest(path: Path, tasks: list[str] | None = None) -> Path:
    manifest = {
        "campaign_id": "pilot",
        "benchmark": "terminal-bench-4",
        "pin": "452bf305",
        "tasks": tasks if tasks is not None else ["cargo-flight-dispatch"],
        "models": {"red": "glm-5.3"},
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_run_refuses_without_budget_and_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json")
    code = cli.run_cli(["vcheck", "run", "--manifest", str(manifest)], workspace=tmp_path)
    assert code == 1
    err = capsys.readouterr().err
    assert "--budget-usd" in err and "--approve" in err


def test_run_refuses_with_only_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json")
    code = cli.run_cli(
        ["vcheck", "run", "--manifest", str(manifest), "--budget-usd", "5"],
        workspace=tmp_path,
    )
    assert code == 1
    assert "--approve" in capsys.readouterr().err


def test_run_plan_json_is_a_single_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json", tasks=["task-a", "task-b"])
    code = cli.run_cli(
        [
            "vcheck",
            "run",
            "--manifest",
            str(manifest),
            "--budget-usd",
            "5",
            "--approve",
            "pilot approval",
            "--json",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # raises if stdout is not exactly one JSON doc
    assert payload["verdict"] == "plan"
    assert payload["counts"] == {"tasks": 2, "hypotheses": 0, "survivors": 0}
    assert payload["survivors"] == [] and payload["hypotheses"] == []
    plan_path = Path(payload["run_dir"]) / "vcheck-plan.json"
    assert plan_path.is_file()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["approval"] == "pilot approval"  # approval text recorded in manifest output
    assert plan["manifest"]["campaign_id"] == "pilot"
    assert not (Path(payload["run_dir"]) / "vcheck-report.json").exists()


def test_run_plan_human_mode_writes_plan_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json")
    code = cli.run_cli(
        [
            "vcheck",
            "run",
            "--manifest",
            str(manifest),
            "--budget-usd",
            "5",
            "--approve",
            "pilot approval",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Plan only" in out
    run_dirs = list((tmp_path / "runs" / ".vcheck").iterdir())
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "vcheck-plan.json").is_file()
    assert not (run_dirs[0] / "vcheck-report.json").exists()


def test_run_rejects_an_estimate_over_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json")
    per_task = estimate_task_usd("glm-5.3")
    assert per_task > 0
    code = cli.run_cli(
        [
            "vcheck",
            "run",
            "--manifest",
            str(manifest),
            "--budget-usd",
            "0.000001",
            "--approve",
            "tiny",
        ],
        workspace=tmp_path,
    )
    assert code == 1
    assert "exceeds --budget-usd" in capsys.readouterr().err


def _write_report_with_hypothesis(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "vcheck-plan.json").write_text(
        json.dumps({"schema": "evallab.vcheck.plan/v1", "campaign_id": "pilot", "tasks": ["t"]}),
        encoding="utf-8",
    )
    (run_dir / "vcheck-report.json").write_text(
        json.dumps(
            {
                "schema": "evallab.vcheck.report/v1",
                "campaign_id": "pilot",
                "verdict": "executed",
                "counts": {"tasks": 1, "hypotheses": 1, "survivors": 1},
                "survivors": [],
                "hypotheses": [
                    {
                        "id": "H-001",
                        "task": "t",
                        "requirement_ids": ["R-1"],
                        "statement": "verifier ignores the flag",
                        "status": "candidate",
                        "evidence": [],
                        "history": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_confirm_rejects_unknown_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "runs" / ".vcheck" / "pilot-x"
    _write_report_with_hypothesis(run_dir)
    code = cli.run_cli(
        ["vcheck", "confirm", "H-999", "--verdict", "confirm", "--run-dir", str(run_dir)],
        workspace=tmp_path,
    )
    assert code == 1
    assert "unknown hypothesis 'H-999'" in capsys.readouterr().err


def test_confirm_roundtrip_updates_status_and_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "runs" / ".vcheck" / "pilot-x"
    _write_report_with_hypothesis(run_dir)
    code = cli.run_cli(
        ["vcheck", "confirm", "H-001", "--verdict", "confirm", "--run-dir", str(run_dir)],
        workspace=tmp_path,
    )
    assert code == 0
    assert "candidate -> promoted" in capsys.readouterr().out
    report = json.loads((run_dir / "vcheck-report.json").read_text(encoding="utf-8"))
    hypothesis = report["hypotheses"][0]
    assert hypothesis["status"] == "promoted"
    assert report["hypotheses"][0]["history"]
    code = cli.run_cli(
        [
            "vcheck",
            "confirm",
            "H-001",
            "--verdict",
            "reject",
            "--run-dir",
            str(run_dir),
            "--json",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["to"] == "rejected"


def test_status_reports_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "runs" / ".vcheck" / "pilot-x"
    _write_report_with_hypothesis(run_dir)
    code = cli.run_cli(["vcheck", "status", str(run_dir)], workspace=tmp_path)
    assert code == 0
    assert "pilot" in capsys.readouterr().out
    code = cli.run_cli(["vcheck", "status", str(tmp_path / "absent")], workspace=tmp_path)
    assert code == 1


def test_export_unknown_finding_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "runs" / ".vcheck" / "pilot-x"
    _write_report_with_hypothesis(run_dir)
    code = cli.run_cli(["vcheck", "export", "F-999", "--run-dir", str(run_dir)], workspace=tmp_path)
    assert code == 1
    assert "unknown finding" in capsys.readouterr().err


def test_export_writes_finding_bundle(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / ".vcheck" / "pilot-x"
    _write_report_with_hypothesis(run_dir)
    out = tmp_path / "exports"
    code = cli.run_cli(
        ["vcheck", "export", "H-001", "--run-dir", str(run_dir), "--output-dir", str(out)],
        workspace=tmp_path,
    )
    assert code == 0
    assert (out / "FINDING.md").is_file()
    assert (out / "finding.json").is_file()
    assert (out / "REPRODUCE.md").is_file()
