"""Native Harbor decisions at the Lab's real request/queue boundary; no execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from evallab.execution_contracts import RunRequest
from evallab.job_diff import _local_plan, diff_sources, preview_diff
from evallab.queue import DirectoryQueue, Executor, approved_spec_digest, load_events
from evallab.runner import stage_request_task
from evallab.schemas import AutoRunRule, ExperimentSpec, StandingApprovalsPolicy
from evallab.setup_fingerprint import lock_setup_fingerprint


def task(root: Path, *, version: str = "1.0.0") -> Path:
    path = root / "library/tasks/diff-task"
    path.mkdir(parents=True, exist_ok=True)
    (path / "task.toml").write_text(
        f'[task]\nname = "test/diff-task"\nversion = "{version}"\n'
        '[verifier]\nenvironment_mode = "separate"\n'
        '[agent]\ntimeout_sec = 60\n[environment]\nnetwork_mode = "public"\n'
    )
    (path / "instruction.md").write_text("Write the answer artifact.\n")
    (path / "environment").mkdir(exist_ok=True)
    (path / "environment/Dockerfile").write_text("FROM python:3.12-slim\n")
    (path / "tests").mkdir(exist_ok=True)
    (path / "tests/test.sh").write_text("#!/bin/sh\nprintf '0' > /logs/verifier/reward.txt\n")
    return path


def request(root: Path, **changes) -> RunRequest:
    base = RunRequest(
        task=task(root), agent="nop", name="prior-job", jobs_dir=root / "runs", timeout_seconds=60
    )
    return replace(base, **changes)


def stored_job(root: Path, original: RunRequest, *, complete: bool = True) -> Path:
    pytest.importorskip("harbor.job_diff", reason="native diff requires the Harbor 0.24 runtime")
    from harbor.models.trial.result import AgentInfo, TrialResult
    from harbor.models.verifier.result import VerifierResult
    from harbor.trial.regrade import local_task_name

    job = original.jobs_dir / original.name
    job.mkdir(parents=True)
    with TemporaryDirectory() as temporary:
        staged, _ = stage_request_task(original, Path(temporary).resolve() / "task")
        plan = _local_plan(
            replace(original, task=staged),
            repo_root=root,
            setup_fingerprint=lock_setup_fingerprint(original, repo_root=root),
        )
        (job / "config.json").write_text(plan.config.model_dump_json())
        (job / "lock.json").write_text(plan.job_lock.model_dump_json())
        (job / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": original.attempts,
                    "stats": {},
                    "finished_at": "2026-10-06T00:00:00Z",
                }
            )
        )
        for index, (config, lock) in enumerate(
            zip(plan.trial_configs, plan.trial_locks, strict=True)
        ):
            trial = job / f"source-{index}"
            trial.mkdir()
            result = TrialResult(
                task_name=local_task_name(staged),
                trial_name=trial.name,
                trial_uri=trial.as_uri(),
                task_id=config.task.get_task_id(),
                task_checksum=lock.task.digest,
                config=config,
                agent_info=AgentInfo(name=original.agent, version="fixture"),
                finished_at=datetime(2026, 10, 6, tzinfo=UTC) if complete else None,
                verifier_result=VerifierResult(rewards={"reward": 0.0}),
            )
            (trial / "result.json").write_text(result.model_dump_json())
            (trial / "lock.json").write_text(lock.model_dump_json())
    if original.experiment_spec:
        (job / "experiment-spec.json").write_text(original.experiment_spec.model_dump_json())
    return job


def test_temperature_change_forces_native_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evallab.execution_contracts as contracts

    original = request(
        tmp_path,
        agent="terminus-2",
        environment="daytona",
        egress_lock=True,
        model="selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
    )
    source = stored_job(tmp_path, original)
    target = replace(original, name="next-job", diff_sources=(source,))
    assert preview_diff(target, repo_root=tmp_path).counts == {"reuse": 1, "regrade": 0, "rerun": 0}
    monkeypatch.setattr(contracts, "MIMO_SELFHOSTED_TEMPERATURE", 1.0)
    changed = preview_diff(target, repo_root=tmp_path)
    assert changed.counts == {"reuse": 0, "regrade": 0, "rerun": 1}
    assert changed.trials[0].reason == "non-task trial inputs differ"


@pytest.mark.parametrize(
    ("version", "action"), [("1.0.1", "reuse"), ("1.1.0", "regrade"), ("2.0.0", "rerun")]
)
def test_semver_uses_native_actions_without_locking_task_bookkeeping(
    tmp_path: Path, version: str, action: str
) -> None:
    original = request(tmp_path)
    source = stored_job(tmp_path, original)
    task(tmp_path, version=version)
    target = replace(original, name="new-job", diff_sources=(source,))
    assert preview_diff(target, repo_root=tmp_path).trials[0].action == action


def test_incomplete_sources_and_cross_version_locks_are_not_reused(tmp_path: Path) -> None:
    original = request(tmp_path)
    source = stored_job(tmp_path, original, complete=False)
    target = replace(original, name="new-job", diff_sources=(source,))
    incomplete = preview_diff(target, repo_root=tmp_path)
    assert incomplete.trials[0].reason == "source trials failed or are incomplete"
    lock = json.loads((source / "lock.json").read_text())
    lock["harbor"]["version"] = "0.21.0"
    (source / "lock.json").write_text(json.dumps(lock))
    old = preview_diff(target, repo_root=tmp_path)
    assert old.counts == {"reuse": 0, "regrade": 0, "rerun": 1}
    assert old.sources == ()
    assert "0.21.0" in old.warnings[0]


def test_reuse_does_not_duplicate_trials_when_attempt_count_grows(tmp_path: Path) -> None:
    original = request(tmp_path)
    source = stored_job(tmp_path, original)
    target = replace(original, name="three-attempts", attempts=3, diff_sources=(source,))
    plan = preview_diff(target, repo_root=tmp_path)
    assert plan.counts == {"reuse": 1, "regrade": 0, "rerun": 2}
    assert [trial.source_trial_id for trial in plan.trials if trial.action == "reuse"] == [
        json.loads((source / "source-0/result.json").read_text())["id"]
    ]


def test_same_version_mutation_is_not_silently_treated_as_reuse(tmp_path: Path) -> None:
    original = request(tmp_path)
    source = stored_job(tmp_path, original)
    (original.task / "instruction.md").write_text("A different problem without a version bump.\n")
    with pytest.raises(ValueError, match="digests differ"):
        preview_diff(replace(original, name="new-job", diff_sources=(source,)), repo_root=tmp_path)


def test_source_selection_is_explicit_and_approval_bound(tmp_path: Path) -> None:
    spec = ExperimentSpec(
        name="prior-job",
        hypothesis="diff behavior",
        purpose="comparison",
        task="library/tasks/diff-task",
        agent="nop",
        submitted_by="test",
        grid_id="same-cohort",
    )
    original = request(tmp_path, experiment_spec=spec)
    source = stored_job(tmp_path, original)
    candidate = spec.model_copy(update={"name": "candidate"})
    target = replace(original, name="candidate", experiment_spec=candidate)
    assert diff_sources(target, repo_root=tmp_path) == (source,)
    fresh = candidate.model_copy(update={"diff_sources": []})
    assert diff_sources(replace(target, experiment_spec=fresh), repo_root=tmp_path) == ()
    assert approved_spec_digest(candidate) != approved_spec_digest(fresh)
    unrelated = candidate.model_copy(update={"grid_id": "another-cohort"})
    assert diff_sources(replace(target, experiment_spec=unrelated), repo_root=tmp_path) == ()


def test_all_reuse_completes_queue_without_runner_reservation_or_ingest(tmp_path: Path) -> None:
    original = request(tmp_path)
    source = stored_job(tmp_path, original)
    before = {
        path.relative_to(source): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*")
        if path.is_file()
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("reused observations must not execute or be re-ingested")

    executor = Executor(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=StandingApprovalsPolicy(
            daily_cost_ceiling_usd=1,
            per_job_cost_ceiling_usd=1,
            quiet_failure_rule=3,
            auto_run=[AutoRunRule(name="local-controls", agents=["nop"])],
            escalate_to_human=[],
        ),
        runner=forbidden,
        ingester=forbidden,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=frozenset,
        watch_enabled=False,
    )
    spec = ExperimentSpec(
        name="reused-job",
        hypothesis="reuse stored control",
        purpose="practice",
        task="library/tasks/diff-task",
        agent="nop",
        submitted_by="test",
        timeout_seconds=60,
        diff_sources=[str(source)],
    )
    executor.submit(spec)
    assert executor.tick() == 1
    assert [item.name for _, item in executor.queue.list_specs("done")] == ["reused-job"]
    assert not (original.jobs_dir / "reused-job").exists()
    events = load_events(executor.queue.events_path)
    assert any(event.event == "dispatch_reused" for event in events)
    assert not any(event.event == "dispatch_attempt_reserved" for event in events)
    after = {
        path.relative_to(source): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_question_refs_do_not_reuse_independent_replicates(tmp_path: Path) -> None:
    spec = ExperimentSpec(
        name="prior-job",
        hypothesis="Repeated independent samples must not become cached observations",
        purpose="comparison",
        question_ref="same-research-question",
        task="library/tasks/diff-task",
        agent="nop",
        submitted_by="test",
    )
    original = request(tmp_path, experiment_spec=spec)
    stored_job(tmp_path, original)
    next_attempt = spec.model_copy(update={"name": "independent-replicate"})
    target = replace(original, name=next_attempt.name, experiment_spec=next_attempt)
    assert diff_sources(target, repo_root=tmp_path) == ()
    assert preview_diff(target, repo_root=tmp_path).counts == {
        "reuse": 0, "regrade": 0, "rerun": 1
    }


def test_parser_code_change_under_same_name_forces_rerun(tmp_path: Path) -> None:
    parser = tmp_path / "src/evallab/mimo_tool_calls.py"
    parser.parent.mkdir(parents=True)
    parser.write_text("def normalize(text):\n    return text\n")
    original = request(
        tmp_path,
        agent="terminus-2",
        environment="daytona",
        egress_lock=True,
        model="selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B",
    )
    source = stored_job(tmp_path, original)
    target = replace(original, name="changed-parser", diff_sources=(source,))
    assert preview_diff(target, repo_root=tmp_path).all_reused
    parser.write_text("def normalize(text):\n    return text.strip()\n")
    assert preview_diff(target, repo_root=tmp_path).counts == {
        "reuse": 0, "regrade": 0, "rerun": 1
    }
