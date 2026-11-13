"""Supported PR publication without unchanged-patch CI cycles."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from evallab.ship import Blocked

USAGE = ("Publication refused: use ship open --title-file <file> --body-file <file>, "
         "ship comment <PR> --body-file <file>, or ship check-push [PR] [--needs-base <sha> --reason <text>].")
BASE_REFUSAL = "Push refused: --needs-base must name a commit SHA reachable from HEAD and origin/main."


def run(args: list[str], root: Path, *, data: bytes | None = None) -> bytes:
    result = subprocess.run(args, cwd=root, input=data, capture_output=True, timeout=300)
    if result.returncode:
        raise Blocked(f"Command refused ({result.returncode}): {' '.join(args)}; "
                      f"{result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def text(args: list[str], root: Path) -> str:
    return run(args, root).decode().strip()


def body_file(path: Path | None) -> tuple[Path, str]:
    if path is None:
        raise Blocked("Publication refused: --body-file is required; write the complete text to a UTF-8 file.")
    resolved = path.expanduser().resolve()
    body = resolved.read_text(encoding="utf-8").strip()
    if not body:
        raise Blocked("Publication refused: empty body; write the complete text in --body-file.")
    return resolved, body


def complete_body(body: str) -> None:
    substantive = []
    for line in re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).splitlines():
        if re.match(r"^\s*#", line):
            continue
        line = re.sub(r"^\s*(?:[-*+>]\s*|\d+[.)]\s*|\[[ xX]\]\s*)+", "", line).strip()
        if not line or re.fullmatch(r"[-*_ =]+", line):
            continue
        if re.fullmatch(r"(?:todo|tbd|wip|placeholder|none|n/a|coming soon|fill (?:this )?in|\.{3}|<[^>]*>|\[[^]]*\])[:.! ]*", line, re.IGNORECASE):
            raise Blocked("Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details.")
        substantive.append(line)
    if not substantive:
        raise Blocked("Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details.")


def branch_name(root: Path) -> str:
    return text(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], root)


def open_prs(root: Path, repo: str, branch: str) -> list[dict[str, Any]]:
    rows = json.loads(text(["gh", "pr", "list", "--repo", repo, "--state", "open", "--head", branch,
                           "--json", "number,url,headRefOid,baseRefName,isCrossRepository"], root))
    return [row for row in rows if not row["isCrossRepository"]]


def open_pr(args: argparse.Namespace, root: Path, repo: str) -> None:
    if args.title_file is None:
        raise Blocked("Publication refused: --title-file is required; write the final PR title to a UTF-8 file.")
    title = args.title_file.expanduser().resolve().read_text(encoding="utf-8").strip()
    if not title or len(title.splitlines()) != 1 or re.fullmatch(r"todo|tbd|wip|placeholder|\.{3}", title, re.IGNORECASE):
        raise Blocked("Publication refused: title is empty, multiline, or a placeholder; write the final title in --title-file.")
    path, body = body_file(args.body_file)
    complete_body(body)
    branch = branch_name(root)
    existing = open_prs(root, repo, branch)
    if existing:
        number = existing[0]["number"]
        raise Blocked(f"Publication refused: PR {number} already exists; add evidence with evallab ship comment {number} --body-file <file>. Do not edit its title or body.")
    head = text(["git", "rev-parse", "HEAD"], root)
    remote = text(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], root)
    if not remote or remote.split()[0] != head:
        raise Blocked(f"Publication refused: topic HEAD is not pushed; run make prepush, then git push -u origin {branch}.")
    print(text(["gh", "pr", "create", "--repo", repo, "--base", "main", "--head", branch,
                "--title", title, "--body-file", str(path)], root), flush=True)


def patch_id(root: Path, main: str, head: str) -> bytes:
    base = text(["git", "merge-base", main, head], root)
    patch = run(["git", "-c", "diff.algorithm=myers", "-c", "diff.context=3", "-c", "diff.ignoreSubmodules=none",
                 "-c", "diff.interHunkContext=0", "-c", "diff.suppressBlankEmpty=false", "diff",
                 "--binary", "--full-index", "--no-color", "--no-relative", "--no-ext-diff", "--no-textconv",
                 "--no-renames", "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/", base, head, "--"], root)
    rows = run(["git", "-c", "patchid.verbatim=false", "patch-id", "--stable"], root, data=patch).split()
    # Preserve header-only or empty diffs if this Git version emits no patch ID.
    return rows[0] if rows else b"\0" + patch


def check_push(args: argparse.Namespace, root: Path, repo: str) -> None:
    branch = branch_name(root)
    if args.target is None:
        prs = open_prs(root, repo, branch)
        if not prs:
            print("Push check passed: no open PR for this topic branch.", flush=True)
            return
        if len(prs) != 1:
            raise Blocked("Push refused: multiple open PRs match this branch; pass the intended PR number.")
        number = prs[0]["number"]
    else:
        number = args.target
    pr = json.loads(text(["gh", "pr", "view", str(number), "--repo", repo, "--json",
                          "number,state,headRefOid,headRefName,baseRefName,isCrossRepository"], root))
    if (pr["state"] != "OPEN" or pr["isCrossRepository"] or pr["baseRefName"] != "main"
            or pr["headRefName"] != branch):
        raise Blocked("Push refused: PR must be open, target main, and match this origin topic branch.")
    run(["git", "fetch", "--no-tags", "origin", "main"], root)
    main = text(["git", "rev-parse", "FETCH_HEAD"], root)
    run(["git", "fetch", "--no-tags", "origin", f"pull/{number}/head"], root)
    remote = text(["git", "rev-parse", "FETCH_HEAD"], root)
    if remote != pr["headRefOid"]:
        raise Blocked("Push refused: fetched PR head differs from GitHub; repeat check-push before pushing.")
    head = text(["git", "rev-parse", "HEAD"], root)
    if patch_id(root, main, head) != patch_id(root, main, remote):
        print("Push check passed: the patch differs from the current PR head.", flush=True)
        return
    message = (f"Push refused: same patch as PR head {remote}; main advancing does not require a new head "
               "(ship accepts it). Push only for a real conflict or a needed commit: add --needs-base <sha> with a reason.")
    if not args.needs_base or head == remote:
        raise Blocked(message)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.needs_base):
        raise Blocked(BASE_REFUSAL)
    try:
        needed = text(["git", "rev-parse", "--verify", "--end-of-options", args.needs_base + "^{commit}"], root)
        for tip in (head, main):
            run(["git", "merge-base", "--is-ancestor", needed, tip], root)
    except Blocked as exc:
        raise Blocked(BASE_REFUSAL) from exc
    print(f"Push check passed: explicit base dependency {needed}; reason: {args.reason.strip()}", flush=True)


def run_publish(args: argparse.Namespace, root: Path) -> int:
    try:
        mode = args.pr
        if args.preflight_only or args.smoke:
            raise Blocked("Publication refused: --preflight-only and --smoke apply only to numeric PR delivery.")
        if mode == "open":
            valid = args.target is None and args.needs_base is None and args.reason is None
        elif mode == "comment":
            valid = args.target is not None and args.title_file is None and args.needs_base is None and args.reason is None
        else:
            valid = (args.title_file is None and args.body_file is None and (
                (args.needs_base is None and args.reason is None)
                or bool(args.needs_base and args.needs_base.strip() and args.reason and args.reason.strip())))
        if not valid or (args.target is not None and args.target <= 0):
            raise Blocked(USAGE)
        root = Path(text(["git", "rev-parse", "--show-toplevel"], args.repo_root or root))
        repo = json.loads(text(["gh", "repo", "view", "--json", "nameWithOwner"], root))["nameWithOwner"]
        if mode == "open":
            open_pr(args, root, repo)
        elif mode == "comment":
            path, _ = body_file(args.body_file)
            print(text(["gh", "pr", "comment", str(args.target), "--repo", repo, "--body-file", str(path)], root), flush=True)
        else:
            check_push(args, root, repo)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(str(exc), flush=True)
        return 1
    return 0
