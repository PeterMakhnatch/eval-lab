from __future__ import annotations

import json
import subprocess
from pathlib import Path

from evallab.steward import (
    Action,
    Git,
    Memory,
    Phase,
    StewardConfig,
    classify,
    decide,
    is_pure_merge,
    make_marker,
    parse_markers,
    parse_pull,
    parse_verdict,
    worktree_disposition,
)


def _pull(**overrides: object) -> object:
    raw: dict[str, object] = {
        "number": 100,
        "title": "feat: thing (HAR-9)",
        "body": "body",
        "author": {"login": "PeterMakhnatch"},
        "headRefOid": "a" * 40,
        "headRefName": "feat/thing",
        "baseRefName": "main",
        "isDraft": False,
        "labels": [],
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "updatedAt": "2026-09-18T00:00:00Z",
    }
    raw.update(overrides)
    return parse_pull(raw)


def _green_rollup(review: str | None = None) -> list[dict[str, object]]:
    rollup: list[dict[str, object]] = [
        {"__typename": "CheckRun", "name": "quality-required", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "typecheck-required", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    if review is not None:
        rollup.append({"__typename": "StatusContext", "context": "independent-review", "state": review})
    return rollup


CONFIG = StewardConfig()


# --- classification ----------------------------------------------------------


def test_draft_and_hold_and_foreign_base_are_skipped() -> None:
    assert classify(_pull(isDraft=True)) is Phase.DRAFT
    assert classify(_pull(labels=["steward:hold"])) is Phase.HELD
    assert classify(_pull(baseRefName="integrate/x")) is Phase.FOREIGN_BASE


def test_pending_checks_wait() -> None:
    pr = _pull(statusCheckRollup=[
        {"__typename": "CheckRun", "name": "quality-required", "status": "IN_PROGRESS", "conclusion": None},
    ])
    assert classify(pr) is Phase.CI_PENDING
    assert decide(pr, Phase.CI_PENDING, Memory(), 0.0, CONFIG).action is Action.WAIT


def test_missing_required_context_is_red_even_when_present_ones_pass() -> None:
    pr = _pull(statusCheckRollup=[
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ])
    assert classify(pr) is Phase.CI_RED
    assert decide(pr, Phase.CI_RED, Memory(), 0.0, CONFIG).action is Action.NOTIFY_CI_RED


def test_failed_check_is_red_and_names_the_check() -> None:
    pr = _pull(statusCheckRollup=_green_rollup()[:-1] + [
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
    ])
    assert classify(pr) is Phase.CI_RED
    decision = decide(pr, Phase.CI_RED, Memory(), 0.0, CONFIG)
    assert decision.action is Action.NOTIFY_CI_RED
    assert "lint (FAILURE)" in decision.reason


def test_skipped_and_pending_statuses_are_not_green() -> None:
    skipped = _pull(statusCheckRollup=[
        {"__typename": "CheckRun", "name": "quality-required", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "CheckRun", "name": "typecheck-required", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ])
    assert classify(skipped) is Phase.CI_RED


def test_green_without_review_awaits_review() -> None:
    pr = _pull(statusCheckRollup=_green_rollup())
    assert classify(pr) is Phase.AWAITING_REVIEW
    assert decide(pr, Phase.AWAITING_REVIEW, Memory(), 0.0, CONFIG).action is Action.REVIEW


def test_review_states_map_to_phases() -> None:
    assert classify(_pull(statusCheckRollup=_green_rollup("SUCCESS"))) is Phase.ELIGIBLE
    assert classify(_pull(statusCheckRollup=_green_rollup("FAILURE"))) is Phase.REVIEW_REJECTED
    assert classify(_pull(statusCheckRollup=_green_rollup("ERROR"))) is Phase.REVIEW_ERROR


def test_linear_id_extracted_from_title_branch_body() -> None:
    assert _pull().linear_id == "HAR-9"
    assert _pull(title="plain", headRefName="har-12/x").linear_id == "HAR-12"
    assert _pull(title="plain", headRefName="x", body="see HAR-30").linear_id == "HAR-30"
    assert _pull(title="plain", headRefName="x", body="").linear_id is None


# --- decisions ------------------------------------------------------------------


def test_eligible_clean_merges() -> None:
    pr = _pull(statusCheckRollup=_green_rollup("SUCCESS"))
    assert decide(pr, Phase.ELIGIBLE, Memory(), 0.0, CONFIG).action is Action.MERGE


def test_eligible_behind_updates_branch_then_respects_cooldown() -> None:
    pr = _pull(statusCheckRollup=_green_rollup("SUCCESS"), mergeStateStatus="BEHIND")
    memory = Memory()
    first = decide(pr, Phase.ELIGIBLE, memory, 1_000.0, CONFIG)
    assert first.action is Action.UPDATE_BRANCH
    memory.updated_branch["100"] = {"sha": pr.head_sha, "at": 1_000.0}
    within = decide(pr, Phase.ELIGIBLE, memory, 1_000.0 + CONFIG.update_branch_cooldown_seconds - 1, CONFIG)
    assert within.action is Action.WAIT
    after = decide(pr, Phase.ELIGIBLE, memory, 1_000.0 + CONFIG.update_branch_cooldown_seconds + 1, CONFIG)
    assert after.action is Action.UPDATE_BRANCH


def test_conflicting_pr_waits_with_notification() -> None:
    pr = _pull(statusCheckRollup=_green_rollup("SUCCESS"), mergeStateStatus="DIRTY")
    decision = decide(pr, Phase.ELIGIBLE, Memory(), 0.0, CONFIG)
    assert decision.action is Action.NOTIFY_CONFLICT


def test_review_error_backs_off_then_retries_until_limit() -> None:
    pr = _pull(statusCheckRollup=_green_rollup("ERROR"))
    memory = Memory()
    memory.review_attempts[f"100:{pr.head_sha}"] = {"count": 1, "last": 1_000.0}
    inside = decide(pr, Phase.REVIEW_ERROR, memory, 1_000.0 + CONFIG.review_error_backoff_seconds - 1, CONFIG)
    assert inside.action is Action.WAIT
    outside = decide(pr, Phase.REVIEW_ERROR, memory, 1_000.0 + CONFIG.review_error_backoff_seconds + 1, CONFIG)
    assert outside.action is Action.REVIEW
    memory.review_attempts[f"100:{pr.head_sha}"] = {"count": CONFIG.review_attempt_limit, "last": 1_000.0}
    exhausted = decide(pr, Phase.REVIEW_ERROR, memory, 9_999.0, CONFIG)
    assert exhausted.action is Action.WAIT
    assert "needs Peter" in exhausted.reason


def test_memory_roundtrip_and_pruning() -> None:
    memory = Memory()
    memory.record_approval(7, "b" * 40, 1.0, ["runtime=x", "workflow=y"])
    memory.record_approval(7, "c" * 40, 2.0, ["carry-forward"])
    memory.updated_branch["7"] = {"sha": "b" * 40, "at": 3.0}
    path = Path("/tmp/evallab-steward-test-state.json")
    memory.save(path)
    loaded = Memory.load(path)
    assert loaded.approved_heads(7) == ["b" * 40, "c" * 40]
    loaded.forget([8])
    assert loaded.approved_heads(7) == []
    path.unlink(missing_ok=True)


# --- verdicts -------------------------------------------------------------------


def test_verdict_parsing_and_blocking_rule() -> None:
    text = json.dumps({
        "verdict": "request_changes",
        "summary": "two real problems",
        "findings": [
            {"title": "A", "body": "b", "priority": 0, "confidence": 0.9},
            {"title": "B", "body": "b", "priority": 1, "confidence": 0.7},
            {"title": "C", "body": "b", "priority": 1, "confidence": 0.5},
            {"title": "D", "body": "b", "priority": 2, "confidence": 0.99},
        ],
    })
    verdict = parse_verdict(text, lens="runtime")
    assert verdict is not None and verdict.verdict == "request_changes"
    blocking = verdict.blocking(CONFIG)
    assert [f.title for f in blocking] == ["A", "B"]  # C lacks confidence, D is advisory


def test_malformed_verdicts_fail_closed() -> None:
    assert parse_verdict("not json", lens="runtime") is None
    assert parse_verdict('{"verdict":"maybe","summary":"x","findings":[]}', lens="runtime") is None
    assert parse_verdict('{"verdict":"approve","summary":"","findings":[]}', lens="runtime") is None
    bad_priority = '{"verdict":"approve","summary":"s","findings":[{"title":"t","body":"b","priority":9,"confidence":0.5}]}'
    assert parse_verdict(bad_priority, lens="runtime") is None


def test_request_changes_with_only_advisories_is_not_blocking() -> None:
    text = '{"verdict":"request_changes","summary":"nits","findings":[{"title":"t","body":"b","priority":2,"confidence":0.9}]}'
    verdict = parse_verdict(text, lens="workflow")
    assert verdict is not None
    assert verdict.blocking(CONFIG) == ()


# --- markers --------------------------------------------------------------------


def test_marker_roundtrip_across_comment_bodies() -> None:
    marker = make_marker(kind="review", head="d" * 40, verdict="approve")
    body = f"prefix\n{marker}\n**CI steward:** things"
    parsed = parse_markers([body, "unrelated comment", "broken <!-- ci-steward oops --> tail"])
    assert parsed == [{"kind": "review", "head": "d" * 40, "verdict": "approve"}]


# --- pure merge detection (real git) ----------------------------------------------


def _git_repo(path: Path) -> Git:
    def git(*argv: str) -> str:
        completed = subprocess.run(["git", "-C", str(path), *argv], capture_output=True, text=True, check=True)
        return completed.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    Path(path / "f.txt").write_text("base\n")
    git("add", "f.txt")
    git("commit", "-qm", "base")
    git("branch", "-M", "main")
    git("remote", "add", "origin", str(path))
    return Git(path)


def test_pure_merge_vs_edited_merge_vs_rebase(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    git = _git_repo(path)

    def sh(*argv: str) -> str:
        completed = subprocess.run(["git", "-C", str(path), *argv], capture_output=True, text=True, check=True)
        return completed.stdout.strip()

    # reviewed branch changes f.txt; main moves g.txt
    sh("switch", "-q", "-c", "pr")
    (path / "f.txt").write_text("branch\n")
    sh("add", "f.txt")
    sh("commit", "-qm", "pr work")
    reviewed = sh("rev-parse", "HEAD")

    sh("switch", "-q", "main")
    (path / "g.txt").write_text("main\n")
    sh("add", "g.txt")
    sh("commit", "-qm", "main moves on")
    main_tip = sh("rev-parse", "HEAD")

    # update remote-tracking so origin/main resolves
    sh("update-ref", "refs/remotes/origin/main", main_tip)

    sh("switch", "-q", "pr")
    sh("merge", "-q", "--no-edit", "main")
    pure_merge_head = sh("rev-parse", "HEAD")
    assert is_pure_merge(git, reviewed, pure_merge_head) is True

    # an edited merge (evil conflict resolution / extra tweak) must not carry
    (path / "f.txt").write_text("branch tweaked\n")
    sh("add", "f.txt")
    sh("commit", "-qm", "merge with edit")
    edited_head = sh("rev-parse", "HEAD")
    assert is_pure_merge(git, reviewed, edited_head) is False

    # a rebase produces a single-parent commit: never a carry-forward
    sh("switch", "-q", "-c", "rebased", reviewed)
    sh("rebase", "-q", "main")
    rebased_head = sh("rev-parse", "HEAD")
    assert is_pure_merge(git, reviewed, rebased_head) is False


# --- worktree policy ----------------------------------------------------------------


def test_worktree_disposition_tiers() -> None:
    base = dict(
        clean=True, locked=False, branch="feat/x", head_in_main=False,
        commit_age_days=30, mtime_age_days=30, open_pr=False, in_use=False, config=CONFIG,
    )
    assert worktree_disposition(**base)[0] == "remove"  # abandoned
    assert worktree_disposition(**{**base, "open_pr": True})[0] == "keep"
    assert worktree_disposition(**{**base, "clean": False})[0] == "keep"
    assert worktree_disposition(**{**base, "locked": True})[0] == "keep"
    assert worktree_disposition(**{**base, "in_use": True})[0] == "keep"
    assert worktree_disposition(**{**base, "commit_age_days": 2, "mtime_age_days": 2})[0] == "keep"
    assert worktree_disposition(**{**base, "head_in_main": True, "commit_age_days": 0})[0] == "remove"
    detached = {**base, "branch": None, "head_in_main": True}
    assert worktree_disposition(**detached)[0] == "remove"
    detached_fresh = {**base, "branch": None, "head_in_main": False}
    assert worktree_disposition(**detached_fresh)[0] == "keep"
