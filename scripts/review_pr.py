#!/usr/bin/env python3
"""Run an advisory, model-attested independent review of a pinned PR head.

Small PRs get one static reviewer (``shallow``). Large PRs get ``deep`` review: several
focused reviewers run in parallel as separate ``omp -p`` processes, each in its own detached
worktree with real command execution, and their reports are consolidated into one REVIEW.md.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.independent_review_contract import (
    FOCUSES,
    attest_sessions,
    bind_report,
    binding_status,
    changed_lines,
    choose_mode,
    consolidate_reviews,
    merge_attestations,
    parse_review,
    render_brief,
    reviewed_head,
    verify_evidence,
)

DEFAULT_MODEL = "google-antigravity/gemini-4-argon"
DEEP_THRESHOLD_LINES = 300
SHALLOW_TIMEOUT = 1800
DEEP_TIMEOUT = 2400
SHALLOW_TOOLS = "read,grep,glob,write"
# No task/eval/browser tools: no sub-agent may run on another provider.
DEEP_TOOLS = "read,bash,grep,glob,write,edit"
RULE_PATHS = (
    "AGENTS.md",
    "src/evallab/AGENTS.md",
    "agents/WORKFLOW.md",
    "agents/CHECKS.md",
    "agents/STRUCTURE.md",
)
REFUSED_PREFIX = "> **REFUSED: no valid verdict was issued:"


def _command(argv: list[str], cwd: Path) -> str:
    return subprocess.run(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True
    ).stdout.strip()


def _run_reviewer(
    argv: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, timeout: int
) -> int:
    """Run one reviewer with closed stdin; always kill its whole process group afterwards."""
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout)
        finally:
            # Timeout, interrupt or normal exit: never leave reviewer children running.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.wait()


def comment_summary(review: str, *, verdict: str, head: str, model: str) -> str:
    """Bound the optional public summary; the full report stays in local evidence."""
    blocking = parse_review(review)["blocking"]
    return (
        f"Independent review (advisory): **{verdict}**\n\n"
        f"Head: `{head}`\nServed model: `{model}` (attested; no fallback).\n\n"
        f"### Blocking findings\n{blocking[:1800]}\n\n"
        "Full REVIEW.md and session attestation are retained by the operator. "
        "This is not a CI status, merge approval, or proof that tests passed."
    )


@dataclass
class Lane:
    """One reviewer process: its own worktree, brief, session directory and report."""

    name: str
    focus: str | None
    worktree: Path
    directory: Path

    @property
    def report(self) -> Path:
        return self.directory / "REVIEW.md"

    @property
    def sessions(self) -> Path:
        return self.directory / "sessions"

    @property
    def scratch(self) -> Path:
        return self.directory / "scratch"


def _assess(lane: Lane, run: dict[str, Any], model: str, head: str) -> dict[str, Any]:
    """Judge one finished reviewer from evidence only; never from its own claims."""
    attestation = attest_sessions(sorted(lane.sessions.rglob("*.jsonl")), model)
    # Always inspect the checkout, even after a timeout: dirt is evidence, kept as dirty.txt.
    try:
        dirty = _command(["git", "status", "--porcelain", "--untracked-files=all"], lane.worktree)
    except (OSError, subprocess.SubprocessError) as error:
        dirty = f"cannot inspect checkout: {error}"
    if dirty:
        with contextlib.suppress(OSError):
            (lane.directory / "dirty.txt").write_text(dirty + "\n", encoding="utf-8")
    try:
        current = _command(["git", "rev-parse", "HEAD"], lane.worktree)
    except (OSError, subprocess.SubprocessError) as error:
        current = f"cannot read HEAD: {error}"
    reason: str | None = None
    if run["error"] is not None:
        reason = run["error"]
    elif not attestation["valid"] and not attestation["violations"]:
        reason = "served model attestation failed: " + "; ".join(attestation["errors"])
    elif run["returncode"]:
        reason = f"reviewer exited with status {run['returncode']}"
    elif dirty:
        reason = "reviewer modified the checkout; refusing a read-only review verdict"
    elif current != head:
        reason = f"reviewer moved HEAD to {current!r}; the checkout is no longer the reviewed commit"
    report = None
    with contextlib.suppress(OSError, UnicodeDecodeError):
        report = lane.report.read_text(encoding="utf-8")
    return {
        "attestation": attestation,
        "reason": reason,
        "report": report,
        "complete": reason is None and not attestation["violations"],
        "returncode": run["returncode"],
    }


def run_review(
    pr_number: int,
    *,
    repo: Path,
    out: Path,
    model: str = DEFAULT_MODEL,
    contract: str | None = None,
    previous_review: str | None = None,
    comment: bool = False,
    timeout: int | None = None,
    mode: str = "auto",
    deep_threshold: int = DEEP_THRESHOLD_LINES,
    lanes: Sequence[str] | None = None,
    pin_head: str | None = None,
) -> dict[str, Any]:
    """Preserve evidence, refuse unattested verdicts, and remove every owned checkout.

    ``timeout`` is per reviewer process. In deep mode ``out/REVIEW.md`` is always written, with
    partial coverage marked and never reported as PASS.
    """
    if pr_number <= 0 or (timeout is not None and timeout <= 0) or deep_threshold < 0:
        raise ValueError("PR number and timeout must be positive and the deep threshold non-negative")
    if not re.fullmatch(r"[^\s/]+/[^\s]+", model):
        raise ValueError("--model must be an exact provider/model route")
    choose_mode(mode, 0, deep_threshold)  # reject unknown modes before any side effect
    unknown_lanes = [name for name in (lanes or ()) if name not in FOCUSES]
    if unknown_lanes or (lanes is not None and (not lanes or len(set(lanes)) != len(lanes))):
        raise ValueError(f"--lanes must be distinct names from {sorted(FOCUSES)}")
    lane_names = list(lanes) if lanes else None
    if pin_head is not None and not re.fullmatch(r"[0-9a-f]{40}", pin_head):
        raise ValueError("--head must be a full 40-hex commit id")
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    receipt: dict[str, Any] = {
        "schema": "evallab.independent_review/v1",
        "pr_number": pr_number,
        "requested_model": model,
        "valid": False,
        "errors": [],
        "comment_posted": False,
    }
    worktrees: list[Path] = []
    worktree_root: Path | None = None
    review = ""
    try:
        metadata = json.loads(
            _command(
                ["gh", "pr", "view", str(pr_number), "--json", "number,headRefOid,baseRefOid,url"],
                repo,
            )
        )
        pr_head, base = metadata["headRefOid"], metadata["baseRefOid"]
        if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (pr_head, base)):
            raise ValueError("PR metadata lacks exact head/base commit ids")
        receipt.update(base_sha=base, url=metadata["url"], pr_head_at_start=pr_head)
        _command(["git", "fetch", "origin", f"refs/pull/{pr_number}/head"], repo)
        fetched_head = _command(["git", "rev-parse", "FETCH_HEAD"], repo)
        if fetched_head != pr_head:
            raise ValueError("PR head changed while fetching; start a new review")
        _command(["git", "fetch", "origin", base], repo)
        # The verdict is bound to exactly this commit, not to the moving PR branch.
        head = pin_head or pr_head
        if head != pr_head:
            try:
                _command(["git", "merge-base", "--is-ancestor", head, pr_head], repo)
            except subprocess.CalledProcessError:
                raise ValueError("--head is neither the PR head nor one of its ancestors") from None
        receipt["head_sha"] = head
        delta_base: str | None = None
        previous_head: str | None = None
        if previous_review is not None:
            parse_review(previous_review)  # a base must itself be a complete, valid review
            previous_head = reviewed_head(previous_review, pr_number)
            if previous_head is None:
                receipt.setdefault("notes", []).append(
                    "earlier review is not bound to a commit; reviewing the full PR"
                )
            elif previous_head == head:
                raise ValueError("the earlier review already covers this exact commit; nothing to verify")
            else:
                try:
                    _command(["git", "merge-base", "--is-ancestor", previous_head, head], repo)
                    delta_base = previous_head
                except subprocess.CalledProcessError:
                    receipt.setdefault("notes", []).append(
                        "earlier reviewed commit is not an ancestor of the reviewed head; reviewing the full PR"
                    )
        receipt["previous_head_sha"] = delta_base
        range_spec = f"{delta_base}..{head}" if delta_base else f"{base}...{head}"
        lines = changed_lines(_command(["git", "diff", "--numstat", range_spec], repo))
        resolved = choose_mode(mode, lines, deep_threshold)
        deep = resolved == "deep"
        timeout = timeout or (DEEP_TIMEOUT if deep else SHALLOW_TIMEOUT)
        receipt.update(mode=resolved, changed_lines=lines, reviewer_timeout=timeout)

        worktree_root = Path(tempfile.mkdtemp(prefix=f"evallab-review-pr{pr_number}-", dir="/private/tmp"))
        if deep:
            lanes = [
                Lane(name, name, worktree_root / name, out / "reviewers" / name)
                for name in (lane_names or FOCUSES)
            ]
        else:
            lanes = [Lane("review", None, worktree_root / "checkout", out)]
        for lane in lanes:
            _command(["git", "worktree", "add", "--detach", str(lane.worktree), head], repo)
            worktrees.append(lane.worktree)
            lane.sessions.mkdir(parents=True, exist_ok=True)
            if lane.focus is not None:
                lane.scratch.mkdir()
        receipt["worktrees"] = {lane.name: str(lane.worktree) for lane in lanes}
        if not deep:
            receipt["worktree"] = str(lanes[0].worktree)

        rules = {path: (lanes[0].worktree / path).read_text(encoding="utf-8") for path in RULE_PATHS}
        diff_path = out / "pr.diff"
        diff_path.write_text(_command(["git", "diff", f"{base}...{head}"], lanes[0].worktree), encoding="utf-8")
        delta_path = out / "delta.diff"
        if delta_base is not None:
            delta_path.write_text(_command(["git", "diff", delta_base, head], lanes[0].worktree), encoding="utf-8")
        config = out / "review-config.yml"
        config.write_text("retry:\n  modelFallback: false\n", encoding="utf-8")
        # Subscription login remains available; inherited paid API keys do not.
        base_env = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}

        launches = []
        for lane in lanes:
            brief = render_brief(
                pr_number=pr_number, head_sha=head, base_sha=base, worktree=lane.worktree,
                review_path=lane.report, rules=rules, contract=contract,
                previous_review=previous_review, focus=lane.focus,
                scratch=lane.scratch if deep else None,
                budget_minutes=max(1, timeout // 60 - 3) if deep else None,
                lanes=lane_names, delta_base=delta_base,
            )
            brief += (
                f"\nThe pinned PR diff is saved at {diff_path}. Read it and inspect relevant source.\n"
            )
            if delta_base is not None:
                brief += f"The delta since the earlier review is saved at {delta_path}.\n"
            if deep:
                brief += (
                    "Real command execution is expected in this lane; state exactly which claims you "
                    "ran and which you only read.\n"
                )
            else:
                brief += (
                    "Only read, grep, glob and write tools are enabled. Do not execute tests; "
                    "state that tests were not executed. Write only the designated REVIEW.md; "
                    "do not modify the checkout or publish anything.\n"
                )
            brief_path = lane.directory / "brief.md"
            brief_path.write_text(brief, encoding="utf-8")
            argv = [
                "omp", "-p", "--model", model, "--thinking", "high", "--session-dir", str(lane.sessions),
                "--config", str(config), "--no-title", "--no-extensions", "--no-skills", "--no-rules",
                "--no-context-files", "--tools", DEEP_TOOLS if deep else SHALLOW_TOOLS,
                f"@{brief_path}",
            ]
            env = dict(base_env, TMPDIR=str(lane.scratch)) if deep else base_env
            launches.append((lane, argv, env))
        receipt["commands"] = {lane.name: argv for lane, argv, _ in launches}
        if not deep:
            receipt["command"] = launches[0][1]

        def launch(item: tuple[Lane, list[str], dict[str, str]]) -> dict[str, Any]:
            lane, argv, env = item
            run: dict[str, Any] = {"returncode": None, "error": None}
            try:
                run["returncode"] = _run_reviewer(
                    argv, cwd=lane.worktree, env=env, log_path=lane.directory / "omp.log", timeout=timeout
                )
            except subprocess.TimeoutExpired:
                run["error"] = f"reviewer timed out after {timeout}s and was killed"
            except OSError as error:
                run["error"] = f"reviewer could not run: {error}"
            return run

        with ThreadPoolExecutor(max_workers=len(launches)) as pool:
            runs = list(pool.map(launch, launches))
        assessed = {
            lane.name: _assess(lane, run, model, head)
            for (lane, _, _), run in zip(launches, runs, strict=True)
        }
        receipt["attestation"] = merge_attestations(
            {name: item["attestation"] for name, item in assessed.items()}, model
        ) if deep else assessed["review"]["attestation"]
        receipt["lanes"] = {
            name: {
                "returncode": item["returncode"],
                "complete": item["complete"],
                "reason": item["reason"],
                "attestation_valid": item["attestation"]["valid"],
            }
            for name, item in assessed.items()
        }
        if not deep:
            receipt["omp_returncode"] = assessed["review"]["returncode"]

        refusal: str | None = None
        violations = receipt["attestation"]["violations"]
        if violations:
            refusal = "served model attestation failed: " + "; ".join(violations)
        elif not deep and not assessed["review"]["complete"]:
            refusal = assessed["review"]["reason"]
        else:
            # A moved PR head is not an error: the verdict is bound to `head` alone and the
            # report says so. Whatever came after needs its own --verify-fixes review.
            current_head = json.loads(
                _command(["gh", "pr", "view", str(pr_number), "--json", "headRefOid"], repo)
            )["headRefOid"]
            receipt["pr_head_after"] = current_head
            receipt["head_moved"] = current_head != head

        skipped = [name for name in FOCUSES if lane_names and name not in lane_names]

        def bound(text: str, *, coverage_complete: bool = True) -> str:
            return bind_report(
                text, pr_number=pr_number, head_sha=head, base_sha=base, model=model,
                mode=resolved, previous_head=delta_base, pr_head_after=receipt.get("pr_head_after"),
                coverage_complete=coverage_complete,
            )

        if deep:
            parts = [
                {
                    "name": lane.name,
                    "complete": item["complete"] and refusal is None,
                    "report": item["report"],
                    "reason": refusal or item["reason"],
                }
                for lane, _, _ in launches
                for item in [assessed[lane.name]]
            ]
            consolidated = consolidate_reviews(
                parts, pr_number=pr_number, head_sha=head, base_sha=base, model=model,
                skipped=skipped,
            )
            coverage_complete = not consolidated["coverage"]["incomplete"] and not skipped
            review = bound(consolidated["report"], coverage_complete=coverage_complete)
            (out / "REVIEW.md").write_text(review, encoding="utf-8")
            receipt["coverage"] = consolidated["coverage"]
            for name, detail in receipt["lanes"].items():
                detail["complete"] = name in consolidated["coverage"]["complete"]
                detail["reason"] = consolidated["coverage"]["incomplete"].get(name)
        if refusal is not None:
            raise ValueError(refusal)

        if deep:
            parse_review(review)  # the consolidated report must itself be a valid review
            verdict = consolidated["verdict"]
            if verdict == "INCONCLUSIVE":
                receipt["outcome"] = "INCONCLUSIVE"
                raise ValueError(
                    "deep review is inconclusive: no blocker found but lanes are incomplete: "
                    + ", ".join(consolidated["coverage"]["incomplete"])
                )
        else:
            review = assessed["review"]["report"]
            if review is None:
                raise ValueError("reviewer wrote no REVIEW.md")
            verdict = parse_review(review)["verdict"]
            (out / "REVIEW.reviewer.md").write_text(review, encoding="utf-8")
            review = bound(review)
            (out / "REVIEW.md").write_text(review, encoding="utf-8")
        receipt.update(valid=True, verdict=verdict, review_path=str(out / "REVIEW.md"))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        receipt["valid"] = False
        receipt.pop("verdict", None)
        receipt["errors"].append(str(error))
    finally:
        removed = bool(worktrees)
        for worktree in worktrees:
            try:
                # A reviewer may have left this checkout dirty; _assess already recorded that as
                # dirty.txt and made the lane incomplete, so force removal must not fail the run.
                _command(["git", "worktree", "remove", "--force", str(worktree)], repo)
            except (OSError, subprocess.SubprocessError) as error:
                removed = False
                receipt["valid"] = False
                receipt.pop("verdict", None)
                receipt["errors"].append(f"worktree cleanup failed; preserved at {worktree}: {error}")
        if worktrees:
            receipt["worktree_removed"] = removed
        if worktree_root is not None:
            with contextlib.suppress(OSError):
                worktree_root.rmdir()  # Never force-delete unknown files left in the root.
    if receipt["valid"] and comment and receipt["verdict"] in ("PASS", "BLOCK"):
        try:
            body = out / "comment.md"
            body.write_text(
                comment_summary(review, verdict=receipt["verdict"], head=head, model=model),
                encoding="utf-8",
            )
            _command(["gh", "pr", "comment", str(pr_number), "--body-file", str(body)], repo)
            receipt["comment_posted"] = True
        except (OSError, subprocess.SubprocessError) as error:
            receipt["valid"] = False
            receipt.pop("verdict", None)
            receipt["errors"].append(f"comment failed: {error}")
    report_file = out / "REVIEW.md"
    if not receipt["valid"] and report_file.is_file():
        # A rejected review must never read as a verdict to someone who opens only REVIEW.md.
        text = report_file.read_text(encoding="utf-8").rstrip()
        if text.endswith("VERDICT: PASS"):
            text = text.removesuffix("VERDICT: PASS") + "VERDICT: BLOCK"
        if not text.startswith(REFUSED_PREFIX):
            reasons = " ".join("; ".join(receipt["errors"]).split())
            text = f"{REFUSED_PREFIX} {reasons}**\n\n{text}"
        report_file.write_text(text + "\n", encoding="utf-8")
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def check_receipt(receipt_path: Path, pr_number: int, repo: Path) -> dict[str, Any]:
    """Merge-time rule: does the evidence behind a receipt cover the PR's head right now?

    The receipt's own fields are never trusted: the evidence directory is re-derived first, and
    only the verified head and verdict are compared with the PR head. This catches stale,
    accidental and inconsistent results; it cannot stop a deliberate forger who can write the
    evidence directory.
    """
    try:
        verified = verify_evidence(receipt_path)
    except (OSError, ValueError) as error:
        return {
            "pr_number": pr_number,
            "reviewed_sha": None,
            "current_head": None,
            "verdict": None,
            "covers": False,
            "action": "evidence does not verify; run a new review",
            "problems": [item for item in str(error).split("; ") if item.strip()] or [repr(error)],
        }
    current = _command(["gh", "pr", "view", str(pr_number), "--json", "headRefOid", "-q", ".headRefOid"], repo)
    status = binding_status(
        {
            "schema": "evallab.independent_review/v1",
            "pr_number": verified["pr_number"],
            "valid": True,
            "verdict": verified["verdict"],
            "head_sha": verified["head_sha"],
        },
        pr_number,
        current,
    )
    status.update(mode=verified.get("mode"), requested_model=verified.get("requested_model"))
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--verify-fixes", type=Path, metavar="PREV_REVIEW.md")
    parser.add_argument(
        "--head", metavar="SHA", help="review exactly this commit (the PR head or an ancestor of it)"
    )
    parser.add_argument(
        "--check", type=Path, metavar="RECEIPT.json",
        help="do not review: report whether this receipt's verdict covers the PR's current head "
        "(exit 0 covers a PASS, 1 covers a BLOCK, 4 does not cover, 2 invalid)",
    )
    parser.add_argument(
        "--mode", choices=("auto", "shallow", "deep"), default="auto",
        help="auto: deep when the PR changes more than --deep-threshold lines",
    )
    parser.add_argument("--deep-threshold", type=int, default=DEEP_THRESHOLD_LINES)
    parser.add_argument(
        "--lanes", help="comma-separated subset of deep lanes to run (default: all): " + ",".join(FOCUSES)
    )
    parser.add_argument("--comment", action="store_true", help="publish a short advisory summary (default: local only)")
    parser.add_argument("--out", type=Path, help="new evidence directory; never overwritten")
    parser.add_argument(
        "--timeout", type=int,
        help=f"seconds per reviewer process (default {SHALLOW_TIMEOUT} shallow, {DEEP_TIMEOUT} deep)",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            repo = Path(_command(["git", "rev-parse", "--show-toplevel"], Path.cwd()))
            status = check_receipt(args.check, args.pr, repo)
            print(json.dumps(status, indent=2))
            if not status["covers"]:
                return 4 if status["action"].startswith("PR head differs") else 2
            return 0 if status["verdict"] == "PASS" else 1
        repo = Path(_command(["git", "rev-parse", "--show-toplevel"], Path.cwd()))
        contract = args.contract.read_text(encoding="utf-8") if args.contract else None
        previous = args.verify_fixes.read_text(encoding="utf-8") if args.verify_fixes else None
        out = args.out.resolve() if args.out else Path(tempfile.mkdtemp(prefix="evallab-review-evidence-", dir="/private/tmp")) / f"pr-{args.pr}"
        if out.is_relative_to(repo.resolve()):
            raise ValueError("review evidence must live outside the repository")
        receipt = run_review(
            args.pr, repo=repo, out=out, model=args.model, contract=contract,
            previous_review=previous, comment=args.comment, timeout=args.timeout,
            mode=args.mode, deep_threshold=args.deep_threshold,
            lanes=args.lanes.split(",") if args.lanes else None, pin_head=args.head,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Review refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2))
    if not receipt["valid"]:
        return 3 if receipt.get("outcome") == "INCONCLUSIVE" else 2
    return {"PASS": 0, "BLOCK": 1}[receipt["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
