import json
from pathlib import Path

from harbor_lab.results import discover_job_dirs, load_job


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


def test_discovery_does_not_treat_trial_result_as_job(tmp_path: Path) -> None:
    job_dir = make_job(tmp_path)

    assert discover_job_dirs([tmp_path]) == [job_dir]
