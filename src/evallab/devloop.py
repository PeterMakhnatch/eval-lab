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
import tomllib
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
            check=True,
        )
        untracked_res = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = diff_res.stdout.splitlines() + untracked_res.stdout.splitlines()
    else:
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", since],
            cwd=root_path,
            capture_output=True,
            text=True,
            check=True,
        )
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


def is_test_path(path_str: str, test_roots: Sequence[str] = ("tests",)) -> bool:
    """Return True if path is a Python test consumer under a declared test root."""
    path = PurePosixPath(path_str.replace("\\", "/"))
    return (
        path.suffix == ".py"
        and path.name != "conftest.py"
        and any(path.is_relative_to(test_root) for test_root in test_roots)
    )


def _test_roots(root: Path) -> list[str]:
    config_path = root / "pyproject.toml"
    if not config_path.is_file():
        return ["tests"]
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    return (
        config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", ["tests"])
    )


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


def _referenced_modules(test_path: Path, target_modules: set[str]) -> set[str]:
    """Read a consumer once and retain its direct matches for this invocation."""
    try:
        content = test_path.read_text(encoding="utf-8")
    except Exception:
        return set()

    matched = {target for target in target_modules if target in content}
    remaining = target_modules - matched
    if not remaining:
        return matched

    try:
        tree = ast.parse(content, filename=str(test_path))
    except Exception:
        return matched

    for node in ast.walk(tree):
        for target in remaining:
            if isinstance(node, ast.Import):
                if any(
                    alias.name == target
                    or alias.name.startswith(target + ".")
                    or (target == "evallab" and alias.name.startswith("evallab"))
                    for alias in node.names
                ):
                    matched.add(target)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if (
                    node.module == target
                    or node.module.startswith(target + ".")
                    or target.startswith(node.module + ".")
                    and any(
                        f"{node.module}.{alias.name}" == target
                        or f"{node.module}.{alias.name}".startswith(target + ".")
                        for alias in node.names
                    )
                ):
                    matched.add(target)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and target in node.value
            ):
                matched.add(target)
        remaining.difference_update(matched)
        if not remaining:
            break

    return matched


def _is_unclassified(path_str: str, test_roots: Sequence[str] = ("tests",)) -> bool:
    """Check if file is unknown / unclassified configuration or root asset."""
    return is_full_suite_trigger(path_str) or not (
        is_doc_path(path_str) or is_test_path(path_str, test_roots) or is_src_path(path_str)
    )


def affected_test_modules(changed: Sequence[str], root: Path) -> list[str]:
    """Pure function mapping changed file paths to affected test modules."""
    return plan_devloop(changed, root).modules


def plan_devloop(changed: Sequence[str], root: Path) -> DevloopPlan:
    """Compute selection and reasons together from one local consumer inventory."""
    root_path = Path(root).resolve()
    if not changed:
        return DevloopPlan(changed=[], modules=[], command="", reasons={}, lane="none")

    test_roots = _test_roots(root_path)
    # Removed/renamed tests cannot be passed to pytest. Collect the remaining
    # suite instead of silently skipping coverage formerly owned by that file.
    requires_full_suite = any(
        is_full_suite_trigger(path)
        or _is_unclassified(path, test_roots)
        or (is_test_path(path, test_roots) and not (root_path / path.replace("\\", "/")).is_file())
        for path in changed
    )
    reasons: dict[str, list[str]] = {}

    if requires_full_suite:
        modules = ["__all__"]
        lane = "full"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider"
        triggering = [
            p
            for p in changed
            if is_full_suite_trigger(p)
            or _is_unclassified(p, test_roots)
            or (is_test_path(p, test_roots) and not (root_path / p).is_file())
        ]
        reasons["__all__"] = [
            f"Global, configuration, or removed test change: {', '.join(triggering)}"
        ]
    elif all(is_doc_path(p) for p in changed):
        modules = []
        lane = "docs_consumer"
        command = "uv run --no-sync pytest -q -n0 -p no:cacheprovider -m docs_consumer"
        reasons["docs_consumer"] = [f"Documentation-only change: {', '.join(changed)}"]
    else:
        targets = {src_path_to_module(path) for path in changed if is_src_path(path)}
        test_paths: dict[str, Path] = {}
        if targets:
            test_paths = {
                test_file.relative_to(root_path).as_posix(): test_file
                for test_root in test_roots
                for pattern in ("test_*.py", "*_test.py")
                for test_file in (root_path / test_root).rglob(pattern)
                if test_file.is_file()
            }
            # Directly changed consumers need reasons even when their names do
            # not match pytest's discovery patterns.
            test_paths.update(
                {
                    path.replace("\\", "/"): root_path / path.replace("\\", "/")
                    for path in changed
                    if is_test_path(path, test_roots)
                }
            )
        inventory = {
            path: _referenced_modules(path, targets) for path in sorted(set(test_paths.values()))
        }
        consumers = sorted(test_paths)
        for path_str in changed:
            norm = path_str.replace("\\", "/")
            if is_test_path(norm, test_roots):
                reasons.setdefault(norm, []).append("Direct test file modification")
            elif is_src_path(norm):
                target_mod = src_path_to_module(norm)
                for rel in consumers:
                    if target_mod in inventory[test_paths[rel]]:
                        reasons.setdefault(rel, []).append(f"Imports {target_mod} ({norm})")
        modules = sorted(reasons)
        lane = "focused" if modules else "none"
        command = (
            f"uv run --no-sync pytest -q -n0 -p no:cacheprovider {shlex.join(modules)}"
            if modules
            else ""
        )

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
    try:
        changed = get_changed_files(root_path, since=since)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"error: Git change inventory failed for {since!r}: {detail}", file=sys.stderr)
        return 1
    plan = plan_devloop(changed, root_path)

    if as_json:
        payload = {
            "changed": plan.changed,
            "modules": plan.modules,
            "command": plan.command,
            "lane": plan.lane,
            "reasons": plan.reasons,
        }
        print(json.dumps(payload, indent=2), flush=True)
        if run and plan.command:
            proc = subprocess.run(shlex.split(plan.command), cwd=root_path, stdout=sys.stderr)
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
