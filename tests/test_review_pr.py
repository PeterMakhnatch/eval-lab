"""PR review orchestration is synthetic, head-pinned, and fails closed (shallow and deep)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import review_pr  # noqa: E402
from scripts.independent_review_contract import (  # noqa: E402
    FOCUSES,
    bind_report,
    binding_status,
    parse_review,
    reviewed_head,
)

HEAD = "a" * 40
BASE = "b" * 40
OTHER_HEAD = "c" * 40
PR_NUMBER = 42
LANES = ("gates", "behaviour", "adversarial", "tests_docs")
SMALL = "10\t5\texample.py\n"
BIG = "400\t0\tbig.py\n"
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
SHOULD_FIX_REVIEW = PASS_REVIEW.replace("## Should-fix\nNone", "## Should-fix\n- Synthetic style concern.")
SHALLOW_TOOLS = {"read", "grep", "glob", "write"}
DEEP_TOOLS = {"read", "bash", "grep", "glob", "write", "edit"}


@dataclass
class LaneBehavior:
    """What the fake reviewer does for one lane ("review" is the shallow reviewer)."""

    review: str | None = PASS_REVIEW
    session_mode: str = "valid"
    returncode: int = 0
    error: Exception | None = None
    dirty: str = ""
    cleanup_error: Exception | None = None


@dataclass
class ReviewHarness:
    repo: Path
    out: Path
    worktree_root: Path
    commands: list[tuple[list[str], Path]] = field(default_factory=list)
    launches: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    added: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    statuses: list[Path] = field(default_factory=list)
    behaviors: dict[str, LaneBehavior] = field(
        default_factory=lambda: {name: LaneBehavior() for name in ("review", *LANES)}
    )
    numstat: str = SMALL
    fetched_head: str = HEAD
    current_head: str = HEAD
    add_fails_for: str | None = None
    expected_head: str = HEAD
    delta_base: str | None = None
    delta_numstat: str = "3\t1\tfix.py\n"
    ancestors: set[tuple[str, str]] = field(default_factory=set)
    metadata: dict[str, Any] = field(
        default_factory=lambda: {
            "number": PR_NUMBER,
            "headRefOid": HEAD,
            "baseRefOid": BASE,
            "url": f"https://example.invalid/owner/repo/pull/{PR_NUMBER}",
        }
    )

    def lane(self, name: str) -> LaneBehavior:
        return self.behaviors[name]

    def all_lanes(self, **attributes: Any) -> None:
        for name in LANES:
            for key, value in attributes.items():
                setattr(self.behaviors[name], key, value)

    @staticmethod
    def lane_of(worktree: Path) -> str:
        return "review" if worktree.name == "checkout" else worktree.name

    def lane_directory(self, name: str) -> Path:
        return self.out if name == "review" else self.out / "reviewers" / name

    def command(self, argv: list[str], cwd: Path) -> str:
        cwd = Path(cwd)
        self.commands.append((list(argv), cwd))
        if argv == ["gh", "pr", "view", str(PR_NUMBER), "--json", "number,headRefOid,baseRefOid,url"]:
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
        if argv[:3] == ["git", "diff", "--numstat"]:
            assert cwd == self.repo
            if "..." in argv[3]:
                assert argv[3] == f"{BASE}...{self.expected_head}"
                return self.numstat
            assert self.delta_base is not None
            assert argv[3] == f"{self.delta_base}..{self.expected_head}"
            return self.delta_numstat
        if argv[:3] == ["git", "merge-base", "--is-ancestor"] and len(argv) == 5:
            assert cwd == self.repo
            if (argv[3], argv[4]) not in self.ancestors:
                raise subprocess.CalledProcessError(1, argv)
            return ""
        if argv[:4] == ["git", "worktree", "add", "--detach"]:
            assert cwd == self.repo and argv[5] == self.expected_head and len(argv) == 6
            path = Path(argv[4])
            assert path.parent == self.worktree_root
            if self.lane_of(path) == self.add_fails_for:
                raise subprocess.CalledProcessError(128, argv)
            self.events.append("worktree_add")
            path.mkdir()
            for rule in review_pr.RULE_PATHS:
                rule_path = path / rule
                rule_path.parent.mkdir(parents=True, exist_ok=True)
                rule_path.write_text(f"Synthetic repository rule: {rule}\n", encoding="utf-8")
            self.added.append(path)
            return ""
        if argv == ["git", "diff", f"{BASE}...{self.expected_head}"]:
            assert cwd == self.added[0]
            return "diff --git a/example.py b/example.py\n+synthetic_change = True\n"
        if self.delta_base is not None and argv == ["git", "diff", self.delta_base, self.expected_head]:
            assert cwd == self.added[0]
            return "diff --git a/fix.py b/fix.py\n+synthetic_delta = True\n"
        if argv == ["git", "status", "--porcelain", "--untracked-files=all"]:
            assert cwd in self.added
            self.statuses.append(cwd)
            return self.behaviors[self.lane_of(cwd)].dirty
        if argv == ["gh", "pr", "view", str(PR_NUMBER), "--json", "headRefOid"]:
            assert cwd == self.repo
            self.events.append("head_recheck")
            return json.dumps({"headRefOid": self.current_head})
        if argv == ["gh", "pr", "view", str(PR_NUMBER), "--json", "headRefOid", "-q", ".headRefOid"]:
            assert cwd == self.repo
            return self.current_head
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return str(self.repo)
        if argv[:3] == ["git", "worktree", "remove"] and len(argv) == 4:
            assert cwd == self.repo
            path = Path(argv[3])
            assert path in self.added
            self.events.append("worktree_remove")
            error = self.behaviors[self.lane_of(path)].cleanup_error
            if error is not None:
                raise error
            shutil.rmtree(path)
            self.removed.append(path)
            return ""
        if argv == ["gh", "pr", "comment", str(PR_NUMBER), "--body-file", str(self.out / "comment.md")]:
            assert cwd == self.repo
            self.events.append("comment")
            assert (self.out / "comment.md").is_file()
            return "https://example.invalid/comment/1"
        raise AssertionError(f"Unexpected command: {argv!r}")

    def reviewer(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, timeout: int
    ) -> int:
        name = self.lane_of(Path(cwd))
        sessions = Path(argv[argv.index("--session-dir") + 1])
        assert argv[0] == "omp"
        assert sessions == self.lane_directory(name) / "sessions"
        assert sessions.is_dir()
        assert Path(cwd) in self.added
        self.events.append("reviewer")
        self.launches.append(
            {"lane": name, "argv": list(argv), "cwd": Path(cwd), "env": dict(env),
             "log_path": Path(log_path), "timeout": timeout}
        )
        behavior = self.behaviors[name]
        Path(log_path).write_text(f"synthetic reviewer log for {name}\n", encoding="utf-8")
        if behavior.review is not None:
            (sessions.parent / "REVIEW.md").write_text(behavior.review, encoding="utf-8")
        if behavior.session_mode != "missing":
            route = argv[argv.index("--model") + 1].removesuffix(":thinking")
            provider, model = route.split("/", 1)
            message: dict[str, Any] = {
                "role": "assistant", "provider": provider, "model": model, "stopReason": "stop",
            }
            record: dict[str, Any] = {"type": "message", "message": message}
            if behavior.session_mode == "fallback":
                record["resolvedModelIsFallback"] = True
            elif behavior.session_mode == "mismatch":
                message["model"] = "unexpected-model"
            elif behavior.session_mode == "aborted":
                message["stopReason"] = "aborted"
            session = sessions / "nested" / "review.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            text = "{malformed session" if behavior.session_mode == "malformed" else json.dumps(record)
            session.write_text(text + "\n", encoding="utf-8")
        if behavior.error is not None:
            raise behavior.error
        return behavior.returncode

    def assert_never_touched_head(self, sha: str) -> None:
        """A head the review did not pin is never fetched into a worktree, diffed or checked out."""
        assert not any(sha in argv for argv, _ in self.commands)
        assert self.expected_head != sha

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return review_pr.run_review(PR_NUMBER, repo=self.repo, out=self.out, **kwargs)

    def set_pr_head(self, sha: str) -> None:
        """The PR branch is at ``sha`` from the start (the reviewed commit stays ``expected_head``)."""
        self.metadata["headRefOid"] = sha
        self.fetched_head = sha
        self.current_head = sha

    def launch(self, lane: str) -> dict[str, Any]:
        (found,) = [item for item in self.launches if item["lane"] == lane]
        return found

    def consolidated(self) -> str:
        return (self.out / "REVIEW.md").read_text(encoding="utf-8")

    def assert_receipt(self, receipt: dict[str, Any]) -> None:
        persisted = json.loads((self.out / "receipt.json").read_text(encoding="utf-8"))
        assert persisted == receipt

    def assert_no_comment(self) -> None:
        assert "comment" not in self.events
        assert not (self.out / "comment.md").exists()

    def assert_withheld(self, receipt: dict[str, Any], text: str, finding: str) -> None:
        """An invalid receipt whose report said PASS must read as refused, keeping the findings."""
        reasons = " ".join("; ".join(receipt["errors"]).split())
        first, _, rest = text.partition("\n")
        assert first == f"> **REFUSED: no valid verdict was issued: {reasons}**"
        assert not pass_lines(text)
        assert text.rstrip().splitlines()[-1] == "VERDICT: BLOCK"
        assert finding in text
        assert rest.startswith("\n")

    def assert_rejected(self, receipt: dict[str, Any], *, removed: bool = True) -> None:
        assert receipt["valid"] is False
        assert "verdict" not in receipt
        assert receipt["errors"]
        assert receipt["comment_posted"] is False
        self.assert_no_comment()
        self.assert_receipt(receipt)
        if removed:
            assert receipt["worktree_removed"] is True
            assert sorted(self.removed) == sorted(self.added)
            assert not self.worktree_root.exists()


def pass_lines(text: str) -> bool:
    return any(line.strip() == "VERDICT: PASS" for line in text.splitlines())


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

    def forbidden_subprocess(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Orchestration must not spawn processes outside the fakes")

    monkeypatch.setattr(review_pr, "_command", fake.command)
    monkeypatch.setattr(review_pr, "_run_reviewer", fake.reviewer)
    monkeypatch.setattr(review_pr.subprocess, "run", forbidden_subprocess)
    monkeypatch.setattr(review_pr.subprocess, "Popen", forbidden_subprocess)
    monkeypatch.setattr(review_pr.tempfile, "mkdtemp", mkdtemp)
    return fake


# --------------------------------------------------------------------------- shallow


def test_success_pins_head_preserves_evidence_and_removes_owned_checkout(harness: ReviewHarness) -> None:
    receipt = harness.run(contract="Synthetic acceptance contract", previous_review="Prior review evidence")

    assert receipt["valid"] is True
    assert receipt["verdict"] == "PASS"
    assert receipt["mode"] == "shallow"
    assert receipt["changed_lines"] == 15
    assert receipt["errors"] == []
    assert receipt["head_sha"] == HEAD
    assert receipt["base_sha"] == BASE
    assert receipt["attestation"]["valid"] is True
    assert receipt["attestation"]["assistant_counts"] == {review_pr.DEFAULT_MODEL: 1}
    assert receipt["worktree_removed"] is True
    assert not harness.worktree_root.exists()
    assert receipt["review_path"] == str(harness.out / "REVIEW.md")
    report = (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    assert report.startswith(f"> Reviewed head: `{HEAD}`")
    assert report.endswith(PASS_REVIEW)
    assert "NOT covered" not in report
    assert (harness.out / "REVIEW.reviewer.md").read_text(encoding="utf-8") == PASS_REVIEW
    assert receipt["pr_head_at_start"] == HEAD and receipt["pr_head_after"] == HEAD
    assert receipt["head_moved"] is False
    # An earlier review that carries no commit binding is not a delta base.
    assert receipt["previous_head_sha"] is None
    assert any("not bound to a commit" in note for note in receipt["notes"])
    assert "synthetic_change" in (harness.out / "pr.diff").read_text(encoding="utf-8")
    brief = (harness.out / "brief.md").read_text(encoding="utf-8")
    assert HEAD in brief and BASE in brief
    assert "Synthetic acceptance contract" in brief
    assert "Prior review evidence" in brief
    for rule in review_pr.RULE_PATHS:
        assert f"Synthetic repository rule: {rule}" in brief
    assert (harness.out / "omp.log").is_file()
    assert harness.events == ["worktree_add", "reviewer", "head_recheck", "worktree_remove"]
    assert receipt["comment_posted"] is False
    harness.assert_no_comment()
    harness.assert_receipt(receipt)


@pytest.mark.parametrize("model", [review_pr.DEFAULT_MODEL, "google-antigravity/gemini-4-argon:thinking"])
def test_shallow_reviewer_uses_exact_route_high_thinking_session_and_read_only_tools(
    harness: ReviewHarness, monkeypatch: pytest.MonkeyPatch, model: str,
) -> None:
    monkeypatch.setenv("SYNTHETIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("SYNTHETIC_SESSION_LOGIN", "synthetic-login")
    receipt = harness.run(model=model, timeout=73)

    assert receipt["valid"] is True
    assert receipt["requested_model"] == model
    assert receipt["reviewer_timeout"] == 73
    (launch,) = harness.launches
    assert launch["argv"] == [
        "omp", "-p", "--model", model, "--thinking", "high",
        "--session-dir", str(harness.out / "sessions"),
        "--config", str(harness.out / "review-config.yml"),
        "--no-title", "--no-extensions", "--no-skills", "--no-rules", "--no-context-files",
        "--tools", "read,grep,glob,write", f"@{harness.out / 'brief.md'}",
    ]
    assert receipt["command"] == launch["argv"]
    assert launch["cwd"] == harness.worktree_root / "checkout"
    assert launch["log_path"] == harness.out / "omp.log"
    assert launch["timeout"] == 73
    assert not any(key.endswith("API_KEY") for key in launch["env"])
    assert launch["env"]["SYNTHETIC_SESSION_LOGIN"] == "synthetic-login"
    assert (harness.out / "review-config.yml").read_text(encoding="utf-8") == (
        "retry:\n  modelFallback: false\n"
    )


def test_default_timeouts_are_per_mode(harness: ReviewHarness) -> None:
    shallow = harness.run()
    assert shallow["reviewer_timeout"] == 1800
    assert harness.launches[0]["timeout"] == 1800


@pytest.mark.parametrize("review,verdict", [(PASS_REVIEW, "PASS"), (BLOCK_REVIEW, "BLOCK")])
def test_comment_requires_opt_in_valid_evidence_and_successful_cleanup(
    harness: ReviewHarness, review: str, verdict: str,
) -> None:
    harness.lane("review").review = review
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


def test_block_verdict_is_advisory_and_local_by_default(harness: ReviewHarness) -> None:
    harness.lane("review").review = BLOCK_REVIEW
    receipt = harness.run()

    assert receipt["valid"] is True
    assert receipt["verdict"] == "BLOCK"
    assert receipt["comment_posted"] is False
    assert receipt["worktree_removed"] is True
    harness.assert_no_comment()
    harness.assert_receipt(receipt)


def test_fetch_race_rejects_before_checkout_or_reviewer(harness: ReviewHarness) -> None:
    harness.fetched_head = OTHER_HEAD
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert "fetch" in " ".join(receipt["errors"]).lower()
    assert not harness.launches
    assert not harness.worktree_root.exists()
    assert not (harness.out / "REVIEW.md").exists()
    assert "worktree_add" not in harness.events


def test_moved_pr_head_during_shallow_review_keeps_the_bound_verdict_and_notes_the_gap(
    harness: ReviewHarness,
) -> None:
    harness.current_head = OTHER_HEAD
    receipt = harness.run(comment=True)

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["errors"] == []
    assert receipt["head_sha"] == HEAD
    assert receipt["pr_head_after"] == OTHER_HEAD
    assert receipt["head_moved"] is True
    report = (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    assert reviewed_head(report) == HEAD
    assert f"> NOTE: the PR head is now `{OTHER_HEAD}`" in report
    assert "NOT covered" in report and "--verify-fixes" in report
    assert report.endswith(PASS_REVIEW)
    harness.assert_never_touched_head(OTHER_HEAD)
    assert receipt["comment_posted"] is True
    harness.assert_receipt(receipt)
    # The stored verdict does not cover the new head: a merge gate must still refuse it.
    status = binding_status(receipt, PR_NUMBER, OTHER_HEAD)
    assert status["covers"] is False and status["reviewed_sha"] == HEAD
    assert binding_status(receipt, PR_NUMBER, HEAD)["covers"] is True


@pytest.mark.parametrize("session_mode", ["fallback", "mismatch", "missing", "malformed", "aborted"])
def test_unattested_review_never_becomes_verdict_or_comment(
    harness: ReviewHarness, session_mode: str,
) -> None:
    harness.lane("review").review = SHOULD_FIX_REVIEW
    harness.lane("review").session_mode = session_mode
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert receipt["attestation"]["valid"] is False
    withheld = harness.out / "REVIEW.md"
    harness.assert_withheld(receipt, withheld.read_text(encoding="utf-8"), "Synthetic style concern.")


@pytest.mark.parametrize("session_mode", ["mismatch", "missing"])
def test_rejected_blocking_report_is_left_untouched(harness: ReviewHarness, session_mode: str) -> None:
    harness.lane("review").review = BLOCK_REVIEW
    harness.lane("review").session_mode = session_mode
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert (harness.out / "REVIEW.md").read_text(encoding="utf-8") == BLOCK_REVIEW


def test_rejected_pass_without_trailing_verdict_line_is_not_rewritten(harness: ReviewHarness) -> None:
    report = PASS_REVIEW + "Additional trailing text\n"
    harness.lane("review").review = report
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert (harness.out / "REVIEW.md").read_text(encoding="utf-8") == report


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
    ids=["missing", "empty", "no-sections", "trailing-text", "duplicate-verdict", "missing-section",
         "pass-with-blocker", "fenced"],
)
def test_invalid_report_never_becomes_verdict_or_comment(
    harness: ReviewHarness, review: str | None,
) -> None:
    harness.lane("review").review = review
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert receipt["attestation"]["valid"] is True


@pytest.mark.parametrize("dirty", [" M example.py", "?? unexpected.txt"])
def test_checkout_mutation_rejects_review(harness: ReviewHarness, dirty: str) -> None:
    harness.lane("review").dirty = dirty
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt)
    assert "modified the checkout" in " ".join(receipt["errors"])


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "launch-error"])
def test_reviewer_failure_cleans_checkout_and_preserves_failed_receipt(
    harness: ReviewHarness, failure: str,
) -> None:
    behavior = harness.lane("review")
    if failure == "timeout":
        behavior.error = subprocess.TimeoutExpired(["omp"], 19)
    elif failure == "launch-error":
        behavior.error = FileNotFoundError("synthetic missing omp")
    else:
        behavior.returncode = 9
    receipt = harness.run(comment=True, timeout=19)

    harness.assert_rejected(receipt)
    assert len(harness.launches) == 1
    assert harness.launches[0]["timeout"] == 19
    assert harness.events[-1] == "worktree_remove"
    if failure == "timeout":
        assert "timed out" in " ".join(receipt["errors"])
    if failure == "nonzero":
        assert receipt["omp_returncode"] == 9
        assert receipt["attestation"]["valid"] is True
        assert "status 9" in " ".join(receipt["errors"])


@pytest.mark.parametrize("failure", ["command-failure", "os-error"])
def test_cleanup_failure_withholds_verdict_and_comment_and_preserves_checkout(
    harness: ReviewHarness, failure: str,
) -> None:
    if failure == "command-failure":
        error: Exception = subprocess.CalledProcessError(1, ["git", "worktree", "remove"])
    else:
        error = OSError("synthetic cleanup error")
    harness.lane("review").cleanup_error = error
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt, removed=False)
    checkout = harness.worktree_root / "checkout"
    assert receipt["worktree_removed"] is False
    assert checkout.exists()
    assert "cleanup failed" in " ".join(receipt["errors"])
    assert str(checkout) in " ".join(receipt["errors"])
    harness.assert_withheld(receipt, (harness.out / "REVIEW.md").read_text(encoding="utf-8"), "## Nit")


def test_command_helper_uses_devnull_without_executing_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize(
    "pr_number,options",
    [
        (0, {}),
        (-1, {}),
        (PR_NUMBER, {"timeout": 0}),
        (PR_NUMBER, {"timeout": -1}),
        (PR_NUMBER, {"deep_threshold": -1}),
        (PR_NUMBER, {"mode": "thorough"}),
        (PR_NUMBER, {"lanes": []}),
        (PR_NUMBER, {"lanes": ["gates", "gates"]}),
        (PR_NUMBER, {"lanes": ["gates", "unknown"]}),
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
    assert not harness.launches
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
    assert not harness.launches
    assert not harness.worktree_root.exists()


# ---------------------------------------------------------------------- mode selection


@pytest.mark.parametrize(
    "numstat,expected",
    [
        ("200\t100\tat_threshold.py\n", "shallow"),
        ("200\t101\tover_threshold.py\n", "deep"),
        ("150\t150\ta.py\n0\t1\tb.py\n", "deep"),
        ("-\t-\tbinary.png\n", "shallow"),
        ("", "shallow"),
    ],
    ids=["exactly-threshold", "one-over", "summed-over-files", "binary-counts-zero", "empty-diff"],
)
def test_auto_mode_is_deep_only_strictly_above_threshold(
    harness: ReviewHarness, numstat: str, expected: str,
) -> None:
    harness.numstat = numstat
    receipt = harness.run()

    assert receipt["mode"] == expected
    assert len(harness.launches) == (4 if expected == "deep" else 1)
    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["reviewer_timeout"] == (2400 if expected == "deep" else 1800)
    assert {launch["timeout"] for launch in harness.launches} == {receipt["reviewer_timeout"]}


def test_custom_deep_threshold_applies_to_auto_mode(harness: ReviewHarness) -> None:
    harness.numstat = SMALL
    receipt = harness.run(deep_threshold=14)
    assert receipt["mode"] == "deep" and receipt["changed_lines"] == 15


@pytest.mark.parametrize("numstat,mode", [(BIG, "shallow"), (SMALL, "deep")])
def test_explicit_mode_overrides_size(harness: ReviewHarness, numstat: str, mode: str) -> None:
    harness.numstat = numstat
    receipt = harness.run(mode=mode)

    assert receipt["mode"] == mode
    assert len(harness.launches) == (4 if mode == "deep" else 1)
    assert ("reviewers" in os.listdir(harness.out)) == (mode == "deep")


def test_explicit_timeout_applies_to_every_deep_lane(harness: ReviewHarness) -> None:
    receipt = harness.run(mode="deep", timeout=61)

    assert receipt["reviewer_timeout"] == 61
    assert [launch["timeout"] for launch in harness.launches] == [61] * 4


# ----------------------------------------------------------------------------- deep


def test_focus_lanes_are_the_documented_four() -> None:
    assert set(FOCUSES) == set(LANES)


def test_deep_launches_four_isolated_reviewers_and_preserves_per_lane_evidence(
    harness: ReviewHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHETIC_API_KEY", "not-a-real-key")
    receipt = harness.run(mode="deep", contract="Synthetic acceptance contract")

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["coverage"] == {"expected": list(FOCUSES), "complete": list(FOCUSES), "incomplete": {}}
    assert sorted(launch["lane"] for launch in harness.launches) == sorted(LANES)
    # One private worktree, session directory, brief and log per lane.
    assert len({launch["cwd"] for launch in harness.launches}) == 4
    assert {launch["cwd"] for launch in harness.launches} == {harness.worktree_root / lane for lane in LANES}
    assert len({tuple(launch["argv"]) for launch in harness.launches}) == 4
    assert len({launch["log_path"] for launch in harness.launches}) == 4
    for lane in LANES:
        launch = harness.launch(lane)
        directory = harness.out / "reviewers" / lane
        brief = directory / "brief.md"
        assert launch["argv"] == [
            "omp", "-p", "--model", review_pr.DEFAULT_MODEL, "--thinking", "high",
            "--session-dir", str(directory / "sessions"),
            "--config", str(harness.out / "review-config.yml"),
            "--no-title", "--no-extensions", "--no-skills", "--no-rules", "--no-context-files",
            "--tools", "read,bash,grep,glob,write,edit", f"@{brief}",
        ]
        assert set(launch["argv"][launch["argv"].index("--tools") + 1].split(",")) == DEEP_TOOLS
        assert launch["log_path"] == directory / "omp.log"
        assert launch["timeout"] == 2400
        assert launch["env"]["TMPDIR"] == str(directory / "scratch")
        assert not any(key.endswith("API_KEY") for key in launch["env"])
        text = brief.read_text(encoding="utf-8")
        assert str(harness.worktree_root / lane) in text
        assert str(directory / "REVIEW.md") in text
        assert FOCUSES[lane]["title"] in text
        assert "Synthetic acceptance contract" in text
        for other in LANES:
            if other != lane:
                assert str(harness.out / "reviewers" / other / "REVIEW.md") not in text
                assert str(harness.worktree_root / other) not in text
        # Evidence preserved after the worktree is gone.
        for artifact in ("brief.md", "REVIEW.md", "omp.log", "sessions/nested/review.jsonl"):
            assert (directory / artifact).is_file()
        assert (directory / "scratch").is_dir()
        assert receipt["lanes"][lane]["complete"] is True
        assert receipt["commands"][lane] == launch["argv"]
    assert (harness.out / "review-config.yml").read_text(encoding="utf-8") == (
        "retry:\n  modelFallback: false\n"
    )
    assert receipt["attestation"]["valid"] is True
    assert receipt["attestation"]["assistant_counts"] == {review_pr.DEFAULT_MODEL: 4}
    assert len(receipt["attestation"]["files"]) == 4
    assert sorted(harness.removed) == sorted(harness.added) and len(harness.added) == 4
    assert not harness.worktree_root.exists()
    assert receipt["worktree_removed"] is True
    assert receipt["review_path"] == str(harness.out / "REVIEW.md")
    assert parse_review(harness.consolidated())["verdict"] == "PASS"
    harness.assert_receipt(receipt)


def test_deep_tool_sets_never_include_task_or_eval(harness: ReviewHarness) -> None:
    harness.run(mode="deep")
    for launch in harness.launches:
        tools = set(launch["argv"][launch["argv"].index("--tools") + 1].split(","))
        assert tools == DEEP_TOOLS
        assert not tools & {"task", "eval", "browser", "web_search"}
    assert SHALLOW_TOOLS < DEEP_TOOLS


def test_deep_exact_model_route_reaches_every_lane(harness: ReviewHarness) -> None:
    model = "google-antigravity/gemini-4-argon:thinking"
    receipt = harness.run(mode="deep", model=model)

    assert receipt["valid"] is True
    for launch in harness.launches:
        assert launch["argv"][launch["argv"].index("--model") + 1] == model
        assert launch["argv"][launch["argv"].index("--thinking") + 1] == "high"


def test_all_lanes_pass_then_comment_posts_after_every_cleanup(harness: ReviewHarness) -> None:
    receipt = harness.run(mode="deep", comment=True)

    assert receipt["verdict"] == "PASS" and receipt["valid"] is True
    assert receipt["comment_posted"] is True
    assert harness.events[-5:] == ["worktree_remove"] * 4 + ["comment"]
    body = (harness.out / "comment.md").read_text(encoding="utf-8")
    assert "**PASS**" in body and HEAD in body
    harness.assert_receipt(receipt)


def test_deep_pass_keeps_lane_should_fix_findings_in_consolidated_report(harness: ReviewHarness) -> None:
    harness.lane("behaviour").review = SHOULD_FIX_REVIEW
    receipt = harness.run(mode="deep")

    assert receipt["verdict"] == "PASS"
    report = harness.consolidated()
    assert "Synthetic style concern." in report and "[behaviour]" in report
    parse_review(report)


@pytest.mark.parametrize("lane", LANES)
def test_one_blocking_lane_blocks_with_full_coverage(harness: ReviewHarness, lane: str) -> None:
    harness.lane(lane).review = BLOCK_REVIEW
    receipt = harness.run(mode="deep", comment=True)

    assert receipt["valid"] is True
    assert receipt["verdict"] == "BLOCK"
    assert receipt["coverage"]["incomplete"] == {}
    assert receipt["coverage"]["complete"] == list(FOCUSES)
    report = harness.consolidated()
    assert f"[{lane}]" in report and "Synthetic correctness defect." in report
    assert parse_review(report)["verdict"] == "BLOCK"
    # A BLOCK is a valid, attested result and may be commented, after cleanup.
    assert receipt["comment_posted"] is True
    assert harness.events[-1] == "comment" and harness.events[-2] == "worktree_remove"


@pytest.mark.parametrize("partial", ["Working notes: found nothing yet", PASS_REVIEW])
def test_one_lane_timeout_leaves_report_and_partial_coverage_never_pass(
    harness: ReviewHarness, partial: str,
) -> None:
    harness.lane("adversarial").error = subprocess.TimeoutExpired(["omp"], 2400)
    harness.lane("adversarial").review = partial
    receipt = harness.run(mode="deep", comment=True)

    assert receipt["valid"] is True
    assert receipt["verdict"] == "INCONCLUSIVE"
    assert receipt["verdict"] != "PASS"
    assert receipt["coverage"]["expected"] == list(FOCUSES)
    assert receipt["coverage"]["complete"] == ["gates", "behaviour", "tests_docs"]
    assert list(receipt["coverage"]["incomplete"]) == ["adversarial"]
    assert "timed out" in receipt["coverage"]["incomplete"]["adversarial"]
    assert receipt["lanes"]["adversarial"]["complete"] is False
    assert all(receipt["lanes"][lane]["complete"] for lane in ("gates", "behaviour", "tests_docs"))
    report = harness.consolidated()
    assert "COVERAGE INCOMPLETE" in report and "PARTIAL (3 of 4 lanes)" in report
    assert "unvalidated" in report
    first_partial_line = next(line for line in partial.splitlines() if line.strip())
    assert f"> {first_partial_line}" in report
    assert not pass_lines(report)
    assert report.splitlines()[-1] == "VERDICT: BLOCK"
    # Neither a comment nor a PASS-like claim leaves the machine for partial coverage.
    assert receipt["comment_posted"] is False
    harness.assert_no_comment()
    assert not harness.worktree_root.exists()
    harness.assert_receipt(receipt)


def test_timeout_in_one_lane_does_not_hide_a_blocker_from_another(harness: ReviewHarness) -> None:
    harness.lane("adversarial").error = subprocess.TimeoutExpired(["omp"], 2400)
    harness.lane("tests_docs").review = BLOCK_REVIEW
    receipt = harness.run(mode="deep", comment=True)

    assert receipt["valid"] is True and receipt["verdict"] == "BLOCK"
    assert list(receipt["coverage"]["incomplete"]) == ["adversarial"]
    assert "Synthetic correctness defect." in harness.consolidated()
    assert receipt["comment_posted"] is True


@pytest.mark.parametrize(
    "failure",
    ["nonzero", "missing-report", "invalid-report", "dirty", "launch-error", "missing-session",
     "malformed-session", "aborted-session"],
)
@pytest.mark.parametrize("lane", ["gates", "tests_docs"])
def test_lane_failure_makes_only_that_lane_incomplete(
    harness: ReviewHarness, lane: str, failure: str,
) -> None:
    behavior = harness.lane(lane)
    if failure == "nonzero":
        behavior.returncode = 7
    elif failure == "missing-report":
        behavior.review = None
    elif failure == "invalid-report":
        behavior.review = PASS_REVIEW + "Trailing text invalidates the report\n"
    elif failure == "dirty":
        behavior.dirty = " M tracked.py"
    elif failure == "launch-error":
        behavior.error = FileNotFoundError("synthetic missing omp")
    else:
        behavior.session_mode = failure.removesuffix("-session")
    receipt = harness.run(mode="deep", comment=True)

    others = [name for name in LANES if name != lane]
    assert receipt["valid"] is True
    assert receipt["verdict"] == "INCONCLUSIVE"
    assert list(receipt["coverage"]["incomplete"]) == [lane]
    assert receipt["coverage"]["complete"] == [name for name in LANES if name != lane]
    assert receipt["lanes"][lane]["complete"] is False
    assert all(receipt["lanes"][name]["complete"] for name in others)
    report = harness.consolidated()
    assert f"[{lane}] COVERAGE INCOMPLETE" in report
    assert not pass_lines(report)
    assert receipt["comment_posted"] is False
    harness.assert_no_comment()
    assert sorted(harness.removed) == sorted(harness.added)
    assert len(harness.launches) == 4
    if failure == "nonzero":
        assert receipt["lanes"][lane]["returncode"] == 7
    if failure == "dirty":
        assert "modified the checkout" in receipt["coverage"]["incomplete"][lane]
        assert harness.worktree_root / lane in harness.statuses
    if failure == "invalid-report":
        assert receipt["coverage"]["incomplete"][lane].startswith("report invalid")


@pytest.mark.parametrize("session_mode", ["fallback", "mismatch"])
@pytest.mark.parametrize("lane", LANES)
def test_off_route_model_in_one_lane_refuses_the_whole_run(
    harness: ReviewHarness, lane: str, session_mode: str,
) -> None:
    harness.lane(lane).session_mode = session_mode
    receipt = harness.run(mode="deep", comment=True)

    harness.assert_rejected(receipt)
    assert "served model attestation failed" in " ".join(receipt["errors"])
    assert receipt["attestation"]["valid"] is False
    assert receipt["attestation"]["violations"]
    assert all(item.startswith(f"[{lane}] ") for item in receipt["attestation"]["violations"])
    expected_default = 4 if session_mode == "fallback" else 3
    assert receipt["attestation"]["assistant_counts"][review_pr.DEFAULT_MODEL] == expected_default
    assert receipt["coverage"]["complete"] == []
    assert sorted(receipt["coverage"]["incomplete"]) == sorted(LANES)
    report = harness.consolidated()
    assert not pass_lines(report)
    assert report.splitlines()[-1] == "VERDICT: BLOCK"
    assert "PARTIAL (0 of 4 lanes)" in report


@pytest.mark.parametrize("lane", ["gates", "adversarial"])
def test_off_route_model_beats_a_blocking_report_in_another_lane(harness: ReviewHarness, lane: str) -> None:
    other = "tests_docs"
    harness.lane(other).review = BLOCK_REVIEW
    harness.lane(lane).session_mode = "mismatch"
    receipt = harness.run(mode="deep", comment=True)

    harness.assert_rejected(receipt)
    assert receipt["coverage"]["complete"] == []


def test_moved_pr_head_during_deep_review_keeps_the_bound_verdict_and_notes_the_gap(
    harness: ReviewHarness,
) -> None:
    harness.lane("tests_docs").review = BLOCK_REVIEW
    harness.current_head = OTHER_HEAD
    receipt = harness.run(mode="deep", comment=True)

    assert receipt["valid"] is True and receipt["verdict"] == "BLOCK"
    assert receipt["head_sha"] == HEAD
    assert receipt["pr_head_at_start"] == HEAD
    assert receipt["pr_head_after"] == OTHER_HEAD
    assert receipt["head_moved"] is True
    assert receipt["coverage"]["incomplete"] == {}
    report = harness.consolidated()
    assert reviewed_head(report) == HEAD
    assert f"> NOTE: the PR head is now `{OTHER_HEAD}`" in report
    assert "NOT covered" in report and "--verify-fixes" in report
    assert parse_review(report)["verdict"] == "BLOCK"
    harness.assert_never_touched_head(OTHER_HEAD)
    # The comment names the reviewed commit only.
    assert receipt["comment_posted"] is True
    body = (harness.out / "comment.md").read_text(encoding="utf-8")
    assert HEAD in body and OTHER_HEAD not in body
    harness.assert_receipt(receipt)


def test_deep_cleanup_failure_preserves_that_worktree_attempts_the_rest_and_withholds_verdict(
    harness: ReviewHarness,
) -> None:
    harness.lane("gates").cleanup_error = subprocess.CalledProcessError(1, ["git", "worktree", "remove"])
    receipt = harness.run(mode="deep", comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert receipt["worktree_removed"] is False
    assert (harness.worktree_root / "gates").exists()
    assert sorted(harness.removed) == sorted(harness.worktree_root / lane for lane in LANES[1:])
    assert harness.events.count("worktree_remove") == 4
    assert "cleanup failed" in " ".join(receipt["errors"])
    assert str(harness.worktree_root / "gates") in " ".join(receipt["errors"])
    assert harness.worktree_root.exists()
    for lane in LANES:
        assert (harness.out / "reviewers" / lane / "REVIEW.md").is_file()
    refused = harness.consolidated()
    assert refused.startswith("> **REFUSED: no valid verdict was issued:")
    assert "cleanup failed" in refused.splitlines()[0]
    assert not pass_lines(refused)
    assert refused.rstrip().splitlines()[-1] == "VERDICT: BLOCK"
    assert "# Independent review of PR #42 (deep, 4 focused reviewers)" in refused
    harness.assert_withheld(receipt, refused, "focused reviewers")


def test_deep_blocking_lane_with_stray_heading_keeps_the_whole_blocking_section(
    harness: ReviewHarness,
) -> None:
    harness.lane("adversarial").review = BLOCK_REVIEW.replace(
        "- `example.py:3`: Synthetic correctness defect.",
        "# Stray heading\n- `example.py:3`: Synthetic stray-heading defect.",
    )
    receipt = harness.run(mode="deep")

    assert receipt["valid"] is True and receipt["verdict"] == "BLOCK"
    assert receipt["coverage"]["incomplete"] == {}
    report = harness.consolidated()
    assert "# Stray heading" in report
    assert "Synthetic stray-heading defect." in report
    assert parse_review(report)["verdict"] == "BLOCK"
    assert "Synthetic stray-heading defect." in parse_review(report)["blocking"]


def test_deep_pass_with_content_after_a_stray_heading_in_blocking_is_incomplete(
    harness: ReviewHarness,
) -> None:
    harness.lane("gates").review = PASS_REVIEW.replace(
        "## Blocking\nNone", "## Blocking\nNone\n# Stray heading\nHidden blocker."
    )
    receipt = harness.run(mode="deep")

    assert receipt["valid"] is True and receipt["verdict"] == "INCONCLUSIVE"
    assert list(receipt["coverage"]["incomplete"]) == ["gates"]
    assert receipt["coverage"]["incomplete"]["gates"].startswith("report invalid")


def test_deep_worktree_creation_failure_removes_already_added_worktrees(harness: ReviewHarness) -> None:
    harness.add_fails_for = "adversarial"
    receipt = harness.run(mode="deep", comment=True)

    harness.assert_rejected(receipt)
    assert not harness.launches
    assert [path.name for path in harness.removed] == ["gates", "behaviour"]


def test_deep_reviewer_launch_waits_for_all_worktrees_and_runs_all_lanes(harness: ReviewHarness) -> None:
    harness.run(mode="deep")

    assert harness.events[:4] == ["worktree_add"] * 4
    assert harness.events[4:8] == ["reviewer"] * 4
    assert harness.events[8] == "head_recheck"
    assert harness.events[9:] == ["worktree_remove"] * 4


# ---------------------------------------------------------------------- lane subsets


def test_deep_two_lane_subset_passes_with_skipped_notice_and_isolated_briefs(
    harness: ReviewHarness,
) -> None:
    requested = ["gates", "behaviour"]
    receipt = harness.run(mode="deep", lanes=requested)

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["coverage"] == {
        "expected": requested, "complete": requested, "incomplete": {},
    }
    assert sorted(receipt["lanes"]) == sorted(requested)
    assert sorted(receipt["worktrees"]) == sorted(requested)
    assert sorted(receipt["commands"]) == sorted(requested)
    assert len(harness.launches) == 2
    assert sorted(launch["lane"] for launch in harness.launches) == sorted(requested)
    assert len(harness.added) == len(harness.removed) == 2
    assert {path.name for path in harness.added} == set(requested)
    for skipped in ("adversarial", "tests_docs"):
        assert not (harness.out / "reviewers" / skipped).exists()
    report = harness.consolidated()
    assert "- Lanes not run (operator chose a subset): adversarial, tests_docs\n" in report
    assert "| gates | complete | PASS |" in report
    assert "| behaviour | complete | PASS |" in report
    assert "adversarial |" not in report and "tests_docs |" not in report
    assert parse_review(report)["verdict"] == "PASS"
    for lane, other in (("gates", "behaviour"), ("behaviour", "gates")):
        brief = (harness.out / "reviewers" / lane / "brief.md").read_text(encoding="utf-8")
        other_lanes = next(line for line in brief.splitlines() if "Other reviewers run" in line)
        assert other in other_lanes
        assert "adversarial" not in other_lanes and "tests_docs" not in other_lanes
    harness.assert_receipt(receipt)


def test_deep_single_lane_brief_has_no_other_reviewers(harness: ReviewHarness) -> None:
    receipt = harness.run(mode="deep", lanes=("tests_docs",))

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["coverage"]["expected"] == ["tests_docs"]
    assert len(harness.launches) == 1 and len(harness.added) == 1
    brief = (harness.out / "reviewers" / "tests_docs" / "brief.md").read_text(encoding="utf-8")
    assert "Other reviewers run in parallel on the other lanes (none)" in brief
    assert "- Lanes not run (operator chose a subset): gates, behaviour, adversarial\n" in (
        harness.consolidated()
    )


def test_timeout_in_two_lane_subset_is_inconclusive_not_pass(harness: ReviewHarness) -> None:
    harness.lane("behaviour").error = subprocess.TimeoutExpired(["omp"], 2400)
    receipt = harness.run(mode="deep", lanes=["gates", "behaviour"], comment=True)

    assert receipt["valid"] is True and receipt["verdict"] == "INCONCLUSIVE"
    assert receipt["coverage"]["expected"] == ["gates", "behaviour"]
    assert receipt["coverage"]["complete"] == ["gates"]
    assert list(receipt["coverage"]["incomplete"]) == ["behaviour"]
    assert len(harness.launches) == 2 and len(harness.added) == 2
    report = harness.consolidated()
    assert "PARTIAL (1 of 2 lanes)" in report
    assert "- Lanes not run (operator chose a subset): adversarial, tests_docs\n" in report
    assert not pass_lines(report)
    harness.assert_no_comment()
    harness.assert_receipt(receipt)


def test_valid_lane_subset_is_ignored_in_shallow_mode(harness: ReviewHarness) -> None:
    receipt = harness.run(mode="shallow", lanes=["gates"], comment=True)

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["mode"] == "shallow"
    assert receipt["worktree"] == str(harness.worktree_root / "checkout")
    assert receipt["lanes"].keys() == {"review"}
    assert receipt["commands"].keys() == {"review"}
    assert len(harness.launches) == 1 and harness.launches[0]["lane"] == "review"
    assert not (harness.out / "reviewers").exists()
    assert "Lanes not run" not in harness.consolidated()
    assert receipt["comment_posted"] is True


# ------------------------------------------------------------------------ pinned head

NEW_PR_HEAD = "d" * 40
PREVIOUS_HEAD = "e" * 40


def bound_previous(head: str = PREVIOUS_HEAD, review: str = PASS_REVIEW) -> str:
    return bind_report(
        review, pr_number=PR_NUMBER, head_sha=head, base_sha=BASE,
        model=review_pr.DEFAULT_MODEL, mode="shallow",
    )


def test_pinned_ancestor_head_is_reviewed_and_the_newer_pr_head_is_never_checked_out(
    harness: ReviewHarness,
) -> None:
    harness.set_pr_head(NEW_PR_HEAD)
    harness.ancestors.add((HEAD, NEW_PR_HEAD))
    receipt = harness.run(pin_head=HEAD, comment=True)

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["head_sha"] == HEAD
    assert receipt["pr_head_at_start"] == NEW_PR_HEAD
    assert receipt["pr_head_after"] == NEW_PR_HEAD
    assert receipt["head_moved"] is True
    assert (["git", "merge-base", "--is-ancestor", HEAD, NEW_PR_HEAD], harness.repo) in harness.commands
    adds = [argv for argv, _ in harness.commands if argv[:3] == ["git", "worktree", "add"]]
    assert [argv[-1] for argv in adds] == [HEAD]
    assert not any(
        NEW_PR_HEAD in argv for argv, _ in harness.commands
        if argv[:2] in (["git", "worktree"], ["git", "diff"])
    )
    report = (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    assert reviewed_head(report) == HEAD
    assert f"`{NEW_PR_HEAD}`" in report and "NOT covered" in report
    brief = (harness.out / "brief.md").read_text(encoding="utf-8")
    assert HEAD in brief and NEW_PR_HEAD not in brief
    assert receipt["comment_posted"] is True
    assert NEW_PR_HEAD not in (harness.out / "comment.md").read_text(encoding="utf-8")
    harness.assert_receipt(receipt)


def test_pin_equal_to_pr_head_needs_no_ancestry_check(harness: ReviewHarness) -> None:
    receipt = harness.run(pin_head=HEAD)

    assert receipt["valid"] is True and receipt["head_moved"] is False
    assert not any(argv[:2] == ["git", "merge-base"] for argv, _ in harness.commands)


def test_pinned_head_that_is_not_an_ancestor_is_refused_before_any_checkout(
    harness: ReviewHarness,
) -> None:
    harness.set_pr_head(NEW_PR_HEAD)  # no ancestry registered: HEAD is on another branch
    receipt = harness.run(pin_head=HEAD, comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert "neither the PR head nor one of its ancestors" in " ".join(receipt["errors"])
    assert not harness.launches and not harness.added
    assert not harness.worktree_root.exists()
    assert "worktree_removed" not in receipt
    assert not (harness.out / "REVIEW.md").exists()


@pytest.mark.parametrize("pin", ["", "abc123", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "HEAD", " " + "a" * 39])
def test_malformed_pinned_head_is_rejected_before_any_side_effect(harness: ReviewHarness, pin: str) -> None:
    with pytest.raises(ValueError, match="--head"):
        harness.run(pin_head=pin)

    assert not harness.commands and not harness.launches
    assert not harness.out.exists()
    assert not harness.worktree_root.exists()


# --------------------------------------------------------------------- binding block


@pytest.mark.parametrize("review,verdict", [(PASS_REVIEW, "PASS"), (BLOCK_REVIEW, "BLOCK")])
def test_shallow_report_starts_with_binding_block_and_verdict_is_unchanged(
    harness: ReviewHarness, review: str, verdict: str,
) -> None:
    harness.lane("review").review = review
    receipt = harness.run()

    text = (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith(f"> Reviewed head: `{HEAD}` (PR #{PR_NUMBER}, base `{BASE}`, mode shallow")
    assert lines[1] == "> This verdict covers only that commit."
    assert all(line.startswith(">") for line in lines[: lines.index("")])
    assert text.endswith(review)
    assert (harness.out / "REVIEW.reviewer.md").read_text(encoding="utf-8") == review
    assert reviewed_head(text) == HEAD
    assert parse_review(text)["verdict"] == receipt["verdict"] == verdict
    assert "NOT covered" not in text


def test_binding_block_cannot_be_spoofed_by_the_reviewers_own_quoted_text(harness: ReviewHarness) -> None:
    harness.lane("review").review = f"> Reviewed head: `{OTHER_HEAD}`\n\n" + PASS_REVIEW
    receipt = harness.run()

    assert receipt["valid"] is True
    assert reviewed_head((harness.out / "REVIEW.md").read_text(encoding="utf-8")) == HEAD


@pytest.mark.parametrize("verdict_lane", [None, "behaviour"])
def test_deep_report_starts_with_binding_block_and_verdict_is_unchanged(
    harness: ReviewHarness, verdict_lane: str | None,
) -> None:
    if verdict_lane:
        harness.lane(verdict_lane).review = BLOCK_REVIEW
    receipt = harness.run(mode="deep")
    expected = "BLOCK" if verdict_lane else "PASS"

    text = harness.consolidated()
    lines = text.splitlines()
    assert lines[0].startswith(f"> Reviewed head: `{HEAD}` (PR #{PR_NUMBER}, base `{BASE}`, mode deep")
    assert lines[1] == "> This verdict covers only that commit."
    assert reviewed_head(text) == HEAD
    assert parse_review(text)["verdict"] == receipt["verdict"] == expected
    assert text.rstrip().splitlines()[-1] == f"VERDICT: {expected}"


def test_refusal_text_precedes_the_binding_block_so_a_rejected_review_is_never_a_bound_pass(
    harness: ReviewHarness,
) -> None:
    harness.lane("review").cleanup_error = OSError("synthetic cleanup error")
    receipt = harness.run(comment=True)

    harness.assert_rejected(receipt, removed=False)
    text = (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    harness.assert_withheld(receipt, text, "## Nit")
    assert binding_status(receipt, PR_NUMBER, HEAD)["covers"] is False


# ------------------------------------------------------------------------ verify-fixes


def test_verify_fixes_with_ancestor_review_reviews_only_the_delta(harness: ReviewHarness) -> None:
    harness.delta_base = PREVIOUS_HEAD
    harness.ancestors.add((PREVIOUS_HEAD, HEAD))
    harness.numstat = BIG  # the whole PR is large; only the 4-line delta is reviewed
    receipt = harness.run(previous_review=bound_previous(review=BLOCK_REVIEW))

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["previous_head_sha"] == PREVIOUS_HEAD
    assert receipt["mode"] == "shallow" and receipt["changed_lines"] == 4
    assert "notes" not in receipt
    assert (["git", "merge-base", "--is-ancestor", PREVIOUS_HEAD, HEAD], harness.repo) in harness.commands
    assert "synthetic_delta" in (harness.out / "delta.diff").read_text(encoding="utf-8")
    assert "synthetic_change" in (harness.out / "pr.diff").read_text(encoding="utf-8")
    brief = (harness.out / "brief.md").read_text(encoding="utf-8")
    assert "### Delta scope" in brief and PREVIOUS_HEAD in brief
    assert str(harness.out / "delta.diff") in brief
    report = (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    assert f"> Delta review of `{PREVIOUS_HEAD}`..`{HEAD}`" in report
    assert reviewed_head(report) == HEAD  # the new report binds the new head, not the old one
    assert parse_review(report)["verdict"] == "PASS"


def test_verify_fixes_delta_size_chooses_the_mode(harness: ReviewHarness) -> None:
    harness.delta_base = PREVIOUS_HEAD
    harness.delta_numstat = BIG
    harness.ancestors.add((PREVIOUS_HEAD, HEAD))
    harness.numstat = SMALL  # small PR, large delta
    receipt = harness.run(previous_review=bound_previous())

    assert receipt["mode"] == "deep" and receipt["changed_lines"] == 400
    assert receipt["previous_head_sha"] == PREVIOUS_HEAD
    assert len(harness.launches) == 4
    for lane in LANES:
        brief = (harness.out / "reviewers" / lane / "brief.md").read_text(encoding="utf-8")
        assert "### Delta scope" in brief
        assert str(harness.out / "delta.diff") in brief


def test_verify_fixes_with_non_ancestor_review_falls_back_to_the_full_pr_with_a_note(
    harness: ReviewHarness,
) -> None:
    # PREVIOUS_HEAD was force-pushed away: no ancestry is registered.
    receipt = harness.run(previous_review=bound_previous())

    assert receipt["valid"] is True and receipt["verdict"] == "PASS"
    assert receipt["previous_head_sha"] is None
    assert any("not an ancestor" in note and "full PR" in note for note in receipt["notes"])
    assert receipt["changed_lines"] == 15
    assert not (harness.out / "delta.diff").exists()
    assert "### Delta scope" not in (harness.out / "brief.md").read_text(encoding="utf-8")
    assert "Delta review" not in (harness.out / "REVIEW.md").read_text(encoding="utf-8")
    assert not any(argv[:3] == ["git", "diff", PREVIOUS_HEAD] for argv, _ in harness.commands)


@pytest.mark.parametrize("previous", ["Plain review without any binding\n", PASS_REVIEW, ""])
def test_verify_fixes_with_an_unbound_review_reviews_the_full_pr_with_a_note(
    harness: ReviewHarness, previous: str,
) -> None:
    receipt = harness.run(previous_review=previous)

    assert receipt["valid"] is True
    assert receipt["previous_head_sha"] is None
    assert any("not bound to a commit" in note for note in receipt["notes"])
    assert not (harness.out / "delta.diff").exists()
    assert not any(argv[:2] == ["git", "merge-base"] for argv, _ in harness.commands)


def test_verify_fixes_with_a_review_of_this_exact_commit_is_refused_and_cleaned_up(
    harness: ReviewHarness,
) -> None:
    receipt = harness.run(previous_review=bound_previous(head=HEAD), comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert "already covers this exact commit" in " ".join(receipt["errors"])
    assert not harness.launches and not harness.added
    assert not harness.worktree_root.exists()
    assert receipt["head_sha"] == HEAD


def test_verify_fixes_with_a_refused_previous_review_is_refused(harness: ReviewHarness) -> None:
    refused = (
        "> **REFUSED: no valid verdict was issued: synthetic**\n\n"
        f"> Reviewed head: `{PREVIOUS_HEAD}` (PR #{PR_NUMBER})\n\n" + BLOCK_REVIEW
    )
    receipt = harness.run(previous_review=refused, comment=True)

    harness.assert_rejected(receipt, removed=False)
    assert "refused" in " ".join(receipt["errors"]).lower()
    assert not harness.launches and not harness.added


def test_verify_fixes_ignores_a_binding_quoted_later_in_the_previous_review(harness: ReviewHarness) -> None:
    harness.delta_base = PREVIOUS_HEAD
    harness.ancestors.add((PREVIOUS_HEAD, HEAD))
    receipt = harness.run(previous_review="Intro text\n\n" + bound_previous())

    assert receipt["valid"] is True
    assert receipt["previous_head_sha"] is None
    assert any("not bound to a commit" in note for note in receipt["notes"])


def test_verify_fixes_on_a_pinned_older_head_diffs_against_that_head(harness: ReviewHarness) -> None:
    harness.set_pr_head(NEW_PR_HEAD)
    harness.ancestors.update({(HEAD, NEW_PR_HEAD), (PREVIOUS_HEAD, HEAD)})
    harness.delta_base = PREVIOUS_HEAD
    receipt = harness.run(pin_head=HEAD, previous_review=bound_previous())

    assert receipt["valid"] is True
    assert receipt["head_sha"] == HEAD and receipt["previous_head_sha"] == PREVIOUS_HEAD
    assert receipt["head_moved"] is True
    assert (["git", "diff", "--numstat", f"{PREVIOUS_HEAD}..{HEAD}"], harness.repo) in harness.commands


# ----------------------------------------------------------------------------- --check


def write_receipt(tmp_path: Path, **changes: Any) -> Path:
    data: dict[str, Any] = {
        "schema": "evallab.independent_review/v1",
        "pr_number": PR_NUMBER,
        "valid": True,
        "verdict": "PASS",
        "head_sha": HEAD,
    }
    data.update(changes)
    path = tmp_path / "synthetic-receipt.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def check(harness: ReviewHarness, path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    code = review_pr.main([str(PR_NUMBER), "--check", str(path)])
    captured = capsys.readouterr()
    assert not harness.launches and not harness.added
    assert not harness.out.exists()
    assert not any(argv[:2] == ["git", "fetch"] for argv, _ in harness.commands)
    return code, (json.loads(captured.out) if captured.out else None)


@pytest.mark.parametrize("verdict,code", [("PASS", 0), ("BLOCK", 1)])
def test_check_exit_code_reports_the_verdict_that_covers_the_current_head(
    harness: ReviewHarness, tmp_path: Path, capsys: pytest.CaptureFixture[str], verdict: str, code: int,
) -> None:
    exit_code, status = check(harness, write_receipt(tmp_path, verdict=verdict), capsys)

    assert exit_code == code
    assert status["covers"] is True and status["verdict"] == verdict
    assert status["reviewed_sha"] == status["current_head"] == HEAD


@pytest.mark.parametrize("verdict", ["PASS", "BLOCK"])
def test_check_exits_4_when_the_pr_head_moved_even_for_a_pass(
    harness: ReviewHarness, tmp_path: Path, capsys: pytest.CaptureFixture[str], verdict: str,
) -> None:
    harness.current_head = OTHER_HEAD
    exit_code, status = check(harness, write_receipt(tmp_path, verdict=verdict), capsys)

    assert exit_code == 4
    assert status["covers"] is False
    assert status["reviewed_sha"] == HEAD and status["current_head"] == OTHER_HEAD
    assert "--verify-fixes" in status["action"]


@pytest.mark.parametrize(
    "changes",
    [
        {"verdict": "INCONCLUSIVE"},
        {"verdict": None},
        {"verdict": "MAYBE"},
        {"valid": False},
        {"valid": "true"},
        {"pr_number": 7},
        {"schema": "something.else/v1"},
    ],
    ids=["inconclusive", "no-verdict", "unknown-verdict", "invalid", "truthy-string-valid",
         "other-pr", "other-schema"],
)
def test_check_exits_2_for_receipts_that_carry_no_usable_verdict(
    harness: ReviewHarness, tmp_path: Path, capsys: pytest.CaptureFixture[str], changes: dict[str, Any],
) -> None:
    harness.current_head = OTHER_HEAD  # even with a moved head these are 2, never 4
    exit_code, status = check(harness, write_receipt(tmp_path, **changes), capsys)

    assert exit_code == 2
    assert status["covers"] is False


def test_check_a_receipt_without_a_reviewed_head_never_covers(
    harness: ReviewHarness, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, status = check(harness, write_receipt(tmp_path, head_sha=None), capsys)

    assert exit_code != 0 and exit_code != 1
    assert status["covers"] is False


@pytest.mark.parametrize("content", [None, "", "{not json", "[]", '"text"', "null"])
def test_check_exits_2_for_missing_or_unreadable_receipts(
    harness: ReviewHarness, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None,
) -> None:
    path = tmp_path / "receipt.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    code = review_pr.main([str(PR_NUMBER), "--check", str(path)])
    captured = capsys.readouterr()

    assert code == 2
    assert "Review refused" in captured.err
    assert not harness.launches and not harness.added and not harness.out.exists()


def test_check_receipt_function_returns_status_without_running_a_review(
    harness: ReviewHarness, tmp_path: Path,
) -> None:
    status = review_pr.check_receipt(write_receipt(tmp_path), PR_NUMBER, harness.repo)

    assert status["covers"] is True and status["verdict"] == "PASS"
    assert [argv[0] for argv, _ in harness.commands] == ["gh"]
    assert not harness.launches and not harness.out.exists()


def test_check_accepts_the_receipt_written_by_a_real_run_until_the_head_moves(
    harness: ReviewHarness, capsys: pytest.CaptureFixture[str],
) -> None:
    receipt = harness.run()
    stored = harness.out / "receipt.json"

    assert receipt["valid"] is True
    assert review_pr.main([str(PR_NUMBER), "--check", str(stored)]) == 0
    capsys.readouterr()
    harness.current_head = OTHER_HEAD
    assert review_pr.main([str(PR_NUMBER), "--check", str(stored)]) == 4
    status = json.loads(capsys.readouterr().out)
    assert status["covers"] is False and status["reviewed_sha"] == HEAD
    assert len(harness.launches) == 1  # only the original review ran


def test_check_of_a_pinned_older_review_is_4_when_the_pr_branch_moved(
    harness: ReviewHarness, capsys: pytest.CaptureFixture[str],
) -> None:
    harness.set_pr_head(NEW_PR_HEAD)
    harness.ancestors.add((HEAD, NEW_PR_HEAD))
    receipt = harness.run(pin_head=HEAD)
    stored = harness.out / "receipt.json"

    assert receipt["valid"] is True and receipt["head_moved"] is True
    assert review_pr.main([str(PR_NUMBER), "--check", str(stored)]) == 4
    assert json.loads(capsys.readouterr().out)["current_head"] == NEW_PR_HEAD


# --------------------------------------------------------------------------- CLI --head


def test_cli_head_flag_pins_the_review_and_exit_code_follows_the_verdict(
    harness: ReviewHarness, capsys: pytest.CaptureFixture[str],
) -> None:
    harness.set_pr_head(NEW_PR_HEAD)
    harness.ancestors.add((HEAD, NEW_PR_HEAD))
    code = review_pr.main([str(PR_NUMBER), "--head", HEAD, "--out", str(harness.out)])

    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["head_sha"] == HEAD and printed["head_moved"] is True


def test_cli_malformed_head_is_refused_with_exit_2_and_no_evidence(
    harness: ReviewHarness, capsys: pytest.CaptureFixture[str],
) -> None:
    code = review_pr.main([str(PR_NUMBER), "--head", "nope", "--out", str(harness.out)])

    assert code == 2
    assert "Review refused" in capsys.readouterr().err
    assert not harness.out.exists() and not harness.launches


# ------------------------------------------------------------------ real process group

CHILD_SPAWNS_GRANDCHILD = """
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(grandchild.pid))
print("child-started", flush=True)
time.sleep(120)
"""
CHILD_EXITS_LEAVING_GRANDCHILD = """
import subprocess, sys
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(grandchild.pid))
sys.exit(0)
"""
CHILD_CHECKS_STDIN = """
import os, sys
closed = os.path.samestat(os.fstat(0), os.stat(os.devnull)) and sys.stdin.buffer.read() == b""
print("stdin-closed" if closed else "stdin-open", flush=True)
sys.stderr.write("stderr-line\\n")
sys.exit(3 if closed else 4)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_reviewer_timeout_kills_the_whole_process_group_and_writes_log(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    log = tmp_path / "omp.log"
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            review_pr._run_reviewer(
                [sys.executable, "-c", CHILD_SPAWNS_GRANDCHILD, str(pid_file)],
                cwd=tmp_path, env=dict(os.environ), log_path=log, timeout=2,
            )
        assert time.monotonic() - started < 60
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), "grandchild survived the reviewer timeout"
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
    assert "child-started" in log.read_text(encoding="utf-8")


def test_run_reviewer_returns_exit_code_with_closed_stdin_and_merged_log(tmp_path: Path) -> None:
    log = tmp_path / "omp.log"
    code = review_pr._run_reviewer(
        [sys.executable, "-c", CHILD_CHECKS_STDIN],
        cwd=tmp_path, env=dict(os.environ), log_path=log, timeout=30,
    )

    assert code == 3
    text = log.read_text(encoding="utf-8")
    assert "stdin-closed" in text
    assert "stderr-line" in text


def test_run_reviewer_kills_background_children_after_a_normal_zero_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    log = tmp_path / "omp.log"
    try:
        code = review_pr._run_reviewer(
            [sys.executable, "-c", CHILD_EXITS_LEAVING_GRANDCHILD, str(pid_file)],
            cwd=tmp_path, env=dict(os.environ), log_path=log, timeout=30,
        )
        assert code == 0
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), "background child survived a normal reviewer exit"
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
