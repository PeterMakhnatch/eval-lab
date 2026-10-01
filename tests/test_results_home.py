"""Results home: publish copies a finished job with the code that produced it."""

from __future__ import annotations

import json
import os
from pathlib import Path

from evallab.process_job import process_job
from evallab.results_home import backfill, publish_job, write_index


def _job(
    root: Path, name: str = "har117-sample", *, agent: str | None = None, model: str | None = None
) -> Path:
    job = root / "runs" / name
    job.mkdir(parents=True)
    if agent is not None or model is not None:
        job_config: dict[str, object] = {"agents": [{"name": agent or "nop", "model_name": model}]}
    else:
        job_config = {}
    (job / "config.json").write_text(json.dumps(job_config), encoding="utf-8")
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
    (research / "RESULTS.md").write_text("results", encoding="utf-8")

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
    # No recorded agent, so the job collapses to one routine line for its card.
    assert sum(line.startswith("| HAR-117") for line in index.splitlines()) == 1
    assert "**uncommitted code**" in index
    full = (home / "INDEX-all.md").read_text()
    assert sum(line.startswith("| HAR-117") for line in full.splitlines()) == 1
    assert "RESULTS.md" in full
    assert "`abc123def456`" in full


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
    assert "None (no processed report)" in (home / "INDEX-all.md").read_text()


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


def test_index_lists_agent_runs_first_newest_first(tmp_path: Path) -> None:
    home = tmp_path / "results"
    agent = "evallab.harbor_terminus:SecretSafeTerminus2"
    older = _job(tmp_path / "a", "har90-old", agent=agent, model="selfhosted/example")
    newer = _job(tmp_path / "b", "har116-new", agent=agent, model="selfhosted/example")
    control = _job(tmp_path / "c", "har116-nop-1", agent="nop")
    meta = json.loads((newer / "lab-metadata.json").read_text())
    meta["started_at"] = "2026-10-01T00:00:00+00:00"
    (newer / "lab-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    for job in (older, newer, control):
        publish_job(job, root=home, pr_lookup=lambda _commit: None)
    text = (home / "INDEX.md").read_text()
    agent_section = text.split("## Agent runs")[1].split("## Routine")[0]
    rows = [line for line in agent_section.splitlines() if line.startswith("| 202")]
    assert len(rows) == 2
    assert "har116-new" in rows[0]
    assert "har90-old" in rows[1]
    assert "SecretSafeTerminus2" in rows[0]
    # The control job collapses to one line for its card, after the agent runs.
    assert "| HAR-116 | 1 |" in text.split("## Routine")[1]
    assert "har116-nop-1" not in text
    full = (home / "INDEX-all.md").read_text()
    assert "har116-nop-1" in full
    assert sum("har116-new" in line for line in full.splitlines()) == 1
    write_index(home)
    index = (home / "INDEX.md").read_text()
    assert sum("har116-new" in line for line in index.splitlines()) == 1


def test_plain_process_job_call_never_writes_the_real_home(tmp_path: Path) -> None:
    """A ``process_job`` call without ``publish=False`` stays inside the test."""
    import os

    from evallab.results_home import DEFAULT_ROOT, ENV_VAR, results_root

    assert ENV_VAR in os.environ  # the autouse conftest fixture redirects publishing
    assert Path(results_root()).is_relative_to(tmp_path)
    job = _job(tmp_path)
    report = process_job(job, ingest=False, pr_lookup=lambda _commit: None)
    assert Path(str(report["results_home"])).is_relative_to(tmp_path)
    assert not str(report["results_home"]).startswith(str(DEFAULT_ROOT))


def test_agent_run_comes_from_records_not_name_heuristics(tmp_path: Path) -> None:
    from evallab.results_home import _agent_model, is_agent_run

    published = tmp_path / "job"
    published.mkdir()
    (published / "config.json").write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "dryrun_rlm_agent:DryRunRlmAgent",
                        "model_name": "zai-coding-plan/glm-5.3-flash",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # An old backfilled job with no recorded command still resolves via config.
    agent, model = _agent_model(published, {"command": None, "model": None})
    assert agent == "dryrun_rlm_agent:DryRunRlmAgent"
    assert model == "zai-coding-plan/glm-5.3-flash"
    assert is_agent_run(agent) is True
    assert is_agent_run("nop") is False
    assert is_agent_run("oracle") is False
    assert is_agent_run("evallab.harbor_terminus:SecretSafeTerminus2") is True
    assert is_agent_run(None) is False


def test_research_lookup_ignores_deep_trees(tmp_path: Path, monkeypatch) -> None:
    from pathlib import Path as _Path

    from evallab.results_home import _research_docs

    checkout = tmp_path / "checkout"
    experiment = checkout / "research" / "experiments" / "har117-results-home"
    experiment.mkdir(parents=True)
    (experiment / "RESULTS.md").write_text("results", encoding="utf-8")
    # Decoy slug matches hidden where the old unbounded rglob used to look.
    deep = checkout / "research" / "evidence" / "runs" / "deep"
    deep.mkdir(parents=True)
    (deep / "har117-results-home-notes.md").write_text("decoy", encoding="utf-8")
    vendor = checkout / "research" / "external" / "harbor-ecosystem" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "har117-vendor.md").write_text("decoy", encoding="utf-8")

    def _no_rglob(self: _Path, *args: object, **kwargs: object) -> object:
        raise AssertionError("INDEX lookup must never rglob the checkout")

    monkeypatch.setattr(_Path, "rglob", _no_rglob)
    assert _research_docs(checkout) == {"HAR-117": str(experiment / "RESULTS.md")}


def test_write_index_never_descends_into_jobs_or_checkout(tmp_path: Path, monkeypatch) -> None:
    from pathlib import Path as _Path

    home = tmp_path / "results"
    agent = "evallab.harbor_terminus:SecretSafeTerminus2"
    job = _job(tmp_path / "src", "har117-agent-1", agent=agent, model="selfhosted/example")
    checkout = tmp_path / "checkout"
    experiment = checkout / "research" / "experiments" / "har117-results-home"
    experiment.mkdir(parents=True)
    (experiment / "SUMMARY.md").write_text("summary", encoding="utf-8")
    publish_job(job, root=home, pr_lookup=lambda _commit: None, primary_checkout=checkout)

    def _no_rglob(self: _Path, *args: object, **kwargs: object) -> object:
        raise AssertionError("INDEX regeneration must never rglob")

    monkeypatch.setattr(_Path, "rglob", _no_rglob)
    write_index(home, primary_checkout=checkout)
    text = (home / "INDEX.md").read_text()
    assert "har117-agent-1" in text
    assert "SUMMARY.md" in text
