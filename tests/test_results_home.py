"""Results home: publish copies a finished job with the code that produced it."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

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


def _uncarded_job(root: Path) -> Path:
    job = _job(root, "ovn-g5-000169-stock", agent="terminus-2")
    (job / "experiment-spec.json").write_text(
        json.dumps({"question_ref": "ovn-g5"}), encoding="utf-8"
    )
    return job


def test_explicit_card_rehomes_unknown_and_survives_reprocessing(tmp_path: Path) -> None:
    job = _uncarded_job(tmp_path)
    home = tmp_path / "results"
    raw = {p.relative_to(job): p.read_bytes() for p in job.rglob("*") if p.is_file()}
    original = process_job(job, ingest=False, results_home=home, pr_lookup=lambda _: None)
    old = Path(original["results_home"])
    assert old.name == "unknown-ovn-g5-000169-stock"
    corrected = process_job(
        job, ingest=False, results_home=home, pr_lookup=lambda _: None,
        publication_card="HAR-126",
    )
    dest = Path(corrected["results_home"])
    assert dest.name == "HAR-126-ovn-g5-000169-stock"
    assert not old.exists()
    provenance = json.loads((dest / "provenance.json").read_text())
    assert provenance["card"] == "HAR-126"
    assert provenance["card_assignment"] == {"source": "publication_argument", "card": "HAR-126"}
    assert all((job / relative).read_bytes() == content for relative, content in raw.items())
    assert all((dest / relative).read_bytes() == content for relative, content in raw.items())
    assert original["trials"] == corrected["trials"]
    assert str(old) not in (home / "INDEX-all.md").read_text()

    again = process_job(job, ingest=False, results_home=home, pr_lookup=lambda _: None)
    assert again["results_home"] == str(dest)
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]
    before = (dest / "provenance.json").read_bytes()
    with pytest.raises(ValueError, match="existing source-bound assignment"):
        publish_job(job, root=home, pr_lookup=lambda _: None, publication_card="HAR-131")
    assert (dest / "provenance.json").read_bytes() == before


def test_card_rehome_keeps_same_name_from_another_source(tmp_path: Path) -> None:
    first = _uncarded_job(tmp_path / ".worktrees" / "one")
    second = _uncarded_job(tmp_path / ".worktrees" / "two")
    (second / "result.json").write_text('{"id": "other-job"}', encoding="utf-8")
    home = tmp_path / "results"
    publish_job(first, root=home, pr_lookup=lambda _: None)
    other = Path(publish_job(second, root=home, pr_lookup=lambda _: None)["published"])
    corrected = Path(publish_job(
        first, root=home, pr_lookup=lambda _: None, publication_card="HAR-126",
    )["published"])
    assert json.loads((other / "result.json").read_text())["id"] == "other-job"
    assert json.loads((corrected / "result.json").read_text())["id"] == "job-1"
    assert {p.name for p in other.parent.iterdir()} == {other.name, corrected.name}


def test_failed_card_rehome_keeps_original_publication(tmp_path: Path, monkeypatch) -> None:
    job = _uncarded_job(tmp_path)
    home = tmp_path / "results"
    old = Path(publish_job(job, root=home, pr_lookup=lambda _: None)["published"])
    before = (old / "result.json").read_bytes()

    def fail_copy(*args, **kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr("evallab.results_home._copy_tree", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        publish_job(job, root=home, pr_lookup=lambda _: None, publication_card="HAR-126")
    assert (old / "result.json").read_bytes() == before


@pytest.mark.parametrize("card", ["../HAR-126", "HAR-126"])
def test_explicit_card_cannot_escape_or_override_recorded_identity(tmp_path: Path, card: str) -> None:
    job = _job(tmp_path)
    home = tmp_path / "results"
    with pytest.raises(ValueError, match="Publication card"):
        publish_job(job, root=home, pr_lookup=lambda _: None, publication_card=card)
    assert not home.exists()


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
    assert "1 pass, 0 fail, 0 unscored" in (home / "INDEX-all.md").read_text()


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


def test_automatic_publish_shows_processed_reward_and_spend(tmp_path: Path) -> None:
    """The executor's call shape publishes a processed INDEX row, not 'unprocessed'.

    The tick calls ``process_job(job_dir, root=repo_root)`` with defaults, so
    this test uses the same shape (default output dir, publishing on). The
    job report must be written into ``processed/`` before the publish copies
    the tree, otherwise the published copy has no ``job.json`` and the INDEX
    row reads ``unprocessed | None (no processed report)`` (HAR-117 live:
    20 HAR-116 round-2 jobs).
    """
    import os

    from evallab.results_home import ENV_VAR

    job = _job(tmp_path, "har117-auto")
    report = process_job(job, root=tmp_path, ingest=False, pr_lookup=lambda _commit: None)
    home = Path(os.environ[ENV_VAR])
    published = Path(str(report["results_home"]))
    assert published.is_relative_to(home)
    assert (job / "processed" / "job.json").is_file()
    assert (published / "processed" / "job.json").is_file()
    full = (home / "INDEX-all.md").read_text()
    assert "1 pass, 0 fail, 0 unscored" in full
    assert "unprocessed" not in full


def _write_report(job: Path, summary: dict) -> None:
    processed = job / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "job.json").write_text(
        json.dumps({"schema": "process-job/v1", "job_name": job.name, "summary": summary}),
        encoding="utf-8",
    )


def test_index_shows_raw_pass_alongside_counted_exclusion(tmp_path: Path) -> None:
    """A raw pass excluded as copied_fix is visibly both: raw pass, counted excluded."""
    from evallab.results_home import _counts_summary

    job = _job(
        tmp_path,
        "har131-counted",
        agent="evallab.harbor_terminus:SecretSafeTerminus2",
        model="selfhosted/example",
    )
    _write_report(
        job,
        {
            "n_pass": 1,
            "n_fail": 0,
            "n_unscored": 0,
            "n_counted_pass": 0,
            "n_counted_fail": 0,
            "n_excluded": 1,
            "excluded_reasons": {"copied_fix": 1},
        },
    )
    home = tmp_path / "results"
    published = Path(
        publish_job(job, root=home, pr_lookup=lambda _commit: None)["published"]
    )
    assert _counts_summary(published) == (
        "0 counted pass, 0 counted fail, 1 excluded (copied_fix: 1)"
    )
    for name in ("INDEX.md", "INDEX-all.md"):
        text = (home / name).read_text()
        assert "1 pass, 0 fail, 0 unscored" in text
        assert "0 counted pass, 0 counted fail, 1 excluded (copied_fix: 1)" in text
    agent_header = next(
        line for line in (home / "INDEX.md").read_text().splitlines() if line.startswith("| Date")
    )
    agent_row = next(
        line
        for line in (home / "INDEX.md").read_text().splitlines()
        if "har131-counted" in line
    )
    assert agent_row.count("|") == agent_header.count("|")


def test_old_report_without_counts_stays_unknown_never_zero(tmp_path: Path) -> None:
    """Reports predating counts render unknown, never a fabricated 0."""
    from evallab.results_home import _counts_summary

    job = _job(tmp_path, "har117-legacy")
    _write_report(job, {"n_pass": 2, "n_fail": 1, "n_unscored": 0})
    home = tmp_path / "results"
    published = Path(
        publish_job(job, root=home, pr_lookup=lambda _commit: None)["published"]
    )
    assert _counts_summary(published) == "counts unknown"
    full = (home / "INDEX-all.md").read_text()
    assert "2 pass, 1 fail, 0 unscored" in full
    assert "counts unknown" in full
    row = next(line for line in full.splitlines() if "har117-legacy" in line)
    assert "0 counted" not in row


def test_counts_cell_matches_canonical_multi_trial_summary(tmp_path: Path) -> None:
    """The INDEX cell echoes the stored summary verbatim, reasons sorted."""
    from evallab.results_home import _counts_summary

    job = _job(tmp_path, "har117-multi")
    _write_report(
        job,
        {
            "n_pass": 2,
            "n_fail": 1,
            "n_unscored": 1,
            "n_counted_pass": 1,
            "n_counted_fail": 1,
            "n_excluded": 2,
            "excluded_reasons": {"infra": 1, "copied_fix": 1},
        },
    )
    home = tmp_path / "results"
    published = Path(
        publish_job(job, root=home, pr_lookup=lambda _commit: None)["published"]
    )
    assert _counts_summary(published) == (
        "1 counted pass, 1 counted fail, 2 excluded (copied_fix: 1, infra: 1)"
    )
    full = (home / "INDEX-all.md").read_text()
    assert "1 counted pass, 1 counted fail, 2 excluded (copied_fix: 1, infra: 1)" in full


def test_partial_counts_fields_stay_unknown(tmp_path: Path) -> None:
    """A summary missing any one counted field is unknown, not partial zeros."""
    from evallab.results_home import _counts_summary

    job = _job(tmp_path, "har117-partial")
    _write_report(
        job, {"n_pass": 1, "n_fail": 0, "n_unscored": 0, "n_counted_pass": 1}
    )
    home = tmp_path / "results"
    published = Path(
        publish_job(job, root=home, pr_lookup=lambda _commit: None)["published"]
    )
    assert _counts_summary(published) == "counts unknown"
    row = next(
        line
        for line in (home / "INDEX-all.md").read_text().splitlines()
        if "har117-partial" in line
    )
    assert "counts unknown" in row
    assert "1 counted pass" not in row


def test_custom_output_dir_publishes_fresh_report_not_stale_processed(tmp_path: Path) -> None:
    """A custom output_dir + publish shows the new outcome, not stale source processed/."""
    import shutil

    job = _job(tmp_path, "har117-custom-out")
    home = tmp_path / "results"

    first = process_job(job, ingest=False, pr_lookup=lambda _commit: None, results_home=home)
    assert Path(str(first["results_home"])) == home / "2026-09-30" / "HAR-117-har117-custom-out"
    old_source = json.loads((job / "processed" / "job.json").read_text())
    assert (old_source["summary"]["n_pass"], old_source["summary"]["n_fail"]) == (1, 0)
    (job / "processed" / "trial-obsolete.json").write_text('{"reward": 1.0}', encoding="utf-8")

    # New raw outcome: the trial now fails. No manual mirroring into processed/.
    (job / "trial-one" / "result.json").write_text(
        json.dumps({"trial_name": "trial-one", "verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )
    custom = tmp_path / "custom-out"
    custom.mkdir()
    (custom / "scratch.txt").write_text("analyst scratch, not a report page", encoding="utf-8")
    second = process_job(
        job, output_dir=custom, ingest=False, pr_lookup=lambda _commit: None, results_home=home
    )
    published = Path(str(second["results_home"]))
    assert published == Path(str(first["results_home"]))

    fresh = json.loads((published / "processed" / "job.json").read_text())
    assert (fresh["summary"]["n_pass"], fresh["summary"]["n_fail"]) == (0, 1)
    fresh_trial = json.loads((published / "processed" / "trial-trial-one.json").read_text())
    assert fresh_trial["reward"] == 0.0
    assert "0 pass, 1 fail, 0 unscored" in (home / "INDEX-all.md").read_text(encoding="utf-8")

    # The stale source processed/ is untouched, raw inputs are byte-identical,
    # and unrelated out-dir contents never reach the published tree.
    stale = json.loads((job / "processed" / "job.json").read_text())
    assert (stale["summary"]["n_pass"], stale["summary"]["n_fail"]) == (1, 0)
    assert (published / "result.json").read_bytes() == (job / "result.json").read_bytes()
    assert not (published / "processed" / "scratch.txt").exists()
    assert not (published / "scratch.txt").exists()
    assert not (published / "processed" / "trial-obsolete.json").exists()

    # Without any source processed/, the fresh custom report still publishes.
    shutil.rmtree(job / "processed")
    (job / "trial-one" / "result.json").write_text(
        json.dumps({"trial_name": "trial-one", "verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    third = process_job(
        job,
        output_dir=tmp_path / "custom-out-2",
        ingest=False,
        pr_lookup=lambda _commit: None,
        results_home=home,
    )
    republished = Path(str(third["results_home"]))
    assert republished == published
    latest = json.loads((republished / "processed" / "job.json").read_text())
    assert (latest["summary"]["n_pass"], latest["summary"]["n_fail"]) == (1, 0)
    full = (home / "INDEX-all.md").read_text(encoding="utf-8")
    assert "1 pass, 0 fail, 0 unscored" in full
    assert "0 pass, 1 fail, 0 unscored" not in full

    # Explicit bad inputs cannot silently fall back to an older snapshot.
    for invalid in (tmp_path / "missing-reports", republished / "processed"):
        with pytest.raises(ValueError):
            publish_job(
                job, root=home, processed_report_root=invalid, pr_lookup=lambda _commit: None
            )
        still_published = json.loads((republished / "processed" / "job.json").read_text())
        assert still_published["summary"]["n_pass"] == 1

    # Reject a live publication as output before writing a different outcome into it.
    published_before = (republished / "processed" / "job.json").read_bytes()
    (job / "trial-one" / "result.json").write_text(
        json.dumps({"trial_name": "trial-one", "verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        process_job(
            job,
            output_dir=republished / "processed",
            ingest=False,
            pr_lookup=lambda _commit: None,
            results_home=home,
        )
    assert (republished / "processed" / "job.json").read_bytes() == published_before


def _spend_job(root: Path, name: str = "har131-session") -> Path:
    """Job whose native id/spec/commit bind it to the test session receipt."""
    job = _job(root, name)
    metadata = json.loads((job / "lab-metadata.json").read_text(encoding="utf-8"))
    metadata["repository"]["commit"] = "abc123def456"
    experiment = metadata.get("experiment") or {}
    experiment["spec_id"] = "spec-A"
    metadata["experiment"] = experiment
    (job / "lab-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    spec_path = job / "experiment-spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["spec_id"] = "spec-A"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return job


def _session_receipt(job: Path, receipt_path: Path, *, daytona_a: float | None = 0.1) -> Path:
    """Two-member unequal-weight session receipt binding ``job`` as spec-A."""
    metadata_sha = hashlib.sha256((job / "lab-metadata.json").read_bytes()).hexdigest()
    receipt = {
        "schema": "evallab.session_spend/v1",
        "sessions": [
            {
                "session_id": "ap-test123",
                "billing_rows": [
                    {
                        "object_id": "ap-test123",
                        "description": "evallab-test-app",
                        "environment": "main",
                        "interval_start": "2026-10-01T00:00:00Z",
                        "resource": "GPU",
                        "cost_usd": 1.2,
                        "resolution": "d",
                        "reported_at": "2026-10-01T02:23:10Z",
                    }
                ],
                "deployment": {"commit": "abc123def456", "time_deployed": "2026-10-01T00:01:27Z"},
                "teardown": {
                    "app": "evallab-test-app",
                    "recorded_at": "2026-10-01T01:45:38Z",
                    "completed_spec_ids": ["spec-A", "spec-B"],
                },
                "members": [
                    {
                        "job_id": "job-1",
                        "job_name": job.name,
                        "spec_id": "spec-A",
                        "repository_commit": "abc123def456",
                        "lab_metadata_sha256": metadata_sha,
                        "trial_wall_seconds": 100.0,
                        "daytona_estimate_usd": daytona_a,
                    },
                    {
                        "job_id": "job-2",
                        "job_name": "other-job",
                        "spec_id": "spec-B",
                        "repository_commit": "abc123def456",
                        "lab_metadata_sha256": "0" * 64,
                        "trial_wall_seconds": 300.0,
                        "daytona_estimate_usd": 0.2,
                    },
                ],
            }
        ],
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt_path


def test_session_spend_allocation_replaces_legacy_spend_in_report_and_index(
    tmp_path: Path,
) -> None:
    """An explicit receipt stores the real allocation and prefers it on pages + INDEX."""
    from evallab.spend_day import session_spend_for_job

    job = _spend_job(tmp_path)
    home = tmp_path / "results"
    receipt = _session_receipt(job, tmp_path / "receipt.json")
    expected = session_spend_for_job(job.resolve(), receipt)
    assert expected["modal_allocated_usd"] == pytest.approx(0.3)
    assert expected["daytona_estimate_usd"] == 0.1
    assert expected["total_usd"] == pytest.approx(0.4)

    baseline = process_job(job, output_dir=tmp_path / "base-out", ingest=False, publish=False)
    report = process_job(
        job,
        ingest=False,
        pr_lookup=lambda _commit: None,
        results_home=home,
        session_spend=receipt,
    )
    assert report["summary"]["session_spend"] == expected
    # Settled proxy cost/tokens are untouched by the allocation.
    assert report["summary"]["cost_usd"] == baseline["summary"]["cost_usd"]
    assert report["summary"]["tokens_used"] == baseline["summary"]["tokens_used"]
    published = Path(str(report["results_home"]))
    saved = json.loads((published / "processed" / "job.json").read_text(encoding="utf-8"))
    assert saved["summary"]["session_spend"] == expected
    # Job-scope only: the allocation never becomes a per-trial GPU share.
    saved_trial = json.loads(
        (published / "processed" / "trial-trial-one.json").read_text(encoding="utf-8")
    )
    assert "session_spend" not in saved_trial
    markdown = (published / "processed" / "job.md").read_text(encoding="utf-8")
    assert "$0.4000" in markdown
    assert "$0.3000" in markdown
    assert "$0.4000" in (home / "INDEX-all.md").read_text(encoding="utf-8")

    # Unknown Daytona renders the GPU share plus unknown sandbox, never a
    # full total and never the legacy wall-time estimate.
    unknown_receipt = _session_receipt(job, tmp_path / "receipt-unknown.json", daytona_a=None)
    rerun = process_job(
        job,
        ingest=False,
        pr_lookup=lambda _commit: None,
        results_home=home,
        session_spend=unknown_receipt,
    )
    assert rerun["summary"]["session_spend"]["total_usd"] is None
    assert rerun["summary"]["session_spend"]["daytona_estimate_usd"] is None
    assert rerun["summary"]["session_spend"]["reason"] is not None
    full = (home / "INDEX-all.md").read_text(encoding="utf-8")
    row = next(line for line in full.splitlines() if "har131-session" in line)
    assert "$0.3000" in row and "unknown" in row
    assert "$0.4000" not in full
    remarked = (published / "processed" / "job.md").read_text(encoding="utf-8")
    assert "$0.3000" in remarked
    assert "$0.4000" not in remarked


def test_invalid_session_spend_receipt_preserves_existing_publication(
    tmp_path: Path,
) -> None:
    """A stale receipt fails before the previous publication is replaced."""
    job = _spend_job(tmp_path, "har131-stale")
    home = tmp_path / "results"
    first = process_job(job, ingest=False, pr_lookup=lambda _commit: None, results_home=home)
    published = Path(str(first["results_home"]))
    baseline_report = (published / "processed" / "job.json").read_bytes()
    baseline_index = (home / "INDEX-all.md").read_bytes()
    baseline_source = (job / "processed" / "job.json").read_bytes()
    assert "session_spend" not in json.loads(baseline_report.decode())["summary"]

    stale = _session_receipt(job, tmp_path / "stale-receipt.json")
    payload = json.loads(stale.read_text(encoding="utf-8"))
    payload["sessions"][0]["members"][0]["repository_commit"] = "stale000commit"
    stale.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        process_job(
            job,
            ingest=False,
            pr_lookup=lambda _commit: None,
            results_home=home,
            session_spend=stale,
        )
    assert (published / "processed" / "job.json").read_bytes() == baseline_report
    assert (home / "INDEX-all.md").read_bytes() == baseline_index
    assert (job / "processed" / "job.json").read_bytes() == baseline_source


def test_process_job_without_receipt_leaves_spend_untouched(tmp_path: Path) -> None:
    """Default runs keep the legacy spend path and never touch raw inputs."""
    job = _job(tmp_path, "har131-default")
    raw = [
        job / "result.json",
        job / "config.json",
        job / "lab-metadata.json",
        job / "trial-one" / "result.json",
    ]
    before = [path.read_bytes() for path in raw]
    report = process_job(job, output_dir=tmp_path / "out", ingest=False, publish=False)
    assert "session_spend" not in report["summary"]
    for path, content in zip(raw, before, strict=True):
        assert path.read_bytes() == content


def test_corrupt_retained_card_cannot_escape_results_home(tmp_path: Path) -> None:
    job = _uncarded_job(tmp_path)
    home = tmp_path / "results"
    published = Path(publish_job(
        job, root=home, pr_lookup=lambda _: None, publication_card="HAR-126",
    )["published"])
    provenance_path = published / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["card"] = "../../outside"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    before = provenance_path.read_bytes()
    with pytest.raises(ValueError, match="Invalid existing source-bound publication card"):
        publish_job(job, root=home, pr_lookup=lambda _: None)
    assert provenance_path.read_bytes() == before
