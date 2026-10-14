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


def _write_packaged_manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "campaign_id": "pilot",
                "benchmark": "terminal-bench-4",
                "pin": "452bf305",
                "tasks": [
                    {"task": "alpha", "package": "pkgs/alpha"},
                    "pkgs/beta",
                    "gamma",
                ],
                "packages": {"gamma": "pkgs/gamma"},
                "models": {"red": "glm-5.3"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_execute_passes_resolved_packages_to_wave_and_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute wiring: wave gets packages; red grades/ctx bind to the package."""
    from types import SimpleNamespace

    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli
    import evallab.vcheck_red as vcheck_red

    for name in ("alpha", "beta", "gamma"):
        (tmp_path / "pkgs" / name).mkdir(parents=True)
    manifest = _write_packaged_manifest(tmp_path / "manifest.json")
    expected = {
        "alpha": str((tmp_path / "pkgs" / "alpha").resolve()),
        "pkgs/beta": str((tmp_path / "pkgs" / "beta").resolve()),
        "gamma": str((tmp_path / "pkgs" / "gamma").resolve()),
    }

    grade_calls: list[tuple[str, object]] = []

    def fake_grade_fn(package: object, submission: object) -> dict[str, int]:
        grade_calls.append((str(package), submission))
        return {"reward": 0}

    monkeypatch.setattr(vcheck_cli, "_build_grade_fn", lambda root, run_dir: fake_grade_fn)

    red_contexts: dict[str, dict[str, object]] = {}

    def fake_run_red(
        task_ctx: object,
        *,
        client: object,
        budget: object,
        broadcast_hints: object,
        grade_fn: object,
        probe_fn: object,
        run_dir: object,
        model: object,
        **kwargs: object,
    ) -> list[object]:
        assert isinstance(task_ctx, dict)
        ctx = dict(task_ctx)
        task_id = str(ctx["task"])
        red_contexts[task_id] = ctx
        assert callable(grade_fn)
        grade_fn({"answer": "wrong"})  # type: ignore[operator]
        return []

    monkeypatch.setattr(vcheck_red, "run_red", fake_run_red)

    wave_seen: dict[str, object] = {}

    def fake_run_wave(
        manifest: object,
        tasks: object,
        red_factory: object,
        broadcast: object = (),
        grade_fn: object = None,
        packages: object = None,
        **kwargs: object,
    ) -> object:
        assert isinstance(tasks, list)
        assert isinstance(packages, dict)
        wave_seen["tasks"] = list(tasks)
        wave_seen["packages"] = dict(packages)
        wave_seen["grade_fn"] = grade_fn
        assert callable(red_factory)
        for task_id in tasks:
            red_factory(task_id, broadcast)  # type: ignore[operator]
        return SimpleNamespace(hypotheses=[], broadcast=[], skipped_task_broken=[])

    monkeypatch.setattr(vcheck_core, "run_wave", fake_run_wave)

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
            "--execute",
            "--json",
        ],
        workspace=tmp_path,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "executed"

    assert wave_seen["packages"] == expected
    assert wave_seen["grade_fn"] is fake_grade_fn
    assert set(red_contexts) == set(expected)
    for task_id, package in expected.items():
        ctx = red_contexts[task_id]
        assert ctx["package"] == package
        assert ctx["task_dir"] == package
    assert sorted(package for package, _ in grade_calls) == sorted(expected.values())

    plan = json.loads(
        (Path(payload["run_dir"]) / "vcheck-plan.json").read_text(encoding="utf-8")
    )
    assert plan["packages"] == expected


def test_execute_missing_package_fails_before_wave(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ID-only manifests fail --execute before any wave/grade construction."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli

    manifest = _write_manifest(tmp_path / "manifest.json", tasks=["task-a"])
    called = {"wave": False, "grade": False}

    def fake_run_wave(*args: object, **kwargs: object) -> object:
        called["wave"] = True
        raise AssertionError("run_wave must not run without packages")

    def fake_build_grade_fn(*args: object, **kwargs: object) -> object:
        called["grade"] = True
        raise AssertionError("grade_fn must not be built without packages")

    monkeypatch.setattr(vcheck_core, "run_wave", fake_run_wave)
    monkeypatch.setattr(vcheck_cli, "_build_grade_fn", fake_build_grade_fn)

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
            "--execute",
        ],
        workspace=tmp_path,
    )
    assert code == 1
    assert called == {"wave": False, "grade": False}
    assert "package" in capsys.readouterr().err.lower()
    run_dirs = list((tmp_path / "runs" / ".vcheck").iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "vcheck-report.json").exists()


def test_execute_missing_package_directory_fails_before_wave(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound-but-absent package directories fail --execute before the wave."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli

    manifest = _write_manifest(tmp_path / "manifest.json", tasks=["task-a"])
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_data["tasks"] = [{"task": "task-a", "package": "pkgs/absent"}]
    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

    def fake_run_wave(*args: object, **kwargs: object) -> object:
        raise AssertionError("run_wave must not run with a missing package dir")

    def fake_build_grade_fn(*args: object, **kwargs: object) -> object:
        raise AssertionError("grade_fn must not be built with a missing package dir")

    monkeypatch.setattr(vcheck_core, "run_wave", fake_run_wave)
    monkeypatch.setattr(vcheck_cli, "_build_grade_fn", fake_build_grade_fn)

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
            "--execute",
        ],
        workspace=tmp_path,
    )
    assert code == 1
    assert "package directory missing" in capsys.readouterr().err


def test_plan_records_resolved_packages_as_strings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Plan-only runs record the resolved mapping without requiring execution."""
    (tmp_path / "pkgs" / "alpha").mkdir(parents=True)
    manifest = _write_packaged_manifest(tmp_path / "manifest.json")
    (tmp_path / "pkgs" / "beta").mkdir(parents=True)
    (tmp_path / "pkgs" / "gamma").mkdir(parents=True)
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
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "plan"
    plan = json.loads(
        (Path(payload["run_dir"]) / "vcheck-plan.json").read_text(encoding="utf-8")
    )
    assert plan["packages"] == {
        "alpha": str((tmp_path / "pkgs" / "alpha").resolve()),
        "pkgs/beta": str((tmp_path / "pkgs" / "beta").resolve()),
        "gamma": str((tmp_path / "pkgs" / "gamma").resolve()),
    }
    assert all(isinstance(value, str) for value in plan["packages"].values())


def test_run_rejects_duplicate_or_conflicting_package_bindings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Duplicate ids and conflicting bindings fail closed on the plan path."""
    manifest = _write_manifest(tmp_path / "manifest.json", tasks=["task-a", "task-a"])
    assert (
        cli.run_cli(
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
        == 1
    )
    assert "duplicate" in capsys.readouterr().err.lower()

    (tmp_path / "pkgs" / "alpha").mkdir(parents=True)
    (tmp_path / "pkgs" / "other").mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "campaign_id": "pilot",
                "benchmark": "terminal-bench-4",
                "pin": "452bf305",
                "tasks": [{"task": "alpha", "package": "pkgs/alpha"}],
                "packages": {"alpha": "pkgs/other"},
                "models": {"red": "glm-5.3"},
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli.run_cli(
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
        == 1
    )
    assert "conflicting" in capsys.readouterr().err.lower()


def _ok_control(task: str) -> object:
    from types import SimpleNamespace

    def grade(reward: float, status: str = "graded") -> object:
        return SimpleNamespace(
            reward=reward, status=status, job_dir=f"/jobs/{task}", script="grade.sh"
        )

    return SimpleNamespace(
        task=task,
        baseline=grade(1),
        legit=grade(1),
        negative=grade(0),
        verdict="ok",
        reason="oracle=1, empty=0; legit=1",
    )


def _broken_control(task: str) -> object:
    from types import SimpleNamespace

    def grade(reward: float, status: str = "graded") -> object:
        return SimpleNamespace(
            reward=reward, status=status, job_dir=f"/jobs/{task}", script="grade.sh"
        )

    return SimpleNamespace(
        task=task,
        baseline=grade(0),
        legit=grade(0),
        negative=grade(0),
        verdict="task_broken",
        reason="oracle earned 0, expected 1; task or verifier is broken",
    )


def test_controls_only_needs_no_budget_or_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controls-only runs without --budget-usd/--approve and writes a report."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli

    (tmp_path / "pkgs" / "alpha").mkdir(parents=True)
    manifest = _write_packaged_manifest(tmp_path / "manifest.json")
    (tmp_path / "pkgs" / "beta").mkdir(parents=True)
    (tmp_path / "pkgs" / "gamma").mkdir(parents=True)

    grade_built = {"calls": 0}

    def fake_grade_fn(package: object, submission: object) -> dict[str, int]:
        return {"reward": 0}

    def fake_build_grade_fn(root: object, run_dir: object) -> object:
        grade_built["calls"] += 1
        return fake_grade_fn

    controls_calls: list[tuple[str, str]] = []

    def fake_run_controls(
        package: object, grade_fn: object, task: object = None
    ) -> object:
        controls_calls.append((str(task), str(package)))
        assert grade_fn is fake_grade_fn
        return _ok_control(str(task))

    monkeypatch.setattr(vcheck_cli, "_build_grade_fn", fake_build_grade_fn)
    monkeypatch.setattr(vcheck_core, "run_controls", fake_run_controls)

    code = cli.run_cli(
        ["vcheck", "run", "--manifest", str(manifest), "--controls-only", "--json"],
        workspace=tmp_path,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)  # exactly one JSON document
    assert payload["verdict"] == "controls"
    assert payload["counts"] == {"tasks": 3, "controls_ok": 3, "task_broken": 0}
    assert payload["task_broken"] == []
    assert len(payload["controls"]) == 3
    row = next(r for r in payload["controls"] if r["task"] == "alpha")
    assert row["verdict"] == "ok"
    assert row["baseline"]["reward"] == 1 and row["negative"]["reward"] == 0
    assert row["baseline"]["job_dir"] == "/jobs/alpha"
    assert row["baseline"]["script"] == "grade.sh"

    expected = {
        "alpha": str((tmp_path / "pkgs" / "alpha").resolve()),
        "pkgs/beta": str((tmp_path / "pkgs" / "beta").resolve()),
        "gamma": str((tmp_path / "pkgs" / "gamma").resolve()),
    }
    assert sorted(controls_calls) == sorted(
        (task, package) for task, package in expected.items()
    )
    assert grade_built["calls"] == 1

    report_path = Path(payload["run_dir"]) / "vcheck-report.json"
    assert report_path.is_file()
    plan = json.loads(
        (Path(payload["run_dir"]) / "vcheck-plan.json").read_text(encoding="utf-8")
    )
    assert plan["controls_only"] is True
    assert plan["execute"] is False


def test_controls_only_task_broken_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task_broken control fails the gate but still writes the report."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli

    (tmp_path / "pkgs" / "alpha").mkdir(parents=True)
    (tmp_path / "pkgs" / "beta").mkdir(parents=True)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "campaign_id": "pilot",
                "benchmark": "terminal-bench-4",
                "pin": "452bf305",
                "tasks": [
                    {"task": "alpha", "package": "pkgs/alpha"},
                    {"task": "beta", "package": "pkgs/beta"},
                ],
                "models": {"red": "glm-5.3"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        vcheck_cli, "_build_grade_fn", lambda root, run_dir: (lambda p, s: None)
    )

    def fake_run_controls(
        package: object, grade_fn: object, task: object = None
    ) -> object:
        if str(task) == "beta":
            return _broken_control("beta")
        return _ok_control(str(task))

    monkeypatch.setattr(vcheck_core, "run_controls", fake_run_controls)

    code = cli.run_cli(
        ["vcheck", "run", "--manifest", str(manifest_path), "--controls-only"],
        workspace=tmp_path,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "task-broken" in out and "beta" in out
    run_dirs = list((tmp_path / "runs" / ".vcheck").iterdir())
    assert len(run_dirs) == 1
    report = json.loads(
        (run_dirs[0] / "vcheck-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "controls"
    assert report["counts"] == {"tasks": 2, "controls_ok": 1, "task_broken": 1}
    assert report["task_broken"] == ["beta"]


def test_controls_only_rejects_execute_combo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--controls-only with --execute fails before any grading or wave."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli

    manifest = _write_manifest(tmp_path / "manifest.json", tasks=["task-a"])

    def fake_run_wave(*args: object, **kwargs: object) -> object:
        raise AssertionError("run_wave must not run for --controls-only --execute")

    def fake_build_grade_fn(*args: object, **kwargs: object) -> object:
        raise AssertionError("grade_fn must not be built for --controls-only --execute")

    monkeypatch.setattr(vcheck_core, "run_wave", fake_run_wave)
    monkeypatch.setattr(vcheck_cli, "_build_grade_fn", fake_build_grade_fn)

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
            "--execute",
            "--controls-only",
        ],
        workspace=tmp_path,
    )
    assert code == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_execute_wave_typeerror_fails_without_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TypeError inside the wave fails once; the wave is never re-run."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli
    import evallab.vcheck_red as vcheck_red

    (tmp_path / "pkgs" / "alpha").mkdir(parents=True)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "campaign_id": "pilot",
                "benchmark": "terminal-bench-4",
                "pin": "452bf305",
                "tasks": [{"task": "alpha", "package": "pkgs/alpha"}],
                "models": {"red": "glm-5.3"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        vcheck_cli, "_build_grade_fn", lambda root, run_dir: (lambda p, s: None)
    )
    monkeypatch.setattr(
        vcheck_cli,
        "_build_red_factory",
        lambda **kwargs: (lambda task_id, hints=(): []),
    )

    calls = {"wave": 0}

    def fake_run_wave(*args: object, **kwargs: object) -> object:
        calls["wave"] += 1
        assert kwargs.get("packages") == {
            "alpha": str((tmp_path / "pkgs" / "alpha").resolve())
        }
        raise TypeError("boom deep inside the wave")

    monkeypatch.setattr(vcheck_core, "run_wave", fake_run_wave)
    monkeypatch.setattr(
        vcheck_red, "run_red", lambda *args, **kwargs: []
    )

    code = cli.run_cli(
        [
            "vcheck",
            "run",
            "--manifest",
            str(manifest_path),
            "--budget-usd",
            "5",
            "--approve",
            "pilot approval",
            "--execute",
        ],
        workspace=tmp_path,
    )
    assert code == 1
    assert calls["wave"] == 1
    assert "wave failed" in capsys.readouterr().err
    run_dirs = list((tmp_path / "runs" / ".vcheck").iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "vcheck-report.json").exists()


def test_build_grade_fn_redirects_runner_stdout_and_separates_revisions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grading progress goes to stderr, and two revisions of the same slug get distinct run names."""
    import evallab.vcheck as vcheck_core
    import evallab.vcheck_cli as vcheck_cli
    import evallab.verifier_mutation as vm

    for rev in ("v1", "v2"):
        pkg = tmp_path / rev / "fix-git"
        pkg.mkdir(parents=True)
        (pkg / "task.toml").write_text('[task]\nname = "terminal-bench/fix-git"\n', encoding="utf-8")
        (pkg / "instruction.md").write_text(f"Fix git {rev}\n", encoding="utf-8")
        (pkg / "solution").mkdir()
        (pkg / "solution" / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (pkg / "tests").mkdir()
        (pkg / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    seen_names: list[str] = []

    def fake_runner(matrix: object, repo: Path) -> list[dict[str, object]]:
        run_name = matrix.runs[0].name  # type: ignore[attr-defined]
        seen_names.append(run_name)
        print(f"{run_name}: ok (expected 0.0, got [1.0])")
        return [{"name": run_name, "status": "ok", "rewards": [1.0]}]

    monkeypatch.setattr(vm, "_default_runner", fake_runner)
    run_dir = tmp_path / "runs" / ".vcheck" / "rev-test"
    grade_fn = vcheck_cli._build_grade_fn(tmp_path, run_dir)
    assert grade_fn is not None

    r1 = grade_fn(tmp_path / "v1" / "fix-git", vcheck_core.oracle_submission())
    r2 = grade_fn(tmp_path / "v2" / "fix-git", vcheck_core.oracle_submission())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vc-" in captured.err
    assert r1.reward == 1.0 and r2.reward == 1.0
    assert len(seen_names) == 2 and seen_names[0] != seen_names[1]
