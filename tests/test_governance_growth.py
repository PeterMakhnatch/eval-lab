import json
import subprocess
from pathlib import Path

import pytest

from evallab.governance_growth import BUDGET_METRICS, BUDGETS, LEDGER, growth_issues


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def put(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(root, "add", "--", path)


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


def test_local_scope_counts_committed_staged_and_unstaged_but_not_scratch(repo: Path) -> None:
    put(repo, "tests/committed.py", "# line\n" * 300)
    commit(repo)
    put(repo, "tests/staged.py", "# line\n" * 300)
    (repo / "tests/committed.py").write_text("# line\n" * 400)
    (repo / "tests/untracked.py").write_text("# line\n" * 301)
    assert growth_issues(repo) == []
    git(repo, "add", "tests/untracked.py")
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


def test_unborn_repository_enforces_staged_file_limit(repo: Path) -> None:
    unborn = repo / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q", "-b", "main")
    put(unborn, "tests/new.py", "# line\n" * 600)
    assert growth_issues(unborn) == []
    put(unborn, "tests/new.py", "# line\n" * 601)
    assert "new-file: tests/new.py has 601 lines" in growth_issues(unborn)[0]


def budgets(root: Path, overrides: dict[str, int], *, metadata: bool = True) -> None:
    rows = ["[budgets]"]
    for key in BUDGET_METRICS:
        fields = f"limit = {overrides.get(key, 1000000)}"
        if metadata:
            fields += ', reason = "Required consumer behavior", raised_by = "Fixture PR"'
        rows.append(f'{json.dumps(key)} = {{ {fields} }}')
    put(root, BUDGETS, "\n".join(rows) + "\n")


@pytest.mark.parametrize(
    ("key", "path", "initial", "added", "cap", "total", "suggested"),
    [
        ("python-lines:src/evallab", "src/evallab/a.py", "", "# a\n# final", 1, 2, 3),
        ("python-lines:tests", "tests/a.py", "", "# a\n" * 51, 50, 51, 53),
        ("python-lines:research", "research/a.py", "", "# a\n# b\n", 1, 2, 3),
        ("python-lines:library", "library/a.py", "", "# a\n# b\n", 1, 2, 3),
        ("markdown-files:docs", "docs/a.md", "", "# A\n", 0, 1, 2),
        ("markdown-files:agents", "agents/a.md", "", "# A\n", 0, 1, 2),
        ("experiment-directories:research/experiments", "research/experiments/a/result.json", "", "{}\n", 0, 1, 2),
        ("large-python-files:src/evallab", "src/evallab/a.py", "# a\n" * 800, "# a\n" * 801, 0, 1, 1),
        ("large-python-files:tests", "tests/a.py", "# a\n" * 800, "# a\n" * 801, 0, 1, 1),
    ],
)
def test_each_total_budget_pass_message_and_exact_raise(
    repo: Path, key: str, path: str, initial: str, added: str, cap: int, total: int, suggested: int,
) -> None:
    budgets(repo, {key: cap})
    if initial:
        put(repo, path, initial)
    baseline = commit(repo)
    assert growth_issues(repo, baseline) == []
    put(repo, path, added)
    name, unit = BUDGET_METRICS[key]
    delta = 1 if initial else total
    replacement = (
        f'{json.dumps(key)} = {{ limit = {suggested}, reason = "State the concrete need", '
        'raised_by = "Your name or PR" }'
    )
    if key.startswith("large-python-files:"):
        explanation = "limit = total; no slack because large-file counts should only fall"
    elif key.startswith("python-lines:"):
        explanation = "limit = total + 2% (rounded up) to cover later PR growth"
    else:
        explanation = "limit = total + 2% (rounded up, at least 1 item) to cover later PR growth"
    assert growth_issues(repo, baseline) == [
        f"total-budget: {name} has {total} {unit}; budget {cap}; "
        f"this PR contributes {delta:+d}; remove {total - cap} {unit}, "
        f"or replace its line in {BUDGETS}: {replacement} ({explanation}; replace both placeholders)"
    ]
    rows = (repo / BUDGETS).read_text().splitlines()
    rows = [replacement if row.startswith(json.dumps(key)) else row for row in rows]
    fixed = "\n".join(rows).replace("State the concrete need", "Required API contract")
    put(repo, BUDGETS, fixed.replace("Your name or PR", "Fixture PR") + "\n")
    assert growth_issues(repo, baseline) == []
    put(repo, BUDGETS, (repo / BUDGETS).read_text().replace(
        f"limit = {suggested},", f"limit = {total},",
    ))
    assert growth_issues(repo, baseline) == []


def test_printed_raise_covers_later_pr_without_another_budget_edit(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "python-lines:tests"
    budgets(repo, {key: 99})
    put(repo, "tests/seed.py", "# seed\n" * 99)
    baseline = commit(repo)
    put(repo, "tests/a.py", "# A\n")
    issues = growth_issues(repo, baseline)
    assert len(issues) == 1
    replacement = issues[0].split(f"or replace its line in {BUDGETS}: ", 1)[1].split(" (limit =", 1)[0]
    replacement = replacement.replace("State the concrete need", "Required API contract").replace(
        "Your name or PR", "PR A",
    )
    rows = (repo / BUDGETS).read_text().splitlines()
    put(repo, BUDGETS, "\n".join(
        replacement if row.startswith(json.dumps(key)) else row for row in rows
    ) + "\n")
    assert growth_issues(repo, baseline) == []
    head_a = commit(repo)
    budget_blob = git(repo, "rev-parse", f"HEAD:{BUDGETS}")
    git(repo, "branch", "-f", "main", head_a)
    git(repo, "checkout", "-qb", "second", head_a)
    put(repo, "tests/b.py", "# B\n" * 2)
    head_b = commit(repo)
    event = repo / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "head": {"sha": head_b}, "base": {"sha": head_a},
    }}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert git(repo, "rev-parse", f"HEAD:{BUDGETS}") == budget_blob
    assert growth_issues(repo) == []


def test_budget_raise_needs_reason_and_author_but_lowering_does_not(repo: Path) -> None:
    budgets(repo, {"python-lines:tests": 3})
    baseline = commit(repo)
    budgets(repo, {"python-lines:tests": 4}, metadata=False)
    issues = growth_issues(repo, baseline)
    assert len(issues) == 1
    assert "raise for python-lines:tests from 3 to 4 requires a concrete reason and raised_by" in issues[0]
    budgets(repo, {"python-lines:tests": 2}, metadata=False)
    assert growth_issues(repo, baseline) == []


@pytest.mark.parametrize("field", ["reason", "raised_by"])
def test_each_raise_attribution_field_is_required(repo: Path, field: str) -> None:
    budgets(repo, {"python-lines:tests": 1})
    baseline = commit(repo)
    budgets(repo, {"python-lines:tests": 2})
    text = (repo / BUDGETS).read_text()
    value = "Required consumer behavior" if field == "reason" else "Fixture PR"
    put(repo, BUDGETS, text.replace(f'{field} = "{value}"', f'{field} = ""'))
    assert "requires a concrete reason and raised_by" in growth_issues(repo, baseline)[0]


@pytest.mark.parametrize("content", ["[budgets\n", "[other]\n", '[budgets]\n"python-lines:tests" = {limit = true}\n'])
def test_malformed_or_missing_caps_fail_closed(repo: Path, content: str) -> None:
    budgets(repo, {})
    baseline = commit(repo)
    put(repo, BUDGETS, content)
    issues = growth_issues(repo, baseline)
    assert any(BUDGETS in issue for issue in issues)
    assert any("invalid or missing" in issue or "nonnegative integer limit" in issue for issue in issues)
    (repo / BUDGETS).unlink()
    assert growth_issues(repo, baseline) == [
        f"{BUDGETS}: budget file removed; restore all total-size budgets"
    ]


def test_push_reports_budget_excess_without_failing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    budgets(repo, {"python-lines:tests": 1})
    baseline = commit(repo)
    put(repo, "tests/a.py", "# a\n# b\n")
    commit(repo)
    event = repo / "event.json"
    event.write_text(json.dumps({"before": baseline}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert growth_issues(repo) == []
    report = capsys.readouterr().out
    assert "report-only: total-budget: tests has 2 Python lines; budget 1; this PR contributes +2" in report
    put(repo, BUDGETS, "[invalid\n")
    assert growth_issues(repo) == []
    assert "report-only:" in capsys.readouterr().out


def test_over_budget_shrinking_and_untouched_roots_always_pass(
    repo: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    budgets(repo, {"python-lines:tests": 1})
    put(repo, "tests/a.py", "# a\n" * 3)
    baseline = commit(repo)
    put(repo, "tests/a.py", "# a\n" * 2)
    assert growth_issues(repo, baseline) == []
    assert "budget 1; this PR contributes -1" in capsys.readouterr().out
    put(repo, "tests/a.py", "# a\n" * 3)
    put(repo, "src/evallab/a.py", "# new\n")
    assert growth_issues(repo, baseline) == []
    assert "budget 1; this PR contributes +0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("key", "directory", "suffix"),
    [
        ("python-lines:tests", "tests", ".py"),
        ("markdown-files:docs", "docs", ".md"),
        ("experiment-directories:research/experiments", "research/experiments", "/result.json"),
    ],
)
def test_two_prs_fit_alone_but_later_growing_merge_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch, key: str, directory: str, suffix: str,
) -> None:
    budgets(repo, {key: 2})
    put(repo, f"{directory}/seed{suffix}", "# seed\n")
    baseline = commit(repo)
    git(repo, "branch", "-f", "main", baseline)
    put(repo, f"{directory}/a{suffix}", "# A\n")
    head_a = commit(repo)
    assert growth_issues(repo, baseline) == []
    git(repo, "branch", "-f", "main", head_a)
    git(repo, "checkout", "-qb", "second", baseline)
    put(repo, f"{directory}/b{suffix}", "# B\n")
    head_b = commit(repo)
    assert growth_issues(repo, baseline) == []
    git(repo, "merge", "--no-ff", "-qm", "tested merge", "main")
    event = repo / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "head": {"sha": head_b}, "base": {"sha": head_a},
    }}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    issues = growth_issues(repo)
    assert len(issues) == 1
    assert f"{BUDGET_METRICS[key][0]} has 3 {BUDGET_METRICS[key][1]}; budget 2; this PR contributes +1" in issues[0]


def test_existing_experiment_and_untracked_files_do_not_consume_count_slack(repo: Path) -> None:
    key = "experiment-directories:research/experiments"
    budgets(repo, {key: 1, "markdown-files:docs": 0})
    put(repo, "research/experiments/a/seed.json", "{}\n")
    baseline = commit(repo)
    put(repo, "research/experiments/a/nested/new.json", "{}\n")
    put(repo, "research/experiments/README.md", "# Experiments\n")
    (repo / "docs").mkdir()
    (repo / "docs/scratch.md").write_text("# Scratch\n")
    assert growth_issues(repo, baseline) == []


def test_each_budget_raise_needs_its_own_reason(repo: Path) -> None:
    budgets(repo, {"python-lines:tests": 1})
    baseline = commit(repo)
    budgets(repo, {"python-lines:tests": 2})
    issues = growth_issues(repo, baseline)
    assert len(issues) == 1
    assert "use a new reason for this raise" in issues[0]
    put(repo, BUDGETS, (repo / BUDGETS).read_text().replace(
        "Required consumer behavior", "Add the required parsing cases",
    ))
    assert growth_issues(repo, baseline) == []


@pytest.mark.parametrize("source", ["empty-baseline", "head", "pr-base"])
def test_tracked_budget_cannot_disappear_when_ancestor_predates_policy(
    repo: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    ancestor = git(repo, "rev-parse", "HEAD")
    if source == "empty-baseline":
        put(repo, BUDGETS, "")
        ancestor = commit(repo)
    elif source == "head":
        budgets(repo, {})
        commit(repo)
    else:
        put(repo, "tests/topic.py", "# Topic\n")
        head = commit(repo)
        git(repo, "checkout", "-q", "main")
        budgets(repo, {})
        main = commit(repo)
        git(repo, "checkout", "-q", "topic")
        git(repo, "merge", "--no-ff", "--no-commit", "main")
    (repo / BUDGETS).unlink()
    git(repo, "add", "--", BUDGETS)
    if source == "pr-base":
        commit(repo)
        event = repo / "event.json"
        event.write_text(json.dumps({"pull_request": {
            "head": {"sha": head}, "base": {"sha": main},
        }}))
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert growth_issues(repo, ancestor if source != "pr-base" else None) == [
        f"{BUDGETS}: budget file removed; restore all total-size budgets"
    ]


def test_concurrent_base_raise_applies_to_older_topic_without_false_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets(repo, {"python-lines:tests": 1})
    put(repo, "tests/seed.py", "# Seed\n")
    baseline = commit(repo)
    git(repo, "branch", "-f", "main", baseline)
    put(repo, "tests/topic.py", "# Topic\n")
    head = commit(repo)
    git(repo, "checkout", "-q", "main")
    budgets(repo, {"python-lines:tests": 3})
    put(repo, BUDGETS, (repo / BUDGETS).read_text().replace(
        "Required consumer behavior", "Keep the required concurrent contract",
    ))
    put(repo, "tests/concurrent.py", "# Concurrent\n")
    main = commit(repo)
    git(repo, "checkout", "-q", "topic")
    git(repo, "merge", "--no-ff", "-qm", "tested merge", "main")
    event = repo / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "head": {"sha": head}, "base": {"sha": main},
    }}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert growth_issues(repo) == []
