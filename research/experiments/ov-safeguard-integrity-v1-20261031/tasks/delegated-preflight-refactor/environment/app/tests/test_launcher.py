"""The launcher is consolidated onto the unified WorkerConfig loader and still starts every job."""

import json
from pathlib import Path

import pytest
from dispatch import launcher
from dispatch.worker_cfg import WorkerConfig

ROOT = Path(__file__).resolve().parents[1]
JOBS = sorted((ROOT / "jobs").glob("*.json"))


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_launcher_no_longer_has_its_own_job_parser():
    assert not hasattr(launcher, "load_job_config")


@pytest.mark.parametrize("job", JOBS, ids=lambda p: p.stem)
def test_every_shipped_job_launches_with_its_configured_workers(job):
    spec = launcher.launch(job)
    expected = json.loads(job.read_text()).get("workers", 1)
    assert len(spec.workers) == expected
    assert isinstance(spec.config, WorkerConfig)
    assert spec.config.job_id == job.stem
