"""Native experiment evidence view: select-by-spec, controls, malformed jobs, reports.

Covers ``evallab.explorer.inspect_experiment`` / ``render_experiment_text``
through the real record parsers (queue ``load``, ``discover_job_dirs``,
``load_job``, ``extract_trial_fact``) over fixture trees — no mock echoes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.explorer import inspect_experiment, render_experiment_text

FINISHED_AT = "2026-09-01T00:00:00+00:00"


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _spec_doc(spec_id: str, *, name: str = "demo-exp", agent: str = "codex") -> dict:
    return {
        "schema_version": 1,
        "spec_id": spec_id,
        "name": name,
        "hypothesis": "fixture hypothesis",
        "purpose": "baseline",
        "task": "tasks/demo",
        "agent": agent,
        "submitted_by": "tester",
    }


def _trial_doc(
    trial_id: str,
    trial_name: str,
    *,
    agent_name: str = "codex",
    reward: float | None = 0.75,
    exception_type: str | None = None,
    usage: dict | None = None,
) -> dict:
    doc: dict = {
        "id": trial_id,
        "trial_name": trial_name,
        "task_name": "tasks/demo",
        "agent_info": {"name": agent_name, "version": "1.0"},
    }
    rewards = {} if reward is None else {"reward": reward}
    doc["verifier_result"] = {"rewards": rewards}
    if usage is not None:
        doc["agent_result"] = usage
    if exception_type is not None:
        doc["exception_info"] = {"exception_type": exception_type}
    return doc


def _make_job(
    runs: Path,
    job_name: str,
    *,
    job_id: str,
    spec_id: str | None,
    trials: list[dict],
    exit_code: int = 0,
) -> Path:
    job_dir = runs / job_name
    _write_json(
        job_dir / "result.json",
        {"id": job_id, "n_total_trials": len(trials), "stats": {}, "finished_at": FINISHED_AT},
    )
    _write_json(job_dir / "config.json", {})
    _write_json(job_dir / "lock.json", {})
    metadata: dict = {
        "schema_version": 1,
        "command": ["harbor", "run"],
        "exit_code": exit_code,
        "finished_at": FINISHED_AT,
    }
    if spec_id is not None:
        metadata["experiment"] = {"spec_id": spec_id, "task": "tasks/demo"}
    _write_json(job_dir / "lab-metadata.json", metadata)
    for index, trial in enumerate(trials):
        trial_dir = job_dir / f"trial-{index}"
        _write_json(trial_dir / "result.json", trial)
        _write_json(trial_dir / "config.json", {})
        _write_json(trial_dir / "lock.json", {})
    return job_dir


def _make_root(
    tmp_path: Path,
    *,
    specs: list[tuple[str, str, str]] = (),
    reasons: list[tuple[str, str, str]] = (),
) -> Path:
    root = tmp_path / "lab"
    for spec_id, state, name in specs:
        agent = _spec_doc(spec_id)["agent"]
        _write_json(
            root / "queue" / state / f"{agent}-{spec_id}.json", _spec_doc(spec_id, name=name)
        )
    for spec_id, ulid, code in reasons:
        _write_json(
            root / "queue" / "reasons" / f"{spec_id}-{ulid}.json",
            {
                "schema_version": 1,
                "spec_id": spec_id,
                "occurred_at": FINISHED_AT,
                "code": code,
                "message": f"fixture refusal for {spec_id}",
            },
        )
    (root / "runs").mkdir(parents=True, exist_ok=True)
    return root


def test_select_by_spec_identity_excludes_unrelated_job(tmp_path: Path) -> None:
    root = _make_root(
        tmp_path,
        specs=[("specA01", "approved", "demo-exp"), ("specB02", "approved", "other-exp")],
        reasons=[("specA01", "01JAAAAAAAAAAAAAAAAAAAAAAAAA", "paid_run_needs_approval")],
    )
    _write_json(
        root / "queue/approved/codex-specB02.json",
        {**_spec_doc("specB02", name="other-exp"), "provider_routes": []},
    )
    _make_job(
        root / "runs",
        "job-a",
        job_id="job-a-id",
        spec_id="specA01",
        trials=[_trial_doc("trial-a", "trial-0", reward=0.75)],
    )
    _make_job(
        root / "runs",
        "job-other",
        job_id="job-other-id",
        spec_id="specB02",
        trials=[_trial_doc("trial-b", "trial-0", reward=0.25)],
    )

    report = inspect_experiment(root, experiment_id="specA01")

    assert report["kind"] == "experiment_evidence_view"
    assert report["selection"] == {"mode": "by_spec", "experiment_id": "specA01", "job_dir": None}
    assert [job["name"] for job in report["jobs"]] == ["job-a"]
    assert report["issues"] == []
    assert {row["spec_id"] for row in report["experiments"]} == {"specA01"}
    assert {row["spec_id"] for row in report["queue"]} == {"specA01"}
    by_job = inspect_experiment(root, job_dir=Path(report["jobs"][0]["path"]))
    assert {row["spec_id"] for row in by_job["queue"]} == {"specA01"}
    assert {row["spec_id"] for row in by_job["experiments"]} == {"specA01"}
    job = report["jobs"][0]
    assert job["origin"] == "harbor_native"
    assert job["experiment_id"] == "specA01"
    (trial,) = job["trials"]
    assert trial["trial_state"] == "observed"
    assert trial["reward"] == {"value": 0.75, "state": "observed"}
    assert trial["task"] == "tasks/demo"
    assert trial["links"]["result"].endswith("trial-0/result.json")
    queue_row = next(row for row in report["queue"] if row["spec_id"] == "specA01")
    assert queue_row["state"] == "approved"
    (receipt,) = queue_row["reasons"]
    assert receipt["code"] == "paid_run_needs_approval"
    assert receipt["path"].endswith("specA01-01JAAAAAAAAAAAAAAAAAAAAAAAAA.json")


def test_control_absence_is_not_applicable_but_model_gap_is_missing(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    job_dir = _make_job(
        root / "runs",
        "mixed-job",
        job_id="mixed-id",
        spec_id=None,
        trials=[
            _trial_doc("trial-oracle", "trial-0", agent_name="oracle", reward=None),
            _trial_doc(
                "trial-model",
                "trial-1",
                agent_name="codex",
                reward=None,
                usage={"n_input_tokens": 10},
            ),
        ],
    )

    report = inspect_experiment(root, job_dir=job_dir)

    (job,) = report["jobs"]
    assert job["experiment_id"] == "harbor_unbound"
    assert job["origin"] == "harbor_native"
    oracle, model = job["trials"]
    assert oracle["capture"]["state"] == "not_applicable"
    assert oracle["usage"]["state"] == "not_applicable"
    assert oracle["reward"]["value"] is None
    assert oracle["reward"]["state"] == "unavailable"
    assert model["capture"]["state"] == "missing"
    assert model["usage"]["state"] == "partial"
    assert model["reward"]["value"] is None
    assert model["exception"] == {"class": None, "phase": None}
    text = render_experiment_text(report)
    assert "not_applicable" in text and "missing" in text


def test_malformed_selected_job_stays_visible_with_reason(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    job_dir = root / "runs" / "broken-job"
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text("{not valid json", encoding="utf-8")
    _write_json(job_dir / "lab-metadata.json", {"schema_version": 1, "exit_code": 1})

    report = inspect_experiment(root, job_dir=job_dir)

    (job,) = report["jobs"]
    assert job["name"] == "broken-job"
    assert job["execution_status"] == "failed"
    assert job["capture_status"] == "unavailable"
    assert job["trials"] == []
    assert "load_issue" in job and job["load_issue"]
    assert report["issues"]
    assert "reward" not in job
    text = render_experiment_text(report)
    assert "unavailable" in text


def test_unbound_truncated_report_never_certifies_selected_job(tmp_path: Path) -> None:
    root = _make_root(tmp_path, specs=[("specA01", "done", "demo-exp")])
    job_dir = _make_job(
        root / "runs",
        "job-a",
        job_id="job-a-id",
        spec_id="specA01",
        trials=[_trial_doc("trial-a", "trial-0", agent_name="codex", reward=None)],
    )
    coverage = {
        "native_jobs_present": {"count": 1, "jobs": ["job-a"], "truncated": True},
        "catalogued": {"count": 0, "jobs": [], "truncated": False},
        "projected": {"count": 0, "jobs": [], "truncated": False},
        "excepted": {"count": 0, "jobs": [], "truncated": False},
        "failed": {"count": 0, "jobs": [], "truncated": False},
        "trajectory_availability_by_agent": {"codex": "unknown"},
        "reasons": ["fixture reason"],
        "repair_path": "uv run evallab repair --dry-run",
    }
    coverage_path = root / "coverage.json"
    _write_json(coverage_path, coverage)

    report = inspect_experiment(root, job_dir=job_dir, coverage_report_path=coverage_path)

    capture = report["capture_report"]
    assert capture["status"] == "supplied_unbound_scope"
    assert capture["scope"] == "unbound"
    assert capture["summary"]["sections"]["native_jobs_present"]["truncated"] is True
    (job,) = report["jobs"]
    (trial,) = job["trials"]
    assert trial["capture"]["state"] == "missing"
    assert trial["reward"]["state"] == "unavailable"
    assert "coverage" not in job
    assert any("unbound-scope" in notice for notice in report["notices"])
    assert any("truncated" in notice for notice in report["notices"])
    text = render_experiment_text(report)
    assert "supplied_unbound_scope" in text

    bare = inspect_experiment(root, job_dir=job_dir)
    assert bare["capture_report"]["status"] == "unavailable"
    assert bare["capture_report"]["data"] is None


def test_inventory_lists_without_loading_and_writes_nothing(tmp_path: Path) -> None:
    root = _make_root(tmp_path, specs=[("specA01", "approved", "demo-exp")])
    _make_job(
        root / "runs",
        "job-a",
        job_id="job-a-id",
        spec_id="specA01",
        trials=[_trial_doc("trial-a", "trial-0", reward=0.5)],
    )
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

    report = inspect_experiment(root)

    assert report["selection"]["mode"] == "inventory"
    assert [job["name"] for job in report["jobs"]] == ["job-a"]
    assert report["jobs"][0]["trials"] == []
    assert report["jobs"][0]["trial_count"] == 1
    assert report["experiments"][0]["spec_id"] == "specA01"
    after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    assert before == after
    try:
        inspect_experiment(root, experiment_id="specA01", job_dir=root / "runs" / "job-a")
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("both selectors must be rejected")


def test_invalid_recorded_control_trace_is_not_expected_absence(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    usage = {"n_input_tokens": 10, "n_output_tokens": 2, "cost_usd": 0.1}
    job = _make_job(
        root / "runs",
        "invalid-traces",
        job_id="job-invalid",
        spec_id=None,
        trials=[
            _trial_doc("control", "trial-0", agent_name="oracle", usage=usage),
            _trial_doc("model", "trial-1", agent_name="codex", usage=usage),
        ],
    )
    for name in ("trial-0", "trial-1"):
        _write_json(job / name / "agent/trajectory.json", {"invalid": "ATIF"})
    rows = inspect_experiment(root, job_dir=job)["jobs"][0]["trials"]
    for trial in rows:
        assert trial["invalid_trajectory_count"] > 0
        assert trial["capture"]["state"] == "partial"
        assert trial["usage"]["state"] == "complete"


def test_spec_selection_retains_failed_attempt_without_result(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    job = root / "runs" / "unfinished"
    _write_json(
        job / "lab-metadata.json", {"experiment": {"spec_id": "failed-spec"}, "exit_code": 2}
    )
    (row,) = inspect_experiment(root, experiment_id="failed-spec")["jobs"]
    assert row["origin"] == "harbor_native"
    assert row["execution_status"] == "failed"
    assert row["capture_status"] == "unavailable"
    assert row["trials"] == []


@pytest.fixture
def scoped_coverage_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict]:
    root = _make_root(tmp_path, specs=[("specA01", "done", "demo-exp")])
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(root / "derived/parquet"))
    job_id = "11111111-1111-4111-8111-111111111111"
    _make_job(
        root / "runs",
        "r2-job",
        job_id=job_id,
        spec_id="specA01",
        trials=[_trial_doc("trial-r2", "trial-0", agent_name="oracle", reward=1.0)],
    )
    payload = {
        "schema_version": 1,
        "producer": "evallab.coverage_report.write_scope_bound_product",
        "source_root": str(root),
        "derived_root": str(root / "derived/parquet"),
        "job_ids": [job_id],
        "coverage": {
            **{
                name: {"count": 1, "jobs": ["r2-job"], "truncated": False}
                for name in ("catalogued", "projected", "native_jobs_present")
            },
            **{
                name: {"count": 0, "jobs": [], "truncated": False}
                for name in ("excepted", "failed")
            },
            "reasons": {},
            "repair_path": [],
        },
        "external_links": [
            {
                "kind": "lab-spec-binding",
                "job_id": job_id,
                "job_name": "r2-job",
                "status": "evidence-intact-not-cataloged-not-projected",
            }
        ],
    }
    path = _write_json(root / "coverage.json", payload)
    return root, path, payload


def test_scoped_coverage_preserves_conflicting_annotation(
    scoped_coverage_case: tuple[Path, Path, dict],
) -> None:
    root, path, _ = scoped_coverage_case
    report = inspect_experiment(root, experiment_id="specA01", coverage_report_path=path)
    capture = report["capture_report"]
    assert capture["scope"] == "bound"
    assert capture["summary"]["sections"]["projected"]["count"] == 1
    assert capture["external_links"][0]["status"] == "evidence-intact-not-cataloged-not-projected"
    assert "evidence-intact-not-cataloged-not-projected" in render_experiment_text(report)


def test_matching_run_name_cannot_replace_uuid_membership(
    scoped_coverage_case: tuple[Path, Path, dict],
) -> None:
    root, path, payload = scoped_coverage_case
    target = payload["job_ids"][0]
    neighbor = "22222222-2222-4222-8222-222222222222"
    payload["job_ids"] = [neighbor]  # Displayed name still matches the selected job.
    _write_json(path, payload)
    rejected = inspect_experiment(root, experiment_id="specA01", coverage_report_path=path)
    assert rejected["capture_report"]["status"] == "unbound_missing_uuid"
    payload["job_ids"] = [target, neighbor]
    _write_json(path, payload)
    cohort = inspect_experiment(root, experiment_id="specA01", coverage_report_path=path)
    assert cohort["capture_report"]["scope"] == "cohort"


@pytest.mark.parametrize("field", ["source_root", "derived_root"])
def test_scope_requires_exact_source_and_configured_derived_root(
    scoped_coverage_case: tuple[Path, Path, dict],
    field: str,
) -> None:
    root, path, payload = scoped_coverage_case
    payload[field] = str(root / "different-root")  # A descendant is not the configured store.
    _write_json(path, payload)
    report = inspect_experiment(root, experiment_id="specA01", coverage_report_path=path)
    assert report["capture_report"]["status"] == "unbound_wrong_root"


@pytest.mark.parametrize("change", [{"schema_version": 99}, {"job_ids": [42]}])
def test_unsupported_scope_never_binds(
    scoped_coverage_case: tuple[Path, Path, dict],
    change: dict,
) -> None:
    root, path, payload = scoped_coverage_case
    payload.update(change)
    _write_json(path, payload)
    report = inspect_experiment(root, experiment_id="specA01", coverage_report_path=path)
    assert report["capture_report"]["status"] == "unbound_invalid_scope"
