from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from evallab.insights import sources
from evallab.insights.config import EngineConfig, RepoMirror, SourceFamily, SourceOverride
from evallab.insights.models import (
    CheckResult,
    ClaimRecord,
    ExtractedClaim,
    claim_fingerprint,
    source_identity,
)
from evallab.insights.sources import (
    read_source_text,
    refresh_mirror,
    scan,
    section_text,
    split_sections,
)
from evallab.insights.store import InsightStore

NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)


def _config(path: Path, **fields: object) -> EngineConfig:
    values: dict[str, object] = {
        "family": "reports", "kind": "report", "globs": [str(path)],
    }
    values.update(fields)
    return EngineConfig(sources=[SourceFamily.model_validate(values)])


def _stamp(path: Path, when: datetime = NOW) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_rescan_keeps_ids_and_only_edited_claim_goes_stale(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_text("# First\n21 of 28 worked.\n# Second\nNothing changed here.\n")
    config = _config(path)
    with InsightStore(tmp_path / "state.sqlite") as store:
        initial = scan(store, config, now=NOW)
        assert initial.sources_changed == 1
        sections = sorted(store.pending_sections(), key=lambda section: section.start_line)
        assert len(sections) == 2
        records = []
        for index, section in enumerate(sections):
            claim = ExtractedClaim(
                local_id=str(index), experiment="EXP1", statement="A source-backed statement.",
                kind="qualitative", quote="A source-backed statement.",
                section_id=section.section_id,
            )
            records.append(ClaimRecord(
                claim_id=f"C-{index}", source_id=section.source_id,
                section_sha256=section.sha256, fingerprint=claim_fingerprint(claim),
                pass_id="P-1", extracted_by="fixture", created_at=NOW,
                claim=claim, check=CheckResult(status="pass"),
            ))
        store.insert_claims(records)
        store.mark_sections([section.section_id for section in sections], "extracted")
        assert scan(store, config, now=NOW).sources_changed == 0
        assert not store.pending_sections()
        path.write_text("# First\n22 of 28 worked.\n# Second\nNothing changed here.\n")
        result = scan(store, config, now=NOW)
        assert result.sections_pending == 1
        assert store.pending_sections()[0].section_id == sections[0].section_id
        first, second = store.get_claim("C-0"), store.get_claim("C-1")
        assert first is not None and first.stale
        assert second is not None and not second.stale
        assert store.get_section(sections[1].section_id) is not None


def test_et_times_inherit_and_convert_with_path_date() -> None:
    text = (
        "Preamble.\n# Update 03:25 ET\nA result.\n## Results\nMore results.\n"
        "### 11:15 AM ET\nLater.\n# Update (2026-10-27, 5:14 PM ET)\nEven later.\n"
    )
    sections = split_sections(
        text, source_id="F-test", as_of_default=NOW, date_hint=date(2026, 10, 27),
    )
    assert sections[0].as_of == NOW
    assert sections[1].as_of == datetime(2026, 10, 27, 7, 25, tzinfo=UTC)
    assert sections[2].as_of == sections[1].as_of
    assert sections[2].heading == "Update 03:25 ET > Results"
    assert sections[3].as_of == datetime(2026, 10, 27, 15, 15, tzinfo=UTC)
    assert sections[4].as_of == datetime(2026, 10, 27, 21, 14, tzinfo=UTC)


def test_time_only_headings_roll_past_midnight_and_date_only_resets() -> None:
    text = (
        "# Log 2026-11-01\n## 11:30 PM ET\nBefore midnight.\n"
        "## 12:40 AM ET\nAfter midnight.\n## 2026-11-02\nDate reset.\n"
        "## 01:10 AM ET\nOn the reset date.\n"
    )
    sections = split_sections(
        text, source_id="F-test", as_of_default=NOW, date_hint=None,
    )
    assert sections[1].as_of == datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
    assert sections[2].as_of == datetime(2026, 11, 2, 5, 40, tzinfo=UTC)
    assert sections[3].as_of == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert sections[4].as_of == datetime(2026, 11, 2, 6, 10, tzinfo=UTC)


def test_winter_time_and_invalid_time_are_safe() -> None:
    sections = split_sections(
        "# Update 12:15 AM ET\nBody.\n# Update 29:99 ET\nBody.\n",
        source_id="F-test", as_of_default=NOW, date_hint=date(2026, 12, 1),
    )
    assert sections[0].as_of == datetime(2026, 12, 1, 5, 15, tzinfo=UTC)
    assert sections[1].as_of == sections[0].as_of


def test_deeper_and_fenced_headings_stay_inside_sections() -> None:
    text = "# Real\n#### Deep\n```md\n# Not a section\n```\n## Next\nText.\n"
    sections = split_sections(text, source_id="F-test", as_of_default=NOW, date_hint=None)
    assert len(sections) == 2
    # Line 5 closes the fence; "## Next" on line 6 starts the second section.
    assert sections[0].start_line == 1 and sections[0].end_line == 5
    assert sections[1].start_line == 6
    assert sections[1].heading == "Real > Next"


def test_fence_closer_must_be_bare() -> None:
    text = "# Real\n```\n```python\n# Still code\n```\n## Next\nText.\n"
    sections = split_sections(text, source_id="F-test", as_of_default=NOW, date_hint=None)
    assert [section.heading for section in sections] == ["Real", "Real > Next"]
    assert sections[0].end_line == 5
    assert sections[1].start_line == 6


def test_heading_only_strips_space_delimited_closing_hashes() -> None:
    sections = split_sections(
        "# Notes on C#\nBody.\n# Closing ###\nBody.\n",
        source_id="F-test", as_of_default=NOW, date_hint=None,
    )
    assert [section.heading for section in sections] == ["Notes on C#", "Closing"]


def test_long_sections_split_without_losing_lines() -> None:
    text = "# Long\n" + ("x" * 3000 + "\n\n") * 10
    sections = split_sections(text, source_id="F-test", as_of_default=NOW, date_hint=None)
    assert len(sections) > 1
    assert len({section.section_id for section in sections}) == len(sections)
    lines = text.splitlines(keepends=True)
    assert "".join("".join(lines[s.start_line - 1:s.end_line]) for s in sections) == text
    repeated = split_sections(text, source_id="F-test", as_of_default=NOW, date_hint=None)
    assert [s.section_id for s in repeated] == [s.section_id for s in sections]


def test_blank_line_free_table_splits_only_at_row_boundaries() -> None:
    rows = [f"| {index} | {'x' * 200} |\n" for index in range(200)]
    text = "# Table\n" + "".join(rows)
    sections = split_sections(text, source_id="F-test", as_of_default=NOW, date_hint=None)
    assert len(sections) > 1
    lines = text.splitlines(keepends=True)
    assert "".join("".join(lines[s.start_line - 1:s.end_line]) for s in sections) == text
    assert all(lines[section.end_line - 1].endswith("|\n") for section in sections[:-1])
    repeated = split_sections(text, source_id="F-test", as_of_default=NOW, date_hint=None)
    assert [section.section_id for section in repeated] == [section.section_id for section in sections]


def test_override_freezes_lines_and_overrides_glob(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_text("# First\nFrozen.\n# Later\nDo not read.\n")
    config = _config(path)
    config.overrides = [SourceOverride(path=str(path), family="frozen", kind="report", max_line=2)]
    with InsightStore(tmp_path / "state.sqlite") as store:
        scan(store, config, now=NOW)
        source = store.list_sources()[0]
        assert source.source_id == source_identity(str(path.resolve()))
        assert source.family == "frozen"
        assert read_source_text(source) == "# First\nFrozen.\n"
        assert source.sha256 == hashlib.sha256(b"# First\nFrozen.\n").hexdigest()
        section = store.pending_sections()[0]
        assert section_text(source, section) == "# First\nFrozen.\n"
        path.write_text("# First\nFrozen.\n# Later\nA changed future.\n")
        assert scan(store, config, now=NOW).sources_changed == 0


def test_json_flattening_is_one_stable_bounded_section(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    payload = {"z": [1, True, None], "a/b": {"~": "value"}, "many": list(range(55))}
    path.write_text(json.dumps(payload))
    with InsightStore(tmp_path / "state.sqlite") as store:
        scan(store, _config(path, kind="receipt"), now=NOW)
        source = store.list_sources()[0]
        text = read_source_text(source)
        assert text.startswith('/a~1b/~0 = "value"')
        assert "/z/1 = true" in text and "/z/2 = null" in text
        assert "/many = ... 5 more" in text
        assert "/many/50" not in text
        sections = store.pending_sections()
        assert len(sections) == 1
        assert section_text(source, sections[0]) == text


def test_jsonl_limits_rows_and_reports_total_count(tmp_path: Path) -> None:
    path = tmp_path / "receipt.jsonl"
    path.write_text("\n".join(json.dumps({"k": index}) for index in range(35)))
    with InsightStore(tmp_path / "state.sqlite") as store:
        scan(store, _config(path), now=NOW)
        text = read_source_text(store.list_sources()[0])
        assert "/29/k = 29" in text
        assert "/30/k" not in text
        assert "/row_count = 35" in text
        assert len(store.pending_sections()) == 1


def test_truncated_jsonl_counts_only_complete_rows_and_marks_cut(tmp_path: Path) -> None:
    path = tmp_path / "receipt.jsonl"
    path.write_text("\n".join(json.dumps({"index": index}) for index in range(100)) + "\n")
    with InsightStore(tmp_path / "state.sqlite") as store:
        result = scan(store, _config(path, max_bytes=1000), now=NOW)
        assert result.refused == {"oversize": 1}
        prefix = path.read_bytes()[:1000]
        complete = prefix.splitlines(keepends=True)
        assert not complete[-1].endswith(b"\n")
        complete.pop()
        expected = sum(bool(line.strip()) for line in complete)
        assert expected > 30
        text = read_source_text(store.list_sources()[0])
        assert f"/row_count = >= {expected} (truncated)" in text


def test_excludes_age_and_recursive_deduplication(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    for name in ("keep", "exclude", "old"):
        path = nested / f"{name}.md"
        path.write_text(f"# {name}\nBody.\n")
        _stamp(path, NOW - timedelta(days=3) if name == "old" else NOW)
    config = _config(tmp_path / "**/*.md", exclude=["exclude.md"], max_age_days=1)
    config.sources[0].globs.append(str(nested / "keep.md"))
    with InsightStore(tmp_path / "state.sqlite") as store:
        result = scan(store, config, now=NOW)
        assert result.sources_seen == 2
        assert result.refused == {"too_old": 1}
        assert len(store.list_sources()) == 1


def test_expands_environment_variables_and_path_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "2026-10-27-report.md"
    path.write_text("# Update 03:25 ET\nBody.\n")
    monkeypatch.setenv("INSIGHT_TEST_ROOT", str(tmp_path))
    config = _config(Path("$INSIGHT_TEST_ROOT/*.md"))
    with InsightStore(tmp_path / "state.sqlite") as store:
        scan(store, config, now=NOW)
        assert store.pending_sections()[0].as_of == datetime(2026, 10, 27, 7, 25, tzinfo=UTC)


def test_git_dates_override_checkout_mtime_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    current = mirror / "new.md"
    old = mirror / "old.md"
    for path in (current, old):
        path.write_text("# Results\nBody.\n")
        _stamp(path, NOW)
    calls: list[Path] = []

    def dates(root: Path) -> dict[str, datetime]:
        calls.append(root)
        return {str(current): NOW, str(old): NOW - timedelta(days=50)}

    monkeypatch.setattr(sources, "git_dates", dates)
    config = _config(mirror / "*.md", max_age_days=2)
    config.repo_mirror = RepoMirror(repo=str(tmp_path), path=str(mirror))
    with InsightStore(tmp_path / "state.sqlite") as store:
        result = scan(store, config, now=NOW)
        assert result.refused == {"too_old": 1}
        assert [source.path for source in store.list_sources()] == [str(current)]
        assert calls == [mirror]


@pytest.mark.parametrize("exists", [False, True])
def test_refresh_mirror_only_updates_designated_worktree(tmp_path: Path, exists: bool) -> None:
    repo, mirror = tmp_path / "repo", tmp_path / "mirror"
    repo.mkdir()
    if exists:
        mirror.mkdir()
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        calls.append(command)
        stdout = f"{tmp_path / 'gitdir'}\n" if "--absolute-git-dir" in command else "abc123\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    assert refresh_mirror(repo, mirror, run=run) == "abc123"
    expected = (
        [["git", "-C", str(repo), "fetch", "-q", "origin", "main"],
         ["git", "-C", str(mirror), "rev-parse", "--absolute-git-dir"],
         ["git", "-C", str(mirror), "checkout", "-q", "-f", "--detach", "origin/main"]]
        if exists else
        [["git", "-C", str(repo), "worktree", "add", "--detach", str(mirror), "origin/main"]]
    )
    assert calls == [*expected, ["git", "-C", str(mirror), "rev-parse", "HEAD"]]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize("lock_age_seconds", [None, 3600, 5])
def test_refresh_mirror_recovers_from_a_killed_checkout(
    tmp_path: Path, lock_age_seconds: int | None
) -> None:
    # A checkout killed at its deadline leaves index.lock and half-written files;
    # the next refresh must still land on origin/main unless a live git holds the lock.
    origin, repo, mirror = tmp_path / "origin", tmp_path / "repo", tmp_path / "mirror"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "RESULTS.md").write_text("old\n")
    _git(origin, "add", "RESULTS.md")
    _git(origin, "commit", "-q", "-m", "old")
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    refresh_mirror(repo, mirror)
    (origin / "RESULTS.md").write_text("new\n")
    _git(origin, "commit", "-q", "-am", "new")
    (mirror / "RESULTS.md").write_text("new\n")  # written before the kill
    lock = Path(_git(mirror, "rev-parse", "--absolute-git-dir")) / "index.lock"
    if lock_age_seconds is not None:
        lock.touch()
        stamp = datetime.now().timestamp() - lock_age_seconds
        os.utime(lock, (stamp, stamp))

    if lock_age_seconds is not None and lock_age_seconds < sources.STALE_INDEX_LOCK_SECONDS:
        with pytest.raises(subprocess.CalledProcessError):
            refresh_mirror(repo, mirror)
        assert lock.exists()
        return
    head = refresh_mirror(repo, mirror)
    assert head == _git(repo, "rev-parse", "origin/main")
    assert not lock.exists()
    assert _git(mirror, "status", "--porcelain") == ""


def test_oversize_prefix_hash_and_unreadable_count(tmp_path: Path) -> None:
    path = tmp_path / "report.md"
    path.write_bytes(b"# First\nSome data beyond the byte cap.\n")
    config = _config(path, max_bytes=8)
    config.overrides = [
        SourceOverride(path=str(tmp_path / "missing.md"), family="missing", kind="report"),
    ]
    with InsightStore(tmp_path / "state.sqlite") as store:
        result = scan(store, config, now=NOW)
        assert result.refused == {"oversize": 1, "unreadable": 1}
        source = store.list_sources()[0]
        assert read_source_text(source) == "# First\n"
        assert source.sha256 == hashlib.sha256(b"# First\n").hexdigest()
        assert source.size == path.stat().st_size


def test_invalid_json_is_not_indexed(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text('{"broken":')
    with InsightStore(tmp_path / "state.sqlite") as store:
        assert scan(store, _config(path), now=NOW).refused == {"invalid_json": 1}
        assert not store.list_sources()


def test_git_dates_reads_history_once_and_keeps_latest_file_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        output = (
            "\x002026-11-02T12:00:00-05:00\n\nnew.md\nshared.md\n"
            "\x002026-10-01T12:00:00-04:00\n\nold.md\nshared.md\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(sources.subprocess, "run", run)
    dates = sources.git_dates(tmp_path)
    assert calls == [
        ["git", "-C", str(tmp_path), "log", "--name-only", "--format=%x00%cI"],
    ]
    assert dates[str(tmp_path / "shared.md")] == datetime(2026, 11, 2, 17, tzinfo=UTC)
    assert dates[str(tmp_path / "old.md")] == datetime(2026, 10, 1, 16, tzinfo=UTC)


def test_tilde_expansion_and_underscore_path_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "report_2026-10-27_update.md"
    path.write_text("# Update 03:25 ET\nBody.\n")
    config = _config(Path("~/report_*.md"))
    with InsightStore(tmp_path / "state.sqlite") as store:
        scan(store, config, now=NOW)
        assert store.list_sources()[0].path == str(path)
        assert store.pending_sections()[0].as_of == datetime(2026, 10, 27, 7, 25, tzinfo=UTC)


def test_truncated_json_is_refused_and_oversize_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text('{"value": "longer than the bounded prefix"}')
    with InsightStore(tmp_path / "state.sqlite") as store:
        result = scan(store, _config(path, max_bytes=10), now=NOW)
        assert result.refused == {"invalid_json": 1, "oversize": 1}
        assert not store.list_sources()
