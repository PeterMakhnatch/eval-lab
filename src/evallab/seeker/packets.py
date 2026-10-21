"""Workspaces (task view minus the reference solution) and submission validation."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

HOME = Path.home()
DEFAULT_VIEW_ROOT = (
    HOME / "Developer/eval-lab-results/2026-10-20/tb4-audit-v1/agent-inputs/dev/tasks"
)
DEFAULT_UPSTREAM_ROOT = (
    HOME / "Developer/eval-lab-data/tb4-20261020/upstream-git-v4.0.0/terminal-bench"
)
EXCLUDED_TOP = {"solution", "cheat"}
MAX_SUBMISSION_BYTES = 2_000_000


def dev_tasks(view_root: Path = DEFAULT_VIEW_ROOT) -> list[str]:
    return sorted(p.name for p in view_root.iterdir() if p.is_dir())


def _ignore_top(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDED_TOP}


def build_workspace(task: str, view_root: Path, workspace: Path) -> dict[str, Any]:
    """Copy the blind task view (no reference solution) into ``workspace/task``."""
    source = view_root / task
    if not source.is_dir():
        raise FileNotFoundError(f"task view missing: {source}")
    if workspace.exists():
        raise FileExistsError(f"workspace already exists: {workspace}")
    workspace.mkdir(parents=True)
    shutil.copytree(source, workspace / "task", ignore=_ignore_top)
    (workspace / "submission").mkdir()
    files = [
        {"path": p.relative_to(workspace / "task").as_posix(), "bytes": p.stat().st_size}
        for p in sorted((workspace / "task").rglob("*"))
        if p.is_file()
    ]
    return {"task": task, "source_view": str(source), "files": files, "file_count": len(files)}


def _file_rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append({"path": rel, "symlink": True})
        elif path.is_file():
            rows.append(
                {
                    "path": rel,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    return rows


def tree_digest(rows: list[dict[str, Any]]) -> str:
    payload = "".join(f"{r['path']}\0{r.get('sha256', 'symlink')}\n" for r in rows)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def validate_submission(submission: Path) -> dict[str, Any]:
    """Structural checks only: presence, size, links, bash syntax, notes schema."""
    problems: list[str] = []
    if not submission.is_dir():
        return {"valid": False, "problems": ["submission directory missing"], "files": []}
    rows = _file_rows(submission)
    if any(r.get("symlink") for r in rows):
        problems.append("symlinks are not allowed")
    total = sum(r.get("bytes", 0) for r in rows)
    if total > MAX_SUBMISSION_BYTES:
        problems.append(f"submission is {total} bytes, over the {MAX_SUBMISSION_BYTES} cap")
    solve = submission / "solve.sh"
    notes: dict[str, Any] | None = None
    if not solve.is_file():
        problems.append("solve.sh missing")
    else:
        proc = subprocess.run(
            ["bash", "-n", str(solve)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            problems.append(f"bash -n failed: {proc.stderr.strip()[:300]}")
    notes_path = submission / "notes.json"
    if not notes_path.is_file():
        problems.append("notes.json missing")
    else:
        try:
            notes = json.loads(notes_path.read_text())
            for field in ("exploit_found", "mechanism", "class", "confidence"):
                if field not in notes:
                    problems.append(f"notes.json missing field {field}")
        except (ValueError, OSError) as exc:
            problems.append(f"notes.json unreadable: {exc}")
    return {
        "valid": not problems,
        "problems": problems,
        "files": rows,
        "bytes": total,
        "digest": tree_digest(rows),
        "notes": notes,
    }
