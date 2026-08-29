import json
from pathlib import Path

import pytest

from evallab.results import discover_job_dirs, load_job


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_job(root: Path) -> Path:
    job = root / "sample-job"
    trial = job / "sample-task__abc123"
    write_json(job / "config.json", {"job_name": "sample-job"})
    write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    write_json(
        job / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "started_at": "2026-08-13T12:00:00Z",
            "finished_at": "2026-08-13T12:00:02Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
        },
    )
    write_json(trial / "config.json", {"agent": {"name": "oracle"}})
    write_json(trial / "lock.json", {"schema_version": 2})
    write_json(
        trial / "result.json",
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "trial_name": trial.name,
            "task_name": "local-lab/sample-task",
            "task_checksum": "abc",
            "started_at": "2026-08-13T12:00:00Z",
            "finished_at": "2026-08-13T12:00:01.5Z",
            "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
            "agent_result": {
                "n_input_tokens": None,
                "n_cache_tokens": None,
                "n_output_tokens": None,
                "cost_usd": None,
            },
            "verifier_result": {"rewards": {"reward": 1.0, "correctness": 1.0}},
            "exception_info": None,
        },
    )
    artifact = trial / "artifacts/app/output/answer.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"answer": 42}\n')
    write_json(
        trial / "artifacts/manifest.json",
        [
            {
                "source": "/app/output/answer.json",
                "destination": "artifacts/app/output/answer.json",
                "type": "file",
                "status": "ok",
                "service": None,
            }
        ],
    )
    (trial / "verifier").mkdir()
    (trial / "verifier/reward.json").write_text('{"reward": 1}\n')
    return job


def test_load_job_indexes_trials_rewards_artifacts_and_files(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)

    job = load_job(job_dir)

    assert job.id == "00000000-0000-0000-0000-000000000001"
    assert job.harbor_version == "0.21.0"
    assert len(job.trials) == 1
    assert job.trials[0].primary_reward == 1.0
    assert job.trials[0].artifacts[0].exists is True
    assert job.trials[0].artifacts[0].size_bytes == 15
    kinds = {item.relative_path: item.kind for item in job.files}
    assert kinds["result.json"] == "result"
    assert kinds[f"{job.trials[0].name}/verifier/reward.json"] == "verifier_evidence"


def test_artifact_destination_cannot_escape_trial_directory(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    trial_dir = next(path for path in job_dir.iterdir() if path.is_dir())
    outside = job_dir / "outside.txt"
    outside.write_text("secret")
    write_json(
        trial_dir / "artifacts/manifest.json",
        [
            {
                "source": "/app/output/answer.json",
                "destination": "../outside.txt",
                "type": "file",
                "status": "ok",
                "service": None,
            }
        ],
    )

    with pytest.raises(ValueError, match="artifact destination escapes"):
        load_job(job_dir)


def test_completed_job_reconciles_declared_trial_count(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    result_path = job_dir / "result.json"
    result = json.loads(result_path.read_text())
    result["n_total_trials"] = 2
    result["stats"]["n_completed_trials"] = 2
    write_json(result_path, result)

    with pytest.raises(ValueError, match="trial count mismatch"):
        load_job(job_dir)


def test_discovery_does_not_treat_trial_result_as_job(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)

    assert discover_job_dirs([tmp_path]) == [job_dir]


def test_partial_harbor_job_is_not_a_completed_job(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)
    result_path = job_dir / "result.json"
    result = json.loads(result_path.read_text())
    result["finished_at"] = None
    write_json(result_path, result)

    assert discover_job_dirs([tmp_path]) == []
    with pytest.raises(ValueError, match="Not a completed Harbor job"):
        load_job(job_dir)


# ---- malformed result.json discovery (preserved failed-artifact evidence) ----


def test_discovery_survives_a_malformed_nested_artifact_result(tmp_path: Path) -> None:
    """A faithfully-preserved failed-trial artifact whose result.json is not
    valid JSON (the agent wrote a scalar before the document) must not abort
    discovery or hide the real job around it."""
    job_dir = make_job(tmp_path)
    malformed = job_dir / "sample-task__abc123/artifacts/app/output/result.json"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("3\n{\n  \"target\": \"n_2_0\",\n  \"value\": 3\n}\n")

    # Discovery still finds the real job and does not crash.
    assert discover_job_dirs([tmp_path]) == [job_dir.resolve()]
    # The job still loads; the malformed artifact is preserved, not dropped.
    job = load_job(job_dir)
    assert job.id == "00000000-0000-0000-0000-000000000001"
    assert malformed.exists()


def test_discovery_skips_a_malformed_job_level_result(tmp_path: Path) -> None:
    """A job whose own result.json is malformed is not a completed Harbor job:
    discovery must skip it (not crash), while explicit load stays fail-closed."""
    job_dir = make_job(tmp_path)
    (job_dir / "result.json").write_text("not-json", encoding="utf-8")

    assert discover_job_dirs([tmp_path]) == []
    with pytest.raises(Exception):
        load_job(job_dir)


def test_load_job_fails_closed_on_a_malformed_job_result(tmp_path: Path) -> None:
    """Discovery is lenient, but selecting the job explicitly must not be: a
    malformed job-level result raises rather than silently loading an empty job."""
    job_dir = make_job(tmp_path)
    (job_dir / "result.json").write_text("garbage", encoding="utf-8")

    with pytest.raises(Exception):
        load_job(job_dir)
