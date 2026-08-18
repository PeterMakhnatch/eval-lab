"""Property-based tests for unified attach surface, catalog rebuild, and query invariance."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from evallab.attach import TABLES, attach
from evallab.facts import rebuild_from_raw
from evallab.results import load_jobs


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def _create_raw_job(
    runs_dir: Path,
    job_id: str,
    trial_count: int = 2,
    reward_value: float = 1.0,
    input_tokens: int = 150,
) -> Path:
    job = runs_dir / f"job-{job_id}"
    _write_json(job / "config.json", {"job_name": f"job-{job_id}"})
    _write_json(job / "lock.json", {"harbor": {"version": "0.21.0"}})
    _write_json(
        job / "result.json",
        {
            "id": job_id,
            "started_at": "2026-08-16T12:00:00Z",
            "finished_at": "2026-08-16T12:05:00Z",
            "n_total_trials": trial_count,
            "stats": {"n_completed_trials": trial_count, "n_errored_trials": 0},
        },
    )

    for idx in range(1, trial_count + 1):
        trial_id = f"trial-{job_id}-{idx}"
        trial = job / trial_id
        _write_json(trial / "config.json", {"agent": {"name": "oracle"}})
        _write_json(trial / "lock.json", {"schema_version": 2})
        _write_json(
            trial / "result.json",
            {
                "id": trial_id,
                "trial_name": trial_id,
                "task_name": "local-lab/event-summary",
                "task_checksum": "abc12345",
                "started_at": "2026-08-16T12:00:00Z",
                "finished_at": "2026-08-16T12:02:00Z",
                "agent_info": {"name": "oracle", "version": "1.0.0", "model_info": None},
                "agent_result": {
                    "n_input_tokens": input_tokens,
                    "n_cache_tokens": 0,
                    "n_output_tokens": 50,
                    "cost_usd": 0.01,
                },
                "verifier_result": {
                    "rewards": {
                        "reward": reward_value,
                        "accuracy": 1.0 if reward_value > 0.5 else 0.0,
                    }
                },
                "exception_info": None,
            },
        )
        artifact = trial / "artifacts/answer.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('{"answer": 42}\n')
        _write_json(
            trial / "artifacts/manifest.json",
            [
                {
                    "source": "/app/answer.json",
                    "destination": "artifacts/answer.json",
                    "type": "file",
                    "status": "ok",
                    "service": None,
                }
            ],
        )

    return job


ANALYTICAL_QUERIES = [
    "SELECT count(*), round(coalesce(sum(primary_reward), 0), 4) FROM trial_facts",
    "SELECT trial_id, task_name, agent_name, round(primary_reward, 4) FROM trial_facts "
    "ORDER BY trial_id",
    "SELECT count(*), round(coalesce(sum(reward_value), 0), 4) FROM reward_facts",
    "SELECT count(*), coalesce(sum(size_bytes), 0) FROM artifact_facts",
    "SELECT count(*) FROM tool_usage",
    "SELECT count(*) FROM trajectories",
    "SELECT count(*) FROM steps",
    "SELECT count(*) FROM tool_calls",
    "SELECT count(*) FROM observations",
    "SELECT count(*) FROM jobs",
]


def _execute_query_suite(derived_dir: Path, repo_root: Path) -> dict[str, list[tuple[Any, ...]]]:
    res = attach(repo_root=repo_root, explicit_derived=derived_dir)
    results = {}
    try:
        for query in ANALYTICAL_QUERIES:
            results[query] = res.connection.execute(query).fetchall()
    finally:
        res.connection.close()
    return results


# --- Standalone Properties ---


@given(
    st.lists(
        st.tuples(
            st.text(alphabet="0123456789abcdef", min_size=6, max_size=12),
            st.integers(min_value=1, max_value=3),
            st.floats(min_value=0.0, max_value=1.0),
            st.integers(min_value=50, max_value=1000),
        ),
        min_size=1,
        max_size=4,
        unique_by=lambda t: t[0],
    )
)
@settings(max_examples=25, deadline=None)
def test_property_attach_queries_identical_after_drop_and_rebuild(
    jobs_data: list[tuple[str, int, float, int]],
) -> None:
    """Dropping derived parquet and rebuilding from raw runs yields 100% identical query results."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        runs_dir = root / "runs"
        derived_dir = root / "derived" / "parquet"

        # 1. Create raw jobs
        for job_id, trials, reward, tokens in jobs_data:
            _create_raw_job(
                runs_dir,
                job_id,
                trial_count=trials,
                reward_value=reward,
                input_tokens=tokens,
            )

        # 2. Build initial derived parquet
        jobs = load_jobs([runs_dir])
        rebuild_from_raw(jobs, derived_dir)

        # 3. Query initial attach surface
        results_before = _execute_query_suite(derived_dir, root)

        # 4. Drop derived parquet completely
        shutil.rmtree(derived_dir)
        assert not derived_dir.exists()

        # 5. Rebuild derived parquet from raw jobs
        rebuild_from_raw(jobs, derived_dir)
        assert derived_dir.is_dir()

        # 6. Query rebuilt attach surface
        results_after = _execute_query_suite(derived_dir, root)

        # 7. Assert exact identity across all analytical queries
        assert results_after == results_before, (
            f"Query results drifted after drop and rebuild: {results_after} != {results_before}"
        )


@given(
    st.lists(
        st.tuples(
            st.text(alphabet="0123456789abcdef", min_size=6, max_size=12),
            st.integers(min_value=1, max_value=2),
            st.floats(min_value=0.0, max_value=1.0),
        ),
        min_size=1,
        max_size=3,
        unique_by=lambda t: t[0],
    )
)
@settings(max_examples=20, deadline=None)
def test_property_catalog_rebuild_is_byte_stable(
    jobs_data: list[tuple[str, int, float]],
) -> None:
    """Rebuilding from raw jobs into two clean destinations produces byte-identical files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        runs_dir = root / "runs"
        derived1 = root / "derived1"
        derived2 = root / "derived2"

        for job_id, trials, reward in jobs_data:
            _create_raw_job(runs_dir, job_id, trial_count=trials, reward_value=reward)

        jobs = load_jobs([runs_dir])

        rebuild_from_raw(jobs, derived1)
        rebuild_from_raw(jobs, derived2)

        files1 = sorted(p.relative_to(derived1) for p in derived1.rglob("*.parquet"))
        files2 = sorted(p.relative_to(derived2) for p in derived2.rglob("*.parquet"))

        assert files1 == files2

        for rel_path in files1:
            h1 = _sha256_file(derived1 / rel_path)
            h2 = _sha256_file(derived2 / rel_path)
            assert h1 == h2, f"Byte divergence in rebuilt file {rel_path}: {h1} != {h2}"


def test_property_attach_graceful_degradation_on_empty_or_missing_derived() -> None:
    """Attach handles non-existent and empty derived directories gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        nonexistent = root / "nonexistent"

        # Case 1: Non-existent directory reports z3 attached=False
        res_nonexistent = attach(repo_root=root, explicit_derived=nonexistent)
        try:
            z3 = next(z for z in res_nonexistent.zones if z.name == "z3")
            assert z3.attached is False
            assert z3.reason == "derived root does not exist"
        finally:
            res_nonexistent.connection.close()

        # Case 2: Existing empty directory attaches with empty views (0 rows)
        empty_dir = root / "empty_derived"
        empty_dir.mkdir()
        res_empty = attach(repo_root=root, explicit_derived=empty_dir)
        try:
            z3 = next(z for z in res_empty.zones if z.name == "z3")
            assert z3.attached is True
            for table in TABLES:
                count = res_empty.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                assert count == 0
        finally:
            res_empty.connection.close()
