"""Per-task viewer pages: trusted trials of one task, every source's verdict, a Laminar link."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from evallab.results_viewer import ResultsViewerRoot
from evallab.task_pages import TaskPages

TASK = "mimo-v2.6-rl/format-code-task-000792"
URL = "https://lmnr.ai/project/p/traces/t-1"


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _publish(home: Path, name: str, *, task: str = TASK, copied: bool = False) -> Path:
    job = home / "2026-10-06" / name
    _write(job / "config.json", {"job_name": name})
    _write(job / "result.json", {"job": name})
    trial = job / f"{name.split('-', 2)[-1]}__x1"
    _write(trial / "config.json", {"trial_name": trial.name})
    _write(
        trial / "result.json",
        {
            "trial_name": trial.name,
            "task_name": task,
            "verifier_result": {
                "rewards": {
                    "reward": 1.0,
                    "integrity": 0 if copied else 1,
                    "reward_gated": 0.0 if copied else 1.0,
                }
            },
        },
    )
    taint = [{"kind": "copied_code", "matched_lines": 15, "added_lines": 19}] if copied else []
    _write(
        job / "processed" / f"trial-{trial.name}.json",
        {"taint": taint, "stop_reason": "task_complete"},
    )
    stamp = job.stat().st_mtime - 600
    for dirpath, dirnames, filenames in os.walk(job):
        for item in (*dirnames, *filenames, "."):
            os.utime(os.path.join(dirpath, item), (stamp, stamp))
    return job


def _verdict(store: Path, job: Path, reader: str, checks: dict, **extra: object) -> None:
    trial = next(p for p in job.iterdir() if "__" in p.name)
    _write(
        store / job.name / trial.name / f"{reader}.json",
        {
            "schema": "evallab.reader_verdict/v1",
            "reader": reader,
            "trial": trial.name,
            "job": job.name,
            "checks": checks,
            "explanations": {},
            **extra,
        },
    )


def _digest(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def test_task_page_shows_every_trusted_attempt_and_every_source(tmp_path: Path) -> None:
    home, store = tmp_path / "results", tmp_path / "readers"
    a1 = _publish(home, "HAR-168-har168-000792-a1", copied=True)
    a2 = _publish(home, "HAR-168-har168-000792-a2")
    _verdict(store, a1, "laminar_signals", {"copied": True, "stuck_loop": False}, trace_url=URL)
    _verdict(
        store,
        a1,
        "harbor_analyze",
        {"reward_hacking": False, "task_specification": None},
        input_policy="evallab.reader_input/blind-v2",
    )
    # An analyzer verdict made from unblinded inputs is never shown.
    _verdict(store, a2, "harbor_analyze", {"reward_hacking": True})
    before = _digest(home)

    root = tmp_path / "viewer"
    report = TaskPages(root, store=store).sync(sorted((home / "2026-10-06").iterdir()))
    assert report["built"] == ["task-000792"] and not report["failed"]
    assert _digest(home) == before

    page = root / "task-000792"
    trials = {p.name: p for p in page.iterdir() if p.is_dir()}
    assert set(trials) == {"har168-000792-a1__x1", "har168-000792-a2__x1"}

    copied = json.loads((trials["har168-000792-a1__x1"] / "result.json").read_text())
    assert copied["source"] == "a1"
    rewards = copied["verifier_result"]["rewards"]
    # The real reward dims are untouched; flags are added beside them.
    assert (rewards["reward"], rewards["integrity"], rewards["reward_gated"]) == (1.0, 0, 0.0)
    assert rewards["copied.copy_check"] == 1 and rewards["copied.laminar"] == 1
    assert rewards["reward_hacking.analyze"] == 0 and "task_spec.analyze" not in rewards
    clean = json.loads((trials["har168-000792-a2__x1"] / "result.json").read_text())
    assert clean["verifier_result"]["rewards"]["copied.copy_check"] == 0
    assert "copied.laminar" not in clean["verifier_result"]["rewards"]  # no verdict yet
    assert "reward_hacking.analyze" not in clean["verifier_result"]["rewards"]

    markdown = (trials["har168-000792-a1__x1"] / "analysis.md").read_text()
    assert f"[Open this trial in Laminar]({URL})" in markdown
    assert "| copy check v1 (Eval Lab) | **copied** |" in markdown
    assert "| stop reason | task_complete |" in markdown

    analysis = json.loads((page / "analysis.json").read_text())
    first = analysis["results"][0]
    assert first["trial_name"] == "har168-000792-a1__x1"
    assert list(first["checks"])[:3] == [
        "copied_copy_check_v1",
        "copied_rewardkit_integrity",
        "copied_laminar_signal",
    ]
    assert first["checks"]["copied_laminar_signal"]["outcome"] == "fail"
    assert first["checks"]["reward_hacking_harbor_analyze"]["outcome"] == "pass"
    assert first["checks"]["task_specification_harbor_analyze"]["outcome"] == "not_applicable"


def test_viewer_keeps_task_pages_and_rebuilds_them_when_a_verdict_lands(tmp_path: Path) -> None:
    home, store, root = tmp_path / "results", tmp_path / "readers", tmp_path / "viewer" / "jobs"
    job = _publish(home, "HAR-168-har168-000792-a1", copied=True)
    viewer = ResultsViewerRoot(
        root, [home], settle_seconds=60, task_pages=TaskPages(root, store=store)
    )
    first = viewer.sync()
    assert first.added == ["HAR-168-har168-000792-a1"]
    assert first.task_pages["built"] == ["task-000792"]

    second = viewer.sync()  # the job mirror must not discard the page
    assert not second.changed and (root / "task-000792" / "analysis.json").is_file()

    _verdict(store, job, "laminar_signals", {"copied": True}, trace_url=URL)
    third = viewer.sync()
    assert third.task_pages["built"] == ["task-000792"]
    md = next((root / "task-000792").glob("*/analysis.md")).read_text()
    assert URL in md


def test_mimo_page_url_is_preserved_and_namespaces_do_not_collide() -> None:
    from evallab.task_pages import page_name

    assert page_name("mimo-v2.6-rl/format-code-task-000792") == "task-000792"
    assert page_name("format-code-task-000792") == "task-000792"
    harbor = page_name("harbor/hello-world")
    other = page_name("other/hello-world")
    assert harbor != other
    assert harbor != "task-hello-world"
    assert harbor.startswith("task-harbor-hello-world-")
