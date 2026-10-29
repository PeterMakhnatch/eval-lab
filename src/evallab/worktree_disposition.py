"""Per-worktree disposition report for every registered eval-lab worktree.

Authority: ``docs/GENERATED-CACHE-POLICY.md`` (worktree retirement). That policy
requires a per-path disposition manifest before any worktree is retired; this
module produces it as Markdown plus JSON. It is strictly observational: it runs
only read-only Git plumbing (``--no-optional-locks``), reads no file contents, and
never changes a worktree, branch, PR, or registration.

Verdicts, one per registered worktree, decided by :func:`classify` from recorded
facts only (never from directory names, age, or branch-name reuse):

- ``delivered``: every commit of HEAD that is not an ancestor of the base ref is
  accounted for on the base ref, and nothing uncommitted is at risk. A commit is
  accounted for when it is reachable from the base ref, patch-equivalent to a base
  commit (``git cherry``), or reachable from the recorded head of a merged PR
  that targeted the base branch (squash merges leave no graph ancestry).
- ``unique-unmerged``: a branch-attached HEAD carries commits the base ref does
  not account for; the work is unique and at risk.
- ``blocked``: no safe disposition can be claimed: missing directory, prunable
  registration, Git could not report state, a detached HEAD with unaccounted
  commits (no branch ref protects them), or a delivered HEAD whose tree is dirty,
  has untracked files, or has a merge/rebase operation in progress.

Locked registrations, open PRs, and unique ignored evidence (``runs/``,
``derived/``, ``queue/``, recorded by presence only) never change a verdict; they
are retirement blockers listed per row. A report is evidence of state at its
generation time, not a standing approval to retire anything.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from evallab.tidy import WorktreeRegistration, parse_worktree_porcelain

SCHEMA = "evallab.worktree_disposition/v1"
REPOSITORY = "PeterMakhnatch/eval-lab"
PR_LIMIT = 5000
IGNORED_EVIDENCE_DIRS = ("runs", "derived", "queue")
OPERATION_MARKERS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "index.lock",
)
GIT_TIMEOUT_SECONDS = 300

Verdict = Literal["delivered", "unique-unmerged", "blocked"]
Proof = Literal["ancestry", "patch-equivalent", "merged-pr", "undelivered", "unproven"]
PrState = Literal["MERGED", "OPEN", "CLOSED"]

GitRunner = Callable[[Path, Sequence[str], str | None], "subprocess.CompletedProcess[str]"]
PrFetcher = Callable[[], str]


def run_git(
    cwd: Path, args: Sequence[str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one read-only Git command; a timeout is reported as exit status 124."""
    command = ["git", "--no-optional-locks", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "timed out")


def run_gh_pr_list() -> str:
    """Return one batched ``gh pr list`` JSON document for every PR state."""
    res = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPOSITORY,
            "--state",
            "all",
            "--limit",
            str(PR_LIMIT),
            "--json",
            "number,state,headRefName,headRefOid,baseRefName,mergedAt,mergeCommit",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if res.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {res.stderr.strip()[:200]}")
    return res.stdout


@dataclass(frozen=True)
class PrRecord:
    """One pull request reduced to the fields a disposition needs."""

    number: int
    state: PrState
    head_ref: str
    head_sha: str | None
    base_ref: str | None
    merged_at: str | None
    merge_commit: str | None


@dataclass(frozen=True)
class Facts:
    """Observed state of one registered worktree; the sole input to :func:`classify`."""

    path: Path
    primary: bool
    head: str | None
    branch: str | None
    detached: bool
    locked: str | None
    prunable: str | None
    exists: bool
    state_known: bool = False
    ahead: int | None = None
    behind: int | None = None
    undelivered: int | None = None
    proof: Proof = "unproven"
    proof_detail: str = ""
    tracked_changes: int = 0
    untracked: int = 0
    operations: tuple[str, ...] = ()
    head_date: str | None = None
    ignored_evidence: Mapping[str, int] | None = None
    symlinked_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Disposition:
    """One report row: facts, PR evidence, verdict, and retirement blockers."""

    facts: Facts
    pr: PrRecord | None
    pr_head_match: bool
    open_prs: tuple[int, ...]
    verdict: Verdict
    reason: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class DispositionReport:
    generated_at: str
    base_ref: str
    base_commit: str | None
    pr_state: Literal["available", "unavailable"]
    pr_count: int
    rows: tuple[Disposition, ...]

    @property
    def verdict_counts(self) -> dict[str, int]:
        counts = {"delivered": 0, "unique-unmerged": 0, "blocked": 0}
        for row in self.rows:
            counts[row.verdict] += 1
        return counts


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------


def classify(facts: Facts) -> tuple[Verdict, str]:
    """Map recorded facts to ``(verdict, reason)``; first matching rule wins."""
    if not facts.exists:
        return "blocked", "worktree directory missing"
    if facts.prunable is not None:
        return "blocked", f"prunable registration: {facts.prunable or 'stale'}"
    if not facts.state_known or facts.undelivered is None:
        return "blocked", facts.proof_detail or "git could not report worktree state"
    if facts.undelivered > 0:
        if facts.detached:
            return (
                "blocked",
                f"detached HEAD carries {facts.undelivered} unaccounted commit(s); "
                "no branch ref protects them",
            )
        return "unique-unmerged", facts.proof_detail
    risks = _uncommitted_risks(facts)
    if risks:
        return "blocked", "HEAD delivered but " + ", ".join(risks)
    return "delivered", facts.proof_detail


def _uncommitted_risks(facts: Facts) -> list[str]:
    risks: list[str] = []
    if facts.tracked_changes:
        risks.append(f"{facts.tracked_changes} uncommitted tracked change(s)")
    if facts.untracked:
        risks.append(f"{facts.untracked} untracked path(s)")
    if facts.operations:
        risks.append("operation in progress (" + ", ".join(facts.operations) + ")")
    return risks


def retirement_blockers(facts: Facts, verdict: Verdict, open_prs: Sequence[int]) -> list[str]:
    """List every GENERATED-CACHE-POLICY retirement condition this tree fails."""
    blockers: list[str] = []
    if facts.primary:
        blockers.append("primary checkout")
    if facts.locked is not None:
        blockers.append(f"locked registration ({facts.locked or 'no reason'})")
    if verdict != "delivered":
        blockers.append(f"verdict {verdict}")
    blockers.extend(_uncommitted_risks(facts))
    if open_prs:
        blockers.append("open PR " + ", ".join(f"#{number}" for number in open_prs))
    for name, count in sorted((facts.ignored_evidence or {}).items()):
        blockers.append(f"ignored evidence present: {name}/ ({count} entr{_y(count)})")
    return blockers


def _y(count: int) -> str:
    return "y" if count == 1 else "ies"


# ---------------------------------------------------------------------------
# PR records
# ---------------------------------------------------------------------------


def parse_pr_records(payload: str) -> list[PrRecord]:
    """Parse the ``gh pr list --json`` document; malformed rows are an error."""
    data = json.loads(payload or "[]")
    if not isinstance(data, list):
        raise ValueError("PR payload must be a JSON list")
    records: list[PrRecord] = []
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("PR payload rows must be objects")
        number, state, head_ref = row.get("number"), row.get("state"), row.get("headRefName")
        if not isinstance(number, int) or state not in ("MERGED", "OPEN", "CLOSED"):
            raise ValueError(f"unrecognized PR row: {row!r}")
        if not isinstance(head_ref, str):
            raise ValueError(f"PR #{number} has no headRefName")
        merge_commit = row.get("mergeCommit")
        records.append(
            PrRecord(
                number=number,
                state=state,
                head_ref=head_ref,
                head_sha=_opt_str(row.get("headRefOid")),
                base_ref=_opt_str(row.get("baseRefName")),
                merged_at=_opt_str(row.get("mergedAt")),
                merge_commit=_opt_str(merge_commit.get("oid"))
                if isinstance(merge_commit, dict)
                else None,
            )
        )
    return sorted(records, key=lambda record: record.number)


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def select_pr(records: Sequence[PrRecord], head: str | None) -> tuple[PrRecord | None, bool]:
    """Pick the PR describing this HEAD; return ``(record, head_matches)``.

    Only a PR whose recorded head commit equals HEAD counts as evidence about this
    commit; otherwise the newest PR for the branch name is shown as context.
    """
    exact = [record for record in records if head is not None and record.head_sha == head]
    if exact:
        merged = [record for record in exact if record.state == "MERGED"]
        return (merged or exact)[-1], True
    return (records[-1], False) if records else (None, False)


# ---------------------------------------------------------------------------
# Probing (read-only Git)
# ---------------------------------------------------------------------------


def _lines(res: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in res.stdout.splitlines() if line.strip()]


def present_commits(primary: Path, oids: Sequence[str], git: GitRunner) -> set[str]:
    """Return the subset of ``oids`` that exist as commits in the object database."""
    if not oids:
        return set()
    res = git(primary, ["cat-file", "--batch-check"], "".join(f"{oid}\n" for oid in oids))
    present: set[str] = set()
    for line in _lines(res):
        parts = line.split()
        if len(parts) == 3 and parts[1] == "commit":
            present.add(parts[0])
    return present


def account_commits(
    path: Path,
    head: str,
    base_ref: str,
    merged_pr_heads: Sequence[str],
    git: GitRunner,
) -> tuple[int, int, int, Proof, str] | None:
    """Return ``(ahead, behind, undelivered, proof, detail)`` or ``None`` on Git failure."""
    counts = git(path, ["rev-list", "--left-right", "--count", f"{base_ref}...{head}"], None)
    parts = counts.stdout.split()
    if counts.returncode != 0 or len(parts) != 2:
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    if ahead == 0:
        return 0, behind, 0, "ancestry", f"reachable from {base_ref}"

    unique = git(path, ["rev-list", head, f"^{base_ref}"], None)
    cherry = git(path, ["cherry", base_ref, head], None)
    if unique.returncode != 0 or cherry.returncode != 0:
        return None
    unique_commits = set(_lines(unique))
    novel = {
        line.split()[1]
        for line in _lines(cherry)
        if line.startswith("+ ") and len(line.split()) == 2
    }
    stdin = "".join(
        f"{ref}\n" for ref in [head, f"^{base_ref}", *(f"^{oid}" for oid in merged_pr_heads)]
    )
    outside_prs = git(path, ["rev-list", "--stdin"], stdin)
    if outside_prs.returncode != 0:
        return None
    remaining = novel & set(_lines(outside_prs)) & unique_commits
    undelivered = len(remaining)
    if undelivered:
        return (
            ahead,
            behind,
            undelivered,
            "undelivered",
            f"{undelivered} of {ahead} commit(s) ahead of {base_ref} are neither "
            "patch-equivalent on the base nor in a merged PR head",
        )
    covered_by_pr = len(unique_commits) - len(set(_lines(outside_prs)) & unique_commits)
    if covered_by_pr:
        return (
            ahead,
            behind,
            0,
            "merged-pr",
            f"{ahead} commit(s) ahead of {base_ref}, all patch-equivalent on the base "
            "or inside merged PR heads",
        )
    return (
        ahead,
        behind,
        0,
        "patch-equivalent",
        f"{ahead} commit(s) ahead of {base_ref}, all patch-equivalent on the base",
    )


def _git_dir_markers(path: Path, git: GitRunner) -> tuple[str, ...] | None:
    res = git(path, ["rev-parse", "--absolute-git-dir"], None)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    git_dir = Path(res.stdout.strip())
    return tuple(name for name in OPERATION_MARKERS if (git_dir / name).exists())


def _ignored_evidence(path: Path, git: GitRunner) -> tuple[dict[str, int], tuple[str, ...]] | None:
    """Count ignored top-level entries under evidence dirs; contents are never read."""
    res = git(
        path,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "--no-empty-directory",
            "--",
            *IGNORED_EVIDENCE_DIRS,
        ],
        None,
    )
    if res.returncode != 0:
        return None
    counts: dict[str, int] = {}
    symlinks: list[str] = []
    for line in _lines(res):
        entry = line.rstrip("/")
        top = entry.split("/", 1)[0]
        if os.path.islink(path / entry):
            symlinks.append(entry)
        elif top in IGNORED_EVIDENCE_DIRS:
            counts[top] = counts.get(top, 0) + 1
    return counts, tuple(sorted(symlinks))


def probe_worktree(
    registration: WorktreeRegistration,
    *,
    primary: bool,
    base_ref: str,
    merged_pr_heads: Sequence[str],
    git: GitRunner = run_git,
) -> Facts:
    """Probe one registered worktree through the filesystem and read-only Git."""
    path = registration.path
    exists = path.is_dir()
    base = Facts(
        path=path,
        primary=primary,
        head=registration.head,
        branch=registration.branch,
        detached=registration.detached,
        locked=registration.locked_reason,
        prunable=registration.prunable_reason,
        exists=exists,
        proof_detail="worktree directory missing" if not exists else "",
    )
    if not exists or registration.prunable_reason is not None:
        return base
    if registration.head is None:
        return replace(base, proof_detail="registration has no HEAD commit")

    accounted = account_commits(path, registration.head, base_ref, merged_pr_heads, git)
    status = git(path, ["status", "--porcelain=v1", "--untracked-files=normal"], None)
    markers = _git_dir_markers(path, git)
    ignored = _ignored_evidence(path, git)
    date = git(path, ["log", "-1", "--format=%cI", registration.head], None)
    if accounted is None or status.returncode != 0 or markers is None or ignored is None:
        return replace(base, proof_detail="git could not report worktree state")
    ahead, behind, undelivered, proof, detail = accounted
    status_lines = _lines(status)
    untracked = sum(1 for line in status_lines if line.startswith("??"))
    return replace(
        base,
        state_known=True,
        ahead=ahead,
        behind=behind,
        undelivered=undelivered,
        proof=proof,
        proof_detail=detail,
        tracked_changes=len(status_lines) - untracked,
        untracked=untracked,
        operations=markers,
        head_date=date.stdout.strip() or None if date.returncode == 0 else None,
        ignored_evidence=ignored[0],
        symlinked_evidence=ignored[1],
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def base_branch_name(base_ref: str) -> str:
    """``origin/main`` -> ``main``: the branch name merged PRs must have targeted."""
    return base_ref.split("/", 1)[1] if base_ref.startswith("origin/") else base_ref


def collect_report(
    primary: Path,
    *,
    base_ref: str = "origin/main",
    pr_records: Sequence[PrRecord] | None,
    now: datetime | None = None,
    git: GitRunner = run_git,
    max_workers: int = 8,
) -> DispositionReport:
    """Build the disposition report; ``pr_records=None`` means PR state is unavailable."""
    listing = git(primary, ["worktree", "list", "--porcelain"], None)
    if listing.returncode != 0:
        raise RuntimeError(f"git worktree list failed: {listing.stderr.strip()[:200]}")
    registrations = parse_worktree_porcelain(listing.stdout, root=primary)
    base = git(primary, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"], None)
    if base.returncode != 0:
        raise RuntimeError(f"base ref {base_ref} does not resolve: {base.stderr.strip()[:200]}")

    records = list(pr_records or ())
    target = base_branch_name(base_ref)
    merged_candidates = sorted(
        {
            record.head_sha
            for record in records
            if record.state == "MERGED" and record.base_ref == target and record.head_sha
        }
    )
    merged_heads = sorted(present_commits(primary, merged_candidates, git))

    def probe(item: tuple[int, WorktreeRegistration]) -> Facts:
        index, registration = item
        return probe_worktree(
            registration,
            primary=index == 0,
            base_ref=base_ref,
            merged_pr_heads=merged_heads,
            git=git,
        )

    items = list(enumerate(registrations))
    if items:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items)))) as pool:
            probed = list(pool.map(probe, items))
    else:
        probed = []

    by_branch: dict[str, list[PrRecord]] = {}
    by_head: dict[str, list[PrRecord]] = {}
    for record in records:
        by_branch.setdefault(record.head_ref, []).append(record)
        if record.head_sha:
            by_head.setdefault(record.head_sha, []).append(record)

    rows: list[Disposition] = []
    for facts in probed:
        branch_records = by_branch.get(facts.branch or "", [])
        candidates = branch_records or by_head.get(facts.head or "", [])
        pr, matches = select_pr(candidates, facts.head)
        open_prs = tuple(r.number for r in branch_records if r.state == "OPEN")
        verdict, reason = classify(facts)
        rows.append(
            Disposition(
                facts=facts,
                pr=pr,
                pr_head_match=matches,
                open_prs=open_prs,
                verdict=verdict,
                reason=reason,
                blockers=tuple(retirement_blockers(facts, verdict, open_prs)),
            )
        )
    rows.sort(key=lambda row: str(row.facts.path))
    generated = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    return DispositionReport(
        generated_at=generated,
        base_ref=base_ref,
        base_commit=base.stdout.strip(),
        pr_state="unavailable" if pr_records is None else "available",
        pr_count=len(records),
        rows=tuple(rows),
    )


def fetch_pr_records(fetch: PrFetcher = run_gh_pr_list) -> list[PrRecord] | None:
    """Return every PR, or ``None`` when GitHub state is unavailable or truncated."""
    try:
        records = parse_pr_records(fetch())
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        return None
    return None if len(records) >= PR_LIMIT else records


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def to_json(report: DispositionReport) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for row in report.rows:
        facts = row.facts
        rows.append(
            {
                "path": str(facts.path),
                "role": "primary" if facts.primary else "linked",
                "branch": facts.branch,
                "head": facts.head,
                "head_date": facts.head_date,
                "detached": facts.detached,
                "locked": facts.locked,
                "prunable": facts.prunable,
                "exists": facts.exists,
                "ahead": facts.ahead,
                "behind": facts.behind,
                "undelivered_commits": facts.undelivered,
                "proof": facts.proof,
                "tracked_changes": facts.tracked_changes,
                "untracked_paths": facts.untracked,
                "operations_in_progress": list(facts.operations),
                "ignored_evidence": dict(sorted((facts.ignored_evidence or {}).items())),
                "symlinked_evidence": list(facts.symlinked_evidence),
                "pr": None
                if row.pr is None
                else {
                    "number": row.pr.number,
                    "state": row.pr.state,
                    "head_sha": row.pr.head_sha,
                    "merged_at": row.pr.merged_at,
                    "merge_commit": row.pr.merge_commit,
                    "head_matches_worktree": row.pr_head_match,
                },
                "open_prs": list(row.open_prs),
                "verdict": row.verdict,
                "reason": row.reason,
                "retirement_blockers": list(row.blockers),
                "retirement_ready": not row.blockers,
            }
        )
    return {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "generated_at": report.generated_at,
        "base_ref": report.base_ref,
        "base_commit": report.base_commit,
        "pr_state": report.pr_state,
        "pr_count": report.pr_count,
        "worktree_count": len(report.rows),
        "verdict_counts": report.verdict_counts,
        "worktrees": rows,
    }


def render_json(report: DispositionReport) -> str:
    return json.dumps(to_json(report), indent=2, sort_keys=True) + "\n"


def display_path(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ") or "-"


def _pr_cell(row: Disposition) -> str:
    if row.pr is None:
        return "none"
    suffix = "" if row.pr_head_match else " (other head)"
    return f"#{row.pr.number} {row.pr.state.lower()}{suffix}"


def _row(row: Disposition, home: Path, *, detail: str) -> str:
    facts = row.facts
    branch = facts.branch or ("detached" if facts.detached else "-")
    ahead = "-" if facts.ahead is None else f"{facts.ahead}/{facts.behind}"
    return (
        f"| `{_cell(display_path(facts.path, home))}` | `{_cell(branch)}` "
        f"| `{(facts.head or '-')[:10]}` | {ahead} | {_pr_cell(row)} | {_cell(detail)} |"
    )


_HEADER = (
    "| Worktree | Branch | HEAD | Ahead/Behind | PR | {detail} |",
    "|---|---|---|---|---|---|",
)


def render_markdown(report: DispositionReport, *, home: Path) -> str:
    counts = report.verdict_counts
    lines = [
        "# Worktree disposition report",
        "",
        "Produced by `evallab worktrees` (`src/evallab/worktree_disposition.py`) from",
        "`git worktree list --porcelain`, read-only Git plumbing, and one batched",
        "`gh pr list`. Observational only: nothing was deleted, moved, pruned, or",
        "fetched inside any worktree, and the report is not approval to retire any tree.",
        "",
        f"- Generated: {report.generated_at}",
        f"- Base: `{report.base_ref}` at `{report.base_commit or 'unknown'}`",
        f"- PR state: {report.pr_state} ({report.pr_count} PRs read)",
        f"- Registered worktrees: {len(report.rows)}",
        f"- delivered: {counts['delivered']} | unique-unmerged: {counts['unique-unmerged']}"
        f" | blocked: {counts['blocked']}",
        f"- Retirement-ready (delivered and no blocker): "
        f"{sum(1 for row in report.rows if not row.blockers)}",
        "",
        "## Verdict rules (first match wins)",
        "",
        "1. Directory missing, prunable registration, or Git cannot report state: **blocked**.",
        "2. Commits not accounted for on the base and HEAD detached: **blocked**.",
        "3. Commits not accounted for on the base and HEAD on a branch: **unique-unmerged**.",
        "4. All commits accounted for but uncommitted tracked changes, untracked paths, or a",
        "   merge/rebase/cherry-pick/bisect operation: **blocked**.",
        "5. Otherwise: **delivered**.",
        "",
        "A commit is accounted for when it is reachable from the base, patch-equivalent to a",
        "base commit (`git cherry`), or reachable from the recorded head of a merged PR into",
        "the base branch. Locked registrations, open PRs, and ignored `runs/`, `derived/`,",
        "`queue/` entries (presence only) are listed as retirement blockers and never change",
        "a verdict.",
    ]
    sections = (
        (
            "unique-unmerged",
            "Unique-unmerged (unique work at risk)",
            "Reason / retirement blockers",
        ),
        ("blocked", "Blocked (no safe disposition)", "Reason / retirement blockers"),
        ("delivered", "Delivered", "Retirement blockers"),
    )
    for verdict, title, detail_title in sections:
        rows = [row for row in report.rows if row.verdict == verdict]
        lines.extend(["", f"## {title}: {len(rows)}", ""])
        if not rows:
            lines.append("None.")
            continue
        lines.append(_HEADER[0].format(detail=detail_title))
        lines.append(_HEADER[1])
        for row in rows:
            blockers = "; ".join(row.blockers) or "none"
            detail = blockers if verdict == "delivered" else f"{row.reason}. {blockers}"
            lines.append(_row(row, home, detail=detail))
    lines.append("")
    return "\n".join(lines)
