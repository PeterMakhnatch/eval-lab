#!/usr/bin/env python3
"""Run an advisory, model-attested independent review of a pinned PR head."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.independent_review_contract import attest_sessions, extract_verdict, render_brief

DEFAULT_MODEL = "google-antigravity/gemini-4-argon"
RULE_PATHS = (
    "AGENTS.md",
    "src/evallab/AGENTS.md",
    "agents/WORKFLOW.md",
    "agents/CHECKS.md",
    "agents/STRUCTURE.md",
)


def _command(argv: list[str], cwd: Path) -> str:
    return subprocess.run(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True
    ).stdout.strip()


def comment_summary(review: str, *, verdict: str, head: str, model: str) -> str:
    """Bound the optional public summary; the full report stays in local evidence."""
    blocking = review.split("## Blocking", 1)[1].split("## ", 1)[0].strip()
    return (
        f"Independent review (advisory): **{verdict}**\n\n"
        f"Head: `{head}`\nServed model: `{model}` (attested; no fallback).\n\n"
        f"### Blocking findings\n{blocking[:1800]}\n\n"
        "Full REVIEW.md and session attestation are retained by the operator. "
        "This is not a CI status, merge approval, or proof that tests passed."
    )


def run_review(
    pr_number: int,
    *,
    repo: Path,
    out: Path,
    model: str = DEFAULT_MODEL,
    contract: str | None = None,
    previous_review: str | None = None,
    comment: bool = False,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Preserve evidence, reject untrusted verdicts, and remove the owned checkout."""
    if pr_number <= 0 or timeout <= 0:
        raise ValueError("PR number and timeout must be positive")
    if not re.fullmatch(r"[^\s/]+/[^\s]+", model):
        raise ValueError("--model must be an exact provider/model route")
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
    worktree: Path | None = None
    worktree_root: Path | None = None
    try:
        metadata = json.loads(
            _command(
                ["gh", "pr", "view", str(pr_number), "--json", "number,headRefOid,baseRefOid,url"],
                repo,
            )
        )
        head, base = metadata["headRefOid"], metadata["baseRefOid"]
        if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, base)):
            raise ValueError("PR metadata lacks exact head/base commit ids")
        receipt.update(head_sha=head, base_sha=base, url=metadata["url"])
        _command(["git", "fetch", "origin", f"refs/pull/{pr_number}/head"], repo)
        fetched_head = _command(["git", "rev-parse", "FETCH_HEAD"], repo)
        if fetched_head != head:
            raise ValueError("PR head changed while fetching; start a new review")
        _command(["git", "fetch", "origin", base], repo)
        worktree_root = Path(tempfile.mkdtemp(prefix=f"evallab-review-pr{pr_number}-", dir="/private/tmp"))
        checkout = worktree_root / "checkout"
        _command(["git", "worktree", "add", "--detach", str(checkout), head], repo)
        worktree = checkout
        receipt["worktree"] = str(worktree)
        rules = {path: (worktree / path).read_text(encoding="utf-8") for path in RULE_PATHS}
        review_path = out / "REVIEW.md"
        diff_path = out / "pr.diff"
        diff_path.write_text(_command(["git", "diff", f"{base}...{head}"], worktree), encoding="utf-8")
        brief = render_brief(
            pr_number=pr_number,
            head_sha=head,
            base_sha=base,
            worktree=worktree,
            review_path=review_path,
            rules=rules,
            contract=contract,
            previous_review=previous_review,
        )
        brief += (
            f"\nThe pinned PR diff is saved at {diff_path}. Read it and inspect relevant source.\n"
            "Only read, grep, glob and write tools are enabled. Do not execute tests; "
            "state that tests were not executed. Write only the designated REVIEW.md; "
            "do not modify the checkout or publish anything.\n"
        )
        brief_path = out / "brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        config = out / "review-config.yml"
        config.write_text("retry:\n  modelFallback: false\n", encoding="utf-8")
        sessions = out / "sessions"
        sessions.mkdir()
        argv = [
            "omp", "-p", "--model", model, "--thinking", "high", "--session-dir", str(sessions),
            "--config", str(config), "--no-title", "--no-extensions", "--no-skills", "--no-rules",
            "--no-context-files", "--tools", "read,grep,glob,write", f"@{brief_path}",
        ]
        receipt["command"] = argv
        # Subscription login remains available; inherited paid API keys do not.
        env = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}
        with (out / "omp.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(
                argv, cwd=worktree, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=env, timeout=timeout, check=False,
            )
        receipt["omp_returncode"] = process.returncode
        attestation = attest_sessions(sorted(sessions.rglob("*.jsonl")), model)
        receipt["attestation"] = attestation
        if not attestation["valid"]:
            raise ValueError("served model attestation failed: " + "; ".join(attestation["errors"]))
        if process.returncode:
            raise ValueError(f"reviewer exited with status {process.returncode}")
        dirty = _command(["git", "status", "--porcelain", "--untracked-files=all"], worktree)
        if dirty:
            raise ValueError("reviewer modified the checkout; refusing a read-only review verdict")
        current_head = json.loads(
            _command(["gh", "pr", "view", str(pr_number), "--json", "headRefOid"], repo)
        )["headRefOid"]
        if current_head != head:
            raise ValueError("PR head changed during review; review is stale")
        review = review_path.read_text(encoding="utf-8")
        verdict = extract_verdict(review)
        receipt.update(valid=True, verdict=verdict, review_path=str(review_path))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        receipt["valid"] = False
        receipt.pop("verdict", None)
        receipt["errors"].append(str(error))
    finally:
        if worktree is not None:
            try:
                _command(["git", "worktree", "remove", str(worktree)], repo)
                receipt["worktree_removed"] = True
            except (OSError, subprocess.SubprocessError) as error:
                receipt["worktree_removed"] = False
                receipt["valid"] = False
                receipt.pop("verdict", None)
                receipt["errors"].append(f"worktree cleanup failed; preserved at {worktree}: {error}")
        if worktree_root is not None:
            with contextlib.suppress(OSError):
                worktree_root.rmdir()  # Never force-delete a dirty checkout or unknown evidence.
    if receipt["valid"] and comment:
        try:
            body = out / "comment.md"
            body.write_text(
                comment_summary(review, verdict=verdict, head=head, model=model), encoding="utf-8"
            )
            _command(["gh", "pr", "comment", str(pr_number), "--body-file", str(body)], repo)
            receipt["comment_posted"] = True
        except (OSError, subprocess.SubprocessError) as error:
            receipt["valid"] = False
            receipt.pop("verdict", None)
            receipt["errors"].append(f"comment failed: {error}")
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--verify-fixes", type=Path, metavar="PREV_REVIEW.md")
    parser.add_argument("--comment", action="store_true", help="publish a short advisory summary (default: local only)")
    parser.add_argument("--out", type=Path, help="new evidence directory; never overwritten")
    parser.add_argument("--timeout", type=int, default=1800, help="reviewer timeout in seconds")
    args = parser.parse_args(argv)
    try:
        repo = Path(_command(["git", "rev-parse", "--show-toplevel"], Path.cwd()))
        contract = args.contract.read_text(encoding="utf-8") if args.contract else None
        previous = args.verify_fixes.read_text(encoding="utf-8") if args.verify_fixes else None
        out = args.out.resolve() if args.out else Path(tempfile.mkdtemp(prefix="evallab-review-evidence-", dir="/private/tmp")) / f"pr-{args.pr}"
        if out.is_relative_to(repo.resolve()):
            raise ValueError("review evidence must live outside the repository")
        receipt = run_review(
            args.pr, repo=repo, out=out, model=args.model, contract=contract,
            previous_review=previous, comment=args.comment, timeout=args.timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Review refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2))
    if not receipt["valid"]:
        return 2
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
