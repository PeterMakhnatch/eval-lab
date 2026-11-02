"""PR review orchestration is synthetic, head-pinned, and fails closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import review_pr  # noqa: E402

HEAD = "a" * 40
BASE = "b" * 40
OTHER_HEAD = "c" * 40
PR_NUMBER = 42
PASS_REVIEW = """# Independent review

## Blocking
None

## Should-fix
None

## Nit
None

VERDICT: PASS
"""
BLOCK_REVIEW = PASS_REVIEW.replace(
    "## Blocking\nNone", "## Blocking\n- `example.py:3`: Synthetic correctness defect."
).replace("VERDICT: PASS", "VERDICT: BLOCK")


@dataclass
class ReviewHarness:
    repo: Path
    out: Path
    worktree_root: Path
    commands: list[tuple[list[str], Path]] = field(default_factory=list)
    processes: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    review: str | None = PASS_REVIEW
    fetched_head: str = HEAD
    current_head: str = HEAD
    dirty: str = ""
    returncode: int = 0
    process_error: Exception | None = None
    cleanup_error: Exception | None = None
    session_mode: str = "valid"
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "number": PR_NUMBER,
            "headRefOid": HEAD,
            "baseRefOid": BASE,
            "url": f"https://example.invalid/owner/repo/pull/{PR_NUMBER}",
        }
    )

    @property
    def checkout(self) -> Path:
        return self.worktree_root / "checkout"

    def command(self, argv: list[str], cwd: Path) -> str:
        self.commands.append((list(argv), Path(cwd)))
        if argv == [
            "gh", "pr", "view", str(PR_NUMBER), "--json",
            "number,headRefOid,baseRefOid,url",
        ]:
            assert cwd == self.repo
            return json.dumps(self.metadata)
        if argv == ["git", "fetch", "origin", f"refs/pull/{PR_NUMBER}/head"]:
            assert cwd == self.repo
            return ""
        if argv == ["git", "rev-parse", "FETCH_HEAD"]:
            assert cwd == self.repo
            return self.fetched_head
        if argv == ["git", "fetch", "origin", BASE]:
            assert cwd == self.repo
            return ""
        if argv == ["git", "worktree", "add", "--detach", str(self.checkout), HEAD]:
            assert cwd == self.repo
            self.events.append("worktree_add")
            self.checkout.mkdir()
            for rule in review_pr.RULE_PATHS:
                path = self.checkout / rule
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"Synthetic repository rule: {rule}\n", encoding="utf-8")
            return ""
        if argv == ["git", "diff", f"{BASE}...{HEAD}"]:
            assert cwd == self.checkout
            return "diff --git a/example.py b/example.py\n+synthetic_change = True\n"
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            assert cwd == self.checkout
            return self.dirty
        if argv == ["gh", "pr", "view", str(PR_NUMBER), "--json", "headRefOid"]:
            assert cwd == self.repo
            self.events.append("head_recheck")
            return json.dumps({"headRefOid": self.current_head})
        if argv == ["git", "worktree", "remove", str(self.checkout)]:
            assert cwd == self.repo
            self.events.append("worktree_remove")
            if self.cleanup_error is not None:
                raise self.cleanup_error
            shutil.rmtree(self.checkout)
            return ""
        if argv == ["gh", "pr", "comment", str(PR_NUMBER), "--body-file", str(self.out / "comment.md")]:
            assert cwd == self.repo
            self.events.append("comment")
            assert (self.out / "comment.md").is_file()
            return "https://example.invalid/comment/1"
        raise AssertionError(f"Unexpected command: {argv!r}")

    def process(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv[0] == "omp", "Only the fake reviewer may use subprocess.run"
        self.processes.append((list(argv), kwargs))
        self.events.append("reviewer")
        if self.process_error is not None:
            raise self.process_error
        sessions = Path(argv[argv.index("--session-dir") + 1])
        assert sessions == self.out / "sessions"
        assert sessions.is_dir()
        if self.review is not None:
            (sessions.parent / "REVIEW.md").write_text(self.review, encoding="utf-8")
        if self.session_mode != "missing":
            route = argv[argv.index("--model") + 1].removesuffix(":thinking")
            provider, model = route.split("/", 1)
            message: dict[str, Any] = {
                "role": "assistant", "provider": provider, "model": model, "stopReason": "stop",
            }
            record: dict[str, Any] = {"type": "message", "message": message}
            if self.session_mode == "fallback":
                record["resolvedModelIsFallback"] = True
            elif self.session_mode == "mismatch":
                message["model"] = "unexpected-model"
            elif self.session_mode == "aborted":
                message["stopReason"] = "aborted"
            session = sessions / "nested" / "review.jsonl"
            session.parent.mkdir()
            text = "{malformed session" if self.session_mode == "malformed" else json.dumps(record)
            session.write_text(text + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.returncode)

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return review_pr.run_review(PR_NUMBER, repo=self.repo, out=self.out, **kwargs)

    def assert_receipt(self, receipt: dict[str, Any]) -> None:
        persisted = json.loads((self.out / "receipt.json").read_text(encoding="utf-8"))
        assert persisted == receipt

    def assert_no_comment(self) -> None:
        assert "comment" not in self.events
        assert not (self.out / "comment.md").exists()

    def assert_rejected(self, receipt: dict[str, Any], *, removed: bool = True) -> None:
        assert receipt["valid"] is False
        assert "verdict" not in receipt
        assert receipt["errors"]
        assert receipt["comment_posted"] is False
        self.assert_no_comment()
        self.assert_receipt(receipt)
        if removed:
            assert receipt["worktree_removed"] is True
            assert not self.worktree_root.exists()


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviewHarness:
    repo = tmp_path / "repository"
    repo.mkdir()
    fake = ReviewHarness(repo=repo, out=tmp_path / "evidence", worktree_root=tmp_path / "worktree")

    def mkdtemp(*, prefix: str, dir: str) -> str:
        assert prefix == f"evallab-review-pr{PR_NUMBER}-"
        assert dir == "/private/tmp"
        fake.worktree_root.mkdir()
        return str(fake.worktree_root)

    monkeypatch.setattr(review_pr, "_command", fake.command)
    monkeypatch.setattr(review_pr.subprocess, "run", fake.process)
    monkeypatch.setattr(review_pr.tempfile, "mkdtemp", mkdtemp)
    return fake


def test_success_pins_head_preserves_evidence_and_removes_owned_checkout(harness: ReviewHarness) -> None:
    receipt = harness.run(contract="Synthetic acceptance contract", previous_review="Prior review evidence")

    assert receipt["valid"] is True
    assert receipt["verdict"] == "PASS"
    assert receipt["errors"] == []
    assert receipt["head_sha"] == HEAD
    assert receipt["base_sha"] == BASE
    assert receipt["attestation"]["valid"] is True
    assert receipt["attestation"]["assistant_counts"] == {review_pr.DEFAULT_MODEL: 1}
    assert receipt["worktree_removed"] is True
    assert not harness.worktree_root.exists()
    assert receipt["review_path"] == str(harness.out / "REVIEW.md")
    assert (harness.out / "REVIEW.md").read_text(encoding="utf-8") == PASS_REVIEW
    assert "synthetic_change" in (harness.out / "pr.diff").read_text(encoding="utf-8")
    brief = (harness.out / "brief.md").read_text(encoding="utf-8")
    assert HEAD in brief and BASE in brief
    assert "Synthetic acceptance contract" in brief
    assert "Prior review evidence" in brief
    for rule in review_pr.RULE_PATHS:
        assert f"Synthetic repository rule: {rule}" in brief
    assert harness.events == ["worktree_add", "reviewer", "head_recheck", "worktree_remove"]
    assert receipt["comment_posted"] is False
    harness.assert_no_comment()
    harness.assert_receipt(receipt)


@pytest.mark.parametrize("model", [review_pr.DEFAULT_MODEL, "google-antigravity/gemini-4-argon:thinking"])
def test_reviewer_uses_exact_route_high_thinking_session_and_noninteractive_stdin(
    harness: ReviewHarness, monkeypatch: pytest.MonkeyPatch, model: str,
) -> None:
    monkeypatch.setenv("SYNTHETIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("SYNTHETIC_SESSION_LOGIN", "synthetic-login")
    receipt = harness.run(model=model, timeout=73)

    assert receipt["valid"] is True
    assert receipt["requested_model"] == model
    assert len(harness.processes) == 1
    argv, kwargs = harness.processes[0]
    assert argv == [
        "omp", "-p", "--model", model, "--thinking", "high",
        "--session-dir", str(harness.out / "sessions"),
        "--config", str(harness.out / "review-config.yml"),
        "--no-title", "--no-extensions", "--no-skills", "--no-rules", "--no-context-files",
        "--tools", "read,grep,glob,write", f"@{harness.out / 'brief.md'}",
    ]
    assert receipt["command"] == argv
    assert kwargs["cwd"] == harness.checkout
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 73
    assert kwargs["stdout"].name == str(harness.out / "omp.log")
    assert kwargs["stdout"].closed
    assert not any(key.endswith("API_KEY") for key in kwargs["env"])
    assert kwargs["env"]["SYNTHETIC_SESSION_LOGIN"] == "synthetic-login"
    assert (harness.out / "review-config.yml").read_text(encoding="utf-8") == (
        "retry:\n  modelFallback: false\n"
    )


@pytest.mark.parametrize("review,verdict", [(PASS_REVIEW, "PASS"), (BLOCK_REVIEW, "BLOCK")])
def test_comment_requires_opt_in_valid_evidence_and_successful_cleanup(
    harness: ReviewHarness, review: str, verdict: str,
) -> None:
    harness.review = review
    receipt = harness.run(comment=True)

    assert receipt["valid"] is True
    assert receipt["verdict"] == verdict
    assert receipt["comment_posted"] is True
    assert harness.events[-2:] == ["worktree_remove", "comment"]
    body = (harness.out / "comment.md").read_text(encoding="utf-8")
    assert f"**{verdict}**" in body
    assert HEAD in body
    assert review_pr.DEFAULT_MODEL in body
    assert "advisory" in body
    assert "not a CI status" in body
    harness.assert_receipt(receipt)


def test_fetch_race_rejects_before_checkout_or_reviewer(harness: ReviewHarness) -> None:
    harness.fetched_head = OTHER_HEAD
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert "fetch" in " ".join(receipt["errors"]).lower()
    assert not harness.processes
    assert not harness.worktree_root.exists()
    assert not (harness.out / "REVIEW.md").exists()
    assert "worktree_add" not in harness.events


def test_head_change_during_review_rejects_stale_report(harness: ReviewHarness) -> None:
    harness.current_head = OTHER_HEAD
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert "head changed" in " ".join(receipt["errors"]).lower()
    assert (harness.out / "REVIEW.md").exists()


@pytest.mark.parametrize("session_mode", ["fallback", "mismatch", "missing", "malformed", "aborted"])
def test_unattested_review_never_becomes_verdict_or_comment(
    harness: ReviewHarness, session_mode: str,
) -> None:
    harness.session_mode = session_mode
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert receipt["attestation"]["valid"] is False
    assert (harness.out / "REVIEW.md").read_text(encoding="utf-8") == PASS_REVIEW


@pytest.mark.parametrize(
    "review",
    [
        None,
        "",
        "VERDICT: PASS\n",
        PASS_REVIEW + "Additional trailing text\n",
        PASS_REVIEW + "VERDICT: BLOCK\n",
        PASS_REVIEW.replace("## Should-fix", "## Other"),
        PASS_REVIEW.replace("## Blocking\nNone", "## Blocking\nAn unresolved blocker."),
        "```markdown\n" + PASS_REVIEW + "```\n",
    ],
    ids=["missing", "empty", "no-sections", "trailing-text", "duplicate-verdict", "missing-section", "pass-with-blocker", "fenced"],
)
def test_invalid_report_never_becomes_verdict_or_comment(
    harness: ReviewHarness, review: str | None,
) -> None:
    harness.review = review
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert receipt["attestation"]["valid"] is True


@pytest.mark.parametrize("dirty", [" M example.py", "?? unexpected.txt"])
def test_checkout_mutation_rejects_review(harness: ReviewHarness, dirty: str) -> None:
    harness.dirty = dirty
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert "modified the checkout" in " ".join(receipt["errors"])


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "launch-error"])
def test_reviewer_failure_cleans_checkout_and_preserves_failed_receipt(
    harness: ReviewHarness, failure: str,
) -> None:
    if failure == "timeout":
        harness.process_error = subprocess.TimeoutExpired(["omp"], 19)
    elif failure == "launch-error":
        harness.process_error = FileNotFoundError("synthetic missing omp")
    else:
        harness.returncode = 9
    receipt = harness.run(comment=True, timeout=19)

    harness.assert_rejected(receipt)
    assert len(harness.processes) == 1
    assert harness.processes[0][1]["stdout"].closed
    assert harness.events[-1] == "worktree_remove"
    if failure == "nonzero":
        assert receipt["omp_returncode"] == 9
        assert receipt["attestation"]["valid"] is True
        assert "status 9" in " ".join(receipt["errors"])


@pytest.mark.parametrize("failure", ["command-failure", "os-error"])
def test_cleanup_failure_withholds_verdict_and_comment_and_preserves_checkout(
    harness: ReviewHarness, failure: str,
) -> None:
    if failure == "command-failure":
        harness.cleanup_error = subprocess.CalledProcessError(1, ["git", "worktree", "remove"])
    else:
        harness.cleanup_error = OSError("synthetic cleanup error")
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert receipt["worktree_removed"] is False
    assert harness.checkout.exists()
    assert "cleanup failed" in " ".join(receipt["errors"])
    assert str(harness.checkout) in " ".join(receipt["errors"])


def test_command_helper_uses_devnull_without_executing_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="  synthetic output\n")

    monkeypatch.setattr(review_pr.subprocess, "run", fake_run)
    assert review_pr._command(["git", "status"], tmp_path) == "synthetic output"
    assert calls == [
        (["git", "status"], {
            "cwd": tmp_path, "stdin": subprocess.DEVNULL,
            "capture_output": True, "text": True, "check": True,
        })
    ]


def test_block_verdict_is_advisory_and_local_by_default(harness: ReviewHarness) -> None:
    harness.review = BLOCK_REVIEW
    receipt = harness.run()

    assert receipt["valid"] is True
    assert receipt["verdict"] == "BLOCK"
    assert receipt["comment_posted"] is False
    assert receipt["worktree_removed"] is True
    harness.assert_no_comment()
    harness.assert_receipt(receipt)


@pytest.mark.parametrize(
    "pr_number,options",
    [
        (0, {}),
        (-1, {}),
        (PR_NUMBER, {"timeout": 0}),
        (PR_NUMBER, {"timeout": -1}),
        (PR_NUMBER, {"model": "gemini-4-argon"}),
        (PR_NUMBER, {"model": "provider/has whitespace"}),
    ],
)
def test_invalid_invocation_is_rejected_before_commands_or_evidence(
    harness: ReviewHarness, pr_number: int, options: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        review_pr.run_review(pr_number, repo=harness.repo, out=harness.out, **options)

    assert not harness.commands
    assert not harness.processes
    assert not harness.out.exists()
    assert not harness.worktree_root.exists()


def test_existing_evidence_is_never_overwritten(harness: ReviewHarness) -> None:
    harness.out.mkdir()
    marker = harness.out / "REVIEW.md"
    marker.write_text("Earlier immutable evidence", encoding="utf-8")

    with pytest.raises(FileExistsError):
        harness.run()

    assert marker.read_text(encoding="utf-8") == "Earlier immutable evidence"
    assert not harness.commands
    assert not harness.processes
    assert not harness.worktree_root.exists()
