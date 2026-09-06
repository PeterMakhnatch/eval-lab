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
    "research/inbox/board.md": ("# Board — pull, don't push", "# house rules"),
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
    """Check the canonical pickup counter and any explicitly linked live handoffs."""
    issues: list[str] = []
    handoffs = root / "agents/handoffs"
    claims = root / "research/inbox/claims"
    if not claims.is_dir():
        issues.append("missing pickup counter: research/inbox/claims")
    if not handoffs.is_dir():
        issues.append("missing live handoff directory: agents/handoffs")
    claimants: dict[str, str] = {}
    linked: dict[str, str] = {}
    for claim in sorted(claims.glob("*.md")):
        if claim.name == "README.md":
            continue
        relative = claim.relative_to(root).as_posix()
        fields: dict[str, str] = {}
        for line in claim.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                key = key.strip()
                if key in fields:
                    issues.append(f"{relative}: duplicate claim field {key!r}")
                fields[key] = value.strip()
        for required in ("item", "role", "why-me"):
            if not fields.get(required):
                issues.append(f"{relative}: missing non-empty claim field {required!r}")
        claimant = claim.stem.split("-", 1)[0]
        if claimant in claimants:
            issues.append(f"{relative}: multiple open claims for {claimant}: {claimants[claimant]}")
        claimants[claimant] = relative
        if "handoff" in fields:
            handoff = fields["handoff"]
            if not re.fullmatch(r"agents/handoffs/[^/]+\.md", handoff):
                issues.append(f"{relative}: handoff must name a live agents/handoffs/*.md path")
                continue
            if handoff in linked:
                issues.append(f"{relative}: handoff already claimed by {linked[handoff]}")
            linked[handoff] = relative
            if not (root / handoff).is_file():
                issues.append(f"{relative}: live handoff does not exist: {handoff}")
    for path in sorted(handoffs.glob("*.md")):
        relative = path.relative_to(root).as_posix()
        if relative not in linked:
            issues.append(f"{relative}: live handoff has no pickup-counter claim")
        lines = path.read_text(encoding="utf-8").splitlines()
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
