"""Check observable delivery, not the worker's completion claim."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable

from evallab.fleet.state import Lane

Run = Callable[..., subprocess.CompletedProcess[str]]


def verify(lane: Lane, *, run: Run = subprocess.run, hook: list[str] | None = None) -> dict:
    reasons: list[str] = []
    prs: list[dict] = []
    result = lane.directory / "RESULT.md"
    if not result.is_file():
        return {"verified": False, "reasons": ["RESULT.md is missing"], "prs": []}
    try:
        text = result.read_text()
    except (OSError, UnicodeError) as error:
        return {"verified": False, "reasons": [f"RESULT.md cannot be read: {error}"], "prs": []}
    if not lane.served or set(lane.served) != {lane.route}:
        reasons.append("Served-route evidence is missing or differs from the requested route")
    citations = re.findall(
        r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+\b|(?:#|\bPR\s+)\d+\b",
        text,
    )
    references = {
        citation if citation.startswith("https://") else re.sub(r"^(?:#|PR\s+)", "", citation)
        for citation in citations
    }
    if not references:
        reasons.append("RESULT.md cites no PR")
    cwd = lane.worktree or str(lane.directory)

    def command(argv: list[str]) -> str:
        response = run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
        if response.returncode:
            raise ValueError(f"{' '.join(argv)}: {response.stderr.strip()}")
        return response.stdout.strip()

    for reference in sorted(references):
        number = reference.rsplit("/", 1)[-1]
        try:
            pr = json.loads(
                command(
                    [
                        "gh",
                        "pr",
                        "view",
                        reference,
                        "--json",
                        "url,state,headRefOid,statusCheckRollup,mergeCommit",
                    ]
                )
            )
            prs.append(pr)
            # A PR citation is a delivery claim. Do not infer success from prose.
            if pr["state"] != "MERGED":
                reasons.append(f"PR #{number} is {pr['state']}, not MERGED")
            paragraphs = [
                paragraph
                for paragraph in text.split("\n\n")
                if (reference.startswith("https://") and reference in paragraph)
                or re.search(r"(?:#|\bPR\s+)" + re.escape(number) + r"\b", paragraph)
            ]
            claims: list[str] = []
            for paragraph in paragraphs:
                ids = set(re.findall(r"(?:pull/|#|\bPR\s+)(\d+)\b", paragraph))
                if len(ids) == 1:
                    claims.append(paragraph)
                else:
                    claims.extend(
                        line
                        for line in paragraph.splitlines()
                        if set(re.findall(r"(?:pull/|#|\bPR\s+)(\d+)\b", line)) == {number}
                    )
            if not any(pr["headRefOid"] in claim for claim in claims):
                reasons.append(f"PR #{number} exact head SHA is absent from its citation claim")
            checks = pr.get("statusCheckRollup") or []
            successful = {
                check.get("name")
                for check in checks
                if check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS"
            }
            if not {"quality-required", "typecheck-required"} <= successful:
                reasons.append(f"PR #{number} lacks successful required checks")
            if any(
                check.get("conclusion") != "SUCCESS"
                if "conclusion" in check
                else check.get("state") != "SUCCESS"
                for check in checks
            ):
                reasons.append(f"PR #{number} has a non-successful check")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            reasons.append(f"PR #{number} cannot be verified: {error}")
    if lane.worktree is None:
        reasons.append("No worktree is registered")
    else:
        try:
            if command(["git", "status", "--porcelain"]):
                reasons.append("Worktree is not clean")
            head = command(["git", "rev-parse", "HEAD"])
            branch = command(["git", "symbolic-ref", "--short", "HEAD"])
            remote = command(["git", "ls-remote", "origin", f"refs/heads/{branch}"])
            if remote.split()[:1] != [head] and not any(
                pr.get("state") == "MERGED" and pr.get("headRefOid") == head for pr in prs
            ):
                reasons.append("Branch HEAD is not pushed or bound to a merged PR")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reasons.append(f"Worktree cannot be verified: {error}")
    if hook:
        try:
            command([*hook, str(lane.directory)])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reasons.append(f"Lane verifier failed: {error}")
    return {"verified": not reasons, "reasons": reasons, "prs": prs}
