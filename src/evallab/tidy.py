"""E16: working tree tidy sweep reporting strays, stale worktrees, and retention violations.

Authority: docs/platform-architecture.md (T7, §2.6, §8).

Sweeps:
1. Stale worktrees: .worktrees/* whose branch is merged or deleted (skips dirty).
2. Merged local branches: role/* fully contained in origin/main without open PR.
3. Unindexed docs: docs/ absent from docs/INDEX.md or with missing/invalid front-matter.
4. Untracked strays: untracked files not gitignored, distinguishing recognized junk from drafts.
5: Retention violations: Z3 hot partitions >7d, unpromoted Z1 jobs >14d, events.jsonl >30d
   (report only).

Never-touch list (hard invariant):
- research/evidence/ (retention ∞, delete never)
- policy/ (standing approvals policy)
- Z5 briefs and handoffs (docs/prompts/, agents/handoffs/, board/, agents/briefs/)
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from evallab.docindex import collect_check_issues
from evallab.paths import derived_root_from_environment, shared_checkout_root

NEVER_TOUCH_PREFIXES: tuple[str, ...] = (
    "research/evidence",
    "policy",
    "docs/prompts",
    "agents/handoffs",
    "board",
    "agents/briefs",
)

RECOGNIZED_JUNK_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".tmp",
        ".temp",
        ".bak",
        ".backup",
        ".swp",
        ".swo",
        ".orig",
        ".rej",
        ".old",
        ".log",
        ".pyc",
        ".pyo",
        ".pyd",
    }
)

RECOGNIZED_JUNK_FILENAMES: frozenset[str] = frozenset(
    {
        ".DS_Store",
        "Thumbs.db",
        "dump.rdb",
        "core",
    }
)

RECOGNIZED_JUNK_DIR_PARTS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".coverage",
        ".mypy_cache",
        ".ruff_cache",
    }
)

RECOGNIZED_JUNK_PREFIXES: tuple[str, ...] = (
    "tmp_",
    "temp_",
    "scratch_",
    "test_output_",
)

RECOGNIZED_JUNK_STEMS: frozenset[str] = frozenset({"scratch", "temp", "tmp"})


def is_never_touch(rel_path: str) -> bool:
    """True if path matches the platform never-touch contract."""
    normalized = rel_path.replace("\\", "/").lstrip("./")
    for prefix in NEVER_TOUCH_PREFIXES:
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return True
    return False


def classify_junk(path: Path) -> str | None:
    """Return a reason string if path matches recognized junk signatures, else None."""
    name = path.name
    lower_name = name.lower()

    if name in RECOGNIZED_JUNK_FILENAMES:
        return f"system artifact: {name}"

    for part in path.parts:
        if part in RECOGNIZED_JUNK_DIR_PARTS:
            return f"build/cache directory artifact: {part}"

    if name.endswith("~"):
        return "editor backup file (~ suffix)"

    suffix = path.suffix.lower()
    if suffix in RECOGNIZED_JUNK_EXTENSIONS:
        return f"temporary/backup extension: {suffix}"

    for prefix in RECOGNIZED_JUNK_PREFIXES:
        if lower_name.startswith(prefix):
            return f"scratch file prefix: {prefix}"

    if path.stem.lower() in RECOGNIZED_JUNK_STEMS:
        return f"scratch file name: {path.stem}"

    return None


def dir_size_bytes(path: Path) -> int:
    """Calculate the total size in bytes of all regular files in a directory."""
    if not path.is_dir():
        return path.stat().st_size if path.is_file() else 0
    total = 0
    try:
        for root, _, files in os.walk(path):
            for file in files:
                file_path = Path(root) / file
                with contextlib.suppress(OSError):
                    total += file_path.stat().st_size
    except OSError:
        pass
    return total


def format_bytes(num_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _rel_path_str(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Findings Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeFinding:
    path: Path
    branch: str
    status: Literal["clean_merged", "clean_vanished", "dirty", "current"]
    file_count: int
    size_bytes: int
    reason: str
    actionable: bool


@dataclass(frozen=True)
class BranchFinding:
    branch: str
    status: Literal["merged_no_pr", "open_pr", "gh_unavailable", "active_worktree"]
    reason: str
    pr_number: int | None = None
    actionable: bool = False


@dataclass(frozen=True)
class DocFinding:
    path: Path
    size_bytes: int
    issue: str
    reason: str
    actionable: bool = False


@dataclass(frozen=True)
class StrayFinding:
    path: Path
    size_bytes: int
    is_junk: bool
    reason: str
    actionable: bool


@dataclass(frozen=True)
class RetentionFinding:
    path: Path
    category: Literal["z3_hot_partition", "z1_unpromoted_job", "events_log"]
    age_days: float
    size_bytes: int
    reason: str
    actionable: bool = False


@dataclass
class TidyReport:
    worktrees: list[WorktreeFinding] = field(default_factory=list)
    branches: list[BranchFinding] = field(default_factory=list)
    docs: list[DocFinding] = field(default_factory=list)
    strays: list[StrayFinding] = field(default_factory=list)
    retention: list[RetentionFinding] = field(default_factory=list)
    apply: bool = False
    deleted_worktrees: list[str] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    deleted_strays: list[str] = field(default_factory=list)

    @property
    def total_findings_count(self) -> int:
        return (
            len(self.worktrees)
            + len(self.branches)
            + len(self.docs)
            + len(self.strays)
            + len(self.retention)
        )

    @property
    def actionable_count(self) -> int:
        return (
            sum(1 for w in self.worktrees if w.actionable)
            + sum(1 for b in self.branches if b.actionable)
            + sum(1 for s in self.strays if s.actionable)
        )

    @property
    def total_bytes(self) -> int:
        return (
            sum(w.size_bytes for w in self.worktrees)
            + sum(d.size_bytes for d in self.docs)
            + sum(s.size_bytes for s in self.strays)
            + sum(r.size_bytes for r in self.retention)
        )


# ---------------------------------------------------------------------------
# Sweep Implementations
# ---------------------------------------------------------------------------


def get_target_main_ref(root: Path) -> str | None:
    """Find the upstream main reference (origin/main, main, or HEAD)."""
    for ref in ("origin/main", "main", "HEAD"):
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return ref
    return None


def sweep_worktrees(
    root: Path,
    *,
    current_worktree: Path | None = None,
) -> list[WorktreeFinding]:
    """Sweep .worktrees/* for stale or dirty worktrees."""
    primary = shared_checkout_root(root)
    worktrees_dir = primary / ".worktrees"
    if not worktrees_dir.is_dir():
        # Also check relative to root if root is a fixture directory
        worktrees_dir = root / ".worktrees"
        if not worktrees_dir.is_dir():
            return []

    target_main = get_target_main_ref(primary) or "origin/main"
    active_wt = (current_worktree or root).resolve()

    findings: list[WorktreeFinding] = []
    candidates = sorted(worktrees_dir.iterdir(), key=lambda p: p.name)

    for wt_path in candidates:
        if not wt_path.is_dir():
            continue

        size = dir_size_bytes(wt_path)

        # 1. Is this the current invoking worktree?
        if wt_path.resolve() == active_wt:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch="current",
                    status="current",
                    file_count=0,
                    size_bytes=size,
                    reason="active worktree (current invocation)",
                    actionable=False,
                )
            )
            continue

        # 2. Check git status in the worktree
        status_res = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status_res.returncode == 0:
            dirty_lines = [line for line in status_res.stdout.splitlines() if line.strip()]
            is_dirty = len(dirty_lines) > 0
            dirty_count = len(dirty_lines)
        else:
            # Fallback for non-git or broken worktree
            is_dirty = True
            dirty_count = 1

        # 3. Determine branch
        branch_res = subprocess.run(
            ["git", "-C", str(wt_path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if branch_res.returncode == 0:
            branch = branch_res.stdout.strip()
        else:
            head_res = subprocess.run(
                ["git", "-C", str(wt_path), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            branch = (
                f"detached ({head_res.stdout.strip()})"
                if head_res.returncode == 0
                else "unknown"
            )

        # 4. Check staleness (merged into target or branch vanished)
        is_merged = False
        branch_exists = True

        if branch and not branch.startswith("detached") and branch != "unknown":
            # Check if branch exists
            check_branch = subprocess.run(
                ["git", "-C", str(primary), "show-ref", "--verify", f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                check=False,
            )
            branch_exists = check_branch.returncode == 0

            # Check if merged
            check_merged = subprocess.run(
                ["git", "-C", str(primary), "merge-base", "--is-ancestor", branch, target_main],
                capture_output=True,
                text=True,
                check=False,
            )
            is_merged = check_merged.returncode == 0
        else:
            # For detached or unknown, check commit ancestor if HEAD resolved
            head_res = subprocess.run(
                ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if head_res.returncode == 0:
                commit_sha = head_res.stdout.strip()
                check_merged = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(primary),
                        "merge-base",
                        "--is-ancestor",
                        commit_sha,
                        target_main,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                is_merged = check_merged.returncode == 0

        # Construct finding based on state
        if is_dirty:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=branch,
                    status="dirty",
                    file_count=dirty_count,
                    size_bytes=size,
                    reason=(
                        f"dirty — skipped ({dirty_count} uncommitted "
                        f"file{'s' if dirty_count != 1 else ''})"
                    ),
                    actionable=False,
                )
            )
        elif is_merged:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=branch,
                    status="clean_merged",
                    file_count=0,
                    size_bytes=size,
                    reason=f"branch merged into {target_main}",
                    actionable=True,
                )
            )
        elif not branch_exists:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=branch,
                    status="clean_vanished",
                    file_count=0,
                    size_bytes=size,
                    reason="branch no longer exists",
                    actionable=True,
                )
            )

    return sorted(findings, key=lambda f: f.path.as_posix())


def default_gh_pr_checker(branch: str, root: Path) -> tuple[bool, int | None, str | None]:
    """Check if branch has an open PR using `gh`.

    Returns (is_available, open_pr_number, error_message).
    """
    if shutil.which("gh") is None:
        return (False, None, "gh CLI is not installed / not on PATH")

    res = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return (False, None, res.stderr.strip() or "gh pr list failed")

    try:
        prs = json.loads(res.stdout)
        if isinstance(prs, list) and len(prs) > 0:
            first_pr = prs[0]
            if isinstance(first_pr, dict) and "number" in first_pr:
                return (True, int(first_pr["number"]), None)
        return (True, None, None)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return (False, None, f"failed to parse gh output: {exc}")


def sweep_branches(
    root: Path,
    *,
    gh_checker: Callable[[str, Path], tuple[bool, int | None, str | None]] | None = None,
) -> list[BranchFinding]:
    """Sweep local role/* branches fully contained in origin/main."""
    primary = shared_checkout_root(root)
    target_main = get_target_main_ref(primary) or "origin/main"

    # List local branches under refs/heads/role/
    res = subprocess.run(
        [
            "git",
            "-C",
            str(primary),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/role/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return []

    branch_names = [line.strip() for line in res.stdout.splitlines() if line.strip()]

    # Find which branches are currently checked out across all worktrees
    wt_res = subprocess.run(
        ["git", "-C", str(primary), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    active_branches: set[str] = set()
    if wt_res.returncode == 0:
        for line in wt_res.stdout.splitlines():
            if line.startswith("branch refs/heads/"):
                active_branches.add(line.removeprefix("branch refs/heads/").strip())

    checker = gh_checker or default_gh_pr_checker
    findings: list[BranchFinding] = []

    for branch in sorted(branch_names):
        # Check if merged into target
        merged_res = subprocess.run(
            [
                "git",
                "-C",
                str(primary),
                "merge-base",
                "--is-ancestor",
                branch,
                target_main,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if merged_res.returncode != 0:
            # Branch is not merged; skip
            continue

        # If checked out in a worktree:
        if branch in active_branches:
            findings.append(
                BranchFinding(
                    branch=branch,
                    status="active_worktree",
                    reason=f"merged into {target_main}; checked out in active worktree (preserved)",
                    actionable=False,
                )
            )
            continue

        # Check PR status via gh
        gh_available, open_pr, err_msg = checker(branch, primary)
        if not gh_available:
            findings.append(
                BranchFinding(
                    branch=branch,
                    status="gh_unavailable",
                    reason=(
                        f"merged into {target_main}; gh unavailable ({err_msg or 'skipped'}) "
                        "— preserved"
                    ),
                    actionable=False,
                )
            )
        elif open_pr is not None:
            findings.append(
                BranchFinding(
                    branch=branch,
                    status="open_pr",
                    pr_number=open_pr,
                    reason=f"merged into {target_main}; open PR #{open_pr} — preserved",
                    actionable=False,
                )
            )
        else:
            findings.append(
                BranchFinding(
                    branch=branch,
                    status="merged_no_pr",
                    reason=f"merged into {target_main} (no open PR)",
                    actionable=True,
                )
            )

    return sorted(findings, key=lambda f: f.branch)


def sweep_unindexed_docs(root: Path) -> list[DocFinding]:
    """Sweep docs/ for unindexed markdown files and front-matter issues."""
    docs_dir = root / "docs"
    index_path = root / "docs/INDEX.md"
    if not docs_dir.is_dir():
        return []

    issues = collect_check_issues(docs_dir, index_path, root=root)
    findings: list[DocFinding] = []

    for issue in issues:
        doc_path = root / issue.path
        size = doc_path.stat().st_size if doc_path.is_file() else 0
        findings.append(
            DocFinding(
                path=doc_path,
                size_bytes=size,
                issue=issue.message,
                reason=f"docindex issue: {issue.message}",
                actionable=False,
            )
        )

    return sorted(findings, key=lambda f: f.path.as_posix())


def sweep_untracked_strays(root: Path) -> list[StrayFinding]:
    """Sweep working tree for untracked files that are not gitignored."""
    res = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return []

    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    findings: list[StrayFinding] = []

    for rel in lines:
        if is_never_touch(rel):
            continue

        if rel.startswith(".worktrees/") or rel == ".worktrees":
            continue

        file_path = root / rel
        if not file_path.exists():
            continue

        size = dir_size_bytes(file_path)
        junk_reason = classify_junk(file_path)

        if junk_reason is not None:
            findings.append(
                StrayFinding(
                    path=file_path,
                    size_bytes=size,
                    is_junk=True,
                    reason=f"recognized junk: {junk_reason}",
                    actionable=True,
                )
            )
        else:
            findings.append(
                StrayFinding(
                    path=file_path,
                    size_bytes=size,
                    is_junk=False,
                    reason="unrecognized stray (preserved: possible work in progress)",
                    actionable=False,
                )
            )

    return sorted(findings, key=lambda f: f.path.as_posix())


def sweep_retention_violations(
    root: Path,
    *,
    now: datetime | None = None,
) -> list[RetentionFinding]:
    """Sweep for retention violations according to platform-architecture.md §2.6.

    Report only: evidence deletion is the job of evallab gc with tombstones.
    """
    clock = now if now is not None else datetime.now(UTC)
    clock_ts = clock.timestamp()

    findings: list[RetentionFinding] = []

    # 1. Z3 hot partitions older than 7 days (7 * 86400s)
    try:
        derived_root = derived_root_from_environment(root)
    except Exception:
        derived_root = root / "derived/parquet"

    if derived_root.is_dir():
        for parquet_file in derived_root.rglob("*.parquet"):
            if not parquet_file.is_file():
                continue
            try:
                mtime = parquet_file.stat().st_mtime
                age_days = (clock_ts - mtime) / 86400.0
                if age_days > 7.0:
                    findings.append(
                        RetentionFinding(
                            path=parquet_file,
                            category="z3_hot_partition",
                            age_days=age_days,
                            size_bytes=parquet_file.stat().st_size,
                            reason=(
                                f"Z3 hot partition older than 7d ({int(age_days)}d old; "
                                "hot retention is 7d, compaction required)"
                            ),
                            actionable=False,
                        )
                    )
            except OSError:
                pass

    # 2. Unpromoted Z1 jobs older than 14 days
    runs_dir = root / "runs"
    evidence_dir = root / "research/evidence"
    if runs_dir.is_dir():
        for job_dir in runs_dir.iterdir():
            if not job_dir.is_dir() or job_dir.name.startswith("."):
                continue

            # Check if promoted
            if evidence_dir.is_dir() and (evidence_dir / job_dir.name).exists():
                continue

            # Determine job age from result.json or dir mtime
            result_json = job_dir / "result.json"
            job_ts = None
            if result_json.is_file():
                try:
                    data = json.loads(result_json.read_text(encoding="utf-8"))
                    finished_at = data.get("finished_at") or data.get("started_at")
                    if finished_at:
                        job_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                        job_ts = job_dt.timestamp()
                except Exception:
                    pass

            if job_ts is None:
                try:
                    job_ts = job_dir.stat().st_mtime
                except OSError:
                    continue

            age_days = (clock_ts - job_ts) / 86400.0
            if age_days > 14.0:
                findings.append(
                    RetentionFinding(
                        path=job_dir,
                        category="z1_unpromoted_job",
                        age_days=age_days,
                        size_bytes=dir_size_bytes(job_dir),
                        reason=(
                            f"unpromoted Z1 job older than 14d ({int(age_days)}d old; "
                            "hot retention is 14d, compression/gc required)"
                        ),
                        actionable=False,
                    )
                )

    # 3. queue/events.jsonl beyond 30-day rolling window
    events_file = root / "queue/events.jsonl"
    if events_file.is_file():
        oldest_age_days = 0.0
        has_old_events = False
        try:
            with events_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        ts_str = record.get("timestamp") or record.get("at")
                        if ts_str:
                            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            age = (clock_ts - ts_dt.timestamp()) / 86400.0
                            if age > oldest_age_days:
                                oldest_age_days = age
                            if age > 30.0:
                                has_old_events = True
                    except Exception:
                        pass
        except OSError:
            pass

        if has_old_events:
            findings.append(
                RetentionFinding(
                    path=events_file,
                    category="events_log",
                    age_days=oldest_age_days,
                    size_bytes=events_file.stat().st_size,
                    reason=(
                        f"queue/events.jsonl contains entries older than 30d "
                        f"({int(oldest_age_days)}d old; 30d rolling retention window)"
                    ),
                    actionable=False,
                )
            )

    return sorted(findings, key=lambda f: f.path.as_posix())


# ---------------------------------------------------------------------------
# Tidy Engine & Formatting
# ---------------------------------------------------------------------------


def collect_tidy_report(
    root: Path,
    *,
    current_worktree: Path | None = None,
    gh_checker: Callable[[str, Path], tuple[bool, int | None, str | None]] | None = None,
    now: datetime | None = None,
    apply: bool = False,
) -> TidyReport:
    """Run all five sweeps and compile a unified TidyReport."""
    return TidyReport(
        worktrees=sweep_worktrees(root, current_worktree=current_worktree),
        branches=sweep_branches(root, gh_checker=gh_checker),
        docs=sweep_unindexed_docs(root),
        strays=sweep_untracked_strays(root),
        retention=sweep_retention_violations(root, now=now),
        apply=apply,
    )


def apply_deletions(report: TidyReport, root: Path) -> TidyReport:
    """Execute deletions for actionable items in report.

    Deletes only clean stale worktrees, merged local branches without open PR,
    and untracked strays with recognized junk signatures.
    """
    primary = shared_checkout_root(root)

    # 1. Actionable worktrees
    for wt in report.worktrees:
        if wt.actionable:
            rel = _rel_path_str(wt.path, primary)
            # Remove worktree safely
            res = subprocess.run(
                ["git", "-C", str(primary), "worktree", "remove", "--force", str(wt.path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0 and wt.path.exists():
                shutil.rmtree(wt.path, ignore_errors=True)
                subprocess.run(
                    ["git", "-C", str(primary), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            report.deleted_worktrees.append(rel)

    # 2. Actionable branches
    for branch in report.branches:
        if branch.actionable:
            res = subprocess.run(
                ["git", "-C", str(primary), "branch", "-D", branch.branch],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                report.deleted_branches.append(branch.branch)

    # 3. Actionable strays (recognized junk)
    for stray in report.strays:
        if stray.actionable and stray.path.exists():
            rel = _rel_path_str(stray.path, root)
            if stray.path.is_file() or stray.path.is_symlink():
                stray.path.unlink(missing_ok=True)
            elif stray.path.is_dir():
                shutil.rmtree(stray.path, ignore_errors=True)
            report.deleted_strays.append(rel)

    return report


def format_tidy_report(report: TidyReport, root: Path) -> str:
    """Format the tidy report into a deterministic, timestamp-free string."""
    primary = shared_checkout_root(root)
    lines: list[str] = []

    lines.append("# evallab tidy report")
    lines.append("")

    # 1. Stale worktrees
    wt_bytes = sum(w.size_bytes for w in report.worktrees)
    lines.append(f"## 1. Stale worktrees ({len(report.worktrees)} items, {format_bytes(wt_bytes)})")
    if not report.worktrees:
        lines.append("  (clean — no stale worktrees found)")
    else:
        for wt in report.worktrees:
            rel = _rel_path_str(wt.path, primary)
            action_tag = " [eligible for removal]" if wt.actionable else ""
            lines.append(
                f"- `{rel}` ({wt.branch}, {format_bytes(wt.size_bytes)}) — {wt.reason}{action_tag}"
            )
    lines.append("")

    # 2. Merged local branches
    lines.append(f"## 2. Merged local branches ({len(report.branches)} items)")
    if not report.branches:
        lines.append("  (clean — no merged local role/* branches found)")
    else:
        for b in report.branches:
            action_tag = " [eligible for deletion]" if b.actionable else ""
            lines.append(f"- `{b.branch}` — {b.reason}{action_tag}")
    lines.append("")

    # 3. Unindexed docs
    doc_bytes = sum(d.size_bytes for d in report.docs)
    lines.append(f"## 3. Unindexed docs ({len(report.docs)} items, {format_bytes(doc_bytes)})")
    if not report.docs:
        lines.append("  (clean — all documentation indexed and valid)")
    else:
        for d in report.docs:
            rel = _rel_path_str(d.path, root)
            lines.append(f"- `{rel}` ({format_bytes(d.size_bytes)}) — {d.reason}")
    lines.append("")

    # 4. Untracked strays
    stray_bytes = sum(s.size_bytes for s in report.strays)
    lines.append(
        f"## 4. Untracked strays ({len(report.strays)} items, {format_bytes(stray_bytes)})"
    )
    if not report.strays:
        lines.append("  (clean — no untracked stray files found)")
    else:
        for s in report.strays:
            rel = _rel_path_str(s.path, root)
            action_tag = " [safe to delete]" if s.actionable else ""
            lines.append(f"- `{rel}` ({format_bytes(s.size_bytes)}) — {s.reason}{action_tag}")
    lines.append("")

    # 5. Retention violations
    ret_bytes = sum(r.size_bytes for r in report.retention)
    lines.append(
        f"## 5. Retention violations ({len(report.retention)} items, {format_bytes(ret_bytes)})"
    )
    lines.append("  (report only — run evallab gc to manage evidence retention with tombstones)")
    if not report.retention:
        lines.append("  (clean — no retention violations found)")
    else:
        for r in report.retention:
            rel = _rel_path_str(r.path, root)
            lines.append(f"- `{rel}` ({format_bytes(r.size_bytes)}) — {r.reason}")
    lines.append("")

    # Summary & Action block
    lines.append("## Summary")
    lines.append(
        f"Total findings: {report.total_findings_count} items ({format_bytes(report.total_bytes)})"
    )
    lines.append(f"Actionable items: {report.actionable_count} items")
    lines.append("")

    if report.apply:
        lines.append("## Applied Actions")
        if report.deleted_worktrees:
            lines.append(f"- Removed {len(report.deleted_worktrees)} worktrees:")
            for wt in sorted(report.deleted_worktrees):
                lines.append(f"    - `{wt}`")
        if report.deleted_branches:
            lines.append(f"- Deleted {len(report.deleted_branches)} branches:")
            for br in sorted(report.deleted_branches):
                lines.append(f"    - `{br}`")
        if report.deleted_strays:
            lines.append(f"- Removed {len(report.deleted_strays)} stray junk files:")
            for st in sorted(report.deleted_strays):
                lines.append(f"    - `{st}`")
        if (
            not report.deleted_worktrees
            and not report.deleted_branches
            and not report.deleted_strays
        ):
            lines.append("- No actionable items were eligible for deletion.")
        lines.append("")
    else:
        lines.append(
            "Dry-run mode: no files or branches were modified. "
            "Pass --apply to execute safe cleanup."
        )
        lines.append("")

    return "\n".join(lines)


def run_tidy(
    root: Path,
    *,
    apply: bool = False,
    current_worktree: Path | None = None,
    gh_checker: Callable[[str, Path], tuple[bool, int | None, str | None]] | None = None,
    now: datetime | None = None,
) -> int:
    """Entry point for evallab tidy subcommand.

    Returns 0 on clean tree or successful apply, and 1 if findings exist under --dry-run.
    """
    report = collect_tidy_report(
        root,
        current_worktree=current_worktree,
        gh_checker=gh_checker,
        now=now,
        apply=apply,
    )

    if apply:
        report = apply_deletions(report, root)

    text = format_tidy_report(report, root)
    print(text)

    if not apply and report.total_findings_count > 0:
        return 1

    return 0
