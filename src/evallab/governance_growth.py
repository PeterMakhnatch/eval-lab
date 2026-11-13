"""PR-local Python growth limits; no network calls or application imports."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

LEDGER = "agents/growth-ledger.jsonl"
ROOT_LIMITS = {"src/evallab": 1000, "tests": 1000, "research": 2000, "library": 1000}
NEW_FILE_LIMITS = {"src/evallab": 600, "tests": 600, "research": 1000, "library": 1000}
LARGE_FILE_LINES = 8000
PYTHON_PATHS = tuple(f"{name}/*.py" for name in ROOT_LIMITS)


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
    return issues
