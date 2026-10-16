"""Deterministic rollout tests: fake executor, no Modal, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.rollout import (
    TrialOutcome,
    run_rollout,
    sandbox_cost_usd,
    summarize,
    task_resources,
    validate_model_selector,
)


def _task(tmp_path: Path, name: str, *, cpus: int = 2, memory_mb: int = 8192) -> Path:
    task = tmp_path / name
    task.mkdir()
    (task / "task.toml").write_text(
        f'[environment]\ncpus = {cpus}\nmemory_mb = {memory_mb}\n', encoding="utf-8"
    )
    return task


def test_selector_validation_admits_the_three_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_model_selector("tinker/Qwen/Qwen3.5-9B") == "tinker"
    assert validate_model_selector("openrouter-metered/xiaomi/mimo-v2.6-flash") == "openrouter"
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "https://lora.example/modal")
    base = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    assert validate_model_selector(base) == "selfhosted"
    for adapter in ("rds1-a", "rds1-b", "rds1-c"):
        assert validate_model_selector(f"{base}:{adapter}") == "selfhosted"


def test_selector_validation_refuses_unknown_and_missing_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", raising=False)
    with pytest.raises(ValueError, match="EVALLAB_MIMO_SELFHOSTED_UPSTREAM"):
        validate_model_selector("selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-a")
    with pytest.raises(ValueError):
        validate_model_selector("selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-z")
    with pytest.raises(ValueError, match="unroutable"):
        validate_model_selector("gpt-5")


def test_attempts_expand_to_independent_trials(tmp_path: Path) -> None:
    tasks = [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")]
    summary = run_rollout(
        tasks,
        model="tinker/Qwen/Qwen3.5-9B",
        concurrency=4,
        attempts=3,
        out_dir=tmp_path / "out",
        root=tmp_path,
        execute=_fake_execute,
    )
    assert summary["trials"] == 6 and summary["attempts"] == 3
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    ]
    assert {row["name"].rsplit("-a", 1)[-1] for row in rows} == {"1", "2", "3"}


def test_cli_forwards_attempts_to_run_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from evallab import rollout

    captured: dict[str, object] = {}

    def fake_run_rollout(tasks: list[Path], **kwargs: object) -> dict[str, int]:
        captured.update(kwargs)
        return {"errors": 0}

    monkeypatch.setattr(rollout, "run_rollout", fake_run_rollout)
    parser = argparse.ArgumentParser()
    rollout.build_rollout_parser(parser.add_subparsers())
    task = _task(tmp_path, "task-a")
    args = parser.parse_args(
        [
            "rollout",
            "run",
            "--task",
            str(task),
            "--model",
            "tinker/Qwen/Qwen3.5-9B",
            "--out",
            str(tmp_path / "out"),
            "--actor",
            "tester",
            "--attempts",
            "2",
        ]
    )
    assert args.func(args, tmp_path) == 0
    assert captured["attempts"] == 2



def test_arm_slugs_are_disjoint() -> None:
    import re

    from evallab.rollout import _arm_slug

    base = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    assert _arm_slug(base) == "mimo-v2-6-distill-qwen-9b"
    assert _arm_slug(f"{base}:rds1-b") == "mimo-v2-6-distill-qwen-9b-rds1-b"
    slugs = {
        _arm_slug(base),
        *(_arm_slug(f"{base}:{arm}") for arm in ("rds1-a", "rds1-b", "rds1-c")),
    }
    assert len(slugs) == 4
    name_re = re.compile(r"^[a-z0-9][a-z0-9-]+$")
    for slug in slugs:
        assert name_re.match(f"rollout-{slug}-000-7d0add50180f-a1")


def test_production_spec_names_are_admitted() -> None:
    from evallab.rollout import _arm_slug
    from evallab.schemas import ExperimentSpec

    base = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
    for model in (base, *(f"{base}:{a}" for a in ("rds1-a", "rds1-b", "rds1-c"))):
        spec = ExperimentSpec(
            spec_id="01M00000000000000000000000",
            name=f"rollout-{_arm_slug(model)}-000-7d0add50180f-a1",
            hypothesis="test",
            purpose="baseline",
            task="derived/task",
            task_path="derived/task",
            agent="terminus-2",
            model=model,
            environment="modal",
            jobs_dir="runs",
            submitted_by="infra-rollout",
            timeout_seconds=1800,
            est_cost_usd=0.10,
            cost_limit_usd=1.00,
            max_requests=300,
            max_input_tokens=2_000_000,
            max_output_tokens=300_000,
            max_total_tokens=2_300_000,
            concurrency=1,
            attempts=1,
        )
        assert spec.name.endswith("-a1")


def test_selfhosted_specs_carry_profile_lock_and_deviations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evallab.queue as queue_module
    from evallab import rollout

    captured: list = []

    class FakeQueue:
        def approve(self, spec_id: str, *, actor: str) -> None:
            pass

        def locate(self, spec_id: str) -> Path:
            return tmp_path / "done" / "spec.json"

    class FakeExecutor:
        queue = FakeQueue()

        @classmethod
        def from_repo(cls, root: Path, **kwargs: object) -> FakeExecutor:
            return cls()

        def submit(self, spec: object) -> None:
            captured.append(spec)

        def tick(self, **kwargs: object) -> int:
            return 0

    monkeypatch.setattr(queue_module, "Executor", FakeExecutor)
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "https://trainer.example/modal")
    tasks = [_task(tmp_path, "task-a")]
    rollout.run_rollout(
        tasks,
        model="selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-b",
        agent="mimoagent",
        concurrency=2,
        attempts=2,
        out_dir=tmp_path / "out",
        root=tmp_path,
        actor="research-harbor",
    )
    assert len(captured) == 2
    spec = captured[0]
    assert spec.reference_profile == "xiaomi-mimo-rl"
    assert spec.egress_lock is True
    assert {d.field for d in spec.deviations} == {
        "harness.additions",
        "budgets.max_requests",
    }
    assert spec.name.endswith("-a1") and captured[1].name.endswith("-a2")

def _fake_execute(request):  # type: ignore[no-untyped-def]
    job_dir = request.jobs_dir / request.name
    trial = job_dir / f"{request.name}__trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}), encoding="utf-8"
    )
    (job_dir / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-16T00:00:00+00:00",
                "finished_at": "2026-10-16T00:01:00+00:00",
                "provider_usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cost_usd": 0.05,
                },
            }
        ),
        encoding="utf-8",
    )
    return job_dir


def test_sandbox_cost_uses_declared_resources(tmp_path: Path) -> None:
    task = _task(tmp_path, "shaped")
    assert task_resources(task) == (2.0, 8192.0)
    # 60 s at 2 cores + 8 GiB at Modal list prices.
    expected = 60 * (2 * 0.0000131 + 8 * 0.00000222)
    assert abs(sandbox_cost_usd(60, cpus=2.0, memory_mb=8192.0) - expected) < 1e-12


def test_run_rollout_reports_throughput_and_cost(tmp_path: Path) -> None:
    tasks = [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")]
    out = tmp_path / "out"
    summary = run_rollout(
        tasks,
        model="tinker/Qwen/Qwen3.5-9B",
        concurrency=2,
        out_dir=out,
        root=tmp_path,
        execute=_fake_execute,
    )
    assert summary["completed"] == 2 and summary["errors"] == 0
    assert summary["passed"] == 2 and summary["pass_rate"] == 1.0
    assert summary["tasks_per_hour"] > 0
    per_task = 0.05 + sandbox_cost_usd(60, cpus=2.0, memory_mb=8192.0)
    assert abs(summary["cost_per_task_usd"] - round(per_task, 4)) < 1e-9
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert {row["reward"] for row in rows} == {1.0}
    assert (out / "summary.json").is_file() and (out / "spend.jsonl").is_file()


def test_one_failed_trial_does_not_kill_the_batch(tmp_path: Path) -> None:
    def flaky(request):  # type: ignore[no-untyped-def]
        if "task-b" in request.name:
            raise RuntimeError("sandbox refused")
        return _fake_execute(request)

    tasks = [_task(tmp_path, "task-a"), _task(tmp_path, "task-b")]
    summary = run_rollout(
        tasks,
        model="tinker/Qwen/Qwen3.5-9B",
        concurrency=2,
        out_dir=tmp_path / "out",
        root=tmp_path,
        execute=flaky,
    )
    assert summary["completed"] == 1 and summary["errors"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    ]
    assert any("sandbox refused" in (row["error"] or "") for row in rows)


def test_summarize_handles_no_completions() -> None:
    outcome = TrialOutcome(
        task="t", name="rollout-000-t", reward=None, passed=None, wall_seconds=1.0,
        sandbox_seconds=None, input_tokens=None, output_tokens=None,
        model_cost_usd=None, sandbox_cost_usd=None, job_dir="", error="boom",
    )
    summary = summarize([outcome], batch_wall_seconds=10.0)
    assert summary["pass_rate"] is None and summary["cost_per_task_usd"] is None
    assert summary["tasks_per_hour"] == 0.0
