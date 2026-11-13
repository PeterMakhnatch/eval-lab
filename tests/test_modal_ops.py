"""Pinned Modal runner, model identity, and retained queue teardown hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evallab import modal_ops
from evallab.modal_ops import is_selfhosted_model
from evallab.queue import DirectoryQueue, Executor
from evallab.schemas import (
    AutoRunRule,
    StandingApprovalsPolicy,
)

SELFHOSTED = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"


def completed(
    argv: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_selfhosted_model_matches_queue_selector_only() -> None:
    assert is_selfhosted_model(SELFHOSTED)
    assert not is_selfhosted_model("XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B")
    assert not is_selfhosted_model("openrouter-metered/XiaomiMiMo/MiMo-V2.6-Flash")
    assert not is_selfhosted_model(None)


def test_default_runner_runs_pinned_modal_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return completed(argv, stdout="[]")

    monkeypatch.setattr(modal_ops.subprocess, "run", fake_run)

    result = modal_ops.default_runner(tmp_path)(["app", "list", "--json"])

    assert result.stdout == "[]"
    assert calls == [
        (
            [
                "uv",
                "run",
                "--project",
                "tools/modal-mimo-serve",
                "--locked",
                "modal",
                "app",
                "list",
                "--json",
            ],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": 180,
                "cwd": tmp_path,
            },
        )
    ]


def make_executor(root: Path, **overrides) -> Executor:
    policy = StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[AutoRunRule(name="local-controls", agents=["oracle", "nop"])],
        escalate_to_human=["anything_exceeding_ceilings"],
    )
    settings: dict = {
        "repo_root": root,
        "queue": DirectoryQueue(root / "queue"),
        "policy": policy,
        "runner": lambda request: request.jobs_dir / request.name,
        "ingester": lambda path: None,
        "spent_today": lambda: 0,
        "consecutive_harness_failures": lambda: 0,
        "credential_probe": lambda: frozenset({"claude_oauth", "codex_auth"}),
        "sleeper": lambda _seconds: None,
    }
    settings.update(overrides)
    return Executor(**settings)


def test_tick_without_work_never_calls_teardown_hook(tmp_path: Path) -> None:
    calls: list = []
    service = make_executor(tmp_path, modal_teardown=lambda *args: calls.append(args) or None)
    assert service.tick() == 0
    assert calls == []
