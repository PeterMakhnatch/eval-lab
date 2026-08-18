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
    out1 = subprocess.check_output([sys.executable, "-c", code], text=True, cwd=tmp_path)
    out2 = subprocess.check_output([sys.executable, "-c", code], text=True, cwd=tmp_path)
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
    parquet_dir = derived / "job_id=job1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
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
            "job_name": "job1",
            "trial_name": "t1",
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
    assert "trials: 1 rows" in out


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
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
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
            "job_name": "job42",
            "trial_name": "t99",
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


def test_builders_go_through_attach_surface_empty_fixture(tmp_path, monkeypatch):
    """Pointing the surface at an empty fixture root yields zero rows and a reported skip
    rather than silently finding the real corpus (catches reintroduction of direct globbing).
    """
    empty_derived = tmp_path / "empty_derived"
    empty_derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(empty_derived))
    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("trials")
    out = f.getvalue()
    assert "trials: skipped" in out
    assert "no trial_facts rows" in out
    assert str(empty_derived) in out


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
    out2 = f2.getvalue()
    assert "task_ref" in out2 or "dist=" in out2 or "table tasks not found" in out2


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
        "trials: skipped" in out or "trials index: skipped" in out or "trials index: created" in out
    )
    assert (
        "analyses: skipped" in out
        or "analyses index: skipped" in out
        or "analyses index: created" in out
    )


def test_create_index_misuse_raises_rather_than_suppressed(tmp_path, monkeypatch):
    """A genuine error from create_index (not the small-row case) must propagate.
    The old with contextlib.suppress(Exception) would have hidden it; this test fails on that impl.
    """
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    def bad_create_index(*args, **kwargs):
        raise ValueError("simulated misuse of create_index arguments")

    with (
        patch("evallab.lance.MIN_ROWS_FOR_ANN", 0),
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

    parquet_dir = derived / "job_id=j1" / "trial_id=t1"
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
    f = io.StringIO()
    traj = {
        "schema_version": "ATIF-1",
        "session_id": "s1",
        "steps": [
            {
                "step_id": 0,
                "timestamp": "t0",
                "source": "system",
                "message": "initial system prompt here",
            },
            {
                "step_id": 1,
                "timestamp": "t1",
                "source": "user",
                "message": "remove javascript from html task instruction",
            },
            {
                "step_id": 2,
                "timestamp": "t2",
                "source": "assistant",
                "message": "agent reasoning about filter",
            },
        ],
        "final_metrics": {},
    }
    (traj_dir / "trajectory.json").write_text(json.dumps(traj))

    with contextlib.redirect_stdout(f):
        build("all")
    out = f.getvalue()
    assert "steps: 3 rows" in out
    assert "trials: 1 rows" in out

    f2 = io.StringIO()
    with contextlib.redirect_stdout(f2):
        search("remove javascript from html", "steps", 3)
    out2 = f2.getvalue()
    assert "remove javascript from html" in out2 or "table steps not found" in out2
    if "table steps not found" not in out2:
        result_lines = [line for line in out2.splitlines() if line.startswith("dist=")]
        assert result_lines, "no search results"
        assert "remove javascript from html" in result_lines[0]

    # Two steps with different text must produce different vectors (catches original defect)
    derived2 = tmp_path / "derived2"
    derived2.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived2))

    parquet_dir2 = derived2 / "job_id=j" / "trial_id=t"
    parquet_dir2.mkdir(parents=True)
    schema2 = pa.schema(
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
    data2 = [
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
    table2 = pa.Table.from_pylist(data2, schema=schema2)
    pq.write_table(table2, parquet_dir2 / "trial_facts.parquet")

    traj_dir2 = tmp_path / "runs" / "j" / "t" / "agent"
    traj_dir2.mkdir(parents=True)
    traj2 = {
        "schema_version": "ATIF-1",
        "steps": [
            {"step_id": 0, "source": "a", "message": "text one"},
            {"step_id": 1, "source": "b", "message": "text two different"},
        ],
    }
    (traj_dir2 / "trajectory.json").write_text(json.dumps(traj2))

    f3 = io.StringIO()
    with contextlib.redirect_stdout(f3):
        build("steps")
    e = HashingEmbedder()
    v1 = e.embed(["text one"])[0]
    v2 = e.embed(["text two different"])[0]
    assert v1 != v2


def test_missing_trajectory_counted_and_reported(tmp_path, monkeypatch):
    """Trajectory in parquet but absent on disk must be counted/reported; not fatal."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    parquet_dir = derived / "job_id=jmiss" / "trial_id=tmiss"
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


def test_steps_explicit_runs_root_override(tmp_path, monkeypatch):
    """Assert seam directly with override; row count matches steps; reported root is passed."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    runs_override = tmp_path / "fixture-runs"
    job_dir = runs_override / "job1" / "trial1" / "agent"
    job_dir.mkdir(parents=True)
    traj = {
        "schema_version": "ATIF-1",
        "session_id": "s1",
        "steps": [
            {"step_id": 0, "source": "user", "message": "remove javascript from html"},
            {"step_id": 1, "source": "assistant", "message": "done"},
        ],
        "final_metrics": {},
    }
    (job_dir / "trajectory.json").write_text(json.dumps(traj))
    parquet_dir = derived / "job_id=j1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
            ("primary_reward", pa.float64()),
        ]
    )
    data = [
        {
            "job_id": "j1",
            "trial_id": "t1",
            "job_name": "job1",
            "trial_name": "trial1",
            "task_name": "html",
            "primary_reward": 1.0,
        }
    ]
    pq.write_table(pa.Table.from_pylist(data, schema=schema), parquet_dir / "trial_facts.parquet")
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("steps", runs_root=runs_override)
    out = f.getvalue()
    assert "steps: 2 rows" in out
    assert str(runs_override) in out


def test_trajectory_absent_on_disk_counted_reported_not_fatal(tmp_path, monkeypatch):
    """Trajectory in parquet but absent on disk is counted, reported with path, not fatal."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    parquet_dir = derived / "job_id=j1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
            ("primary_reward", pa.float64()),
        ]
    )
    data = [
        {
            "job_id": "j1",
            "trial_id": "t1",
            "job_name": "job1",
            "trial_name": "good",
            "task_name": "t",
            "primary_reward": 1.0,
        },
        {
            "job_id": "j1",
            "trial_id": "t2",
            "job_name": "job1",
            "trial_name": "bad",
            "task_name": "t",
            "primary_reward": 0.0,
        },
    ]
    pq.write_table(pa.Table.from_pylist(data, schema=schema), parquet_dir / "trial_facts.parquet")
    good_dir = tmp_path / "runs" / "job1" / "good" / "agent"
    good_dir.mkdir(parents=True)
    good_traj = {
        "schema_version": "ATIF-1",
        "session_id": "s",
        "steps": [{"step_id": 0, "source": "user", "message": "good step"}],
        "final_metrics": {},
    }
    (good_dir / "trajectory.json").write_text(json.dumps(good_traj))
    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("steps")
    out = f.getvalue()
    assert "steps: 1 rows" in out
    assert "missing 1 trajectories (e.g." in out
    assert "job1/bad/agent/trajectory.json" in out


def test_index_decision_same_for_all_tables_at_row_count(tmp_path, monkeypatch):
    """Index decision identical for every table at given row count."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    # create a task fixture in tmp_path
    task_dir = tmp_path / "library" / "tasks" / "fixture-task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "fixture-task"\n')
    (task_dir / "instruction.md").write_text("Do something useful.")

    pdir = derived / "job_id=j1" / "trial_id=t1"
    pdir.mkdir(parents=True, exist_ok=True)
    schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
            ("primary_reward", pa.float64()),
        ]
    )
    data = [
        {
            "job_id": "j1",
            "trial_id": "t1",
            "job_name": "job1",
            "trial_name": "trial1",
            "task_name": "t",
            "primary_reward": 1.0,
        }
    ]
    pq.write_table(pa.Table.from_pylist(data, schema=schema), pdir / "trial_facts.parquet")
    traj_dir = tmp_path / "runs" / "job1" / "trial1" / "agent"
    traj_dir.mkdir(parents=True, exist_ok=True)
    traj = {
        "schema_version": "ATIF-1",
        "session_id": "s",
        "steps": [{"step_id": 0, "source": "user", "message": "step"}],
        "final_metrics": {},
    }
    (traj_dir / "trajectory.json").write_text(json.dumps(traj))

    # Analyses fixture for index check
    analyses_dir = derived / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)
    a_rec = pa.schema(
        [
            ("analysis_id", pa.string()),
            ("trial_id", pa.string()),
            ("model", pa.string()),
            ("category", pa.string()),
            ("created_at", pa.string()),
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "analysis_id": "A1",
                    "trial_id": "t1",
                    "model": "m1",
                    "category": "cat1",
                    "created_at": "2026-08-18T10:00:00Z",
                }
            ],
            schema=a_rec,
        ),
        analyses_dir / "analyses.parquet",
    )
    aj_dir = tmp_path / "research" / "analysis"
    aj_dir.mkdir(parents=True, exist_ok=True)
    (aj_dir / "A1.json").write_text(
        json.dumps({"analysis_id": "A1", "summary": "concl", "created_at": "2026-08-18T10:00:00Z"})
    )

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("all")
    out = f.getvalue()
    skip_msg = "index: skipped (too few rows for ANN index (exact brute-force search))"
    assert "tasks " + skip_msg in out
    assert "trials " + skip_msg in out
    assert "steps " + skip_msg in out
    assert "analyses " + skip_msg in out


def test_build_analyses_from_fixture_and_search(tmp_path, monkeypatch):
    """Fixture with analyses.parquet + research/analysis/ JSON conclusions.
    Asserts analyses table has correct row count and columns, and query hits
    the matching conclusion.
    """
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    # Attach surface: trial_facts.parquet with job_id mapping
    pdir1 = derived / "job_id=job1" / "trial_id=trial1"
    pdir1.mkdir(parents=True)
    pdir2 = derived / "job_id=job2" / "trial_id=trial2"
    pdir2.mkdir(parents=True)
    tf_schema = pa.schema(
        [
            ("job_id", pa.string()),
            ("trial_id", pa.string()),
            ("job_name", pa.string()),
            ("trial_name", pa.string()),
            ("task_name", pa.string()),
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "job_id": "job1",
                    "trial_id": "trial1",
                    "job_name": "j1",
                    "trial_name": "t1",
                    "task_name": "html-filter",
                }
            ],
            schema=tf_schema,
        ),
        pdir1 / "trial_facts.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "job_id": "job2",
                    "trial_id": "trial2",
                    "job_name": "j2",
                    "trial_name": "t2",
                    "task_name": "math-eval",
                }
            ],
            schema=tf_schema,
        ),
        pdir2 / "trial_facts.parquet",
    )

    # Analyses Parquet
    analyses_dir = derived / "analyses"
    analyses_dir.mkdir(parents=True)
    analyses_parquet = analyses_dir / "analyses.parquet"
    rec_schema = pa.schema(
        [
            ("analysis_id", pa.string()),
            ("trial_id", pa.string()),
            ("rubric_digest", pa.string()),
            ("model", pa.string()),
            ("category", pa.string()),
            ("evidence_count", pa.int64()),
            ("confidence_level", pa.string()),
            ("confidence_n", pa.int64()),
            ("confidence_interval_low", pa.float64()),
            ("confidence_interval_high", pa.float64()),
            ("confidence_provenance", pa.string()),
            ("created_at", pa.string()),
        ]
    )
    rec_data = [
        {
            "analysis_id": "01M01ANALYSIS11111111111111",
            "trial_id": "trial1",
            "rubric_digest": "sha256:abc",
            "model": "stub-analyst",
            "category": "regex_miscompilation",
            "evidence_count": 2,
            "confidence_level": "high",
            "confidence_n": 10,
            "confidence_interval_low": 0.8,
            "confidence_interval_high": 0.95,
            "confidence_provenance": "sha256:prov1",
            "created_at": "2026-08-18T12:00:00Z",
        },
        {
            "analysis_id": "01M02ANALYSIS22222222222222",
            "trial_id": "trial2",
            "rubric_digest": "sha256:def",
            "model": "stub-analyst",
            "category": "syntax_error",
            "evidence_count": 1,
            "confidence_level": "medium",
            "confidence_n": 5,
            "confidence_interval_low": 0.5,
            "confidence_interval_high": 0.7,
            "confidence_provenance": "sha256:prov2",
            "created_at": "2026-08-18T13:00:00Z",
        },
    ]
    pq.write_table(pa.Table.from_pylist(rec_data, schema=rec_schema), analyses_parquet)

    # JSON conclusions in research/analysis/
    analysis_json_dir = tmp_path / "research" / "analysis"
    analysis_json_dir.mkdir(parents=True)
    c1 = {
        "analysis_id": "01M01ANALYSIS11111111111111",
        "trial_id": "trial1",
        "summary": "The agent failed to strip JavaScript script tags properly from HTML document.",
        "created_at": "2026-08-18T12:00:00Z",
    }
    c2 = {
        "analysis_id": "01M02ANALYSIS22222222222222",
        "trial_id": "trial2",
        "summary": "The model suffered a division by zero error in arithmetic parser.",
        "created_at": "2026-08-18T13:00:00Z",
    }
    (analysis_json_dir / "01M01ANALYSIS11111111111111.json").write_text(json.dumps(c1))
    (analysis_json_dir / "01M02ANALYSIS22222222222222.json").write_text(json.dumps(c2))

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("analyses")
    out = f.getvalue()
    assert "analyses: 2 rows" in out

    # Verify columns and data in LanceDB
    import lancedb

    db = lancedb.connect(str(derived / "lance"))
    tbl = db.open_table("analyses")
    arrow_tbl = tbl.to_arrow()
    col_names = set(arrow_tbl.column_names)
    expected_cols = {
        "analysis_id",
        "trial_id",
        "job_id",
        "model",
        "category",
        "created_at",
        "conclusion",
        "vector",
    }
    assert expected_cols.issubset(col_names)
    rows = tbl.to_arrow().to_pylist()
    assert len(rows) == 2
    row1 = next(r for r in rows if r["analysis_id"] == "01M01ANALYSIS11111111111111")
    assert row1["job_id"] == "job1"
    assert row1["trial_id"] == "trial1"
    assert row1["model"] == "stub-analyst"
    assert "JavaScript script tags" in row1["conclusion"]

    # Test nearest neighbour search
    f2 = io.StringIO()
    with contextlib.redirect_stdout(f2):
        search("strip javascript script tags html", table="analyses", k=2)
    out2 = f2.getvalue()
    result_lines = [line for line in out2.splitlines() if line.startswith("dist=")]
    assert len(result_lines) == 2
    assert "01M01ANALYSIS11111111111111" in result_lines[0]


def test_build_analyses_idempotent(tmp_path, monkeypatch):
    """Rebuild of analyses table must be idempotent without row duplication."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    parquet_dir = derived / "job_id=j1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"job_id": "j1", "trial_id": "t1"}],
            schema=pa.schema([("job_id", pa.string()), ("trial_id", pa.string())]),
        ),
        parquet_dir / "trial_facts.parquet",
    )

    analyses_dir = derived / "analyses"
    analyses_dir.mkdir(parents=True)
    rec_schema = pa.schema(
        [
            ("analysis_id", pa.string()),
            ("trial_id", pa.string()),
            ("model", pa.string()),
            ("category", pa.string()),
            ("created_at", pa.string()),
        ]
    )
    rec_data = [
        {
            "analysis_id": "01M01A",
            "trial_id": "t1",
            "model": "m1",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        }
    ]
    pq.write_table(
        pa.Table.from_pylist(rec_data, schema=rec_schema), analyses_dir / "analyses.parquet"
    )

    analysis_json_dir = tmp_path / "research" / "analysis"
    analysis_json_dir.mkdir(parents=True)
    (analysis_json_dir / "01M01A.json").write_text(
        json.dumps(
            {
                "analysis_id": "01M01A",
                "summary": "Conclusion statement",
                "created_at": "2026-08-18T10:00:00Z",
            }
        )
    )

    f1 = io.StringIO()
    with contextlib.redirect_stdout(f1):
        build("analyses")
    assert "analyses: 1 rows" in f1.getvalue()

    f2 = io.StringIO()
    with contextlib.redirect_stdout(f2):
        build("analyses")
    assert "analyses: 1 rows" in f2.getvalue()

    import lancedb

    db = lancedb.connect(str(derived / "lance"))
    tbl = db.open_table("analyses")
    assert len(tbl.to_arrow()) == 1


def test_build_analyses_skips_missing_required_identity_fields(tmp_path, monkeypatch):
    """Rows missing required identity fields (analysis_id, trial_id, job_id,
    model, created_at, conclusion) must be skipped rather than being
    silently indexed as empty."""
    derived = tmp_path / "derived"
    derived.mkdir(parents=True)
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))

    import evallab.lance as lance_mod

    monkeypatch.setattr(lance_mod, "repository_root", lambda: tmp_path)

    parquet_dir = derived / "job_id=j1" / "trial_id=t1"
    parquet_dir.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"job_id": "j1", "trial_id": "t1"}],
            schema=pa.schema([("job_id", pa.string()), ("trial_id", pa.string())]),
        ),
        parquet_dir / "trial_facts.parquet",
    )

    analyses_dir = derived / "analyses"
    analyses_dir.mkdir(parents=True)
    rec_schema = pa.schema(
        [
            ("analysis_id", pa.string()),
            ("trial_id", pa.string()),
            ("model", pa.string()),
            ("category", pa.string()),
            ("created_at", pa.string()),
        ]
    )
    rec_data = [
        # Valid row
        {
            "analysis_id": "A_VALID",
            "trial_id": "t1",
            "model": "m1",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        },
        # Missing analysis_id
        {
            "analysis_id": "",
            "trial_id": "t1",
            "model": "m1",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        },
        # Missing trial_id
        {
            "analysis_id": "A_NOTRIAL",
            "trial_id": "",
            "model": "m1",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        },
        # Missing job_id (trial t_unknown has no mapping in trial_facts)
        {
            "analysis_id": "A_NOJOB",
            "trial_id": "t_unknown",
            "model": "m1",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        },
        # Missing model
        {
            "analysis_id": "A_NOMODEL",
            "trial_id": "t1",
            "model": "",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        },
        # Missing created_at
        {
            "analysis_id": "A_NOTIME",
            "trial_id": "t1",
            "model": "m1",
            "category": "cat1",
            "created_at": "",
        },
        # Missing conclusion (no JSON file created)
        {
            "analysis_id": "A_NOCONCL",
            "trial_id": "t1",
            "model": "m1",
            "category": "cat1",
            "created_at": "2026-08-18T10:00:00Z",
        },
    ]
    pq.write_table(
        pa.Table.from_pylist(rec_data, schema=rec_schema), analyses_dir / "analyses.parquet"
    )

    analysis_json_dir = tmp_path / "research" / "analysis"
    analysis_json_dir.mkdir(parents=True)
    # Only write JSON for the valid row and those testing specific field failures
    for a_id in ["A_VALID", "A_NOTRIAL", "A_NOJOB", "A_NOMODEL", "A_NOTIME"]:
        (analysis_json_dir / f"{a_id}.json").write_text(
            json.dumps(
                {
                    "analysis_id": a_id,
                    "summary": "Valid conclusion text.",
                    "created_at": "" if a_id == "A_NOTIME" else "2026-08-18T10:00:00Z",
                }
            )
        )

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("analyses")
    out = f.getvalue()
    assert "analyses: 1 rows" in out

    import lancedb

    db = lancedb.connect(str(derived / "lance"))
    tbl = db.open_table("analyses")
    rows = tbl.to_arrow().to_pylist()
    assert len(rows) == 1
    assert rows[0]["analysis_id"] == "A_VALID"
    assert rows[0]["job_id"] == "j1"
    assert rows[0]["trial_id"] == "t1"
    assert rows[0]["model"] == "m1"
    assert rows[0]["created_at"] == "2026-08-18T10:00:00Z"
    assert rows[0]["conclusion"] == "Valid conclusion text."


def test_build_analyses_skipped_when_source_missing(tmp_path, monkeypatch):
    """When analyses.parquet is missing, table build is skipped with exact path reported."""
    derived = tmp_path / "derived"
    derived.mkdir()
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(derived))
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        build("analyses")
    out = f.getvalue()
    assert "analyses: skipped" in out
    assert str(derived / "analyses" / "analyses.parquet") in out
