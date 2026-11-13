import json
import subprocess
from pathlib import Path

import pytest

from evallab.governance_growth import LEDGER, growth_issues


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def put(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def commit(root: Path) -> str:
    git(root, "add", "--", ".")
    git(root, "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "commit.gpgsign", "false")
    put(tmp_path, "seed", "fixture\n")
    head = commit(tmp_path)
    git(tmp_path, "update-ref", "refs/remotes/origin/main", head)
    git(tmp_path, "checkout", "-qb", "topic")
    return tmp_path


@pytest.mark.parametrize(
    ("rule", "path", "files", "initial", "actual", "limit", "unit"),
    [
        (
            "root-growth", "tests", {"tests/a.py": 501, "tests/b.py": 500},
            {}, 1001, 1000, "net lines",
        ),
        (
            "new-file", "src/evallab/new.py", {"src/evallab/new.py": 601},
            {}, 601, 600, "lines",
        ),
        (
            "large-file-growth", "tests/large.py", {"tests/large.py": 8002},
            {"tests/large.py": 8001}, 1, 0, "net lines",
        ),
    ],
)
def test_each_ratchet_pass_failure_and_ledger_escape(
    repo: Path, rule: str, path: str, files: dict[str, int], initial: dict[str, int],
    actual: int, limit: int, unit: str,
) -> None:
    for name, lines in initial.items():
        put(repo, name, "# line\n" * lines)
    baseline = commit(repo) if initial else git(repo, "rev-parse", "HEAD")
    assert growth_issues(repo, baseline) == []
    for name, lines in files.items():
        put(repo, name, "# line\n" * lines)
    entry = {"rule": rule, "path": path, "limit": actual, "reason": "State the concrete need"}
    line = json.dumps(entry, separators=(",", ":"))
    expected = (
        f"{rule}: {path} has {actual} {unit}; limit {limit}; "
        f"remove {actual - limit} {unit}, or append to {LEDGER}: {line} "
        "(replace the reason with the concrete need)"
    )
    assert growth_issues(repo, baseline) == [expected]
    entry["reason"] = "Keep the independently required consumer contract in this change"
    put(repo, LEDGER, json.dumps(entry) + "\n")
    assert growth_issues(repo, baseline) == []


def test_old_ledger_entry_cannot_authorize_later_growth(repo: Path) -> None:
    entry = {"rule": "new-file", "path": "tests/new.py", "limit": 700, "reason": "Earlier PR"}
    put(repo, LEDGER, json.dumps(entry) + "\n")
    baseline = commit(repo)
    put(repo, "tests/new.py", "# line\n" * 650)
    assert len(growth_issues(repo, baseline)) == 1
    entry["reason"] = "This PR needs an additional contract"
    put(repo, LEDGER, json.dumps(entry) + "\n")
    assert growth_issues(repo, baseline) == []


def test_local_scope_counts_committed_staged_unstaged_and_untracked(repo: Path) -> None:
    put(repo, "tests/committed.py", "# line\n" * 300)
    commit(repo)
    put(repo, "tests/staged.py", "# line\n" * 300)
    git(repo, "add", "tests/staged.py")
    put(repo, "tests/committed.py", "# line\n" * 400)
    put(repo, "tests/untracked.py", "# line\n" * 301)
    issues = growth_issues(repo)
    assert len(issues) == 1
    assert "root-growth: tests has 1001 net lines; limit 1000; remove 1 net lines" in issues[0]


def test_deletion_offsets_root_growth_and_rename_is_a_new_file(repo: Path) -> None:
    put(repo, "tests/old.py", "# line\n" * 700)
    baseline = commit(repo)
    git(repo, "mv", "tests/old.py", "tests/moved.py")
    issues = growth_issues(repo, baseline)
    assert len(issues) == 1
    assert "new-file: tests/moved.py has 700 lines; limit 600; remove 100 lines" in issues[0]
    (repo / "tests/moved.py").unlink()
    assert growth_issues(repo, baseline) == []


def test_unterminated_last_line_counts_and_limits_are_inclusive(repo: Path) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    put(repo, "tests/new.py", "# line\n" * 600)
    assert growth_issues(repo, baseline) == []
    put(repo, "tests/new.py", "# line\n" * 600 + "# final")
    assert "has 601 lines; limit 600" in growth_issues(repo, baseline)[0]


def test_research_uses_its_own_root_and_file_limits(repo: Path) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    for name in ("a", "b"):
        put(repo, f"research/{name}.py", "# line\n" * 1000)
    assert growth_issues(repo, baseline) == []
    put(repo, "research/c.py", "# final")
    assert "root-growth: research has 2001 net lines; limit 2000" in growth_issues(repo, baseline)[0]


def test_pr_event_excludes_concurrent_base_changes_and_uses_exact_head(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    put(repo, "tests/topic.py", "# topic\n" * 500)
    head = commit(repo)
    git(repo, "checkout", "-q", "main")
    put(repo, "tests/concurrent.py", "# concurrent\n" * 700)
    main = commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", main)
    git(repo, "checkout", "-q", "topic")
    assert growth_issues(repo) == []
    git(repo, "merge", "--no-ff", "-qm", "synthetic merge", "main")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": head}, "base": {"sha": main}}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert growth_issues(repo) == []


def test_push_event_checks_change_even_when_origin_main_equals_head(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    put(repo, "tests/new.py", "# line\n" * 601)
    head = commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": baseline}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert "new-file: tests/new.py has 601 lines" in growth_issues(repo)[0]


def test_missing_history_fails_closed_with_preparation_command(repo: Path, tmp_path: Path) -> None:
    put(repo, "seed", "second commit\n")
    commit(repo)
    clone = tmp_path / "shallow"
    git(repo, "clone", "-q", "--depth", "1", "--branch", "topic", repo.as_uri(), str(clone))
    issues = growth_issues(clone)
    assert len(issues) == 1
    assert "Python growth scope unavailable (1 error)" in issues[0]
    assert "git fetch origin main --unshallow" in issues[0]
    assert "CI lint checkout needs `fetch-depth: 0`" in issues[0]


@pytest.mark.parametrize("entry", ["not json", "{}", '{"rule":"new-file","path":"tests/a.py","limit":true,"reason":"x"}'])
def test_invalid_ledger_names_exact_line_and_edit(repo: Path, entry: str) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    put(repo, LEDGER, "\n" + entry + "\n")
    assert growth_issues(repo, baseline) == [
        f"{LEDGER}:2: 1 invalid ledger entry; replace line 2 with "
        '{"rule":"root-growth","path":"tests","limit":1000,'
        '"reason":"State the concrete need"} or delete that line'
    ]


def test_topic_at_origin_main_uses_head_but_main_and_detached_use_parent(repo: Path) -> None:
    put(repo, "tests/previous.py", "# line\n" * 601)
    head = commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    assert growth_issues(repo) == []
    git(repo, "branch", "-f", "main", head)
    git(repo, "checkout", "-q", "main")
    assert "new-file: tests/previous.py has 601 lines" in growth_issues(repo)[0]
    git(repo, "checkout", "-q", "--detach", head)
    assert "new-file: tests/previous.py has 601 lines" in growth_issues(repo)[0]


def test_invalid_utf8_is_a_ledger_error_not_a_history_error(repo: Path) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    put(repo, LEDGER, "")
    (repo / LEDGER).write_bytes(b"\xff\n")
    issues = growth_issues(repo, baseline)
    assert len(issues) == 1
    assert issues[0].startswith(f"{LEDGER}:1: 1 invalid ledger entry; replace line 1")
    assert "git fetch" not in issues[0]


def test_unborn_repository_enforces_untracked_file_limit(repo: Path) -> None:
    unborn = repo / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q", "-b", "main")
    put(unborn, "tests/new.py", "# line\n" * 600)
    assert growth_issues(unborn) == []
    put(unborn, "tests/new.py", "# line\n" * 601)
    assert "new-file: tests/new.py has 601 lines" in growth_issues(unborn)[0]
