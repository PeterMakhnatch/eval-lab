"""Render supplied experiment reports without rediscovering retired evidence views."""

from evallab.explorer import render_experiment_text


def test_control_absence_is_not_applicable_but_model_gap_is_missing() -> None:
    report = {
        "jobs": [{
            "name": "mixed-job",
            "origin": "harbor_native",
            "experiment_id": "harbor_unbound",
            "trials": [
                {
                    "trial_name": "oracle-trial",
                    "agent": {"name": "oracle"},
                    "reward": {"value": None, "state": "unavailable"},
                    "capture": {"state": "not_applicable"},
                    "usage": {"state": "not_applicable"},
                },
                {
                    "trial_name": "model-trial",
                    "agent": {"name": "codex"},
                    "reward": {"value": None, "state": "unavailable"},
                    "capture": {"state": "missing"},
                    "usage": {"state": "partial", "input_tokens": 10},
                },
            ],
        }],
    }
    text = render_experiment_text(report)
    assert "capture=not_applicable" in text
    assert "capture=missing" in text
    assert "reward=None (unavailable)" in text
    assert "in=10" in text and "(partial)" in text


def test_malformed_selected_job_stays_visible_with_reason() -> None:
    report = {"jobs": [{
        "name": "broken-job",
        "execution_status": "failed",
        "load_issue": "Invalid JSON in result.json",
        "trials": [],
    }]}
    text = render_experiment_text(report)
    assert "broken-job" in text and "status=failed" in text
    assert "unavailable: Invalid JSON in result.json" in text
    assert "reward=" not in text


def test_unbound_truncated_report_never_certifies_selected_job() -> None:
    report = {"capture_report": {
        "status": "supplied_unbound_scope",
        "scope": "unbound",
        "summary": {"sections": {
            "native_jobs_present": {"count": 1, "jobs": ["job-a"], "truncated": True},
        }},
    }}
    text = render_experiment_text(report)
    assert "supplied_unbound_scope scope=unbound" in text
    assert "count=1 truncated=True jobs=[job-a]" in text


def test_scoped_coverage_preserves_conflicting_annotation() -> None:
    report = {"capture_report": {
        "status": "supplied",
        "scope": "bound",
        "external_links": [{
            "kind": "lab-spec-binding",
            "job_name": "r2-job",
            "status": "evidence-intact-not-cataloged-not-projected",
        }],
    }}
    text = render_experiment_text(report)
    assert "scope=bound" in text
    assert "r2-job" in text
    assert "evidence-intact-not-cataloged-not-projected" in text
