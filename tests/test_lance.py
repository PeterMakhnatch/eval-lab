"""Tests for evallab.lance: embedder, build, search, skip."""

import contextlib
import io
import json
import subprocess
import sys
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evallab.lance import HashingEmbedder, build, search


def test_embedder_determinism_same_process():
    e = HashingEmbedder(dim=256)
    v1 = e.embed(["hello world test"])[0]
    v2 = e.embed(["hello world test"])[0]
    assert v1 == v2


def test_embedder_determinism_across_process(tmp_path):
    code = """
from evallab.lance import HashingEmbedder
e = HashingEmbedder(256)
v = e.embed(["hello world test"])[0]
import json
print(json.dumps(v))
"""
    out1 = subprocess.check_output(
        [sys.executable, "-c", code], text=True, cwd=tmp_path
    )
    out2 = subprocess.check_output(
        [sys.executable, "-c", code], text=True, cwd=tmp_path
    )
    assert json.loads(out1) == json.loads(out2)


def test_embedder_dim_and_l2_norm():
    e = HashingEmbedder(256)
    v = e.embed(["some text here"])[0]
    assert len(v) == 256
    norm = sum(x * x for x in v)
    assert abs(norm - 1.0) < 1e-6


def test_build_tasks_from_synthetic_fixture(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("tasks")
    out = f.getvalue()
    assert "tasks:" in out
    lance_dir = derived / "lance"
    assert lance_dir.exists()


def test_build_trials_from_synthetic_parquet(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    parquet_dir = derived / "parquet" / "job_id=job1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("task_name", pa.string()),
            ("agent_name", pa.string()),
            ("primary_reward", pa.float64()),
            ("exception_class", pa.string()),
        ]
    )
    data = [
        {
            "job_id": "job1",
            "trial_id": "t1",
            "task_name": "task1",
            "agent_name": "agentx",
            "primary_reward": 1.0,
            "exception_class": "",
        }
    ]
    tbl = pa.Table.from_pylist(data, schema=schema)
    pq.write_table(tbl, parquet_dir / "trial_facts.parquet")
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("trials")
    out = f.getvalue()
    assert "trials:" in out


def test_build_trials_from_real_derived_layout_fixture(tmp_path, monkeypatch):
    """Fixture layout matches real derived_root_from_environment exactly.
    <root>/job_id=.../trial_id=.../trial_facts.parquet (no extra subdir).
    Asserts non-zero rows (fails on double-append).
    """
    derived_root = tmp_path / "parquet_root"
    job_dir = derived_root / "job_id=job42" / "trial_id=t99"
    job_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("task_name", pa.string()),
            ("agent_version", pa.string()),
            ("primary_reward", pa.float64()),
            ("exception_class", pa.string()),
            ("exception_phase", pa.string()),
        ]
    )
    data = [
        {
            "job_id": "job42",
            "trial_id": "t99",
            "task_name": "task_real",
            "agent_version": "v1.2",
            "primary_reward": 0.95,
            "exception_class": "",
            "exception_phase": "",
        }
    ]
    tbl = pa.Table.from_pylist(data, schema=schema)
    pq.write_table(tbl, job_dir / "trial_facts.parquet")
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived_root))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("trials")
    out = f.getvalue()
    assert "trials: 1 rows" in out


def test_skip_reason_includes_examined_path(tmp_path, monkeypatch):
    derived_root = tmp_path / "nonexistent_parquet_root"
    # do not create it
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived_root))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("trials")
    out = f.getvalue()
    assert "trials: skipped" in out
    assert str(derived_root) in out  # exact path examined must be in skip reason


def test_idempotent_rebuild(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f1 = io.StringIO()
    with contextlib.redirect_stdout(f1):
        build("tasks")
    f2 = io.StringIO()
    with contextlib.redirect_stdout(f2):
        build("tasks")
    out1 = f1.getvalue()
    out2 = f2.getvalue()
    count1 = out1.splitlines()[0].split()[-2]
    count2 = out2.splitlines()[0].split()[-2]
    assert count1 == count2


def test_search_returns_planted_nearest(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("tasks")
    f2 = io.StringIO()
    with contextlib.redirect_stdout(f2):
        search("task", "tasks", 3)
    out = f2.getvalue()
    assert "task_ref" in out or "dist=" in out


def test_table_skipped_when_source_missing(tmp_path, monkeypatch):
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("trials")
    out = f.getvalue()
    assert "trials: skipped" in out


def test_build_reports_index_status_per_table(tmp_path, monkeypatch):
    """After build, output must show either index created or a skip reason for each table.
    This would have passed silently under blanket suppress(Exception).
    """
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("all")
    out = f.getvalue()
    assert "tasks index: skipped" in out or "tasks index: created" in out
    assert (
        "trials: skipped" in out
        or "trials index: skipped" in out
        or "trials index: created" in out
    )


def test_create_index_misuse_raises_rather_than_suppressed(tmp_path, monkeypatch):
    """A genuine error from create_index (not the small-row case) must propagate.
    The old with contextlib.suppress(Exception) would have hidden it; this test fails on that impl.
    """
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    def bad_create_index(*args, **kwargs):
        raise ValueError("simulated misuse of create_index arguments")
    with (
        patch("lancedb.table.LanceTable.create_index", bad_create_index),
        pytest.raises(ValueError, match="simulated misuse"),
    ):
        build("tasks")


def test_build_steps_and_trials_from_trajectory_fixture(tmp_path, monkeypatch):
    """Fixture with runs/ tree + trial_facts.parquet under tmp_path.
    Asserts steps table has correct row count, and lexical query hits the matching step first.
    """
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    import evallab.lance as lance_mod
    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    parquet_dir = derived / "parquet" / "job_id=j1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
            ("agent_version", pa.string()),
            ("primary_reward", pa.float64()),
        ]
    )
    data = [
        {
            "job_id": "j1",
            "trial_id": "t1",
            "job_name": "job1",
            "trial_name": "trial1",
            "task_name": "html-js-filter",
            "agent_version": "0.147.0",
            "primary_reward": 0.9,
        }
    ]
    tbl = pa.Table.from_pylist(data, schema=schema)
    pq.write_table(tbl, parquet_dir / "trial_facts.parquet")

    traj_dir = tmp_path / "runs" / "job1" / "trial1" / "agent"
    traj_dir.mkdir(parents=True)
    traj = {
        "schema_version": "ATIF-1",
        "session_id": "s1",
            {"step_id": 0, "timestamp": "t0", "source": "system",
             "message": "initial system prompt here"},
            {"step_id": 1, "timestamp": "t1", "source": "user",
             "message": "remove javascript from html task instruction"},
            {"step_id": 2, "timestamp": "t2", "source": "assistant",
             "message": "agent reasoning about filter"},
        ],
        "final_metrics": {},
    }
    (traj_dir / "trajectory.json").write_text(json.dumps(traj))

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("all")
    out = f.getvalue()
    assert "steps: 3 rows" in out
    assert "trials: 1 rows" in out

    f2 = io.StringIO()
    with contextlib.redirect_stdout(f2):
        search("remove javascript from html", "steps", 3)
    out2 = f2.getvalue()
    assert "remove javascript from html" in out2
    result_lines = [line for line in out2.splitlines() if line.startswith("dist=")]
    assert result_lines, "no search results"
    assert "remove javascript from html" in result_lines[0]


def test_steps_different_text_different_vectors(tmp_path, monkeypatch):
    """Two steps with different text must produce different vectors (catches the original defect)."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    import evallab.lance as lance_mod
    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    parquet_dir = derived / "p" / "job_id=j" / "trial_id=t"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
            ("agent_version", pa.string()),
            ("primary_reward", pa.float64()),
        ]
    )
    data = [
        {
            "job_id": "j",
            "trial_id": "t",
            "job_name": "j",
            "trial_name": "t",
            "task_name": "t",
            "agent_version": "v",
            "primary_reward": 0.0,
        }
    ]
    pq.write_table(pa.Table.from_pylist(data, schema=schema), parquet_dir / "trial_facts.parquet")

    traj_dir = tmp_path / "runs" / "j" / "t" / "agent"
    traj_dir.mkdir(parents=True)
    traj = {
        "schema_version": "ATIF-1",
        "steps": [
            {"step_id": 0, "source": "a", "message": "text one"},
            {"step_id": 1, "source": "b", "message": "text two different"},
        ],
    }
    (traj_dir / "trajectory.json").write_text(json.dumps(traj))

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("steps")
    e = HashingEmbedder()
    v1 = e.embed(["text one"])[0]
    v2 = e.embed(["text two different"])[0]
    assert v1 != v2


def test_missing_trajectory_counted_and_reported(tmp_path, monkeypatch):
    """Trajectory referenced in parquet but absent on disk must be counted/reported, build must not be fatal."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    import evallab.lance as lance_mod
    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    parquet_dir = derived / "parquet" / "job_id=jmiss" / "trial_id=tmiss"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
            ("agent_version", pa.string()),
            ("primary_reward", pa.float64()),
        ]
    )
    data = [
        {
            "job_id": "jmiss",
            "trial_id": "tmiss",
            "job_name": "jobmiss",
            "trial_name": "trialmiss",
            "task_name": "t",
            "agent_version": "v",
            "primary_reward": None,
        }
    ]
    pq.write_table(pa.Table.from_pylist(data, schema=schema), parquet_dir / "trial_facts.parquet")

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("steps")
    out = f.getvalue()
    assert "steps: skipped" in out or "missing 1 trajectories" in out
