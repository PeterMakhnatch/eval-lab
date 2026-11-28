from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ._multiagent_io import epoch_to_iso_utc, sanitize_utf8_text

_HARNESS_SEED_AUTHORS = frozenset({"harness", "multi-agent-harness", "seed", "root", "harbor"})
_TRUNK_BRANCHES = frozenset({"main", "master"})
_MAX_BRANCHES = 500
_MAX_COMMITS = 20_000
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_VALID_BUNDLE_HEADERS = (b"# v2 git bundle\n", b"# v3 git bundle\n")
_GIT_TIMEOUT_SEC = 30
_SHA_PREFIX = "__EVALLAB_SHA__"


class GitCommitRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha: str
    author_name: str
    author_email: str
    resolved_actor: str | None = None
    ts: str | None = None
    ts_epoch: float | None = None
    subject: str = ""
    files: list[str] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=list)
    is_root_commit: bool = False
    author_email_mismatch: bool = False


class GitBundleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commits: list[GitCommitRecord] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=list)
    default_branch: str = "main"
    shared_branches: dict[str, list[str]] = Field(default_factory=dict)
    shared_files: dict[str, list[str]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


def _clean_git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SEC,
        check=True,
        env=_clean_git_env(),
    ).stdout


def _resolve_git_actor(
    author_name: str,
    author_email: str,
    roster: set[str],
    alias_map: dict[str, str] | None = None,
) -> tuple[str | None, bool]:
    exact: dict[str, str] = {name: name for name in sorted(roster)}
    by_lower = {name.lower(): name for name in sorted(roster)}
    if alias_map:
        for alias, canonical in sorted(alias_map.items()):
            if canonical in roster or not roster:
                exact.setdefault(alias, canonical)
                by_lower.setdefault(alias.lower(), canonical)
    name_hit = exact.get(author_name) or by_lower.get(author_name.lower())
    email_local_raw = author_email.split("@", 1)[0].strip()
    email_hit = exact.get(email_local_raw) or by_lower.get(email_local_raw.lower())

    if name_hit is not None and email_hit is not None and name_hit != email_hit:
        return name_hit, True
    if name_hit is not None:
        return name_hit, False
    if email_hit is not None:
        mismatch = (
            bool(author_name.strip()) and author_name.strip().lower() not in _HARNESS_SEED_AUTHORS
        )
        return email_hit, mismatch
    return None, False


def is_seed_author(author_name: str, author_email: str) -> bool:
    low_name = author_name.strip().lower()
    low_local = author_email.split("@", 1)[0].strip().lower()
    if not low_name and not low_local:
        return False
    return (not low_name or low_name in _HARNESS_SEED_AUTHORS) and (
        not low_local or low_local in _HARNESS_SEED_AUTHORS
    )


def _list_branches(repo: Path) -> tuple[list[str], str]:
    raw_branches = [
        b
        for b in _run_git(repo, "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads").split(
            "\n"
        )
        if b.strip()
    ]
    unique = sorted(set(raw_branches))
    try:
        head_ref = _run_git(repo, "symbolic-ref", "HEAD").strip()
        head_branch = (
            head_ref.removeprefix("refs/heads/") if head_ref.startswith("refs/heads/") else "main"
        ) or "main"
    except subprocess.CalledProcessError:
        head_branch = "main"
    priority = [b for b in ("main", "master", head_branch) if b in unique]
    priority_set = set(priority)
    rest = [b for b in unique if b not in priority_set]
    branches = sorted(set((*priority, *rest[: max(0, _MAX_BRANCHES - len(priority))])))
    if "main" in branches:
        default_branch = "main"
    elif "master" in branches:
        default_branch = "master"
    else:
        default_branch = head_branch
    return branches, default_branch


def _parse_commit_files(raw_status: str) -> dict[str, list[str]]:
    tokens = raw_status.split("\x00")
    files_by_sha: dict[str, set[str]] = defaultdict(set)
    cur_sha: str | None = None
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if not tok:
            idx += 1
            continue
        if tok.startswith(_SHA_PREFIX):
            cur_sha = tok[len(_SHA_PREFIX) :].strip()
            idx += 1
            continue
        status = tok.lstrip("\n")
        if cur_sha is None or not status:
            idx += 1
            continue
        if status[0] in ("R", "C") and idx + 2 < n:
            if tokens[idx + 1]:
                files_by_sha[cur_sha].add(sanitize_utf8_text(tokens[idx + 1]))
            if tokens[idx + 2]:
                files_by_sha[cur_sha].add(sanitize_utf8_text(tokens[idx + 2]))
            idx += 3
        elif idx + 1 < n:
            if tokens[idx + 1]:
                files_by_sha[cur_sha].add(sanitize_utf8_text(tokens[idx + 1]))
            idx += 2
        else:
            idx += 1
    return {sha: sorted(paths) for sha, paths in files_by_sha.items()}


def _parse_commits(
    repo: Path,
    sha_branches: dict[str, set[str]],
    roster_set: set[str],
    member_aliases: dict[str, str] | None,
) -> list[GitCommitRecord]:
    meta_out = _run_git(
        repo,
        "log",
        "--all",
        "--no-merges",
        "--reverse",
        f"-n{_MAX_COMMITS}",
        "-z",
        "--format=%H%x00%P%x00%an%x00%ae%x00%at%x00%s",
    )
    status_out = _run_git(
        repo,
        "log",
        "--all",
        "--no-merges",
        "--reverse",
        f"-n{_MAX_COMMITS}",
        "-z",
        f"--format=tformat:{_SHA_PREFIX}%H",
        "--name-status",
    )
    files_by_sha = _parse_commit_files(status_out)
    fields = meta_out.split("\x00")
    commits: list[GitCommitRecord] = []
    for pos in range(0, len(fields) - 5, 6):
        sha = fields[pos].strip()
        if not sha:
            continue
        parents = fields[pos + 1]
        an = sanitize_utf8_text(fields[pos + 2]).strip()
        ae = sanitize_utf8_text(fields[pos + 3]).strip()
        at_raw = fields[pos + 4].strip()
        subject = sanitize_utf8_text(fields[pos + 5]).strip()
        try:
            at_float: float | None = float(at_raw)
        except ValueError:
            at_float = None
        ts_iso, ts_epoch = epoch_to_iso_utc(at_float)
        resolved, mismatch = _resolve_git_actor(an, ae, roster_set, member_aliases)
        commits.append(
            GitCommitRecord(
                sha=sha,
                author_name=an,
                author_email=ae,
                resolved_actor=resolved,
                ts=ts_iso,
                ts_epoch=ts_epoch,
                subject=subject,
                files=files_by_sha.get(sha, []),
                branches=sorted(sha_branches.get(sha, set())),
                is_root_commit=not parents.strip(),
                author_email_mismatch=mismatch,
            )
        )
    return commits


def _collect_shared_branches(
    commits: list[GitCommitRecord],
    branches: list[str],
    default_branch: str,
) -> dict[str, list[str]]:
    trunk_refs = ({default_branch} | set(_TRUNK_BRANCHES)) & set(branches)
    authors_by_branch: dict[str, set[str]] = defaultdict(set)
    for commit in commits:
        if commit.is_root_commit or commit.resolved_actor is None:
            continue
        for branch in commit.branches:
            if branch not in trunk_refs and branch not in _TRUNK_BRANCHES:
                authors_by_branch[branch].add(commit.resolved_actor)
    return {
        branch: sorted(authors)
        for branch, authors in sorted(authors_by_branch.items())
        if len(authors) > 1
    }


def _validate_git_bundle_file(bundle_path: Path) -> str | None:
    try:
        st = bundle_path.lstat()
    except (OSError, ValueError):
        return f"bundle_missing:{bundle_path.name}"
    if not stat.S_ISREG(st.st_mode):
        return f"bundle_not_regular:{bundle_path.name}"
    if st.st_size > _MAX_BUNDLE_BYTES:
        return f"bundle_too_large:{bundle_path.name}"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(bundle_path, flags)
    except OSError:
        return f"bundle_unreadable:{bundle_path.name}"
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return f"bundle_not_regular:{bundle_path.name}"
        if fst.st_size > _MAX_BUNDLE_BYTES:
            return f"bundle_too_large:{bundle_path.name}"
        head = os.read(fd, 32)
    except OSError:
        return f"bundle_unreadable:{bundle_path.name}"
    finally:
        os.close(fd)
    if not any(head.startswith(prefix) for prefix in _VALID_BUNDLE_HEADERS):
        return f"bundle_invalid_header:{bundle_path.name}"
    return None


def _clone_bundle_mirror(bundle_path: Path, repo: Path) -> str | None:
    try:
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.file.allow=always",
                "clone",
                "-q",
                "--mirror",
                "--",
                str(bundle_path.resolve()),
                str(repo),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SEC,
            check=True,
            env=_clean_git_env(),
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        OSError,
    ) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return f"git_bundle_clone_failed:{detail.strip()[:200]}"
    return None


def _rev_list_shas(repo: Path, *args: str) -> list[str]:
    out = _run_git(repo, "rev-list", "--no-merges", f"-n{_MAX_COMMITS}", *args)
    return [s for line in out.splitlines() if (s := line.strip())]


def _collect_sha_branches(
    repo: Path, branches: list[str], trunk_refs: set[str]
) -> dict[str, set[str]]:
    trunk_first_parent: set[str] = set()
    for tref in sorted(trunk_refs):
        trunk_first_parent.update(
            _rev_list_shas(repo, "--first-parent", "--end-of-options", f"refs/heads/{tref}", "--")
        )
    sha_branches: dict[str, set[str]] = defaultdict(set)
    for branch in branches:
        if branch in trunk_refs:
            for sha in _rev_list_shas(repo, "--end-of-options", f"refs/heads/{branch}", "--"):
                sha_branches[sha].add(branch)
            continue
        excludes = [f"^refs/heads/{ref}" for ref in sorted(trunk_refs) if ref != branch]
        unique = _rev_list_shas(repo, "--end-of-options", f"refs/heads/{branch}", *excludes, "--")
        if unique:
            for sha in unique:
                sha_branches[sha].add(branch)
        else:
            for sha in _rev_list_shas(
                repo, "--first-parent", "--end-of-options", f"refs/heads/{branch}", "--"
            ):
                if sha not in trunk_first_parent:
                    sha_branches[sha].add(branch)
    return sha_branches


def _collect_shared_files(commits: list[GitCommitRecord]) -> dict[str, list[str]]:
    file_owners: dict[str, set[str]] = defaultdict(set)
    for commit in commits:
        if commit.is_root_commit or commit.resolved_actor is None:
            continue
        for file_path in commit.files:
            file_owners[file_path].add(commit.resolved_actor)
    return {path: sorted(owners) for path, owners in sorted(file_owners.items()) if len(owners) > 1}


def read_git_bundle(
    bundle_path: Path,
    roster: list[str],
    *,
    member_aliases: dict[str, str] | None = None,
) -> GitBundleSummary:
    roster_set = set(roster)
    if (err := _validate_git_bundle_file(bundle_path)) is not None:
        return GitBundleSummary(errors=[err])

    with tempfile.TemporaryDirectory(prefix="evallab-git-bundle-") as tmp:
        repo = Path(tmp) / "team.git"
        if (clone_err := _clone_bundle_mirror(bundle_path, repo)) is not None:
            return GitBundleSummary(errors=[clone_err])
        try:
            branches, default_branch = _list_branches(repo)
            trunk_refs = ({default_branch} | set(_TRUNK_BRANCHES)) & set(branches)
            sha_branches = _collect_sha_branches(repo, branches, trunk_refs)
            commits = _parse_commits(repo, sha_branches, roster_set, member_aliases)
            shared_branches = _collect_shared_branches(commits, branches, default_branch)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            return GitBundleSummary(errors=[f"git_bundle_inspect_failed:{detail.strip()[:200]}"])

    return GitBundleSummary(
        commits=commits,
        branches=branches,
        default_branch=default_branch,
        shared_branches=shared_branches,
        shared_files=_collect_shared_files(commits),
    )
