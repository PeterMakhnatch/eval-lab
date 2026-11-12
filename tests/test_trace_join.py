"""Guards for multi-document `session_id` ingest and schema indexing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.evidence.atif import project_trial
from evallab.results import load_job

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "research/evidence/runs"
EVIDENCE_TRIAL = EVIDENCE / "canary-event-summary-codex-20260815/event-summary__5E3btLv"
EVIDENCE_SESSION_ID = "01a0043a-4b83-7252-a594-fa289617124f"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _evidence_trajectory() -> dict[str, Any]:
    return _load(EVIDENCE_TRIAL / "agent/trajectory.json")


def _write_trial(root: Path, trajectory: dict[str, Any]) -> Path:
    """A minimal completed Harbor job with one trial holding `trajectory`."""
    job = root / "job"
    trial = job / "task__aaaaaaa"
    (trial / "agent").mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-0000000000aa",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1},
                "finished_at": "2026-08-16T00:00:00Z",
            }
        )
    )
    for name in ("config.json", "lock.json", "lab-metadata.json"):
        (job / name).write_text("{}")
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-0000000000bb",
                "task_name": "task",
                "trial_name": "task__aaaaaaa",
                "agent_name": "codex",
            }
        )
    )
    for name in ("config.json", "lock.json"):
        (trial / name).write_text("{}")
    (trial / "agent/trajectory.json").write_text(json.dumps(trajectory))
    return job


def test_embedded_subagents_produce_several_documents_with_one_session_id(
    tmp_path: Path,
) -> None:
    """The reason `sql/schema.sql` indexes `session_id` instead of constraining it.

    ATIF v1.7 embedded subagents share their parent's `session_id` and are
    disambiguated by `trajectory_id`. `project_trial` writes one
    `trajectory_documents` row per embedded payload, so `UNIQUE (session_id)`
    would not deduplicate a multi-agent trial — it would abort its ingest.
    """
    parent = _evidence_trajectory()
    child = dict(parent, trajectory_id="subagent-1")
    parent = dict(parent, trajectory_id="root", subagent_trajectories=[child])

    job = load_job(_write_trial(tmp_path, parent))
    documents = project_trial(job, job.trials[0]).trajectories

    assert len(documents) == 2
    assert {doc.session_id for doc in documents} == {EVIDENCE_SESSION_ID}
    assert len({doc.document_id for doc in documents}) == 2
    assert sorted(doc.embedded_path or "" for doc in documents) == [
        "",
        "subagent:subagent-1",
    ]


def test_schema_indexes_session_id_without_asserting_uniqueness() -> None:
    """A guard, not a style check: a future agent will be tempted by UNIQUE."""
    lines = (ROOT / "sql/schema.sql").read_text().splitlines()
    statements = " ".join(
        line for line in lines if not line.lstrip().startswith("--")
    ).upper()
    session_ddl = [
        part for part in statements.split(";") if "TRAJECTORY_DOCUMENTS" in part
    ]

    assert any(
        "CREATE INDEX IF NOT EXISTS TRAJECTORY_DOCUMENTS_SESSION_IDX" in part
        for part in session_ddl
    )
    assert not any("UNIQUE" in part for part in session_ddl if "SESSION_ID" in part)
