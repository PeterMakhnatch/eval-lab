"""Tests for the ``evallab view`` command path and the Harbor probes (HAR-361).

Every test uses ``tmp_path`` job dirs only. The viewer launch, the Harbor
version probe and the distribution metadata lookup are faked, so no viewer
process, Docker container or network call starts.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import harbor_view
from evallab.harbor_telemetry import HARBOR_TELEMETRY_ENV, HARBOR_TELEMETRY_OFF
from evallab.harbor_view import (
    _view_command,
    harbor_runtime_problem,
    installed_harbor_version,
    pinned_harbor_bin_dir,
    require_harbor_runtime,
)

# ---------------------------------------------------------------------------
# Builders (same job-dir shape as tests/test_harbor_view.py)
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _trial_result(name: str, *, rewards: dict | None) -> dict:
    return {
        "task_name": "pkg/task",
        "trial_name": name,
        "trial_uri": f"file:///jobs/job/{name}",
        "task_id": {"path": "/tmp/task"},
        "task_checksum": "abc",
        "config": {
            "task": {"path": "/tmp/task"},
            "trial_name": name,
            "trials_dir": "/tmp/trials",
            "agent": {"name": "agent", "model_name": "provider/model"},
            "environment": {"import_path": "env"},
            "job_id": "job-1",
        },
        "agent_info": {
            "name": "agent",
            "model_info": {"name": "model", "provider": "provider"},
        },
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 0,
            "n_output_tokens": 10,
            "cost_usd": None,
        },
        "verifier_result": None if rewards is None else {"rewards": rewards},
        "exception_info": None,
        "started_at": "2026-10-01T00:00:00Z",
        "finished_at": "2026-10-01T00:01:00Z",
    }


def _make_job(root: Path, name: str, trial_rewards: dict[str, dict | None]) -> Path:
    job = root / name
    _write_json(job / "config.json", {"job": name})
    _write_json(job / "result.json", {"job": name})
    for trial_name, rewards in trial_rewards.items():
        trial = job / trial_name
        _write_json(trial / "config.json", {"trial_name": trial_name})
        _write_json(trial / "result.json", _trial_result(trial_name, rewards=rewards))
        (trial / "agent").mkdir(exist_ok=True)
    return job


def _args(paths: list[Path], **overrides: object) -> argparse.Namespace:
    """``evallab view`` namespace with the parser's defaults, plus overrides."""
    values: dict[str, object] = {
        "paths": list(paths),
        "out": None,
        "port": 8080,
        "host": "127.0.0.1",
        "merge": [],
        "arm_regex": None,
        "task_health": None,
        "tag": [],
        "task_variants": [],
        "no_launch": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class _FakeRun:
    """``subprocess.run`` stand-in recording every call."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        error: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> SimpleNamespace:
        self.calls.append((cmd, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, fake: _FakeRun) -> None:
    monkeypatch.setattr(
        harbor_view,
        "subprocess",
        SimpleNamespace(run=fake, TimeoutExpired=subprocess.TimeoutExpired),
    )


def _two_trial_job(tmp_path: Path, name: str = "job-a") -> Path:
    return _make_job(
        tmp_path / "jobs", name, {"trial-1": {"reward": 1.0}, "trial-2": {"reward": 0.0}}
    )


# ---------------------------------------------------------------------------
# Argument and build refusals
# ---------------------------------------------------------------------------


def test_view_command_refuses_without_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _view_command(_args([]), tmp_path)

    assert code == 2
    assert "need at least one job dir or results root" in capsys.readouterr().err


def test_view_command_refuses_when_no_job_dirs_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty-root"
    (empty / "stray").mkdir(parents=True)

    code = _view_command(_args([empty]), tmp_path)

    assert code == 2
    err = capsys.readouterr().err
    assert "no job dirs found" in err
    assert f"skip: {empty / 'stray'}: not a job dir" in err
    assert f"skip: {empty}: no job dirs inside" in err


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"merge": ["no-equals-sign"]}, "--merge needs NAME=GLOB"),
        ({"arm_regex": "("}, "bad --arm-regex"),
        ({"arm_regex": "stock"}, "must define a named (?P<arm>...) group"),
    ],
)
def test_view_command_refuses_bad_option_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, object],
    message: str,
) -> None:
    job = _two_trial_job(tmp_path)

    code = _view_command(_args([job], no_launch=True, **overrides), tmp_path)

    assert code == 2
    assert message in capsys.readouterr().err


def test_view_command_refuses_non_empty_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _two_trial_job(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "already-here").write_text("x", encoding="utf-8")

    code = _view_command(_args([job], out=out, no_launch=True), tmp_path)

    assert code == 2
    assert "viewer root exists and is not empty" in capsys.readouterr().err


def test_view_command_refuses_health_manifest_without_tags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _two_trial_job(tmp_path)

    code = _view_command(_args([job], task_health=Path("health.json"), no_launch=True), tmp_path)

    assert code == 2
    assert "--task-health and at least one --tag" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def test_view_command_prints_totals_and_skip_notes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _two_trial_job(tmp_path)
    missing = tmp_path / "missing-root"
    fake = _FakeRun(error=AssertionError("no launch expected under --no-launch"))
    _patch_subprocess(monkeypatch, fake)

    code = _view_command(_args([job, missing], no_launch=True), tmp_path)

    captured = capsys.readouterr()
    assert code == 0
    assert "viewer root: " in captured.out
    assert "jobs=1 trials=2 scored=2 (native=0 overlay=2) unscored=0 integrity_0=0" in (
        captured.out
    )
    assert f"skip: {missing}: not a job dir or directory" in captured.out
    assert fake.calls == []


def _tagged_jobs(tmp_path: Path) -> tuple[Path, Path]:
    """Two jobs whose trials carry health/solve tags, plus their manifest."""
    from evallab.task_health_tags import SCHEMA

    src = tmp_path / "src"
    jobs = [
        _make_job(
            src,
            "job-mixed",
            {
                "keep": {"reward": 1, "integrity": 1, "reward_gated": 1},
                "leak": {"reward": 0, "integrity": 1, "reward_gated": 0},
            },
        ),
        _make_job(src, "job-excluded", {"unsolved": None}),
    ]
    rows = []
    cases = (
        (jobs[0], "keep", "000084", "a", "health:sound", "solve:always"),
        (jobs[0], "leak", "001269", "b", "health:leak-found", "solve:0-of-n"),
        (jobs[1], "unsolved", "000090", "c", "health:sound", "solve:0-of-n"),
    )
    for job, name, suffix, digit, health, solve in cases:
        task_id = f"format-code-task-{suffix}"
        task = {"path": f"/tasks/{task_id}"}
        agent = {"name": "agent", "model_name": f"provider/{name}-model"}
        config = {"task": task, "agent": agent, "trial_name": name}
        _write_json(job / name / "config.json", config)
        _write_json(
            job / name / "lock.json",
            {"task": {**task, "name": task_id, "digest": "sha256:" + digit * 64}},
        )
        result_path = job / name / "result.json"
        result = json.loads(result_path.read_text())
        result.update(task_name=task_id, task_id=task, config=config)
        _write_json(result_path, result)
        rows.append(
            {
                "task_id": task_id,
                "parent_digest": "sha256:" + digit * 63 + "1",
                "parent_harbor_digest": "sha256:" + digit * 64,
                "variant_digest": "sha256:" + digit * 63 + "2",
                "variant_harbor_digest": "sha256:" + digit * 63 + "3",
                "tags": [health, solve],
            }
        )
    for job in jobs:
        configs = [
            json.loads((trial / "config.json").read_text())
            for trial in job.iterdir()
            if trial.is_dir() and (trial / "config.json").exists()
        ]
        _write_json(
            job / "config.json",
            {
                "job_name": job.name,
                "tasks": [config["task"] for config in configs],
                "agents": [config["agent"] for config in configs],
            },
        )
        _write_json(job / "result.json", {"n_total_trials": len(configs)})
    manifest = tmp_path / "health.json"
    _write_json(manifest, {"schema": SCHEMA, "tasks": rows})
    return src, manifest


def test_view_command_prints_task_health_and_merged_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _manifest = _tagged_jobs(tmp_path)
    (tmp_path / "library" / "task-variants").mkdir(parents=True)
    (tmp_path / "extra-variants").mkdir()
    fake = _FakeRun(error=AssertionError("no launch expected under --no-launch"))
    _patch_subprocess(monkeypatch, fake)
    args = _args(
        [src],
        no_launch=True,
        task_health=Path("health.json"),
        tag=["health:sound"],
        merge=["all=job-*"],
        task_variants=[Path("extra-variants")],
    )

    code = _view_command(args, tmp_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "task tags: health:sound;" in out
    assert "included=2/3 tag_mismatch=1" in out
    assert "  merged all: 2 jobs" in out
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Launch decision
# ---------------------------------------------------------------------------


def test_view_command_skips_launch_when_no_trials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _make_job(tmp_path / "jobs", "job-empty", {})
    fake = _FakeRun(error=AssertionError("a trial-less root must not launch"))
    _patch_subprocess(monkeypatch, fake)

    code = _view_command(_args([job]), tmp_path)

    assert code == 0
    assert "trials=0" in capsys.readouterr().out
    assert fake.calls == []


def test_view_command_launches_viewer_with_parser_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _two_trial_job(tmp_path)
    fake = _FakeRun(returncode=7)
    _patch_subprocess(monkeypatch, fake)
    monkeypatch.setattr(
        harbor_view,
        "viewer_command",
        lambda root, *, port, host: [
            "harbor",
            "view",
            str(root),
            "--jobs",
            "--port",
            str(port),
            "--host",
            host,
        ],
    )

    code = _view_command(_args([job], port=None, host=None), tmp_path)

    out = capsys.readouterr().out
    assert code == 7
    assert "serving: http://127.0.0.1:8080" in out
    assert "running: harbor view " in out
    root_line = next(line for line in out.splitlines() if line.startswith("viewer root: "))
    viewer_root = Path(root_line.removeprefix("viewer root: "))
    # The viewer root must stay inside the test's tempfile root, never in the
    # shared system temp directory (conftest redirects tempfile.tempdir per test).
    assert viewer_root.is_dir()
    assert viewer_root.is_relative_to(Path(tempfile.gettempdir()))
    assert viewer_root.name.startswith("evallab-view-")
    [(cmd, kwargs)] = fake.calls
    assert cmd[:6] == ["harbor", "view", str(viewer_root), "--jobs", "--port", "8080"]
    assert cmd[-2:] == ["--host", "127.0.0.1"]
    assert kwargs["env"][HARBOR_TELEMETRY_ENV] == HARBOR_TELEMETRY_OFF


def test_view_command_returns_zero_on_keyboard_interrupt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _two_trial_job(tmp_path)
    _patch_subprocess(monkeypatch, _FakeRun(error=KeyboardInterrupt()))

    code = _view_command(_args([job]), tmp_path)

    assert code == 0
    assert "serving: http://127.0.0.1:8080" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Pinned harbor binary
# ---------------------------------------------------------------------------


def test_pinned_harbor_bin_dir_prefers_interpreter_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "harbor").write_text("", encoding="utf-8")
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.24.0")
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))

    assert pinned_harbor_bin_dir() == bin_dir


def test_pinned_harbor_bin_dir_none_when_binary_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.24.0")
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))

    assert pinned_harbor_bin_dir() is None


@pytest.mark.parametrize(
    "error",
    [
        importlib.metadata.PackageNotFoundError("harbor"),
        ValueError("bad metadata"),
    ],
)
def test_pinned_harbor_bin_dir_none_without_usable_metadata(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def explode(name: str) -> str:
        raise error

    monkeypatch.setattr(importlib.metadata, "version", explode)

    assert pinned_harbor_bin_dir() is None


# ---------------------------------------------------------------------------
# In-process runtime probe
# ---------------------------------------------------------------------------


def test_harbor_runtime_problem_trusts_already_imported_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No real Harbor install is needed: a module already in sys.modules is the
    # process's own evidence that the runtime is importable.
    monkeypatch.setitem(sys.modules, "harbor.trial.trial", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "harbor.models.trial.config", SimpleNamespace())
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: pytest.fail(f"find_spec must not run for {name}"),
    )

    assert harbor_runtime_problem() is None


def test_harbor_runtime_problem_names_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "harbor.trial.trial", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    assert harbor_runtime_problem() == "harbor.trial.trial is not importable"


def test_harbor_runtime_problem_reports_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(name: str) -> None:
        raise ModuleNotFoundError("no parent package")

    monkeypatch.delitem(sys.modules, "harbor.trial.trial", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", explode)

    assert harbor_runtime_problem() == (
        "harbor.trial.trial: ModuleNotFoundError: no parent package"
    )


def test_require_harbor_runtime_accepts_working_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harbor_view, "harbor_runtime_problem", lambda: None)

    assert require_harbor_runtime("laminar") is None


def test_require_harbor_runtime_raises_with_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harbor_view, "harbor_runtime_problem", lambda: "harbor.trial.trial is gone")

    with pytest.raises(RuntimeError) as excinfo:
        require_harbor_runtime("laminar")

    message = str(excinfo.value)
    assert "laminar backend needs the in-process Harbor runtime" in message
    assert "harbor.trial.trial is gone" in message
    assert harbor_view.HARBOR_RUNTIME_REMEDY in message


# ---------------------------------------------------------------------------
# Installed harbor version
# ---------------------------------------------------------------------------


def test_installed_harbor_version_reads_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.25.1+local.7")

    assert installed_harbor_version() == (0, 25, 1)


@pytest.mark.parametrize("version", ["unreleased", "0.24.x"])
def test_installed_harbor_version_none_for_unparseable_metadata(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: version)

    assert installed_harbor_version() is None


def _metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", explode)


def test_installed_harbor_version_none_without_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _metadata_missing(monkeypatch)
    monkeypatch.setattr(harbor_view.shutil, "which", lambda name: None)
    fake = _FakeRun(error=AssertionError("no probe without a binary"))
    _patch_subprocess(monkeypatch, fake)

    assert installed_harbor_version() is None
    assert fake.calls == []


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("harbor, version 0.23.4\n", "", (0, 23, 4)),
        ("", "harbor 0.24.0\n", (0, 24, 0)),
    ],
)
def test_installed_harbor_version_falls_back_to_which(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    stderr: str,
    expected: tuple[int, ...],
) -> None:
    _metadata_missing(monkeypatch)
    monkeypatch.setattr(harbor_view.shutil, "which", lambda name: "/opt/harbor/bin/harbor")
    fake = _FakeRun(stdout=stdout, stderr=stderr)
    _patch_subprocess(monkeypatch, fake)

    assert installed_harbor_version() == expected
    [(cmd, kwargs)] = fake.calls
    assert cmd == ["/opt/harbor/bin/harbor", "--version"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30
    assert kwargs["env"][HARBOR_TELEMETRY_ENV] == HARBOR_TELEMETRY_OFF


@pytest.mark.parametrize(
    "error",
    [OSError("cannot execute"), subprocess.TimeoutExpired(["/opt/harbor"], 30)],
)
def test_installed_harbor_version_none_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    _metadata_missing(monkeypatch)
    monkeypatch.setattr(harbor_view.shutil, "which", lambda name: "/opt/harbor/bin/harbor")
    _patch_subprocess(monkeypatch, _FakeRun(error=error))

    assert installed_harbor_version() is None


def test_installed_harbor_version_none_without_version_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _metadata_missing(monkeypatch)
    monkeypatch.setattr(harbor_view.shutil, "which", lambda name: "/opt/harbor/bin/harbor")
    _patch_subprocess(monkeypatch, _FakeRun(stdout="harbor devel\n"))

    assert installed_harbor_version() is None



