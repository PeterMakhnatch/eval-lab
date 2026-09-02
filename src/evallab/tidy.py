"""E16: working tree tidy sweep reporting strays, stale worktrees, and retention violations.

Authority: docs/platform-architecture.md (T7, §2.6, §8).

Sweeps:
1. Stale worktrees: registered linked worktrees (authoritative inventory from
   `git worktree list --porcelain`; paths may live anywhere on disk) whose branch is
   merged, whose branch no longer exists, or whose registration Git reports prunable
   (skips dirty).
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
from evallab.storage.paths import derived_root_from_environment, shared_checkout_root

NEVER_TOUCH_PREFIXES: tuple[str, ...] = (
    "research/evidence",
    "policy",
    "docs/prompts",
    "agents/handoffs",
    "board",
    "agents/briefs",
)

RECOGNIZED_JUNK_EXTENSIONS: frozenset[str] = frozenset({
    ".tmp", ".temp", ".bak", ".backup", ".swp", ".swo", ".orig", ".rej",
    ".old", ".log", ".pyc", ".pyo", ".pyd",
})

RECOGNIZED_JUNK_FILENAMES: frozenset[str] = frozenset({
    ".DS_Store", "Thumbs.db", "dump.rdb", "core",
})

RECOGNIZED_JUNK_DIR_PARTS: frozenset[str] = frozenset({
    "__pycache__", ".pytest_cache", ".coverage", ".mypy_cache", ".ruff_cache",
})

RECOGNIZED_JUNK_PREFIXES: tuple[str, ...] = ("tmp_", "temp_", "scratch_", "test_output_")

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
    status: Literal[
        "clean_merged",
        "clean_vanished",
        "dirty",
        "current",
        "active_clean",
        "merged",
        "unmerged",
        "unproven",
        "prunable",
        "locked",
    ]
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


def check_branch_merged_status(
    primary: Path,
    branch: str,
    target_main: str,
) -> tuple[Literal["merged", "unmerged", "unproven"], str]:
    """Check if branch is merged into target_main via ancestry or content.

    Returns (state, reason) where state is:
    - "merged": provably in target_main (via ancestry or 3-way merge equivalence).
    - "unmerged": provably carrying content target_main lacks.
    - "unproven": cannot be established (git error, missing ref, detached HEAD).

    Predicate (hand-checkable):
      1. Fast path (ancestry):
         `git merge-base --is-ancestor <branch> <target_main>` == 0
         If true, the branch tip is reachable from target_main.
      2. Content path (3-way merge equivalence):
         `git merge-tree --write-tree <target_main> <branch>` ==
         `git rev-parse <target_main>^{tree}`
         If the tree resulting from 3-way merge of <branch> into <target_main>
         exactly matches <target_main>'s tree, merging <branch> introduces zero
         new changes. All content from <branch> is already present in
         <target_main>, even if squash-merged without graph ancestry.

    Why git merge-tree --write-tree:
      - Grounded in git plumbing: computes exact 3-way tree merge without
        touching index/worktree.
      - Safe under multi-commit squash merges: unlike `git cherry` or
        commit-level `patch-id`, `merge-tree` evaluates the net tree change
        of the entire branch.
      - Deletion safety: any unmerged commit or difference will produce a
        tree different from target_main's tree, or return exit code 1
        (merge conflict), refusing deletion.
      - Error safety: any git execution error or unrecognized ref classifies
        as "unproven".
    """
    # 1. Verify branch ref exists in local heads
    check_branch = subprocess.run(
        ["git", "-C", str(primary), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check_branch.returncode != 0:
        return ("unproven", f"branch '{branch}' does not exist in local refs")

    # 2. Fast path: graph ancestry
    check_ancestor = subprocess.run(
        ["git", "-C", str(primary), "merge-base", "--is-ancestor", branch, target_main],
        capture_output=True,
        text=True,
        check=False,
    )
    if check_ancestor.returncode == 0:
        return ("merged", f"branch merged into {target_main}")
    if check_ancestor.returncode not in (0, 1):
        err = check_ancestor.stderr.strip() or "exit non-zero"
        return ("unproven", f"git merge-base failed: {err}")

    # 3. Content path: 3-way merge tree equivalence
    target_tree_res = subprocess.run(
        ["git", "-C", str(primary), "rev-parse", f"{target_main}^{{tree}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if target_tree_res.returncode != 0 or not target_tree_res.stdout.strip():
        err = target_tree_res.stderr.strip() or "exit non-zero"
        return ("unproven", f"failed to resolve {target_main} tree: {err}")
    target_tree = target_tree_res.stdout.strip()

    merge_tree_res = subprocess.run(
        ["git", "-C", str(primary), "merge-tree", "--write-tree", target_main, branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_tree_res.returncode == 0:
        stdout_lines = merge_tree_res.stdout.splitlines()
        merged_tree = stdout_lines[0].strip() if stdout_lines else ""
        if merged_tree and merged_tree == target_tree:
            return ("merged", f"branch merged into {target_main} (content)")
        return ("unmerged", f"active branch {branch} (not merged into {target_main})")
    elif merge_tree_res.returncode == 1:
        # Exit code 1 means merge conflicts (unmerged changes conflicting with target_main)
        return ("unmerged", f"active branch {branch} (not merged into {target_main})")
    else:
        # Exit code > 1 means git tool error / corrupted repo state
        err = merge_tree_res.stderr.strip() or "exit non-zero"
        return ("unproven", f"git merge-tree failed: {err}")


@dataclass(frozen=True)
class WorktreeRegistration:
    """One registered worktree parsed from `git worktree list --porcelain`."""

    path: Path
    head: str | None
    branch: str | None
    bare: bool
    detached: bool
    locked_reason: str | None
    prunable_reason: str | None


def parse_worktree_porcelain(output: str, *, root: Path) -> list[WorktreeRegistration]:
    """Parse `git worktree list --porcelain` output into registrations.

    Fail-closed: a block without an explicit `worktree <path>` header is skipped
    entirely and paths are never inferred from names. `root` resolves relative
    paths only as a defensive fallback (Git emits absolute paths).
    """
    registrations: list[WorktreeRegistration] = []
    fields: dict[str, str] = {}

    def build(block: dict[str, str]) -> WorktreeRegistration | None:
        header = block.get("worktree")
        if not header:
            return None
        path = Path(header)
        if not path.is_absolute():
            path = (root / path).resolve()
        branch_raw = block.get("branch")
        branch = None
        if branch_raw and branch_raw.startswith("refs/heads/"):
            branch = branch_raw.removeprefix("refs/heads/").strip() or None
        return WorktreeRegistration(
            path=path,
            head=block.get("head") or None,
            branch=branch,
            bare="bare" in block,
            detached="detached" in block,
            locked_reason=block.get("locked"),
            prunable_reason=block.get("prunable"),
        )

    def flush() -> None:
        entry = build(fields)
        if entry is not None:
            registrations.append(entry)

    for raw_line in output.splitlines():
        if not raw_line.strip():
            flush()
            fields = {}
            continue
        key, _, value = raw_line.partition(" ")
        if key == "worktree":
            # A new header without a separating blank line: flush the previous block.
            flush()
            fields = {"worktree": value.strip()}
        else:
            fields[key] = value.strip()
    flush()
    return registrations


def sweep_worktrees(
    root: Path,
    *,
    current_worktree: Path | None = None,
    porcelain_output: str | None = None,
) -> list[WorktreeFinding]:
    """Sweep registered linked worktrees for stale or dirty entries.

    The inventory authority is Git: `git worktree list --porcelain`. Registered
    linked worktrees may live anywhere on disk (not only under .worktrees/), and
    worktree paths are never inferred from directory names. On Git failure the
    sweep fails closed with no candidates and no actions.

    Three-state classification:
    - merged: provably in target_main (via ancestry or git merge-tree) -> actionable if clean
    - unmerged: provably carrying content target_main lacks -> active, not actionable
    - unproven: cannot be established (detached HEAD, missing branch, git failure) -> not actionable

    Registration states (from the porcelain listing itself):
    - prunable: Git reports the registration stale -> actionable; apply removes it
      through `git worktree prune` and never touches directory contents.
    - locked: Git reports the registration locked -> never actionable.
    - bare and the primary checkout are not candidates; the current invoking worktree
      is excluded before any expensive work.

    Only clean_merged and Git-reported prunable registrations are actionable. Dirty,
    unmerged, detached, broken, locked, current, and unproven worktrees are NEVER
    actionable. Byte sizes are measured only for actionable entries whose path still
    exists; active entries are intentionally not walked.
    """
    primary = shared_checkout_root(root)
    if porcelain_output is None:
        listing = subprocess.run(
            ["git", "-C", str(primary), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if listing.returncode != 0:
            # Fail closed: no authoritative inventory, no candidates, no actions.
            return []
        porcelain_output = listing.stdout
    registrations = parse_worktree_porcelain(porcelain_output, root=primary)

    target_main = get_target_main_ref(primary) or "origin/main"
    active_wt = (current_worktree or root).resolve()
    primary_resolved = primary.resolve()

    def measured_size(path: Path) -> int:
        # The expensive recursive walk runs only for actionable entries that exist.
        if not path.is_dir():
            return 0
        return dir_size_bytes(path)

    findings: list[WorktreeFinding] = []

    for reg in registrations:
        wt_path = reg.path
        if reg.bare:
            continue
        if wt_path.resolve() == primary_resolved:
            # The primary checkout is never a stale-worktree candidate.
            continue
        if wt_path.resolve() == active_wt:
            # The current invoking worktree is excluded before any expensive work.
            continue

        if reg.locked_reason is not None:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=reg.branch or "unknown",
                    status="locked",
                    file_count=0,
                    size_bytes=0,
                    reason=(
                        f"locked — Git registration locked "
                        f"({reg.locked_reason or 'no reason given'}); never actionable"
                    ),
                    actionable=False,
                )
            )
            continue

        if reg.prunable_reason is not None:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=reg.branch or "unknown",
                    status="prunable",
                    file_count=0,
                    size_bytes=measured_size(wt_path),
                    reason=(
                        f"prunable — Git reports stale registration "
                        f"({reg.prunable_reason or 'unspecified'}); apply removes the "
                        f"registration via git worktree prune (directory contents "
                        f"never touched)"
                    ),
                    actionable=True,
                )
            )
            continue

        # Check git status in the worktree
        status_res = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status_res.returncode != 0:
            # Fallback for non-git or broken worktree: unproven and NEVER actionable
            err = status_res.stderr.strip() or "exit non-zero"
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch="unknown",
                    status="unproven",
                    file_count=0,
                    size_bytes=0,
                    reason=f"unproven — broken worktree (git status error: {err})",
                    actionable=False,
                )
            )
            continue

        dirty_lines = [line for line in status_res.stdout.splitlines() if line.strip()]
        is_dirty = len(dirty_lines) > 0
        dirty_count = len(dirty_lines)

        # Determine branch
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
                if head_res.returncode == 0 and head_res.stdout.strip()
                else "unknown"
            )

        # If dirty, skip immediately (never actionable, preserved)
        if is_dirty:
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=branch,
                    status="dirty",
                    file_count=dirty_count,
                    size_bytes=0,
                    reason=(
                        f"dirty — skipped ({dirty_count} uncommitted "
                        f"file{'s' if dirty_count != 1 else ''})"
                    ),
                    actionable=False,
                )
            )
            continue

        # For clean worktree: determine three-state merged / unmerged / unproven
        if not branch or branch == "unknown":
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch="unknown",
                    status="unproven",
                    file_count=0,
                    size_bytes=0,
                    reason="unproven — unknown branch / broken worktree",
                    actionable=False,
                )
            )
        elif branch.startswith("detached"):
            findings.append(
                WorktreeFinding(
                    path=wt_path,
                    branch=branch,
                    status="unproven",
                    file_count=0,
                    size_bytes=0,
                    reason=f"unproven — {branch} (cannot verify branch merge status)",
                    actionable=False,
                )
            )
        else:
            # Branch name is known and clean: check merged status in primary
            state, reason = check_branch_merged_status(primary, branch, target_main)
            if state == "merged":
                findings.append(
                    WorktreeFinding(
                        path=wt_path,
                        branch=branch,
                        status="clean_merged",
                        file_count=0,
                        size_bytes=measured_size(wt_path),
                        reason=reason,
                        actionable=True,
                    )
                )
            elif state == "unmerged":
                findings.append(
                    WorktreeFinding(
                        path=wt_path,
                        branch=branch,
                        status="active_clean",
                        file_count=0,
                        size_bytes=0,
                        reason=reason,
                        actionable=False,
                    )
                )
            else:  # state == "unproven"
                findings.append(
                    WorktreeFinding(
                        path=wt_path,
                        branch=branch,
                        status="unproven",
                        file_count=0,
                        size_bytes=0,
                        reason=f"unproven — {reason}",
                        actionable=False,
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
            "gh", "pr", "list", "--head", branch,
            "--state", "open", "--json", "number,url",
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
            "git", "-C", str(primary), "for-each-ref",
            "--format=%(refname:short)", "refs/heads/role/",
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
    worktree_branches: dict[str, Path] = {}
    if wt_res.returncode == 0:
        current_wt_path: Path | None = None
        for line in wt_res.stdout.splitlines():
            if line.startswith("worktree "):
                current_wt_path = Path(line.removeprefix("worktree ").strip())
            elif line.startswith("branch refs/heads/") and current_wt_path is not None:
                b_name = line.removeprefix("branch refs/heads/").strip()
                worktree_branches[b_name] = current_wt_path

    checker = gh_checker or default_gh_pr_checker
    findings: list[BranchFinding] = []

    for branch in sorted(branch_names):
        merged_res = subprocess.run(
            ["git", "-C", str(primary), "merge-base", "--is-ancestor", branch, target_main],
            capture_output=True,
            text=True,
            check=False,
        )
        if merged_res.returncode != 0:
            # Branch is not merged; skip
            continue

        # If checked out in a worktree:
        # Check if branch tip equals target_main (no commits of its own)
        b_res = subprocess.run(
            ["git", "-C", str(primary), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True, text=True, check=False,
        )
        t_res = subprocess.run(
            ["git", "-C", str(primary), "rev-parse", target_main],
            capture_output=True, text=True, check=False,
        )
        branch_sha = b_res.stdout.strip() if b_res.returncode == 0 else ""
        target_sha = t_res.stdout.strip() if t_res.returncode == 0 else ""
        no_commits_of_own = bool(branch_sha and branch_sha == target_sha)

        # If checked out in a worktree:
        if branch in worktree_branches:
            wt_path = worktree_branches[branch]
            wt_rel = _rel_path_str(wt_path, primary)
            wt_status = subprocess.run(
                ["git", "-C", str(wt_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            )
            wt_dirty = False
            if wt_status.returncode == 0:
                wt_dirty = len([line for line in wt_status.stdout.splitlines() if line.strip()]) > 0

            if wt_dirty:
                reason = (
                    f"contained in {target_main} (no commits of its own); "
                    f"checked out in active dirty worktree `{wt_rel}` (preserved)"
                    if no_commits_of_own
                    else f"checked out in active dirty worktree `{wt_rel}` (preserved)"
                )
            elif no_commits_of_own:
                reason = (
                    f"contained in {target_main} (no commits of its own); "
                    f"checked out in active worktree `{wt_rel}` (preserved)"
                )
            else:
                reason = (
                    f"merged into {target_main}; "
                    f"checked out in active worktree `{wt_rel}` (preserved)"
                )

            findings.append(
                BranchFinding(
                    branch=branch,
                    status="active_worktree",
                    reason=reason,
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

    Deletes only clean stale worktrees, Git-reported prunable registrations,
    merged local branches without open PR, and untracked strays with recognized
    junk signatures. Prunable registrations are removed exclusively through
    `git worktree prune`; their directory contents are never touched.
    """
    primary = shared_checkout_root(root)

    # 1. Actionable worktrees
    prunable_paths: list[str] = []
    for wt in report.worktrees:
        if not wt.actionable:
            continue
        if wt.status == "prunable":
            # Registration-only removal through Git's own prune operation. Never
            # rm/rmtree: a prunable marker must never escalate to deleting a
            # live directory.
            prunable_paths.append(_rel_path_str(wt.path, primary))
            continue
        rel = _rel_path_str(wt.path, primary)
        # Remove worktree safely
        res = subprocess.run(
            ["git", "-C", str(primary), "worktree", "remove", "--force", str(wt.path)],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0 and wt.path.exists():
            shutil.rmtree(wt.path, ignore_errors=True)
            subprocess.run(
                ["git", "-C", str(primary), "worktree", "prune"],
                capture_output=True, text=True, check=False,
            )
        report.deleted_worktrees.append(rel)

    if prunable_paths:
        prune_res = subprocess.run(
            ["git", "-C", str(primary), "worktree", "prune"],
            capture_output=True, text=True, check=False,
        )
        if prune_res.returncode == 0:
            report.deleted_worktrees.extend(prunable_paths)

    # 2. Actionable branches
    for branch in report.branches:
        if branch.actionable:
            res = subprocess.run(
                ["git", "-C", str(primary), "branch", "-D", branch.branch],
                capture_output=True, text=True, check=False,
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

    # 1. Stale worktrees (only actionable sweepable worktrees)
    stale_worktrees = [w for w in report.worktrees if w.actionable]
    active_worktrees = [w for w in report.worktrees if not w.actionable]

    wt_bytes = sum(w.size_bytes for w in stale_worktrees)
    lines.append(f"## 1. Stale worktrees ({len(stale_worktrees)} items, {format_bytes(wt_bytes)})")
    if not stale_worktrees:
        lines.append("  (clean — no stale worktrees found)")
    else:
        for wt in stale_worktrees:
            rel = _rel_path_str(wt.path, primary)
            action_tag = " [eligible for removal]" if wt.actionable else ""
            lines.append(
                f"- `{rel}` ({wt.branch}, {format_bytes(wt.size_bytes)}) — {wt.reason}{action_tag}"
            )
    lines.append("")

    # Active worktrees (not swept) — sizes intentionally not walked
    if active_worktrees:
        lines.append(
            f"## Active worktrees (not swept) "
            f"({len(active_worktrees)} items, sizes not walked)"
        )
        for wt in active_worktrees:
            rel = _rel_path_str(wt.path, primary)
            lines.append(f"- `{rel}` ({wt.branch}) — {wt.reason}")
        lines.append("")

    # 2. Merged local branches (excluding branches checked out in active worktrees)
    merged_branches = [b for b in report.branches if b.status != "active_worktree"]
    lines.append(f"## 2. Merged local branches ({len(merged_branches)} items)")
    if not merged_branches:
        lines.append("  (clean — no merged local role/* branches found)")
    else:
        for b in merged_branches:
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
        if not (
            report.deleted_worktrees or report.deleted_branches or report.deleted_strays
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

    if not apply and report.actionable_count > 0:
        return 1
    return 0
