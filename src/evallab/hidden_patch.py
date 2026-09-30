"""Read a MiMo code task's hidden test patch (``tests/test.patch``).

The patch adds the hidden tests plus harness files. The modules its test
files import name the project under test: a missing submodule of one of
them under a nop is the code the agent is asked to write, not a grader
defect.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

#: Files the MiMo harness adds to every code task; not hidden tests.
HARNESS_FILES = frozenset({"mimo_test_command.sh", "test_commands.json"})

#: Top-level modules a hidden test imports that are not the project.
EXCLUDED_IMPORTS = frozenset(
    {
        "pytest",
        "mock",
        "hypothesis",
        "numpy",
        "pandas",
        "requests",
        "yaml",
        "six",
        "attr",
        "attrs",
        "pydantic",
        "sqlalchemy",
        "django",
        "flask",
        "typing_extensions",
        "tests",
        "test",
        "conftest",
    }
)

#: Patch path directories that name a test layout, not a project.
GENERIC_DIRS = frozenset(
    {
        "tests",
        "test",
        "testing",
        "unittests",
        "unit_tests",
        "usercase-test-coderl",
        "src",
        "lib",
        "python",
        "t",
        "spec",
        "specs",
    }
)
#: Layout roots whose next directory is the package (``src/<pkg>/…``).
_PACKAGE_ROOTS = frozenset({"src", "lib", "python"})


def patch_sections(patch_text: str) -> list[tuple[str, str]]:
    """``(path, section text)`` for every ``diff --git`` file section."""
    sections: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?m)^diff --git a/(\S+) b/\S+.*?(?=^diff --git |\Z)", patch_text, re.S
    ):
        sections.append((match.group(1), match.group(0)))
    return sections


def added_lines(section: str) -> list[str]:
    """Added source lines of one patch section, hunk headers excluded."""
    lines: list[str] = []
    in_hunk = False
    for raw in section.splitlines():
        if raw.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:])
    return lines


def top_level_imports(line: str) -> list[str]:
    """Top-level module names one ``import``/``from … import`` line names."""
    stripped = line.strip()
    if stripped.startswith("from "):
        module = stripped[len("from ") :].split(" import ", 1)[0].strip()
        top = module.split(".")[0]
        return [top] if top and not top.startswith(".") else []
    if not stripped.startswith("import "):
        return []
    found: list[str] = []
    for part in stripped[len("import ") :].split("#", 1)[0].split(","):
        name = part.strip().split()[0] if part.strip() else ""
        top = name.split(".")[0]
        if top and top != "as":
            found.append(top)
    return found


def imported_modules(sections: Sequence[tuple[str, str]]) -> list[str]:
    """Top-level modules the patch's added Python lines import, in order.

    Context lines are not read: an existing file's unchanged imports are
    mostly fixtures and mocks (``respx``, ``matplotlib``), and would outrank
    the package directory the test file lives in as the project key.
    """
    modules: list[str] = []
    for path, section in sections:
        if Path(path).name in HARNESS_FILES or not str(path).endswith(".py"):
            continue
        for line in added_lines(section):
            modules.extend(top_level_imports(line))
    return modules


def excluded_module(name: str) -> bool:
    """A stdlib or common third-party module: never the project under test."""
    return name in EXCLUDED_IMPORTS or name in sys.stdlib_module_names


def path_packages(sections: Sequence[tuple[str, str]]) -> list[str]:
    """Package directories the patched ``.py`` files live in, in order.

    ``colour/difference/tests/test_delta_e.py`` names ``colour``;
    ``src/pkg/tests/test_x.py`` names ``pkg``; a generic layout directory
    (``tests/``) names nothing.
    """
    packages: list[str] = []
    for path, _section in sections:
        if Path(path).name in HARNESS_FILES or not str(path).endswith(".py"):
            continue
        parts = Path(path).parts
        if len(parts) > 2 and parts[0] in _PACKAGE_ROOTS:
            packages.append(parts[1])
        elif len(parts) > 1 and parts[0] not in GENERIC_DIRS:
            packages.append(parts[0])
    return packages


def project_modules(task_dir: Path) -> frozenset[str]:
    """Top-level project modules of a task (empty when the patch is unreadable).

    The modules its hidden tests import, plus the package directories the
    patched test files live in (a parenthesised import's added names carry
    no module), stdlib and common third-party names excluded.
    """
    try:
        patch_text = (task_dir / "tests" / "test.patch").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return frozenset()
    sections = patch_sections(patch_text)
    return frozenset(
        name
        for name in imported_modules(sections) + path_packages(sections)
        if not excluded_module(name)
    )
