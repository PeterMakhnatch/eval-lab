#!/usr/bin/env python3
"""Automated Self-Repairing Daily Digest Generator.

Generates daily markdown digests for the eval-lab and research context repos,
recording git commit snapshot, GitHub PR health, and newly created/modified research documents.
Self-repairs missing digest dates using runs/digest-state.json watermark.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import yaml


class FrontmatterDoc:
    """Represents a research markdown document with parsed frontmatter."""

    def __init__(
        self,
        path: Path,
        date_str: str,
        author: str,
        summary: str,
        status: str,
        time_str: str,
        relative_link: str,
        name: str | None = None,
    ) -> None:
        self.path = path
        self.date_str = date_str
        self.author = author
        self.summary = summary
        self.status = status
        self.time_str = time_str
        self.relative_link = relative_link
        self.name = name or path.name


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter delimited by '---' from markdown content."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm_raw = parts[1]
            body = parts[2]
            try:
                data = yaml.safe_load(fm_raw)
                if isinstance(data, dict):
                    return data, body
            except Exception:
                pass
    return {}, content


def format_frontmatter_date(date_val: Any) -> str:
    """Normalize frontmatter date value to YYYY-MM-DD string."""
    if isinstance(date_val, (datetime.date, datetime.datetime)):
        return date_val.strftime("%Y-%m-%d")
    if isinstance(date_val, str):
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", date_val)
        if match:
            return match.group(1)
        return date_val.strip()
    return ""


def format_frontmatter_time(time_val: Any) -> str:
    """Extract or format time as HH:MM:SS string."""
    if isinstance(time_val, datetime.datetime):
        return time_val.strftime("%H:%M:%S")
    if isinstance(time_val, datetime.time):
        return time_val.strftime("%H:%M:%S")
    if isinstance(time_val, str):
        match = re.search(r"(\d{2}:\d{2}(?::\d{2})?)", time_val)
        if match:
            t = match.group(1)
            if len(t) == 5:
                return f"{t}:00"
            return t
    return "00:00:00"


def extract_doc_date(file_path: Path, fm: dict[str, Any]) -> str:
    """Extract document date from frontmatter or filename."""
    for field in ("date", "retrieved", "reviewed", "created", "updated"):
        val = fm.get(field)
        if val:
            d = format_frontmatter_date(val)
            if d:
                return d

    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", file_path.name)
    if match:
        return match.group(1)

    return ""


def scan_filesystem(
    repo_a: Path,
    repo_b: Path | None,
    target_date: str,
    output_dir: Path,
    vault_root: Path | None = None,
) -> list[FrontmatterDoc]:
    """Scan research directories for .md documents matching target_date."""
    dirs_to_scan: list[tuple[Path, Path, str]] = []
    if repo_a and repo_a.is_dir():
        for sub in ("inbox", "analysis", "explorations"):
            d = repo_a / "research" / sub
            if d.is_dir():
                dirs_to_scan.append((d, repo_a, "repo_a"))
    if repo_b and repo_b.is_dir():
        for sub in ("trajectory-analysis", "benchmarks"):
            d = repo_b / sub
            if d.is_dir():
                dirs_to_scan.append((d, repo_b, "repo_b"))

    matched_docs: list[FrontmatterDoc] = []
    for scan_dir, base_repo, repo_kind in dirs_to_scan:
        for file_path in sorted(scan_dir.rglob("*.md")):
            if not file_path.is_file():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            fm, _ = parse_frontmatter(content)
            doc_date = extract_doc_date(file_path, fm)
            if doc_date != target_date:
                continue

            # Calculate relative link from eval-lab/digests/ to document in vault
            if repo_kind == "repo_a":
                try:
                    rel_to_repo = file_path.resolve().relative_to(base_repo.resolve())
                    rel_path = f"../{rel_to_repo.as_posix()}"
                except Exception:
                    rel_path = os.path.relpath(file_path.resolve(), start=output_dir.resolve())
            else:
                try:
                    rel_to_repo = file_path.resolve().relative_to(base_repo.resolve())
                    rel_path = f"../../research-context/{rel_to_repo.as_posix()}"
                except Exception:
                    rel_path = os.path.relpath(file_path.resolve(), start=output_dir.resolve())

            # Verify that the target file exists on disk
            if not file_path.resolve().exists():
                continue

            author = str(fm.get("author", "unknown"))
            summary = str(fm.get("summary", "")).strip()
            status = str(fm.get("status", "unknown"))
            time_str = format_frontmatter_time(fm.get("time"))
            doc_name = str(fm.get("title") or file_path.stem)

            matched_docs.append(
                FrontmatterDoc(
                    path=file_path,
                    date_str=doc_date,
                    author=author,
                    summary=summary,
                    status=status,
                    time_str=time_str,
                    relative_link=rel_path,
                    name=doc_name,
                )
            )

    matched_docs.sort(key=lambda d: (d.time_str, d.name, str(d.path)))
    return matched_docs


def query_git_head_sha(repo_path: Path) -> tuple[str | None, str | None]:
    """Query origin/main or HEAD short SHA in given repo. Returns (sha, error_reason)."""
    if not repo_path.is_dir():
        return None, f"repository path {repo_path} not found"

    # Try origin/main short sha
    res = subprocess.run(
        ["git", "rev-parse", "--short", "origin/main"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip(), None

    # Fallback to HEAD short sha
    res_head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if res_head.returncode == 0 and res_head.stdout.strip():
        return res_head.stdout.strip(), None

    err = (res.stderr or res_head.stderr or "git rev-parse failed").strip()
    return None, err


def query_git_commits_in_window(repo_path: Path, target_date: str) -> tuple[int | None, str | None]:
    """Query count of git commits merged/committed in target_date window."""
    if not repo_path.is_dir():
        return None, f"repository path {repo_path} not found"

    cmd = [
        "git",
        "log",
        f"--since={target_date} 00:00:00",
        f"--until={target_date} 23:59:59",
        "--oneline",
    ]
    res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
    if res.returncode != 0:
        return None, (res.stderr or "git log failed").strip()

    lines = [line for line in res.stdout.strip().splitlines() if line.strip()]
    return len(lines), None


def query_gh_prs(repo_path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Query GitHub PRs using gh CLI."""
    if not repo_path.is_dir():
        return None, f"repository path {repo_path} not found"

    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "all",
        "--limit",
        "200",
        "--json",
        "number,title,state,mergeStateStatus,statusCheckRollup,reviewDecision,updatedAt,mergedAt",
    ]
    try:
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
    except FileNotFoundError:
        return None, "gh CLI unavailable or not authenticated"
    except Exception as e:
        return None, f"gh execution failed: {e}"

    if res.returncode != 0:
        return None, "gh CLI unavailable or not authenticated"

    try:
        data = json.loads(res.stdout)
        if isinstance(data, list):
            return data, None
        return None, "unexpected gh output format"
    except Exception as e:
        return None, f"failed to parse gh output: {e}"


def is_check_green(check: dict[str, Any]) -> bool:
    """Determine if a status check is successful or neutral."""
    conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
    status = str(check.get("status") or "").upper()
    return conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") or status == "SUCCESS"


def is_check_failing(check: dict[str, Any]) -> bool:
    """Determine if a status check is failing."""
    conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
    status = str(check.get("status") or "").upper()
    return conclusion in (
        "FAILURE",
        "ERROR",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "CANCELLED",
        "STARTUP_FAILURE",
    ) or status in ("FAILURE", "ERROR")


def classify_prs(prs: list[dict[str, Any]] | None, target_date: str) -> dict[str, Any]:
    """Classify PRs into merged, green unreviewed, blocked/conflicted, and health stats."""
    if prs is None:
        return {
            "available": False,
            "reason": "gh CLI unavailable or not authenticated",
            "merged_prs": [],
            "open_count": 0,
            "green_count": 0,
            "failing_count": 0,
            "conflicted_count": 0,
            "green_unreviewed_prs": [],
            "blocked_or_conflicted_prs": [],
        }

    merged_prs: list[dict[str, Any]] = []
    green_unreviewed_prs: list[dict[str, Any]] = []
    blocked_or_conflicted_prs: list[dict[str, Any]] = []

    open_prs: list[dict[str, Any]] = []
    green_open_count = 0
    failing_open_count = 0
    conflicted_open_count = 0

    for pr in prs:
        state = str(pr.get("state", "")).upper()
        merged_at = pr.get("mergedAt")
        if (state == "MERGED" or merged_at) and merged_at:
            if str(merged_at).startswith(target_date):
                merged_prs.append(pr)

        if state == "OPEN":
            open_prs.append(pr)
            raw_checks = pr.get("statusCheckRollup")
            checks: list[dict[str, Any]] = []
            if isinstance(raw_checks, list):
                checks = [c for c in raw_checks if isinstance(c, dict)]
            elif isinstance(raw_checks, dict):
                checks = [raw_checks]

            has_failure = any(is_check_failing(c) for c in checks)
            has_success = len(checks) > 0 and all(is_check_green(c) for c in checks)

            merge_state = str(pr.get("mergeStateStatus") or "").upper()
            review_dec = str(pr.get("reviewDecision") or "").upper()

            is_conflicted = merge_state in ("DIRTY", "CONFLICTING", "BLOCKED")

            if has_success:
                green_open_count += 1
            if has_failure:
                failing_open_count += 1
            if is_conflicted:
                conflicted_open_count += 1

            if has_success and review_dec != "APPROVED":
                green_unreviewed_prs.append(pr)

            if has_failure or is_conflicted:
                blocked_or_conflicted_prs.append(pr)

    merged_prs.sort(key=lambda p: p.get("number", 0))
    green_unreviewed_prs.sort(key=lambda p: p.get("number", 0))
    blocked_or_conflicted_prs.sort(key=lambda p: p.get("number", 0))

    return {
        "available": True,
        "reason": None,
        "merged_prs": merged_prs,
        "open_count": len(open_prs),
        "green_count": green_open_count,
        "failing_count": failing_open_count,
        "conflicted_count": conflicted_open_count,
        "green_unreviewed_prs": green_unreviewed_prs,
        "blocked_or_conflicted_prs": blocked_or_conflicted_prs,
    }


def render_digest_content(
    target_date: str,
    sha: str | None,
    sha_err: str | None,
    commits_a: int | None,
    commits_a_err: str | None,
    commits_b: int | None,
    commits_b_err: str | None,
    pr_data: dict[str, Any],
    docs: list[FrontmatterDoc],
) -> str:
    """Render the exact four-section daily digest markdown."""
    lines: list[str] = [
        "---",
        f"date: {target_date}",
        "author: digest-automation",
        f"summary: Daily eval lab digest for {target_date}.",
        "status: distilled",
        "---",
        "",
        f"# Eval lab digest \u2014 {target_date}",
        "",
        "## Morning snapshot",
        "",
    ]

    # Section 1: Morning snapshot
    if sha:
        lines.append(f"- origin/main: {sha}")
    else:
        lines.append(f"- origin/main: unavailable: {sha_err or 'unknown error'}")

    count_a_str = str(commits_a) if commits_a is not None else "0"
    count_b_str = str(commits_b) if commits_b is not None else "0"
    lines.append(
        f"- Commits merged in window: {count_a_str} in eval-lab, {count_b_str} in research-context"
    )

    if pr_data["available"]:
        open_c = pr_data["open_count"]
        green_c = pr_data["green_count"]
        fail_c = pr_data["failing_count"]
        conf_c = pr_data["conflicted_count"]
        lines.append(f"- Open PRs: {open_c} open PRs ({green_c} green, {fail_c} failing, {conf_c} conflicted)")
    else:
        lines.append(f"- Open PRs: unavailable: {pr_data.get('reason') or 'gh CLI unavailable or not authenticated'}")

    # Section 2: What changed and what was learned
    lines.extend([
        "",
        "## What changed and what was learned",
        "",
    ])

    learned_bullets: list[str] = []
    if pr_data["available"]:
        for pr in pr_data["merged_prs"]:
            num = pr.get("number")
            title = pr.get("title", "").strip()
            learned_bullets.append(f"- PR #{num}: {title}")

    for doc in docs:
        if doc.summary:
            learned_bullets.append(f"- {doc.name}: {doc.summary}")

    if learned_bullets:
        lines.extend(learned_bullets)
    else:
        lines.append("- No PRs merged or reports updated in this window.")

    # Section 3: Landed versus in flight
    lines.extend([
        "",
        "## Landed versus in flight",
        "",
    ])

    if not pr_data["available"]:
        lines.append(f"unavailable: {pr_data.get('reason') or 'gh CLI unavailable or not authenticated'}")
    else:
        lines.append("### Merged PRs")
        lines.append("")
        if pr_data["merged_prs"]:
            for pr in pr_data["merged_prs"]:
                lines.append(f"- PR #{pr.get('number')}: {pr.get('title', '').strip()}")
        else:
            lines.append("- None.")

        lines.append("")
        lines.append("### Green-and-unreviewed PRs")
        lines.append("")
        if pr_data["green_unreviewed_prs"]:
            for pr in pr_data["green_unreviewed_prs"]:
                lines.append(f"- PR #{pr.get('number')}: {pr.get('title', '').strip()}")
        else:
            lines.append("- None.")

        lines.append("")
        lines.append("### Blocked or conflicted PRs")
        lines.append("")
        if pr_data["blocked_or_conflicted_prs"]:
            for pr in pr_data["blocked_or_conflicted_prs"]:
                lines.append(f"- PR #{pr.get('number')}: {pr.get('title', '').strip()}")
        else:
            lines.append("- None.")

    # Section 4: Documents created or changed
    lines.extend([
        "",
        "## Documents created or changed",
        "",
        "| Time | Author | Document | Summary |",
        "|---|---|---|---|",
    ])

    if docs:
        for doc in docs:
            summary_escaped = doc.summary.replace("|", "\\|")
            author_escaped = doc.author.replace("|", "\\|")
            doc_link = f"[{doc.name}]({doc.relative_link})"
            lines.append(f"| {doc.time_str} | {author_escaped} | {doc_link} | {summary_escaped} |")
    else:
        lines.append("| - | - | - | No documents created or changed on this date. |")

    lines.append("")
    return "\n".join(lines)


def get_watermark(watermark_path: Path, digests_dir: Path) -> str | None:
    """Read last successful digest date from watermark state or discover latest existing digest."""
    if watermark_path.is_file():
        try:
            data = json.loads(watermark_path.read_text(encoding="utf-8"))
            last_date = data.get("last_successful_digest_date")
            if last_date and re.match(r"^\d{4}-\d{2}-\d{2}$", str(last_date)):
                return str(last_date)
        except Exception:
            pass

    # Discover latest date from digests/*.md
    if digests_dir.is_dir():
        dates: list[str] = []
        for file in digests_dir.glob("*.md"):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", file.name)
            if m:
                dates.append(m.group(1))
        if dates:
            dates.sort()
            return dates[-1]

    return None


def determine_date_range(
    watermark_date: str | None,
    target_date: str,
    backfill: bool = False,
) -> list[str]:
    """Compute list of dates to generate in chronological order."""
    target_dt = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
    if watermark_date:
        watermark_dt = datetime.datetime.strptime(watermark_date, "%Y-%m-%d").date()
        start_dt = watermark_dt + datetime.timedelta(days=1)
        if start_dt <= target_dt:
            dates: list[str] = []
            curr = start_dt
            while curr <= target_dt:
                dates.append(curr.strftime("%Y-%m-%d"))
                curr += datetime.timedelta(days=1)
            return dates
    return [target_date]


def update_watermark(watermark_path: Path, last_date: str, generated_dates: list[str]) -> None:
    """Update watermark JSON state file."""
    watermark_path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    if watermark_path.is_file():
        try:
            state = json.loads(watermark_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    existing_generated = set(state.get("generated_dates", []))
    existing_generated.update(generated_dates)

    state["last_successful_digest_date"] = last_date
    state["generated_dates"] = sorted(list(existing_generated))

    watermark_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def generate_single_digest(
    target_date: str,
    repo_a: Path,
    repo_b: Path | None,
    output_dir: Path,
    vault_root: Path | None = None,
) -> Path:
    """Generate and write a single daily digest for target_date."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Query git
    sha, sha_err = query_git_head_sha(repo_a)
    commits_a, commits_a_err = query_git_commits_in_window(repo_a, target_date)
    commits_b: int | None = 0
    commits_b_err: str | None = None
    if repo_b and repo_b.is_dir():
        commits_b, commits_b_err = query_git_commits_in_window(repo_b, target_date)

    # 2. Query GitHub PRs
    raw_prs, _ = query_gh_prs(repo_a)
    pr_data = classify_prs(raw_prs, target_date)

    # 3. Filesystem scan for documents
    docs = scan_filesystem(repo_a, repo_b, target_date, output_dir, vault_root=vault_root)

    # 4. Render markdown content
    content = render_digest_content(
        target_date=target_date,
        sha=sha,
        sha_err=sha_err,
        commits_a=commits_a,
        commits_a_err=commits_a_err,
        commits_b=commits_b,
        commits_b_err=commits_b_err,
        pr_data=pr_data,
        docs=docs,
    )

    out_file = output_dir / f"{target_date}.md"
    out_file.write_text(content, encoding="utf-8")
    return out_file


def main(argv: list[str] | None = None) -> int:
    """CLI Entrypoint."""
    parser = argparse.ArgumentParser(description="Automated Daily Digest Generator")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date for digest (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill missing digest dates from watermark to target date.",
    )
    parser.add_argument(
        "--vault-root",
        type=str,
        default=None,
        help="Obsidian vault root directory path.",
    )
    parser.add_argument(
        "--repo-a",
        type=str,
        default=None,
        help="Path to eval-lab repository root.",
    )
    parser.add_argument(
        "--repo-b",
        type=str,
        default=None,
        help="Path to research-context repository root.",
    )
    parser.add_argument(
        "--watermark",
        type=str,
        default=None,
        help="Path to runs/digest-state.json watermark file.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Path to digests output directory.",
    )

    args = parser.parse_args(argv)

    default_repo_a = Path(__file__).resolve().parents[1]
    repo_a = Path(args.repo_a).resolve() if args.repo_a else default_repo_a

    vault_root = Path(args.vault_root).resolve() if args.vault_root else Path("/Users/petermakhnatch/Developer")

    # Resolve repo_b: check args, sibling of repo_a, or vault_root/research-context
    if args.repo_b:
        repo_b = Path(args.repo_b).resolve()
    elif (repo_a.parent / "research-context").is_dir():
        repo_b = (repo_a.parent / "research-context").resolve()
    elif (vault_root / "research-context").is_dir():
        repo_b = (vault_root / "research-context").resolve()
    else:
        repo_b = None

    output_dir = Path(args.output_dir).resolve() if args.output_dir else (repo_a / "digests")
    watermark_path = (
        Path(args.watermark).resolve() if args.watermark else (repo_a / "runs" / "digest-state.json")
    )

    target_date = args.date or datetime.datetime.now().strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", target_date):
        print(f"Error: Invalid date format '{target_date}', expected YYYY-MM-DD", file=sys.stderr)
        return 1

    watermark_date = get_watermark(watermark_path, output_dir)
    dates_to_generate = determine_date_range(watermark_date, target_date, args.backfill)

    generated: list[str] = []
    for d in dates_to_generate:
        out_file = generate_single_digest(d, repo_a, repo_b, output_dir, vault_root=vault_root)
        print(f"Generated digest: {out_file}")
        generated.append(d)

    update_watermark(watermark_path, target_date, generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
