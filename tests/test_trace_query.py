"""Behavioral boundary tests for evallab.trace_query (HAR-131).

Tests:
    - Deduplication of duplicate published/source trial identities.
    - Missingness transparency: missing ATIF, processed, counts visible as coverage.
    - Trial vs step grain separation (no denominator multiplication).
    - Label freeze integrity and empty list representation for unlabeled trials.
    - Execution of all 10 canonical trace queries.
    - CLI db attach trace mode flags.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.trace_query import connect_trace_query


def _create_minimal_trial(
    job_dir: Path,
    trial_name: str,
    *,
    job_id: str = "job-uuid-1",
    trial_id: str = "trial-uuid-1",
    task_name: str = "format-code-task-001",
    reward: float | None = 1.0,
    with_trajectory: bool = True,
    with_processed: bool = True,
    with_counts: bool = True,
    counts_verdict: str = "counted_pass",
    n_steps: int = 3,
) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True, exist_ok=True)

    # Job result.json
    job_result = {
        "id": job_id,
        "n_total_trials": 1,
        "finished_at": "2026-10-01T00:00:00Z",
        "stats": {"n_completed_trials": 1},
    }
    (job_dir / "result.json").write_text(json.dumps(job_result), encoding="utf-8")

    # Provenance
    prov = {
        "card": "HAR-116",
        "job_name": job_dir.name,
        "source_path": str(job_dir),
        "model": "test-model",
    }
    (job_dir / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")

    # Spec
    spec = {
        "hypothesis": "arm=original: test hypothesis",
        "model": "test-model",
        "task_package_digest": "sha256:pkgdigest123",
    }
    (job_dir / "experiment-spec.json").write_text(json.dumps(spec), encoding="utf-8")

    # Trial result.json
    trial_result = {
        "id": trial_id,
        "job_id": job_id,
        "trial_name": trial_name,
        "task_name": task_name,
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else {},
        "agent_result": {"n_input_tokens": 1000, "n_output_tokens": 100},
    }
    (trial_dir / "result.json").write_text(json.dumps(trial_result), encoding="utf-8")

    # Trajectory
    if with_trajectory:
        agent_dir = trial_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        steps = []
        for i in range(1, n_steps + 1):
            steps.append(
                {
                    "step_id": i,
                    "source": "agent",
                    "timestamp": f"2026-10-01T00:0{i}:00Z",
                    "is_copied_context": False,
                    "metrics": {"prompt_tokens": 100 * i, "completion_tokens": 10 * i},
                    "tool_calls": [
                        {
                            "function_name": "bash_command",
                            "arguments": {"keystrokes": f"echo step {i} > /tmp/out"},
                        }
                    ],
                    "observation": {"results": [{"content": f"output from step {i}"}]},
                }
            )
        (agent_dir / "trajectory.json").write_text(
            json.dumps({"steps": steps}), encoding="utf-8"
        )

    # Processed
    if with_processed:
        proc_dir = job_dir / "processed"
        proc_dir.mkdir(parents=True, exist_ok=True)
        proc_data = {
            "trial_name": trial_name,
            "task_name": task_name,
            "reward": reward,
            "scored": reward is not None,
            "stop_reason": "completed" if reward is not None else "agent_timeout",
            "agent_steps": n_steps,
            "counts": (
                {"verdict": counts_verdict, "reasons": [], "evidence": []}
                if with_counts
                else None
            ),
            "tokens_proxy": {
                "source": "proxy_settled_ledger",
                "attribution": "single_trial",
                "input_tokens": 2500,
                "output_tokens": 150,
            },
            "decision": {
                "schema": "trial_decision/v2",
                "judgments": {"loop_kind": {"kind": "none"}},
            },
            "token_flow": {
                "last_useful_edit": {"step_id": 1},
                "tokens_after_last_edit": {"input_tokens": 200, "output_tokens": 20},
            },
        }
        (proc_dir / f"trial-{trial_name}.json").write_text(
            json.dumps(proc_data), encoding="utf-8"
        )

    return trial_dir


def test_duplicate_identities_deduplicated(tmp_path: Path) -> None:
    """Duplicate published/source identities deduplicate to exactly one canonical row."""
    job1 = tmp_path / "job1"
    job2 = tmp_path / "job2"

    # Same native job_id and trial_id in both jobs
    # job1 has processed data; job2 does not
    _create_minimal_trial(
        job1,
        "trial_alpha",
        job_id="uuid-job-shared",
        trial_id="uuid-trial-shared",
        with_processed=True,
    )
    _create_minimal_trial(
        job2,
        "trial_alpha_copy",
        job_id="uuid-job-shared",
        trial_id="uuid-trial-shared",
        with_processed=False,
    )

    con, coverage = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job1, job2],
    )
    try:
        rows = con.execute(
            "SELECT job_id, trial_id, processed_available FROM v_trace_trials"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "uuid-job-shared"
        assert rows[0][1] == "uuid-trial-shared"
        assert rows[0][2] is True  # The processed-available version was preserved
        assert coverage["discovered_trials"] == 1
    finally:
        con.close()


def test_missing_inputs_visible_as_coverage(tmp_path: Path) -> None:
    """Missing ATIF, processed, and counts remain visible without dropping the trial."""
    job = tmp_path / "job_sparse"
    _create_minimal_trial(
        job,
        "trial_sparse",
        job_id="uuid-sparse-job",
        trial_id="uuid-sparse-trial",
        reward=None,
        with_trajectory=False,
        with_processed=False,
        with_counts=False,
    )

    con, coverage = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        # Trial is NOT dropped
        t_row = con.execute("SELECT * FROM v_trace_trials").fetchone()
        assert t_row is not None

        cols = [d[0] for d in con.execute("DESCRIBE v_trace_trials").fetchall()]
        row_dict = dict(zip(cols, t_row, strict=True))

        assert row_dict["trajectory_available"] is False
        assert row_dict["processed_available"] is False
        assert row_dict["counts_available"] is False
        assert row_dict["step_evidence_source"] == "none"
        assert row_dict["counts_verdict"] is None
        assert row_dict["counts_reasons_json"] == "[]"

        # Missing trajectories cannot fabricate events in step-level aggregates.
        assert con.execute(
            "SELECT COUNT(*) FROM v_trace_steps WHERE trial_id = 'uuid-sparse-trial'"
        ).fetchone() == (0,)

        # Coverage dict reflects explicit missingness
        assert coverage["missing_atif"] == 1
        assert coverage["missing_processed"] == 1
        assert coverage["missing_counts"] == 1
    finally:
        con.close()


def test_stale_processed_reward_cannot_promote_an_unscored_native_trial(tmp_path: Path) -> None:
    job = tmp_path / "job_stale"
    _create_minimal_trial(job, "trial_stale", reward=None)
    report = job / "processed/trial-trial_stale.json"
    stale = json.loads(report.read_text())
    stale["reward"] = 1.0
    stale["scored"] = True
    report.write_text(json.dumps(stale))
    con, _ = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        assert con.execute("SELECT raw_reward, scored FROM v_trace_trials").fetchone() == (
            None, False
        )
    finally:
        con.close()


def test_trial_vs_step_grain_isolation(tmp_path: Path) -> None:
    """Trial aggregations are not corrupted by joined steps."""
    job = tmp_path / "job_multistep"
    _create_minimal_trial(
        job,
        "trial_multistep",
        job_id="uuid-multi-job",
        trial_id="uuid-multi-trial",
        reward=1.0,
        n_steps=5,
    )

    con, _ = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        trial_count = con.execute("SELECT COUNT(*) FROM v_trace_trials").fetchone()[0]
        step_count = con.execute("SELECT COUNT(*) FROM v_trace_steps").fetchone()[0]
        assert trial_count == 1
        assert step_count == 5

        # Joined fields on v_trace_steps
        step_row = con.execute(
            "SELECT trial_name, raw_reward, step_id, command_text, step_ref FROM v_trace_steps WHERE step_id = 1"
        ).fetchone()
        assert step_row[0] == "trial_multistep"
        assert step_row[1] == 1.0
        assert step_row[2] == 1
        assert "echo step 1" in step_row[3]
        assert step_row[4] == "head#1"
    finally:
        con.close()


def test_unlabeled_is_empty_list_not_clean(tmp_path: Path) -> None:
    """Unlabeled trials carry labels_json='[]', never a fake empty-clean assumption."""
    job = tmp_path / "job_unlabeled"
    _create_minimal_trial(job, "trial_unlabeled")

    con, _ = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        labels_json = con.execute(
            "SELECT labels_json FROM v_trace_trials"
        ).fetchone()[0]
        assert labels_json == "[]"
    finally:
        con.close()


@pytest.mark.parametrize("duplicate", [False, True])
def test_frozen_pass_gate_is_separate_from_counts_and_loop_truth(
    tmp_path: Path, duplicate: bool
) -> None:
    job = tmp_path / "job_pass"
    _create_minimal_trial(job, "trial_pass")
    gate = tmp_path / "research/explorations/trace-lab/har128/sft_gate"
    gate.mkdir(parents=True)
    row = {"trial": "trial_pass", "clean": False, "genuine": True, "cut_step_id": None}
    body = (json.dumps(row) + "\n") * (2 if duplicate else 1)
    (gate / "labels.jsonl").write_text(body)
    (gate / "labels.sha256").write_text(
        hashlib.sha256(body.encode()).hexdigest() + "  labels.jsonl\n"
    )
    if duplicate:
        with pytest.raises(ValueError, match="duplicate"):
            connect_trace_query(repo_root=tmp_path, job_dirs=[job])
        return
    con, _ = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        verdict, labels = con.execute(
            "SELECT counts_verdict, labels_json FROM v_trace_trials"
        ).fetchone()
        annotation = json.loads(labels)[0]
        assert verdict == "counted_pass"
        assert annotation["label"]["clean"] is False
        assert annotation["label"]["genuine"] is True
        assert annotation["rater"] is None
        assert "loop_kind" not in annotation
    finally:
        con.close()


@pytest.mark.parametrize(
    ("source", "attribution", "expected"),
    [
        ("proxy_settled_ledger", "single_trial", (2500, 150)),
        ("proxy_settled_ledger", "unavailable", (None, None)),
        ("native_harbor", "single_trial", (None, None)),
        (None, None, (None, None)),
    ],
)
def test_proxy_tokens_require_validated_trial_attribution(
    tmp_path: Path, source: str | None, attribution: str | None, expected: tuple
) -> None:
    job = tmp_path / "job_tokens"
    _create_minimal_trial(job, "trial_tokens")
    report = job / "processed" / "trial-trial_tokens.json"
    payload = json.loads(report.read_text())
    payload["tokens_proxy"]["source"] = source
    payload["tokens_proxy"]["attribution"] = attribution
    report.write_text(json.dumps(payload))
    # Tempting job-level totals are neither validated nor trial-attributed.
    (job / "processed" / "job.json").write_text(json.dumps(
        {"ledger": {"totals": {"used": {"input_tokens": 999900, "output_tokens": 990}}}}
    ))
    (job / "lab-metadata.json").write_text(json.dumps(
        {"provider_usage": {"totals": {"input_tokens": 777700, "output_tokens": 770}}}
    ))
    con, _ = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        actual = con.execute("SELECT input_tokens, output_tokens FROM v_trace_trials").fetchone()
        assert actual == expected
    finally:
        con.close()


def test_cli_attach_trace_mode(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI evallab db attach supports --trace and --trace-job-dir."""
    job = tmp_path / "job_cli"
    _create_minimal_trial(job, "trial_cli", reward=1.0, n_steps=2)

    code_zones = run_cli(
        ["db", "attach", "--trace-job-dir", str(job), "--zones"],
        workspace=tmp_path,
    )
    assert code_zones == 0

    code_query = run_cli(
        [
            "db",
            "attach",
            "--trace-job-dir",
            str(job),
            "--query",
            "SELECT trial_name, raw_reward FROM v_trace_trials",
        ],
        workspace=tmp_path,
    )
    assert code_query == 0
    output = capsys.readouterr().out
    assert "trial_cli" in output
    assert "1.0" in output


def test_manifest_prefix_matching(tmp_path: Path) -> None:
    """CARD-prefixed published dirs resolve arm/split from bare manifest job names."""
    manifest_dir = tmp_path / "research" / "experiments" / "har110-python-gepa"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "results-v2-trials.jsonl").write_text(
        json.dumps({"job": "bare-job", "arm": "plain", "split": "heldout", "task": "0001"}) + "\n",
        encoding="utf-8",
    )
    job = tmp_path / "HAR-99-bare-job"
    _create_minimal_trial(job, "trial_prefixed")

    con, coverage = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        row = con.execute("SELECT arm, split FROM v_trace_trials").fetchone()
        assert row[0] == "plain"
        assert row[1] == "heldout"
        assert coverage["by_arm"].get("plain") == 1
    finally:
        con.close()


def test_continuation_stitch_dedup(tmp_path: Path) -> None:
    """Restated continuation prefixes count once; copied replays stay flagged."""
    job = tmp_path / "job_stitch"
    trial_dir = _create_minimal_trial(
        job, "trial_stitch", reward=0.0, n_steps=3, with_processed=False
    )
    agent_dir = trial_dir / "agent"
    head_steps = json.loads((agent_dir / "trajectory.json").read_text(encoding="utf-8"))["steps"]
    cont_steps = [dict(step) for step in head_steps]
    cont_steps.append(
        {
            "step_id": 4,
            "source": "agent",
            "timestamp": "2026-10-01T00:04:00Z",
            "tool_calls": [
                {"function_name": "bash_command", "arguments": {"keystrokes": "echo new"}}
            ],
            "observation": {"results": [{"content": "new output"}]},
        }
    )
    (agent_dir / "trajectory.cont-1.json").write_text(
        json.dumps({"steps": cont_steps}), encoding="utf-8"
    )

    con, coverage = connect_trace_query(
        repo_root=tmp_path,
        job_dirs=[job],
    )
    try:
        step_count = con.execute(
            "SELECT COUNT(*) FROM v_trace_steps WHERE step_id >= 0"
        ).fetchone()[0]
        assert step_count == 4
        assert coverage["stitched_duplicated_steps"] == 3
    finally:
        con.close()


@pytest.mark.parametrize("missing", [False, True])
def test_frozen_label_drift_aborts_instead_of_changing_denominator(
    tmp_path: Path, missing: bool
) -> None:
    labels_base = tmp_path / "research" / "explorations" / "trace-lab" / "har119" / "labels"
    rater_a = labels_base / "rater_a"
    rater_a.mkdir(parents=True)
    label_file = rater_a / "trial_drift.json"
    original = json.dumps({"loop_kind": "repetition"})
    label_file.write_text(original, encoding="utf-8")
    digest = hashlib.sha256(original.encode()).hexdigest()
    (labels_base / "MANIFEST.sha256").write_text(
        f"{digest}  rater_a/trial_drift.json\n", encoding="utf-8"
    )
    if missing:
        label_file.unlink()
    else:
        label_file.write_text(json.dumps({"loop_kind": "none"}), encoding="utf-8")
    job = tmp_path / "job_drift"
    _create_minimal_trial(job, "trial_drift")
    with pytest.raises(ValueError):
        connect_trace_query(repo_root=tmp_path, job_dirs=[job])


@pytest.mark.parametrize("source", ["system", "user", "agent"])
def test_prompt_examples_are_not_agent_proposals(source: str) -> None:
    from evallab.trace_query import _extract_commands_from_step

    message = json.dumps({"commands": [{"keystrokes": "ls -la; cd project\n"}]})
    command, provenance, recorded_calls = _extract_commands_from_step(
        {"source": source, "message": message}
    )
    if source == "agent":
        assert command == "ls -la; cd project"
        assert provenance == "reconstructed"
    else:
        assert command is None
        assert provenance is None
    assert recorded_calls == 0


@pytest.mark.parametrize("state", ["current", "stale", "conflicting"])
def test_first_edit_feature_requires_unambiguous_current_source(tmp_path: Path, state: str) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    job = tmp_path / "job_features"
    trial = _create_minimal_trial(job, "trial_features")
    digest = hashlib.sha256((trial / "agent/trajectory.json").read_bytes()).hexdigest()
    feature = {
        "job_id": "job-uuid-1", "trial_id": "trial-uuid-1",
        "source_sha256": "0" * 64 if state == "stale" else digest,
        "status": "featured", "unavailable_reason": None,
        "agent_step_count": 3, "step_to_first_edit": 1,
    }
    features = [feature]
    if state == "conflicting":
        features.append({**feature, "step_to_first_edit": 2})
    derived = tmp_path / "derived"
    partition = derived / "job_id=job-uuid-1/trial_id=trial-uuid-1"
    partition.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(features), partition / "traj_features.parquet")
    con, _ = connect_trace_query(repo_root=tmp_path, derived_root=derived, job_dirs=[job])
    try:
        measured, first_edit = con.execute(
            "SELECT first_edit_measure_available, first_edit_step FROM v_trace_trials"
        ).fetchone()
        assert (measured, first_edit) == ((True, 1) if state == "current" else (False, None))
    finally:
        con.close()


def test_first_edit_reader_accepts_explicit_job_outside_data_checkout(tmp_path: Path) -> None:
    data_root = tmp_path / "data-checkout"
    data_root.mkdir()
    job = tmp_path / "published-results" / "job"
    trial = _create_minimal_trial(job, "trial_external")
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text())
    result["config"] = {"job_id": "job-uuid-1"}
    result_path.write_text(json.dumps(result))
    trajectory_path = trial / "agent/trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"][0]["tool_calls"] = [
        {"function_name": "write_file", "arguments": {"path": "/testbed/f.py", "content": "x = 1"}}
    ]
    trajectory_path.write_text(json.dumps(trajectory))
    con, _ = connect_trace_query(repo_root=data_root, job_dirs=[job])
    try:
        measured, step = con.execute(
            "SELECT first_edit_measure_available, first_edit_step FROM v_trace_trials"
        ).fetchone()
        assert measured is True
        assert step == 1
    finally:
        con.close()


def _write_rater_freeze(repo_root: Path, rel_dir: str, files: dict) -> Path:
    """Write rater_a/rater_b label files plus a matching MANIFEST.sha256."""
    base = repo_root / rel_dir
    lines = []
    for rel, payload in sorted(files.items()):
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload)
        target.write_text(body, encoding="utf-8")
        lines.append(f"{hashlib.sha256(body.encode()).hexdigest()}  {rel}\n")
    (base / "MANIFEST.sha256").write_text("".join(lines), encoding="utf-8")
    return base


def test_har128_har116_cohort_loads_distinct_from_har119(tmp_path: Path) -> None:
    """Same native trial in both rater cohorts yields four entries, never pooled."""
    trial = "trial_shared"
    job = tmp_path / "job_shared"
    _create_minimal_trial(job, trial)
    _write_rater_freeze(
        tmp_path,
        "research/explorations/trace-lab/har119/labels",
        {
            f"{rater}/{trial}.json": {"loop_kind": "none", "trial": trial}
            for rater in ("rater_a", "rater_b")
        },
    )
    _write_rater_freeze(
        tmp_path,
        "research/explorations/trace-lab/har128/labels_har116",
        {
            f"{rater}/{trial}.json": {"loop_kind": "repetition", "trial": trial}
            for rater in ("rater_a", "rater_b")
        },
    )
    con, coverage = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        (labels_json,) = con.execute("SELECT labels_json FROM v_trace_trials").fetchone()
        entries = json.loads(labels_json)
        assert len(entries) == 4
        by_cohort: dict = {}
        for entry in entries:
            by_cohort.setdefault(entry["cohort"], []).append(entry)
        assert sorted(by_cohort) == ["har119", "har128-har116"]
        for cohort_entries in by_cohort.values():
            assert sorted(e["rater"] for e in cohort_entries) == ["rater_a", "rater_b"]
            assert {e["provenance"] for e in cohort_entries} == {"agent_rater"}
            assert all(e["trial_name"] == trial for e in cohort_entries)
            assert all(isinstance(e["loop_kind"], str) for e in cohort_entries)
        assert [e["loop_kind"] for e in by_cohort["har128-har116"]] == [
            "repetition",
            "repetition",
        ]
        report = coverage["label_verification"]["har128-har116"]
        assert report["verified"] == 2
        assert report["failed"] == []
        assert coverage["label_manifest_hashes"]["har128-har116"]
    finally:
        con.close()


@pytest.mark.parametrize("case", ["missing", "corrupt", "duplicate", "empty"])
def test_har128_har116_declared_file_boundaries(tmp_path: Path, case: str) -> None:
    """A declared-but-missing, drifted, double-declared, or empty freeze aborts."""
    base = tmp_path / "research/explorations/trace-lab/har128/labels_har116"
    rater_a = base / "rater_a"
    rater_a.mkdir(parents=True)
    good_body = json.dumps({"trial": "trial_ok", "loop_kind": "none"})
    (rater_a / "trial_ok.json").write_text(good_body, encoding="utf-8")
    member = f"{hashlib.sha256(good_body.encode()).hexdigest()}  rater_a/trial_ok.json\n"
    if case == "missing":
        manifest_body = member + "0" * 64 + "  rater_a/trial_gone.json\n"
    elif case == "corrupt":
        manifest_body = member
        (rater_a / "trial_ok.json").write_text(
            json.dumps({"loop_kind": "repetition"}), encoding="utf-8"
        )
    elif case == "duplicate":
        manifest_body = member + member
    else:
        manifest_body = ""
    (base / "MANIFEST.sha256").write_text(manifest_body, encoding="utf-8")
    job = tmp_path / "job_bounds"
    _create_minimal_trial(job, "trial_ok")
    with pytest.raises(ValueError):
        connect_trace_query(repo_root=tmp_path, job_dirs=[job])


def test_har128_labels_cannot_be_reassigned_by_filename(tmp_path: Path) -> None:
    job = tmp_path / "job_identity"
    _create_minimal_trial(job, "trial_target")
    _write_rater_freeze(
        tmp_path,
        "research/explorations/trace-lab/har128/labels_har116",
        {
            f"{rater}/trial_target.json": {"trial": "trial_other", "loop_kind": "none"}
            for rater in ("rater_a", "rater_b")
        },
    )
    with pytest.raises(ValueError):
        connect_trace_query(repo_root=tmp_path, job_dirs=[job])


def test_har128_har116_absent_cohort_is_unavailable_not_empty_freeze(
    tmp_path: Path,
) -> None:
    """Without the freeze dir, the cohort reports missing, never verified-empty."""
    job = tmp_path / "job_absent"
    _create_minimal_trial(job, "trial_absent")
    con, coverage = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        (labels_json,) = con.execute("SELECT labels_json FROM v_trace_trials").fetchone()
        assert json.loads(labels_json) == []
        report = coverage["label_verification"]["har128-har116"]
        assert report["manifest"] == "missing"
        assert report["verified"] == 0
        assert "har128-har116" not in coverage["label_manifest_hashes"]
    finally:
        con.close()

def test_har128_g2_a1_cohort_loads_distinct(tmp_path: Path) -> None:
    """G2 attempt-one freeze joins under its own cohort, never pooled."""
    trial = "trial_g2_shared"
    job = tmp_path / "job_g2_shared"
    _create_minimal_trial(job, trial)
    _write_rater_freeze(
        tmp_path,
        "research/explorations/trace-lab/har128/labels_g2_a1",
        {
            f"{rater}/{trial}.json": {"trial": trial, "loop_kind": "none"}
            for rater in ("rater_a", "rater_b")
        },
    )
    con, coverage = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        (labels_json,) = con.execute("SELECT labels_json FROM v_trace_trials").fetchone()
        entries = json.loads(labels_json)
        assert len(entries) == 2
        assert {e["cohort"] for e in entries} == {"har128-g2-a1"}
        assert sorted(e["rater"] for e in entries) == ["rater_a", "rater_b"]
        assert {e["provenance"] for e in entries} == {"agent_rater"}
        assert all(e["trial_name"] == trial for e in entries)
        assert all(e["loop_kind"] == "none" for e in entries)
        report = coverage["label_verification"]["har128-g2-a1"]
        assert report["verified"] == 2
        assert report["failed"] == []
        assert coverage["label_manifest_hashes"]["har128-g2-a1"]
    finally:
        con.close()


@pytest.mark.parametrize("case", ["missing", "corrupt", "duplicate", "empty"])
def test_har128_g2_a1_declared_file_boundaries(tmp_path: Path, case: str) -> None:
    """A declared-but-missing, drifted, double-declared, or empty G2 freeze aborts."""
    base = tmp_path / "research/explorations/trace-lab/har128/labels_g2_a1"
    rater_a = base / "rater_a"
    rater_a.mkdir(parents=True)
    good_body = json.dumps({"trial": "trial_g2_ok", "loop_kind": "none"})
    (rater_a / "trial_g2_ok.json").write_text(good_body, encoding="utf-8")
    member = f"{hashlib.sha256(good_body.encode()).hexdigest()}  rater_a/trial_g2_ok.json\n"
    if case == "missing":
        manifest_body = member + "0" * 64 + "  rater_a/trial_g2_gone.json\n"
    elif case == "corrupt":
        manifest_body = member
        (rater_a / "trial_g2_ok.json").write_text(
            json.dumps({"loop_kind": "repetition"}), encoding="utf-8"
        )
    elif case == "duplicate":
        manifest_body = member + member
    else:
        manifest_body = ""
    (base / "MANIFEST.sha256").write_text(manifest_body, encoding="utf-8")
    job = tmp_path / "job_g2_bounds"
    _create_minimal_trial(job, "trial_g2_ok")
    with pytest.raises(ValueError):
        connect_trace_query(repo_root=tmp_path, job_dirs=[job])


def test_har128_g2_a1_labels_cannot_be_reassigned_by_filename(tmp_path: Path) -> None:
    job = tmp_path / "job_g2_identity"
    _create_minimal_trial(job, "trial_g2_target")
    _write_rater_freeze(
        tmp_path,
        "research/explorations/trace-lab/har128/labels_g2_a1",
        {
            f"{rater}/trial_g2_target.json": {"trial": "trial_g2_other", "loop_kind": "none"}
            for rater in ("rater_a", "rater_b")
        },
    )
    with pytest.raises(ValueError):
        connect_trace_query(repo_root=tmp_path, job_dirs=[job])


def test_har128_g2_a1_absent_cohort_is_unavailable_not_empty_freeze(
    tmp_path: Path,
) -> None:
    """Without the G2 freeze dir, the cohort reports missing, never verified-empty."""
    job = tmp_path / "job_g2_absent"
    _create_minimal_trial(job, "trial_g2_absent")
    con, coverage = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        (labels_json,) = con.execute("SELECT labels_json FROM v_trace_trials").fetchone()
        assert json.loads(labels_json) == []
        report = coverage["label_verification"]["har128-g2-a1"]
        assert report["manifest"] == "missing"
        assert report["verified"] == 0
        assert "har128-g2-a1" not in coverage["label_manifest_hashes"]
    finally:
        con.close()


def _write_gate_batch(gate: Path, name: str, rows: list[dict]) -> str:
    """Write one gate JSONL batch; return its sha256 hex digest."""
    body = "".join(json.dumps(row) + "\n" for row in rows)
    (gate / name).write_text(body, encoding="utf-8")
    return hashlib.sha256(body.encode()).hexdigest()


def test_sft_gate_two_declared_batches_both_retained_distinguishable(tmp_path: Path) -> None:
    """Two declared gate batches on the same trial both append with batch identity."""
    job = tmp_path / "job_gate_two"
    _create_minimal_trial(job, "trial_gate_shared")
    gate = tmp_path / "research/explorations/trace-lab/har128/sft_gate"
    gate.mkdir(parents=True)
    row_a = {"trial": "trial_gate_shared", "clean": True, "cut_step_id": 3}
    row_b = {"trial": "trial_gate_shared", "clean": False, "cut_step_id": None}
    digest_a = _write_gate_batch(gate, "labels.jsonl", [row_a])
    digest_b = _write_gate_batch(gate, "labels_second.jsonl", [row_b])
    (gate / "labels.sha256").write_text(
        f"{digest_a}  labels.jsonl\n{digest_b}  labels_second.jsonl\n", encoding="utf-8"
    )
    con, coverage = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        (labels_json,) = con.execute("SELECT labels_json FROM v_trace_trials").fetchone()
        entries = json.loads(labels_json)
        assert len(entries) == 2
        assert {e["cohort"] for e in entries} == {"har128-sft-pass"}
        assert {e["label_scope"] for e in entries} == {"sft_pass_cleanliness"}
        assert sorted(e["batch"] for e in entries) == ["labels", "labels_second"]
        assert sorted(e["source_file"] for e in entries) != [entries[0]["source_file"]] * 2
        assert {e["source_sha256"] for e in entries} == {digest_a, digest_b}
        assert "loop_kind" not in entries[0]
        report = coverage["label_verification"]["har128-sft-pass"]
        assert report["verified"] == 2
        assert report["rows"] == 2
        assert sorted(report["batches"]) == ["labels", "labels_second"]
        assert report["batches"]["labels"]["rows"] == 1
        assert report["batches"]["labels_second"]["rows"] == 1
    finally:
        con.close()


def test_sft_gate_within_batch_duplicate_refused(tmp_path: Path) -> None:
    """Duplicate trial identity within one declared batch aborts, even with two batches."""
    job = tmp_path / "job_gate_dup"
    _create_minimal_trial(job, "trial_gate_dup")
    gate = tmp_path / "research/explorations/trace-lab/har128/sft_gate"
    gate.mkdir(parents=True)
    dup_row = {"trial": "trial_gate_dup", "clean": True, "cut_step_id": 1}
    digest_a = _write_gate_batch(gate, "labels.jsonl", [dup_row, dup_row])
    digest_b = _write_gate_batch(gate, "labels_second.jsonl", [dup_row])
    (gate / "labels.sha256").write_text(
        f"{digest_a}  labels.jsonl\n{digest_b}  labels_second.jsonl\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate"):
        connect_trace_query(repo_root=tmp_path, job_dirs=[job])


def test_sft_gate_undeclared_batch_remains_unavailable(tmp_path: Path) -> None:
    """A present-but-undeclared gate JSONL file is not loaded, but visibly reported."""
    job = tmp_path / "job_gate_undeclared"
    _create_minimal_trial(job, "trial_gate_declared")
    gate = tmp_path / "research/explorations/trace-lab/har128/sft_gate"
    gate.mkdir(parents=True)
    declared_row = {"trial": "trial_gate_declared", "clean": True, "cut_step_id": 5}
    digest = _write_gate_batch(gate, "labels.jsonl", [declared_row])
    (gate / "labels.sha256").write_text(f"{digest}  labels.jsonl\n", encoding="utf-8")
    undeclared_row = {"trial": "trial_gate_undeclared", "clean": True, "cut_step_id": 7}
    _write_gate_batch(gate, "labels_g2_a1.jsonl", [undeclared_row])
    con, coverage = connect_trace_query(repo_root=tmp_path, job_dirs=[job])
    try:
        (labels_json,) = con.execute("SELECT labels_json FROM v_trace_trials").fetchone()
        entries = json.loads(labels_json)
        assert len(entries) == 1
        assert entries[0]["label"]["trial"] == "trial_gate_declared"
        assert entries[0]["batch"] == "labels"
        report = coverage["label_verification"]["har128-sft-pass"]
        assert report["verified"] == 1
        assert report["rows"] == 1
        assert "labels_g2_a1.jsonl" in report["undeclared"]
    finally:
        con.close()
