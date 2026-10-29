"""Verdict contract of `evallab worktrees` over a real fixture repository.

Git runs for real against a temporary repository with host configuration
isolated; the GitHub side is injected as parsed PR records.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.worktree_disposition import (
    PR_LIMIT,
    Disposition,
    PrRecord,
    collect_report,
    fetch_pr_records,
    render_json,
    render_markdown,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return res.stdout.strip()


def commit(cwd: Path, name: str, text: str = "x\n") -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-m", f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


def pr(number: int, state: str, branch: str, head: str, base: str = "main") -> PrRecord:
    return PrRecord(
        number=number,
        state=state,  # type: ignore[arg-type]
        head_ref=branch,
        head_sha=head,
        base_ref=base,
        merged_at="2026-10-01T00:00:00Z" if state == "MERGED" else None,
        merge_commit=None,
    )


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """One primary checkout plus a linked worktree for every disposition case."""
    for key, value in {
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-b", "main")
    (primary / ".gitignore").write_text("runs/\n.worktrees/\n", encoding="utf-8")
    git(primary, "add", ".gitignore")
    git(primary, "commit", "-m", "root")

    wt = primary / ".worktrees"
    wt.mkdir()
    trees: dict[str, Path] = {"primary": primary}

    def add(name: str, *extra: str) -> Path:
        path = wt / name
        git(primary, "worktree", "add", *extra, str(path))
        trees[name] = path
        return path

    add("ancestor", "-b", "ancestor")
    add("locked", "-b", "locked")
    git(primary, "worktree", "lock", "--reason", "initializing", str(trees["locked"]))
    add("dirty", "-b", "dirty")
    (trees["dirty"] / "scratch.py").write_text("draft\n", encoding="utf-8")
    add("evidence", "-b", "evidence")
    (trees["evidence"] / "runs").mkdir()
    (trees["evidence"] / "runs" / "job.json").write_text("{}\n", encoding="utf-8")
    add("openpr", "-b", "openpr")

    add("unique", "-b", "unique")
    commit(trees["unique"], "unique.txt")
    add("reuse", "-b", "reuse")
    commit(trees["reuse"], "reuse.txt")

    add("squash", "-b", "squash")
    commit(trees["squash"], "s1.txt")
    commit(trees["squash"], "s2.txt")
    add("picked", "-b", "picked")
    commit(trees["picked"], "picked.txt", "picked\n")
    add("detached", "--detach")
    commit(trees["detached"], "detached.txt")
    add("gone", "-b", "gone")

    # main advances: a squash of `squash` (no graph ancestry) and a cherry-pick of `picked`.
    git(primary, "merge", "--squash", "squash")
    git(primary, "commit", "-m", "squash of squash")
    git(primary, "cherry-pick", git(trees["picked"], "rev-parse", "HEAD"))
    shutil.rmtree(trees["gone"])
    return trees


def rows(report_rows: tuple[Disposition, ...]) -> dict[str, Disposition]:
    return {row.facts.path.name: row for row in report_rows}


def head_of(trees: dict[str, Path], name: str) -> str:
    return git(trees[name], "rev-parse", "HEAD")


def test_verdicts_follow_recorded_facts(estate: dict[str, Path]) -> None:
    primary = estate["primary"]
    prs = [
        pr(1, "MERGED", "squash", head_of(estate, "squash")),
        pr(2, "OPEN", "openpr", head_of(estate, "openpr")),
        pr(3, "MERGED", "reuse", "0" * 40),
        pr(4, "MERGED", "unique", head_of(estate, "unique"), base="integrate/x"),
    ]
    report = collect_report(primary, base_ref="main", pr_records=prs, now=NOW)
    by_name = rows(report.rows)

    verdicts = {name: row.verdict for name, row in by_name.items()}
    assert verdicts == {
        "primary": "delivered",
        "ancestor": "delivered",
        "locked": "delivered",
        "dirty": "blocked",
        "evidence": "delivered",
        "openpr": "delivered",
        "unique": "unique-unmerged",
        "reuse": "unique-unmerged",
        "squash": "delivered",
        "picked": "delivered",
        "detached": "blocked",
        "gone": "blocked",
    }
    assert by_name["ancestor"].facts.proof == "ancestry"
    assert by_name["picked"].facts.proof == "patch-equivalent"
    assert by_name["squash"].facts.proof == "merged-pr"
    assert by_name["squash"].pr is not None and by_name["squash"].pr_head_match
    assert by_name["reuse"].pr is not None and not by_name["reuse"].pr_head_match
    assert by_name["unique"].facts.undelivered == 1
    assert by_name["gone"].facts.exists is False
    assert by_name["dirty"].facts.untracked == 1

    # Blockers that never change a verdict but forbid retirement.
    assert any("locked" in item for item in by_name["locked"].blockers)
    assert by_name["openpr"].open_prs == (2,)
    assert any("open PR #2" in item for item in by_name["openpr"].blockers)
    assert by_name["evidence"].facts.ignored_evidence == {"runs": 1}
    assert any("runs/" in item for item in by_name["evidence"].blockers)
    assert "primary checkout" in by_name["primary"].blockers
    assert by_name["ancestor"].blockers == ()


def test_squash_without_pr_state_is_not_claimed_delivered(estate: dict[str, Path]) -> None:
    report = collect_report(estate["primary"], base_ref="main", pr_records=None, now=NOW)
    by_name = rows(report.rows)
    assert report.pr_state == "unavailable"
    assert by_name["squash"].verdict == "unique-unmerged"
    assert by_name["picked"].verdict == "delivered"


def test_report_is_read_only_and_deterministic(estate: dict[str, Path]) -> None:
    primary = estate["primary"]

    def snapshot() -> tuple[str, str, dict[str, str]]:
        files = {
            str(path.relative_to(primary)): path.read_text(encoding="utf-8")
            for path in sorted((primary / ".worktrees").rglob("*"))
            if path.is_file() and ".git" not in path.name
        }
        return (
            git(primary, "for-each-ref"),
            git(primary, "worktree", "list", "--porcelain"),
            files,
        )

    before = snapshot()
    first = collect_report(primary, base_ref="main", pr_records=[], now=NOW)
    second = collect_report(primary, base_ref="main", pr_records=[], now=NOW)
    assert snapshot() == before
    assert render_json(first) == render_json(second)
    assert render_markdown(first, home=primary) == render_markdown(second, home=primary)
    payload = json.loads(render_json(first))
    assert payload["generated_at"] == "2026-10-29T12:00:00+00:00"
    assert payload["base_commit"] == git(primary, "rev-parse", "main")
    assert payload["worktree_count"] == 12


def test_cli_writes_both_artifacts(
    estate: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code = run_cli(
        [
            "worktrees",
            "--base",
            "main",
            "--no-pr",
            "--json-out",
            str(out / "r.json"),
            "--markdown-out",
            str(out / "r.md"),
        ],
        workspace=estate["primary"],
    )
    assert code == 0
    assert capsys.readouterr().out == ""
    payload = json.loads((out / "r.json").read_text(encoding="utf-8"))
    assert payload["pr_state"] == "unavailable"
    assert payload["verdict_counts"]["unique-unmerged"] >= 1
    assert "## Unique-unmerged" in (out / "r.md").read_text(encoding="utf-8")


def test_truncated_or_failed_pr_listing_is_unavailable() -> None:
    row = {"number": 1, "state": "OPEN", "headRefName": "b", "headRefOid": "a" * 40}
    assert fetch_pr_records(lambda: json.dumps([row])) is not None
    assert fetch_pr_records(lambda: json.dumps([row] * PR_LIMIT)) is None
    assert fetch_pr_records(lambda: "not json") is None

    def failing() -> str:
        raise RuntimeError("gh unavailable")

    assert fetch_pr_records(failing) is None
