"""Results home: publish copies a finished job with the code that produced it."""

from __future__ import annotations

import json
import os
from pathlib import Path

from evallab.process_job import process_job
from evallab.results_home import backfill, publish_job, write_index


def _job(root: Path, name: str = "har117-sample") -> Path:
    job = root / "runs" / name
    job.mkdir(parents=True)
    (job / "config.json").write_text("{}", encoding="utf-8")
    (job / "result.json").write_text('{"id": "job-1"}', encoding="utf-8")
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-09-30T08:25:13+00:00",
                "command": ["harbor", "run", "--model", "selfhosted/example", "--env", "daytona"],
                "repository": {"commit": "abc123", "dirty": True},
                "experiment": {
                    "task_id": "format-code",
                    "package_digest": "sha256:aa",
                    "harness_tree_path": "research/harness",
                    "harness_tree_sha256": "sha256:bb",
                },
                "tools": {"harbor": "0.21.0"},
                "model_identity": {"requested": "selfhosted/example"},
            }
        ),
        encoding="utf-8",
    )
    (job / "experiment-spec.json").write_text(
        json.dumps({"question_ref": "har117-results-home"}), encoding="utf-8"
    )
    provenance = job / "repository-provenance"
    provenance.mkdir()
    (provenance / "uncommitted.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (provenance / "repository.json").write_text(
        json.dumps(
            {
                "remote": "https://github.com/example/eval-lab",
                "worktree": str(root),
                "branch": "har117",
                "commit": "abc123def456",
                "dirty": True,
                "untracked": ["notes.txt"],
                "uncommitted_diff_sha256": None,
                "capture": "run-time",
            }
        ),
        encoding="utf-8",
    )
    trial = job / "trial-one"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps({"trial_name": "trial-one", "verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    return job


def test_publish_copies_bytes_and_marks_uncommitted_code(tmp_path: Path) -> None:
    job = _job(tmp_path)
    home = tmp_path / "results"
    checkout = tmp_path / "checkout"
    research = checkout / "research" / "experiments" / "har117-results-home"
    research.mkdir(parents=True)
    (research / "NOTES.md").write_text("notes", encoding="utf-8")

    first = publish_job(job, root=home, pr_lookup=lambda _commit: "42", primary_checkout=checkout)
    published = Path(first["published"])
    assert published == home / "2026-09-30" / "HAR-117-har117-sample"
    copied = published / "result.json"
    assert not copied.is_symlink()
    assert copied.stat().st_ino != (job / "result.json").stat().st_ino

    provenance = json.loads((published / "provenance.json").read_text())
    assert provenance["repository"]["commit"] == "abc123def456"
    assert provenance["repository"]["dirty"] is True
    assert provenance["repository"]["untracked"] == ["notes.txt"]
    assert provenance["repository"]["uncommitted_diff_sha256"].startswith("sha256:")
    assert provenance["pull_request"] == {"number": 42, "reason": None}
    assert provenance["harness"]["harbor_version"] == "0.21.0"
    assert provenance["tasks"][0]["package_digest"] == "sha256:aa"
    assert (published / "uncommitted.diff").read_text().startswith("diff --git")

    again = publish_job(job, root=home, pr_lookup=lambda _commit: "42", primary_checkout=checkout)
    assert again["published"] == first["published"]
    index = (home / "INDEX.md").read_text()
    assert sum(line.startswith("| HAR-117") for line in index.splitlines()) == 1
    assert "**uncommitted code**" in index
    assert "NOTES.md" in index
    assert "`abc123def456`" in index


def test_republish_never_removes_the_source(tmp_path: Path) -> None:
    job = _job(tmp_path)
    source = job / "result.json"
    before = source.read_bytes()
    publish_job(job, root=tmp_path / "results", pr_lookup=lambda _commit: None)
    publish_job(job, root=tmp_path / "results", pr_lookup=lambda _commit: None)
    assert source.is_file()
    assert source.read_bytes() == before


def test_same_job_name_from_two_sources_keeps_both(tmp_path: Path) -> None:
    first = _job(tmp_path / ".worktrees" / "one" / "runs", "har117-shared")
    second = _job(tmp_path / ".worktrees" / "two" / "runs", "har117-shared")
    (second / "result.json").write_text('{"id": "other"}', encoding="utf-8")
    home = tmp_path / "results"
    publish_job(first, root=home, pr_lookup=lambda _commit: None)
    second_result = publish_job(second, root=home, pr_lookup=lambda _commit: None)
    kept = sorted(path.name for path in (home / "2026-09-30").iterdir())
    assert kept == ["HAR-117-har117-shared~one", "HAR-117-har117-shared~two"]
    assert second_result["collided_with"] == str(first.resolve())
    first_copy = home / "2026-09-30" / "HAR-117-har117-shared~one" / "result.json"
    second_copy = home / "2026-09-30" / "HAR-117-har117-shared~two" / "result.json"
    assert json.loads(first_copy.read_text())["id"] == "job-1"
    assert json.loads(second_copy.read_text())["id"] == "other"


def test_missing_provenance_stays_unknown(tmp_path: Path) -> None:
    job = _job(tmp_path)
    (job / "repository-provenance").rename(tmp_path / "saved")
    published = Path(publish_job(job, root=tmp_path / "results", pr_lookup=None)["published"])
    provenance = json.loads((published / "provenance.json").read_text())
    assert provenance["repository"]["capture"] == "run-time"
    assert provenance["repository"]["commit"] == "abc123"
    assert any("snapshot missing" in reason for reason in provenance["unknown"])
    assert provenance["pull_request"]["number"] is None
    assert "no commit" not in (provenance["pull_request"]["reason"] or "")


def test_process_job_publishes_into_overridden_home(tmp_path: Path, monkeypatch) -> None:
    job = _job(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("EVALLAB_RESULTS_HOME", str(home))
    report = process_job(job, ingest=False, pr_lookup=lambda _commit: None)
    assert report["results_home"] == str(home / "2026-09-30" / "HAR-117-har117-sample")
    assert "HAR-117" in (home / "INDEX.md").read_text()
    assert "None (no processed report)" in (home / "INDEX.md").read_text()


def test_backfill_skips_executor_and_old_cards(tmp_path: Path) -> None:
    trees = tmp_path / "worktrees"
    kept = _job(trees / "har117", "har117-kept")
    old = trees / "har80" / "runs" / "har80-old"
    old.mkdir(parents=True)
    (old / "config.json").write_text("{}", encoding="utf-8")
    (old / "result.json").write_text("{}", encoding="utf-8")
    staging = kept.parent / ".exec-stage" / "har117-staged"
    staging.mkdir(parents=True)
    (staging / "config.json").write_text("{}", encoding="utf-8")
    (staging / "result.json").write_text("{}", encoding="utf-8")

    report = backfill(home=tmp_path / "results", worktrees=trees, archive=tmp_path / "archive")
    assert report["published"] == 1
    assert report["by_card"] == {"HAR-117": 1}
    assert report["skipped"] == []
    assert os.listdir(tmp_path / "results" / "2026-09-30") == ["HAR-117-har117-kept"]


def test_index_regenerates_newest_first(tmp_path: Path) -> None:
    home = tmp_path / "results"
    older = _job(tmp_path / "a", "har90-old")
    newer = _job(tmp_path / "b", "har116-new")
    meta = json.loads((newer / "lab-metadata.json").read_text())
    meta["started_at"] = "2026-10-01T00:00:00+00:00"
    (newer / "lab-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    publish_job(older, root=home, pr_lookup=lambda _commit: None)
    publish_job(newer, root=home, pr_lookup=lambda _commit: None)
    lines = [
        line for line in (home / "INDEX.md").read_text().splitlines() if line.startswith("| HAR-")
    ]
    assert lines[0].startswith("| HAR-116")
    assert lines[1].startswith("| HAR-90")
    write_index(home)
    assert (home / "INDEX.md").read_text().count("| HAR-116") == 1
