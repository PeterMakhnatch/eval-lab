"""Developer fast-loop command: map changed files to affected test modules.

Maps git diff / working tree changes to the minimal set of pytest modules
that cover the touched functionality, avoiding whole-suite runs during
local development loops.
"""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FULL_SUITE_PATTERNS = (
    ".github/",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    "sql/",
    "tests/conftest.py",
)

DOCS_PATTERNS = (
    "docs/",
    "agents/",
    ".omp/skills/",
    ".claude/skills/",
    "skills/",
)

CONFIG_SUFFIXES = {
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".cfg",
    ".sh",
    ".env",
}


@dataclass(frozen=True)
class DevloopPlan:
    changed: list[str]
    modules: list[str]
    command: str
    reasons: dict[str, list[str]]
    lane: str


def get_changed_files(root: Path, since: str = "working") -> list[str]:
    """Return relative paths of files modified according to `since`."""
    root_path = Path(root).resolve()
    if since == "working" or since is None or since == "":
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked_res = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = diff_res.stdout.splitlines() + untracked_res.stdout.splitlines()
    else:
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", since],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_res.returncode != 0:
            print(
                f"error: git diff failed for revision {since!r}: {diff_res.stderr.strip()}",
                file=sys.stderr,
            )
            return []
        lines = diff_res.stdout.splitlines()

    return sorted({line.strip() for line in lines if line.strip()})


def is_full_suite_trigger(path_str: str) -> bool:
    """Return True if changed path requires running the full test suite."""
    norm = path_str.replace("\\", "/")
    if any(norm == p or norm.startswith(p) for p in FULL_SUITE_PATTERNS):
        return True
    p = PurePosixPath(norm)
    return len(p.parts) == 1 and p.suffix in CONFIG_SUFFIXES


def is_doc_path(path_str: str) -> bool:
    """Return True if path is documentation-only."""
    norm = path_str.replace("\\", "/")
    if norm in {"AGENTS.md", "README.md"}:
        return True
    if norm.endswith(".md"):
        if any(norm.startswith(d) for d in DOCS_PATTERNS):
            return True
        if "/" not in norm:
            return True
    return False


def is_test_path(path_str: str) -> bool:
    """Return True if path is a pytest test file."""
    norm = path_str.replace("\\", "/")
    return norm.startswith("tests/") and norm.endswith(".py") and not norm.endswith("conftest.py")


def is_src_path(path_str: str) -> bool:
    """Return True if path is an evallab source file."""
    norm = path_str.replace("\\", "/")
    return norm.startswith("src/evallab/") and norm.endswith(".py")


def src_path_to_module(path_str: str) -> str:
    """Convert a src/evallab relative path to its dotted Python module name."""
    norm = path_str.replace("\\", "/")
    if not (norm.startswith("src/") and norm.endswith(".py")):
        raise ValueError(f"Path is not a src python file: {path_str}")
    inner = norm[len("src/") : -len(".py")]
    if inner.endswith("/__init__"):
        inner = inner[: -len("/__init__")]
    return inner.replace("/", ".")


def test_imports_or_references(test_path: Path, target_module: str) -> bool:
    """Check if test_path imports or string-references target_module."""
    try:
        content = test_path.read_text(encoding="utf-8")
    except Exception:
        return False

    if target_module in content:
        return True

    try:
        tree = ast.parse(content, filename=str(test_path))
    except Exception:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module or alias.name.startswith(target_module + "."):
                    return True
                if target_module == "evallab" and alias.name.startswith("evallab"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                if node.module == target_module or node.module.startswith(target_module + "."):
                    return True
                if target_module.startswith(node.module + "."):
                    for alias in node.names:
                        full = f"{node.module}.{alias.name}"
                        if full == target_module or full.startswith(target_module + "."):
                            return True
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and target_module in node.value
        ):
            return True

    return False


def _is_unclassified(path_str: str) -> bool:
    """Check if file is unknown / unclassified configuration or root asset."""
    return is_full_suite_trigger(path_str) or not (
        is_doc_path(path_str) or is_test_path(path_str) or is_src_path(path_str)
    )


def affected_test_modules(changed: Sequence[str], root: Path) -> list[str]:
    """Pure function mapping changed file paths to affected test modules."""
    if not changed:
        return []

    root_path = Path(root).resolve()

    # 1. Check if any file triggers full suite or is an unknown config change
    for path_str in changed:
        if is_full_suite_trigger(path_str) or _is_unclassified(path_str):
            return ["__all__"]

    # 2. Check if all changes are documentation-only
    if all(is_doc_path(p) for p in changed):
        return []

    # 3. Collect affected test modules
    test_dir = root_path / "tests"
    available_tests = sorted(test_dir.glob("**/test_*.py")) if test_dir.is_dir() else []

    affected: set[str] = set()

    for path_str in changed:
        norm = path_str.replace("\\", "/")
        if is_test_path(norm):
            affected.add(norm)
        elif is_src_path(norm):
            target_mod = src_path_to_module(norm)
            for test_file in available_tests:
                rel = test_file.relative_to(root_path).as_posix()
                if test_imports_or_references(test_file, target_mod):
                    affected.add(rel)

    return sorted(affected)


def plan_devloop(changed: Sequence[str], root: Path) -> DevloopPlan:
    """Compute the DevloopPlan including command and reasons."""
    root_path = Path(root).resolve()
    modules = affected_test_modules(changed, root_path)
    reasons: dict[str, list[str]] = {}

    if modules == ["__all__"]:
        lane = "full"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider"
        triggering = [p for p in changed if is_full_suite_trigger(p) or _is_unclassified(p)]
        reasons["__all__"] = [f"Global or configuration change: {', '.join(triggering)}"]
    elif not modules:
        if changed and all(is_doc_path(p) for p in changed):
            lane = "docs_consumer"
            command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider -m docs_consumer"
            reasons["docs_consumer"] = [f"Documentation-only change: {', '.join(changed)}"]
        else:
            lane = "none"
            command = ""
    else:
        lane = "focused"
        command = f"uv run --no-sync pytest -q -n0 -p no:cacheprovider {' '.join(modules)}"
        test_dir = root_path / "tests"
        available_tests = sorted(test_dir.glob("**/test_*.py")) if test_dir.is_dir() else []
        for path_str in changed:
            norm = path_str.replace("\\", "/")
            if is_test_path(norm):
                reasons.setdefault(norm, []).append("Direct test file modification")
            elif is_src_path(norm):
                target_mod = src_path_to_module(norm)
                for test_file in available_tests:
                    rel = test_file.relative_to(root_path).as_posix()
                    if rel in modules and test_imports_or_references(test_file, target_mod):
                        reasons.setdefault(rel, []).append(f"Imports {target_mod} ({norm})")

    return DevloopPlan(
        changed=list(changed),
        modules=modules,
        command=command,
        reasons=reasons,
        lane=lane,
    )


def run_devloop(
    root: Path,
    *,
    since: str = "working",
    run: bool = False,
    as_json: bool = False,
) -> int:
    """Execute devloop resolution and optionally stream pytest run."""
    root_path = Path(root).resolve()
    changed = get_changed_files(root_path, since=since)
    plan = plan_devloop(changed, root_path)

    if as_json:
        payload = {
            "changed": plan.changed,
            "modules": plan.modules,
            "command": plan.command,
        }
        print(json.dumps(payload, indent=2))
        if run and plan.command:
            proc = subprocess.run(shlex.split(plan.command), cwd=root_path)
            return proc.returncode
        return 0

    # Human-readable output
    if not plan.changed:
        print("devloop: no working tree changes detected.")
        return 0

    print(f"devloop: {len(plan.changed)} changed file(s) detected.")
    if plan.lane == "full":
        print("devloop: full test suite required.")
        for r in plan.reasons.get("__all__", []):
            print(f"  - {r}")
    elif plan.lane == "docs_consumer":
        print("devloop: documentation changes detected; using docs_consumer lane.")
        for r in plan.reasons.get("docs_consumer", []):
            print(f"  - {r}")
    elif plan.lane == "focused":
        print(f"devloop: {len(plan.modules)} affected test module(s):")
        for mod in plan.modules:
            mod_reasons = plan.reasons.get(mod, [])
            reasons_str = "; ".join(mod_reasons) if mod_reasons else "Affected"
            print(f"  - {mod} ({reasons_str})")

    if plan.command:
        print(f"\nCommand:\n  {plan.command}")

    if run:
        if not plan.command:
            print("devloop: no test command to execute.")
            return 0
        print("\nExecuting command...\n")
        proc = subprocess.run(shlex.split(plan.command), cwd=root_path)
        return proc.returncode

    return 0
