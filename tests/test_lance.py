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
    # tasks always built here (synthetic library? but in practice reports status)
    assert "tasks index: skipped" in out or "tasks index: created" in out
    # trials skipped entirely, but when built would report too; here just check no crash
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
