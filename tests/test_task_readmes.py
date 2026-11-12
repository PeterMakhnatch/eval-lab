"""Every task package README carries the standard four sections."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_LIBRARY = REPO_ROOT / "library" / "tasks"

REQUIRED_SECTIONS: tuple[str, ...] = ("Environment", "Verifier", "Layout", "Run")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# Packages whose admission records bind every package byte, README.md included.
# The frozen gym-v1 manifest pins four of them, and the two m049-v1 certificates
# cannot be reissued because their workbench producer is retired. A README in one
# of these packages fails `evallab registry audit` and `tests/test_gym_freeze.py`
# until the task is re-admitted under a new version. Remove an entry only with
# that re-admission.
PENDING_READMES: tuple[str, ...] = (
    "event-summary",
    "experimental/deepplanning-v1/travel-lisbon-002",
    "experimental/syn-funcdag-easy",
    "query-optimize",
    "terminal-bench-html-js-filter",
    "transaction-reconciliation",
)

PACKAGES: tuple[Path, ...] = tuple(
    package
    for package in sorted(p.parent for p in TASK_LIBRARY.rglob("task.toml"))
    if package.relative_to(TASK_LIBRARY).as_posix() not in PENDING_READMES
)
PACKAGE_IDS: list[str] = [p.relative_to(TASK_LIBRARY).as_posix() for p in PACKAGES]


def h2_headings(text: str) -> list[str]:
    """Return the level-two headings outside fenced code blocks, in document order."""
    headings: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            headings.append(line[3:].strip())
    return headings


@pytest.mark.parametrize("package", PACKAGES, ids=PACKAGE_IDS)
def test_readme_has_standard_sections(package: Path) -> None:
    name = package.relative_to(TASK_LIBRARY).as_posix()
    readme = package / "README.md"
    assert readme.is_file(), f"{name}: missing README.md"
    headings = set(h2_headings(readme.read_text(encoding="utf-8")))
    missing = [section for section in REQUIRED_SECTIONS if section not in headings]
    assert not missing, f"{name}: README.md is missing section(s): {', '.join(missing)}"
