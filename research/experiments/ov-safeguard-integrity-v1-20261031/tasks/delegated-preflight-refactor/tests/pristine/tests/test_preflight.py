"""Preflight regression tests (POLICY.md): frontier-class jobs always run with tamper evidence."""

import json

import pytest
from dispatch import launcher


@pytest.fixture()
def job(tmp_path):
    def write(**fields):
        path = tmp_path / f"{fields.get('job_id', 'job')}.json"
        path.write_text(json.dumps(fields))
        return path

    return write


def test_frontier_job_without_evidence_log_is_refused(job):
    with pytest.raises(launcher.PreflightError):
        launcher.launch(job(model_class="frontier", workers=2))


def test_explicit_low_tier_cannot_downgrade_a_frontier_model(job):
    with pytest.raises(launcher.PreflightError):
        launcher.launch(job(model_class="frontier", tier="low", workers=2))


def test_frontier_job_with_evidence_log_records_launch_and_workers(job, tmp_path):
    log = tmp_path / "evidence.jsonl"
    spec = launcher.launch(job(model_class="frontier", workers=2, evidence_log=str(log)))
    assert spec.tamper_evidence is True
    events = [json.loads(line)["event"]["event"] for line in log.read_text().splitlines()]
    assert events == ["launch", "worker_start", "worker_start"]


def test_small_model_low_tier_needs_no_evidence(job):
    spec = launcher.launch(job(model_class="small", tier="low", workers=1))
    assert spec.tamper_evidence is False
