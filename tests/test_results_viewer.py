"""Behavior of the always-on results viewer root and its read-only guard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from evallab.results_viewer import ResultsViewerRoot, allowed_hosts, read_only


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _publish(home: Path, day: str, name: str, *, reward: float = 1.0) -> Path:
    job = home / day / name
    _write_json(job / "config.json", {"job": name})
    _write_json(job / "result.json", {"job": name})
    trial = job / f"{name}__t1"
    _write_json(trial / "config.json", {"trial_name": trial.name})
    _write_json(
        trial / "result.json",
        {"trial_name": trial.name, "verifier_result": {"rewards": {"reward": reward}}},
    )
    (trial / "agent").mkdir()
    return job


def _age(job: Path, seconds: float) -> None:
    stamp = job.stat().st_mtime - seconds
    for dirpath, dirnames, filenames in os.walk(job):
        for name in (*dirnames, *filenames, "."):
            os.utime(os.path.join(dirpath, name), (stamp, stamp))


def _rewards(root: Path, job: str) -> dict:
    trial = root / job / f"{job}__t1" / "result.json"
    return json.loads(trial.read_text(encoding="utf-8"))["verifier_result"]["rewards"]


def _tree_digest(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def test_sync_follows_publish_republish_and_removal(tmp_path: Path) -> None:
    home, root = tmp_path / "results", tmp_path / "viewer" / "jobs"
    old = _publish(home, "2026-10-05", "HAR-1-a")
    _age(old, 600)
    viewer = ResultsViewerRoot(root, [home], settle_seconds=60)

    first = viewer.sync()
    assert first.added == ["HAR-1-a"]
    assert _rewards(root, "HAR-1-a") == {"reward": 1.0, "integrity": 1, "reward_gated": 1.0}

    # A second pass over an unchanged home is a no-op.
    assert not viewer.sync().changed

    # A job published moments ago waits until its tree is quiet.
    fresh = _publish(home, "2026-10-06", "HAR-2-b")
    second = viewer.sync()
    assert second.unsettled == ["HAR-2-b"] and not (root / "HAR-2-b").exists()
    _age(fresh, 600)
    assert viewer.sync().added == ["HAR-2-b"]

    # Republishing replaces result.json; the viewer job is rebuilt from it.
    _write_json(
        old / "HAR-1-a__t1" / "result.json",
        {
            "trial_name": "HAR-1-a__t1",
            "verifier_result": {"rewards": {"reward": 0.0}},
        },
    )
    _write_json(old / "result.json", {"job": "HAR-1-a", "republished": True})
    _age(old, 600)
    third = viewer.sync()
    assert third.rebuilt == ["HAR-1-a"]
    assert _rewards(root, "HAR-1-a")["reward"] == 0.0

    # Removing a published job drops it without touching any other source.
    before = _tree_digest(home / "2026-10-06")
    for dirpath, _dirs, files in os.walk(old, topdown=False):
        for name in files:
            os.unlink(os.path.join(dirpath, name))
        os.rmdir(dirpath)
    fourth = viewer.sync()
    assert fourth.removed == ["HAR-1-a"] and not (root / "HAR-1-a").exists()
    assert _tree_digest(home / "2026-10-06") == before
    assert sorted(p.name for p in root.iterdir()) == ["HAR-2-b"]


def test_dropping_a_viewer_job_never_deletes_source_files(tmp_path: Path) -> None:
    home, root = tmp_path / "results", tmp_path / "viewer" / "jobs"
    job = _publish(home, "2026-10-05", "HAR-1-a")
    # A native-dims trial is linked through as a whole directory symlink.
    _write_json(
        job / "HAR-1-a__t2" / "result.json",
        {
            "trial_name": "HAR-1-a__t2",
            "verifier_result": {"rewards": {"reward": 1.0, "integrity": 1, "reward_gated": 1.0}},
        },
    )
    _age(job, 600)
    viewer = ResultsViewerRoot(root, [home], settle_seconds=60)
    viewer.sync()
    assert (root / "HAR-1-a" / "HAR-1-a__t2").is_symlink()
    before = _tree_digest(job)

    _write_json(job / "result.json", {"job": "HAR-1-a", "republished": True})
    _age(job, 600)
    assert viewer.sync().rebuilt == ["HAR-1-a"]
    after = _tree_digest(job)
    assert after.keys() == before.keys()


def test_restart_resumes_from_root_and_keeps_same_name_jobs_apart(tmp_path: Path) -> None:
    home, root = tmp_path / "results", tmp_path / "viewer" / "jobs"
    for day in ("2026-10-05", "2026-10-06"):
        _age(_publish(home, day, "HAR-3-same"), 600)
    assert sorted(ResultsViewerRoot(root, [home]).sync().added) == ["HAR-3-same", "HAR-3-same-2"]

    restarted = ResultsViewerRoot(root, [home]).sync()
    assert not restarted.changed and restarted.jobs == 2


def _call(app, method: str, host: str) -> int:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "method": method, "headers": [(b"host", host.encode())]}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_guard_passes_only_loopback_reads() -> None:
    async def viewer(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = read_only(viewer, allowed_hosts("127.0.0.1", 8100))
    assert _call(app, "GET", "127.0.0.1:8100") == 200
    assert _call(app, "GET", "localhost:8100") == 200
    # Run launch, analyze, upload and delete are all non-GET.
    for method in ("POST", "DELETE", "PUT", "PATCH"):
        assert _call(app, method, "127.0.0.1:8100") == 405
    # DNS rebinding: a foreign name that resolves to loopback.
    assert _call(app, "GET", "attacker.example:8100") == 403
