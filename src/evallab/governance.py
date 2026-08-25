"""Deterministic checks for the repository coordination contract.

Entry point: ``python -m evallab.governance check``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

REQUIRED_DOCUMENTS: dict[str, tuple[str, ...]] = {
    "agents/missions/ACTIVE.md": ("# Mission board", "## Now", "## Missions"),
    "agents/missions/TEMPLATE.md": ("# Mission template", "| Exclusive paths |", "| State |"),
    "agents/STRUCTURE.md": ("# Repository structure", "## The map", "## Placement guide"),
    "agents/WORKFLOW.md": (
        "# Agent workflow",
        "## The handoff file",
        "Status: ready | building | blocked | review-wanted | done",
    ),
    "agents/CHECKS.md": ("# Definition of Green", "## CI contract", "## Merge rule"),
}
HEADER_PREFIXES = ("Status: ", "Last: ", "Next: ", "Blockers: ")
LIVE_STATUSES = frozenset({"ready", "building", "blocked", "review-wanted"})
_ROOT_LINE = re.compile(r"^[├└]──\s+([^\s]+)")
_TABLE_SEPARATOR = re.compile(r":?-{3,}:?")
_BOARD_STATUS_TOKEN = re.compile(r"[a-z]+")
_LIVE_BOARD_STATUSES = frozenset({"ready", "active", "review", "blocked"})
_TERMINAL_BOARD_STATUSES = frozenset({"merged", "candidate"})
_STATUS_MARKUP = "`*_~"
_HANDOFF_PATH = re.compile(r"agents/(?:handoffs|archive/[^/]+)/[^/]+\.md")


def _table_cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _board_status_rows(board_text: str) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
    """Parse all State/Status rows from header-aware Markdown tables."""
    lines = board_text.splitlines()
    rows: list[tuple[int, str, tuple[str, ...]]] = []
    for index in range(len(lines) - 1):
        if not lines[index].startswith("|") or not lines[index + 1].startswith("|"):
            continue
        headers = tuple(cell.casefold() for cell in _table_cells(lines[index]))
        separators = _table_cells(lines[index + 1])
        if len(headers) != len(separators) or not all(
            _TABLE_SEPARATOR.fullmatch(cell) for cell in separators
        ):
            continue
        status_indexes = [
            position for position, header in enumerate(headers) if header in {"state", "status"}
        ]
        if len(status_indexes) != 1:
            continue
        status_index = status_indexes[0]
        row_index = index + 2
        while row_index < len(lines) and lines[row_index].startswith("|"):
            cells = _table_cells(lines[row_index])
            if len(cells) == len(headers):
                normalized = cells[status_index].lstrip(_STATUS_MARKUP).casefold()
                match = _BOARD_STATUS_TOKEN.match(normalized)
                rows.append((row_index + 1, match.group() if match else "", cells))
            row_index += 1
    return tuple(rows)


def _row_handoff_paths(cells: tuple[str, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    for cell in cells:
        value = cell.strip("`")
        if _HANDOFF_PATH.fullmatch(value):
            paths.append(value)
    return tuple(paths)


def declared_roots(structure_text: str) -> frozenset[str]:
    """Return top-level entries declared by the root tree in STRUCTURE.md."""
    in_map = False
    in_tree = False
    roots: set[str] = set()
    for line in structure_text.splitlines():
        if line == "## The map":
            in_map = True
            continue
        if not in_map:
            continue
        if line == "```":
            if not in_tree:
                in_tree = True
                continue
            break
        if not in_tree:
            continue
        match = _ROOT_LINE.match(line)
        if match:
            roots.add(match.group(1).rstrip("/"))
    return frozenset(roots)


def tracked_roots(paths: Iterable[str]) -> frozenset[str]:
    """Collapse tracked repository paths to their top-level entries."""
    return frozenset(path.split("/", 1)[0] for path in paths if path)


def _document_issues(root: Path) -> list[str]:
    issues: list[str] = []
    for relative, required in REQUIRED_DOCUMENTS.items():
        path = root / relative
        if not path.is_file():
            issues.append(f"missing governance document: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for marker in required:
            if marker not in text:
                issues.append(f"{relative}: missing required marker {marker!r}")
    return issues


def _handoff_issues(root: Path) -> list[str]:
    issues: list[str] = []
    handoffs = root / "agents/handoffs"
    if not handoffs.is_dir():
        return ["missing live handoff directory: agents/handoffs"]
    board_path = root / "agents/missions/ACTIVE.md"
    board_text = board_path.read_text(encoding="utf-8") if board_path.is_file() else ""
    board_rows = _board_status_rows(board_text)
    rows_by_path: dict[str, list[tuple[int, str]]] = {}

    for line_number, board_status, cells in board_rows:
        if board_status not in _LIVE_BOARD_STATUSES | _TERMINAL_BOARD_STATUSES:
            rendered = board_status or "<missing>"
            issues.append(
                f"agents/missions/ACTIVE.md:{line_number}: unknown board status "
                f"{rendered!r}"
            )
            continue
        if board_status in _TERMINAL_BOARD_STATUSES:
            continue
        paths = _row_handoff_paths(cells)
        live_paths = tuple(path for path in paths if path.startswith("agents/handoffs/"))
        archive_paths = tuple(path for path in paths if path.startswith("agents/archive/"))
        if archive_paths:
            issues.append(
                f"agents/missions/ACTIVE.md:{line_number}: live {board_status} row "
                "references archived handoff: "
                + ", ".join(archive_paths)
            )
        if len(live_paths) != 1:
            issues.append(
                f"agents/missions/ACTIVE.md:{line_number}: live {board_status} row "
                f"must reference exactly one live handoff path; found {len(live_paths)}"
            )
            continue
        live_path = live_paths[0]
        rows_by_path.setdefault(live_path, []).append((line_number, board_status))
        if not (root / live_path).is_file():
            issues.append(
                f"agents/missions/ACTIVE.md:{line_number}: live handoff does not exist: "
                f"{live_path}"
            )

    for path in sorted(handoffs.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        relative = path.relative_to(root).as_posix()
        matching_rows = rows_by_path.get(relative, [])
        if not matching_rows:
            issues.append(f"{relative}: live handoff is not referenced by a mission board row")
        elif len(matching_rows) > 1:
            issues.append(f"{relative}: live handoff is referenced by multiple mission board rows")
        if len(lines) < 4:
            issues.append(f"{relative}: fewer than four header lines")
            continue
        for index, prefix in enumerate(HEADER_PREFIXES):
            if not lines[index].startswith(prefix) or not lines[index][len(prefix) :].strip():
                issues.append(f"{relative}:{index + 1}: expected non-empty {prefix.strip()}")
        if lines[0].startswith(HEADER_PREFIXES[0]):
            status = lines[0][len(HEADER_PREFIXES[0]) :].strip()
            if status not in LIVE_STATUSES:
                allowed = ", ".join(sorted(LIVE_STATUSES))
                issues.append(
                    f"{relative}: invalid live status {status!r}; expected one of {allowed}"
                )
            if status in LIVE_STATUSES and len(matching_rows) == 1:
                board_status = matching_rows[0][1]
                expected_board_status = {
                    "ready": "ready",
                    "building": "active",
                    "blocked": "blocked",
                    "review-wanted": "review",
                }[status]
                if board_status != expected_board_status:
                    issues.append(
                        f"{relative}: handoff status {status!r} contradicts "
                        f"board status {board_status!r}"
                    )
    return issues


def collect_issues(root: Path, tracked_paths: Iterable[str]) -> list[str]:
    """Return deterministic governance violations without mutating the tree."""
    issues = _document_issues(root)
    structure = root / "agents/STRUCTURE.md"
    if structure.is_file():
        declared = declared_roots(structure.read_text(encoding="utf-8"))
        missing = sorted(tracked_roots(tracked_paths) - declared)
        if missing:
            issues.append("undeclared tracked root entries: " + ", ".join(missing))
    issues.extend(_handoff_issues(root))
    return issues


def git_tracked_paths(root: Path) -> tuple[str, ...]:
    """Read the tracked path set from Git for root-freeze enforcement."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(path for path in result.stdout.decode().split("\0") if path)


def check(root: Path) -> list[str]:
    return collect_issues(root, git_tracked_paths(root))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    issues = check(args.root.resolve())
    if issues:
        for issue in issues:
            print(f"governance check failed: {issue}")
        return 1
    print("governance check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
