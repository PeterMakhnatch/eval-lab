"""Explorer contract tests (M005). Fixture-driven, read-only, no host state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from evallab.evidence.atif import project_trial
from evallab.evidence.facts import write_analysis_review
from evallab.explorer import (
    redact_mapping,
)
from evallab.results import load_job
from evallab.status import build_status_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
JOBS = FIXTURES / "jobs"
ANALYSES = FIXTURES / "analyses"


def test_the_fixture_records_exit_codes_where_the_validated_shape_puts_them():
    """The exit codes are in the document, and `atif` reads all four of them.

    `atif.command_exit_code` takes an exit code
    from `observation.results[].extra.{exit_code,returncode,return_code}` and
    projects it as the derived column `command_exit_code`. Asserting it here
    fixes the fact that the data *is* in the fixture, so the explorer's
    blindness to it in the next test is a defect in the explorer's reader
    rather than a fixture that forgot to record an exit code.
    """
    # .resolve(): project_trial derives source paths relative to trial.path and
    # raises ValueError for a JobRecord loaded through a relative path.
    job = load_job((JOBS / "job-fail").resolve())
    projection = project_trial(job, job.trials[0])
    assert [t.validation_status for t in projection.trajectories] == ["valid"]
    assert sorted(
        (observation.source_call_id, observation.command_exit_code)
        for observation in projection.observations
    ) == [("L0", 2), ("L1", 2), ("L2", 2), ("L3", 2)]


def test_secret_shaped_config_values_are_redacted(tmp_path: Path):
    """Redaction proven against a constructed config, no longer against fiction.

    The committed fixture used to carry `{"env": {"FAKE_API_KEY": ...}}`. Harbor
    writes no `env` mapping into a trial `config.json` — the real file holds
    `agent`, `task`, `trial_name`, `trials_dir`, `job_id` and nothing else — so
    the suite was proving the redactor against a document that cannot exist.
    The fixture now matches the real shape and the adversarial input is built
    here, which is where a hypothetical belongs.
    """
    assert redact_mapping({"MY_TOKEN": "x"})["MY_TOKEN"] == "[redacted]"
    nested = redact_mapping({"providers": [{"PASSWORD": "nested-secret"}]})
    assert "nested-secret" not in str(nested)


def test_review_writer_persists_disposition(tmp_path: Path):
    """The review writer persists the supplied decision beside the sidecar."""
    analyses = tmp_path / "analyses"
    shutil.copytree(ANALYSES / "valid", analyses / "11111111-1111-4111-8111-111111111111")
    sidecar_path = analyses / "11111111-1111-4111-8111-111111111111" / "analysis.json"
    review_path, review = write_analysis_review(
        sidecar_path,
        disposition="accepted",
        rationale="reviewed during the M009 flight",
        reviewer="operator-fixes",
    )
    assert review_path.is_file()  # the review really is next to the sidecar

    saved = json.loads(review_path.read_text())
    assert saved["analysis_id"] == str(review.analysis_id)
    assert saved["disposition"] == "accepted"


def test_status_snapshot_preserves_agent_exit_error():
    # FIXTURES has jobs/ and no src/evallab -> status scratch layout applies
    snapshot = build_status_snapshot(FIXTURES)
    status_text = snapshot.model_dump_json()
    assert "AgentTimeoutError" in status_text


