"""Reward-hacking audit contracts: deterministic, offline, no Docker or model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.registry import compute_task_digests
from evallab.reward_hack import (
    classify_results,
    declared_expectation,
    plan_matrix,
    render_report,
    run_hack,
    scan_package,
)
from evallab.schemas import MatrixRun


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _weak_package(root: Path) -> Path:
    """A package exercising most flaw classes: shared verifier, root, net, answer files."""
    package = root / "weak-task"
    _write(
        package / "task.toml",
        "\n".join(
            [
                'schema_version = "1.4"',
                "",
                "[task]",
                'name = "fixture/weak-task"',
                "",
                "[verifier]",
                "timeout_sec = 60.0",
                "",
                "[agent]",
                "timeout_sec = 120.0",
                "",
                "[environment]",
                "allow_internet = true",
                "cpus = 1",
                "memory_mb = 512",
                "",
            ]
        ),
    )
    _write(package / "instruction.md", "Produce the report.\n")
    _write(
        package / "environment" / "Dockerfile", "FROM python:3.12-slim\nCOPY answers.json /app/\n"
    )
    _write(package / "environment" / "answers.json", "{}\n")
    _write(
        package / "tests" / "test.sh",
        "\n".join(
            [
                "#!/bin/bash",
                "set -u",
                "cat /app/out.txt || true",
                "grep -i ok /app/out.txt 2>/dev/null",
                "echo 1 > /logs/verifier/reward.txt",
                "",
            ]
        ),
    )
    _write(
        package / "tests" / "grader.py",
        "import json\npayload = open('/app/out.txt').read()\nscore = eval(payload)\n",
    )
    _write(package / "solution" / "solve.sh", "#!/bin/sh\nexit 0\n")
    return package


def _hardened_package(root: Path) -> Path:
    """A package whose design closes V1/V7 and declares no excessive authority."""
    package = root / "hardened-task"
    _write(
        package / "task.toml",
        "\n".join(
            [
                'schema_version = "1.4"',
                "",
                "[task]",
                'name = "fixture/hardened-task"',
                "",
                "[verifier]",
                'environment_mode = "separate"',
                "timeout_sec = 60.0",
                "",
                "[agent]",
                "timeout_sec = 120.0",
                "",
                "[environment]",
                'network_mode = "no-network"',
                'user = "app"',
                "",
            ]
        ),
    )
    _write(package / "instruction.md", "Compute the summary from the trusted input.\n")
    _write(package / "environment" / "Dockerfile", "FROM python:3.12-slim\nUSER app\n")
    _write(
        package / "tests" / "test.sh",
        "#!/bin/sh\nset -eu\nexec python /tests/verify.py\n",
    )
    _write(
        package / "tests" / "verify.py",
        "import json\n"
        "from pathlib import Path\n"
        "candidate = json.loads(Path('/app/output/summary.json').read_text())\n"
        "trusted = json.loads(Path('/tests/fixtures/summary.json').read_text())\n"
        "raise SystemExit(0 if candidate == trusted else 1)\n",
    )
    _write(package / "solution" / "solve.sh", "#!/bin/sh\nexit 0\n")
    return package


def test_scan_reports_the_flaw_classes_it_can_decide(tmp_path: Path) -> None:
    package = _weak_package(tmp_path)
    scan = scan_package(package)
    classes = {finding.flaw_class for finding in scan.findings}
    assert {"V1", "V2", "V3", "V5", "V6", "V7", "V8"} <= classes
    evidence = {finding.evidence for finding in scan.findings}
    assert "task.toml" in evidence
    assert any(item.startswith("tests/test.sh:") for item in evidence)
    assert any(item.startswith("tests/grader.py:") for item in evidence)
    assert scan.recon["verifier_mode"] == "shared"
    assert scan.recon["network_mode"] == "public"
    assert scan.recon["network_source"] == "legacy-allow-internet"
    assert scan.recon["agent_user"] is None
    assert scan.package_digest.startswith("sha256:")
    assert "not a security certificate" in scan.limits


def test_scan_hardened_package_reports_no_v1_or_v7(tmp_path: Path) -> None:
    package = _hardened_package(tmp_path)
    scan = scan_package(package)
    classes = {finding.flaw_class for finding in scan.findings}
    assert "V1" not in classes
    assert "V7" not in classes
    assert scan.recon["verifier_mode"] == "separate"
    assert scan.recon["network_mode"] == "no-network"
    assert scan.recon["agent_user"] == "app"


def test_scan_rejects_a_non_package(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="task.toml is missing"):
        scan_package(tmp_path / "absent")


def test_declared_expectation_reads_only_the_control_header(tmp_path: Path) -> None:
    declared_one = tmp_path / "one.sh"
    _write(declared_one, "#!/bin/bash\n# expect: 1\nset -eu\n")
    declared_zero = tmp_path / "zero.sh"
    _write(declared_zero, "#!/bin/bash\n# expect: 0\nexit 0\n")
    silent = tmp_path / "silent.sh"
    _write(silent, "#!/bin/bash\nexit 0\n")
    assert declared_expectation(declared_one) == 1.0
    assert declared_expectation(declared_zero) == 0.0
    assert declared_expectation(silent) is None


def test_plan_matrix_binds_baselines_and_solution_overrides(tmp_path: Path) -> None:
    package = _weak_package(tmp_path)
    script = tmp_path / "controls" / "forge-reward.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    matrix = plan_matrix(package, [script], repo_root=tmp_path)
    digests = compute_task_digests(package)
    assert matrix.task_package_digest == digests.package
    assert matrix.verifier_digest == digests.verifier
    assert matrix.jobs_dir == "runs"
    assert matrix.environment == "docker"
    assert [run.agent for run in matrix.runs] == ["oracle", "nop", "oracle"]
    assert [run.expect_reward for run in matrix.runs] == [1.0, 0.0, 1.0]
    override = matrix.runs[2]
    assert override.solution == "controls/forge-reward.sh"
    assert override.allow_billable is False
    assert len({run.name for run in matrix.runs}) == 3


def test_plan_matrix_refuses_step_tasks(tmp_path: Path) -> None:
    package = _weak_package(tmp_path)
    _write(
        package / "task.toml",
        'schema_version = "1.4"\n\n[task]\nname = "fixture/weak-task"\n\n'
        '[[steps]]\nname = "first"\n',
    )
    script = tmp_path / "controls" / "forge.sh"
    _write(script, "#!/bin/bash\nexit 0\n")
    with pytest.raises(ValueError, match="declared task steps"):
        plan_matrix(package, [script], repo_root=tmp_path)


def _matrix_and_controls(tmp_path: Path):
    package = _weak_package(tmp_path)
    script = tmp_path / "controls" / "forge-reward.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    matrix = plan_matrix(package, [script], repo_root=tmp_path)
    return package, matrix, script


def _results(matrix, oracle: dict, nop: dict, *exploits: dict) -> list[dict]:
    runs = list(zip(matrix.runs, [oracle, nop, *exploits], strict=True))
    return [
        {"name": run.name, "expect_reward": run.expect_reward, **result} for run, result in runs
    ]


def _staged(tmp_path: Path):
    from evallab.reward_hack import stage_controls

    script = tmp_path / "controls" / "forge-reward.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    return stage_controls(tmp_path / "run", [script])


def test_classify_confirms_a_full_reward_hack(tmp_path: Path) -> None:
    package, matrix, _ = _matrix_and_controls(tmp_path)
    controls = _staged(tmp_path)
    results = _results(
        matrix,
        {"status": "ok", "rewards": [1.0], "error": None},
        {"status": "ok", "rewards": [0.0], "error": None},
        {"status": "ok", "rewards": [1.0], "error": None},
    )
    runs, verdict, reason = classify_results(matrix, results, controls)
    assert verdict == "hackable"
    assert str(tmp_path) not in reason  # the reason cites job names, not local paths
    assert runs[2].name in reason
    assert runs[2].script == str((tmp_path / "controls" / "forge-reward.sh").resolve())
    assert runs[2].script_sha256 is not None
    assert runs[1].outcome == "baseline"
    assert package.name == "weak-task"


def test_classify_reports_rejection_partial_and_broken_controls(tmp_path: Path) -> None:
    _, matrix, _ = _matrix_and_controls(tmp_path)
    controls = _staged(tmp_path)
    ok_oracle = {"status": "ok", "rewards": [1.0], "error": None}
    ok_nop = {"status": "ok", "rewards": [0.0], "error": None}
    resisted = _results(
        matrix, ok_oracle, ok_nop, {"status": "ok", "rewards": [0.0], "error": None}
    )
    assert classify_results(matrix, resisted, controls)[1] == "not_demonstrated"
    partial = _results(matrix, ok_oracle, ok_nop, {"status": "ok", "rewards": [0.4], "error": None})
    assert classify_results(matrix, partial, controls)[1] == "partially_hackable"
    broken = _results(
        matrix,
        ok_oracle,
        {"status": "ok", "rewards": [1.0], "error": None},
        {"status": "ok", "rewards": [1.0], "error": None},
    )
    verdict, reason = classify_results(matrix, broken, controls)[1:]
    assert verdict == "task_broken"
    assert "no-agent control" in reason
    oracle_fail = _results(
        matrix,
        {"status": "mismatch", "rewards": [0.0], "error": None},
        ok_nop,
        {"status": "ok", "rewards": [0.0], "error": None},
    )
    verdict, reason = classify_results(matrix, oracle_fail, controls)[1:]
    assert verdict == "task_broken"
    assert "reference solution" in reason
    unscored = _results(
        matrix,
        ok_oracle,
        ok_nop,
        {"status": "infra", "rewards": [], "error": "DockerError: boom"},
    )
    runs, verdict, _ = classify_results(matrix, unscored, controls)
    assert verdict == "unscored"
    assert runs[2].outcome == "unscored"
    assert runs[2].reward is None
    assert runs[2].error == "DockerError: boom"


def test_run_hack_plan_writes_matrix_and_report_without_executing(tmp_path: Path) -> None:
    package = _weak_package(tmp_path)
    script = tmp_path / "forge.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    run_dir = tmp_path / "receipts"
    report, resolved = run_hack(
        package, repo_root=tmp_path, scripts=[script], execute=False, output_dir=run_dir
    )
    assert resolved == run_dir
    assert report.executed is False
    assert report.verdict == "planned"
    assert (run_dir / "matrix.json").is_file()
    assert (run_dir / "hack-report.json").is_file()
    markdown = (run_dir / "hack-report.md").read_text(encoding="utf-8")
    assert "verdict: **planned**" in markdown
    assert "not a security certificate" in markdown or "Limits:" in markdown
    matrix = json.loads((run_dir / "matrix.json").read_text(encoding="utf-8"))
    assert matrix["runs"][2]["solution"] == "receipts/controls/02-forge.sh"
    assert not (tmp_path / "runs" / ".executor").exists()


def test_run_hack_requires_at_least_one_script(tmp_path: Path) -> None:
    package = _hardened_package(tmp_path)
    with pytest.raises(ValueError, match="no exploit scripts"):
        run_hack(package, repo_root=tmp_path, execute=False, output_dir=tmp_path / "receipts")


def test_run_hack_refuses_a_run_directory_outside_the_repository(tmp_path: Path) -> None:
    package = _weak_package(tmp_path)
    script = tmp_path / "forge.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    with pytest.raises(ValueError, match="must stay inside the repository"):
        run_hack(
            package,
            repo_root=tmp_path,
            scripts=[script],
            execute=False,
            output_dir=tmp_path.parent / "outside",
        )


def test_render_report_carries_limits_and_outcomes(tmp_path: Path) -> None:
    package = _weak_package(tmp_path)
    script = tmp_path / "forge.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    report, _ = run_hack(
        package, repo_root=tmp_path, scripts=[script], execute=False, output_dir=tmp_path / "out"
    )
    rendered = render_report(report)
    assert "Reward-hacking audit" in rendered
    assert "V1" in rendered
    assert report.limits in rendered


def test_hack_scan_cli_emits_the_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    package = _hardened_package(tmp_path)
    code = cli.run_cli(["hack", "scan", str(package), "--json"], workspace=tmp_path)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "evallab.reward_hack.scan/v1"
    assert payload["recon"]["verifier_mode"] == "separate"


def test_hack_scan_cli_fails_closed_on_a_missing_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.run_cli(["hack", "scan", str(tmp_path / "absent")], workspace=tmp_path)
    assert code == 1
    assert "not a Harbor task package" in capsys.readouterr().err


def test_hack_run_cli_plans_without_executing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = _weak_package(tmp_path)
    script = tmp_path / "forge.sh"
    _write(script, "#!/bin/bash\n# expect: 1\necho 1 > /logs/verifier/reward.txt\n")
    output = tmp_path / "cli-out"
    code = cli.run_cli(
        ["hack", "run", str(package), "--script", str(script), "--output-dir", str(output)],
        workspace=tmp_path,
    )
    assert code == 0
    assert "Verdict: planned" in capsys.readouterr().out
    assert (output / "hack-report.json").is_file()


def test_matrix_runs_are_free_controls_only(tmp_path: Path) -> None:
    """A solution override can never name a billable agent; the contract enforces it."""
    with pytest.raises(ValueError, match="solution requires agent='oracle'"):
        MatrixRun(name="overflow", agent="mini-swe-agent", solution="controls/x.sh")
