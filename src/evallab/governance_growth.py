"""PR-local growth limits and total-size budgets, using stdlib and local Git only.

Totals measure the tested candidate tree; contributions measure the exact PR head
against its merge base. With non-strict branch protection, concurrent PRs can each
fit alone yet exceed a budget together. A PR fails only when its tested total is
over budget AND its own net contribution to that metric is positive. Shrinking
and untouched metrics always pass. Push events report all budget problems without
failing, so a race cannot make main red. Budget raises stay visible in the same
PR; deletions never require a budget reduction.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

LEDGER = "agents/growth-ledger.jsonl"
ROOT_LIMITS = {"src/evallab": 1000, "tests": 1000, "research": 2000, "library": 1000}
NEW_FILE_LIMITS = {"src/evallab": 600, "tests": 600, "research": 1000, "library": 1000}
LARGE_FILE_LINES = 8000
PYTHON_PATHS = tuple(f"{name}/*.py" for name in ROOT_LIMITS)
BUDGETS = "agents/budgets.toml"
BUDGET_METRICS = {
    **{f"python-lines:{name}": (name, "Python lines") for name in ROOT_LIMITS},
    "markdown-files:docs": ("docs", "Markdown files"),
    "markdown-files:agents": ("agents", "Markdown files"),
    "experiment-directories:research/experiments": ("research/experiments", "experiment directories"),
    "large-python-files:src/evallab": ("src/evallab", "Python files above 800 lines"),
    "large-python-files:tests": ("tests", "Python files above 800 lines"),
}


def _budget_line(key: str, limit: int) -> str:
    return (
        f'{json.dumps(key)} = {{ limit = {limit}, reason = "State the concrete need", '
        'raised_by = "Your name or PR" }'
    )


def _budget_entries(
    current: bytes, previous: bytes,
) -> tuple[dict[str, int], list[str]]:
    """Validate every cap and require attribution for initial caps and raises."""
    try:
        rows = tomllib.loads(current.decode())["budgets"]
        old = tomllib.loads(previous.decode()).get("budgets", {}) if previous else {}
        if not isinstance(rows, dict) or not isinstance(old, dict):
            raise ValueError("budgets must be a table")
    except (ValueError, KeyError) as exc:
        return {}, [f"{BUDGETS}: invalid or missing [budgets] table: {exc}"]
    limits: dict[str, int] = {}
    issues: list[str] = []
    for key in BUDGET_METRICS:
        row = rows.get(key)
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("limit"), int)
            or isinstance(row["limit"], bool)
            or row["limit"] < 0
        ):
            issues.append(f"{BUDGETS}: {key} needs a nonnegative integer limit; add {_budget_line(key, 0)}")
            continue
        limits[key] = row["limit"]
        prior = old.get(key, {})
        prior_limit = prior.get("limit", -1) if isinstance(prior, dict) else -1
        if not isinstance(prior_limit, int) or isinstance(prior_limit, bool):
            prior_limit = -1
        prior_reason = prior.get("reason") if isinstance(prior, dict) else None
        if row["limit"] > prior_limit and (
            any(
                not isinstance(row.get(field), str) or not row[field].strip()
                or row[field].strip() in {"State the concrete need", "Your name or PR"}
                for field in ("reason", "raised_by")
            )
            or (isinstance(prior_reason, str) and row["reason"].strip() == prior_reason.strip())
        ):
            issues.append(
                f"{BUDGETS}: raise for {key} from {prior_limit} to {row['limit']} "
                "requires a concrete reason and raised_by; use a new reason for this raise; replace its line with "
                f"{_budget_line(key, row['limit'])} (replace both placeholders)"
            )
    return limits, issues


def _inventory(root: Path, revision: str | None = None) -> set[str]:
    paths = (*PYTHON_PATHS, "docs/*.md", "agents/*.md", "research/experiments")
    output = (
        _git(root, "ls-tree", "-r", "--name-only", "-z", revision,
             "--", "docs", "agents", "research/experiments")
        if revision else _git(root, "ls-files", "-z", "--", *paths)
    )
    return set(output.decode().split("\0")) - {""}


def _path_counts(paths: set[str]) -> dict[str, int]:
    return {
        "markdown-files:docs": sum(path.startswith("docs/") and path.endswith(".md") for path in paths),
        "markdown-files:agents": sum(path.startswith("agents/") and path.endswith(".md") for path in paths),
        "experiment-directories:research/experiments": len({
            path.split("/")[2] for path in paths
            if path.startswith("research/experiments/") and len(path.split("/")) > 3
        }),
    }


def _budget_issues(
    root: Path, ancestor: str | None, candidate: str | None,
    before: dict[str, bytes], after: dict[str, bytes],
) -> list[str]:
    policy = root / BUDGETS
    previous_blobs = _blobs(root, ancestor, [BUDGETS])
    previous = previous_blobs.get(BUDGETS, b"")
    if not policy.is_file():
        required = BUDGETS in previous_blobs or bool(_git(root, "ls-files", "--", BUDGETS))
        if not required and ancestor:
            required = BUDGETS in _blobs(root, "HEAD", [BUDGETS])
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not required and event_path and os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            required = BUDGETS in _blobs(root, event["pull_request"]["base"]["sha"], [BUDGETS])
        # Repositories that predate this policy still support the PR-local checker.
        if not required:
            return []
        return [f"{BUDGETS}: budget file removed; restore all total-size budgets"]
    limits, issues = _budget_entries(policy.read_bytes(), previous)
    if issues:
        return issues
    paths = {path for path in _inventory(root) if (root / path).is_file()}
    totals = dict.fromkeys(BUDGET_METRICS, 0)
    totals.update(_path_counts(paths))
    for path in paths:
        if not path.endswith(".py"):
            continue
        name = next((name for name in ROOT_LIMITS if path.startswith(f"{name}/")), None)
        if name is None:
            continue
        lines = _lines((root / path).read_bytes())
        totals[f"python-lines:{name}"] += lines
        if name in {"src/evallab", "tests"} and lines > 800:
            totals[f"large-python-files:{name}"] += 1
    old_paths = _inventory(root, ancestor) if ancestor else set()
    pr_paths = _inventory(root, candidate) if candidate else paths
    old_counts, pr_counts = _path_counts(old_paths), _path_counts(pr_paths)
    contribution = dict.fromkeys(BUDGET_METRICS, 0)
    contribution.update({key: pr_counts[key] - old_counts[key] for key in old_counts})
    for path in before.keys() | after.keys():
        if not path.endswith(".py"):
            continue
        name = next((name for name in ROOT_LIMITS if path.startswith(f"{name}/")), None)
        if name is None:
            continue
        old, new = _lines(before.get(path, b"")), _lines(after.get(path, b""))
        contribution[f"python-lines:{name}"] += new - old
        if name in {"src/evallab", "tests"}:
            contribution[f"large-python-files:{name}"] += int(new > 800) - int(old > 800)
    for key, total in totals.items():
        limit = limits[key]
        if total <= limit:
            continue
        name, unit = BUDGET_METRICS[key]
        message = (
            f"total-budget: {name} has {total} {unit}; budget {limit}; "
            f"this PR contributes {contribution[key]:+d}; remove {total - limit} {unit}, "
            f"or replace its line in {BUDGETS}: {_budget_line(key, total)} "
            "(replace both placeholders)"
        )
        if os.environ.get("GITHUB_EVENT_NAME") == "push" or contribution[key] <= 0:
            print(f"governance budget report-only: {message}")
        else:
            issues.append(message)
    return issues


def _git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, input=input, check=True, capture_output=True
    ).stdout


def _scope(root: Path, base: str | None) -> tuple[str | None, str | None]:
    """Return baseline commit and optional exact candidate commit (otherwise worktree)."""
    if base is not None:
        return _git(root, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip(), None
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_path and event_name in {"pull_request", "push"}:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        if event_name == "pull_request":
            pr = event["pull_request"]
            head = pr["head"]["sha"]
            ancestor = _git(root, "merge-base", pr["base"]["sha"], head).decode().strip()
            return ancestor, head
        before = event["before"]
        if before != "0" * 40:
            return _git(root, "rev-parse", "--verify", f"{before}^{{commit}}").decode().strip(), None
    # An unborn repository is an empty baseline, not a skipped check.
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True
    )
    if head.returncode:
        symbolic = _git(root, "symbolic-ref", "HEAD").decode().strip()
        if symbolic:
            return None, None
    ancestor = _git(root, "merge-base", "origin/main", "HEAD").decode().strip()
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=root, capture_output=True
    )
    if ancestor == head.stdout.decode().strip() and branch.stdout.strip() in {b"", b"main"}:
        # Main itself still checks the last change, rather than an empty diff.
        ancestor = _git(root, "rev-parse", "--verify", "HEAD^1").decode().strip()
    return ancestor, None


def _blobs(root: Path, revision: str | None, paths: Sequence[str]) -> dict[str, bytes]:
    if revision is None:
        return {}
    output = _git(
        root, "cat-file", "--batch",
        input=b"".join(f"{revision}:{path}\n".encode() for path in paths),
    )
    blobs: dict[str, bytes] = {}
    offset = 0
    for path in paths:
        end = output.index(b"\n", offset)
        header = output[offset:end]
        offset = end + 1
        if header.endswith(b" missing"):
            continue
        size = int(header.rsplit(b" ", 1)[1])
        blobs[path] = output[offset : offset + size]
        offset += size + 1
    return blobs


def _lines(content: bytes) -> int:
    return content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))


def _entries(current: bytes, previous: bytes) -> tuple[dict[tuple[str, str], int], list[str]]:
    old_lines = set(previous.splitlines())
    allowances: dict[tuple[str, str], int] = {}
    issues: list[str] = []
    for number, line in enumerate(current.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            valid = (
                isinstance(entry, dict)
                and set(entry) == {"rule", "path", "limit", "reason"}
                and entry["rule"] in {"root-growth", "new-file", "large-file-growth"}
                and isinstance(entry["path"], str)
                and isinstance(entry["limit"], int)
                and not isinstance(entry["limit"], bool)
                and entry["limit"] >= 0
                and isinstance(entry["reason"], str)
                and bool(entry["reason"].strip())
            )
            if not valid:
                raise ValueError("invalid fields")
        except (ValueError, TypeError):
            issues.append(
                f"{LEDGER}:{number}: 1 invalid ledger entry; replace line {number} with "
                '{"rule":"root-growth","path":"tests","limit":1000,'
                '"reason":"State the concrete need"} or delete that line'
            )
            continue
        if line not in old_lines:
            key = (entry["rule"], entry["path"])
            allowances[key] = max(allowances.get(key, 0), entry["limit"])
    return allowances, issues


def growth_issues(root: Path, base: str | None = None) -> list[str]:
    """Check changed Python files and only ledger allowances added by this diff."""
    try:
        ancestor, candidate = _scope(root, base)
        if ancestor is None:
            changed = set(_git(root, "ls-files", "-z", "--", *PYTHON_PATHS).decode().split("\0"))
        else:
            changed = set(
                _git(root, "diff", "--name-only", "-z", "--no-renames", ancestor,
                     *([candidate] if candidate else []), "--", *PYTHON_PATHS).decode().split("\0")
            )
        paths = sorted(
            path for path in changed
            if path.endswith(".py") and any(path.startswith(f"{name}/") for name in ROOT_LIMITS)
        )
        before = _blobs(root, ancestor, [*paths, LEDGER])
        if candidate:
            after = _blobs(root, candidate, [*paths, LEDGER])
        else:
            after = {}
            for path in [*paths, LEDGER]:
                file = root / path
                if file.is_file():
                    after[path] = file.read_bytes()
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode().strip() or detail
        return [
            "Python growth scope unavailable (1 error): " + detail + "; "
            "run `git fetch origin main --unshallow` for a shallow checkout, or "
            "`git fetch origin main` for a full checkout; CI lint checkout needs `fetch-depth: 0`"
        ]
    allowances, issues = _entries(after.get(LEDGER, b""), before.get(LEDGER, b""))

    def require(rule: str, path: str, actual: int, limit: int, unit: str) -> None:
        allowed = max(limit, allowances.get((rule, path), 0))
        if actual <= allowed:
            return
        entry = json.dumps(
            {"rule": rule, "path": path, "limit": actual, "reason": "State the concrete need"},
            separators=(",", ":"),
        )
        issues.append(
            f"{rule}: {path} has {actual} {unit}; limit {allowed}; "
            f"remove {actual - allowed} {unit}, or append to {LEDGER}: {entry} "
            "(replace the reason with the concrete need)"
        )

    growth = dict.fromkeys(ROOT_LIMITS, 0)
    for path in paths:
        name = next(name for name in ROOT_LIMITS if path.startswith(f"{name}/"))
        old = _lines(before.get(path, b""))
        new = _lines(after.get(path, b""))
        growth[name] += new - old
        if path not in after:
            continue
        if path not in before:
            require("new-file", path, new, NEW_FILE_LIMITS[name], "lines")
        elif name in {"src/evallab", "tests"} and old > LARGE_FILE_LINES:
            require("large-file-growth", path, new - old, 0, "net lines")
    for name, actual in growth.items():
        require("root-growth", name, actual, ROOT_LIMITS[name], "net lines")
    try:
        budget_issues = _budget_issues(root, ancestor, candidate, before, after)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        budget_issues = [f"Total-size budget measurement unavailable: {exc}"]
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        for issue in budget_issues:
            print(f"governance budget report-only: {issue}")
    else:
        issues.extend(budget_issues)
    return issues
